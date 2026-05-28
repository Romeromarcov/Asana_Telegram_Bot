"""
Bot de Telegram para seguimiento de tareas de Asana — Lubrikca
Versión 3.0 — Botones interactivos + Crear tareas + Tareas recurrentes
"""

import asyncio
import os
import json
import logging
from datetime import datetime, time, date, timedelta
from pathlib import Path
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)

from utils import ASANA_BASE, load_team, http_client
from mover_tareas import (
    get_task_projects, get_workspace_projects,
    get_project_sections, move_task_to_section,
    get_task_current_section
)
from asana_projects import (
    ensure_member_project, add_task_to_member_project,
    move_task_status, add_task_comment, get_member_project,
    STANDARD_SECTIONS,
)
from team_manager import add_member, remove_member
from minuta import (
    call_gemini, enrich_tasks, format_tasks_preview,
    tasks_need_fixing, next_incomplete_idx,
    GeminiError, build_minuta_record, save_minuta,
)
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── CONFIGURACIÓN ──────────────────────────────────────────────────────────────
def _require_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise SystemExit(
            f"\n❌ Variable de entorno requerida no encontrada: {key}\n"
            f"   Agrégala en Railway → servicio Worker → Variables\n"
        )
    return val

TELEGRAM_TOKEN  = _require_env("TELEGRAM_TOKEN")
ASANA_TOKEN     = _require_env("ASANA_TOKEN")
ASANA_WORKSPACE = _require_env("ASANA_WORKSPACE_ID")
MANAGER_CHAT_ID = int(_require_env("MANAGER_CHAT_ID"))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Nota: el cliente Gemini se crea en minuta.py con google-genai SDK (gemini-2.5-flash)

# Archivos de datos
RECURRING_FILE   = Path(__file__).parent / "recurring.json"
KNOWN_TASKS_FILE = Path(__file__).parent / "known_tasks.json"

# ── Config desde variables de entorno ─────────────────────────────────────────
# Los cambios de configuración del panel web se aplican vía env vars en Railway.
# ── Configuración: env vars como valores por defecto; la DB puede sobreescribir ──
# La función _load_config() lee de kv_store primero (guardado desde el dashboard)
# y cae en las env vars si no hay nada en DB. Así no se necesita redeploy.

def _load_config() -> dict:
    """Lee config desde DB (prioridad) o env vars (fallback)."""
    cfg = {}
    try:
        from db import db_get
        db_cfg = db_get("config") or {}
        cfg = {k: v for k, v in db_cfg.items() if v != "" and v is not None}
    except Exception:
        pass
    def _int(key, default):
        return int(cfg.get(key, os.environ.get(key, default)))
    def _str(key, default):
        return str(cfg.get(key, os.environ.get(key, default)))
    return {
        "TIMEZONE":               _str("TIMEZONE",               "America/Caracas"),
        "MORNING_HOUR":           _int("MORNING_HOUR",            "9"),
        "MORNING_MIN":            _int("MORNING_MIN",             "0"),
        "AFTERNOON_HOUR":         _int("AFTERNOON_HOUR",          "15"),
        "AFTERNOON_MIN":          _int("AFTERNOON_MIN",           "0"),
        "REPORT_HOUR":            _int("REPORT_HOUR",             "18"),
        "REPORT_MIN":             _int("REPORT_MIN",              "0"),
        "EVENING_HOUR":           _int("EVENING_HOUR",            "18"),
        "EVENING_MIN":            _int("EVENING_MIN",             "0"),
        "CHECK_INTERVAL_MINUTES": _int("CHECK_INTERVAL_MINUTES",  "5"),
    }

_cfg = _load_config()
TIMEZONE               = _cfg["TIMEZONE"]
MORNING_HOUR           = _cfg["MORNING_HOUR"]
MORNING_MIN            = _cfg["MORNING_MIN"]
AFTERNOON_HOUR         = _cfg["AFTERNOON_HOUR"]
AFTERNOON_MIN          = _cfg["AFTERNOON_MIN"]
REPORT_HOUR            = _cfg["REPORT_HOUR"]
REPORT_MIN             = _cfg["REPORT_MIN"]
EVENING_HOUR           = _cfg["EVENING_HOUR"]
EVENING_MIN            = _cfg["EVENING_MIN"]
CHECK_INTERVAL_MINUTES = _cfg["CHECK_INTERVAL_MINUTES"]

TZ = pytz.timezone(TIMEZONE)

# Referencias a los jobs diarios para poder reprogramarlos sin reiniciar
_daily_jobs: dict = {}
_config_snapshot: str = ""   # hash del último config aplicado

# ── Persistencia de known_tasks (evita re-notificar al reiniciar) ─────────────
def load_known_tasks() -> dict[str, set]:
    if not KNOWN_TASKS_FILE.exists():
        return {}
    try:
        data = json.loads(KNOWN_TASKS_FILE.read_text(encoding="utf-8"))
        return {k: set(v) for k, v in data.items()}
    except Exception:
        return {}

def save_known_tasks():
    try:
        data = {k: list(v) for k, v in known_tasks.items()}
        KNOWN_TASKS_FILE.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"No se pudo guardar known_tasks: {e}")

# Memoria de tareas conocidas {asana_gid: set(task_gids)} — persiste entre reinicios
known_tasks: dict[str, set] = load_known_tasks()

# Estados de ConversationHandlers
(
    # Crear tarea (manager asigna a otro)
    TASK_ASSIGNEE,
    TASK_DUE,
    TASK_DUE_CUSTOM,
    TASK_RECURRING,
    TASK_FREQ,
    TASK_TIMES_PER_DAY,
    TASK_HOURS,
    TASK_WEEKDAY,
    # Crear tarea propia (cualquier usuario)
    SELF_TASK_NAME,
    SELF_TASK_DUE,
    SELF_TASK_DUE_CUSTOM,
    # Minuta
    MINUTA_WAIT,
    MINUTA_REVIEW,
    MINUTA_FIX_ASSIGN,
    MINUTA_FIX_DATE,
    # Agregar miembro
    TEAM_ADD_NAME,
    TEAM_ADD_TGID,
    TEAM_ADD_ASANA,
    # Descripción opcional
    TASK_NOTES,
    SELF_TASK_NOTES,
    # Área de trabajo (selección)
    AREA_TASK_AREA,
    # Solicitud de delegación al manager (líder)
    LEADER_REQ_TASK,
) = range(22)

def get_members(team: dict) -> list:
    """Devuelve miembros del equipo sin el manager."""
    return [(tid, info) for tid, info in team.items() if tid != MANAGER_CHAT_ID]

# ── TAREAS RECURRENTES — PERSISTENCIA ─────────────────────────────────────────

def load_recurring() -> list:
    """Carga tareas recurrentes. Prioridad: PostgreSQL → archivo local."""
    from db import db_get
    db_data = db_get("recurring")
    if db_data is not None:
        return db_data
    if not RECURRING_FILE.exists():
        return []
    try:
        return json.loads(RECURRING_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_recurring(data: list):
    """Guarda tareas recurrentes en archivo local y en PostgreSQL."""
    RECURRING_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    from db import db_set
    db_set("recurring", data)

def add_recurring(config: dict):
    data = load_recurring()
    data.append(config)
    save_recurring(data)

def update_recurring(idx: int, config: dict):
    data = load_recurring()
    if 0 <= idx < len(data):
        data[idx] = config
        save_recurring(data)

# ── ASANA API ──────────────────────────────────────────────────────────────────

async def asana_get(path: str, params: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {ASANA_TOKEN}"}
    r = await http_client.get(f"{ASANA_BASE}{path}", headers=headers, params=params)
    r.raise_for_status()
    return r.json()

async def asana_post(path: str, data: dict) -> dict:
    headers = {"Authorization": f"Bearer {ASANA_TOKEN}", "Content-Type": "application/json"}
    r = await http_client.post(f"{ASANA_BASE}{path}", headers=headers, json={"data": data})
    r.raise_for_status()
    return r.json()

async def asana_put(path: str, data: dict) -> dict:
    headers = {"Authorization": f"Bearer {ASANA_TOKEN}", "Content-Type": "application/json"}
    r = await http_client.put(f"{ASANA_BASE}{path}", headers=headers, json={"data": data})
    r.raise_for_status()
    return r.json()

async def get_pending_tasks(asana_gid: str) -> list:
    params = {
        "assignee": asana_gid,
        "workspace": ASANA_WORKSPACE,
        "completed_since": "now",
        "opt_fields": "gid,name,due_on",
    }
    return (await asana_get("/tasks", params)).get("data", [])

async def get_completed_tasks_today(asana_gid: str) -> list:
    """
    Devuelve las tareas completadas HOY por asana_gid.
    Asana filtra con completed_since=<inicio_de_hoy> y retorna tareas
    completadas desde esa hora; luego filtramos las que completed=True.
    """
    today_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    today_iso   = today_start.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    params = {
        "assignee":        asana_gid,
        "workspace":       ASANA_WORKSPACE,
        "completed_since": today_iso,
        "opt_fields":      "gid,name,due_on,completed,completed_at",
    }
    data = (await asana_get("/tasks", params)).get("data", [])
    return [t for t in data if t.get("completed")]

async def create_asana_task(name: str, assignee_gid: str, due_on: str = None,
                             recurrence: str = None, notes: str = None) -> dict:
    """Crea una tarea en Asana. recurrence: 'daily'|'weekly'|'monthly' para nativa."""
    data = {
        "name": name,
        "assignee": assignee_gid,
        "workspace": ASANA_WORKSPACE,
    }
    if due_on:
        data["due_on"] = due_on
    if recurrence in ("daily", "weekly", "monthly"):
        data["recurrence"] = {"period": recurrence}
    if notes:
        data["notes"] = notes
    result = await asana_post("/tasks", data)
    return result.get("data", {})

async def complete_task(task_gid: str) -> bool:
    try:
        await asana_put(f"/tasks/{task_gid}", {"completed": True})
        return True
    except Exception as e:
        logger.error(f"Error completando tarea {task_gid}: {e}")
        return False

# ── UTILIDADES ─────────────────────────────────────────────────────────────────

def is_overdue(task: dict) -> bool:
    if not task.get("due_on"):
        return False
    return datetime.strptime(task["due_on"], "%Y-%m-%d").date() < datetime.now(TZ).date()

def get_first_name(name: str) -> str:
    return name.split()[0]

def due_label(due_on: str) -> str:
    if not due_on:
        return "sin fecha"
    d = datetime.strptime(due_on, "%Y-%m-%d").date()
    today = datetime.now(TZ).date()
    if d == today:
        return "hoy"
    if d == today + timedelta(days=1):
        return "mañana"
    return due_on

def freq_label(config: dict) -> str:
    freq = config.get("freq")
    if freq == "intraday":
        times = config.get("times_per_day", 2)
        hours = config.get("hours", [])
        hours_str = ", ".join(f"{h}:00" for h in hours)
        return f"{times}x al día ({hours_str})"
    if freq == "daily":
        return "diaria"
    if freq == "weekly":
        days = ["lun","mar","mié","jue","vie","sáb","dom"]
        d = config.get("weekday", 0)
        return f"semanal (cada {days[d]})"
    if freq == "biweekly":
        days = ["lun","mar","mié","jue","vie","sáb","dom"]
        d = config.get("weekday", 0)
        return f"quincenal (cada {days[d]})"
    if freq == "monthly":
        return "mensual"
    return freq

# ── MENÚ PRINCIPAL ─────────────────────────────────────────────────────────────

def main_menu_keyboard(is_manager: bool = False, leader_slugs: list = None) -> InlineKeyboardMarkup:
    from teams_manager import get_permissions
    perms = get_permissions()
    buttons = [
        [InlineKeyboardButton("📋 Ver mis tareas",       callback_data="ver_tareas")],
        [InlineKeyboardButton("✅ Completar una tarea",  callback_data="completar_menu")],
        [InlineKeyboardButton("✅✅ Completar todas",    callback_data="completar_todas_confirm")],
        [InlineKeyboardButton("🔀 Mover tarea",          callback_data="mover_start")],
        [InlineKeyboardButton("🔄 Actualizar estado",    callback_data="status_menu")],
    ]
    # Crear tarea propia (si el permiso lo permite)
    if is_manager or perms.get("members_can_create_own_tasks", True):
        buttons.append([InlineKeyboardButton("📝 Crear mi tarea", callback_data="self_task_start")])
    if is_manager:
        buttons.append([InlineKeyboardButton("➕ Crear tarea para alguien", callback_data="crear_tarea_start")])
        buttons.append([InlineKeyboardButton("📄 Cargar minuta",            callback_data="minuta_start")])
        buttons.append([InlineKeyboardButton("🔁 Tareas recurrentes",       callback_data="recurrentes_menu")])
        buttons.append([InlineKeyboardButton("📊 Reporte del equipo",       callback_data="reporte")])
        buttons.append([InlineKeyboardButton("👥 Equipo",                   callback_data="equipo")])
        buttons.append([InlineKeyboardButton("➕ Agregar miembro",          callback_data="team_add_start")])
    elif leader_slugs:
        # Opciones exclusivas para líderes de área
        if perms.get("leader_can_create_tasks", True):
            buttons.append([InlineKeyboardButton("➕ Crear tarea para mi equipo", callback_data="leader_create_task")])
        if perms.get("leader_can_request_delegation", True):
            buttons.append([InlineKeyboardButton("📤 Solicitar tarea al manager",  callback_data="leader_req_task_start")])
        if perms.get("leader_receives_reports", True):
            buttons.append([InlineKeyboardButton("📊 Reporte de mi área",          callback_data="leader_area_report")])
    return InlineKeyboardMarkup(buttons)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None):
    from teams_manager import get_areas_for_member
    tg_id = update.effective_user.id
    team  = load_team()
    is_manager = (tg_id == MANAGER_CHAT_ID)

    if tg_id not in team and not is_manager:
        msg = f"👋 Hola! Aún no estás registrado.\nDile a Marco tu ID: `{tg_id}`"
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, parse_mode="Markdown")
        else:
            await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # Detectar si es líder de algún área
    leader_slugs = None
    if not is_manager:
        areas = get_areas_for_member(tg_id)
        slugs = [a["slug"] for a in areas if a.get("is_leader")]
        if slugs:
            leader_slugs = slugs

    name = get_first_name(team[tg_id]["name"]) if tg_id in team else "Marco"
    greeting = text or f"¡Hola {name}! ¿Qué quieres hacer?"
    keyboard  = main_menu_keyboard(is_manager, leader_slugs)

    if update.callback_query:
        await update.callback_query.edit_message_text(greeting, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(greeting, reply_markup=keyboard, parse_mode="Markdown")

_CONV_KEYS = (
    "new_task", "awaiting_task_name", "leader_mode", "leader_area_data",
    "self_task", "nl_task_draft", "nl_awaiting_date",
    "awaiting_comment_for", "awaiting_comment_name",
)

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancela cualquier conversación activa y limpia el estado."""
    for key in _CONV_KEYS:
        context.user_data.pop(key, None)
    await show_main_menu(update, context, "❌ Operación cancelada.")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# FLUJO DE CREACIÓN DE TAREA
# ══════════════════════════════════════════════════════════════════════════════

async def crear_tarea_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entrada: /tarea Nombre de la tarea"""
    query = update.callback_query
    if query:
        await query.answer()

    # Obtener nombre desde el comando o desde el botón
    if update.message and context.args:
        task_name = " ".join(context.args).strip()
    elif context.user_data.get("pending_task_name"):
        task_name = context.user_data["pending_task_name"]
    else:
        msg = (
            "➕ *Crear nueva tarea*\n\n"
            "Escribe el nombre de la tarea:\n"
            "Ej: `Llamar al cliente García`"
        )
        if query:
            await query.edit_message_text(msg, parse_mode="Markdown")
        else:
            await update.message.reply_text(msg, parse_mode="Markdown")
        context.user_data["awaiting_task_name"] = True
        return TASK_ASSIGNEE

    context.user_data["new_task"] = {"name": task_name}
    context.user_data.pop("awaiting_task_name", None)
    return await ask_assignee(update, context)

async def ask_assignee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from teams_manager import load_teams, get_permissions
    team      = load_team()
    task_name = context.user_data["new_task"]["name"]

    # ── Modo líder: mostrar solo miembros del área ─────────────────────────────
    leader_mode = context.user_data.get("leader_mode")
    leader_area = context.user_data.get("leader_area_data", {})
    if leader_mode and leader_area:
        area_members = leader_area.get("members", [])
        buttons = []
        row = []
        for m in area_members:
            first = get_first_name(m["name"])
            row.append(InlineKeyboardButton(first, callback_data=f"assign_{m['tg_id']}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        perms = get_permissions()
        if perms.get("leader_can_assign_to_team", True) and len(area_members) > 1:
            buttons.append([InlineKeyboardButton("👥 Todo el equipo", callback_data="assign_broadcast")])
        buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data="menu")])
        msg = f"➕ *Nueva tarea para {leader_area['name']}:*\n📌 _{task_name}_\n\n👤 ¿A quién se la asignas?"
        keyboard = InlineKeyboardMarkup(buttons)
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")
        elif update.message:
            await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
        return TASK_ASSIGNEE

    # ── Modo manager: todos los miembros ───────────────────────────────────────
    members = get_members(team)
    buttons = []
    row = []
    for tid, info in members:
        first = get_first_name(info["name"])
        row.append(InlineKeyboardButton(first, callback_data=f"assign_{tid}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    # Botón de áreas si hay alguna configurada
    if load_teams():
        buttons.append([InlineKeyboardButton("🏢 Asignar a un área →", callback_data="assign_area_start")])
    buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data="menu")])

    msg = f"➕ *Nueva tarea:*\n📌 _{task_name}_\n\n👤 ¿A quién se la asignas?"
    keyboard = InlineKeyboardMarkup(buttons)

    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    elif update.message:
        await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    return TASK_ASSIGNEE

async def handle_task_name_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe el nombre de la tarea cuando se escribe en chat (estado TASK_ASSIGNEE)."""
    # Si el task ya tiene nombre (estamos en la pantalla de selección de responsable),
    # ignorar el texto y pedir que usen los botones.
    if context.user_data.get("new_task", {}).get("name"):
        await update.message.reply_text(
            "⚠️ Usa los botones para seleccionar el responsable, o /menu para cancelar.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]),
        )
        return TASK_ASSIGNEE
    task_name = update.message.text.strip()
    context.user_data.setdefault("new_task", {})["name"] = task_name
    context.user_data.pop("awaiting_task_name", None)
    return await ask_assignee(update, context)

async def handle_area_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manager quiere asignar la tarea a un área. Muestra lista de áreas."""
    from teams_manager import load_teams
    query = update.callback_query
    await query.answer()
    areas = load_teams()
    if not areas:
        await query.edit_message_text(
            "❌ No hay áreas configuradas. Créalas en el panel web.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]),
        )
        return ConversationHandler.END

    task_name = context.user_data["new_task"]["name"]
    buttons = []
    for slug, area in areas.items():
        leader_first = get_first_name(area["leader_name"])
        buttons.append([InlineKeyboardButton(
            f"🏢 {area['name']} — Líder: {leader_first}",
            callback_data=f"areasel_{slug}",
        )])
    buttons.append([InlineKeyboardButton("◀️ Volver", callback_data="crear_tarea_start")])
    buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data="menu")])

    await query.edit_message_text(
        f"🏢 *Asignar tarea a un área:*\n📌 _{task_name}_\n\n¿A cuál área?",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    return AREA_TASK_AREA


async def handle_area_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manager seleccionó un área. El líder recibirá la notificación para asumir/delegar."""
    from teams_manager import get_area
    query = update.callback_query
    await query.answer()
    slug = query.data[len("areasel_"):]
    area = get_area(slug)
    if not area:
        await query.edit_message_text("❌ Área no encontrada.")
        return ConversationHandler.END

    context.user_data["new_task"]["area_slug"]      = slug
    context.user_data["new_task"]["area_name"]      = area["name"]
    context.user_data["new_task"]["assignee_tg_id"] = area["leader_tg_id"]
    context.user_data["new_task"]["assignee_gid"]   = area["leader_asana_gid"]
    context.user_data["new_task"]["assignee_name"]  = area["leader_name"]

    # Mostrar selector de fecha (igual que handle_assignee)
    task_name = context.user_data["new_task"]["name"]
    today    = datetime.now(TZ).date()
    tomorrow = today + timedelta(days=1)
    week_end = today + timedelta(days=(4 - today.weekday()) % 7 or 7)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"Hoy ({today.strftime('%d/%m')})",       callback_data=f"due_{today}"),
            InlineKeyboardButton(f"Mañana ({tomorrow.strftime('%d/%m')})", callback_data=f"due_{tomorrow}"),
        ],
        [
            InlineKeyboardButton(f"Esta semana ({week_end.strftime('%d/%m')})", callback_data=f"due_{week_end}"),
            InlineKeyboardButton("📅 Elegir fecha",                              callback_data="due_custom"),
        ],
        [InlineKeyboardButton("Sin fecha límite", callback_data="due_none")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="menu")],
    ])
    await query.edit_message_text(
        f"🏢 *Área:* {area['name']}\n📌 _{task_name}_\n\n📅 ¿Cuándo vence?",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return TASK_DUE


async def leader_create_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Líder inicia la creación de una tarea para su equipo."""
    from teams_manager import get_areas_for_member
    query = update.callback_query
    await query.answer()
    tg_id = update.effective_user.id
    areas = [a for a in get_areas_for_member(tg_id) if a.get("is_leader")]
    if not areas:
        await query.edit_message_text("❌ No eres líder de ningún área.")
        return ConversationHandler.END

    # Si es líder de varias áreas, preguntar cuál
    if len(areas) > 1:
        buttons = [[InlineKeyboardButton(f"🏢 {a['name']}", callback_data=f"leader_area_pick_{a['slug']}")]
                   for a in areas]
        buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data="menu")])
        await query.edit_message_text(
            "➕ *Crear tarea para tu equipo*\n\n¿Para cuál área?",
            reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown",
        )
        return AREA_TASK_AREA  # reutilizamos estado para la selección de área

    # Solo un área: usarla directamente
    area = areas[0]
    context.user_data["leader_mode"]      = True
    context.user_data["leader_area_data"] = area
    context.user_data["new_task"]         = {}
    context.user_data["awaiting_task_name"] = True
    await query.edit_message_text(
        f"➕ *Crear tarea para {area['name']}*\n\nEscribe el nombre de la tarea:",
        parse_mode="Markdown",
    )
    return TASK_ASSIGNEE


async def leader_area_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Líder seleccionó el área cuando tiene más de una."""
    from teams_manager import get_areas_for_member
    query = update.callback_query
    await query.answer()
    slug  = query.data[len("leader_area_pick_"):]
    tg_id = update.effective_user.id
    areas = [a for a in get_areas_for_member(tg_id) if a.get("is_leader") and a["slug"] == slug]
    if not areas:
        await query.edit_message_text("❌ Área no encontrada.")
        return ConversationHandler.END
    area = areas[0]
    context.user_data["leader_mode"]      = True
    context.user_data["leader_area_data"] = area
    context.user_data["new_task"]         = {}
    context.user_data["awaiting_task_name"] = True
    await query.edit_message_text(
        f"➕ *Crear tarea para {area['name']}*\n\nEscribe el nombre de la tarea:",
        parse_mode="Markdown",
    )
    return TASK_ASSIGNEE


async def handle_assignee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # ── Broadcast: asignar a todo el equipo ──────────────────────────────────
    if query.data == "assign_broadcast":
        leader_area = context.user_data.get("leader_area_data", {})
        context.user_data["new_task"]["assignee_tg_id"] = "broadcast"
        context.user_data["new_task"]["assignee_gid"]   = "broadcast"
        context.user_data["new_task"]["assignee_name"]  = f"Todo el equipo de {leader_area.get('name','área')}"
        task_name = context.user_data["new_task"]["name"]
        today    = datetime.now(TZ).date()
        tomorrow = today + timedelta(days=1)
        week_end = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Hoy ({today.strftime('%d/%m')})",      callback_data=f"due_{today}"),
             InlineKeyboardButton(f"Mañana ({tomorrow.strftime('%d/%m')})", callback_data=f"due_{tomorrow}")],
            [InlineKeyboardButton(f"Esta semana ({week_end.strftime('%d/%m')})", callback_data=f"due_{week_end}"),
             InlineKeyboardButton("📅 Elegir fecha",                              callback_data="due_custom")],
            [InlineKeyboardButton("Sin fecha límite", callback_data="due_none")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="menu")],
        ])
        await query.edit_message_text(
            f"👥 *Todo el equipo*\n📌 _{task_name}_\n\n📅 ¿Cuándo vence?",
            reply_markup=keyboard, parse_mode="Markdown")
        return TASK_DUE

    tid = int(query.data[len("assign_"):])
    team = load_team()
    context.user_data["new_task"]["assignee_tg_id"] = tid
    context.user_data["new_task"]["assignee_gid"]   = team[tid]["asana_gid"]
    context.user_data["new_task"]["assignee_name"]  = team[tid]["name"]

    task_name     = context.user_data["new_task"]["name"]
    assignee_name = get_first_name(team[tid]["name"])

    today    = datetime.now(TZ).date()
    tomorrow = today + timedelta(days=1)
    week_end = today + timedelta(days=(4 - today.weekday()) % 7 or 7)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"Hoy ({today.strftime('%d/%m')})",      callback_data=f"due_{today}"),
            InlineKeyboardButton(f"Mañana ({tomorrow.strftime('%d/%m')})", callback_data=f"due_{tomorrow}"),
        ],
        [
            InlineKeyboardButton(f"Esta semana ({week_end.strftime('%d/%m')})", callback_data=f"due_{week_end}"),
            InlineKeyboardButton("📅 Elegir fecha",                              callback_data="due_custom"),
        ],
        [InlineKeyboardButton("Sin fecha límite", callback_data="due_none")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="menu")],
    ])
    await query.edit_message_text(
        f"➕ *Nueva tarea:*\n📌 _{task_name}_\n👤 {assignee_name}\n\n📅 ¿Cuándo vence?",
        reply_markup=keyboard, parse_mode="Markdown")
    return TASK_DUE

async def handle_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "due_custom":
        await query.edit_message_text(
            "📅 Escribe la fecha en formato *DD/MM/AAAA*\nEj: `25/04/2026`",
            parse_mode="Markdown")
        return TASK_DUE_CUSTOM

    due_on = None if data == "due_none" else data[4:]
    context.user_data["new_task"]["due_on"] = due_on
    return await ask_recurring(update, context)

async def handle_due_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        d = datetime.strptime(text, "%d/%m/%Y").date()
        context.user_data["new_task"]["due_on"] = d.strftime("%Y-%m-%d")
    except ValueError:
        await update.message.reply_text(
            "❌ Formato inválido. Escribe la fecha así: `25/04/2026`", parse_mode="Markdown")
        return TASK_DUE_CUSTOM
    return await ask_recurring(update, context)

async def ask_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task     = context.user_data["new_task"]
    due_str  = due_label(task.get("due_on"))
    name_str = get_first_name(task["assignee_name"])

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ No, es única",    callback_data="rec_no")],
        [InlineKeyboardButton("🔁 Sí, se repite",  callback_data="rec_yes")],
        [InlineKeyboardButton("❌ Cancelar",        callback_data="menu")],
    ])
    msg = (
        f"➕ *Nueva tarea:*\n"
        f"📌 _{task['name']}_\n"
        f"👤 {name_str}  |  📅 {due_str}\n\n"
        f"🔁 ¿Es una tarea recurrente?"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    return TASK_RECURRING

async def handle_recurring_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "rec_no":
        context.user_data["new_task"]["freq"] = None
        return await confirm_and_create(update, context)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Varias veces al día", callback_data="freq_intraday")],
        [InlineKeyboardButton("📅 Diaria",              callback_data="freq_daily")],
        [
            InlineKeyboardButton("📅 Semanal",    callback_data="freq_weekly"),
            InlineKeyboardButton("📅 Quincenal",  callback_data="freq_biweekly"),
        ],
        [InlineKeyboardButton("📅 Mensual",       callback_data="freq_monthly")],
        [InlineKeyboardButton("❌ Cancelar",       callback_data="menu")],
    ])
    await query.edit_message_text("🔁 ¿Con qué frecuencia se repite?", reply_markup=keyboard)
    return TASK_FREQ

async def handle_freq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    freq = query.data[5:]  # quita "freq_"
    context.user_data["new_task"]["freq"] = freq

    if freq == "intraday":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("2 veces", callback_data="times_2"),
                InlineKeyboardButton("3 veces", callback_data="times_3"),
                InlineKeyboardButton("4 veces", callback_data="times_4"),
            ],
            [InlineKeyboardButton("❌ Cancelar", callback_data="menu")],
        ])
        await query.edit_message_text("🔄 ¿Cuántas veces al día?", reply_markup=keyboard)
        return TASK_TIMES_PER_DAY

    if freq in ("weekly", "biweekly"):
        return await ask_weekday(update, context)

    # daily / monthly — no necesita más info
    return await confirm_and_create(update, context)

async def handle_times_per_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    times = int(query.data[6:])  # quita "times_"
    context.user_data["new_task"]["times_per_day"] = times
    context.user_data["new_task"]["hours_selected"] = []

    return await ask_hours(update, context, times)

async def ask_hours(update: Update, context: ContextTypes.DEFAULT_TYPE, times: int):
    selected = context.user_data["new_task"].get("hours_selected", [])
    remaining = times - len(selected)

    hour_options = [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]
    buttons = []
    row = []
    for h in hour_options:
        label = f"✅{h}:00" if h in selected else f"{h}:00"
        row.append(InlineKeyboardButton(label, callback_data=f"hour_{h}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data="menu")])

    msg = f"🕐 Selecciona {remaining} hora(s) más para los recordatorios:\n_{', '.join(f'{h}:00' for h in selected)}_" if selected else f"🕐 Selecciona {times} hora(s) para los recordatorios:"
    keyboard = InlineKeyboardMarkup(buttons)

    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    return TASK_HOURS

async def handle_hour_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    hour = int(query.data[5:])  # quita "hour_"
    selected = context.user_data["new_task"].get("hours_selected", [])
    times    = context.user_data["new_task"]["times_per_day"]

    if hour in selected:
        selected.remove(hour)
    else:
        selected.append(hour)
    selected.sort()
    context.user_data["new_task"]["hours_selected"] = selected

    if len(selected) == times:
        context.user_data["new_task"]["hours"] = selected
        return await confirm_and_create(update, context)

    return await ask_hours(update, context, times)

async def ask_weekday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb"]
    buttons = [[InlineKeyboardButton(d, callback_data=f"wday_{i}") for i, d in enumerate(days)]]
    buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data="menu")])
    keyboard = InlineKeyboardMarkup(buttons)
    freq = context.user_data["new_task"]["freq"]
    label = "semanal" if freq == "weekly" else "quincenal"
    if update.callback_query:
        await update.callback_query.edit_message_text(
            f"📅 ¿Qué día de la semana se repite la tarea {label}?",
            reply_markup=keyboard)
    return TASK_WEEKDAY

async def handle_weekday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    weekday = int(query.data[5:])  # quita "wday_"
    context.user_data["new_task"]["weekday"] = weekday
    return await confirm_and_create(update, context)

async def confirm_and_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = context.user_data["new_task"]
    freq = task.get("freq")

    # Construir resumen
    due_str   = due_label(task.get("due_on"))
    name_str  = get_first_name(task["assignee_name"])
    freq_str  = freq_label(task) if freq else "única (no se repite)"

    notes_preview = f"\n📝 _{task['notes'][:60]}_" if task.get("notes") else ""
    msg = (
        f"✅ *Confirmando tarea:*\n\n"
        f"📌 *{task['name']}*\n"
        f"👤 {task['assignee_name']}\n"
        f"📅 Vence: {due_str}\n"
        f"🔁 {freq_str}{notes_preview}\n\n"
        f"¿Crear esta tarea?"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Sí, crear",         callback_data="task_confirm_yes")],
        [InlineKeyboardButton("✏️ Agregar descripción", callback_data="task_add_desc")],
        [InlineKeyboardButton("❌ Cancelar",            callback_data="menu")],
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    return TASK_RECURRING  # reutilizamos estado, la lógica real está en el callback

async def handle_task_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    task = context.user_data.get("new_task", {})
    if not task:
        await query.edit_message_text("❌ No hay tarea pendiente.")
        return ConversationHandler.END

    freq    = task.get("freq")
    due_on  = task.get("due_on")
    notes   = task.get("notes")

    # ── BROADCAST: asignar a todos los miembros del área ─────────────────────
    if task.get("assignee_tg_id") == "broadcast":
        leader_area = context.user_data.get("leader_area_data", {})
        creator_tg_id = update.effective_user.id
        team = load_team()
        members = leader_area.get("members", [])
        created_count = 0
        from teams_manager import register_area_task
        for m in members:
            if m["tg_id"] not in team:
                continue
            try:
                created = await create_asana_task(task["name"], m["asana_gid"], due_on, notes=notes)
                t_gid   = created.get("gid", "")
                known_tasks.setdefault(m["asana_gid"], set()).add(t_gid)
                try:
                    await add_task_to_member_project(t_gid, m["asana_gid"], ASANA_TOKEN)
                except Exception:
                    pass
                register_area_task(
                    task_gid=t_gid, task_name=task["name"],
                    area_slug=leader_area.get("slug",""), area_name=leader_area.get("name",""),
                    assigned_to_tg_id=m["tg_id"], assigned_to_name=m["name"],
                    leader_tg_id=creator_tg_id, due_on=due_on,
                )
                try:
                    await context.bot.send_message(
                        chat_id=m["tg_id"],
                        text=(f"🔔 *¡Nueva tarea, {get_first_name(m['name'])}!*\n\n"
                              f"📌 *{task['name']}*\n"
                              f"📅 Vence: {due_label(due_on)}\n"
                              f"_Asignada por tu líder de área._"),
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")
                        ]]),
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
                created_count += 1
            except Exception as e:
                logger.error(f"Error broadcast para {m['name']}: {e}")
        save_known_tasks()
        await query.edit_message_text(
            f"🎉 *¡Tarea creada para todo el equipo!*\n\n"
            f"📌 *{task['name']}*\n"
            f"👥 {created_count} miembro(s) notificados\n"
            f"📅 {due_label(due_on)}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]),
        )
        for k in ("new_task","leader_mode","leader_area_data","awaiting_task_name"):
            context.user_data.pop(k, None)
        return ConversationHandler.END

    # Recurrencia nativa de Asana para daily/weekly/monthly
    asana_recurrence = None
    if freq == "daily":
        asana_recurrence = "daily"
    elif freq == "monthly":
        asana_recurrence = "monthly"

    try:
        created = await create_asana_task(
            name         = task["name"],
            assignee_gid = task["assignee_gid"],
            due_on       = due_on,
            recurrence   = asana_recurrence,
            notes        = notes,
        )
        task_gid = created.get("gid", "")
    except Exception as e:
        logger.error(f"Error creando tarea en Asana: {e}")
        await query.edit_message_text("❌ Error al crear la tarea en Asana. Intenta de nuevo.")
        return ConversationHandler.END

    # ── FIX doble notificación: marcar tarea como ya conocida ─────────────────
    asana_gid_assignee = task["assignee_gid"]
    if asana_gid_assignee not in known_tasks:
        known_tasks[asana_gid_assignee] = set()
    known_tasks[asana_gid_assignee].add(task_gid)
    save_known_tasks()

    # ── Agregar tarea al proyecto Kanban del colaborador ──────────────────────
    try:
        await add_task_to_member_project(task_gid, asana_gid_assignee, ASANA_TOKEN)
    except Exception as e:
        logger.warning(f"No se pudo agregar tarea al proyecto del colaborador: {e}")

    # Registrar metadata de tarea única para calcular recordatorios dinámicos
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    if not freq and due_on and task_gid:
        register_unique_task(task_gid, today_str, due_on)

    # Guardar recurrencia custom (intraday, weekly, biweekly)
    if freq in ("intraday", "weekly", "biweekly"):
        rec_config = {
            "task_name":    task["name"],
            "assignee_gid": task["assignee_gid"],
            "assignee_tg_id": task["assignee_tg_id"],
            "assignee_name":  task["assignee_name"],
            "freq":           freq,
            "weekday":        task.get("weekday"),
            "times_per_day":  task.get("times_per_day"),
            "hours":          task.get("hours", []),
            "due_on":         due_on,
            "last_task_gid":  task_gid,
            "last_created":   datetime.now(TZ).strftime("%Y-%m-%d"),
            "pending_count":  1,
        }
        add_recurring(rec_config)

    # Notificar al responsable
    assignee_tg_id = task["assignee_tg_id"]
    first_name     = get_first_name(task["assignee_name"])
    due_str        = due_label(due_on)
    freq_str       = freq_label(task) if freq else "única"
    area_slug      = task.get("area_slug")

    try:
        if area_slug:
            # ── Tarea asignada a un área: notificar al líder con opciones ────────
            from teams_manager import get_area, register_area_task
            # Registrar en el tracker de tareas de área
            try:
                register_area_task(
                    task_gid          = task_gid,
                    task_name         = task["name"],
                    area_slug         = area_slug,
                    area_name         = task.get("area_name", area_slug),
                    assigned_to_tg_id = assignee_tg_id,
                    assigned_to_name  = task["assignee_name"],
                    leader_tg_id      = assignee_tg_id,  # inicialmente el líder
                    due_on            = due_on,
                )
            except Exception as e:
                logger.warning(f"No se pudo registrar tarea de área: {e}")
            area = get_area(area_slug)
            if area:
                deleg_buttons = [
                    [InlineKeyboardButton("✅ Asumir yo", callback_data=f"asumir_{task_gid}")],
                ]
                for m in area.get("members", []):
                    deleg_buttons.append([InlineKeyboardButton(
                        f"🔀 Delegar a {get_first_name(m['name'])}",
                        callback_data=f"delegar_exec_{task_gid}_{m['tg_id']}_{area_slug}",
                    )])
                deleg_buttons.append([InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")])
                await context.bot.send_message(
                    chat_id=assignee_tg_id,
                    text=(
                        f"🏢 *Nueva tarea para {area['name']}, {first_name}*\n\n"
                        f"📌 *{task['name']}*\n"
                        f"📅 Vence: {due_str}\n\n"
                        f"¿La asumes tú o la delegas a alguien del área?"
                    ),
                    reply_markup=InlineKeyboardMarkup(deleg_buttons),
                    parse_mode="Markdown",
                )
        else:
            # ── Notificación estándar ─────────────────────────────────────────────
            notif_msg = (
                f"🔔 *¡Nueva tarea asignada, {first_name}!*\n\n"
                f"📌 *{task['name']}*\n"
                f"📅 Vence: {due_str}\n"
                f"🔁 {freq_str}"
            )
            keyboard_notif = InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")
            ]])
            await context.bot.send_message(
                chat_id=assignee_tg_id, text=notif_msg,
                reply_markup=keyboard_notif, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error notificando a {assignee_tg_id}: {e}")

    freq_str_short = freq_label(task) if freq else "no se repite"
    area_label = f"\n🏢 Área: {task['area_name']}" if task.get("area_name") else ""
    await query.edit_message_text(
        f"🎉 *¡Tarea creada exitosamente!*\n\n"
        f"📌 *{task['name']}*\n"
        f"👤 {task['assignee_name']}{area_label}\n"
        f"📅 {due_str}  |  🔁 {freq_str_short}\n\n"
        f"_{first_name} ya fue notificado/a._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Crear otra tarea", callback_data="crear_tarea_start"),
            InlineKeyboardButton("⬅️ Menú",             callback_data="menu"),
        ]])
    )
    # Si fue creada en modo líder para un miembro → registrar en area_tasks
    leader_area = context.user_data.get("leader_area_data")
    if leader_area and not area_slug:
        try:
            from teams_manager import register_area_task
            register_area_task(
                task_gid=task_gid, task_name=task["name"],
                area_slug=leader_area.get("slug",""), area_name=leader_area.get("name",""),
                assigned_to_tg_id=assignee_tg_id, assigned_to_name=task["assignee_name"],
                leader_tg_id=update.effective_user.id, due_on=due_on,
            )
        except Exception as e:
            logger.warning(f"No se pudo registrar tarea líder en area_tasks: {e}")

    for k in ("new_task","leader_mode","leader_area_data","awaiting_task_name"):
        context.user_data.pop(k, None)
    return ConversationHandler.END

async def handle_task_add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """El manager quiere agregar descripción antes de crear la tarea."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "✏️ *Escribe la descripción de la tarea:*\n"
        "_(O escribe /skip para crear sin descripción)_",
        parse_mode="Markdown",
    )
    return TASK_NOTES

async def handle_task_notes_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe la descripción y crea la tarea."""
    text = update.message.text.strip()
    if text.lower() != "/skip":
        context.user_data.setdefault("new_task", {})["notes"] = text
    # Simular task_confirm_yes en modo mensaje
    task = context.user_data.get("new_task", {})
    if not task:
        await update.message.reply_text("❌ No hay tarea pendiente.")
        return ConversationHandler.END
    freq   = task.get("freq")
    due_on = task.get("due_on")
    notes  = task.get("notes")
    asana_recurrence = None
    if freq == "daily":
        asana_recurrence = "daily"
    elif freq == "monthly":
        asana_recurrence = "monthly"
    try:
        created  = await create_asana_task(task["name"], task["assignee_gid"], due_on,
                                            asana_recurrence, notes)
        task_gid = created.get("gid", "")
    except Exception as e:
        logger.error(f"Error creando tarea (con descripción): {e}")
        await update.message.reply_text("❌ Error al crear la tarea en Asana.")
        return ConversationHandler.END
    # Marcar como conocida + agregar al tablero
    agid = task["assignee_gid"]
    known_tasks.setdefault(agid, set()).add(task_gid)
    save_known_tasks()
    try:
        await add_task_to_member_project(task_gid, agid, ASANA_TOKEN)
    except Exception:
        pass
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    if not freq and due_on and task_gid:
        register_unique_task(task_gid, today_str, due_on)
    if freq in ("intraday", "weekly", "biweekly"):
        rec_config = {
            "task_name": task["name"], "assignee_gid": agid,
            "assignee_tg_id": task["assignee_tg_id"], "assignee_name": task["assignee_name"],
            "freq": freq, "weekday": task.get("weekday"), "times_per_day": task.get("times_per_day"),
            "hours": task.get("hours", []), "due_on": due_on, "last_task_gid": task_gid,
            "last_created": datetime.now(TZ).strftime("%Y-%m-%d"), "pending_count": 1,
        }
        add_recurring(rec_config)
    # Notificar al responsable
    first_name = get_first_name(task["assignee_name"])
    try:
        await context.bot.send_message(
            chat_id=task["assignee_tg_id"],
            text=(f"🔔 *¡Nueva tarea asignada, {first_name}!*\n\n"
                  f"📌 *{task['name']}*\n"
                  f"📅 Vence: {due_label(due_on)}\n"
                  f"🔁 {freq_label(task) if freq else 'única'}"),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")
            ]]),
            parse_mode="Markdown",
        )
    except Exception:
        pass
    await update.message.reply_text(
        f"🎉 *¡Tarea creada!*\n\n📌 *{task['name']}*\n👤 {task['assignee_name']}\n"
        f"📅 {due_label(due_on)}" + (f"\n📝 _{notes[:60]}_" if notes else ""),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Crear otra tarea", callback_data="crear_tarea_start"),
            InlineKeyboardButton("⬅️ Menú",             callback_data="menu"),
        ]]),
    )
    context.user_data.pop("new_task", None)
    return ConversationHandler.END

# ── MENÚ DE TAREAS RECURRENTES ─────────────────────────────────────────────────

async def recurrentes_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != MANAGER_CHAT_ID:
        await query.edit_message_text("❌ Solo el manager puede ver esto.")
        return

    data = load_recurring()
    if not data:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Menú principal", callback_data="menu")
        ]])
        await query.edit_message_text(
            "🔁 *Tareas recurrentes*\n\nNo hay tareas recurrentes configuradas.",
            reply_markup=keyboard, parse_mode="Markdown")
        return

    msg = "🔁 *Tareas recurrentes activas:*\n\n"
    buttons = []
    for i, r in enumerate(data):
        name  = get_first_name(r["assignee_name"])
        freq  = freq_label(r)
        pend  = r.get("pending_count", 0)
        warn  = " ⚠️" if pend > 1 else ""
        msg  += f"{i+1}. *{r['task_name']}*{warn}\n   👤 {name} | 🔁 {freq}\n\n"
        buttons.append([InlineKeyboardButton(
            f"{'⚠️ ' if pend > 1 else ''}#{i+1} {r['task_name'][:35]}",
            callback_data=f"rec_detail_{i}"
        )])

    buttons.append([InlineKeyboardButton("⬅️ Menú principal", callback_data="menu")])
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def recurrente_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data[len("rec_detail_"):])
    data = load_recurring()
    if idx >= len(data):
        await query.edit_message_text("❌ Tarea no encontrada.")
        return

    r     = data[idx]
    pend  = r.get("pending_count", 0)
    warn  = f"\n⚠️ *{pend} ocurrencias sin completar*" if pend > 1 else ""
    msg   = (
        f"🔁 *{r['task_name']}*{warn}\n\n"
        f"👤 {r['assignee_name']}\n"
        f"🔁 {freq_label(r)}\n"
        f"📅 Última creación: {r.get('last_created','—')}\n"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ Eliminar recurrencia", callback_data=f"rec_delete_{idx}")],
        [InlineKeyboardButton("⬅️ Volver",               callback_data="recurrentes_menu")],
    ])
    await query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")

async def recurrente_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx  = int(query.data[len("rec_delete_"):])
    data = load_recurring()
    if idx < len(data):
        deleted = data.pop(idx)
        save_recurring(data)
        await query.edit_message_text(
            f"🗑️ Recurrencia eliminada:\n_{deleted['task_name']}_\n\nLa tarea actual en Asana no fue modificada.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Volver", callback_data="recurrentes_menu")
            ]]))
    else:
        await query.edit_message_text("❌ No encontrada.")

# ── JOB: PROCESAR RECURRENTES ──────────────────────────────────────────────────

async def job_process_recurring(context: ContextTypes.DEFAULT_TYPE):
    """Revisa tareas recurrentes y crea nuevas cuando corresponde."""
    data = load_recurring()
    if not data:
        return

    now     = datetime.now(TZ)
    today   = now.date()
    changed = False

    for i, r in enumerate(data):
        if r.get("paused"):          # pausada desde el panel web
            continue
        freq    = r["freq"]
        should_create = False

        # ── Intraday ──────────────────────────────────────────────────────────
        if freq == "intraday":
            hours = r.get("hours", [])
            # Crear si la hora actual está en la lista y no se creó en los últimos 30 min
            last_intraday = r.get("last_intraday_hour")
            if now.hour in hours and last_intraday != f"{today}-{now.hour}":
                should_create = True
                r["last_intraday_hour"] = f"{today}-{now.hour}"
                changed = True   # persistir aunque la tarea falle

        # ── Semanal / Quincenal ───────────────────────────────────────────────
        elif freq in ("weekly", "biweekly"):
            weekday      = r.get("weekday", 0)
            last_created = r.get("last_created", "")
            if now.weekday() == weekday:
                if freq == "weekly" and last_created != str(today):
                    should_create = True
                elif freq == "biweekly":
                    try:
                        last_d = datetime.strptime(last_created, "%Y-%m-%d").date()
                        if (today - last_d).days >= 14:
                            should_create = True
                    except Exception:
                        should_create = True

        if not should_create:
            continue

        # Notificar al manager si hay acumulación (sin bloquear la creación)
        pending_count = r.get("pending_count", 0)
        if pending_count >= 2:
            try:
                await context.bot.send_message(
                    chat_id=MANAGER_CHAT_ID,
                    text=(
                        f"⚠️ *Tarea recurrente acumulada*\n\n"
                        f"📌 *{r['task_name']}*\n"
                        f"👤 {r['assignee_name']}\n"
                        f"🔁 {freq_label(r)}\n\n"
                        f"⚠️ *{pending_count} ocurrencias sin completar.*\n"
                        f"Se sigue creando la nueva tarea."
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Error notificando acumulación: {e}")

        # Crear nueva tarea en Asana
        try:
            next_due = str(today)
            created  = await create_asana_task(
                name         = r["task_name"],
                assignee_gid = r["assignee_gid"],
                due_on       = next_due,
            )
            r["last_task_gid"]  = created.get("gid", "")
            r["last_created"]   = str(today)
            r["pending_count"]  = pending_count + 1
            changed = True

            # Notificar al responsable
            first_name = get_first_name(r["assignee_name"])
            keyboard   = InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")
            ]])
            await context.bot.send_message(
                chat_id = r["assignee_tg_id"],
                text    = (
                    f"🔔 *Tarea recurrente, {first_name}*\n\n"
                    f"📌 *{r['task_name']}*\n"
                    f"📅 Vence hoy\n"
                    f"🔁 {freq_label(r)}"
                ),
                reply_markup = keyboard,
                parse_mode   = "Markdown"
            )
            logger.info(f"Recurrente creada: {r['task_name']} → {r['assignee_name']}")
        except Exception as e:
            logger.error(f"Error creando recurrente {r['task_name']}: {e}")

    if changed:
        save_recurring(data)

async def job_check_recurring_completed(context: ContextTypes.DEFAULT_TYPE):
    """Revisa si las tareas recurrentes fueron completadas y actualiza el contador."""
    data = load_recurring()
    if not data:
        return
    changed = False
    for r in data:
        task_gid = r.get("last_task_gid")
        if not task_gid or r.get("pending_count", 0) == 0:
            continue
        try:
            task_data = await asana_get(f"/tasks/{task_gid}", {"opt_fields": "completed"})
            if task_data.get("data", {}).get("completed"):
                r["pending_count"] = max(0, r.get("pending_count", 1) - 1)
                changed = True
        except Exception:
            pass
    if changed:
        save_recurring(data)

# ── REPORTES Y ACCIONES DE LÍDERES DE ÁREA ────────────────────────────────────

async def leader_area_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Líder ve el reporte de su área."""
    from teams_manager import get_areas_for_member
    query = update.callback_query
    await query.answer()
    tg_id = update.effective_user.id
    team  = load_team()
    areas = [a for a in get_areas_for_member(tg_id) if a.get("is_leader")]
    if not areas:
        await query.edit_message_text("❌ No eres líder de ningún área.")
        return

    today = datetime.now(TZ).strftime("%d/%m/%Y")
    for area in areas:
        msg = f"📊 *Reporte de {area['name']} — {today}*\n\n"
        has_data = False
        for m in area.get("members", []):
            m_tg_id = m["tg_id"]
            if m_tg_id not in team:
                continue
            try:
                tasks = await get_pending_tasks(team[m_tg_id]["asana_gid"])
            except Exception:
                tasks = []
            od     = sum(1 for t in tasks if is_overdue(t))
            status = "🟢" if not tasks else ("🔴" if od > 0 else "🟡")
            first  = get_first_name(m["name"])
            msg   += f"{status} *{first}* — {len(tasks)} pendiente(s)"
            if od:
                msg += f" ({od} vencida(s))"
            msg += "\n"
            for t in tasks[:3]:
                due  = f" _{t['due_on']}_" if t.get("due_on") else ""
                warn = " ⚠️" if is_overdue(t) else ""
                msg += f"  • {t['name'][:40]}{due}{warn}\n"
            msg += "\n"
            has_data = True
        if not has_data:
            msg += "_Sin miembros con tareas registradas._"
    await query.edit_message_text(
        msg,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]),
    )


async def leader_req_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Líder inicia solicitud de tarea al manager para otro equipo."""
    from teams_manager import get_areas_for_member, get_permissions
    query = update.callback_query
    await query.answer()
    tg_id = update.effective_user.id
    perms = get_permissions()
    if not perms.get("leader_can_request_delegation", True):
        await query.edit_message_text("❌ No tienes permiso para solicitar tareas al manager.")
        return ConversationHandler.END
    areas = [a for a in get_areas_for_member(tg_id) if a.get("is_leader")]
    if not areas:
        await query.edit_message_text("❌ No eres líder de ningún área.")
        return ConversationHandler.END
    await query.edit_message_text(
        "📤 *Solicitar tarea al manager*\n\n"
        "Describe la tarea que necesitas que otro equipo realice.\n"
        "El manager la recibirá y decidirá a qué área asignarla:",
        parse_mode="Markdown",
    )
    return LEADER_REQ_TASK


async def leader_req_task_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe el texto de la solicitud y la envía al manager."""
    from teams_manager import load_teams, save_delegation_request, get_areas_for_member
    text  = update.message.text.strip()
    tg_id = update.effective_user.id
    team  = load_team()
    leader_name = team.get(tg_id, {}).get("name", f"Líder ({tg_id})")

    # Guardar solicitud en DB y obtener ID
    req_id = save_delegation_request(tg_id, leader_name, text)

    # Enviar al manager con botones por área
    all_areas = load_teams()
    area_buttons = []
    for slug, area in all_areas.items():
        cb = f"req_area_{req_id}_{slug}"
        if len(cb) <= 64:  # Telegram callback limit
            area_buttons.append([InlineKeyboardButton(f"🏢 {area['name']}", callback_data=cb)])
    area_buttons.append([InlineKeyboardButton("🚫 Ignorar solicitud", callback_data=f"req_ignore_{req_id}")])

    try:
        await context.bot.send_message(
            chat_id=MANAGER_CHAT_ID,
            text=(
                f"📤 *Solicitud de delegación*\n\n"
                f"👤 De: *{leader_name}*\n\n"
                f"📋 Tarea:\n_{text}_\n\n"
                f"¿A cuál equipo la asignas?"
            ),
            reply_markup=InlineKeyboardMarkup(area_buttons),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error enviando solicitud al manager: {e}")

    await update.message.reply_text(
        "✅ *Solicitud enviada al manager.*\nTe avisará cuando esté asignada.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]),
    )
    return ConversationHandler.END


# ── HANDLERS DE BOTONES GENERALES ──────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    data   = query.data
    tg_id  = update.effective_user.id
    team   = load_team()

    if data == "menu":
        await show_main_menu(update, context)

    elif data == "ver_tareas":
        if tg_id not in team:
            await query.edit_message_text("❌ No estás registrado.")
            return
        tasks = await get_pending_tasks(team[tg_id]["asana_gid"])
        if not tasks:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]])
            await query.edit_message_text("✅ ¡No tienes tareas pendientes! Estás al día 🎉", reply_markup=keyboard)
            return
        msg = f"📋 *Tus tareas pendientes ({len(tasks)}):*\n\n"
        for i, t in enumerate(tasks, 1):
            due  = f" — _{t['due_on']}_" if t.get("due_on") else ""
            warn = " ⚠️" if is_overdue(t) else ""
            msg += f"{i}. *{t['name']}*{due}{warn}\n"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Completar una", callback_data="completar_menu")],
            [InlineKeyboardButton("✅✅ Completar todas", callback_data="completar_todas_confirm")],
            [InlineKeyboardButton("⬅️ Menú", callback_data="menu")],
        ])
        await query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")

    elif data == "completar_menu":
        if tg_id not in team:
            await query.edit_message_text("❌ No estás registrado.")
            return
        tasks = await get_pending_tasks(team[tg_id]["asana_gid"])
        if not tasks:
            await query.edit_message_text("✅ ¡No tienes tareas pendientes!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]))
            return
        buttons = []
        for t in tasks:
            warn  = "⚠️ " if is_overdue(t) else ""
            label = f"{warn}{t['name']}"[:60]
            buttons.append([InlineKeyboardButton(label, callback_data=f"done_{t['gid']}")])
        buttons.append([InlineKeyboardButton("⬅️ Volver", callback_data="ver_tareas")])
        await query.edit_message_text(
            "✅ *¿Cuál tarea completaste?*\nToca para marcarla:",
            reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

    elif data.startswith("done_"):
        task_gid = data[5:]
        if tg_id not in team:
            await query.edit_message_text("❌ No estás registrado.")
            return
        tasks     = await get_pending_tasks(team[tg_id]["asana_gid"])
        task_name = next((t["name"] for t in tasks if t["gid"] == task_gid), "Tarea")
        if await complete_task(task_gid):
            asana_gid = team[tg_id]["asana_gid"]
            if asana_gid in known_tasks:
                known_tasks[asana_gid].discard(task_gid)
            # Actualizar contador recurrente
            rec_data = load_recurring()
            rec_changed = False
            for r in rec_data:
                if r.get("last_task_gid") == task_gid:
                    r["pending_count"] = max(0, r.get("pending_count", 1) - 1)
                    rec_changed = True
            if rec_changed:
                save_recurring(rec_data)

            # Notificar al líder si era una tarea delegada de área
            try:
                from teams_manager import complete_area_task
                area_record = complete_area_task(task_gid)
                if area_record and area_record["assigned_to_tg_id"] != area_record["leader_tg_id"]:
                    await context.bot.send_message(
                        chat_id=area_record["leader_tg_id"],
                        text=(
                            f"✅ *{area_record['assigned_to_name']}* completó la tarea "
                            f"del área *{area_record['area_name']}*:\n\n"
                            f"📌 _{area_record['task_name']}_"
                        ),
                        parse_mode="Markdown",
                    )
            except Exception as e:
                logger.warning(f"No se pudo notificar completado de área: {e}")

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Completar otra", callback_data="completar_menu")],
                [InlineKeyboardButton("⬅️ Menú",          callback_data="menu")],
            ])
            await query.edit_message_text(
                f"🎉 ¡Perfecto! Marcado en Asana:\n✅ *{task_name}*",
                reply_markup=keyboard, parse_mode="Markdown")
            try:
                await context.bot.send_message(
                    chat_id=MANAGER_CHAT_ID,
                    text=f"✅ *{team[tg_id]['name']}* completó:\n_{task_name}_",
                    parse_mode="Markdown")
            except Exception:
                pass
        else:
            await query.edit_message_text("❌ Error al actualizar Asana. Intenta de nuevo.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]))

    elif data == "completar_todas_confirm":
        if tg_id not in team:
            await query.edit_message_text("❌ No estás registrado.")
            return
        tasks = await get_pending_tasks(team[tg_id]["asana_gid"])
        if not tasks:
            await query.edit_message_text("✅ ¡Ya no tienes tareas pendientes!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]))
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Sí, completar las {len(tasks)}", callback_data="completar_todas")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="ver_tareas")],
        ])
        await query.edit_message_text(
            f"¿Confirmas que completaste *todas* tus {len(tasks)} tareas?",
            reply_markup=keyboard, parse_mode="Markdown")

    elif data == "completar_todas":
        if tg_id not in team:
            await query.edit_message_text("❌ No estás registrado.")
            return
        tasks   = await get_pending_tasks(team[tg_id]["asana_gid"])
        results = []
        for t in tasks:
            if await complete_task(t["gid"]):
                results.append(t["name"])
        asana_gid = team[tg_id]["asana_gid"]
        if asana_gid in known_tasks:
            known_tasks[asana_gid] = set()
        await query.edit_message_text(
            f"🎉 *{len(results)}/{len(tasks)}* tareas completadas en Asana.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]),
            parse_mode="Markdown")
        if results:
            try:
                await context.bot.send_message(
                    chat_id=MANAGER_CHAT_ID,
                    text=f"🎉 *{team[tg_id]['name']}* completó todo:\n" + "\n".join(f"✅ _{n}_" for n in results),
                    parse_mode="Markdown")
            except Exception:
                pass

    elif data == "reporte":
        if tg_id != MANAGER_CHAT_ID:
            await query.edit_message_text("❌ Solo el manager puede ver el reporte.")
            return
        await query.edit_message_text("⏳ Generando reporte...")
        await _send_report(context.bot)
        await show_main_menu(update, context, "📊 Reporte enviado.")

    elif data == "equipo":
        if tg_id != MANAGER_CHAT_ID:
            await query.edit_message_text("❌ Solo el manager puede ver esto.")
            return
        members = get_members(team)
        msg = f"👥 *Equipo registrado ({len(members)} personas):*\n\n"
        for _, info in members:
            msg += f"• *{info['name']}*\n"
        msg += "\n_Para agregar alguien, edita team.txt en GitHub._"
        await query.edit_message_text(msg,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]),
            parse_mode="Markdown")

    elif data == "recurrentes_menu":
        await recurrentes_menu(update, context)

    elif data.startswith("rec_detail_"):
        await recurrente_detail(update, context)

    elif data.startswith("rec_delete_"):
        await recurrente_delete(update, context)

    elif data == "task_confirm_yes":
        await handle_task_confirm(update, context)

    elif data == "crear_tarea_start":
        context.user_data.pop("new_task", None)
        await crear_tarea_start(update, context)

    elif data == "mover_start":
        await mover_tarea_start(update, context)

    elif data.startswith("mover_task_"):
        await mover_elegir_proyecto(update, context)

    elif data.startswith("mover_proj_"):
        await mover_elegir_seccion(update, context)

    elif data.startswith("mover_sec_"):
        await mover_confirmar(update, context)

    elif data == "mover_conf_yes":
        await mover_ejecutar(update, context)

    # ── Tarea propia ──────────────────────────────────────────────────────────
    elif data == "self_task_start":
        await self_task_start(update, context)

    # ── Minuta ────────────────────────────────────────────────────────────────
    elif data == "minuta_start":
        if tg_id != MANAGER_CHAT_ID:
            await query.edit_message_text("❌ Solo el manager puede cargar minutas.")
            return
        await minuta_start(update, context)

    elif data.startswith("minuta_fix_"):
        await minuta_fix_dispatch(update, context)

    elif data == "minuta_confirm_all":
        await minuta_confirm_all(update, context)

    # ── Agregar miembro ───────────────────────────────────────────────────────
    elif data == "team_add_start":
        if tg_id != MANAGER_CHAT_ID:
            await query.edit_message_text("❌ Solo el manager puede agregar miembros.")
            return
        await team_add_start(update, context)

    # ── Estado de tarea ───────────────────────────────────────────────────────
    elif data == "status_menu":
        await status_menu(update, context)

    elif data.startswith("status_task_"):
        await status_task_detail(update, context)

    elif data.startswith("set_status_"):
        await set_task_status(update, context)

    elif data.startswith("task_comment_"):
        await request_comment(update, context)

    # ── NL task (confirmación desde Gemini) ───────────────────────────────────
    elif data == "nl_task_confirm":
        await nl_task_confirm(update, context)

    elif data.startswith("nl_assign_"):
        await nl_assign_handler(update, context)

    elif data.startswith("nl_due_"):
        await nl_due_handler(update, context)

    elif data == "nl_change_assignee":
        await nl_change_assignee_handler(update, context)

    elif data == "nl_change_due":
        await nl_change_due_handler(update, context)

    elif data == "nl_area_start":
        await nl_area_start_handler(update, context)

    elif data.startswith("nl_area_"):
        await nl_area_select_handler(update, context)

    elif data == "nl_back_draft":
        await _show_nl_draft(update, context)

    elif data == "nl_task_cancel":
        context.user_data.pop("nl_task_draft", None)
        await show_main_menu(update, context, "❌ Tarea cancelada.")

    # ── Menú líder ────────────────────────────────────────────────────────────
    elif data == "leader_area_report":
        await leader_area_report(update, context)

    # ── Solicitud de delegación (manager responde) ────────────────────────────
    elif data.startswith("req_area_"):
        # "req_area_{req_id}_{slug}"
        payload    = data[len("req_area_"):]
        parts      = payload.split("_", 1)
        req_id_str = parts[0]
        slug       = parts[1] if len(parts) > 1 else ""
        try:
            req_id = int(req_id_str)
        except ValueError:
            await query.edit_message_text("❌ ID de solicitud inválido.")
            return
        from teams_manager import get_delegation_request, resolve_delegation_request, get_area, register_area_task
        req  = get_delegation_request(req_id)
        area = get_area(slug) if slug else None
        if not req or not area:
            await query.edit_message_text("❌ Solicitud o área no encontrada.")
            return
        if req.get("status") == "resolved":
            await query.edit_message_text("✅ Esta solicitud ya fue procesada.")
            return
        # Crear tarea en Asana para el líder del área destino
        try:
            created  = await create_asana_task(req["task_text"], area["leader_asana_gid"])
            task_gid = created.get("gid", "")
        except Exception as e:
            logger.error(f"Error creando tarea de solicitud: {e}")
            await query.edit_message_text("❌ Error al crear la tarea en Asana.")
            return
        # Registrar en area_tasks
        try:
            register_area_task(
                task_gid=task_gid, task_name=req["task_text"],
                area_slug=slug, area_name=area["name"],
                assigned_to_tg_id=area["leader_tg_id"], assigned_to_name=area["leader_name"],
                leader_tg_id=area["leader_tg_id"],
            )
        except Exception:
            pass
        # Agregar al tablero del líder
        try:
            await add_task_to_member_project(task_gid, area["leader_asana_gid"], ASANA_TOKEN)
        except Exception:
            pass
        # Notificar al líder del área destino con opciones de delegación
        deleg_buttons = [
            [InlineKeyboardButton("✅ Asumir yo", callback_data=f"asumir_{task_gid}")],
        ]
        for m in area.get("members", []):
            deleg_buttons.append([InlineKeyboardButton(
                f"🔀 Delegar a {get_first_name(m['name'])}",
                callback_data=f"delegar_exec_{task_gid}_{m['tg_id']}_{slug}",
            )])
        deleg_buttons.append([InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")])
        try:
            await context.bot.send_message(
                chat_id=area["leader_tg_id"],
                text=(f"🏢 *Nueva tarea para {area['name']}*\n\n"
                      f"📌 *{req['task_text'][:200]}*\n\n"
                      f"_Enviada por {req['leader_name']} y aprobada por el manager._\n\n"
                      f"¿La asumes tú o la delegas?"),
                reply_markup=InlineKeyboardMarkup(deleg_buttons),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Error notificando líder área destino: {e}")
        # Notificar al líder solicitante
        try:
            await context.bot.send_message(
                chat_id=req["leader_tg_id"],
                text=(f"✅ *Tu solicitud fue asignada al área {area['name']}.*\n\n"
                      f"📌 _{req['task_text'][:100]}_"),
                parse_mode="Markdown",
            )
        except Exception:
            pass
        resolve_delegation_request(req_id)
        await query.edit_message_text(
            f"✅ *Tarea asignada al área {area['name']}.*\n\n"
            f"El líder fue notificado para asumir o delegar.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]),
        )

    elif data.startswith("req_ignore_"):
        req_id_str = data[len("req_ignore_"):]
        try:
            from teams_manager import resolve_delegation_request
            resolve_delegation_request(int(req_id_str))
        except Exception:
            pass
        await query.edit_message_text(
            "🚫 Solicitud ignorada.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]),
        )

    # ── Delegación de tareas de área ──────────────────────────────────────────
    elif data.startswith("asumir_"):
        task_gid = data[len("asumir_"):]
        # El líder decidió asumir la tarea (ya está asignada a él en Asana)
        await query.edit_message_text(
            f"✅ *Tarea asumida.*\nQueda en tu lista de pendientes.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas"),
                InlineKeyboardButton("⬅️ Menú",           callback_data="menu"),
            ]]),
        )
        try:
            await context.bot.send_message(
                chat_id=MANAGER_CHAT_ID,
                text=f"✅ El líder asumió la tarea del área (GID: {task_gid}).",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    elif data.startswith("delegar_exec_"):
        # "delegar_exec_{task_gid}_{member_tg_id}_{slug}"
        payload = data[len("delegar_exec_"):]
        parts   = payload.split("_", 2)  # max 2 splits → [task_gid, member_tg_id, slug]
        if len(parts) < 3:
            await query.edit_message_text("❌ Error en los datos de delegación.")
            return
        task_gid, member_tg_id_str, area_slug = parts
        member_tg_id = int(member_tg_id_str)

        from teams_manager import get_area, update_area_task_assignee
        area = get_area(area_slug)
        if not area:
            await query.edit_message_text("❌ Área no encontrada.")
            return

        member_info = next(
            (m for m in area.get("members", []) if m["tg_id"] == member_tg_id), None
        )
        if not member_info:
            await query.edit_message_text("❌ Miembro no encontrado en el área.")
            return

        # Reasignar en Asana
        try:
            await asana_put(f"/tasks/{task_gid}", {"assignee": member_info["asana_gid"]})
        except Exception as e:
            logger.error(f"Error reasignando tarea {task_gid}: {e}")

        # Agregar al tablero del miembro y al known_tasks
        try:
            await add_task_to_member_project(task_gid, member_info["asana_gid"], ASANA_TOKEN)
        except Exception:
            pass
        known_tasks.setdefault(member_info["asana_gid"], set()).add(task_gid)
        save_known_tasks()

        # Actualizar el registro de área (cambiar responsable)
        try:
            update_area_task_assignee(task_gid, member_tg_id, member_info["name"])
        except Exception as e:
            logger.warning(f"No se pudo actualizar asignado en area_tasks: {e}")

        # Notificar al miembro
        member_first = get_first_name(member_info["name"])
        try:
            await context.bot.send_message(
                chat_id=member_tg_id,
                text=(
                    f"🔀 *Tarea delegada, {member_first}*\n\n"
                    f"Tu líder te asignó una tarea del área *{area['name']}*.\n\n"
                    f"Revisa tus tareas pendientes."
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")
                ]]),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Error notificando a miembro delegado {member_tg_id}: {e}")

        # Confirmar al líder
        await query.edit_message_text(
            f"✅ *Tarea delegada a {member_info['name']}.*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Menú", callback_data="menu")
            ]]),
        )

# ── REPORTE ────────────────────────────────────────────────────────────────────

async def _send_report(bot):
    team  = load_team()
    today = datetime.now(TZ).strftime("%d/%m/%Y")
    all_tasks = {}
    for tg_id, info in team.items():
        if tg_id == MANAGER_CHAT_ID:
            continue
        try:
            all_tasks[tg_id] = await get_pending_tasks(info["asana_gid"])
        except Exception:
            all_tasks[tg_id] = []

    total    = sum(len(t) for t in all_tasks.values())
    total_od = sum(1 for tasks in all_tasks.values() for t in tasks if is_overdue(t))

    msg = f"📊 *Reporte del equipo — {today}*\n\n"
    for tg_id, tasks in all_tasks.items():
        info = team[tg_id]
        od   = sum(1 for t in tasks if is_overdue(t))
        status = "🟢" if not tasks else ("🔴" if od > 0 else "🟡")
        msg += f"{status} *{info['name']}*"
        if not tasks:
            msg += " — sin pendientes\n\n"
        else:
            msg += f" — {len(tasks)} pendiente(s){f', {od} vencida(s)' if od else ''}\n"
            for t in tasks:
                due  = f" _{t['due_on']}_" if t.get("due_on") else ""
                msg += f"   • {t['name']}{due}{' ⚠️' if is_overdue(t) else ''}\n"
            msg += "\n"

    msg += f"─────────────────\nTotal pendientes: *{total}*"
    if total_od:
        msg += f" | Vencidas: *{total_od}* ⚠️"

    await bot.send_message(chat_id=MANAGER_CHAT_ID, text=msg, parse_mode="Markdown")

# ── RECORDATORIOS ──────────────────────────────────────────────────────────────

async def send_reminder(bot, tg_id: int, name: str, tasks: list, session: str):
    if not tasks:
        return
    emoji   = "🌅" if session == "mañana" else "🌆"
    overdue = [t for t in tasks if is_overdue(t)]
    msg     = f"{emoji} *Hola {get_first_name(name)}, recordatorio de {session}*\n\n"
    msg    += f"Tienes *{len(tasks)}* tarea(s) pendiente(s):\n\n"
    for i, t in enumerate(tasks, 1):
        due  = f" — _{t['due_on']}_" if t.get("due_on") else ""
        warn = " ⚠️" if is_overdue(t) else ""
        msg += f"{i}. *{t['name']}*{due}{warn}\n"
    if overdue:
        msg += f"\n⚠️ *{len(overdue)} tarea(s) vencida(s)*"

    # Si hay una sola tarea, mostrar botones de estado directamente
    if len(tasks) == 1:
        gid = tasks[0]["gid"]
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⚙️ En ejecución", callback_data=f"set_status_{gid}_ej"),
                InlineKeyboardButton("🔍 En revisión",  callback_data=f"set_status_{gid}_rev"),
            ],
            [
                InlineKeyboardButton("✅ Completar",    callback_data=f"done_{gid}"),
                InlineKeyboardButton("💬 Comentar",     callback_data=f"task_comment_{gid}"),
            ],
            [InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")],
        ])
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Actualizar estado",   callback_data="status_menu")],
            [InlineKeyboardButton("✅ Completar una tarea", callback_data="completar_menu")],
            [InlineKeyboardButton("📋 Ver mis tareas",      callback_data="ver_tareas")],
        ])
    try:
        await bot.send_message(
            chat_id=tg_id, text=msg, reply_markup=keyboard, parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error enviando a {tg_id}: {e}")

async def _send_leader_area_reports(bot) -> None:
    """Envía a cada líder de área un resumen de las tareas de su equipo."""
    from teams_manager import load_teams, get_permissions
    perms = get_permissions()
    if not perms.get("leader_receives_reports", True):
        return
    team      = load_team()
    all_areas = load_teams()
    today_str = datetime.now(TZ).strftime("%d/%m/%Y")

    for slug, area in all_areas.items():
        leader_tg_id = area["leader_tg_id"]
        if not area.get("members"):
            continue
        msg = f"📊 *Resumen de equipo — {area['name']} — {today_str}*\n\n"
        has_data = False
        for m in area["members"]:
            m_tg_id = m["tg_id"]
            if m_tg_id not in team:
                continue
            try:
                tasks = await get_pending_tasks(team[m_tg_id]["asana_gid"])
            except Exception:
                tasks = []
            if not tasks:
                continue
            od    = sum(1 for t in tasks if is_overdue(t))
            status = "🔴" if od > 0 else "🟡"
            msg  += f"{status} *{get_first_name(m['name'])}* — {len(tasks)} pendiente(s)\n"
            for t in tasks[:2]:
                due  = f" _{t['due_on']}_" if t.get("due_on") else ""
                msg += f"  • {t['name'][:40]}{due}\n"
            msg += "\n"
            has_data = True
        if not has_data:
            continue
        try:
            await bot.send_message(
                chat_id=leader_tg_id, text=msg, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📊 Ver reporte completo", callback_data="leader_area_report")
                ]]),
            )
        except Exception as e:
            logger.error(f"Error enviando reporte de área a líder {leader_tg_id}: {e}")


async def job_morning(context: ContextTypes.DEFAULT_TYPE):
    team = load_team()
    for tg_id, info in team.items():
        tasks = await get_pending_tasks(info["asana_gid"])
        await send_reminder(context.bot, tg_id, info["name"], tasks, "mañana")
    await _send_leader_area_reports(context.bot)

async def job_afternoon(context: ContextTypes.DEFAULT_TYPE):
    team = load_team()
    for tg_id, info in team.items():
        tasks = await get_pending_tasks(info["asana_gid"])
        await send_reminder(context.bot, tg_id, info["name"], tasks, "tarde")
    await _send_leader_area_reports(context.bot)

async def send_evening_report(bot, tg_id: int, name: str, completed: list, pending: list):
    """Envía a cada persona su reporte de cierre del día."""
    first     = get_first_name(name)
    today_str = datetime.now(TZ).strftime("%d/%m/%Y")

    msg = f"🌙 *Reporte del día — {first} — {today_str}*\n\n"

    # ── Completadas ────────────────────────────────────────────────────────────
    if completed:
        msg += f"✅ *{len(completed)} tarea(s) completada(s) hoy:*\n"
        for t in completed:
            msg += f"  ✓ {t['name']}\n"
    else:
        msg += "📭 No completaste ninguna tarea hoy.\n"

    # ── Pendientes para mañana ─────────────────────────────────────────────────
    if pending:
        overdue = [t for t in pending if is_overdue(t)]
        msg += f"\n📋 *{len(pending)} pendiente(s) para mañana:*\n"
        for t in pending[:6]:
            due  = f" — _{t['due_on']}_" if t.get("due_on") else ""
            warn = " ⚠️" if is_overdue(t) else ""
            msg += f"  • {t['name'][:50]}{due}{warn}\n"
        if len(pending) > 6:
            msg += f"  _...y {len(pending) - 6} más_\n"
        if overdue:
            msg += f"\n⚠️ *{len(overdue)} tarea(s) vencida(s)*"
    else:
        msg += "\n🎉 *¡Sin pendientes para mañana!* Buen trabajo."

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Ver mis tareas",  callback_data="ver_tareas"),
        InlineKeyboardButton("✅ Completar tarea", callback_data="completar_menu"),
    ]])
    try:
        await bot.send_message(
            chat_id=tg_id, text=msg, reply_markup=keyboard, parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error enviando reporte vespertino a {tg_id}: {e}")


async def job_evening_report(context: ContextTypes.DEFAULT_TYPE):
    """Job 6pm: reporte individual de tareas completadas + pendientes del día."""
    team = load_team()
    for tg_id, info in team.items():
        try:
            completed = await get_completed_tasks_today(info["asana_gid"])
            pending   = await get_pending_tasks(info["asana_gid"])
            await send_evening_report(context.bot, tg_id, info["name"], completed, pending)
        except Exception as e:
            logger.error(f"job_evening_report — error con {info['name']}: {e}")


async def job_daily_report(context: ContextTypes.DEFAULT_TYPE):
    await _send_report(context.bot)


# ── Reconfiguración dinámica sin redeploy ──────────────────────────────────────

def _apply_config(cfg: dict):
    """Actualiza las variables globales con la nueva config."""
    global MORNING_HOUR, MORNING_MIN, AFTERNOON_HOUR, AFTERNOON_MIN
    global REPORT_HOUR, REPORT_MIN, EVENING_HOUR, EVENING_MIN
    global CHECK_INTERVAL_MINUTES, TZ
    MORNING_HOUR           = int(cfg.get("MORNING_HOUR",           MORNING_HOUR))
    MORNING_MIN            = int(cfg.get("MORNING_MIN",            MORNING_MIN))
    AFTERNOON_HOUR         = int(cfg.get("AFTERNOON_HOUR",         AFTERNOON_HOUR))
    AFTERNOON_MIN          = int(cfg.get("AFTERNOON_MIN",          AFTERNOON_MIN))
    REPORT_HOUR            = int(cfg.get("REPORT_HOUR",            REPORT_HOUR))
    REPORT_MIN             = int(cfg.get("REPORT_MIN",             REPORT_MIN))
    EVENING_HOUR           = int(cfg.get("EVENING_HOUR",           EVENING_HOUR))
    EVENING_MIN            = int(cfg.get("EVENING_MIN",            EVENING_MIN))
    CHECK_INTERVAL_MINUTES = int(cfg.get("CHECK_INTERVAL_MINUTES", CHECK_INTERVAL_MINUTES))
    tz_name = cfg.get("TIMEZONE", "")
    if tz_name:
        try:
            TZ = pytz.timezone(tz_name)
        except Exception:
            pass


def _reschedule_daily_jobs(jq):
    """Cancela los jobs diarios actuales y los reprograma con los horarios nuevos."""
    global _daily_jobs
    # Cancelar jobs anteriores
    for name, job in list(_daily_jobs.items()):
        try:
            job.schedule_removal()
        except Exception:
            pass
    _daily_jobs = {}
    # Reprogramar
    _daily_jobs["morning"]   = jq.run_daily(job_morning,        time(MORNING_HOUR,   MORNING_MIN,   tzinfo=TZ))
    _daily_jobs["afternoon"] = jq.run_daily(job_afternoon,      time(AFTERNOON_HOUR, AFTERNOON_MIN, tzinfo=TZ))
    _daily_jobs["evening"]   = jq.run_daily(job_evening_report, time(EVENING_HOUR,   EVENING_MIN,   tzinfo=TZ))
    _daily_jobs["report"]    = jq.run_daily(job_daily_report,   time(REPORT_HOUR,    REPORT_MIN,    tzinfo=TZ))
    logger.info(
        f"⚙️ Jobs reprogramados — mañana:{MORNING_HOUR}:{MORNING_MIN:02d} "
        f"tarde:{AFTERNOON_HOUR}:{AFTERNOON_MIN:02d} "
        f"vespertino:{EVENING_HOUR}:{EVENING_MIN:02d} "
        f"manager:{REPORT_HOUR}:{REPORT_MIN:02d}"
    )


async def job_check_config(context: ContextTypes.DEFAULT_TYPE):
    """
    Cada 5 min: comprueba si la config cambió en la DB.
    Si cambió, actualiza variables globales y reprograma los jobs diarios.
    No requiere redeploy del Worker.
    """
    global _config_snapshot
    try:
        from db import db_get
        cfg = db_get("config") or {}
    except Exception:
        return
    if not cfg:
        return

    snapshot = str(sorted(cfg.items()))
    if snapshot == _config_snapshot:
        return  # Sin cambios

    _apply_config(cfg)
    _reschedule_daily_jobs(context.job_queue)
    _config_snapshot = snapshot
    logger.info("⚙️ Config actualizado desde DB sin redeploy")

# ── DETECCIÓN DE TAREAS NUEVAS ─────────────────────────────────────────────────

async def _get_task_creator(task_gid: str) -> str:
    """Devuelve el asana_gid del usuario que creó la tarea, o '' si no se puede obtener."""
    try:
        from utils import http_client, ASANA_BASE
        r = await http_client.get(
            f"{ASANA_BASE}/tasks/{task_gid}",
            headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
            params={"opt_fields": "created_by"},
            timeout=8,
        )
        return r.json().get("data", {}).get("created_by", {}).get("gid", "")
    except Exception:
        return ""

async def job_check_new_tasks(context: ContextTypes.DEFAULT_TYPE):
    """
    Respaldo de detección de tareas nuevas (cada CHECK_INTERVAL_MINUTES).
    El webhook de Asana es el mecanismo primario; este job actúa como fallback.
    - Omite tareas ya notificadas por el webhook (DB 'notified_tasks').
    - Omite tareas que la persona se asignó a sí misma.
    """
    from db import db_get, db_set
    notified_db = set(db_get("notified_tasks") or [])

    team = load_team()
    for tg_id, info in team.items():
        asana_gid = info["asana_gid"]
        try:
            current_tasks = await get_pending_tasks(asana_gid)
            current_gids  = {t["gid"] for t in current_tasks}

            if asana_gid not in known_tasks:
                # Primera vez que vemos este miembro: registrar sin notificar
                known_tasks[asana_gid] = current_gids
                continue

            new_tasks = [t for t in current_tasks if t["gid"] not in known_tasks[asana_gid]]
            notified_now = []
            for task in new_tasks:
                gid = task["gid"]
                # Saltar si el webhook ya notificó esta tarea
                if gid in notified_db:
                    continue
                # Saltar si la persona se asignó la tarea a sí misma
                creator = await _get_task_creator(gid)
                if creator and creator == asana_gid:
                    logger.info(f"Fallback: {info['name']} creó tarea propia — sin notif")
                    notified_db.add(gid)
                    continue
                due      = f"\n📅 Vence: *{task['due_on']}*" if task.get("due_on") else ""
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")
                ]])
                await context.bot.send_message(
                    chat_id      = tg_id,
                    text         = f"🔔 *¡Nueva tarea, {get_first_name(info['name'])}!*\n\n📌 *{task['name']}*{due}",
                    reply_markup = keyboard,
                    parse_mode   = "Markdown",
                )
                notified_db.add(gid)
                notified_now.append(gid)

            known_tasks[asana_gid] = current_gids
            if notified_now:
                save_known_tasks()
        except Exception as e:
            logger.error(f"Error revisando {info['name']}: {e}")

    # Actualizar set compartido con el web service
    db_set("notified_tasks", list(notified_db)[-10_000:])

# ── COMANDOS ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)

async def cmd_mi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 Tu ID de Telegram es:\n`{update.effective_user.id}`\n\nPásaselo a Marco para registrarte.",
        parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# CREAR TAREA PROPIA (cualquier usuario se auto-asigna)
# ══════════════════════════════════════════════════════════════════════════════

async def self_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    tg_id = update.effective_user.id
    team  = load_team()
    if tg_id not in team:
        msg = "❌ No estás registrado. Contacta a Marco."
        if query:
            await query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return ConversationHandler.END

    msg = "📝 *Crear mi tarea*\n\nEscribe el nombre de la tarea:"
    if query:
        await query.edit_message_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")
    return SELF_TASK_NAME

async def self_task_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task_name = update.message.text.strip()
    context.user_data["self_task"] = {"name": task_name}
    tg_id = update.effective_user.id
    team  = load_team()

    today    = datetime.now(TZ).date()
    tomorrow = today + timedelta(days=1)
    week_end = today + timedelta(days=(4 - today.weekday()) % 7 or 7)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"Hoy ({today.strftime('%d/%m')})",       callback_data=f"sdue_{today}"),
            InlineKeyboardButton(f"Mañana ({tomorrow.strftime('%d/%m')})", callback_data=f"sdue_{tomorrow}"),
        ],
        [
            InlineKeyboardButton(f"Esta semana ({week_end.strftime('%d/%m')})", callback_data=f"sdue_{week_end}"),
            InlineKeyboardButton("📅 Elegir fecha",                               callback_data="sdue_custom"),
        ],
        [InlineKeyboardButton("Sin fecha límite", callback_data="sdue_none")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="menu")],
    ])
    await update.message.reply_text(
        f"📝 *{task_name}*\n\n📅 ¿Cuándo vence?",
        reply_markup=keyboard, parse_mode="Markdown"
    )
    return SELF_TASK_DUE

async def self_task_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "sdue_custom":
        await query.edit_message_text(
            "📅 Escribe la fecha: *DD/MM/AAAA*", parse_mode="Markdown"
        )
        return SELF_TASK_DUE_CUSTOM

    due_on = None if data == "sdue_none" else data[5:]
    context.user_data["self_task"]["due_on"] = due_on
    return await self_task_ask_notes(update, context)

async def self_task_due_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        d = datetime.strptime(text, "%d/%m/%Y").date()
        context.user_data["self_task"]["due_on"] = d.strftime("%Y-%m-%d")
    except ValueError:
        await update.message.reply_text(
            "❌ Formato inválido. Escribe: `25/04/2026`", parse_mode="Markdown"
        )
        return SELF_TASK_DUE_CUSTOM
    return await self_task_ask_notes(update, context)

async def self_task_ask_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pregunta opcionalmente por una descripción antes de crear la tarea propia."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Crear sin descripción", callback_data="sself_skip_notes")],
        [InlineKeyboardButton("✏️ Agregar descripción",  callback_data="sself_add_notes")],
        [InlineKeyboardButton("❌ Cancelar",              callback_data="menu")],
    ])
    task = context.user_data["self_task"]
    msg = (f"📝 *{task['name']}*\n📅 {due_label(task.get('due_on'))}\n\n"
           "¿Quieres agregar una descripción?")
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    return SELF_TASK_NOTES

async def self_task_notes_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "sself_skip_notes":
        return await self_task_create(update, context)
    await query.edit_message_text(
        "✏️ *Escribe la descripción:*\n_(O /skip para continuar sin descripción)_",
        parse_mode="Markdown"
    )
    return SELF_TASK_NOTES

async def self_task_notes_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() != "/skip":
        context.user_data["self_task"]["notes"] = text
    return await self_task_create(update, context)

async def self_task_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    team  = load_team()
    task  = context.user_data["self_task"]
    info  = team[tg_id]
    due_on = task.get("due_on")

    try:
        created  = await create_asana_task(task["name"], info["asana_gid"], due_on,
                                            notes=task.get("notes"))
        task_gid = created.get("gid", "")
    except Exception as e:
        logger.error(f"Error creando tarea propia: {e}")
        msg = "❌ Error al crear la tarea. Intenta de nuevo."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return ConversationHandler.END

    # Fix double-notification + agregar al proyecto
    asana_gid = info["asana_gid"]
    if asana_gid not in known_tasks:
        known_tasks[asana_gid] = set()
    known_tasks[asana_gid].add(task_gid)
    save_known_tasks()
    try:
        await add_task_to_member_project(task_gid, asana_gid, ASANA_TOKEN)
    except Exception:
        pass

    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    if due_on and task_gid:
        register_unique_task(task_gid, today_str, due_on)

    due_str = due_label(due_on)
    msg = (
        f"✅ *Tarea creada:*\n\n"
        f"📌 *{task['name']}*\n"
        f"📅 Vence: {due_str}"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]])
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")

    context.user_data.pop("self_task", None)
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# ACTUALIZAR ESTADO DE TAREA (tablero Asana)
# ══════════════════════════════════════════════════════════════════════════════

STATUS_MAP = {
    "ej":   "⚙️ En ejecución",
    "rev":  "🔍 En revisión",
    "comp": "✅ Completado",
    "bloq": "🚫 Bloqueado",
}

async def status_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra la lista de tareas para elegir a cuál cambiar el estado."""
    query = update.callback_query
    await query.answer()
    tg_id = update.effective_user.id
    team  = load_team()

    if tg_id not in team:
        await query.edit_message_text("❌ No estás registrado.")
        return

    tasks = await get_pending_tasks(team[tg_id]["asana_gid"])
    if not tasks:
        await query.edit_message_text(
            "✅ No tienes tareas pendientes.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]),
        )
        return

    buttons = []
    for t in tasks:
        warn  = "⚠️ " if is_overdue(t) else ""
        label = f"{warn}{t['name']}"[:58]
        buttons.append([InlineKeyboardButton(label, callback_data=f"status_task_{t['gid']}")])
    buttons.append([InlineKeyboardButton("⬅️ Menú", callback_data="menu")])

    await query.edit_message_text(
        "🔄 *¿De cuál tarea quieres actualizar el estado?*",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )

async def status_task_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra las opciones de estado para una tarea específica."""
    query    = update.callback_query
    await query.answer()
    task_gid = query.data[len("status_task_"):]
    tg_id    = update.effective_user.id
    team     = load_team()

    tasks     = await get_pending_tasks(team[tg_id]["asana_gid"])
    task_name = next((t["name"] for t in tasks if t["gid"] == task_gid), "Tarea")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚙️ En ejecución", callback_data=f"set_status_{task_gid}_ej"),
            InlineKeyboardButton("🔍 En revisión",  callback_data=f"set_status_{task_gid}_rev"),
        ],
        [
            InlineKeyboardButton("✅ Completar",    callback_data=f"done_{task_gid}"),
            InlineKeyboardButton("🚫 Bloqueado",    callback_data=f"set_status_{task_gid}_bloq"),
        ],
        [InlineKeyboardButton("💬 Comentar",        callback_data=f"task_comment_{task_gid}")],
        [InlineKeyboardButton("⬅️ Volver",          callback_data="status_menu")],
    ])
    await query.edit_message_text(
        f"🔄 *{task_name}*\n\n¿Cuál es el nuevo estado?",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )

async def set_task_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aplica el cambio de estado moviendo la tarea en Asana."""
    query  = update.callback_query
    await query.answer()
    parts  = query.data[len("set_status_"):].rsplit("_", 1)
    if len(parts) != 2:
        return
    task_gid, status_code = parts
    section_name = STATUS_MAP.get(status_code)
    if not section_name:
        return

    tg_id = update.effective_user.id
    team  = load_team()

    if tg_id not in team:
        await query.edit_message_text("❌ No estás registrado.")
        return

    tasks     = await get_pending_tasks(team[tg_id]["asana_gid"])
    task_name = next((t["name"] for t in tasks if t["gid"] == task_gid), "Tarea")
    asana_gid = team[tg_id]["asana_gid"]

    success = await move_task_status(task_gid, asana_gid, section_name, ASANA_TOKEN)

    if success:
        await query.edit_message_text(
            f"✅ *Estado actualizado*\n\n📌 *{task_name}*\n🔄 → {section_name}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Actualizar otra", callback_data="status_menu")],
                [InlineKeyboardButton("⬅️ Menú",            callback_data="menu")],
            ]),
            parse_mode="Markdown",
        )
        # Notificar al manager
        if tg_id != MANAGER_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=MANAGER_CHAT_ID,
                    text=(
                        f"🔄 *{team[tg_id]['name']}* actualizó estado:\n"
                        f"📌 _{task_name}_\n→ {section_name}"
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass
    else:
        await query.edit_message_text(
            f"⚠️ El proyecto de tablero aún no está configurado.\n"
            f"El manager puede activarlo reiniciando el bot.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]),
        )

async def request_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pide al usuario que escriba un comentario para la tarea."""
    query    = update.callback_query
    await query.answer()
    task_gid = query.data[len("task_comment_"):]
    tg_id    = update.effective_user.id
    team     = load_team()

    tasks     = await get_pending_tasks(team[tg_id]["asana_gid"])
    task_name = next((t["name"] for t in tasks if t["gid"] == task_gid), "Tarea")

    context.user_data["awaiting_comment_for"]   = task_gid
    context.user_data["awaiting_comment_name"]  = task_name

    await query.edit_message_text(
        f"💬 *Comentar tarea:*\n📌 _{task_name}_\n\nEscribe tu comentario:",
        parse_mode="Markdown",
    )

# ══════════════════════════════════════════════════════════════════════════════
# TEXTO LIBRE → TAREA (lenguaje natural via Gemini) + COMENTARIOS
# ══════════════════════════════════════════════════════════════════════════════

async def handle_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Maneja mensajes de texto libres (fuera de ConversationHandlers):
      1. Si hay comentario pendiente → lo agrega a Asana.
      2. Si es el manager → intenta detectar una tarea con Gemini.
      3. Otros → mensaje de ayuda.
    """
    text  = update.message.text.strip()
    tg_id = update.effective_user.id
    team  = load_team()

    # ── 0. Guard: si el usuario está en medio de un flujo de menú, no activar IA ─
    if context.user_data.get("awaiting_task_name"):
        await update.message.reply_text(
            "⚠️ Estás creando una tarea. Escribe el *nombre de la tarea* o usa /menu para cancelar.",
            parse_mode="Markdown",
        )
        return
    if context.user_data.get("new_task") and not context.user_data.get("nl_task_draft"):
        await update.message.reply_text(
            "⚠️ Usa los botones para continuar con la tarea en curso, o /menu para cancelar.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]),
        )
        return

    # ── 1. Comentario pendiente ────────────────────────────────────────────────
    task_gid   = context.user_data.pop("awaiting_comment_for", None)
    task_name  = context.user_data.pop("awaiting_comment_name", "")
    if task_gid:
        first_name = get_first_name(team[tg_id]["name"]) if tg_id in team else "Alguien"
        full_comment = f"[{first_name} via Bot] {text}"
        try:
            await add_task_comment(task_gid, full_comment, ASANA_TOKEN)
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]])
            await update.message.reply_text(
                f"💬 Comentario agregado a:\n📌 *{task_name}*",
                reply_markup=keyboard, parse_mode="Markdown",
            )
            if tg_id != MANAGER_CHAT_ID:
                try:
                    await context.bot.send_message(
                        chat_id=MANAGER_CHAT_ID,
                        text=(
                            f"💬 *{team[tg_id]['name']}* comentó en:\n"
                            f"📌 _{task_name}_\n\n_{text}_"
                        ),
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Error agregando comentario: {e}")
            await update.message.reply_text("❌ No pude agregar el comentario. Intenta de nuevo.")
        return

    # ── 2. NL awaiting custom date ────────────────────────────────────────────
    if context.user_data.get("nl_awaiting_date"):
        context.user_data.pop("nl_awaiting_date", None)
        try:
            d = datetime.strptime(text, "%d/%m/%Y").date()
            draft = context.user_data.get("nl_task_draft", {})
            draft["due_on"] = d.strftime("%Y-%m-%d")
            context.user_data["nl_task_draft"] = draft
            await _show_nl_draft(update, context)
        except ValueError:
            await update.message.reply_text(
                "❌ Formato inválido. Escribe: `25/04/2026`\nO escribe /menu para cancelar.",
                parse_mode="Markdown",
            )
        return

    # ── 3. Detección de tarea con Gemini (manager y equipo) ──────────────────
    if tg_id not in team and tg_id != MANAGER_CHAT_ID:
        await update.message.reply_text(
            "❓ No entendí ese mensaje. Usa el /menu para ver las opciones."
        )
        return

    if not GEMINI_API_KEY:
        await update.message.reply_text(
            "⚙️ Para crear tareas por texto configura GEMINI_API_KEY.\n"
            "Usa el menú para crear tareas."
        )
        return

    # Mostrar indicador de carga
    loading = await update.message.reply_text("🤖 Analizando...")

    today_str  = datetime.now(TZ).strftime("%Y-%m-%d")
    team_names = [info["name"] for _, info in get_members(team)]

    try:
        raw_tasks = await call_gemini(text, None, None, team_names, today_str)
        tasks     = enrich_tasks(raw_tasks, team)
    except GeminiError as e:
        await loading.delete()
        await update.message.reply_text(
            e.user_message(), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]),
        )
        return
    except Exception as e:
        logger.error(f"Error NL task Gemini: {e}")
        await loading.delete()
        await update.message.reply_text("❌ Error al procesar el mensaje. Intenta de nuevo.")
        return

    await loading.delete()

    if not tasks:
        await update.message.reply_text(
            "🤔 No detecté ninguna tarea en ese mensaje.\n"
            "Usa *➕ Crear tarea* en el menú, o escribe algo como:\n"
            "_\"Alexandra: revisar cotización MDF el viernes\"_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Menú", callback_data="menu")]]),
        )
        return

    # Guardar el primer task como draft (procesamos uno a la vez)
    context.user_data["nl_task_draft"] = tasks[0]
    if len(tasks) > 1:
        context.user_data["nl_task_draft"]["_extra_count"] = len(tasks) - 1
    await _show_nl_draft(update, context)

async def _show_nl_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el borrador de tarea detectada por Gemini."""
    draft     = context.user_data.get("nl_task_draft", {})
    task_name = draft.get("task_name", "Sin nombre")
    assignee  = draft.get("assignee_name")
    due_on    = draft.get("due_on")
    area_name = draft.get("area_name")
    extra     = draft.get("_extra_count", 0)

    who  = assignee.split("(")[0].strip() if assignee else "❓ Sin responsable"
    when = due_label(due_on) if due_on else "❓ Sin fecha"

    msg = f"🤖 *Tarea detectada:*\n\n📌 *{task_name}*\n👤 {who}  |  📅 {when}"
    if area_name:
        msg += f"\n🏢 {area_name}"
    if extra:
        msg += f"\n\n_+ {extra} tarea(s) más detectada(s) — créalas desde el menú._"

    buttons = []

    # Si falta el responsable → mostrar selector
    if not draft.get("assignee_tg_id"):
        msg += "\n\n👤 *¿A quién se la asigno?*"
        team    = load_team()
        members = get_members(team)
        row     = []
        for tid, info in members:
            first = get_first_name(info["name"])
            row.append(InlineKeyboardButton(first, callback_data=f"nl_assign_{tid}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data="nl_task_cancel")])

    # Si falta la fecha → mostrar selector
    elif not due_on:
        msg += "\n\n📅 *¿Cuándo vence?*"
        today    = datetime.now(TZ).date()
        tomorrow = today + timedelta(days=1)
        week_end = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
        buttons = [
            [
                InlineKeyboardButton(f"Hoy ({today.strftime('%d/%m')})",        callback_data=f"nl_due_{today}"),
                InlineKeyboardButton(f"Mañana ({tomorrow.strftime('%d/%m')})",  callback_data=f"nl_due_{tomorrow}"),
            ],
            [
                InlineKeyboardButton(f"Esta semana ({week_end.strftime('%d/%m')})", callback_data=f"nl_due_{week_end}"),
                InlineKeyboardButton("📅 Otra fecha",                                callback_data="nl_due_custom"),
            ],
            [InlineKeyboardButton("Sin fecha", callback_data="nl_due_none")],
            [
                InlineKeyboardButton("👤 Cambiar responsable", callback_data="nl_change_assignee"),
                InlineKeyboardButton("❌ Cancelar",            callback_data="nl_task_cancel"),
            ],
        ]

    # Todo completo → confirmar con opciones de cambio y área
    else:
        buttons = [
            [InlineKeyboardButton("✅ Crear tarea", callback_data="nl_task_confirm")],
            [
                InlineKeyboardButton("👤 Cambiar responsable", callback_data="nl_change_assignee"),
                InlineKeyboardButton("📅 Cambiar fecha",       callback_data="nl_change_due"),
            ],
            [InlineKeyboardButton("🏢 Asignar a área", callback_data="nl_area_start")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="nl_task_cancel")],
        ]

    keyboard = InlineKeyboardMarkup(buttons)

    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")

async def nl_assign_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tid   = int(query.data[len("nl_assign_"):])
    team  = load_team()

    if tid not in team:
        return

    draft = context.user_data.get("nl_task_draft", {})
    info  = team[tid]
    draft["assignee_tg_id"] = tid
    draft["assignee_gid"]   = info["asana_gid"]
    draft["assignee_name"]  = info["name"]
    context.user_data["nl_task_draft"] = draft
    await _show_nl_draft(update, context)

async def nl_due_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data[len("nl_due_"):]
    draft = context.user_data.get("nl_task_draft", {})

    if data == "custom":
        context.user_data["nl_awaiting_date"] = True
        await query.edit_message_text(
            "📅 Escribe la fecha en formato *DD/MM/AAAA*:", parse_mode="Markdown"
        )
        return

    draft["due_on"] = None if data == "none" else data
    context.user_data["nl_task_draft"] = draft
    await _show_nl_draft(update, context)

async def nl_change_assignee_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Limpia el responsable del draft NL y muestra el selector."""
    query = update.callback_query
    await query.answer()
    draft = context.user_data.get("nl_task_draft", {})
    for k in ("assignee_tg_id", "assignee_gid", "assignee_name"):
        draft.pop(k, None)
    context.user_data["nl_task_draft"] = draft
    await _show_nl_draft(update, context)

async def nl_change_due_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Limpia la fecha del draft NL y muestra el selector."""
    query = update.callback_query
    await query.answer()
    draft = context.user_data.get("nl_task_draft", {})
    draft.pop("due_on", None)
    context.user_data["nl_task_draft"] = draft
    await _show_nl_draft(update, context)

async def nl_area_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra lista de áreas para asignar la tarea NL."""
    query = update.callback_query
    await query.answer()
    from teams_manager import load_teams
    teams = load_teams()
    if not teams:
        await query.answer("No hay áreas configuradas.", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(f"🏢 {a['name']}", callback_data=f"nl_area_{slug}")]
               for slug, a in teams.items()]
    buttons.append([InlineKeyboardButton("🚫 Sin área", callback_data="nl_area_none")])
    buttons.append([InlineKeyboardButton("◀️ Volver", callback_data="nl_back_draft")])
    await query.edit_message_text(
        "🏢 *¿A qué área pertenece esta tarea?*",
        reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown",
    )

async def nl_area_select_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Asigna área al draft NL y vuelve al borrador."""
    query = update.callback_query
    await query.answer()
    slug = query.data[len("nl_area_"):]
    draft = context.user_data.get("nl_task_draft", {})
    if slug == "none":
        draft.pop("area_slug", None)
        draft.pop("area_name", None)
        draft.pop("area_leader_tg_id", None)
    else:
        from teams_manager import get_area
        area = get_area(slug)
        if area:
            draft["area_slug"]         = slug
            draft["area_name"]         = area["name"]
            draft["area_leader_tg_id"] = area.get("leader_tg_id")
    context.user_data["nl_task_draft"] = draft
    await _show_nl_draft(update, context)

async def nl_task_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = context.user_data.pop("nl_task_draft", {})

    if not draft or not draft.get("assignee_gid"):
        await query.edit_message_text("❌ Faltan datos. Usa ➕ Crear tarea desde el menú.")
        return

    try:
        created  = await create_asana_task(
            draft["task_name"], draft["assignee_gid"], draft.get("due_on")
        )
        task_gid = created.get("gid", "")
    except Exception as e:
        logger.error(f"Error creando NL task: {e}")
        await query.edit_message_text("❌ Error al crear la tarea. Intenta de nuevo.")
        return

    # Fix double-notification + agregar al proyecto
    agid = draft["assignee_gid"]
    if agid not in known_tasks:
        known_tasks[agid] = set()
    known_tasks[agid].add(task_gid)
    save_known_tasks()
    try:
        await add_task_to_member_project(task_gid, agid, ASANA_TOKEN)
    except Exception:
        pass

    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    if draft.get("due_on") and task_gid:
        register_unique_task(task_gid, today_str, draft["due_on"])

    # Registrar como tarea de área si aplica
    if draft.get("area_slug") and task_gid:
        from teams_manager import register_area_task
        try:
            register_area_task(
                task_gid=task_gid,
                task_name=draft["task_name"],
                area_slug=draft["area_slug"],
                area_name=draft["area_name"],
                assigned_to_tg_id=draft["assignee_tg_id"],
                assigned_to_name=draft["assignee_name"],
                leader_tg_id=draft.get("area_leader_tg_id", MANAGER_CHAT_ID),
                due_on=draft.get("due_on"),
            )
        except Exception as e:
            logger.warning(f"No se pudo registrar tarea de área NL: {e}")

    first_name = get_first_name(draft["assignee_name"])
    due_str    = due_label(draft.get("due_on"))

    # Notificar al responsable
    try:
        area_line = f"\n🏢 {draft['area_name']}" if draft.get("area_name") else ""
        await context.bot.send_message(
            chat_id=draft["assignee_tg_id"],
            text=(
                f"🔔 *¡Nueva tarea, {first_name}!*\n\n"
                f"📌 *{draft['task_name']}*\n"
                f"📅 Vence: {due_str}{area_line}"
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")
            ]]),
            parse_mode="Markdown",
        )
    except Exception:
        pass

    area_info = f"\n🏢 {draft['area_name']}" if draft.get("area_name") else ""
    await query.edit_message_text(
        f"🎉 *¡Tarea creada!*\n\n📌 *{draft['task_name']}*\n👤 {draft['assignee_name'].split('(')[0].strip()}  |  📅 {due_str}{area_info}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]),
    )

# ══════════════════════════════════════════════════════════════════════════════
# MINUTA DE REUNIÓN (Gemini extrae tareas masivas)
# ══════════════════════════════════════════════════════════════════════════════

async def minuta_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()

    msg = (
        "📄 *Cargar minuta de reunión*\n\n"
        "Envía el contenido de la minuta en cualquier formato:\n"
        "• ✍️ Texto pegado directamente\n"
        "• 📷 Foto de la pizarra o documento\n"
        "• 📎 Archivo PDF\n\n"
        "_Gemini extraerá todas las tareas y las asignará automáticamente._"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]])
    if query:
        await query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    return MINUTA_WAIT

async def minuta_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe texto, foto o PDF y llama a Gemini."""
    tg_id = update.effective_user.id
    team  = load_team()

    text        = None
    image_bytes = None
    mime_type   = None
    raw_text    = ""

    if update.message.text:
        text     = update.message.text.strip()
        raw_text = text

    elif update.message.photo:
        loading = await update.message.reply_text("📷 Procesando imagen...")
        photo   = update.message.photo[-1]  # mejor resolución
        f       = await photo.get_file()
        image_bytes = bytes(await f.download_as_bytearray())
        mime_type   = "image/jpeg"
        raw_text    = "[imagen]"
        await loading.delete()

    elif update.message.document:
        doc = update.message.document
        if doc.mime_type != "application/pdf":
            await update.message.reply_text(
                "❌ Solo acepto archivos PDF. Envía el texto o una foto."
            )
            return MINUTA_WAIT
        loading = await update.message.reply_text("📎 Procesando PDF...")
        f       = await doc.get_file()
        image_bytes = bytes(await f.download_as_bytearray())
        mime_type   = "application/pdf"
        raw_text    = "[PDF]"
        await loading.delete()

    else:
        await update.message.reply_text(
            "❌ No reconocí ese formato. Envía texto, foto o PDF."
        )
        return MINUTA_WAIT

    loading = await update.message.reply_text("🤖 Extrayendo tareas con Gemini...")

    today_str  = datetime.now(TZ).strftime("%Y-%m-%d")
    team_names = [info["name"] for _, info in get_members(team)]

    try:
        raw_tasks = await call_gemini(text, image_bytes, mime_type, team_names, today_str)
        tasks     = enrich_tasks(raw_tasks, team)
    except GeminiError as e:
        await loading.delete()
        await update.message.reply_text(
            e.user_message(), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]),
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error minuta Gemini: {e}")
        await loading.delete()
        await update.message.reply_text("❌ Error inesperado. Intenta de nuevo.")
        return MINUTA_WAIT

    await loading.delete()

    context.user_data["minuta_tasks"]    = tasks
    context.user_data["minuta_raw_text"] = raw_text
    context.user_data["minuta_fix_idx"]  = None

    return await minuta_show_review(update, context)

async def minuta_show_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el resumen de tareas extraídas para revisión."""
    tasks      = context.user_data.get("minuta_tasks", [])
    need_fix   = tasks_need_fixing(tasks)
    preview    = format_tasks_preview(tasks)

    msg = f"📄 *Tareas extraídas ({len(tasks)}):*\n\n{preview}\n\n"

    if need_fix:
        msg += "⚠️ *Algunas tareas tienen datos incompletos.* Puedes corregirlas o crearlas igual."
        buttons = [
            [InlineKeyboardButton("✏️ Corregir incompletas",  callback_data="minuta_fix_next")],
            [InlineKeyboardButton("✅ Crear todas igual",       callback_data="minuta_confirm_all")],
            [InlineKeyboardButton("❌ Cancelar",               callback_data="menu")],
        ]
    else:
        msg += "✅ Todas las tareas están completas. ¿Las creo en Asana?"
        buttons = [
            [InlineKeyboardButton("✅ Crear todas",  callback_data="minuta_confirm_all")],
            [InlineKeyboardButton("❌ Cancelar",     callback_data="menu")],
        ]

    keyboard = InlineKeyboardMarkup(buttons)
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    return MINUTA_REVIEW

async def minuta_fix_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enruta acciones de corrección de minuta."""
    query = update.callback_query
    await query.answer()
    data  = query.data[len("minuta_fix_"):]

    tasks    = context.user_data.get("minuta_tasks", [])
    fix_idx  = context.user_data.get("minuta_fix_idx")

    if data == "next":
        # Buscar siguiente tarea incompleta
        start = (fix_idx + 1) if fix_idx is not None else 0
        idx   = next_incomplete_idx(tasks, start)
        if idx is None:
            return await minuta_show_review(update, context)
        context.user_data["minuta_fix_idx"] = idx
        task = tasks[idx]

        msg = (
            f"✏️ *Corregir tarea {idx + 1}/{len(tasks)}:*\n\n"
            f"📌 *{task['task_name']}*\n"
        )
        buttons = []
        if not task.get("assignee_tg_id"):
            msg += "👤 *Sin responsable asignado*\n\n¿A quién se la asigno?"
            team    = load_team()
            members = get_members(team)
            row     = []
            for tid, info in members:
                row.append(InlineKeyboardButton(
                    get_first_name(info["name"]), callback_data=f"minuta_fix_assign_{tid}"
                ))
                if len(row) == 3:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
        elif not task.get("due_on"):
            msg += "📅 *Sin fecha de vencimiento*\n\n¿Cuándo vence?"
            today    = datetime.now(TZ).date()
            tomorrow = today + timedelta(days=1)
            week_end = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
            buttons = [
                [
                    InlineKeyboardButton(f"Hoy ({today.strftime('%d/%m')})",        callback_data=f"minuta_fix_date_{today}"),
                    InlineKeyboardButton(f"Mañana ({tomorrow.strftime('%d/%m')})",  callback_data=f"minuta_fix_date_{tomorrow}"),
                ],
                [
                    InlineKeyboardButton(f"Esta semana ({week_end.strftime('%d/%m')})", callback_data=f"minuta_fix_date_{week_end}"),
                    InlineKeyboardButton("Sin fecha",                                    callback_data="minuta_fix_date_none"),
                ],
            ]
        buttons.append([InlineKeyboardButton("⏭️ Saltar", callback_data="minuta_fix_next")])
        buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data="menu")])
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

    elif data.startswith("assign_"):
        tid   = int(data[len("assign_"):])
        team  = load_team()
        idx   = context.user_data.get("minuta_fix_idx", 0)
        info  = team.get(tid)
        if info and idx < len(tasks):
            tasks[idx]["assignee_tg_id"] = tid
            tasks[idx]["assignee_gid"]   = info["asana_gid"]
            tasks[idx]["assignee_name"]  = info["name"]
            context.user_data["minuta_tasks"] = tasks
        await minuta_fix_dispatch_next(update, context)

    elif data.startswith("date_"):
        date_str = data[len("date_"):]
        idx      = context.user_data.get("minuta_fix_idx", 0)
        if idx < len(tasks):
            tasks[idx]["due_on"] = None if date_str == "none" else date_str
            context.user_data["minuta_tasks"] = tasks
        await minuta_fix_dispatch_next(update, context)

async def minuta_fix_dispatch_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Avanza a la siguiente tarea incompleta o vuelve al resumen."""
    tasks    = context.user_data.get("minuta_tasks", [])
    fix_idx  = context.user_data.get("minuta_fix_idx", 0)
    next_idx = next_incomplete_idx(tasks, fix_idx + 1)
    if next_idx is not None:
        # Apuntar directamente al siguiente índice incompleto y mostrar su prompt
        context.user_data["minuta_fix_idx"] = next_idx
        await minuta_fix_dispatch(update, context)
    else:
        await minuta_show_review(update, context)

async def minuta_confirm_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Crea todas las tareas en Asana."""
    query  = update.callback_query
    await query.answer()
    tasks  = context.user_data.get("minuta_tasks", [])
    tg_id  = update.effective_user.id
    team   = load_team()

    if not tasks:
        await query.edit_message_text("❌ No hay tareas para crear.")
        return ConversationHandler.END

    await query.edit_message_text(f"⏳ Creando {len(tasks)} tarea(s) en Asana...")

    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    created   = []
    errors    = []

    for task in tasks:
        if not task.get("assignee_gid"):
            errors.append(task["task_name"])
            continue
        try:
            result   = await create_asana_task(
                task["task_name"], task["assignee_gid"], task.get("due_on")
            )
            task_gid = result.get("gid", "")
            task["created_gid"] = task_gid

            # Fix doble notificación + agregar al proyecto
            agid = task["assignee_gid"]
            if agid not in known_tasks:
                known_tasks[agid] = set()
            known_tasks[agid].add(task_gid)
            try:
                await add_task_to_member_project(task_gid, agid, ASANA_TOKEN)
            except Exception:
                pass

            if task.get("due_on") and task_gid:
                register_unique_task(task_gid, today_str, task["due_on"])

            # Notificar al responsable
            first_name = get_first_name(task["assignee_name"])
            due_str    = due_label(task.get("due_on"))
            try:
                await context.bot.send_message(
                    chat_id=task["assignee_tg_id"],
                    text=(
                        f"🔔 *¡Nueva tarea, {first_name}!*\n\n"
                        f"📌 *{task['task_name']}*\n"
                        f"📅 Vence: {due_str}"
                    ),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")
                    ]]),
                    parse_mode="Markdown",
                )
            except Exception:
                pass

            created.append(task)
        except Exception as e:
            logger.error(f"Error creando tarea de minuta '{task['task_name']}': {e}")
            errors.append(task["task_name"])

    save_known_tasks()

    # Guardar historial
    submitter_name = team.get(tg_id, {}).get("name", "Manager")
    record = build_minuta_record(
        tg_id, submitter_name,
        context.user_data.get("minuta_raw_text", ""),
        created, TZ,
    )
    save_minuta(record)

    msg = f"🎉 *Minuta procesada*\n\n✅ *{len(created)}* tarea(s) creada(s)"
    if errors:
        msg += f"\n⚠️ *{len(errors)}* sin responsable (no se crearon):\n"
        msg += "\n".join(f"  • _{n}_" for n in errors)

    await context.bot.send_message(
        chat_id=tg_id, text=msg,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]),
        parse_mode="Markdown",
    )

    for key in ["minuta_tasks", "minuta_raw_text", "minuta_fix_idx"]:
        context.user_data.pop(key, None)
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# AGREGAR MIEMBRO DEL EQUIPO DESDE TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

async def team_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    msg = (
        "👤 *Agregar nuevo miembro*\n\n"
        "Escribe el *nombre completo* del colaborador\n"
        "_(incluye su área, ej: \"Andrea García (Ventas)\")_"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]])
    if query:
        await query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    return TEAM_ADD_NAME

async def team_add_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_member"] = {"name": update.message.text.strip()}
    await update.message.reply_text(
        "📱 Ahora pídele al colaborador que abra el bot y escriba `/mi_id`\n\n"
        "Cuando tengas su ID, escríbelo aquí:"
    )
    return TEAM_ADD_TGID

async def team_add_receive_tgid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tg_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Eso no parece un ID válido. Escribe solo números:")
        return TEAM_ADD_TGID
    context.user_data["new_member"]["tg_id"] = tg_id
    await update.message.reply_text(
        "🔗 Por último, necesito su *GID de Asana*.\n\n"
        "Lo encuentras en:\n"
        f"`https://app.asana.com/api/1.0/users?workspace={ASANA_WORKSPACE}`\n\n"
        "Escribe el GID (solo números):",
        parse_mode="Markdown",
    )
    return TEAM_ADD_ASANA

async def team_add_receive_asana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asana_gid = update.message.text.strip()
    context.user_data["new_member"]["asana_gid"] = asana_gid

    member = context.user_data["new_member"]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirmar y agregar", callback_data="team_add_confirm")],
        [InlineKeyboardButton("❌ Cancelar",            callback_data="menu")],
    ])
    await update.message.reply_text(
        f"👤 *Confirmar nuevo miembro:*\n\n"
        f"📛 Nombre: *{member['name']}*\n"
        f"📱 Telegram ID: `{member['tg_id']}`\n"
        f"🔗 Asana GID: `{asana_gid}`\n\n"
        f"Se creará su proyecto en Asana automáticamente.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return TEAM_ADD_ASANA

async def team_add_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    member = context.user_data.pop("new_member", {})
    if not member:
        await query.edit_message_text("❌ Error: no hay datos del nuevo miembro.")
        return ConversationHandler.END

    await query.edit_message_text("⏳ Agregando miembro y creando su proyecto en Asana...")

    # Agregar a team.txt
    success = add_member(member["tg_id"], member["asana_gid"], member["name"])
    if not success:
        await context.bot.send_message(
            chat_id=MANAGER_CHAT_ID,
            text="❌ El Telegram ID ya existe en team.txt. Verifica el archivo.",
        )
        return ConversationHandler.END

    # Crear proyecto en Asana
    try:
        await ensure_member_project(
            member["asana_gid"], member["name"], ASANA_WORKSPACE, ASANA_TOKEN
        )
        project_ok = True
    except Exception as e:
        logger.error(f"Error creando proyecto para {member['name']}: {e}")
        project_ok = False

    msg = (
        f"✅ *{member['name']}* fue agregado al equipo.\n\n"
        f"📱 Telegram ID: `{member['tg_id']}`\n"
    )
    msg += "📁 Proyecto en Asana creado ✅" if project_ok else "⚠️ No se pudo crear el proyecto en Asana. Revisa el token."

    # Notificar al nuevo miembro
    try:
        await context.bot.send_message(
            chat_id=member["tg_id"],
            text=(
                f"👋 *¡Bienvenido/a al equipo, {member['name'].split()[0]}!*\n\n"
                "Ya estás registrado/a en el bot de Lubrikca.\n"
                "Escribe /menu para ver tus tareas."
            ),
            parse_mode="Markdown",
        )
    except Exception:
        msg += "\n⚠️ No pude notificar al nuevo miembro (verifica el Telegram ID)."

    await context.bot.send_message(
        chat_id=MANAGER_CHAT_ID, text=msg, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data="menu")]]),
    )
    return ConversationHandler.END

# ── POST-INIT: crea proyectos Asana para todos los miembros al arrancar ────────

async def _register_existing_tasks_to_boards() -> tuple[int, int]:
    """
    Registra todas las tareas pendientes de Asana en el tablero "📌 Pendiente"
    de cada miembro del equipo. Se ejecuta una sola vez al primer despliegue.
    Devuelve (agregadas, omitidas).
    """
    from asana_projects import load_projects
    team     = load_team()
    projects = load_projects()
    headers  = {"Authorization": f"Bearer {ASANA_TOKEN}"}
    added    = 0
    skipped  = 0

    for tg_id, info in team.items():
        if tg_id == MANAGER_CHAT_ID:
            continue
        asana_gid   = info["asana_gid"]
        project_cfg = projects.get(asana_gid)
        if not project_cfg:
            continue
        project_gid = project_cfg.get("project_gid")
        section_gid = project_cfg.get("sections", {}).get("📌 Pendiente")
        if not project_gid or not section_gid:
            continue

        # Obtener tareas pendientes del miembro
        try:
            r = await http_client.get(
                f"{ASANA_BASE}/tasks",
                headers=headers,
                params={
                    "assignee": asana_gid, "workspace": ASANA_WORKSPACE,
                    "completed_since": "now",
                    "opt_fields": "gid,name,memberships.project.gid",
                    "limit": 100,
                },
                timeout=20,
            )
            r.raise_for_status()
            tasks = r.json().get("data", [])
        except Exception as e:
            logger.error(f"Error obteniendo tareas de {info['name']}: {e}")
            continue

        for task in tasks:
            task_gid = task["gid"]
            # Verificar si la tarea ya está en el proyecto del tablero
            existing_projects = {
                m["project"]["gid"]
                for m in task.get("memberships", [])
                if m.get("project")
            }
            if project_gid in existing_projects:
                skipped += 1
                continue
            try:
                r2 = await http_client.post(
                    f"{ASANA_BASE}/sections/{section_gid}/addTask",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"data": {"task": task_gid}},
                    timeout=15,
                )
                r2.raise_for_status()
                added += 1
                await asyncio.sleep(0.25)
            except Exception as e:
                logger.warning(f"No se pudo registrar tarea {task_gid}: {e}")

    return added, skipped


async def post_init(application: Application) -> None:
    """Crea (o verifica) el proyecto Kanban en Asana de cada miembro del equipo."""
    if not ASANA_TOKEN or not ASANA_WORKSPACE:
        logger.warning("post_init: ASANA_TOKEN o ASANA_WORKSPACE no configurados, omitiendo.")
        return
    team = load_team()
    for tg_id, info in team.items():
        if tg_id == MANAGER_CHAT_ID:
            continue
        try:
            await ensure_member_project(
                info["asana_gid"], info["name"], ASANA_WORKSPACE, ASANA_TOKEN
            )
            logger.info(f"✅ Proyecto Asana asegurado para {info['name']}")
        except Exception as e:
            logger.error(f"Error creando proyecto para {info['name']}: {e}")

    # ── Registro único de tareas existentes en tableros ────────────────────────
    from db import db_get, db_set
    if not db_get("boards_registered"):
        logger.info("📋 Primera vez — registrando tareas existentes en tableros Kanban...")
        try:
            added, skipped = await _register_existing_tasks_to_boards()
            logger.info(f"✅ Tableros: {added} registradas, {skipped} ya existían")
            db_set("boards_registered", True)
        except Exception as e:
            logger.error(f"Error en registro inicial de tableros: {e}")

# ── MAIN ───────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# MOVER TAREA ENTRE TABLEROS — v5.0
# ══════════════════════════════════════════════════════════════════════════════

async def mover_tarea_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Paso 1: Mostrar las tareas pendientes del usuario para elegir cuál mover."""
    query  = update.callback_query
    await query.answer()
    tg_id  = update.effective_user.id
    team   = load_team()

    if tg_id not in team:
        await query.edit_message_text("❌ No estás registrado.")
        return

    tasks = await get_pending_tasks(team[tg_id]["asana_gid"])
    if not tasks:
        await query.edit_message_text(
            "✅ No tienes tareas pendientes para mover.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Menú", callback_data="menu")
            ]]))
        return

    buttons = []
    for t in tasks:
        warn  = "⚠️ " if is_overdue(t) else ""
        label = f"{warn}{t['name']}"[:60]
        buttons.append([InlineKeyboardButton(label, callback_data=f"mover_task_{t['gid']}")])
    buttons.append([InlineKeyboardButton("⬅️ Cancelar", callback_data="menu")])

    await query.edit_message_text(
        "🔀 *¿Cuál tarea quieres mover?*\nElige y te mostraré los tableros disponibles:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )

async def mover_elegir_proyecto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Paso 2: Mostrar los proyectos disponibles en Asana."""
    query    = update.callback_query
    await query.answer()
    task_gid = query.data[len("mover_task_"):]
    tg_id    = update.effective_user.id
    team     = load_team()

    context.user_data["mover_task_gid"] = task_gid

    tasks = await get_pending_tasks(team[tg_id]["asana_gid"])
    task_name = next((t["name"] for t in tasks if t["gid"] == task_gid), "Tarea")
    context.user_data["mover_task_name"] = task_name

    try:
        projects = await get_task_projects(task_gid, ASANA_TOKEN)
        if not projects:
            projects = await get_workspace_projects(ASANA_WORKSPACE, ASANA_TOKEN)
    except Exception as e:
        logger.error(f"Error obteniendo proyectos: {e}")
        await query.edit_message_text(
            "❌ Error al obtener los proyectos de Asana. Intenta de nuevo.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Menú", callback_data="menu")
            ]]))
        return

    if not projects:
        await query.edit_message_text(
            "ℹ️ No hay proyectos disponibles en Asana.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Menú", callback_data="menu")
            ]]))
        return

    buttons = []
    for p in projects:
        label = p["name"][:55]
        buttons.append([InlineKeyboardButton(label, callback_data=f"mover_proj_{p['gid']}")])
    buttons.append([InlineKeyboardButton("⬅️ Cancelar", callback_data="mover_start")])

    await query.edit_message_text(
        f"🔀 *Mover tarea:*\n📌 _{task_name}_\n\n📁 ¿A qué proyecto/tablero?",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )

async def mover_elegir_seccion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Paso 3: Mostrar las secciones del proyecto elegido."""
    query      = update.callback_query
    await query.answer()
    project_gid = query.data[len("mover_proj_"):]
    task_name   = context.user_data.get("mover_task_name", "Tarea")
    task_gid    = context.user_data.get("mover_task_gid", "")

    context.user_data["mover_project_gid"] = project_gid

    try:
        sections = await get_project_sections(project_gid, ASANA_TOKEN)
    except Exception as e:
        logger.error(f"Error obteniendo secciones: {e}")
        await query.edit_message_text(
            "❌ Error al obtener las secciones. Intenta de nuevo.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Menú", callback_data="menu")
            ]]))
        return

    if not sections:
        await query.edit_message_text(
            "ℹ️ Este proyecto no tiene secciones configuradas.\n"
            "Agrega secciones en Asana (ej: Pendiente, En progreso, Completado) y vuelve a intentarlo.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Volver", callback_data=f"mover_task_{task_gid}"),
                InlineKeyboardButton("⬅️ Menú",   callback_data="menu"),
            ]]))
        return

    try:
        current_section = await get_task_current_section(task_gid, project_gid, ASANA_TOKEN)
    except Exception:
        current_section = None

    buttons = []
    for s in sections:
        current_mark = " ← actual" if current_section and s["name"] == current_section else ""
        label = f"{s['name']}{current_mark}"[:60]
        buttons.append([InlineKeyboardButton(label, callback_data=f"mover_sec_{s['gid']}|{s['name']}")])
    buttons.append([InlineKeyboardButton("⬅️ Cambiar proyecto", callback_data=f"mover_task_{task_gid}")])
    buttons.append([InlineKeyboardButton("❌ Cancelar",          callback_data="menu")])

    msg = f"🔀 *Mover tarea:*\n📌 _{task_name}_\n\n"
    if current_section:
        msg += f"📍 Sección actual: *{current_section}*\n\n"
    msg += "🗂️ ¿A qué sección la movemos?"

    await query.edit_message_text(
        msg,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )

async def mover_confirmar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Paso 4: Confirmación antes de mover."""
    query = update.callback_query
    await query.answer()

    payload      = query.data[len("mover_sec_"):]
    sec_gid, sec_name = payload.split("|", 1)

    context.user_data["mover_section_gid"]  = sec_gid
    context.user_data["mover_section_name"] = sec_name

    task_name = context.user_data.get("mover_task_name", "Tarea")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Sí, mover a '{sec_name}'", callback_data="mover_conf_yes")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="menu")],
    ])
    await query.edit_message_text(
        f"🔀 *Confirmar movimiento:*\n\n"
        f"📌 _{task_name}_\n"
        f"🗂️ Nueva sección: *{sec_name}*\n\n"
        f"¿Confirmas?",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def mover_ejecutar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Paso 5: Ejecutar el movimiento en Asana."""
    query = update.callback_query
    await query.answer()

    task_gid     = context.user_data.get("mover_task_gid", "")
    task_name    = context.user_data.get("mover_task_name", "Tarea")
    section_gid  = context.user_data.get("mover_section_gid", "")
    section_name = context.user_data.get("mover_section_name", "")
    tg_id        = update.effective_user.id
    team         = load_team()

    try:
        await move_task_to_section(task_gid, section_gid, ASANA_TOKEN)
    except Exception as e:
        logger.error(f"Error moviendo tarea: {e}")
        await query.edit_message_text(
            "❌ Error al mover la tarea en Asana. Intenta de nuevo.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Menú", callback_data="menu")
            ]]))
        return

    for key in ["mover_task_gid", "mover_task_name", "mover_project_gid",
                "mover_section_gid", "mover_section_name"]:
        context.user_data.pop(key, None)

    await query.edit_message_text(
        f"✅ *¡Tarea movida exitosamente!*\n\n"
        f"📌 *{task_name}*\n"
        f"🗂️ Ahora está en: *{section_name}*",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔀 Mover otra tarea", callback_data="mover_start")],
            [InlineKeyboardButton("⬅️ Menú",             callback_data="menu")],
        ]),
        parse_mode="Markdown"
    )

    if tg_id != MANAGER_CHAT_ID:
        try:
            user_name = team.get(tg_id, {}).get("name", "Alguien")
            await context.bot.send_message(
                chat_id=MANAGER_CHAT_ID,
                text=(
                    f"🔀 *{user_name}* movió una tarea:\n"
                    f"📌 _{task_name}_\n"
                    f"🗂️ → *{section_name}*"
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════════════
# ESCALACIÓN AUTOMÁTICA v4.0
# ══════════════════════════════════════════════════════════════════════════════

from escalation import (
    should_remind_before_due, should_escalate_overdue,
    mark_alert_sent, is_task_blocked, cleanup_alert_state,
    load_alert_state, save_alert_state,
    get_freq_for_task, days_until_due, hours_since_due,
    register_unique_task, register_noncompliance, DAYS_LABEL,
)

async def job_escalation(context: ContextTypes.DEFAULT_TYPE, session: str = "pm"):
    team        = load_team()
    rec_data    = load_recurring()
    all_gids    = set()
    alert_state = load_alert_state()   # carga ÚNICA — se pasa a todas las funciones

    for tg_id, info in team.items():
        if tg_id == MANAGER_CHAT_ID:
            continue

        try:
            tasks = await get_pending_tasks(info["asana_gid"])
        except Exception:
            continue

        for task in tasks:
            gid    = task["gid"]
            due_on = task.get("due_on")
            freq   = get_freq_for_task(gid, rec_data)
            all_gids.add(gid)

            if not due_on:
                continue

            first_name = get_first_name(info["name"])

            # sin I/O: pasa alert_state en memoria
            pre_alerts = should_remind_before_due(gid, due_on, freq, TZ, state=alert_state)
            for alert_key in pre_alerts:
                days  = days_until_due(due_on, TZ)
                label = DAYS_LABEL.get(alert_key, f"{days} día(s)")
                msg = (
                    f"⏰ *Recordatorio, {first_name}*\n\n"
                    f"📌 *{task['name']}*\n"
                    f"📅 Vence en *{label}* ({due_on})\n\n"
                    f"Recuerda completarla a tiempo."
                )
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")
                ]])
                try:
                    await context.bot.send_message(
                        chat_id=tg_id, text=msg,
                        reply_markup=keyboard, parse_mode="Markdown")
                    mark_alert_sent(gid, alert_key, state=alert_state)   # sin I/O
                    logger.info(f"Alerta anticipada '{alert_key}' → {info['name']}: {task['name']}")
                except Exception as e:
                    logger.error(f"Error alerta anticipada: {e}")

            esc_key, register_nc = should_escalate_overdue(
                gid, due_on, session, TZ, state=alert_state)   # sin I/O
            if not esc_key:
                continue

            hours = hours_since_due(due_on, TZ) or 0

            if hours < 24:
                icon  = "⚠️"
                level = "Tarea vencida"
            elif hours < 48:
                icon  = "🚨"
                level = "Vencida +24h — sin completar"
            elif hours < 72:
                icon  = "🔴"
                level = "URGENTE — Vencida +48h"
            else:
                icon  = "⛔"
                level = "CRÍTICO — Vencida +72h — registrada como no cumplida"

            hours_int = int(hours)
            time_str  = f"{hours_int}h" if hours_int > 0 else "recién vencida"

            esc_msg = (
                f"{icon} *{level}*\n\n"
                f"📌 *{task['name']}*\n"
                f"👤 {info['name']}\n"
                f"📅 Venció: {due_on} (hace {time_str})\n"
            )
            if register_nc:
                esc_msg += (
                    "\n⛔ *Registrada como no cumplida.*\n"
                    "Las nuevas tareas recurrentes continúan creándose normalmente."
                )

            keyboard_mgr = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"📋 Ver tareas de {get_first_name(info['name'])}",
                                     callback_data="reporte")
            ]])
            try:
                await context.bot.send_message(
                    chat_id=MANAGER_CHAT_ID, text=esc_msg,
                    reply_markup=keyboard_mgr, parse_mode="Markdown")
                mark_alert_sent(gid, esc_key, state=alert_state)   # sin I/O
                if register_nc:
                    # Registrar en tabla de no-cumplimiento y decrementar pending_count
                    rec_data_local = load_recurring()
                    rec_name = get_freq_for_task(gid, rec_data_local)
                    register_noncompliance(
                        task_gid=gid,
                        task_name=task["name"],
                        assignee_name=info["name"],
                        assignee_tg_id=tg_id,
                        due_on=due_on,
                        hours_overdue=hours,
                        recurring_name=rec_name,
                    )
                    # Decrementar pending_count de la recurrente correspondiente
                    rec_changed = False
                    for entry in rec_data_local:
                        if entry.get("last_task_gid") == gid and entry.get("pending_count", 0) > 0:
                            entry["pending_count"] = max(0, entry["pending_count"] - 1)
                            rec_changed = True
                            break
                    if rec_changed:
                        save_recurring(rec_data_local)
                logger.info(f"Escalación '{esc_key}' → manager: {info['name']} / {task['name']}")
            except Exception as e:
                logger.error(f"Error escalación: {e}")

    cleanup_alert_state(all_gids, state=alert_state)   # limpia en memoria
    save_alert_state(alert_state)                       # guarda ÚNICA vez

async def job_escalation_am(context: ContextTypes.DEFAULT_TYPE):
    await job_escalation(context, session="am")

async def job_escalation_pm(context: ContextTypes.DEFAULT_TYPE):
    await job_escalation(context, session="pm")

async def job_friday_summary(context: ContextTypes.DEFAULT_TYPE):
    """Viernes tarde — resumen de pendientes para la semana siguiente."""
    now = datetime.now(TZ)
    if now.weekday() != 4:
        return

    team = load_team()
    msg  = "📋 *Resumen del viernes — Pendientes para la semana*\n\n"
    has_pending = False

    for tg_id, info in team.items():
        if tg_id == MANAGER_CHAT_ID:
            continue
        try:
            tasks = await get_pending_tasks(info["asana_gid"])
            if tasks:
                has_pending = True
                first_name  = get_first_name(info["name"])
                task_msg = (
                    f"📋 *Hola {first_name}, resumen del viernes*\n\n"
                    f"Tienes *{len(tasks)}* tarea(s) pendiente(s) para la próxima semana:\n\n"
                )
                for i, t in enumerate(tasks, 1):
                    due  = f" — _{t['due_on']}_" if t.get("due_on") else ""
                    warn = " ⚠️" if is_overdue(t) else ""
                    task_msg += f"{i}. *{t['name']}*{due}{warn}\n"
                task_msg += "\n¡Que tengas buen fin de semana! 🎉"
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")
                ]])
                await context.bot.send_message(
                    chat_id=tg_id, text=task_msg,
                    reply_markup=keyboard, parse_mode="Markdown")

                od = sum(1 for t in tasks if is_overdue(t))
                status = "🔴" if od > 0 else "🟡"
                msg += f"{status} *{info['name']}* — {len(tasks)} pendiente(s)\n"
                for t in tasks:
                    due = f" _{t['due_on']}_" if t.get("due_on") else ""
                    msg += f"   • {t['name']}{due}\n"
                msg += "\n"
        except Exception as e:
            logger.error(f"Error resumen viernes {info['name']}: {e}")

    if has_pending:
        msg += "─────────────────\n_Resumen enviado a cada miembro del equipo._"
        await context.bot.send_message(
            chat_id=MANAGER_CHAT_ID, text=msg, parse_mode="Markdown")

async def job_sunday_summary(context: ContextTypes.DEFAULT_TYPE):
    """Domingo tarde — recordatorio de pendientes para la semana."""
    now = datetime.now(TZ)
    if now.weekday() != 6:
        return

    team = load_team()
    for tg_id, info in team.items():
        if tg_id == MANAGER_CHAT_ID:
            continue
        try:
            tasks = await get_pending_tasks(info["asana_gid"])
            if not tasks:
                continue
            first_name = get_first_name(info["name"])
            msg = (
                f"🌅 *Hola {first_name}, preparando la semana*\n\n"
                f"Tienes *{len(tasks)}* tarea(s) pendiente(s):\n\n"
            )
            for i, t in enumerate(tasks, 1):
                due  = f" — _{t['due_on']}_" if t.get("due_on") else ""
                warn = " ⚠️" if is_overdue(t) else ""
                msg += f"{i}. *{t['name']}*{due}{warn}\n"
            msg += "\n¡Mañana empieza la semana con todo! 💪"
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")
            ]])
            await context.bot.send_message(
                chat_id=tg_id, text=msg,
                reply_markup=keyboard, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Error resumen domingo {info['name']}: {e}")

# ══════════════════════════════════════════════════════════════════════════════

def main():
    # Inicializar DB antes de arrancar el bot
    from db import setup_db, db_set
    setup_db()
    # Sincronizar estado de la API key de Gemini a la DB (lo lee el servicio Web)
    db_set("has_gemini_key", bool(GEMINI_API_KEY))

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # ── ConversationHandler: manager crea tarea para un colaborador ───────────
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("tarea", crear_tarea_start),
            CallbackQueryHandler(crear_tarea_start,          pattern="^crear_tarea_start$"),
            CallbackQueryHandler(leader_create_task_start,   pattern="^leader_create_task$"),
            CommandHandler("cancel", cancel_conv),
        ],
        states={
            TASK_ASSIGNEE: [
                CallbackQueryHandler(handle_area_start,   pattern="^assign_area_start$"),
                CallbackQueryHandler(handle_assignee,     pattern="^assign_broadcast$"),
                CallbackQueryHandler(handle_assignee,     pattern="^assign_\\d"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_name_text),
            ],
            AREA_TASK_AREA: [
                CallbackQueryHandler(handle_area_select,  pattern="^areasel_"),
                CallbackQueryHandler(leader_area_pick,    pattern="^leader_area_pick_"),
            ],
            TASK_DUE: [
                CallbackQueryHandler(handle_due, pattern="^due_"),
            ],
            TASK_DUE_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_due_custom),
            ],
            TASK_RECURRING: [
                CallbackQueryHandler(handle_recurring_choice, pattern="^rec_(yes|no)$"),
                CallbackQueryHandler(handle_task_confirm,     pattern="^task_confirm_yes$"),
                CallbackQueryHandler(handle_task_add_desc,    pattern="^task_add_desc$"),
            ],
            TASK_NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_notes_input),
            ],
            TASK_FREQ: [
                CallbackQueryHandler(handle_freq, pattern="^freq_"),
            ],
            TASK_TIMES_PER_DAY: [
                CallbackQueryHandler(handle_times_per_day, pattern="^times_"),
            ],
            TASK_HOURS: [
                CallbackQueryHandler(handle_hour_select, pattern="^hour_"),
            ],
            TASK_WEEKDAY: [
                CallbackQueryHandler(handle_weekday, pattern="^wday_"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_conv, pattern="^menu$"),
            CommandHandler("menu",   cmd_menu),
            CommandHandler("cancel", cancel_conv),
        ],
        per_message=False,
    )

    # ── ConversationHandler: colaborador crea su propia tarea ─────────────────
    self_task_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(self_task_start, pattern="^self_task_start$")],
        states={
            SELF_TASK_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, self_task_receive_name),
            ],
            SELF_TASK_DUE: [
                CallbackQueryHandler(self_task_due, pattern="^sdue_"),
            ],
            SELF_TASK_DUE_CUSTOM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, self_task_due_custom),
            ],
            SELF_TASK_NOTES: [
                CallbackQueryHandler(self_task_notes_choice, pattern="^sself_(skip|add)_notes$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, self_task_notes_input),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_conv, pattern="^menu$"),
            CommandHandler("menu",   cmd_menu),
            CommandHandler("cancel", cancel_conv),
        ],
        per_message=False,
    )

    # ── ConversationHandler: cargar minuta de reunión ─────────────────────────
    minuta_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(minuta_start, pattern="^minuta_start$")],
        states={
            MINUTA_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, minuta_receive),
                MessageHandler(filters.PHOTO,                   minuta_receive),
                MessageHandler(filters.Document.ALL,            minuta_receive),
            ],
            MINUTA_REVIEW: [
                CallbackQueryHandler(minuta_fix_dispatch, pattern="^minuta_fix_"),
                CallbackQueryHandler(minuta_confirm_all,  pattern="^minuta_confirm_all$"),
            ],
            MINUTA_FIX_ASSIGN: [
                CallbackQueryHandler(minuta_fix_dispatch, pattern="^minuta_fix_"),
            ],
            MINUTA_FIX_DATE: [
                CallbackQueryHandler(minuta_fix_dispatch, pattern="^minuta_fix_"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_conv, pattern="^menu$"),
            CommandHandler("menu",   cmd_menu),
            CommandHandler("cancel", cancel_conv),
        ],
        per_message=False,
    )

    # ── ConversationHandler: agregar nuevo miembro desde Telegram ─────────────
    team_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(team_add_start, pattern="^team_add_start$")],
        states={
            TEAM_ADD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, team_add_receive_name),
            ],
            TEAM_ADD_TGID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, team_add_receive_tgid),
            ],
            TEAM_ADD_ASANA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, team_add_receive_asana),
                CallbackQueryHandler(team_add_confirm_handler,   pattern="^team_add_confirm$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_conv, pattern="^menu$"),
            CommandHandler("menu",   cmd_menu),
            CommandHandler("cancel", cancel_conv),
        ],
        per_message=False,
    )

    # ── ConversationHandler: líder solicita tarea al manager ─────────────────
    leader_req_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(leader_req_task_start, pattern="^leader_req_task_start$")],
        states={
            LEADER_REQ_TASK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, leader_req_task_receive),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_conv, pattern="^menu$"),
            CommandHandler("menu",   cmd_menu),
            CommandHandler("cancel", cancel_conv),
        ],
        per_message=False,
    )

    # ── Registrar handlers (orden importa: ConversationHandlers primero) ───────
    app.add_handler(conv_handler)
    app.add_handler(self_task_handler)
    app.add_handler(minuta_handler)
    app.add_handler(team_handler)
    app.add_handler(leader_req_handler)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(CommandHandler("mi_id", cmd_mi_id))
    app.add_handler(CallbackQueryHandler(button_handler))
    # Texto libre: comentarios, NL tasks, fechas custom para NL
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_text))

    # ── Jobs programados ───────────────────────────────────────────────────────
    jq = app.job_queue

    # Jobs diarios — guardamos referencias para poder reprogramarlos sin redeploy
    _daily_jobs["morning"]   = jq.run_daily(job_morning,        time(MORNING_HOUR,   MORNING_MIN,   tzinfo=TZ))
    _daily_jobs["afternoon"] = jq.run_daily(job_afternoon,      time(AFTERNOON_HOUR, AFTERNOON_MIN, tzinfo=TZ))
    _daily_jobs["evening"]   = jq.run_daily(job_evening_report, time(EVENING_HOUR,   EVENING_MIN,   tzinfo=TZ))
    _daily_jobs["report"]    = jq.run_daily(job_daily_report,   time(REPORT_HOUR,    REPORT_MIN,    tzinfo=TZ))

    # Escalación — corre junto a los recordatorios de mañana y tarde
    jq.run_daily(job_escalation_am,  time(MORNING_HOUR,   MORNING_MIN,   tzinfo=TZ))
    jq.run_daily(job_escalation_pm,  time(AFTERNOON_HOUR, AFTERNOON_MIN, tzinfo=TZ))

    # Resúmenes de fin de semana (viernes y domingo a las 3pm)
    jq.run_daily(job_friday_summary, time(AFTERNOON_HOUR, AFTERNOON_MIN, tzinfo=TZ))
    jq.run_daily(job_sunday_summary, time(AFTERNOON_HOUR, AFTERNOON_MIN, tzinfo=TZ))

    # Jobs de fondo
    jq.run_repeating(job_check_new_tasks,           interval=CHECK_INTERVAL_MINUTES * 60, first=10)
    jq.run_repeating(job_process_recurring,         interval=60 * 30, first=30)
    jq.run_repeating(job_check_recurring_completed, interval=60 * 60, first=60)
    # Detector de cambios de config: cada 5 min sin redeploy
    jq.run_repeating(job_check_config,              interval=60 * 5,   first=60)

    logger.info(
        f"✅ Bot Lubrikca v6.0 listo | "
        f"Recordatorios: {MORNING_HOUR}:{MORNING_MIN:02d} y {AFTERNOON_HOUR}:{AFTERNOON_MIN:02d} | "
        f"Vespertino: {EVENING_HOUR}:{EVENING_MIN:02d} | "
        f"Reporte mgr: {REPORT_HOUR}:{REPORT_MIN:02d} | Config dinámica: ON"
    )
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

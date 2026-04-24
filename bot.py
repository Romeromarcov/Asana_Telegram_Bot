"""
Bot de Telegram para seguimiento de tareas de Asana — Lubrikca
Versión 3.0 — Botones interactivos + Crear tareas + Tareas recurrentes
"""

import os
import json
import logging
from datetime import datetime, time, date, timedelta
from pathlib import Path
import pytz
import httpx
import google.generativeai as genai
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel('gemini-1.5-flash')
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)
from minuta import (
    call_gemini, enrich_tasks, format_tasks_preview, tasks_need_fixing,
    next_incomplete_idx, save_minuta, load_minutas, build_minuta_record,
    match_assignee,
)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── CONFIGURACIÓN ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN         = os.environ["TELEGRAM_TOKEN"]
ASANA_TOKEN            = os.environ["ASANA_TOKEN"]
ASANA_WORKSPACE        = os.environ["ASANA_WORKSPACE_ID"]
MANAGER_CHAT_ID        = int(os.environ["MANAGER_CHAT_ID"])
TIMEZONE               = os.environ.get("TIMEZONE", "America/Caracas")
MORNING_HOUR           = int(os.environ.get("MORNING_HOUR",   "9"))
MORNING_MIN            = int(os.environ.get("MORNING_MIN",    "0"))
AFTERNOON_HOUR         = int(os.environ.get("AFTERNOON_HOUR", "15"))
AFTERNOON_MIN          = int(os.environ.get("AFTERNOON_MIN",  "0"))
REPORT_HOUR            = int(os.environ.get("REPORT_HOUR",    "18"))
REPORT_MIN             = int(os.environ.get("REPORT_MIN",     "0"))
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "5"))

ASANA_BASE = "https://app.asana.com/api/1.0"
TZ         = pytz.timezone(TIMEZONE)

# Archivos de datos
RECURRING_FILE = Path(__file__).parent / "recurring.json"

# Memoria de tareas conocidas {asana_gid: set(task_gids)}
known_tasks: dict[str, set] = {}

# Estados del ConversationHandler para crear tarea
# + estados del flujo de minutas (8-11)
(
    TASK_ASSIGNEE,
    TASK_DUE,
    TASK_DUE_CUSTOM,
    TASK_RECURRING,
    TASK_FREQ,
    TASK_TIMES_PER_DAY,
    TASK_HOURS,
    TASK_WEEKDAY,
    # Minutas
    MINUTA_WAITING_CONTENT,
    MINUTA_CONFIRM,
    MINUTA_FIX_ASSIGNEE,
    MINUTA_FIX_DUE,
) = range(12)

# ── CARGA DEL EQUIPO ───────────────────────────────────────────────────────────

def load_team() -> dict:
    team = {}
    team_file = Path(__file__).parent / "team.txt"
    if not team_file.exists():
        return team
    for line in team_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        try:
            tg_id = int(parts[0])
            team[tg_id] = {
                "asana_gid": parts[1],
                "name": parts[2] if len(parts) > 2 else f"Usuario {tg_id}"
            }
        except ValueError:
            pass
    return team

def get_members(team: dict) -> list:
    """Devuelve miembros del equipo sin el manager."""
    return [(tid, info) for tid, info in team.items() if tid != MANAGER_CHAT_ID]

# ── TAREAS RECURRENTES — PERSISTENCIA ─────────────────────────────────────────

def load_recurring() -> list:
    if not RECURRING_FILE.exists():
        return []
    try:
        return json.loads(RECURRING_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_recurring(data: list):
    RECURRING_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

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
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{ASANA_BASE}{path}", headers=headers, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

async def asana_post(path: str, data: dict) -> dict:
    headers = {"Authorization": f"Bearer {ASANA_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{ASANA_BASE}{path}", headers=headers, json={"data": data}, timeout=15)
        r.raise_for_status()
        return r.json()

async def asana_put(path: str, data: dict) -> dict:
    headers = {"Authorization": f"Bearer {ASANA_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as client:
        r = await client.put(f"{ASANA_BASE}{path}", headers=headers, json={"data": data}, timeout=15)
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

async def create_asana_task(name: str, assignee_gid: str, due_on: str = None,
                             recurrence: str = None) -> dict:
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

def main_menu_keyboard(is_manager: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📋 Ver mis tareas",      callback_data="ver_tareas")],
        [InlineKeyboardButton("✅ Completar una tarea", callback_data="completar_menu")],
        [InlineKeyboardButton("✅✅ Completar todas",   callback_data="completar_todas_confirm")],
        [InlineKeyboardButton("📝 Subir minuta",        callback_data="minuta_start")],
    ]
    if is_manager:
        buttons.append([InlineKeyboardButton("➕ Crear tarea",           callback_data="crear_tarea_start")])
        buttons.append([InlineKeyboardButton("🔁 Tareas recurrentes",    callback_data="recurrentes_menu")])
        buttons.append([InlineKeyboardButton("📊 Reporte del equipo",    callback_data="reporte")])
        buttons.append([InlineKeyboardButton("👥 Ver equipo",            callback_data="equipo")])
    return InlineKeyboardMarkup(buttons)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None):
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

    name = get_first_name(team[tg_id]["name"]) if tg_id in team else "Marco"
    greeting = text or f"¡Hola {name}! ¿Qué quieres hacer?"
    keyboard  = main_menu_keyboard(is_manager)

    if update.callback_query:
        await update.callback_query.edit_message_text(greeting, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(greeting, reply_markup=keyboard, parse_mode="Markdown")

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
    team    = load_team()
    members = get_members(team)
    task_name = context.user_data["new_task"]["name"]

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
    buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data="menu")])

    msg = f"➕ *Nueva tarea:*\n📌 _{task_name}_\n\n👤 ¿A quién se la asignas?"
    keyboard = InlineKeyboardMarkup(buttons)

    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    elif update.message:
        await update.message.reply_text(msg, reply_markup=keyboard, parse_mode="Markdown")
    return TASK_ASSIGNEE

async def handle_task_name_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe el nombre de la tarea cuando se escribe en chat."""
    if not context.user_data.get("awaiting_task_name"):
        return ConversationHandler.END
    task_name = update.message.text.strip()
    context.user_data["new_task"] = {"name": task_name}
    context.user_data.pop("awaiting_task_name", None)
    return await ask_assignee(update, context)

async def handle_assignee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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

    msg = (
        f"✅ *Confirmando tarea:*\n\n"
        f"📌 *{task['name']}*\n"
        f"👤 {task['assignee_name']}\n"
        f"📅 Vence: {due_str}\n"
        f"🔁 {freq_str}\n\n"
        f"¿Crear esta tarea?"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Sí, crear", callback_data="task_confirm_yes")],
        [InlineKeyboardButton("❌ Cancelar",  callback_data="menu")],
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
        )
        task_gid = created.get("gid", "")
    except Exception as e:
        logger.error(f"Error creando tarea en Asana: {e}")
        await query.edit_message_text("❌ Error al crear la tarea en Asana. Intenta de nuevo.")
        return ConversationHandler.END

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

    try:
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
    await query.edit_message_text(
        f"🎉 *¡Tarea creada exitosamente!*\n\n"
        f"📌 *{task['name']}*\n"
        f"👤 {task['assignee_name']}\n"
        f"📅 {due_str}  |  🔁 {freq_str_short}\n\n"
        f"_{first_name} ya fue notificado/a._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Crear otra tarea", callback_data="crear_tarea_start"),
            InlineKeyboardButton("⬅️ Menú",             callback_data="menu"),
        ]])
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

        # Verificar si la tarea anterior está completada
        pending_count = r.get("pending_count", 0)
        if pending_count >= 2:
            # Escalar al manager
            await context.bot.send_message(
                chat_id=MANAGER_CHAT_ID,
                text=(
                    f"🚨 *Tarea recurrente bloqueada*\n\n"
                    f"📌 *{r['task_name']}*\n"
                    f"👤 {r['assignee_name']}\n"
                    f"🔁 {freq_label(r)}\n\n"
                    f"⚠️ *{pending_count} ocurrencias sin completar.*\n"
                    f"No se creará una nueva hasta que se pongan al día."
                ),
                parse_mode="Markdown"
            )
            continue

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
    emoji    = "🌅" if session == "mañana" else "🌆"
    overdue  = [t for t in tasks if is_overdue(t)]
    msg      = f"{emoji} *Hola {get_first_name(name)}, recordatorio de {session}*\n\n"
    msg     += f"Tienes *{len(tasks)}* tarea(s) pendiente(s):\n\n"
    for i, t in enumerate(tasks, 1):
        due  = f" — _{t['due_on']}_" if t.get("due_on") else ""
        warn = " ⚠️" if is_overdue(t) else ""
        msg += f"{i}. *{t['name']}*{due}{warn}\n"
    if overdue:
        msg += f"\n⚠️ *{len(overdue)} tarea(s) vencida(s)*"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Completar una tarea",  callback_data="completar_menu")],
        [InlineKeyboardButton("✅✅ Completar todas",    callback_data="completar_todas_confirm")],
        [InlineKeyboardButton("📋 Ver mis tareas",       callback_data="ver_tareas")],
    ])
    try:
        await bot.send_message(chat_id=tg_id, text=msg, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error enviando a {tg_id}: {e}")

async def job_morning(context: ContextTypes.DEFAULT_TYPE):
    team = load_team()
    for tg_id, info in team.items():
        if tg_id == MANAGER_CHAT_ID:
            continue
        tasks = await get_pending_tasks(info["asana_gid"])
        await send_reminder(context.bot, tg_id, info["name"], tasks, "mañana")

async def job_afternoon(context: ContextTypes.DEFAULT_TYPE):
    team = load_team()
    for tg_id, info in team.items():
        if tg_id == MANAGER_CHAT_ID:
            continue
        tasks = await get_pending_tasks(info["asana_gid"])
        await send_reminder(context.bot, tg_id, info["name"], tasks, "tarde")

async def job_daily_report(context: ContextTypes.DEFAULT_TYPE):
    await _send_report(context.bot)

# ── DETECCIÓN DE TAREAS NUEVAS ─────────────────────────────────────────────────

async def job_check_new_tasks(context: ContextTypes.DEFAULT_TYPE):
    team = load_team()
    for tg_id, info in team.items():
        if tg_id == MANAGER_CHAT_ID:
            continue
        asana_gid = info["asana_gid"]
        try:
            current_tasks = await get_pending_tasks(asana_gid)
            current_gids  = {t["gid"] for t in current_tasks}
            if asana_gid not in known_tasks:
                known_tasks[asana_gid] = current_gids
                continue
            new_tasks = [t for t in current_tasks if t["gid"] not in known_tasks[asana_gid]]
            for task in new_tasks:
                due = f"\n📅 Vence: *{task['due_on']}*" if task.get("due_on") else ""
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")
                ]])
                await context.bot.send_message(
                    chat_id     = tg_id,
                    text        = f"🔔 *¡Nueva tarea, {get_first_name(info['name'])}!*\n\n📌 *{task['name']}*{due}",
                    reply_markup= keyboard,
                    parse_mode  = "Markdown")
            known_tasks[asana_gid] = current_gids
        except Exception as e:
            logger.error(f"Error revisando {info['name']}: {e}")

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
# MINUTAS DE REUNIÓN v4.2
# ══════════════════════════════════════════════════════════════════════════════

# ── HELPERS INTERNOS ──────────────────────────────────────────────────────────

async def _minuta_show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el resumen de tareas extraídas y los botones de acción."""
    tasks   = context.user_data.get("minuta_tasks", [])
    preview = format_tasks_preview(tasks)
    total   = len(tasks)
    need_fix = tasks_need_fixing(tasks)

    if need_fix:
        action_buttons = [
            [InlineKeyboardButton("✅ Crear todas igual",         callback_data="minuta_create")],
            [InlineKeyboardButton("✏️ Completar info faltante",  callback_data="minuta_fix")],
            [InlineKeyboardButton("❌ Cancelar",                  callback_data="minuta_cancel")],
        ]
        footer = "\n\n⚠️ _Algunas tareas tienen info incompleta._"
    else:
        action_buttons = [
            [InlineKeyboardButton("✅ Crear todas en Asana", callback_data="minuta_create")],
            [InlineKeyboardButton("❌ Cancelar",             callback_data="minuta_cancel")],
        ]
        footer = "\n\n✅ _Todas las tareas están completas._"

    msg = (
        f"📋 *Encontré {total} tarea(s) en la minuta:*\n\n"
        f"{preview}"
        f"{footer}"
    )
    keyboard = InlineKeyboardMarkup(action_buttons)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            msg, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(
            msg, reply_markup=keyboard, parse_mode="Markdown")
    return MINUTA_CONFIRM


async def _minuta_process_and_show(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_text: str,
    image_bytes: bytes | None = None,
    mime_type: str | None = None,
):
    """Llama a Gemini, enriquece las tareas y muestra el resumen."""
    team      = load_team()
    today_str = datetime.now(TZ).strftime("%Y-%m-%d (%A)")
    # Solo nombres (sin cargo) para el prompt
    team_names = [info["name"].split("(")[0].strip() for info in team.values()]

    wait_msg = await update.effective_message.reply_text(
        "⏳ Analizando la minuta con IA…", parse_mode="Markdown"
    )

    try:
        raw_tasks = await call_gemini(raw_text, image_bytes, mime_type, team_names, today_str)
    except Exception as e:
        logger.error(f"Error Gemini en minuta: {e}")
        await wait_msg.edit_text("❌ Error al procesar la minuta con IA. Intenta de nuevo.")
        return ConversationHandler.END

    await wait_msg.delete()

    if not raw_tasks:
        await update.effective_message.reply_text(
            "🤔 No encontré tareas claras en ese texto.\n"
            "Asegúrate de incluir acciones concretas, responsables y fechas.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Menú", callback_data="menu")
            ]])
        )
        return ConversationHandler.END

    tasks = enrich_tasks(raw_tasks, team)
    context.user_data["minuta_tasks"]    = tasks
    context.user_data["minuta_raw_text"] = raw_text or "[imagen/documento]"

    return await _minuta_show_confirm(update, context)


async def _show_fix_for_task(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int):
    """Muestra los controles de corrección para la tarea incompleta en posición idx."""
    tasks = context.user_data["minuta_tasks"]
    task  = tasks[idx]
    total = len(tasks)
    name  = task["task_name"]

    # ── Falta responsable ─────────────────────────────────────────────────────
    if not task.get("assignee_tg_id"):
        team    = load_team()
        members = get_members(team)
        buttons = []
        row     = []
        for tid, info in members:
            first = get_first_name(info["name"])
            row.append(InlineKeyboardButton(
                first, callback_data=f"mfix_as_{idx}_{tid}"
            ))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([
            InlineKeyboardButton("⏭️ Saltar esta tarea", callback_data=f"mfix_skip_{idx}")
        ])
        msg = (
            f"✏️ *Tarea {idx+1}/{total}:*\n"
            f"📌 _{name}_\n\n"
            f"👤 ¿A quién se la asignas?"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(
                msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        else:
            await update.effective_message.reply_text(
                msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return MINUTA_FIX_ASSIGNEE

    # ── Falta fecha ───────────────────────────────────────────────────────────
    if not task.get("due_on"):
        today    = datetime.now(TZ).date()
        tomorrow = today + timedelta(days=1)
        week_end = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
        buttons  = [
            [
                InlineKeyboardButton(
                    f"Hoy ({today.strftime('%d/%m')})", callback_data=f"mfix_due_{idx}_{today}"),
                InlineKeyboardButton(
                    f"Mañana ({tomorrow.strftime('%d/%m')})", callback_data=f"mfix_due_{idx}_{tomorrow}"),
            ],
            [
                InlineKeyboardButton(
                    f"Esta semana ({week_end.strftime('%d/%m')})", callback_data=f"mfix_due_{idx}_{week_end}"),
                InlineKeyboardButton(
                    "📅 Elegir fecha", callback_data=f"mfix_due_{idx}_custom"),
            ],
            [InlineKeyboardButton("⏭️ Saltar esta tarea", callback_data=f"mfix_skip_{idx}")],
        ]
        msg = (
            f"✏️ *Tarea {idx+1}/{total}:*\n"
            f"📌 _{name}_\n"
            f"👤 {task.get('assignee_name','—')}\n\n"
            f"📅 ¿Cuándo vence?"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(
                msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        else:
            await update.effective_message.reply_text(
                msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return MINUTA_FIX_DUE

    # Tarea completa — avanzar a la siguiente
    return await _advance_fix(update, context, idx + 1)


async def _advance_fix(update: Update, context: ContextTypes.DEFAULT_TYPE, next_idx: int):
    """Busca la próxima tarea incompleta o pasa al resumen final."""
    tasks = context.user_data["minuta_tasks"]
    idx   = next_incomplete_idx(tasks, next_idx)
    if idx is not None:
        context.user_data["minuta_fix_idx"] = idx
        return await _show_fix_for_task(update, context, idx)
    # Todas completas → mostrar confirmación final
    return await _minuta_show_confirm(update, context)


async def _create_all_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Crea todas las tareas en Asana, notifica a los responsables y guarda el historial."""
    tasks       = context.user_data.get("minuta_tasks", [])
    raw_text    = context.user_data.get("minuta_raw_text", "")
    tg_id       = update.effective_user.id
    team        = load_team()
    submitter_name = team.get(tg_id, {}).get("name", "Desconocido")

    created_count = 0
    skipped       = []

    for t in tasks:
        if not t.get("assignee_gid"):
            skipped.append(t["task_name"])
            continue
        try:
            result = await create_asana_task(
                name         = t["task_name"],
                assignee_gid = t["assignee_gid"],
                due_on       = t.get("due_on"),
            )
            gid = result.get("gid", "")
            t["created_gid"] = gid

            # Registrar para recordatorios de escalación
            today_str = datetime.now(TZ).strftime("%Y-%m-%d")
            if t.get("due_on") and gid:
                register_unique_task(gid, today_str, t["due_on"])

            created_count += 1

            # Notificar al responsable
            assignee_tg = t.get("assignee_tg_id")
            if assignee_tg:
                first = get_first_name(t["assignee_name"])
                due_s = due_label(t.get("due_on"))
                try:
                    await context.bot.send_message(
                        chat_id = assignee_tg,
                        text    = (
                            f"🔔 *¡Nueva tarea, {first}!*\n\n"
                            f"📌 *{t['task_name']}*\n"
                            f"📅 Vence: {due_s}\n"
                            f"📝 _Asignada desde minuta de reunión_"
                        ),
                        reply_markup = InlineKeyboardMarkup([[
                            InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")
                        ]]),
                        parse_mode = "Markdown",
                    )
                except Exception as e:
                    logger.error(f"Error notificando a {assignee_tg}: {e}")

        except Exception as e:
            logger.error(f"Error creando tarea '{t['task_name']}' en Asana: {e}")
            skipped.append(t["task_name"])

    # Guardar en historial
    record = build_minuta_record(tg_id, submitter_name, raw_text, tasks, TZ)
    save_minuta(record)

    # Resumen final
    msg = f"🎉 *¡Minuta procesada!*\n\n✅ {created_count} tarea(s) creadas en Asana."
    if skipped:
        msg += f"\n⏭️ {len(skipped)} omitida(s) (sin responsable):\n"
        msg += "\n".join(f"• _{t}_" for t in skipped)

    context.user_data.pop("minuta_tasks",    None)
    context.user_data.pop("minuta_raw_text", None)
    context.user_data.pop("minuta_fix_idx",  None)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Menú", callback_data="menu")
    ]])
    if update.callback_query:
        await update.callback_query.edit_message_text(
            msg, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(
            msg, reply_markup=keyboard, parse_mode="Markdown")
    return ConversationHandler.END


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

async def cmd_minuta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /minuta — pide al usuario que envíe el contenido de la minuta."""
    msg = (
        "📝 *Subir minuta de reunión*\n\n"
        "Envía la minuta en cualquiera de estos formatos:\n"
        "• ✍️ Texto directo en este chat\n"
        "• 🖼️ Foto de la pizarra o pizarrón\n"
        "• 📄 Archivo PDF\n\n"
        "_Gemini extraerá las tareas, responsables y fechas automáticamente._"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")
    return MINUTA_WAITING_CONTENT


# ── RECEPCIÓN DE CONTENIDO ────────────────────────────────────────────────────

async def handle_minuta_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe la minuta como texto plano (o mensaje reenviado)."""
    text = update.message.text or (
        update.message.forward_origin and update.message.text
    )
    if not text or len(text.strip()) < 20:
        await update.message.reply_text(
            "⚠️ El texto es muy corto. Pega el contenido completo de la minuta.")
        return MINUTA_WAITING_CONTENT
    return await _minuta_process_and_show(update, context, raw_text=text.strip())


async def handle_minuta_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe la minuta como imagen (foto de pizarra, documento escaneado, etc.)."""
    photo = update.message.photo[-1]          # tamaño más grande
    tg_file = await context.bot.get_file(photo.file_id)
    byte_arr = await tg_file.download_as_bytearray()
    return await _minuta_process_and_show(
        update, context,
        raw_text=None,
        image_bytes=bytes(byte_arr),
        mime_type="image/jpeg",
    )


async def handle_minuta_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe la minuta como documento (PDF, imagen enviada como archivo)."""
    doc = update.message.document
    SUPPORTED = ("application/pdf", "image/jpeg", "image/png", "image/webp")
    if doc.mime_type not in SUPPORTED:
        await update.message.reply_text(
            f"⚠️ Formato no soportado: `{doc.mime_type}`\n"
            "Por favor envía la minuta como texto, foto o PDF.",
            parse_mode="Markdown",
        )
        return MINUTA_WAITING_CONTENT

    tg_file  = await context.bot.get_file(doc.file_id)
    byte_arr = await tg_file.download_as_bytearray()
    return await _minuta_process_and_show(
        update, context,
        raw_text=None,
        image_bytes=bytes(byte_arr),
        mime_type=doc.mime_type,
    )


# ── CONFIRMACIÓN ──────────────────────────────────────────────────────────────

async def handle_minuta_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Crea todas las tareas en Asana sin corrección previa."""
    await update.callback_query.answer()
    return await _create_all_tasks(update, context)


async def handle_minuta_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancela el flujo de minuta y regresa al menú."""
    await update.callback_query.answer()
    context.user_data.pop("minuta_tasks",    None)
    context.user_data.pop("minuta_raw_text", None)
    context.user_data.pop("minuta_fix_idx",  None)
    await show_main_menu(update, context, "❌ Minuta cancelada.")
    return ConversationHandler.END


async def handle_minuta_fix_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia el flujo de corrección de tareas incompletas."""
    await update.callback_query.answer()
    tasks = context.user_data.get("minuta_tasks", [])
    idx   = next_incomplete_idx(tasks, 0)
    if idx is None:
        return await _create_all_tasks(update, context)
    context.user_data["minuta_fix_idx"] = idx
    return await _show_fix_for_task(update, context, idx)


# ── CORRECCIÓN: RESPONSABLE ───────────────────────────────────────────────────

async def handle_minuta_fix_assignee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Asigna un responsable a la tarea en corrección (mfix_as_{idx}_{tg_id})."""
    query = update.callback_query
    await query.answer()
    parts   = query.data.split("_")   # ['mfix', 'as', idx, tg_id]
    task_idx = int(parts[2])
    tg_id    = int(parts[3])

    team = load_team()
    if tg_id in team:
        info = team[tg_id]
        tasks = context.user_data["minuta_tasks"]
        tasks[task_idx]["assignee_tg_id"] = tg_id
        tasks[task_idx]["assignee_gid"]   = info["asana_gid"]
        tasks[task_idx]["assignee_name"]  = info["name"]

    return await _advance_fix(update, context, task_idx)


async def handle_minuta_fix_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Salta la tarea incompleta sin asignar responsable ni fecha."""
    query = update.callback_query
    await query.answer()
    parts    = query.data.split("_")   # ['mfix', 'skip', idx]
    task_idx = int(parts[2])
    return await _advance_fix(update, context, task_idx + 1)


# ── CORRECCIÓN: FECHA ─────────────────────────────────────────────────────────

async def handle_minuta_fix_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Asigna fecha a la tarea en corrección.
    callback_data: mfix_due_{idx}_{date|custom}
    """
    query = update.callback_query
    await query.answer()
    # data = "mfix_due_0_2026-04-30" ó "mfix_due_0_custom"
    parts    = query.data.split("_", 3)   # ['mfix', 'due', idx, date_or_custom]
    task_idx = int(parts[2])
    value    = parts[3]

    if value == "custom":
        context.user_data["minuta_fix_idx"]         = task_idx
        context.user_data["minuta_awaiting_date"]   = True
        await query.edit_message_text(
            "📅 Escribe la fecha en formato *DD/MM/AAAA*\nEj: `30/04/2026`",
            parse_mode="Markdown",
        )
        return MINUTA_FIX_DUE

    # Valor directo de fecha
    tasks = context.user_data["minuta_tasks"]
    tasks[task_idx]["due_on"] = value
    context.user_data.pop("minuta_awaiting_date", None)
    return await _advance_fix(update, context, task_idx)


async def handle_minuta_fix_due_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recibe fecha manual cuando el usuario escribe DD/MM/AAAA."""
    if not context.user_data.get("minuta_awaiting_date"):
        return MINUTA_FIX_DUE

    text = update.message.text.strip()
    try:
        d = datetime.strptime(text, "%d/%m/%Y").date()
        task_idx = context.user_data["minuta_fix_idx"]
        context.user_data["minuta_tasks"][task_idx]["due_on"] = d.strftime("%Y-%m-%d")
        context.user_data.pop("minuta_awaiting_date", None)
        return await _advance_fix(update, context, task_idx)
    except ValueError:
        await update.message.reply_text(
            "❌ Formato inválido. Escribe la fecha así: `30/04/2026`",
            parse_mode="Markdown",
        )
        return MINUTA_FIX_DUE


# ── HISTORIAL DE MINUTAS ──────────────────────────────────────────────────────

async def cmd_historial_minutas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /minutas — muestra las últimas minutas procesadas.
    Solo el manager puede ver el historial completo;
    los demás solo ven las minutas donde participaron.
    """
    tg_id = update.effective_user.id
    data  = load_minutas()

    if not data:
        await update.message.reply_text(
            "📂 Aún no hay minutas guardadas.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Menú", callback_data="menu")
            ]]),
        )
        return

    is_manager = (tg_id == MANAGER_CHAT_ID)
    if is_manager:
        visible = data[-10:]           # últimas 10
    else:
        # Solo minutas donde el usuario tiene tareas asignadas
        visible = [
            m for m in data[-20:]
            if any(t.get("assignee_name") and
                   load_team().get(tg_id, {}).get("name", "") in (t.get("assignee_name") or "")
                   for t in m.get("tasks_created", []))
        ][-5:]

    if not visible:
        await update.message.reply_text("📂 No tienes minutas recientes.")
        return

    msg = "📋 *Historial de minutas:*\n\n"
    for m in reversed(visible):
        n_tasks = len(m.get("tasks_created", []))
        who     = m.get("submitted_by_name", "—").split("(")[0].strip()
        msg += (
            f"📅 *{m['date']} {m.get('time','')}*  —  {who}\n"
            f"   {n_tasks} tarea(s) creada(s)\n\n"
        )

    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Menú", callback_data="menu")
        ]]),
    )


# ══════════════════════════════════════════════════════════════════════════════
# ESCALACIÓN AUTOMÁTICA v4.0
# ══════════════════════════════════════════════════════════════════════════════


from escalation import (
    should_remind_before_due, should_escalate_overdue,
    mark_alert_sent, is_task_blocked, block_task, cleanup_alert_state,
    get_freq_for_task, days_until_due, hours_since_due,
    register_unique_task, DAYS_LABEL,
)

async def job_escalation(context: ContextTypes.DEFAULT_TYPE, session: str = "pm"):
    """
    Corre 2 veces al día (mañana y tarde).
    - Envía recordatorios anticipados al responsable
    - Escala tareas vencidas al manager
    session: 'am' | 'pm'
    """
    team     = load_team()
    rec_data = load_recurring()
    all_gids = set()

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

            # ── Recordatorios anticipados al responsable ────────────────────
            pre_alerts = should_remind_before_due(gid, due_on, freq, TZ)
            for alert_key in pre_alerts:
                days = days_until_due(due_on, TZ)
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
                    mark_alert_sent(gid, alert_key)
                    logger.info(f"Alerta anticipada '{alert_key}' → {info['name']}: {task['name']}")
                except Exception as e:
                    logger.error(f"Error alerta anticipada: {e}")

            # ── Escalación por tarea vencida al manager ────────────────────
            if is_task_blocked(gid):
                continue

            esc_key, should_block = should_escalate_overdue(gid, due_on, session, TZ)
            if not esc_key:
                continue

            hours = hours_since_due(due_on, TZ) or 0

            # Nivel de urgencia
            if hours < 24:
                icon = "⚠️"
                level = "Tarea vencida"
            elif hours < 48:
                icon = "🚨"
                level = "Vencida +24h — sin completar"
            elif hours < 72:
                icon = "🔴"
                level = "URGENTE — Vencida +48h"
            else:
                icon = "⛔"
                level = "CRÍTICO — Bloqueada +72h"

            hours_int = int(hours)
            time_str  = f"{hours_int}h" if hours_int > 0 else "recién vencida"

            esc_msg = (
                f"{icon} *{level}*\n\n"
                f"📌 *{task['name']}*\n"
                f"👤 {info['name']}\n"
                f"📅 Venció: {due_on} (hace {time_str})\n"
            )
            if should_block:
                esc_msg += "\n⛔ *Tarea bloqueada.* No se crearán nuevas recurrencias hasta que se complete."

            keyboard_mgr = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"📋 Ver tareas de {get_first_name(info['name'])}",
                                     callback_data="reporte")
            ]])
            try:
                await context.bot.send_message(
                    chat_id=MANAGER_CHAT_ID, text=esc_msg,
                    reply_markup=keyboard_mgr, parse_mode="Markdown")
                mark_alert_sent(gid, esc_key)
                if should_block:
                    block_task(gid)
                logger.info(f"Escalación '{esc_key}' → manager: {info['name']} / {task['name']}")
            except Exception as e:
                logger.error(f"Error escalación: {e}")

    cleanup_alert_state(all_gids)

async def job_escalation_am(context: ContextTypes.DEFAULT_TYPE):
    await job_escalation(context, session="am")

async def job_escalation_pm(context: ContextTypes.DEFAULT_TYPE):
    await job_escalation(context, session="pm")

async def job_friday_summary(context: ContextTypes.DEFAULT_TYPE):
    """Viernes tarde — resumen de pendientes para la semana siguiente."""
    now = datetime.now(TZ)
    if now.weekday() != 4:  # Solo viernes
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
                # Enviar al responsable
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

                # Agregar al resumen del manager
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
    if now.weekday() != 6:  # Solo domingo
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

# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # ConversationHandler para crear tareas
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("tarea", crear_tarea_start),
            CallbackQueryHandler(crear_tarea_start, pattern="^crear_tarea_start$"),
        ],
        states={
            TASK_ASSIGNEE: [
                CallbackQueryHandler(handle_assignee,    pattern="^assign_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_name_text),
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
            CallbackQueryHandler(show_main_menu, pattern="^menu$"),
            CommandHandler("menu", cmd_menu),
        ],
        per_message=False,
    )

    app.add_handler(conv_handler)

    # ── ConversationHandler: Minutas ──────────────────────────────────────────
    minuta_handler = ConversationHandler(
        entry_points=[
            CommandHandler("minuta", cmd_minuta),
            CallbackQueryHandler(cmd_minuta, pattern="^minuta_start$"),
        ],
        states={
            MINUTA_WAITING_CONTENT: [
                MessageHandler(filters.PHOTO,                      handle_minuta_photo),
                MessageHandler(filters.Document.ALL,               handle_minuta_document),
                MessageHandler(filters.TEXT & ~filters.COMMAND,    handle_minuta_text),
            ],
            MINUTA_CONFIRM: [
                CallbackQueryHandler(handle_minuta_create,    pattern="^minuta_create$"),
                CallbackQueryHandler(handle_minuta_fix_start, pattern="^minuta_fix$"),
                CallbackQueryHandler(handle_minuta_cancel,    pattern="^minuta_cancel$"),
            ],
            MINUTA_FIX_ASSIGNEE: [
                CallbackQueryHandler(handle_minuta_fix_assignee, pattern="^mfix_as_"),
                CallbackQueryHandler(handle_minuta_fix_skip,     pattern="^mfix_skip_"),
            ],
            MINUTA_FIX_DUE: [
                CallbackQueryHandler(handle_minuta_fix_due,     pattern="^mfix_due_"),
                CallbackQueryHandler(handle_minuta_fix_skip,    pattern="^mfix_skip_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_minuta_fix_due_text),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(handle_minuta_cancel, pattern="^minuta_cancel$"),
            CallbackQueryHandler(show_main_menu,       pattern="^menu$"),
            CommandHandler("menu", cmd_menu),
        ],
        per_message=False,
    )
    app.add_handler(minuta_handler)

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("menu",    cmd_menu))
    app.add_handler(CommandHandler("mi_id",   cmd_mi_id))
    app.add_handler(CommandHandler("minutas", cmd_historial_minutas))
    app.add_handler(CallbackQueryHandler(button_handler))

    jq = app.job_queue
    jq.run_daily(job_morning,        time(MORNING_HOUR,   MORNING_MIN,   tzinfo=TZ))
    jq.run_daily(job_afternoon,      time(AFTERNOON_HOUR, AFTERNOON_MIN, tzinfo=TZ))
    jq.run_daily(job_daily_report,   time(REPORT_HOUR,    REPORT_MIN,    tzinfo=TZ))

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

    logger.info(
        f"✅ Bot Lubrikca v4.2 listo | "
        f"Recordatorios: {MORNING_HOUR}:00 y {AFTERNOON_HOUR}:00 | "
        f"Reporte: {REPORT_HOUR}:00 | Escalación + Minutas activas"
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

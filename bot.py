"""
Bot de Telegram para seguimiento de tareas de Asana — Lubrikca
Versión 2.0 — Interfaz con botones interactivos
"""

import os
import logging
from datetime import datetime, time
from pathlib import Path
import pytz
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── CONFIGURACIÓN ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ASANA_TOKEN      = os.environ["ASANA_TOKEN"]
ASANA_WORKSPACE  = os.environ["ASANA_WORKSPACE_ID"]
MANAGER_CHAT_ID  = int(os.environ["MANAGER_CHAT_ID"])
TIMEZONE         = os.environ.get("TIMEZONE", "America/Caracas")

MORNING_HOUR   = int(os.environ.get("MORNING_HOUR",   "9"))
MORNING_MIN    = int(os.environ.get("MORNING_MIN",    "0"))
AFTERNOON_HOUR = int(os.environ.get("AFTERNOON_HOUR", "15"))
AFTERNOON_MIN  = int(os.environ.get("AFTERNOON_MIN",  "0"))
REPORT_HOUR    = int(os.environ.get("REPORT_HOUR",    "18"))
REPORT_MIN     = int(os.environ.get("REPORT_MIN",     "0"))
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "5"))

ASANA_BASE = "https://app.asana.com/api/1.0"
TZ = pytz.timezone(TIMEZONE)

# Memoria de tareas conocidas {asana_gid: set(task_gids)}
known_tasks: dict[str, set] = {}

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

# ── ASANA API ──────────────────────────────────────────────────────────────────

async def asana_get(path: str, params: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {ASANA_TOKEN}"}
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{ASANA_BASE}{path}", headers=headers, params=params, timeout=15)
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

# ── MENÚ PRINCIPAL ─────────────────────────────────────────────────────────────

def main_menu_keyboard(is_manager: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")],
        [InlineKeyboardButton("✅ Completar una tarea", callback_data="completar_menu")],
        [InlineKeyboardButton("✅✅ Completar todas", callback_data="completar_todas_confirm")],
    ]
    if is_manager:
        buttons.append([InlineKeyboardButton("📊 Ver reporte del equipo", callback_data="reporte")])
        buttons.append([InlineKeyboardButton("👥 Ver equipo", callback_data="equipo")])
    return InlineKeyboardMarkup(buttons)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None):
    tg_id = update.effective_user.id
    team = load_team()
    is_manager = (tg_id == MANAGER_CHAT_ID)

    if tg_id not in team:
        msg = f"👋 Hola! Aún no estás registrado.\nDile a Marco tu ID: `{tg_id}`"
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, parse_mode="Markdown")
        else:
            await update.message.reply_text(msg, parse_mode="Markdown")
        return

    name = get_first_name(team[tg_id]["name"])
    greeting = text or f"¡Hola {name}! ¿Qué quieres hacer?"
    keyboard = main_menu_keyboard(is_manager)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            greeting, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await update.message.reply_text(
            greeting, reply_markup=keyboard, parse_mode="Markdown")

# ── HANDLERS DE BOTONES ────────────────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    tg_id = update.effective_user.id
    team = load_team()

    # ── VER TAREAS ─────────────────────────────────────────────────────────────
    if data == "ver_tareas":
        if tg_id not in team:
            await query.edit_message_text("❌ No estás registrado.")
            return
        tasks = await get_pending_tasks(team[tg_id]["asana_gid"])
        if not tasks:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Menú principal", callback_data="menu")
            ]])
            await query.edit_message_text(
                "✅ ¡No tienes tareas pendientes! Estás al día 🎉",
                reply_markup=keyboard)
            return

        msg = f"📋 *Tus tareas pendientes ({len(tasks)}):*\n\n"
        for i, t in enumerate(tasks, 1):
            due = f" — _{t['due_on']}_" if t.get("due_on") else ""
            warn = " ⚠️" if is_overdue(t) else ""
            msg += f"{i}. *{t['name']}*{due}{warn}\n"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Completar una tarea", callback_data="completar_menu")],
            [InlineKeyboardButton("✅✅ Completar todas", callback_data="completar_todas_confirm")],
            [InlineKeyboardButton("⬅️ Menú principal", callback_data="menu")],
        ])
        await query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")

    # ── COMPLETAR — MOSTRAR LISTA DE BOTONES ───────────────────────────────────
    elif data == "completar_menu":
        if tg_id not in team:
            await query.edit_message_text("❌ No estás registrado.")
            return
        tasks = await get_pending_tasks(team[tg_id]["asana_gid"])
        if not tasks:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Menú principal", callback_data="menu")
            ]])
            await query.edit_message_text(
                "✅ ¡No tienes tareas pendientes!", reply_markup=keyboard)
            return

        msg = "✅ *¿Cuál tarea completaste?*\n\nToca la tarea para marcarla:"
        buttons = []
        for t in tasks:
            warn = "⚠️ " if is_overdue(t) else ""
            label = f"{warn}{t['name']}"
            if len(label) > 60:
                label = label[:57] + "..."
            buttons.append([InlineKeyboardButton(label, callback_data=f"done_{t['gid']}")])
        buttons.append([InlineKeyboardButton("⬅️ Volver", callback_data="ver_tareas")])
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

    # ── COMPLETAR TAREA INDIVIDUAL ─────────────────────────────────────────────
    elif data.startswith("done_"):
        task_gid = data[5:]
        if tg_id not in team:
            await query.edit_message_text("❌ No estás registrado.")
            return

        # Obtener nombre de la tarea antes de completarla
        tasks = await get_pending_tasks(team[tg_id]["asana_gid"])
        task_name = next((t["name"] for t in tasks if t["gid"] == task_gid), "Tarea")

        success = await complete_task(task_gid)
        if success:
            # Actualizar memoria
            asana_gid = team[tg_id]["asana_gid"]
            if asana_gid in known_tasks:
                known_tasks[asana_gid].discard(task_gid)

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Completar otra", callback_data="completar_menu")],
                [InlineKeyboardButton("⬅️ Menú principal", callback_data="menu")],
            ])
            await query.edit_message_text(
                f"🎉 ¡Perfecto! Marcado en Asana:\n✅ *{task_name}*",
                reply_markup=keyboard, parse_mode="Markdown")

            # Notificar al manager
            try:
                await context.bot.send_message(
                    chat_id=MANAGER_CHAT_ID,
                    text=f"✅ *{team[tg_id]['name']}* completó:\n_{task_name}_",
                    parse_mode="Markdown")
            except Exception:
                pass
        else:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Menú principal", callback_data="menu")
            ]])
            await query.edit_message_text(
                "❌ Error al actualizar Asana. Intenta de nuevo.",
                reply_markup=keyboard)

    # ── CONFIRMAR COMPLETAR TODAS ──────────────────────────────────────────────
    elif data == "completar_todas_confirm":
        if tg_id not in team:
            await query.edit_message_text("❌ No estás registrado.")
            return
        tasks = await get_pending_tasks(team[tg_id]["asana_gid"])
        if not tasks:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Menú principal", callback_data="menu")
            ]])
            await query.edit_message_text("✅ ¡Ya no tienes tareas pendientes!", reply_markup=keyboard)
            return

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Sí, completar las {len(tasks)} tareas", callback_data="completar_todas")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="ver_tareas")],
        ])
        await query.edit_message_text(
            f"¿Confirmas que completaste *todas* tus {len(tasks)} tareas pendientes?",
            reply_markup=keyboard, parse_mode="Markdown")

    # ── COMPLETAR TODAS ────────────────────────────────────────────────────────
    elif data == "completar_todas":
        if tg_id not in team:
            await query.edit_message_text("❌ No estás registrado.")
            return
        tasks = await get_pending_tasks(team[tg_id]["asana_gid"])
        results = []
        for t in tasks:
            if await complete_task(t["gid"]):
                results.append(t["name"])

        asana_gid = team[tg_id]["asana_gid"]
        if asana_gid in known_tasks:
            known_tasks[asana_gid] = set()

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Menú principal", callback_data="menu")
        ]])
        await query.edit_message_text(
            f"🎉 ¡Excelente! *{len(results)}/{len(tasks)}* tareas completadas en Asana.",
            reply_markup=keyboard, parse_mode="Markdown")

        if results:
            try:
                names_list = "\n".join(f"✅ _{n}_" for n in results)
                await context.bot.send_message(
                    chat_id=MANAGER_CHAT_ID,
                    text=f"🎉 *{team[tg_id]['name']}* completó todas sus tareas:\n{names_list}",
                    parse_mode="Markdown")
            except Exception:
                pass

    # ── REPORTE (solo manager) ─────────────────────────────────────────────────
    elif data == "reporte":
        if tg_id != MANAGER_CHAT_ID:
            await query.edit_message_text("❌ Solo el manager puede ver el reporte.")
            return
        await query.edit_message_text("⏳ Generando reporte...")
        await _send_report(context.bot)
        await show_main_menu(update, context, "📊 Reporte enviado.")

    # ── EQUIPO (solo manager) ──────────────────────────────────────────────────
    elif data == "equipo":
        if tg_id != MANAGER_CHAT_ID:
            await query.edit_message_text("❌ Solo el manager puede ver esto.")
            return
        team = load_team()
        members = [(tid, info) for tid, info in team.items() if tid != MANAGER_CHAT_ID]
        msg = f"👥 *Equipo registrado ({len(members)} personas):*\n\n"
        for tid, info in members:
            msg += f"• *{info['name']}*\n"
        msg += "\n_Para agregar alguien, edita team.txt en GitHub._"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Menú principal", callback_data="menu")
        ]])
        await query.edit_message_text(msg, reply_markup=keyboard, parse_mode="Markdown")

    # ── MENÚ PRINCIPAL ─────────────────────────────────────────────────────────
    elif data == "menu":
        await show_main_menu(update, context)

# ── COMANDOS ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)

async def cmd_mi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 Tu ID de Telegram es:\n`{update.effective_user.id}`\n\nPásaselo a Marco para registrarte.",
        parse_mode="Markdown")

# ── REPORTE INTERNO ────────────────────────────────────────────────────────────

async def _send_report(bot):
    team = load_team()
    today = datetime.now(TZ).strftime("%d/%m/%Y")
    all_tasks = {}
    for tg_id, info in team.items():
        if tg_id == MANAGER_CHAT_ID:
            continue
        try:
            all_tasks[tg_id] = await get_pending_tasks(info["asana_gid"])
        except Exception:
            all_tasks[tg_id] = []

    total = sum(len(t) for t in all_tasks.values())
    total_od = sum(1 for tasks in all_tasks.values() for t in tasks if is_overdue(t))

    msg = f"📊 *Reporte del equipo — {today}*\n\n"
    for tg_id, tasks in all_tasks.items():
        info = team[tg_id]
        od = sum(1 for t in tasks if is_overdue(t))
        status = "🟢" if not tasks else ("🔴" if od > 0 else "🟡")
        msg += f"{status} *{info['name']}*"
        if not tasks:
            msg += " — sin pendientes\n\n"
        else:
            msg += f" — {len(tasks)} pendiente(s){f', {od} vencida(s)' if od else ''}\n"
            for t in tasks:
                due = f" _{t['due_on']}_" if t.get("due_on") else ""
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
    emoji = "🌅" if session == "mañana" else "🌆"
    overdue = [t for t in tasks if is_overdue(t)]
    first_name = get_first_name(name)

    msg  = f"{emoji} *Hola {first_name}, recordatorio de {session}*\n\n"
    msg += f"Tienes *{len(tasks)}* tarea(s) pendiente(s):\n\n"
    for i, t in enumerate(tasks, 1):
        due = f" — _{t['due_on']}_" if t.get("due_on") else ""
        warn = " ⚠️" if is_overdue(t) else ""
        msg += f"{i}. *{t['name']}*{due}{warn}\n"
    if overdue:
        msg += f"\n⚠️ *{len(overdue)} tarea(s) vencida(s)*"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Completar una tarea", callback_data="completar_menu")],
        [InlineKeyboardButton("✅✅ Completar todas", callback_data="completar_todas_confirm")],
        [InlineKeyboardButton("📋 Ver mis tareas", callback_data="ver_tareas")],
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
            current_gids = {t["gid"] for t in current_tasks}

            if asana_gid not in known_tasks:
                known_tasks[asana_gid] = current_gids
                continue

            new_gids = current_gids - known_tasks[asana_gid]
            new_tasks = [t for t in current_tasks if t["gid"] in new_gids]

            for task in new_tasks:
                due = f"\n📅 Vence: *{task['due_on']}*" if task.get("due_on") else ""
                first_name = get_first_name(info["name"])
                msg = (
                    f"🔔 *¡Nueva tarea asignada, {first_name}!*\n\n"
                    f"📌 *{task['name']}*{due}"
                )
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 Ver todas mis tareas", callback_data="ver_tareas")],
                ])
                try:
                    await context.bot.send_message(
                        chat_id=tg_id, text=msg, reply_markup=keyboard, parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Error notificando a {tg_id}: {e}")

            known_tasks[asana_gid] = current_gids
        except Exception as e:
            logger.error(f"Error revisando tareas de {info['name']}: {e}")

# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(CommandHandler("mi_id", cmd_mi_id))
    app.add_handler(CallbackQueryHandler(button_handler))

    jq = app.job_queue
    jq.run_daily(job_morning,      time(MORNING_HOUR,   MORNING_MIN,   tzinfo=TZ))
    jq.run_daily(job_afternoon,    time(AFTERNOON_HOUR, AFTERNOON_MIN, tzinfo=TZ))
    jq.run_daily(job_daily_report, time(REPORT_HOUR,    REPORT_MIN,    tzinfo=TZ))
    jq.run_repeating(job_check_new_tasks, interval=CHECK_INTERVAL_MINUTES * 60, first=10)

    logger.info(f"✅ Bot Lubrikca v2.0 listo | Recordatorios: {MORNING_HOUR}:00 y {AFTERNOON_HOUR}:00 | Reporte: {REPORT_HOUR}:00")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
    

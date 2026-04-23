"""
Bot de Telegram para seguimiento de tareas de Asana — Lubrikca
Versión 4.0 — Corregida (Orden de funciones)
"""

import os
import json
import logging
from datetime import datetime, time, date, timedelta
from pathlib import Path
import pytz
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)

# Importaciones de lógica de escalación
from escalation import (
    should_remind_before_due, should_escalate_overdue,
    mark_alert_sent, is_task_blocked, block_task, cleanup_alert_state,
    get_freq_for_task, days_until_due, hours_since_due,
    register_unique_task, DAYS_LABEL,
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

# Memoria de tareas conocidas
known_tasks: dict[str, set] = {}

# Estados del ConversationHandler
(
    TASK_ASSIGNEE,
    TASK_DUE,
    TASK_DUE_CUSTOM,
    TASK_RECURRING,
    TASK_FREQ,
    TASK_TIMES_PER_DAY,
    TASK_HOURS,
    TASK_WEEKDAY,
) = range(8)

# ── FUNCIONES DE APOYO Y EQUIPO ────────────────────────────────────────────────

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
    return [(tid, info) for tid, info in team.items() if tid != MANAGER_CHAT_ID]

# (Omitidas funciones load_recurring, save_recurring, asana_get/post/put para brevedad, 
#  pero deben estar aquí en tu archivo real)

# ... [INSERTAR AQUÍ TUS FUNCIONES: load_recurring, save_recurring, add_recurring, 
#      update_recurring, asana_get, asana_post, asana_put, get_pending_tasks, 
#      create_asana_task, complete_task, is_overdue, get_first_name, due_label, freq_label] ...

# ── LOGICA DE ESCALACIÓN Y RESÚMENES (MOVIDO ARRIBA) ───────────────────────────

async def job_escalation(context: ContextTypes.DEFAULT_TYPE, session: str = "pm"):
    """Revisa alertas anticipadas y escala tareas vencidas al manager."""
    team     = load_team()
    rec_data = load_recurring()
    all_gids = set()

    for tg_id, info in team.items():
        if tg_id == MANAGER_CHAT_ID: continue
        try:
            tasks = await get_pending_tasks(info["asana_gid"])
        except Exception: continue

        for task in tasks:
            gid = task["gid"]
            due_on = task.get("due_on")
            freq = get_freq_for_task(gid, rec_data)
            all_gids.add(gid)

            if not due_on: continue
            first_name = get_first_name(info["name"])

            # Alertas anticipadas
            pre_alerts = should_remind_before_due(gid, due_on, freq, TZ)
            for alert_key in pre_alerts:
                days = days_until_due(due_on, TZ)
                label = DAYS_LABEL.get(alert_key, f"{days} día(s)")
                msg = f"⏰ *Recordatorio, {first_name}*\n\n📌 *{task['name']}*\n📅 Vence en *{label}* ({due_on})"
                await context.bot.send_message(chat_id=tg_id, text=msg, parse_mode="Markdown")
                mark_alert_sent(gid, alert_key)

            # Escalación al Manager
            if is_task_blocked(gid): continue
            esc_key, should_block = should_escalate_overdue(gid, due_on, session, TZ)
            if esc_key:
                hours = hours_since_due(due_on, TZ) or 0
                icon = "🔴" if hours > 48 else "🚨"
                esc_msg = f"{icon} *Tarea Vencida*\n\n📌 *{task['name']}*\n👤 {info['name']}\n📅 Venció: {due_on}"
                if should_block: 
                    esc_msg += "\n⛔ *BLOQUEADA.*"
                    block_task(gid)
                await context.bot.send_message(chat_id=MANAGER_CHAT_ID, text=esc_msg, parse_mode="Markdown")
                mark_alert_sent(gid, esc_key)

    cleanup_alert_state(all_gids)

async def job_escalation_am(context: ContextTypes.DEFAULT_TYPE):
    await job_escalation(context, session="am")

async def job_escalation_pm(context: ContextTypes.DEFAULT_TYPE):
    await job_escalation(context, session="pm")

async def job_friday_summary(context: ContextTypes.DEFAULT_TYPE):
    """Resumen de pendientes los viernes."""
    now = datetime.now(TZ)
    if now.weekday() != 4: return
    # ... (resto de tu lógica de friday_summary) ...

async def job_sunday_summary(context: ContextTypes.DEFAULT_TYPE):
    """Recordatorio de semana los domingos."""
    now = datetime.now(TZ)
    if now.weekday() != 6: return
    # ... (resto de tu lógica de sunday_summary) ...

# ── RESTO DE HANDLERS (show_main_menu, crear_tarea_start, etc.) ───────────────
# ... [Pega aquí todos los handlers de botones y comandos que ya tenías] ...

# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # ConversationHandler (Asegúrate de incluirlo completo)
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("tarea", crear_tarea_start),
            CallbackQueryHandler(crear_tarea_start, pattern="^crear_tarea_start$"),
        ],
        states={
            TASK_ASSIGNEE: [CallbackQueryHandler(handle_assignee, pattern="^assign_"), MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_name_text)],
            TASK_DUE: [CallbackQueryHandler(handle_due, pattern="^due_")],
            TASK_DUE_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_due_custom)],
            TASK_RECURRING: [CallbackQueryHandler(handle_recurring_choice, pattern="^rec_(yes|no)$"), CallbackQueryHandler(handle_task_confirm, pattern="^task_confirm_yes$")],
            TASK_FREQ: [CallbackQueryHandler(handle_freq, pattern="^freq_")],
            TASK_TIMES_PER_DAY: [CallbackQueryHandler(handle_times_per_day, pattern="^times_")],
            TASK_HOURS: [CallbackQueryHandler(handle_hour_select, pattern="^hour_")],
            TASK_WEEKDAY: [CallbackQueryHandler(handle_weekday, pattern="^wday_")],
        },
        fallbacks=[CallbackQueryHandler(show_main_menu, pattern="^menu$"), CommandHandler("menu", cmd_menu)],
        per_message=False,
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("menu",   cmd_menu))
    app.add_handler(CommandHandler("mi_id",  cmd_mi_id))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Configuración de Jobs
    jq = app.job_queue
    jq.run_daily(job_morning,        time(MORNING_HOUR,   MORNING_MIN,   tzinfo=TZ))
    jq.run_daily(job_afternoon,      time(AFTERNOON_HOUR, AFTERNOON_MIN, tzinfo=TZ))
    jq.run_daily(job_daily_report,   time(REPORT_HOUR,    REPORT_MIN,    tzinfo=TZ))

    # Escalación y Resúmenes (Ahora definidos correctamente antes de main)
    jq.run_daily(job_escalation_am,  time(MORNING_HOUR,   MORNING_MIN,   tzinfo=TZ))
    jq.run_daily(job_escalation_pm,  time(AFTERNOON_HOUR, AFTERNOON_MIN, tzinfo=TZ))
    jq.run_daily(job_friday_summary, time(AFTERNOON_HOUR, AFTERNOON_MIN, tzinfo=TZ))
    jq.run_daily(job_sunday_summary, time(AFTERNOON_HOUR, AFTERNOON_MIN, tzinfo=TZ))

    # Jobs de fondo
    jq.run_repeating(job_check_new_tasks,           interval=CHECK_INTERVAL_MINUTES * 60, first=10)
    jq.run_repeating(job_process_recurring,         interval=60 * 30, first=30)
    jq.run_repeating(job_check_recurring_completed, interval=60 * 60, first=60)

    logger.info("✅ Bot Lubrikca v4.0 con Escalación Cargado Correctamente")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

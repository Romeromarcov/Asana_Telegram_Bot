"""
Bot de Telegram para seguimiento de tareas de Asana — Lubrikca
- Envía recordatorios 2 veces al día (mañana y tarde)
- Detecta tareas nuevas en Asana y notifica al instante
- Permite confirmar tareas completadas con /listo
- Marca tareas como completadas en Asana
- Envía reporte diario al manager
- El equipo se gestiona en team.txt (sin tocar este archivo)
"""

import os
import logging
from datetime import datetime, time
from pathlib import Path
import pytz
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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

# Cada cuántos minutos revisa si hay tareas nuevas en Asana
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "5"))

ASANA_BASE = "https://app.asana.com/api/1.0"
TZ = pytz.timezone(TIMEZONE)

# Memoria de tareas conocidas por usuario {asana_gid: set(task_gids)}
known_tasks: dict[str, set] = {}

# ── CARGA DEL EQUIPO DESDE team.txt ───────────────────────────────────────────

def load_team() -> dict:
    team = {}
    team_file = Path(__file__).parent / "team.txt"
    if not team_file.exists():
        logger.warning("team.txt no encontrado.")
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
            asana_gid = parts[1]
            name = parts[2] if len(parts) > 2 else f"Usuario {tg_id}"
            team[tg_id] = {"asana_gid": asana_gid, "name": name}
        except ValueError:
            logger.warning(f"Línea inválida en team.txt: {line}")
    logger.info(f"Equipo: {[v['name'] for v in team.values()]}")
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

def format_task_list(tasks: list) -> str:
    if not tasks:
        return "✅ ¡No tienes tareas pendientes!"
    lines = []
    for i, t in enumerate(tasks, 1):
        due = f" — vence *{t['due_on']}*" if t.get("due_on") else ""
        warn = " ⚠️" if is_overdue(t) else ""
        lines.append(f"{i}. *{t['name']}*{due}{warn}")
    return "\n".join(lines)

# ── DETECCIÓN DE TAREAS NUEVAS ─────────────────────────────────────────────────

async def job_check_new_tasks(context: ContextTypes.DEFAULT_TYPE):
    """Revisa cada X minutos si hay tareas nuevas asignadas y notifica al instante."""
    team = load_team()
    for tg_id, info in team.items():
        if tg_id == MANAGER_CHAT_ID:
            continue
        asana_gid = info["asana_gid"]
        try:
            current_tasks = await get_pending_tasks(asana_gid)
            current_gids = {t["gid"] for t in current_tasks}

            # Primera vez que vemos a este usuario: guardamos sus tareas actuales sin notificar
            if asana_gid not in known_tasks:
                known_tasks[asana_gid] = current_gids
                logger.info(f"Tareas iniciales cargadas para {info['name']}: {len(current_gids)}")
                continue

            # Detectar tareas nuevas (que no estaban antes)
            new_gids = current_gids - known_tasks[asana_gid]
            new_tasks = [t for t in current_tasks if t["gid"] in new_gids]

            if new_tasks:
                first_name = info["name"].split()[0]
                for task in new_tasks:
                    due = f"\n📅 Vence: *{task['due_on']}*" if task.get("due_on") else ""
                    msg = (
                        f"🔔 *¡Nueva tarea asignada, {first_name}!*\n\n"
                        f"📌 *{task['name']}*{due}\n\n"
                        f"Cuando la completes, responde:\n`/listo` para ver tu lista"
                    )
                    try:
                        await context.bot.send_message(chat_id=tg_id, text=msg, parse_mode="Markdown")
                        logger.info(f"Nueva tarea notificada a {info['name']}: {task['name']}")
                    except Exception as e:
                        logger.error(f"Error notificando a {tg_id}: {e}")

            # Actualizar memoria
            known_tasks[asana_gid] = current_gids

        except Exception as e:
            logger.error(f"Error revisando tareas de {info['name']}: {e}")

# ── RECORDATORIOS ──────────────────────────────────────────────────────────────

async def send_reminder(bot, tg_id: int, name: str, tasks: list, session: str):
    if not tasks:
        return
    emoji = "🌅" if session == "mañana" else "🌆"
    overdue = [t for t in tasks if is_overdue(t)]
    first_name = name.split()[0]
    msg  = f"{emoji} *Hola {first_name}, recordatorio de {session}*\n\n"
    msg += f"Tienes *{len(tasks)}* tarea(s) pendiente(s):\n\n"
    msg += format_task_list(tasks)
    if overdue:
        msg += f"\n\n⚠️ *{len(overdue)} tarea(s) vencida(s)*"
    msg += "\n\n─────────────────\n"
    msg += "Usa `/listo 1`, `/listo 2`... o `/listo_todas`"
    try:
        await bot.send_message(chat_id=tg_id, text=msg, parse_mode="Markdown")
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
    try:
        await context.bot.send_message(chat_id=MANAGER_CHAT_ID, text=msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error enviando reporte: {e}")

# ── COMANDOS ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    team = load_team()
    if tg_id not in team:
        await update.message.reply_text(
            f"👋 Hola! Aún no estás registrado.\nDile a Marco tu ID: `{tg_id}`",
            parse_mode="Markdown")
        return
    name = team[tg_id]["name"].split()[0]
    await update.message.reply_text(
        f"✅ ¡Hola {name}! Conectado al sistema de Lubrikca.\n\n"
        f"• `/mis_tareas` — ver pendientes\n"
        f"• `/listo [número]` — marcar completada\n"
        f"• `/listo_todas` — marcar todas\n\n"
        f"Recibirás notificación inmediata cuando te asignen una tarea nueva.\n"
        f"Recordatorios: {MORNING_HOUR}:00 AM y {AFTERNOON_HOUR}:00 PM.",
        parse_mode="Markdown")

async def cmd_mis_tareas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    team = load_team()
    if tg_id not in team:
        await update.message.reply_text("❌ No estás registrado. Contacta a Marco.")
        return
    tasks = await get_pending_tasks(team[tg_id]["asana_gid"])
    msg = f"📋 *Tus tareas pendientes ({len(tasks)}):*\n\n{format_task_list(tasks)}"
    if tasks:
        msg += "\n\nUsa `/listo [número]` para marcarla como completada."
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_listo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    team = load_team()
    if tg_id not in team:
        await update.message.reply_text("❌ No estás registrado.")
        return
    tasks = await get_pending_tasks(team[tg_id]["asana_gid"])
    if not tasks:
        await update.message.reply_text("✅ ¡Ya no tienes tareas pendientes!")
        return
    if not context.args:
        await update.message.reply_text(
            "¿Cuál completaste?\n\n" + format_task_list(tasks) + "\n\nEj: `/listo 1`",
            parse_mode="Markdown")
        return
    try:
        idx = int(context.args[0]) - 1
        if not (0 <= idx < len(tasks)):
            raise ValueError
    except ValueError:
        await update.message.reply_text(f"❌ Usa un número entre 1 y {len(tasks)}.")
        return
    task = tasks[idx]
    if await complete_task(task["gid"]):
        # Actualizar memoria para que no la vuelva a contar como nueva
        asana_gid = team[tg_id]["asana_gid"]
        if asana_gid in known_tasks:
            known_tasks[asana_gid].discard(task["gid"])
        await update.message.reply_text(
            f"🎉 ¡Listo! Marcado en Asana:\n✅ *{task['name']}*", parse_mode="Markdown")
        try:
            await context.bot.send_message(
                chat_id=MANAGER_CHAT_ID,
                text=f"✅ *{team[tg_id]['name']}* completó:\n_{task['name']}_",
                parse_mode="Markdown")
        except Exception:
            pass
    else:
        await update.message.reply_text("❌ Error al actualizar Asana. Intenta de nuevo.")

async def cmd_listo_todas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    team = load_team()
    if tg_id not in team:
        await update.message.reply_text("❌ No estás registrado.")
        return
    tasks = await get_pending_tasks(team[tg_id]["asana_gid"])
    if not tasks:
        await update.message.reply_text("✅ ¡Ya no tienes tareas pendientes!")
        return
    results = []
    for t in tasks:
        if await complete_task(t["gid"]):
            results.append(t["name"])
    # Limpiar memoria
    asana_gid = team[tg_id]["asana_gid"]
    if asana_gid in known_tasks:
        known_tasks[asana_gid] = set()
    await update.message.reply_text(
        f"🎉 *{len(results)}/{len(tasks)}* tareas completadas en Asana.", parse_mode="Markdown")
    if results:
        try:
            names_list = "\n".join(f"✅ _{n}_" for n in results)
            await context.bot.send_message(
                chat_id=MANAGER_CHAT_ID,
                text=f"✅ *{team[tg_id]['name']}* completó todo:\n{names_list}",
                parse_mode="Markdown")
        except Exception:
            pass

async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MANAGER_CHAT_ID:
        await update.message.reply_text("❌ Solo el manager puede ver el reporte.")
        return
    await job_daily_report(context)

async def cmd_mi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 Tu ID de Telegram es:\n`{update.effective_user.id}`\n\nPásaselo a Marco para registrarte.",
        parse_mode="Markdown")

async def cmd_equipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MANAGER_CHAT_ID:
        await update.message.reply_text("❌ Solo el manager puede ver esto.")
        return
    team = load_team()
    members = [(tg_id, info) for tg_id, info in team.items() if tg_id != MANAGER_CHAT_ID]
    msg = f"👥 *Equipo registrado ({len(members)} personas):*\n\n"
    for tg_id, info in members:
        msg += f"• *{info['name']}* — `{tg_id}`\n"
    msg += "\nPara agregar alguien: edita `team.txt` y reinicia en Railway."
    await update.message.reply_text(msg, parse_mode="Markdown")

# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("mis_tareas",  cmd_mis_tareas))
    app.add_handler(CommandHandler("listo",       cmd_listo))
    app.add_handler(CommandHandler("listo_todas", cmd_listo_todas))
    app.add_handler(CommandHandler("reporte",     cmd_reporte))
    app.add_handler(CommandHandler("mi_id",       cmd_mi_id))
    app.add_handler(CommandHandler("equipo",      cmd_equipo))

    jq = app.job_queue
    jq.run_daily(job_morning,      time(MORNING_HOUR,   MORNING_MIN,   tzinfo=TZ))
    jq.run_daily(job_afternoon,    time(AFTERNOON_HOUR, AFTERNOON_MIN, tzinfo=TZ))
    jq.run_daily(job_daily_report, time(REPORT_HOUR,    REPORT_MIN,    tzinfo=TZ))

    # Revisión de tareas nuevas cada X minutos
    jq.run_repeating(job_check_new_tasks, interval=CHECK_INTERVAL_MINUTES * 60, first=10)

    logger.info(
        f"✅ Bot Lubrikca listo | Recordatorios: {MORNING_HOUR}:00 y {AFTERNOON_HOUR}:00 | "
        f"Reporte: {REPORT_HOUR}:00 | Revisión de nuevas tareas: cada {CHECK_INTERVAL_MINUTES} min"
    )
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

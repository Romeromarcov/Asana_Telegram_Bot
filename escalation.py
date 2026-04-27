"""
Motor de escalación automática — Bot Lubrikca v4.1

Reglas de recordatorio:

TAREAS RECURRENTES:
- Intraday/Diaria : solo recordatorio de las 3pm
- Semanal         : 3 días antes, 1 día antes
- Quincenal       : 7 días antes, 3 días antes, 1 día antes
- Mensual         : 15 días antes, 7 días antes, 3 días antes, 1 día antes

TAREAS ÚNICAS — calculado dinámicamente desde la fecha de creación:
- Plazo >= 30 días : 15d + 7d + 3d + 1d antes (mismos que mensual)
- Plazo >= 14 días : 7d + 3d + 1d antes        (mismos que quincenal)
- Plazo >= 7 días  : 3d + 1d antes              (mismos que semanal)
- Plazo >= 2 días  : 1d antes
- Plazo == 1 día   : solo el recordatorio de tarde

REGLA ESPECIAL: tarea que vence lunes → recordar el viernes anterior

Escalación a gerencia por tareas vencidas:
- Tarde del día que vence
- Mañana del día siguiente
- 24h, 48h, 72h después → BLOQUEAR
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
import pytz

logger = logging.getLogger(__name__)

ALERT_STATE_FILE = Path(__file__).parent / "alert_state.json"
TASK_META_FILE   = Path(__file__).parent / "task_meta.json"

# ── PERSISTENCIA DE ESTADO ─────────────────────────────────────────────────────

def load_alert_state() -> dict:
    if not ALERT_STATE_FILE.exists():
        return {}
    try:
        return json.loads(ALERT_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_alert_state(state: dict):
    ALERT_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def load_task_meta() -> dict:
    """Guarda metadatos de tareas únicas: {task_gid: {created_on, due_on, total_days}}"""
    if not TASK_META_FILE.exists():
        return {}
    try:
        return json.loads(TASK_META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_task_meta(meta: dict):
    TASK_META_FILE.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

def register_unique_task(task_gid: str, created_on: str, due_on: str):
    """Registra una tarea única con su fecha de creación para calcular recordatorios."""
    meta = load_task_meta()
    if task_gid not in meta and created_on and due_on:
        try:
            d_created = datetime.strptime(created_on, "%Y-%m-%d").date()
            d_due     = datetime.strptime(due_on,     "%Y-%m-%d").date()
            total_days = (d_due - d_created).days
            meta[task_gid] = {
                "created_on": created_on,
                "due_on":     due_on,
                "total_days": total_days,
            }
            save_task_meta(meta)
        except Exception as e:
            logger.warning(f"No se pudo registrar meta de tarea {task_gid}: {e}")

def get_task_total_days(task_gid: str) -> int | None:
    meta = load_task_meta()
    return meta.get(task_gid, {}).get("total_days")

# ── HELPERS ────────────────────────────────────────────────────────────────────

def mark_alert_sent(task_gid: str, alert_key: str):
    state = load_alert_state()
    if task_gid not in state:
        state[task_gid] = {"alerts_sent": [], "blocked": False}
    if alert_key not in state[task_gid]["alerts_sent"]:
        state[task_gid]["alerts_sent"].append(alert_key)
    save_alert_state(state)

def was_alert_sent(task_gid: str, alert_key: str) -> bool:
    state = load_alert_state()
    return alert_key in state.get(task_gid, {}).get("alerts_sent", [])

def is_task_blocked(task_gid: str) -> bool:
    state = load_alert_state()
    return state.get(task_gid, {}).get("blocked", False)

def block_task(task_gid: str):
    state = load_alert_state()
    if task_gid not in state:
        state[task_gid] = {"alerts_sent": [], "blocked": False}
    state[task_gid]["blocked"] = True
    save_alert_state(state)

def cleanup_alert_state(active_gids: set):
    state    = load_alert_state()
    meta     = load_task_meta()
    changed  = False
    for gid in list(state.keys()):
        if gid not in active_gids:
            del state[gid]
            changed = True
    for gid in list(meta.keys()):
        if gid not in active_gids:
            del meta[gid]
    if changed:
        save_alert_state(state)
    save_task_meta(meta)

def get_freq_for_task(task_gid: str, recurring_data: list) -> str | None:
    for r in recurring_data:
        if r.get("last_task_gid") == task_gid:
            return r.get("freq")
    return None

def days_until_due(due_on: str, tz) -> int | None:
    if not due_on:
        return None
    due_date = datetime.strptime(due_on, "%Y-%m-%d").date()
    today    = datetime.now(tz).date()
    return (due_date - today).days

def hours_since_due(due_on: str, tz) -> float | None:
    if not due_on:
        return None
    due_date = datetime.strptime(due_on, "%Y-%m-%d").date()
    now      = datetime.now(tz)
    if due_date >= now.date():
        return None
    due_dt = datetime(due_date.year, due_date.month, due_date.day, 23, 59,
                      tzinfo=tz)
    return (now - due_dt).total_seconds() / 3600

# ── LÓGICA PRINCIPAL DE RECORDATORIOS ─────────────────────────────────────────

def get_thresholds_for_unique(task_gid: str) -> list[tuple[str, int]]:
    """
    Calcula los umbrales de recordatorio para una tarea única
    basándose en el plazo total (fecha creación → fecha vencimiento).
    """
    total_days = get_task_total_days(task_gid)

    if total_days is None:
        # Sin metadata: usar regla mínima
        return [("1d", 1)]

    if total_days >= 30:
        # Plazo de 1 mes o más → mismos que mensual
        return [("15d", 15), ("7d", 7), ("3d", 3), ("1d", 1)]
    elif total_days >= 14:
        # Plazo de 2 semanas a 1 mes → mismos que quincenal
        return [("7d", 7), ("3d", 3), ("1d", 1)]
    elif total_days >= 7:
        # Plazo de 1 semana a 2 semanas → mismos que semanal
        return [("3d", 3), ("1d", 1)]
    elif total_days >= 2:
        # Plazo de 2 a 6 días → solo 1 día antes
        return [("1d", 1)]
    else:
        # Plazo de 1 día → solo recordatorio de tarde
        return []

def should_remind_before_due(task_gid: str, due_on: str, freq: str | None, tz) -> list[str]:
    """
    Devuelve lista de claves de alerta que deben enviarse ahora.
    """
    alerts = []
    days   = days_until_due(due_on, tz)
    if days is None or days < 0:
        return alerts

    due_date    = datetime.strptime(due_on, "%Y-%m-%d").date()
    due_weekday = due_date.weekday()  # 0=lun

    # ── Umbrales según tipo de tarea ──────────────────────────────────────────
    if freq in ("intraday", "daily"):
        thresholds = []  # Solo recordatorio de tarde, sin escalación extra

    elif freq == "weekly":
        thresholds = [("3d", 3), ("1d", 1)]

    elif freq == "biweekly":
        thresholds = [("7d", 7), ("3d", 3), ("1d", 1)]

    elif freq == "monthly":
        thresholds = [("15d", 15), ("7d", 7), ("3d", 3), ("1d", 1)]

    else:
        # Tarea única — calculado dinámicamente
        thresholds = get_thresholds_for_unique(task_gid)

    # ── Regla especial: vence lunes → recordar viernes (3 días antes) ────────
    if due_weekday == 0:
        has_3d = any(k == "3d" for k, _ in thresholds)
        if not has_3d and days == 3:
            thresholds.append(("fri_before_monday", 3))

    # ── Evaluar qué alertas corresponden hoy ─────────────────────────────────
    for key, threshold_days in thresholds:
        if days == threshold_days and not was_alert_sent(task_gid, key):
            alerts.append(key)

    return alerts

def should_escalate_overdue(task_gid: str, due_on: str, session: str, tz) -> tuple[str | None, bool]:
    """
    Determina si hay que escalar al manager una tarea vencida.
    Devuelve (alert_key, should_block).
    """
    hours = hours_since_due(due_on, tz)
    if hours is None:
        return None, False

    escalation_steps = [
        # key           min_h  max_h  session  block
        ("overdue_pm",  0,     24,    "pm",    False),
        ("overdue_am",  0,     48,    "am",    False),
        ("24h",         24,    48,    None,    False),
        ("48h",         48,    72,    None,    False),
        ("72h",         72,    9999,  None,    True ),
    ]

    for key, min_h, max_h, req_session, should_block in escalation_steps:
        if min_h <= hours < max_h:
            if req_session and req_session != session:
                continue
            if not was_alert_sent(task_gid, key):
                return key, should_block
    return None, False

# ── ETIQUETAS PARA MENSAJES ────────────────────────────────────────────────────

DAYS_LABEL = {
    "15d":              "15 días",
    "7d":               "7 días",
    "3d":               "3 días",
    "1d":               "mañana",
    "fri_before_monday":"el fin de semana (vence el lunes)",
}

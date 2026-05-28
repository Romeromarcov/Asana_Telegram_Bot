"""
Panel de control web — Bot Lubrikca v6.0
GET  /               → HTML del dashboard (5 tabs)
GET  /api/summary    → resumen de tareas por persona
GET  /api/recurring  → estado checklist + lista para gestión
POST /api/recurring/add          → agregar tarea recurrente
DELETE /api/recurring/{idx}      → eliminar recurrente
POST /api/recurring/{idx}/toggle → pausar/reanudar
GET  /api/team       → miembros del equipo
POST /api/team/remove/{tg_id}    → desactivar miembro
GET  /api/config     → configuración activa
POST /api/config     → guardar overrides en dashboard_config.json
"""

import os
import json
import logging
import secrets
import hmac
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz
from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from utils import ASANA_BASE, load_team, http_client

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent
ASANA_TOKEN     = os.environ.get("ASANA_TOKEN", "")
ASANA_WORKSPACE = os.environ.get("ASANA_WORKSPACE_ID", "")
TIMEZONE        = os.environ.get("TIMEZONE", "America/Caracas")
DASHBOARD_PASS  = os.environ.get("DASHBOARD_PASSWORD", "")
MANAGER_TG_ID   = int(os.environ.get("MANAGER_CHAT_ID", "0"))
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TZ              = pytz.timezone(TIMEZONE)
CFG_FILE        = BASE_DIR / "dashboard_config.json"

AREA_COLORS = {
    "manager": "#D4537E", "ventas": "#1D9E75", "logística": "#D85A30",
    "almacén": "#D85A30", "admin":  "#378ADD", "cobranza":  "#7F77DD",
    "finanzas": "#EF9F27", "atención": "#7F77DD",
}
WEEKDAY_NAMES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
WEEKDAY_FULL  = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app_):
    """Inicializa la base de datos al arrancar el servicio Web."""
    from db import setup_db
    setup_db()
    yield

app = FastAPI(title="Lubrikca Dashboard", docs_url=None, redoc_url=None, lifespan=lifespan)

# ── Auth básica opcional ───────────────────────────────────────────────────────
security = HTTPBasic(auto_error=False)

def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    if not DASHBOARD_PASS:
        return True
    if not credentials:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    if not secrets.compare_digest(credentials.password.encode(), DASHBOARD_PASS.encode()):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return True


def load_recurring() -> list:
    """Carga tareas recurrentes. Prioridad: PostgreSQL → archivo local."""
    from db import db_get
    db_data = db_get("recurring")
    if db_data is not None:
        return db_data
    try:
        return json.loads((BASE_DIR / "recurring.json").read_text(encoding="utf-8"))
    except Exception:
        return []

def save_recurring(data: list):
    """Guarda tareas recurrentes en PostgreSQL y en archivo local."""
    from db import db_set
    db_set("recurring", data)
    try:
        (BASE_DIR / "recurring.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass

def load_saved_config() -> dict:
    if CFG_FILE.exists():
        try:
            return json.loads(CFG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def get_area_color(name: str) -> str:
    nl = name.lower()
    for key, color in AREA_COLORS.items():
        if key in nl:
            return color
    return "#8B9BAB"

def get_initials(name: str) -> str:
    words = name.split("(")[0].strip().split()
    return "".join(w[0].upper() for w in words[:2])

# ── Asana helpers ──────────────────────────────────────────────────────────────
async def asana_get_tasks(asana_gid: str) -> list:
    if not ASANA_TOKEN:
        return []
    headers = {"Authorization": f"Bearer {ASANA_TOKEN}"}
    params  = {
        "assignee": asana_gid, "workspace": ASANA_WORKSPACE,
        "completed_since": "now",
        "opt_fields": "gid,name,due_on,permalink_url",
        "limit": 50,
    }
    try:
        r = await http_client.get(f"{ASANA_BASE}/tasks", headers=headers, params=params)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception:
        return []

async def asana_task_completed(gid: str) -> bool:
    if not ASANA_TOKEN or not gid:
        return False
    try:
        r = await http_client.get(
            f"{ASANA_BASE}/tasks/{gid}",
            headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
            params={"opt_fields": "completed"},
        )
        return r.json().get("data", {}).get("completed", False)
    except Exception:
        return False

# ══════════════════════════════════════════════════════════════════════════════
# API ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/summary")
async def api_summary(_=Depends(check_auth)):
    team     = load_team()
    today    = datetime.now(TZ).strftime("%Y-%m-%d")
    statuses = _load_task_statuses()
    result   = []
    for tg_id, info in team.items():
        tasks   = await asana_get_tasks(info["asana_gid"])
        overdue = sum(1 for t in tasks if t.get("due_on") and t["due_on"] < today)
        # Añadir estado local a cada tarea
        for t in tasks:
            t["status"] = statuses.get(t["gid"], "pending")
        result.append({
            "tg_id": tg_id, "name": info["name"], "asana_gid": info["asana_gid"],
            "initials": get_initials(info["name"]), "color": get_area_color(info["name"]),
            "total": len(tasks), "overdue": overdue,
            "is_manager": tg_id == MANAGER_TG_ID, "tasks": tasks,
        })
    result.sort(key=lambda x: (-x["overdue"], -x["total"]))
    return result

@app.get("/api/recurring")
async def api_recurring(_=Depends(check_auth)):
    data       = load_recurring()
    today      = datetime.now(TZ).date()
    week_start = today - timedelta(days=today.weekday())
    result     = []
    for idx, r in enumerate(data):
        completed = await asana_task_completed(r.get("last_task_gid", ""))
        last_str  = r.get("last_created", "")
        try:
            this_week = datetime.strptime(last_str, "%Y-%m-%d").date() >= week_start
        except Exception:
            this_week = False
        freq  = r.get("freq", "weekly")
        wd    = r.get("weekday")
        if freq == "intraday":
            freq_label = f"Diario ({', '.join(str(h)+':00' for h in r.get('hours',[]))})"
            day_label  = "Diario"
        else:
            freq_label = f"Semanal — {WEEKDAY_FULL[wd]}" if wd is not None else "Semanal"
            day_label  = WEEKDAY_NAMES[wd] if wd is not None else "—"

        result.append({
            "idx": idx, "task_name": r["task_name"], "assignee": r["assignee_name"],
            "color": get_area_color(r["assignee_name"]),
            "initials": get_initials(r["assignee_name"]),
            "freq_label": freq_label, "day_label": day_label,
            "pending_count": r.get("pending_count", 0),
            "completed": completed, "this_week": this_week,
            "last_created": last_str,
            "paused": r.get("paused", False),
            "notes": r.get("notes") or "",
            "status": (
                "paused"    if r.get("paused")
                else "completed" if completed
                else "pending"   if this_week
                else "missing"
            ),
        })
    return result

@app.post("/api/recurring/add")
async def add_recurring(request: Request, _=Depends(check_auth)):
    body = await request.json()
    team = load_team()
    tg_id_str  = str(body.get("assignee_tg_id", ""))
    tg_id      = int(tg_id_str) if tg_id_str.isdigit() else None
    if tg_id is None or tg_id not in team:
        raise HTTPException(400, "Responsable inválido")
    member = team[tg_id]
    freq   = body.get("freq", "weekly")
    notes  = body.get("notes", "").strip() or None
    entry  = {
        "task_name":     body["task_name"].strip(),
        "assignee_gid":  member["asana_gid"],
        "assignee_tg_id": tg_id,
        "assignee_name": member["name"],
        "freq":          freq,
        "due_on":        None,
        "last_task_gid": "",
        "last_created":  "",
        "pending_count": 0,
    }
    if notes:
        entry["notes"] = notes
    if freq == "weekly":
        entry["weekday"] = int(body.get("weekday", 0))
    elif freq == "intraday":
        hours = [int(h) for h in body.get("hours", [9])]
        entry["hours"]         = hours
        entry["times_per_day"] = len(hours)
    data = load_recurring()
    data.append(entry)
    save_recurring(data)
    return {"ok": True, "added": entry["task_name"]}

@app.delete("/api/recurring/{idx}")
async def delete_recurring(idx: int, _=Depends(check_auth)):
    data = load_recurring()
    if idx < 0 or idx >= len(data):
        raise HTTPException(404, "No encontrado")
    name = data.pop(idx)["task_name"]
    save_recurring(data)
    return {"ok": True, "removed": name}

@app.post("/api/recurring/{idx}/toggle")
async def toggle_recurring(idx: int, _=Depends(check_auth)):
    data = load_recurring()
    if idx < 0 or idx >= len(data):
        raise HTTPException(404)
    data[idx]["paused"] = not data[idx].get("paused", False)
    save_recurring(data)
    return {"ok": True, "paused": data[idx]["paused"]}

@app.get("/api/team")
async def api_team(_=Depends(check_auth)):
    team = load_team()
    return [
        {
            "tg_id": tg_id, "name": info["name"], "asana_gid": info["asana_gid"],
            "initials": get_initials(info["name"]), "color": get_area_color(info["name"]),
            "is_manager": tg_id == MANAGER_TG_ID,
        }
        for tg_id, info in team.items()
    ]

@app.post("/api/team/remove/{tg_id}")
async def remove_team_member(tg_id: int, _=Depends(check_auth)):
    from team_manager import remove_member
    name = remove_member(tg_id)
    if not name:
        raise HTTPException(404, "Miembro no encontrado o ya inactivo")
    return {"ok": True, "removed": name}

@app.get("/api/config")
async def api_config(_=Depends(check_auth)):
    """Devuelve la configuración activa del sistema (leída de variables de entorno)."""
    def ev(key, default):
        return os.environ.get(key, default)
    return {
        "TIMEZONE":               ev("TIMEZONE",               "America/Caracas"),
        "MORNING_HOUR":           int(ev("MORNING_HOUR",        "9")),
        "MORNING_MIN":            int(ev("MORNING_MIN",         "0")),
        "AFTERNOON_HOUR":         int(ev("AFTERNOON_HOUR",      "15")),
        "AFTERNOON_MIN":          int(ev("AFTERNOON_MIN",       "0")),
        "REPORT_HOUR":            int(ev("REPORT_HOUR",         "18")),
        "REPORT_MIN":             int(ev("REPORT_MIN",          "0")),
        "CHECK_INTERVAL_MINUTES": int(ev("CHECK_INTERVAL_MINUTES", "5")),
        "_note": "Para modificar la configuración actualiza las variables de entorno en Railway.",
    }

@app.post("/api/config")
async def save_config(request: Request, _=Depends(check_auth)):
    """
    Nota: en la arquitectura de dos servicios (Web + Worker), los cambios
    de configuración deben hacerse en las Variables de Entorno de Railway
    (servicio Worker) y re-desplegar el Worker para que tengan efecto.
    Este endpoint es informativo.
    """
    return {
        "ok": False,
        "message": (
            "Para cambiar la configuración ve a Railway → servicio Worker → Variables "
            "y actualiza las variables de entorno (TIMEZONE, MORNING_HOUR, etc.). "
            "El Worker se re-desplegará automáticamente."
        ),
    }

@app.delete("/api/config")
async def reset_config(_=Depends(check_auth)):
    """No-op en arquitectura de dos servicios; la config viene de env vars."""
    return {"ok": True, "message": "La configuración se gestiona via variables de entorno."}

# ── Recurring: editar ──────────────────────────────────────────────────────────
@app.put("/api/recurring/{idx}")
async def edit_recurring(idx: int, request: Request, _=Depends(check_auth)):
    body = await request.json()
    data = load_recurring()
    if idx < 0 or idx >= len(data):
        raise HTTPException(404, "No encontrado")
    r = data[idx]
    if "task_name" in body and body["task_name"].strip():
        r["task_name"] = body["task_name"].strip()
    if "notes" in body:
        r["notes"] = body["notes"].strip() or None
    if "weekday" in body:
        r["weekday"] = int(body["weekday"])
    if "freq" in body:
        r["freq"] = body["freq"]
    if "hours" in body:
        r["hours"] = [int(h) for h in body["hours"]]
        r["times_per_day"] = len(r["hours"])
    if "assignee_tg_id" in body:
        team = load_team()
        tg_id = int(body["assignee_tg_id"])
        if tg_id in team:
            r["assignee_tg_id"] = tg_id
            r["assignee_name"]  = team[tg_id]["name"]
            r["assignee_gid"]   = team[tg_id]["asana_gid"]
    data[idx] = r
    save_recurring(data)
    return {"ok": True, "updated": r["task_name"]}

# ── Recurring: reiniciar contador pendientes ───────────────────────────────────
@app.post("/api/recurring/{idx}/reset")
async def reset_recurring_count(idx: int, _=Depends(check_auth)):
    from escalation import register_noncompliance
    data = load_recurring()
    if idx < 0 or idx >= len(data):
        raise HTTPException(404)
    r             = data[idx]
    pending_count = r.get("pending_count", 0)

    # Registrar como no cumplida si había pendientes
    if pending_count > 0:
        last_task_gid = r.get("last_task_gid") or f"reset_{idx}_{r['task_name'][:20]}"
        due_on        = r.get("due_on") or r.get("last_created") or ""
        hours_overdue = 0.0
        if due_on:
            try:
                from datetime import date as _date
                due_d = datetime.strptime(due_on, "%Y-%m-%d").date()
                hours_overdue = max(0.0, ((_date.today() - due_d).days) * 24.0)
            except Exception:
                pass
        try:
            register_noncompliance(
                task_gid       = last_task_gid,
                task_name      = r["task_name"],
                assignee_name  = r.get("assignee_name", "—"),
                assignee_tg_id = r.get("assignee_tg_id", 0),
                due_on         = due_on or "—",
                hours_overdue  = hours_overdue,
                recurring_name = r.get("freq"),
            )
        except Exception as e:
            logger.warning(f"No-cumplimiento al resetear: {e}")

    data[idx]["pending_count"] = 0
    save_recurring(data)
    return {"ok": True}

# ── Tareas: marcar completada en Asana ────────────────────────────────────────
@app.post("/api/tasks/{task_gid}/complete")
async def complete_task_endpoint(task_gid: str, _=Depends(check_auth)):
    if not ASANA_TOKEN:
        raise HTTPException(503, "ASANA_TOKEN no configurado")
    try:
        r = await http_client.put(
            f"{ASANA_BASE}/tasks/{task_gid}",
            headers={"Authorization": f"Bearer {ASANA_TOKEN}", "Content-Type": "application/json"},
            json={"data": {"completed": True}},
        )
        r.raise_for_status()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── Equipo: agregar miembro ────────────────────────────────────────────────────
@app.post("/api/team/add")
async def add_team_member(request: Request, _=Depends(check_auth)):
    body = await request.json()
    tg_id     = body.get("tg_id")
    asana_gid = body.get("asana_gid", "").strip()
    name      = body.get("name", "").strip()
    if not tg_id or not asana_gid or not name:
        raise HTTPException(400, "tg_id, asana_gid y name son obligatorios")
    from team_manager import add_member
    ok = add_member(int(tg_id), asana_gid, name)
    if not ok:
        raise HTTPException(409, "El miembro ya existe")
    return {"ok": True, "added": name}

# ── Tareas no cumplidas ────────────────────────────────────────────────────────
@app.get("/api/noncompliant")
async def api_noncompliant(_=Depends(check_auth)):
    from escalation import load_noncompliant
    records = load_noncompliant()
    return sorted(records, key=lambda r: r.get("registered_at", ""), reverse=True)

@app.delete("/api/noncompliant/{task_gid}")
async def delete_noncompliant(task_gid: str, _=Depends(check_auth)):
    from escalation import load_noncompliant, save_noncompliant
    records = [r for r in load_noncompliant() if r.get("task_gid") != task_gid]
    save_noncompliant(records)
    return {"ok": True}

# ── Modelo Gemini ──────────────────────────────────────────────────────────────
@app.get("/api/gemini")
async def api_gemini(_=Depends(check_auth)):
    from db import db_get
    active = db_get("gemini_model") or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    # La API key está en el servicio Worker — leer estado desde DB (sincronizado al arrancar el bot)
    has_key = bool(os.environ.get("GEMINI_API_KEY")) or bool(db_get("has_gemini_key"))
    return {
        "active_model": active,
        "has_api_key":  has_key,
        "available_models": [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
            "gemini-1.5-flash",
        ],
    }

@app.post("/api/gemini")
async def save_gemini_model(request: Request, _=Depends(check_auth)):
    body  = await request.json()
    model = body.get("model", "").strip()
    if not model:
        raise HTTPException(400, "model requerido")
    from db import db_set
    db_set("gemini_model", model)
    return {"ok": True, "model": model}

# ══════════════════════════════════════════════════════════════════════════════
# ÁREAS DE TRABAJO
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/db-status")
async def api_db_status(_=Depends(check_auth)):
    from db import db_connected
    return {"connected": db_connected()}

@app.get("/api/areas")
async def api_areas(_=Depends(check_auth)):
    from teams_manager import load_teams
    teams = load_teams()
    result = []
    for slug, t in teams.items():
        if not isinstance(t, dict):
            continue
        result.append({
            "slug":         slug,
            "name":         t.get("name", slug),
            "leader_name":  t.get("leader_name", "—"),
            "leader_tg_id": t.get("leader_tg_id", 0),
            "members":      t.get("members", []),
        })
    return result

@app.post("/api/areas")
async def create_area_endpoint(request: Request, _=Depends(check_auth)):
    body         = await request.json()
    name         = body.get("name", "").strip()
    leader_tg_id = int(body.get("leader_tg_id", 0))
    if not name or not leader_tg_id:
        raise HTTPException(400, "name y leader_tg_id son obligatorios")
    team = load_team()
    if leader_tg_id not in team:
        raise HTTPException(400, "El líder no está registrado en el equipo")
    leader = team[leader_tg_id]
    from teams_manager import create_area
    try:
        slug = create_area(name, leader_tg_id, leader["asana_gid"], leader["name"])
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "slug": slug}

@app.delete("/api/areas/{slug}")
async def delete_area_endpoint(slug: str, _=Depends(check_auth)):
    from teams_manager import delete_area
    if not delete_area(slug):
        raise HTTPException(404, "Área no encontrada")
    return {"ok": True}

@app.post("/api/areas/{slug}/members")
async def add_area_member(slug: str, request: Request, _=Depends(check_auth)):
    body  = await request.json()
    tg_id = int(body.get("tg_id", 0))
    if not tg_id:
        raise HTTPException(400, "tg_id requerido")
    team = load_team()
    if tg_id not in team:
        raise HTTPException(400, "El miembro no está registrado en el equipo")
    member = team[tg_id]
    from teams_manager import add_member
    ok = add_member(slug, tg_id, member["asana_gid"], member["name"])
    if not ok:
        raise HTTPException(409, "Miembro ya existe en el área o área no encontrada")
    return {"ok": True}

@app.delete("/api/areas/{slug}/members/{tg_id}")
async def remove_area_member(slug: str, tg_id: int, _=Depends(check_auth)):
    from teams_manager import remove_member
    if not remove_member(slug, tg_id):
        raise HTTPException(404, "Miembro o área no encontrada")
    return {"ok": True}

@app.put("/api/areas/{slug}/leader")
async def update_area_leader(slug: str, request: Request, _=Depends(check_auth)):
    body         = await request.json()
    leader_tg_id = int(body.get("leader_tg_id", 0))
    if not leader_tg_id:
        raise HTTPException(400, "leader_tg_id requerido")
    team = load_team()
    if leader_tg_id not in team:
        raise HTTPException(400, "El líder no está registrado en el equipo")
    leader = team[leader_tg_id]
    from teams_manager import update_leader
    if not update_leader(slug, leader_tg_id, leader["asana_gid"], leader["name"]):
        raise HTTPException(404, "Área no encontrada")
    return {"ok": True}

@app.get("/api/areas/{slug}/tasks")
async def get_area_tasks_endpoint(slug: str, _=Depends(check_auth)):
    from teams_manager import get_area_tasks_by_slug
    return get_area_tasks_by_slug(slug)

# ── Eliminar tarea (solo manager) ──────────────────────────────────────────────

@app.delete("/api/tasks/{task_gid}")
async def delete_task_endpoint(task_gid: str, _=Depends(check_auth)):
    """Elimina (completa/archiva) una tarea en Asana. Solo el manager puede hacerlo."""
    import os
    from utils import http_client, ASANA_BASE
    token = os.environ.get("ASANA_TOKEN", "")
    if not token:
        raise HTTPException(500, "ASANA_TOKEN no configurado")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = await http_client.delete(
            f"{ASANA_BASE}/tasks/{task_gid}", headers=headers, timeout=15
        )
        if r.status_code not in (200, 204):
            raise HTTPException(r.status_code, f"Asana error: {r.text[:200]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True}

# ── Permisos ───────────────────────────────────────────────────────────────────

@app.get("/api/permissions")
async def get_permissions_endpoint(_=Depends(check_auth)):
    from teams_manager import get_permissions
    return get_permissions()

@app.post("/api/permissions")
async def save_permissions_endpoint(request: Request, _=Depends(check_auth)):
    body = await request.json()
    from teams_manager import DEFAULT_PERMISSIONS, save_permissions
    # Solo guardar claves conocidas (evitar inyección)
    filtered = {k: bool(body.get(k, v)) for k, v in DEFAULT_PERMISSIONS.items()}
    save_permissions(filtered)
    return {"ok": True}

@app.get("/health")
async def health():
    return {"status": "ok"}

# ══════════════════════════════════════════════════════════════════════════════
# WEBHOOKS DE ASANA — notificaciones en tiempo real
# ══════════════════════════════════════════════════════════════════════════════

def _notified_tasks() -> set:
    from db import db_get
    return set(db_get("notified_tasks") or [])

def _save_notified(gids: set):
    from db import db_set
    # Conservar máximo 10 000 GIDs para no crecer indefinidamente
    db_set("notified_tasks", list(gids)[-10_000:])

async def _telegram_notify(chat_id: int, text: str, keyboard: dict | None = None):
    """Envía mensaje de Telegram directo via Bot API (sin necesitar el worker)."""
    if not TELEGRAM_TOKEN:
        logger.warning("TELEGRAM_TOKEN no configurado en web service — no se puede notificar")
        return
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)
    try:
        await http_client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=10,
        )
    except Exception as e:
        logger.error(f"Error enviando TG webhook-notif a {chat_id}: {e}")

@app.post("/api/webhooks/asana")
async def asana_webhook_receiver(request: Request):
    """
    Receptor de eventos Asana. Dos modos:
      1. Handshake inicial: Asana envía X-Hook-Secret → devolvemos el mismo header.
      2. Eventos: procesamos task.added y notificamos por Telegram.
    """
    logger.info(f"Webhook Asana — método: {request.method}, headers: {dict(request.headers)}")

    # ── Handshake ─────────────────────────────────────────────────────────────
    hook_secret = request.headers.get("X-Hook-Secret") or request.headers.get("x-hook-secret")
    if hook_secret:
        try:
            from db import db_set
            db_set("asana_webhook_secret", hook_secret)
        except Exception as e:
            logger.warning(f"Webhook: no se pudo guardar secret en DB: {e}")
        logger.info("✅ Webhook Asana: handshake completado")
        return Response(
            status_code=200,
            headers={"X-Hook-Secret": hook_secret},
        )

    # ── Verificar firma ───────────────────────────────────────────────────────
    body = await request.body()
    sig  = request.headers.get("X-Hook-Signature", "")
    try:
        from db import db_get
        secret = db_get("asana_webhook_secret") or ""
    except Exception:
        secret = ""
    if secret and sig:
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            logger.warning("Webhook Asana: firma inválida — ignorando")
            raise HTTPException(400, "Invalid signature")

    # ── Procesar eventos ──────────────────────────────────────────────────────
    try:
        payload = json.loads(body)
    except Exception:
        return {"ok": True}

    events = payload.get("events", [])
    if not events:
        return {"ok": True}

    team       = load_team()
    gid_to_tg  = {info["asana_gid"]: tg_id  for tg_id, info in team.items()}
    gid_to_name= {info["asana_gid"]: info["name"] for _, info in team.items()}
    notified   = _notified_tasks()
    changed    = False

    for event in events:
        res    = event.get("resource", {})
        action = event.get("action", "")
        if res.get("resource_type") != "task":
            continue
        if action not in ("added", "changed"):
            continue
        # Para "changed", solo nos importa si cambió el assignee
        if action == "changed":
            change_field = (event.get("change") or {}).get("field", "")
            if change_field != "assignee":
                continue

        task_gid    = res.get("gid", "")
        creator_gid = (event.get("user") or {}).get("gid", "")

        if not task_gid or task_gid in notified:
            continue

        # Obtener detalles de la tarea
        try:
            r = await http_client.get(
                f"{ASANA_BASE}/tasks/{task_gid}",
                headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
                params={"opt_fields": "gid,name,assignee,due_on,completed,permalink_url"},
                timeout=10,
            )
            task = r.json().get("data", {})
        except Exception as e:
            logger.error(f"Webhook: error obteniendo tarea {task_gid}: {e}")
            continue

        if not task or task.get("completed"):
            notified.add(task_gid)
            changed = True
            continue

        assignee_gid = (task.get("assignee") or {}).get("gid", "")
        if not assignee_gid:
            continue

        tg_id = gid_to_tg.get(assignee_gid)
        if not tg_id:
            continue

        # ── Filtro: la persona se asignó la tarea a sí misma → no notificar ──
        if creator_gid and creator_gid == assignee_gid:
            logger.info(f"Webhook: {assignee_gid} creó tarea propia — sin notif")
            notified.add(task_gid)
            changed = True
            continue

        # ── Enviar notificación Telegram ──────────────────────────────────────
        first = get_first_name(gid_to_name.get(assignee_gid, ""))
        due   = f"\n📅 Vence: *{task['due_on']}*" if task.get("due_on") else ""
        link  = f"\n🔗 {task['permalink_url']}" if task.get("permalink_url") else ""
        await _telegram_notify(
            tg_id,
            f"🔔 *¡Nueva tarea, {first}!*\n\n📌 *{task['name']}*{due}{link}",
            {"inline_keyboard": [[{"text": "📋 Ver mis tareas", "callback_data": "ver_tareas"}]]},
        )
        notified.add(task_gid)
        changed = True
        logger.info(f"Webhook: notificado {first} — {task['name']}")

    if changed:
        _save_notified(notified)

    return {"ok": True}

@app.post("/api/webhooks/register")
async def register_asana_webhook(request: Request, _=Depends(check_auth)):
    """
    Registra el webhook en Asana.
    Body: {"url": "https://tu-dominio.railway.app/api/webhooks/asana"}
    """
    body = await request.json()
    url  = body.get("url", "").strip()
    if not url:
        raise HTTPException(400, "url es obligatoria")
    if not ASANA_TOKEN or not ASANA_WORKSPACE:
        raise HTTPException(503, "ASANA_TOKEN / ASANA_WORKSPACE_ID no configurados")
    try:
        r = await http_client.post(
            f"{ASANA_BASE}/webhooks",
            headers={"Authorization": f"Bearer {ASANA_TOKEN}", "Content-Type": "application/json"},
            json={"data": {
                "resource": ASANA_WORKSPACE,
                "target":   url,
                # Asana workspace-scope whitelist: solo resource_type sin action,
                # o action="changed" con fields. Sin action = capta todos los eventos de task.
                "filters":  [{"resource_type": "task"}],
            }},
            timeout=20,
        )
        data = r.json()
        if r.status_code >= 400:
            msg = (data.get("errors") or [{}])[0].get("message", r.text[:200])
            raise HTTPException(r.status_code, f"Asana: {msg}")
        return {"ok": True, "webhook": data.get("data", {})}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/webhooks")
async def list_asana_webhooks(_=Depends(check_auth)):
    """Lista webhooks activos en Asana para este workspace."""
    try:
        r = await http_client.get(
            f"{ASANA_BASE}/webhooks",
            headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
            params={"workspace": ASANA_WORKSPACE, "opt_fields": "gid,resource,target,active"},
            timeout=10,
        )
        return r.json().get("data", [])
    except Exception as e:
        raise HTTPException(500, str(e))

@app.delete("/api/webhooks/{webhook_gid}")
async def delete_asana_webhook(webhook_gid: str, _=Depends(check_auth)):
    try:
        await http_client.delete(
            f"{ASANA_BASE}/webhooks/{webhook_gid}",
            headers={"Authorization": f"Bearer {ASANA_TOKEN}"},
            timeout=10,
        )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── Crear tarea desde la UI ────────────────────────────────────────────────────

@app.post("/api/tasks")
async def create_task_endpoint(request: Request, _=Depends(check_auth)):
    """Crea una tarea en Asana desde el dashboard."""
    body = await request.json()
    task_name      = body.get("name", "").strip()
    assignee_tg_id = int(body.get("assignee_tg_id", 0))
    due_on         = body.get("due_on") or None
    notes          = body.get("notes") or None

    if not task_name or not assignee_tg_id:
        raise HTTPException(400, "name y assignee_tg_id son obligatorios")

    team = load_team()
    if assignee_tg_id not in team:
        raise HTTPException(400, "Responsable no encontrado en el equipo")

    asana_gid = team[assignee_tg_id]["asana_gid"]

    payload: dict = {"name": asana_gid, "workspace": ASANA_WORKSPACE, "assignee": asana_gid}
    if due_on:  payload["due_on"] = due_on
    if notes:   payload["notes"]  = notes
    payload["name"] = task_name  # fix overwrite

    try:
        r = await http_client.post(
            f"{ASANA_BASE}/tasks",
            headers={"Authorization": f"Bearer {ASANA_TOKEN}", "Content-Type": "application/json"},
            json={"data": payload},
            timeout=15,
        )
        r.raise_for_status()
        task_gid = r.json().get("data", {}).get("gid", "")
    except Exception as e:
        raise HTTPException(500, f"Error Asana: {e}")

    return {"ok": True, "gid": task_gid, "name": task_name}

# ── Estado de tarea (tracking local en DB) ────────────────────────────────────

_STATUS_LABELS = {
    "pending":     "⏳ Pendiente",
    "in_progress": "🔄 En progreso",
    "review":      "👁 Revisión",
}

def _load_task_statuses() -> dict:
    from db import db_get
    return db_get("task_statuses") or {}

def _save_task_statuses(statuses: dict):
    from db import db_set
    db_set("task_statuses", statuses)

@app.get("/api/task-statuses")
async def get_task_statuses(_=Depends(check_auth)):
    return _load_task_statuses()

@app.put("/api/tasks/{task_gid}/status")
async def update_task_status(task_gid: str, request: Request, _=Depends(check_auth)):
    body    = await request.json()
    status  = body.get("status", "pending")
    if status not in _STATUS_LABELS:
        raise HTTPException(400, f"Estado inválido: {status}")
    statuses = _load_task_statuses()
    if status == "pending":
        statuses.pop(task_gid, None)  # pending es el default, no necesita entrada
    else:
        statuses[task_gid] = status
    _save_task_statuses(statuses)
    return {"ok": True, "status": status, "label": _STATUS_LABELS[status]}

# ══════════════════════════════════════════════════════════════════════════════
# HTML
# ══════════════════════════════════════════════════════════════════════════════

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lubrikca — Panel de Control</title>
<style>
:root {
  --bg:#F5F5F4; --surface:#FFF; --border:#E5E7EB; --border2:#D1D5DB;
  --text:#111827; --text2:#6B7280; --text3:#9CA3AF;
  --radius:10px; --radius-sm:6px; --shadow:0 1px 3px rgba(0,0,0,.08);
  --accent:#111827;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:var(--bg);color:var(--text);font-size:14px}
.btn-success{border-color:#6EE7B7;color:#065F46;background:#D1FAE5}
.btn-success:hover{background:#A7F3D0}
.btn-warning{border-color:#FDE68A;color:#92400E;background:#FFFBEB}
.btn-warning:hover{background:#FEF3C7}
/* Layout */
.app{display:flex;min-height:100vh}
.sidebar{width:220px;background:var(--surface);border-right:1px solid var(--border);
         padding:24px 0;flex-shrink:0;position:sticky;top:0;height:100vh;overflow-y:auto}
.main{flex:1;padding:28px;max-width:1140px}
/* Sidebar */
.logo{padding:0 20px 22px;border-bottom:1px solid var(--border);margin-bottom:14px}
.logo-title{font-size:16px;font-weight:700;color:var(--text)}
.logo-sub{font-size:11px;color:var(--text2);margin-top:2px}
.nav-item{display:flex;align-items:center;gap:10px;padding:9px 20px;cursor:pointer;
          color:var(--text2);font-size:13px;font-weight:500;transition:background .12s,color .12s}
.nav-item:hover{background:#F9FAFB;color:var(--text)}
.nav-item.active{background:#F3F4F6;color:var(--text);border-right:2px solid var(--text)}
.nav-icon{font-size:15px;width:20px;text-align:center}
/* Tab */
.tab-content{display:none}
.tab-content.active{display:block}
/* Page header */
.page-header{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:22px;gap:12px}
.page-title{font-size:20px;font-weight:700;color:var(--text)}
.page-sub{font-size:13px;color:var(--text2);margin-top:3px}
/* Buttons */
.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border-radius:var(--radius-sm);
     font-size:13px;font-weight:500;cursor:pointer;border:1px solid var(--border2);
     background:var(--surface);color:var(--text);transition:background .12s;white-space:nowrap}
.btn:hover{background:#F9FAFB}
.btn-primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.btn-primary:hover{background:#374151;border-color:#374151}
.btn-danger{border-color:#FCA5A5;color:#B91C1C;background:#FEF2F2}
.btn-danger:hover{background:#FEE2E2}
.btn-sm{padding:4px 10px;font-size:12px}
.btn-group{display:flex;gap:8px;flex-wrap:wrap}
/* Cards summary */
.summary-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:26px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
      padding:16px;box-shadow:var(--shadow)}
.card-top{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.avatar{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;
        justify-content:center;font-weight:700;font-size:12px;color:#fff;flex-shrink:0}
.card-name{font-size:13px;font-weight:600;color:var(--text);line-height:1.3}
.card-area{font-size:11px;color:var(--text2)}
.card-count{font-size:28px;font-weight:700;color:var(--text);line-height:1}
.card-label{font-size:11px;color:var(--text2);margin-top:2px}
.badge-overdue{display:inline-block;font-size:10px;font-weight:600;padding:2px 7px;
               border-radius:20px;background:#FEE2E2;color:#B91C1C;margin-top:5px}
/* Task rows */
.section-title{font-size:15px;font-weight:600;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.person-block{margin-bottom:26px}
.person-header{display:flex;align-items:center;gap:10px;margin-bottom:10px;
               padding-bottom:8px;border-bottom:2px solid}
.task-list{display:flex;flex-direction:column;gap:5px}
.task-row{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);
          padding:9px 14px;display:flex;align-items:center;gap:10px}
.task-row:hover{border-color:var(--border2)}
.task-name{flex:1;font-size:13px}
.task-due{font-size:12px;color:var(--text2);white-space:nowrap}
.task-due.overdue{color:#B91C1C;font-weight:600}
.task-link{font-size:11px;color:#6366F1;text-decoration:none}
.task-link:hover{text-decoration:underline}
.empty-msg{font-size:13px;color:var(--text3);padding:10px 0}
/* Checklist table */
.table-wrap{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
            overflow:hidden;box-shadow:var(--shadow)}
table{width:100%;border-collapse:collapse}
th{background:#F9FAFB;padding:10px 14px;text-align:left;font-size:11px;font-weight:600;
   color:var(--text2);text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid var(--border)}
td{padding:10px 14px;border-bottom:1px solid var(--border);font-size:13px;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:#FAFAFA}
.status-chip{display:inline-flex;align-items:center;gap:5px;font-size:12px;
             font-weight:500;padding:3px 10px;border-radius:20px}
.s-ok{background:#D1FAE5;color:#065F46}
.s-pending{background:#FEF3C7;color:#92400E}
.s-missing{background:#F3F4F6;color:#6B7280}
.s-paused{background:#E0E7FF;color:#3730A3}
/* Recurrentes manage */
.rec-actions{display:flex;gap:6px}
/* Equipo */
.team-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.member-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
             padding:14px 16px;display:flex;align-items:center;gap:14px;box-shadow:var(--shadow)}
.member-info{flex:1;min-width:0}
.member-name{font-size:14px;font-weight:600;color:var(--text)}
.member-meta{font-size:11px;color:var(--text3);margin-top:2px;font-family:monospace}
.badge-manager{font-size:10px;font-weight:600;background:#EDE9FE;color:#5B21B6;
               padding:2px 8px;border-radius:20px;margin-left:6px;vertical-align:middle}
/* Config form */
.config-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}
.config-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
             padding:18px;box-shadow:var(--shadow)}
.form-label{font-size:11px;font-weight:600;color:var(--text2);text-transform:uppercase;
            letter-spacing:.04em;display:block;margin-bottom:6px}
.form-input{width:100%;padding:8px 10px;border:1px solid var(--border2);border-radius:var(--radius-sm);
            font-size:14px;color:var(--text);outline:none;transition:border .12s}
.form-input:focus{border-color:#6366F1;box-shadow:0 0 0 3px rgba(99,102,241,.1)}
.form-row{display:flex;gap:8px}
.form-row .form-input{flex:1}
.info-box{background:#FFFBEB;border:1px solid #FDE68A;border-radius:var(--radius-sm);
          padding:12px 14px;font-size:13px;color:#92400E;display:flex;gap:8px;align-items:flex-start}
.success-box{background:#D1FAE5;border:1px solid #6EE7B7;border-radius:var(--radius-sm);
             padding:12px 14px;font-size:13px;color:#065F46}
/* Modal */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);
               z-index:100;align-items:center;justify-content:center}
.modal-overlay.open{display:flex}
.modal{background:var(--surface);border-radius:var(--radius);padding:24px;
       max-width:480px;width:90%;box-shadow:0 20px 60px rgba(0,0,0,.2)}
.modal-title{font-size:16px;font-weight:700;margin-bottom:16px}
.modal-body{display:flex;flex-direction:column;gap:14px}
.modal-footer{display:flex;gap:8px;justify-content:flex-end;margin-top:20px}
/* Select */
select.form-input{cursor:pointer}
/* Toast */
#toast{position:fixed;bottom:24px;right:24px;padding:12px 18px;border-radius:var(--radius-sm);
       font-size:13px;font-weight:500;box-shadow:0 4px 12px rgba(0,0,0,.15);
       transition:opacity .3s;opacity:0;z-index:200;pointer-events:none}
#toast.show{opacity:1}
#toast.ok{background:#111827;color:#fff}
#toast.err{background:#B91C1C;color:#fff}
/* Loader */
.loader{text-align:center;padding:36px;color:var(--text2)}
.spin{display:inline-block;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
/* Responsive */
@media(max-width:768px){
  .app{flex-direction:column}
  .sidebar{width:100%;height:auto;position:static;padding:10px 0;border-right:none;border-bottom:1px solid var(--border)}
  .sidebar nav{display:flex;overflow-x:auto;padding:0 12px;gap:4px}
  .nav-item{white-space:nowrap;border-radius:var(--radius-sm)}
  .main{padding:14px}
}
</style>
</head>
<body>
<div class="app">
<!-- SIDEBAR -->
<aside class="sidebar">
  <div class="logo">
    <div class="logo-title">🔧 Lubrikca</div>
    <div class="logo-sub">Panel de control v6.0</div>
  </div>
  <nav>
    <div class="nav-item active" onclick="tab('dashboard',this)"><span class="nav-icon">📊</span>Dashboard</div>
    <div class="nav-item" onclick="tab('checklist',this)"><span class="nav-icon">✅</span>Checklist</div>
    <div class="nav-item" onclick="tab('recurrentes',this)"><span class="nav-icon">🔁</span>Recurrentes</div>
    <div class="nav-item" onclick="tab('equipo',this)"><span class="nav-icon">👥</span>Equipo</div>
    <div class="nav-item" onclick="tab('areas',this)"><span class="nav-icon">🏢</span>Áreas</div>
    <div class="nav-item" onclick="tab('no-cumplidas',this)"><span class="nav-icon">📋</span>No Cumplidas</div>
    <div class="nav-item" onclick="tab('api-ia',this)"><span class="nav-icon">🤖</span>API / IA</div>
    <div class="nav-item" onclick="tab('permisos',this)"><span class="nav-icon">🔐</span>Permisos</div>
    <div class="nav-item" onclick="tab('config',this)"><span class="nav-icon">⚙️</span>Configuración</div>
  </nav>
</aside>

<!-- MAIN -->
<main class="main">

<!-- ═══ DASHBOARD ═══ -->
<div id="tab-dashboard" class="tab-content active">
  <div id="db-warning" style="display:none;background:#FEF3C7;border:1px solid #F59E0B;border-radius:8px;padding:10px 14px;margin-bottom:12px;font-size:13px;color:#92400E">
    ⚠️ <strong>Sin conexión a la base de datos (PostgreSQL).</strong> Los datos se guardan solo en memoria local y se perderán al reiniciar. Verifica que <code>DATABASE_URL</code> esté configurada en el servicio Web de Railway.
  </div>
  <div class="page-header">
    <div><div class="page-title">Dashboard</div>
    <div class="page-sub" id="dash-ts">Cargando desde Asana...</div></div>
    <div class="btn-group">
      <button class="btn btn-primary" onclick="openNewTaskModal()">➕ Nueva tarea</button>
      <button class="btn" onclick="loadDashboard()">↻ Actualizar</button>
    </div>
  </div>
  <div id="dash-cards" class="summary-grid"><div class="loader"><span class="spin">⟳</span></div></div>
  <div class="section-title" id="dash-tasks-title">📋 Tareas por área</div>
  <div id="dash-tasks"><div class="loader"><span class="spin">⟳</span></div></div>
</div>

<!-- ═══ CHECKLIST ═══ -->
<div id="tab-checklist" class="tab-content">
  <div class="page-header">
    <div><div class="page-title">Checklist semanal</div>
    <div class="page-sub">¿Se cumplieron las tareas recurrentes esta semana?</div></div>
    <button class="btn" onclick="loadChecklist()">↻ Actualizar</button>
  </div>
  <div id="checklist-body"><div class="loader"><span class="spin">⟳</span></div></div>
</div>

<!-- ═══ RECURRENTES ═══ -->
<div id="tab-recurrentes" class="tab-content">
  <div class="page-header">
    <div><div class="page-title">Tareas Recurrentes</div>
    <div class="page-sub">Gestiona, pausa o elimina tareas recurrentes del bot.</div></div>
    <button class="btn btn-primary" onclick="openAddRecModal()">+ Agregar</button>
  </div>
  <div id="rec-body"><div class="loader"><span class="spin">⟳</span></div></div>
</div>

<!-- ═══ EQUIPO ═══ -->
<div id="tab-equipo" class="tab-content">
  <div class="page-header">
    <div><div class="page-title">Equipo</div>
    <div class="page-sub">Gestiona los miembros del equipo.</div></div>
    <div class="btn-group">
      <button class="btn" onclick="loadTeam()">↻ Actualizar</button>
      <button class="btn btn-primary" onclick="openAddMemberModal()">+ Agregar miembro</button>
    </div>
  </div>
  <div id="team-body" class="team-grid"><div class="loader"><span class="spin">⟳</span></div></div>
</div>

<!-- ═══ ÁREAS ═══ -->
<div id="tab-areas" class="tab-content">
  <div class="page-header">
    <div><div class="page-title">Áreas de Trabajo</div>
    <div class="page-sub">Equipos con líder y miembros para delegación de tareas.</div></div>
    <div class="btn-group">
      <button class="btn" onclick="loadAreas()">↻ Actualizar</button>
      <button class="btn btn-primary" onclick="openAddAreaModal()">+ Nueva área</button>
    </div>
  </div>
  <div id="areas-body"><p class="empty-msg">Haz clic en ↻ Actualizar o selecciona esta pestaña para cargar las áreas.</p></div>
</div>

<!-- ═══ NO CUMPLIDAS ═══ -->
<div id="tab-no-cumplidas" class="tab-content">
  <div class="page-header">
    <div><div class="page-title">Tareas No Cumplidas</div>
    <div class="page-sub">Tareas vencidas más de 72 h sin completar.</div></div>
    <button class="btn" onclick="loadNoncompliant()">↻ Actualizar</button>
  </div>
  <div id="nc-body"><div class="loader"><span class="spin">⟳</span></div></div>
</div>

<!-- ═══ API / IA ═══ -->
<div id="tab-api-ia" class="tab-content">
  <div class="page-header">
    <div><div class="page-title">API / Inteligencia Artificial</div>
    <div class="page-sub">Configura el modelo Gemini para procesamiento de minutas.</div></div>
  </div>
  <div id="ai-body"><div class="loader"><span class="spin">⟳</span></div></div>
</div>

<!-- ═══ PERMISOS ═══ -->
<div id="tab-permisos" class="tab-content">
  <div class="page-header">
    <div><div class="page-title">Permisos</div>
    <div class="page-sub">Configura qué pueden hacer los líderes y miembros de área.</div></div>
    <button class="btn btn-primary" onclick="savePermisos()">💾 Guardar permisos</button>
  </div>
  <div id="permisos-body"><div class="loader"><span class="spin">⟳</span></div></div>
</div>

<!-- ═══ CONFIGURACIÓN ═══ -->
<div id="tab-config" class="tab-content">
  <div class="page-header">
    <div><div class="page-title">Configuración</div>
    <div class="page-sub">Ajusta los horarios del bot. Los cambios aplican en el próximo reinicio.</div></div>
    <div class="btn-group">
      <button class="btn btn-danger btn-sm" onclick="resetConfig()">Restaurar env vars</button>
      <button class="btn btn-primary" onclick="saveConfig()">Guardar cambios</button>
    </div>
  </div>
  <div id="cfg-alert" style="margin-bottom:16px"></div>
  <form id="cfg-form" onsubmit="return false">
    <div class="config-grid" id="cfg-grid">
      <div class="loader"><span class="spin">⟳</span></div>
    </div>
  </form>
  <div class="info-box" style="margin-top:20px">
    <span>ℹ️</span>
    <div>Los cambios se guardan en <code>dashboard_config.json</code> y sobreescriben las variables de entorno de Railway al próximo inicio del bot. Para revertir a las env vars originales usa "Restaurar env vars".</div>
  </div>

  <!-- Webhooks -->
  <div style="margin-top:28px">
    <div style="font-size:15px;font-weight:700;margin-bottom:4px">⚡ Notificaciones en tiempo real (Webhook Asana)</div>
    <div style="font-size:12px;color:var(--text2);margin-bottom:14px">
      El webhook elimina el polling de 5 min y notifica al instante cuando alguien asigna una tarea.
      No notifica si la persona se asignó la tarea a sí misma.
      Requiere <code>TELEGRAM_TOKEN</code> en las variables del servicio <strong>web</strong>.
    </div>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
      <input class="form-input" id="wh-url" placeholder="https://tu-app.railway.app/api/webhooks/asana" style="flex:1;min-width:260px">
      <button class="btn btn-primary" onclick="registerWebhook()">Registrar webhook</button>
      <button class="btn" onclick="loadWebhooks()">↻ Ver activos</button>
    </div>
    <div id="wh-status" style="font-size:12px;color:var(--text2)">Haz clic en "Ver activos" para listar los webhooks registrados.</div>
    <div id="wh-list" style="margin-top:10px"></div>
  </div>
</div>

</main>
</div>

<!-- ═══ MODAL: Agregar recurrente ═══ -->
<div class="modal-overlay" id="add-rec-modal">
  <div class="modal">
    <div class="modal-title">🔁 Nueva tarea recurrente</div>
    <div class="modal-body">
      <div>
        <label class="form-label">Nombre de la tarea</label>
        <input class="form-input" id="rec-name" placeholder="Ej: Reporte semanal de ventas">
      </div>
      <div>
        <label class="form-label">Responsable</label>
        <select class="form-input" id="rec-assignee"></select>
      </div>
      <div>
        <label class="form-label">Frecuencia</label>
        <select class="form-input" id="rec-freq" onchange="updateFreqFields()">
          <option value="weekly">Semanal</option>
          <option value="intraday">Diaria</option>
        </select>
      </div>
      <div id="rec-weekly-field">
        <label class="form-label">Día de la semana</label>
        <select class="form-input" id="rec-weekday">
          <option value="0">Lunes</option><option value="1">Martes</option>
          <option value="2">Miércoles</option><option value="3">Jueves</option>
          <option value="4">Viernes</option>
        </select>
      </div>
      <div id="rec-daily-field" style="display:none">
        <label class="form-label">Hora de recordatorio</label>
        <input class="form-input" id="rec-hour" type="number" min="0" max="23" value="9" placeholder="9">
      </div>
      <div>
        <label class="form-label">Descripción (opcional)</label>
        <textarea class="form-input" id="rec-notes" rows="3" placeholder="Contexto adicional para ejecutar la tarea..." style="resize:vertical"></textarea>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('add-rec-modal')">Cancelar</button>
      <button class="btn btn-primary" onclick="submitAddRec()">Agregar</button>
    </div>
  </div>
</div>

<!-- ═══ MODAL: Confirmar eliminar ═══ -->
<div class="modal-overlay" id="del-modal">
  <div class="modal">
    <div class="modal-title">⚠️ Confirmar eliminación</div>
    <div class="modal-body">
      <p id="del-msg" style="font-size:14px;color:var(--text2)"></p>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('del-modal')">Cancelar</button>
      <button class="btn btn-danger" id="del-confirm-btn">Eliminar</button>
    </div>
  </div>
</div>

<!-- ═══ MODAL: Editar recurrente ═══ -->
<div class="modal-overlay" id="edit-rec-modal">
  <div class="modal">
    <div class="modal-title">✏️ Editar tarea recurrente</div>
    <div class="modal-body">
      <input type="hidden" id="edit-rec-idx">
      <div>
        <label class="form-label">Nombre de la tarea</label>
        <input class="form-input" id="edit-rec-name" placeholder="Nombre de la tarea">
      </div>
      <div>
        <label class="form-label">Responsable</label>
        <select class="form-input" id="edit-rec-assignee"></select>
      </div>
      <div>
        <label class="form-label">Frecuencia</label>
        <select class="form-input" id="edit-rec-freq" onchange="updateEditFreqFields()">
          <option value="weekly">Semanal</option>
          <option value="intraday">Diaria</option>
        </select>
      </div>
      <div id="edit-rec-weekly-field">
        <label class="form-label">Día de la semana</label>
        <select class="form-input" id="edit-rec-weekday">
          <option value="0">Lunes</option><option value="1">Martes</option>
          <option value="2">Miércoles</option><option value="3">Jueves</option>
          <option value="4">Viernes</option>
        </select>
      </div>
      <div id="edit-rec-daily-field" style="display:none">
        <label class="form-label">Hora de recordatorio</label>
        <input class="form-input" id="edit-rec-hour" type="number" min="0" max="23" value="9" placeholder="9">
      </div>
      <div>
        <label class="form-label">Descripción (opcional)</label>
        <textarea class="form-input" id="edit-rec-notes" rows="3" placeholder="Contexto adicional para ejecutar la tarea..." style="resize:vertical"></textarea>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('edit-rec-modal')">Cancelar</button>
      <button class="btn btn-primary" onclick="submitEditRec()">Guardar cambios</button>
    </div>
  </div>
</div>

<!-- ═══ MODAL: Nueva tarea desde dashboard ═══ -->
<div class="modal-overlay" id="new-task-modal">
  <div class="modal">
    <div class="modal-title">➕ Nueva tarea</div>
    <div class="modal-body">
      <div>
        <label class="form-label">Nombre de la tarea *</label>
        <input class="form-input" id="nt-name" placeholder="Ej: Llamar al cliente García">
      </div>
      <div>
        <label class="form-label">Responsable *</label>
        <select class="form-input" id="nt-assignee"></select>
      </div>
      <div>
        <label class="form-label">Fecha límite</label>
        <input class="form-input" id="nt-due" type="date">
      </div>
      <div>
        <label class="form-label">Descripción (opcional)</label>
        <textarea class="form-input" id="nt-notes" rows="3" placeholder="Contexto, instrucciones..." style="resize:vertical"></textarea>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('new-task-modal')">Cancelar</button>
      <button class="btn btn-primary" onclick="submitNewTask()">Crear tarea</button>
    </div>
  </div>
</div>

<!-- ═══ MODAL: Nueva área ═══ -->
<div class="modal-overlay" id="add-area-modal">
  <div class="modal">
    <div class="modal-title">🏢 Nueva área de trabajo</div>
    <div class="modal-body">
      <div>
        <label class="form-label">Nombre del área</label>
        <input class="form-input" id="area-name" placeholder="Ej: Administración, Ventas, Logística">
      </div>
      <div>
        <label class="form-label">Líder del área</label>
        <select class="form-input" id="area-leader"></select>
      </div>
      <div class="info-box">
        <span>ℹ️</span>
        <div>El líder recibe las tareas del área y puede delegarlas a los miembros desde Telegram.</div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('add-area-modal')">Cancelar</button>
      <button class="btn btn-primary" onclick="submitAddArea()">Crear área</button>
    </div>
  </div>
</div>

<!-- ═══ MODAL: Agregar miembro al área ═══ -->
<div class="modal-overlay" id="add-area-member-modal">
  <div class="modal">
    <div class="modal-title">👤 Agregar miembro al área</div>
    <div class="modal-body">
      <input type="hidden" id="area-member-slug">
      <div>
        <label class="form-label">Seleccionar miembro</label>
        <select class="form-input" id="area-member-select"></select>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('add-area-member-modal')">Cancelar</button>
      <button class="btn btn-primary" onclick="submitAddAreaMember()">Agregar</button>
    </div>
  </div>
</div>

<!-- ═══ MODAL: Cambiar líder del área ═══ -->
<div class="modal-overlay" id="change-leader-modal">
  <div class="modal">
    <div class="modal-title">👑 Cambiar líder del área</div>
    <div class="modal-body">
      <input type="hidden" id="change-leader-slug">
      <div>
        <label class="form-label">Nuevo líder</label>
        <select class="form-input" id="change-leader-select"></select>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('change-leader-modal')">Cancelar</button>
      <button class="btn btn-primary" onclick="submitChangeLeader()">Guardar</button>
    </div>
  </div>
</div>

<!-- ═══ MODAL: Agregar miembro ═══ -->
<div class="modal-overlay" id="add-member-modal">
  <div class="modal">
    <div class="modal-title">👤 Agregar miembro al equipo</div>
    <div class="modal-body">
      <div>
        <label class="form-label">Telegram ID</label>
        <input class="form-input" id="mem-tg-id" type="number" placeholder="123456789">
      </div>
      <div>
        <label class="form-label">Asana GID</label>
        <input class="form-input" id="mem-asana-gid" placeholder="1234567890123456">
      </div>
      <div>
        <label class="form-label">Nombre completo (con área)</label>
        <input class="form-input" id="mem-name" placeholder="Ej: Juan Pérez (Ventas)">
      </div>
      <div class="info-box">
        <span>ℹ️</span>
        <div>El Asana GID se encuentra en la URL del perfil del usuario en Asana.</div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('add-member-modal')">Cancelar</button>
      <button class="btn btn-primary" onclick="submitAddMember()">Agregar</button>
    </div>
  </div>
</div>

<!-- Toast -->
<div id="toast"></div>

<script>
const TODAY = new Date().toISOString().slice(0,10);
let teamCache = [];

/* ── Toast ── */
function toast(msg, ok=true) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'show ' + (ok ? 'ok' : 'err');
  setTimeout(() => t.className = '', 2800);
}

/* ── Tab navigation ── */
function tab(name, el) {
  document.querySelectorAll('.tab-content').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(x => x.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  el.classList.add('active');
  const loaders = {
    dashboard:'loadDashboard', checklist:'loadChecklist',
    recurrentes:'loadRecurrentes', equipo:'loadTeam',
    'no-cumplidas':'loadNoncompliant', 'api-ia':'loadGemini',
    config:'loadConfig', permisos:'loadPermisos',
    areas:'loadAreas'
  };
  window[loaders[name]]?.();
}

/* ── Helpers ── */
function fmt(iso) {
  if (!iso) return '—';
  const [y,m,d] = iso.split('-');
  return `${d}/${m}/${y}`;
}
async function api(method, path, body) {
  const opts = { method, headers: {'Content-Type':'application/json'} };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}
function avt(initials, color, size=36, fs=12) {
  return `<div class="avatar" style="background:${color};width:${size}px;height:${size}px;font-size:${fs}px">${initials}</div>`;
}

/* ══════════ DASHBOARD ══════════ */
const STATUS_CYCLE  = ['pending','in_progress','review'];
const STATUS_LABELS = {pending:'⏳ Pendiente', in_progress:'🔄 En progreso', review:'👁 Revisión'};
const STATUS_COLORS = {pending:'#6B7280', in_progress:'#2563EB', review:'#D97706'};

async function cycleStatus(gid, btn) {
  const cur  = btn.dataset.status || 'pending';
  const idx  = STATUS_CYCLE.indexOf(cur);
  const next = STATUS_CYCLE[(idx + 1) % STATUS_CYCLE.length];
  btn.disabled = true;
  try {
    await api('PUT', `/api/tasks/${gid}/status`, {status: next});
    btn.dataset.status = next;
    btn.textContent    = STATUS_LABELS[next];
    btn.style.color    = STATUS_COLORS[next];
  } catch(e) { toast('Error: ' + e.message, false); }
  btn.disabled = false;
}

function _personBlock(p) {
  const rows = p.tasks.map(t => {
    const od  = t.due_on && t.due_on < TODAY;
    const st  = t.status || 'pending';
    return `<div class="task-row" id="tr-${t.gid}">
      <span class="task-name">${t.name}</span>
      <span class="task-due${od?' overdue':''}">${fmt(t.due_on)}</span>
      ${t.permalink_url?`<a class="task-link" href="${t.permalink_url}" target="_blank">↗</a>`:''}
      <button class="btn btn-sm" style="font-size:11px;color:${STATUS_COLORS[st]};min-width:90px" data-status="${st}" onclick="cycleStatus('${t.gid}',this)" title="Cambiar estado">${STATUS_LABELS[st]}</button>
      <button class="btn btn-sm btn-success" onclick="completarTarea('${t.gid}',this)" title="Marcar completada">✓</button>
      <button class="btn btn-sm btn-danger" onclick="eliminarTarea('${t.gid}','${t.name.replace(/'/g,'')}',this)" title="Eliminar">🗑</button>
    </div>`;
  }).join('');
  return `<div class="person-block">
    <div class="person-header" style="border-color:${p.color}25">
      ${avt(p.initials,p.color,28,11)}
      <span style="font-size:15px;font-weight:600">${p.name.split('(')[0].trim()}</span>
      <span style="margin-left:auto;font-size:12px;color:var(--text3)">${p.total} tarea${p.total!==1?'s':''}</span>
    </div>
    <div class="task-list">${rows||'<div class="empty-msg">✅ Sin tareas pendientes</div>'}</div>
  </div>`;
}

function _areaHeader(name, emoji, totalTasks) {
  return `<div style="display:flex;align-items:center;gap:8px;margin:20px 0 8px;padding-bottom:6px;border-bottom:2px solid var(--border)">
    <span style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--text2)">${emoji} ${name}</span>
    <span style="font-size:11px;color:var(--text3);margin-left:4px">${totalTasks} tarea${totalTasks!==1?'s':''}</span>
  </div>`;
}

async function loadDashboard() {
  document.getElementById('dash-cards').innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  document.getElementById('dash-tasks').innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  document.getElementById('dash-ts').textContent  = 'Actualizando...';
  try {
    // Check DB status (don't block dashboard load)
    api('GET', '/api/db-status').then(s => {
      const w = document.getElementById('db-warning');
      if (w) w.style.display = s.connected ? 'none' : 'block';
    }).catch(() => {});

    // Fetch data + areas in parallel
    const [data, areas] = await Promise.all([
      api('GET', '/api/summary'),
      api('GET', '/api/areas').catch(() => [])
    ]);

    // Build tg_id → area map (leader + members)
    const tgToArea = {};
    for (const a of areas) {
      tgToArea[String(a.leader_tg_id)] = a.name;
      for (const m of a.members) tgToArea[String(m.tg_id)] = a.name;
    }

    // Cards — keep sorted by overdue/total, show area name from map
    const cards = data.map(p => {
      const areaName = tgToArea[String(p.tg_id)] || (p.name.match(/\((.+)\)/) || ['',''])[1];
      return `<div class="card">
        <div class="card-top">${avt(p.initials,p.color)}
          <div><div class="card-name">${p.name.split('(')[0].trim()}</div>
          <div class="card-area">${areaName}</div></div>
        </div>
        <div class="card-count">${p.total}</div>
        <div class="card-label">tarea${p.total!==1?'s':''} pendiente${p.total!==1?'s':''}</div>
        ${p.overdue>0?`<div class="badge-overdue">⚠ ${p.overdue} vencida${p.overdue>1?'s':''}</div>`:''}
      </div>`;
    }).join('');
    document.getElementById('dash-cards').innerHTML = cards || '<p class="empty-msg">Sin datos</p>';

    // Task blocks — grouped by area
    let html = '';
    if (areas.length > 0) {
      const grouped = {};
      const ungrouped = [];
      for (const p of data) {
        const aName = tgToArea[String(p.tg_id)];
        if (aName) { (grouped[aName] = grouped[aName] || []).push(p); }
        else ungrouped.push(p);
      }
      for (const a of areas) {
        const people = grouped[a.name] || [];
        if (!people.length) continue;
        const tot = people.reduce((s,p) => s + p.total, 0);
        html += _areaHeader(a.name, '🏢', tot);
        html += people.map(_personBlock).join('');
      }
      if (ungrouped.length) {
        const tot = ungrouped.reduce((s,p) => s + p.total, 0);
        html += _areaHeader('Sin área asignada', '📋', tot);
        html += ungrouped.map(_personBlock).join('');
      }
    } else {
      // No areas defined — flat list
      html = data.map(_personBlock).join('');
      const tt = document.getElementById('dash-tasks-title');
      if (tt) tt.textContent = '📋 Tareas por persona';
    }

    document.getElementById('dash-tasks').innerHTML = html || '<p class="empty-msg">No hay datos.</p>';
    document.getElementById('dash-ts').textContent = 'Actualizado: ' + new Date().toLocaleTimeString('es',{hour:'2-digit',minute:'2-digit'});
  } catch(e) {
    document.getElementById('dash-cards').innerHTML = `<p style="color:#B91C1C">Error: ${e.message}</p>`;
    document.getElementById('dash-tasks').innerHTML = '';
  }
}

/* ══════════ CHECKLIST ══════════ */
async function loadChecklist() {
  document.getElementById('checklist-body').innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  try {
    const data = await api('GET','/api/recurring');
    if (!data.length) { document.getElementById('checklist-body').innerHTML='<p class="empty-msg">No hay tareas recurrentes.</p>'; return; }
    const rows = data.map(r => {
      const chips = {completed:'s-ok ✅ Completada',pending:'s-pending ⏳ Pendiente',
                     missing:'s-missing — Sin crear',paused:'s-paused ⏸ Pausada'};
      const [cls,...words] = (chips[r.status]||'s-missing — ?').split(' ');
      const warn = r.pending_count>1?`<span style="color:#B91C1C;font-size:11px;margin-left:6px">⚠ ${r.pending_count} acum.</span>`:'';
      return `<tr>
        <td><div style="display:flex;align-items:center;gap:8px">
          ${avt(r.initials,r.color,26,10)}
          <div><div style="font-weight:500">${r.task_name}</div>
          <div style="font-size:11px;color:var(--text2)">${r.assignee.split('(')[0].trim()}</div></div>
        </div></td>
        <td><span style="font-size:12px;color:var(--text2)">${r.freq_label}</span></td>
        <td><span class="status-chip ${cls}">${words.join(' ')}</span>${warn}</td>
        <td style="font-size:12px;color:var(--text3)">${fmt(r.last_created)||'—'}</td>
      </tr>`;
    }).join('');
    document.getElementById('checklist-body').innerHTML = `
      <div class="table-wrap"><table>
        <thead><tr><th>Tarea / Responsable</th><th>Frecuencia</th><th>Estado esta semana</th><th>Último ciclo</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`;
  } catch(e) { document.getElementById('checklist-body').innerHTML=`<p style="color:#B91C1C">Error: ${e.message}</p>`; }
}

/* ══════════ RECURRENTES (gestión) ══════════ */
async function loadRecurrentes() {
  _editRecCache = [];
  document.getElementById('rec-body').innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  try {
    const data = await api('GET','/api/recurring');
    _editRecCache = data;
    if (!data.length) { document.getElementById('rec-body').innerHTML='<p class="empty-msg">No hay tareas recurrentes.</p>'; return; }
    const rows = data.map(r => {
      const pauseLabel = r.paused ? '▶ Reanudar' : '⏸ Pausar';
      const rowStyle   = r.paused ? 'opacity:.55' : '';
      return `<tr style="${rowStyle}">
        <td><div style="display:flex;align-items:center;gap:8px">
          ${avt(r.initials,r.color,28,11)}
          <div><div style="font-weight:500">${r.task_name}</div>
          <div style="font-size:11px;color:var(--text2)">${r.assignee.split('(')[0].trim()}</div></div>
        </div></td>
        <td><span style="font-size:12px;color:var(--text2)">${r.freq_label}</span></td>
        <td>${r.paused
          ? '<span class="status-chip s-paused">⏸ Pausada</span>'
          : `<span class="status-chip ${r.pending_count>1?'s-pending':'s-ok'}">${r.pending_count} pendiente${r.pending_count!==1?'s':''}</span>`
        }</td>
        <td><div class="rec-actions">
          <button class="btn btn-sm" onclick="openEditRecModal(${r.idx})" title="Editar">✏️</button>
          <button class="btn btn-sm btn-warning" onclick="resetCount(${r.idx},'${r.task_name.replace(/'/g,"\\'")}',this)" title="Reiniciar contador">↺</button>
          <button class="btn btn-sm" onclick="toggleRec(${r.idx},'${r.task_name.replace(/'/g,"\\'")}',this)">${pauseLabel}</button>
          <button class="btn btn-sm btn-danger" onclick="confirmDelRec(${r.idx},'${r.task_name.replace(/'/g,"\\'")}')">✕</button>
        </div></td>
      </tr>`;
    }).join('');
    document.getElementById('rec-body').innerHTML = `
      <div class="table-wrap"><table>
        <thead><tr><th>Tarea / Responsable</th><th>Frecuencia</th><th>Estado</th><th>Acciones</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`;
  } catch(e) { document.getElementById('rec-body').innerHTML=`<p style="color:#B91C1C">Error: ${e.message}</p>`; }
}

async function toggleRec(idx, name, btn) {
  try {
    const r = await api('POST',`/api/recurring/${idx}/toggle`);
    toast(r.paused ? `⏸ "${name}" pausada` : `▶ "${name}" reanudada`);
    loadRecurrentes();
  } catch(e) { toast('Error: ' + e.message, false); }
}

function confirmDelRec(idx, name) {
  document.getElementById('del-msg').textContent = `¿Eliminar "${name}"? Esta acción no se puede deshacer.`;
  document.getElementById('del-confirm-btn').onclick = () => deleteRec(idx, name);
  document.getElementById('del-modal').classList.add('open');
}

async function deleteRec(idx, name) {
  closeModal('del-modal');
  try {
    await api('DELETE',`/api/recurring/${idx}`);
    toast(`🗑 "${name}" eliminada`);
    loadRecurrentes(); loadChecklist();
  } catch(e) { toast('Error: ' + e.message, false); }
}

/* ── Add recurring modal ── */
function openAddRecModal() {
  const sel = document.getElementById('rec-assignee');
  sel.innerHTML = teamCache.map(m =>
    `<option value="${m.tg_id}">${m.name.split('(')[0].trim()} — ${(m.name.match(/\((.+)\)/)||['',''])[1]}</option>`
  ).join('');
  document.getElementById('rec-name').value  = '';
  document.getElementById('rec-notes').value = '';
  updateFreqFields();
  document.getElementById('add-rec-modal').classList.add('open');
}

function updateFreqFields() {
  const freq = document.getElementById('rec-freq').value;
  document.getElementById('rec-weekly-field').style.display = freq==='weekly' ? '' : 'none';
  document.getElementById('rec-daily-field').style.display  = freq==='intraday' ? '' : 'none';
}

async function submitAddRec() {
  const name = document.getElementById('rec-name').value.trim();
  if (!name) { toast('Escribe el nombre de la tarea', false); return; }
  const tg_id = document.getElementById('rec-assignee').value;
  const freq  = document.getElementById('rec-freq').value;
  const notes = document.getElementById('rec-notes').value.trim();
  const body  = { task_name:name, assignee_tg_id:tg_id, freq };
  if (freq==='weekly')  body.weekday = parseInt(document.getElementById('rec-weekday').value);
  if (freq==='intraday') body.hours  = [parseInt(document.getElementById('rec-hour').value)||9];
  if (notes) body.notes = notes;
  try {
    await api('POST','/api/recurring/add', body);
    toast(`✅ "${name}" agregada`);
    closeModal('add-rec-modal');
    loadRecurrentes(); loadChecklist();
  } catch(e) { toast('Error: ' + e.message, false); }
}

/* ── Completar tarea desde dashboard ── */
async function completarTarea(gid, btn) {
  btn.disabled = true; btn.textContent = '...';
  try {
    await api('POST', `/api/tasks/${gid}/complete`);
    const row = document.getElementById('tr-' + gid);
    if (row) { row.style.opacity='.4'; row.style.textDecoration='line-through'; }
    toast('✅ Tarea marcada como completada');
  } catch(e) {
    toast('Error: ' + e.message, false);
    btn.disabled = false; btn.textContent = '✓';
  }
}

/* ── Eliminar tarea desde dashboard (solo manager) ── */
async function eliminarTarea(gid, name, btn) {
  if (!confirm(`¿Eliminar la tarea "${name}"? Esta acción no se puede deshacer.`)) return;
  btn.disabled = true; btn.textContent = '...';
  try {
    await api('DELETE', `/api/tasks/${gid}`);
    const row = document.getElementById('tr-' + gid);
    if (row) row.remove();
    toast('🗑 Tarea eliminada');
  } catch(e) {
    toast('Error: ' + e.message, false);
    btn.disabled = false; btn.textContent = '🗑';
  }
}

/* ── Reiniciar contador de recurrente ── */
async function resetCount(idx, name, btn) {
  btn.disabled = true;
  try {
    await api('POST', `/api/recurring/${idx}/reset`);
    toast(`↺ Contador de "${name}" reiniciado`);
    loadRecurrentes();
  } catch(e) {
    toast('Error: ' + e.message, false);
    btn.disabled = false;
  }
}

/* ── Edit recurring modal ── */
let _editRecCache = [];
async function openEditRecModal(idx) {
  // Reuse cached data or reload
  let data = _editRecCache;
  if (!data.length) {
    try { data = await api('GET','/api/recurring'); _editRecCache = data; } catch(e) { toast('Error: '+e.message,false); return; }
  }
  const r = data.find(x => x.idx === idx);
  if (!r) return;

  document.getElementById('edit-rec-idx').value    = idx;
  document.getElementById('edit-rec-name').value   = r.task_name;
  document.getElementById('edit-rec-notes').value  = r.notes || '';

  // Populate assignee select
  const sel = document.getElementById('edit-rec-assignee');
  sel.innerHTML = teamCache.map(m =>
    `<option value="${m.tg_id}">${m.name.split('(')[0].trim()} — ${(m.name.match(/\((.+)\)/)||['',''])[1]}</option>`
  ).join('');

  // Detect freq from freq_label (intraday vs weekly)
  const isIntraday = r.freq_label && r.freq_label.startsWith('Diario');
  document.getElementById('edit-rec-freq').value = isIntraday ? 'intraday' : 'weekly';
  updateEditFreqFields();

  if (!isIntraday) {
    // Find weekday index from day_label
    const days = ['Lun','Mar','Mié','Jue','Vie','Sáb','Dom'];
    const wd = days.indexOf(r.day_label);
    document.getElementById('edit-rec-weekday').value = wd >= 0 ? wd : 0;
  } else {
    // Extract hour from freq_label like "Diario (9:00, 15:00)"
    const m = r.freq_label.match(/(\d+):/);
    document.getElementById('edit-rec-hour').value = m ? m[1] : '9';
  }

  document.getElementById('edit-rec-modal').classList.add('open');
}

function updateEditFreqFields() {
  const freq = document.getElementById('edit-rec-freq').value;
  document.getElementById('edit-rec-weekly-field').style.display = freq==='weekly'   ? '' : 'none';
  document.getElementById('edit-rec-daily-field').style.display  = freq==='intraday' ? '' : 'none';
}

async function submitEditRec() {
  const idx  = parseInt(document.getElementById('edit-rec-idx').value);
  const name = document.getElementById('edit-rec-name').value.trim();
  const tg_id = document.getElementById('edit-rec-assignee').value;
  const freq  = document.getElementById('edit-rec-freq').value;
  if (!name) { toast('El nombre no puede estar vacío', false); return; }
  const editNotes = document.getElementById('edit-rec-notes').value.trim();
  const body = { task_name: name, assignee_tg_id: parseInt(tg_id), freq, notes: editNotes };
  if (freq === 'weekly')   body.weekday = parseInt(document.getElementById('edit-rec-weekday').value);
  if (freq === 'intraday') body.hours   = [parseInt(document.getElementById('edit-rec-hour').value) || 9];
  try {
    await api('PUT', `/api/recurring/${idx}`, body);
    toast('✅ Tarea recurrente actualizada');
    closeModal('edit-rec-modal');
    _editRecCache = [];
    loadRecurrentes(); loadChecklist();
  } catch(e) { toast('Error: ' + e.message, false); }
}

/* ══════════ EQUIPO ══════════ */
async function loadTeam() {
  document.getElementById('team-body').innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  try {
    const data = await api('GET','/api/team');
    teamCache = data;
    const cards = data.map(m => {
      const area = (m.name.match(/\((.+)\)/) || ['',''])[1];
      const disableBtn = m.is_manager ? 'disabled style="opacity:.4;cursor:not-allowed"' : `onclick="removeMember(${m.tg_id},'${m.name.replace(/'/g,"\\'")}',this)"`;
      return `<div class="member-card">
        ${avt(m.initials,m.color)}
        <div class="member-info">
          <div class="member-name">${m.name.split('(')[0].trim()}
            ${m.is_manager?'<span class="badge-manager">Manager</span>':''}
          </div>
          <div class="card-area" style="margin-bottom:4px">${area}</div>
          <div class="member-meta">TG: ${m.tg_id}</div>
        </div>
        <button class="btn btn-sm btn-danger" ${disableBtn}>Desactivar</button>
      </div>`;
    }).join('');
    document.getElementById('team-body').innerHTML = cards || '<p class="empty-msg">Sin miembros.</p>';
    document.getElementById('team-body').className = 'team-grid';
  } catch(e) { document.getElementById('team-body').innerHTML=`<p style="color:#B91C1C">Error: ${e.message}</p>`; }
}

async function removeMember(tg_id, name, btn) {
  if (!confirm(`¿Desactivar a "${name.split('(')[0].trim()}"?\nSe comentará su línea en team.txt.`)) return;
  btn.disabled = true; btn.textContent = '...';
  try {
    await api('POST',`/api/team/remove/${tg_id}`);
    toast(`👤 ${name.split('(')[0].trim()} desactivado`);
    loadTeam();
  } catch(e) { toast('Error: ' + e.message, false); btn.disabled=false; btn.textContent='Desactivar'; }
}

/* ── Agregar miembro ── */
function openAddMemberModal() {
  document.getElementById('mem-tg-id').value     = '';
  document.getElementById('mem-asana-gid').value = '';
  document.getElementById('mem-name').value      = '';
  document.getElementById('add-member-modal').classList.add('open');
}

async function submitAddMember() {
  const tg_id     = parseInt(document.getElementById('mem-tg-id').value);
  const asana_gid = document.getElementById('mem-asana-gid').value.trim();
  const name      = document.getElementById('mem-name').value.trim();
  if (!tg_id || !asana_gid || !name) { toast('Todos los campos son obligatorios', false); return; }
  try {
    await api('POST', '/api/team/add', { tg_id, asana_gid, name });
    toast(`✅ ${name.split('(')[0].trim()} agregado al equipo`);
    closeModal('add-member-modal');
    loadTeam();
  } catch(e) { toast('Error: ' + e.message, false); }
}

/* ══════════ ÁREAS ══════════ */
async function loadAreas() {
  document.getElementById('areas-body').innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  try {
    const areas = await api('GET', '/api/areas');
    if (!areas.length) {
      document.getElementById('areas-body').innerHTML =
        '<p class="empty-msg">No hay áreas creadas. Crea la primera con "+ Nueva área".</p>';
      return;
    }
    const cards = areas.map(a => {
      const memberRows = a.members.map(m => `
        <div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)">
          ${avt(getInitials(m.name), get_color(m.name), 26, 10)}
          <span style="flex:1;font-size:13px">${m.name.split('(')[0].trim()}</span>
          <button class="btn btn-sm btn-danger" onclick="removeAreaMember('${a.slug}',${m.tg_id},'${m.name.split('(')[0].trim()}',this)">✕</button>
        </div>`).join('');
      return `
        <div class="card" style="margin-bottom:16px">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
            <div>
              <div style="font-size:16px;font-weight:700">🏢 ${a.name}</div>
              <div style="font-size:12px;color:var(--text2);margin-top:2px">
                👑 Líder: <strong>${a.leader_name.split('(')[0].trim()}</strong>
              </div>
            </div>
            <div class="btn-group">
              <button class="btn btn-sm" onclick="openChangeLeaderModal('${a.slug}','${a.leader_tg_id}')" title="Cambiar líder">👑</button>
              <button class="btn btn-sm btn-primary" onclick="openAddAreaMemberModal('${a.slug}')">+ Miembro</button>
              <button class="btn btn-sm" onclick="toggleAreaTasks('${a.slug}',this)" title="Ver tareas">📋</button>
              <button class="btn btn-sm btn-danger" onclick="confirmDeleteArea('${a.slug}','${a.name}')">✕</button>
            </div>
          </div>
          ${a.members.length
            ? `<div style="font-size:11px;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px">MIEMBROS (${a.members.length})</div>
               <div>${memberRows}</div>`
            : `<div class="empty-msg" style="font-size:12px">Sin miembros. Agrega colaboradores con "+ Miembro".</div>`
          }
          <div id="area-tasks-${a.slug}" style="display:none;margin-top:14px;border-top:1px solid var(--border);padding-top:12px">
            <div style="font-size:11px;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px">TAREAS DEL ÁREA</div>
            <div id="area-tasks-body-${a.slug}"><span style="color:var(--text2);font-size:13px">Cargando...</span></div>
          </div>
        </div>`;
    }).join('');
    document.getElementById('areas-body').innerHTML = `<div style="max-width:640px">${cards}</div>`;
  } catch(e) {
    document.getElementById('areas-body').innerHTML = `<p style="color:#B91C1C">Error: ${e.message}</p>`;
  }
}

function getInitials(name) {
  const words = name.split('(')[0].trim().split(' ');
  return words.slice(0,2).map(w=>w[0]?.toUpperCase()||'').join('');
}

async function toggleAreaTasks(slug, btn) {
  const panel = document.getElementById(`area-tasks-${slug}`);
  if (panel.style.display === 'none') {
    panel.style.display = 'block';
    btn.textContent = '📋✕';
    await loadAreaTasks(slug);
  } else {
    panel.style.display = 'none';
    btn.textContent = '📋';
  }
}

async function loadAreaTasks(slug) {
  const container = document.getElementById(`area-tasks-body-${slug}`);
  try {
    const tasks = await api('GET', `/api/areas/${slug}/tasks`);
    if (!tasks.length) {
      container.innerHTML = '<p style="font-size:13px;color:var(--text2)">Sin tareas registradas.</p>';
      return;
    }
    const pending   = tasks.filter(t => t.status === 'pending');
    const completed = tasks.filter(t => t.status === 'completed');
    const renderRow = t => {
      const icon     = t.status === 'completed' ? '✅' : '⏳';
      const dueStr   = t.due_on   ? ` <span style="color:var(--text2);font-size:11px">📅 ${t.due_on}</span>` : '';
      const whoStr   = t.assigned_to_name ? ` <span style="color:var(--text2);font-size:11px">👤 ${t.assigned_to_name.split('(')[0].trim()}</span>` : '';
      const crossStyle = t.status === 'completed' ? 'text-decoration:line-through;opacity:.6' : '';
      return `<div style="display:flex;align-items:flex-start;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)">
        <span style="margin-top:1px">${icon}</span>
        <div style="flex:1">
          <div style="font-size:13px;${crossStyle}">${t.task_name}</div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:2px">${whoStr}${dueStr}</div>
        </div>
      </div>`;
    };
    let html = '';
    if (pending.length) {
      html += `<div style="font-size:11px;color:var(--text2);font-weight:600;margin-bottom:4px">PENDIENTES (${pending.length})</div>`;
      html += pending.map(renderRow).join('');
    }
    if (completed.length) {
      html += `<div style="font-size:11px;color:var(--text2);font-weight:600;margin:10px 0 4px">COMPLETADAS (${completed.length})</div>`;
      html += completed.slice(0, 10).map(renderRow).join('');
      if (completed.length > 10) html += `<p style="font-size:12px;color:var(--text2)">… y ${completed.length - 10} más.</p>`;
    }
    container.innerHTML = html;
  } catch(e) {
    container.innerHTML = `<p style="color:#B91C1C;font-size:13px">Error: ${e.message}</p>`;
  }
}

function get_color(name) {
  const nl = name.toLowerCase();
  const map = {manager:'#D4537E',ventas:'#1D9E75','logística':'#D85A30','almacén':'#D85A30',
    admin:'#378ADD',cobranza:'#7F77DD',finanzas:'#EF9F27','atención':'#7F77DD'};
  for (const [k,v] of Object.entries(map)) if (nl.includes(k)) return v;
  return '#8B9BAB';
}

function openAddAreaModal() {
  document.getElementById('area-name').value = '';
  document.getElementById('area-leader').innerHTML = teamCache.map(m =>
    `<option value="${m.tg_id}">${m.name.split('(')[0].trim()} — ${(m.name.match(/\((.+)\)/)||['',''])[1]}</option>`
  ).join('');
  document.getElementById('add-area-modal').classList.add('open');
}

async function submitAddArea() {
  const name = document.getElementById('area-name').value.trim();
  const leader_tg_id = parseInt(document.getElementById('area-leader').value);
  if (!name) { toast('Escribe el nombre del área', false); return; }
  try {
    await api('POST', '/api/areas', { name, leader_tg_id });
    toast(`✅ Área "${name}" creada`);
    closeModal('add-area-modal');
    loadAreas();
  } catch(e) { toast('Error: ' + e.message, false); }
}

async function confirmDeleteArea(slug, name) {
  document.getElementById('del-msg').textContent = `¿Eliminar el área "${name}"? Esta acción no se puede deshacer.`;
  document.getElementById('del-confirm-btn').onclick = async () => {
    closeModal('del-modal');
    try {
      await api('DELETE', `/api/areas/${slug}`);
      toast(`🗑 Área "${name}" eliminada`);
      loadAreas();
    } catch(e) { toast('Error: ' + e.message, false); }
  };
  document.getElementById('del-modal').classList.add('open');
}

function openAddAreaMemberModal(slug) {
  document.getElementById('area-member-slug').value = slug;
  document.getElementById('area-member-select').innerHTML = teamCache.map(m =>
    `<option value="${m.tg_id}">${m.name.split('(')[0].trim()} — ${(m.name.match(/\((.+)\)/)||['',''])[1]}</option>`
  ).join('');
  document.getElementById('add-area-member-modal').classList.add('open');
}

async function submitAddAreaMember() {
  const slug  = document.getElementById('area-member-slug').value;
  const tg_id = parseInt(document.getElementById('area-member-select').value);
  try {
    await api('POST', `/api/areas/${slug}/members`, { tg_id });
    toast('✅ Miembro agregado al área');
    closeModal('add-area-member-modal');
    loadAreas();
  } catch(e) { toast('Error: ' + e.message, false); }
}

async function removeAreaMember(slug, tg_id, name, btn) {
  if (!confirm(`¿Quitar a "${name}" del área?`)) return;
  btn.disabled = true;
  try {
    await api('DELETE', `/api/areas/${slug}/members/${tg_id}`);
    toast(`👤 ${name} quitado del área`);
    loadAreas();
  } catch(e) { toast('Error: ' + e.message, false); btn.disabled = false; }
}

function openChangeLeaderModal(slug, currentLeaderTgId) {
  document.getElementById('change-leader-slug').value = slug;
  document.getElementById('change-leader-select').innerHTML = teamCache.map(m =>
    `<option value="${m.tg_id}" ${m.tg_id == currentLeaderTgId ? 'selected' : ''}>${m.name.split('(')[0].trim()} — ${(m.name.match(/\((.+)\)/)||['',''])[1]}</option>`
  ).join('');
  document.getElementById('change-leader-modal').classList.add('open');
}

async function submitChangeLeader() {
  const slug         = document.getElementById('change-leader-slug').value;
  const leader_tg_id = parseInt(document.getElementById('change-leader-select').value);
  try {
    await api('PUT', `/api/areas/${slug}/leader`, { leader_tg_id });
    toast('✅ Líder actualizado');
    closeModal('change-leader-modal');
    loadAreas();
  } catch(e) { toast('Error: ' + e.message, false); }
}

/* ══════════ NO CUMPLIDAS ══════════ */
async function loadNoncompliant() {
  document.getElementById('nc-body').innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  try {
    const data = await api('GET', '/api/noncompliant');
    if (!data.length) {
      document.getElementById('nc-body').innerHTML = '<p class="empty-msg">✅ Sin tareas no cumplidas registradas.</p>';
      return;
    }
    const rows = data.map(r => {
      const hoursLabel = r.hours_overdue >= 24
        ? `${Math.floor(r.hours_overdue/24)}d ${Math.round(r.hours_overdue%24)}h`
        : `${Math.round(r.hours_overdue)}h`;
      const rec = r.recurring_name ? `<div style="font-size:11px;color:var(--text2)">${r.recurring_name}</div>` : '';
      return `<tr>
        <td><div style="font-weight:500">${r.task_name}</div>${rec}</td>
        <td>${r.assignee_name.split('(')[0].trim()}</td>
        <td>${fmt(r.due_on)}</td>
        <td><span class="status-chip s-pending">⚠ ${hoursLabel} retraso</span></td>
        <td style="font-size:11px;color:var(--text3)">${r.registered_at ? r.registered_at.slice(0,16).replace('T',' ') : '—'}</td>
        <td><button class="btn btn-sm btn-danger" onclick="deleteNoncompliant('${r.task_gid}',this)">✕</button></td>
      </tr>`;
    }).join('');
    document.getElementById('nc-body').innerHTML = `
      <div class="table-wrap"><table>
        <thead><tr><th>Tarea</th><th>Responsable</th><th>Fecha límite</th><th>Retraso</th><th>Registrada</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`;
  } catch(e) { document.getElementById('nc-body').innerHTML = `<p style="color:#B91C1C">Error: ${e.message}</p>`; }
}

async function deleteNoncompliant(task_gid, btn) {
  if (!confirm('¿Eliminar este registro de no-cumplimiento?')) return;
  btn.disabled = true;
  try {
    await api('DELETE', `/api/noncompliant/${task_gid}`);
    toast('Registro eliminado');
    loadNoncompliant();
  } catch(e) { toast('Error: ' + e.message, false); btn.disabled = false; }
}

/* ══════════ API / IA ══════════ */
async function loadGemini() {
  document.getElementById('ai-body').innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  try {
    const d = await api('GET', '/api/gemini');
    const keyStatus = d.has_api_key
      ? '<span class="status-chip s-ok">✅ Configurada en Railway</span>'
      : '<span class="status-chip s-missing">❌ No configurada (GEMINI_API_KEY en Railway)</span>';
    const opts = d.available_models.map(m =>
      `<option value="${m}" ${m===d.active_model?'selected':''}>${m}</option>`
    ).join('');
    document.getElementById('ai-body').innerHTML = `
      <div class="config-grid">
        <div class="config-card" style="grid-column:span 2">
          <label class="form-label">Estado API Key</label>
          <div style="margin-top:6px">${keyStatus}</div>
          <div style="font-size:12px;color:var(--text2);margin-top:10px">
            La API key se configura como variable de entorno <code>GEMINI_API_KEY</code> en Railway.
          </div>
        </div>
        <div class="config-card" style="grid-column:span 2">
          <label class="form-label" for="gemini-model">Modelo activo</label>
          <select class="form-input" id="gemini-model" style="margin-top:6px">${opts}</select>
          <div style="font-size:11px;color:var(--text3);margin-top:6px">
            Modelo actual: <strong>${d.active_model}</strong>
          </div>
          <div style="margin-top:14px">
            <button class="btn btn-primary" onclick="saveGemini()">💾 Guardar modelo</button>
          </div>
        </div>
      </div>`;
  } catch(e) { document.getElementById('ai-body').innerHTML = `<p style="color:#B91C1C">Error: ${e.message}</p>`; }
}

async function saveGemini() {
  const model = document.getElementById('gemini-model').value;
  try {
    await api('POST', '/api/gemini', { model });
    toast(`✅ Modelo "${model}" guardado`);
    loadGemini();
  } catch(e) { toast('Error: ' + e.message, false); }
}

/* ══════════ CONFIGURACIÓN ══════════ */
async function loadConfig() {
  document.getElementById('cfg-grid').innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  document.getElementById('cfg-alert').innerHTML = '';
  try {
    const c = await api('GET','/api/config');
    if (c._has_overrides) {
      document.getElementById('cfg-alert').innerHTML =
        '<div class="success-box">✅ Hay overrides guardados desde el panel — se usan en lugar de las env vars de Railway.</div>';
    }
    document.getElementById('cfg-grid').innerHTML = `
      <div class="config-card">
        <label class="form-label" for="tz">Zona horaria</label>
        <input class="form-input" id="tz" name="TIMEZONE" value="${c.TIMEZONE}" placeholder="America/Caracas">
      </div>
      <div class="config-card">
        <label class="form-label">Recordatorio mañana</label>
        <div class="form-row">
          <input class="form-input" id="mh" name="MORNING_HOUR" type="number" min="0" max="23" value="${c.MORNING_HOUR}" placeholder="9">
          <input class="form-input" id="mm" name="MORNING_MIN"  type="number" min="0" max="59" value="${c.MORNING_MIN}"  placeholder="0">
        </div>
        <div style="font-size:11px;color:var(--text3);margin-top:4px">Hora : Minutos</div>
      </div>
      <div class="config-card">
        <label class="form-label">Recordatorio tarde</label>
        <div class="form-row">
          <input class="form-input" id="ah" name="AFTERNOON_HOUR" type="number" min="0" max="23" value="${c.AFTERNOON_HOUR}" placeholder="15">
          <input class="form-input" id="am" name="AFTERNOON_MIN"  type="number" min="0" max="59" value="${c.AFTERNOON_MIN}"  placeholder="0">
        </div>
        <div style="font-size:11px;color:var(--text3);margin-top:4px">Hora : Minutos</div>
      </div>
      <div class="config-card">
        <label class="form-label">Reporte diario al manager</label>
        <div class="form-row">
          <input class="form-input" id="rh" name="REPORT_HOUR" type="number" min="0" max="23" value="${c.REPORT_HOUR}" placeholder="18">
          <input class="form-input" id="rm" name="REPORT_MIN"  type="number" min="0" max="59" value="${c.REPORT_MIN}"  placeholder="0">
        </div>
        <div style="font-size:11px;color:var(--text3);margin-top:4px">Hora : Minutos</div>
      </div>
      <div class="config-card">
        <label class="form-label" for="ci">Intervalo revisión Asana</label>
        <input class="form-input" id="ci" name="CHECK_INTERVAL_MINUTES" type="number" min="1" max="60" value="${c.CHECK_INTERVAL_MINUTES}">
        <div style="font-size:11px;color:var(--text3);margin-top:4px">Minutos entre revisiones</div>
      </div>`;
  } catch(e) { document.getElementById('cfg-grid').innerHTML=`<p style="color:#B91C1C">Error: ${e.message}</p>`; }
}

async function saveConfig() {
  const names = ['TIMEZONE','MORNING_HOUR','MORNING_MIN','AFTERNOON_HOUR','AFTERNOON_MIN',
                 'REPORT_HOUR','REPORT_MIN','CHECK_INTERVAL_MINUTES'];
  const body = {};
  for (const n of names) {
    const el = document.querySelector(`[name="${n}"]`);
    if (el) body[n] = n==='TIMEZONE' ? el.value : Number(el.value);
  }
  try {
    await api('POST','/api/config', body);
    toast('✅ Configuración guardada — aplica en próximo reinicio');
    loadConfig();
  } catch(e) { toast('Error: ' + e.message, false); }
}

async function resetConfig() {
  if (!confirm('¿Eliminar los overrides del panel?\nEl bot usará las env vars de Railway.')) return;
  try {
    await api('DELETE','/api/config');
    toast('Configuración restaurada a env vars');
    loadConfig();
  } catch(e) { toast('Error: ' + e.message, false); }
}

/* ── Modal helpers ── */
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
document.querySelectorAll('.modal-overlay').forEach(o =>
  o.addEventListener('click', e => { if (e.target===o) o.classList.remove('open'); })
);

/* ── Init ── */
/* ══════════ NUEVA TAREA DESDE DASHBOARD ══════════ */
function openNewTaskModal() {
  document.getElementById('nt-name').value  = '';
  document.getElementById('nt-due').value   = '';
  document.getElementById('nt-notes').value = '';
  document.getElementById('nt-assignee').innerHTML = teamCache.map(m =>
    `<option value="${m.tg_id}">${m.name.split('(')[0].trim()} — ${(m.name.match(/\((.+)\)/)||['',''])[1]}</option>`
  ).join('');
  document.getElementById('new-task-modal').classList.add('open');
  setTimeout(() => document.getElementById('nt-name').focus(), 100);
}

async function submitNewTask() {
  const name        = document.getElementById('nt-name').value.trim();
  const assignee_id = parseInt(document.getElementById('nt-assignee').value);
  const due         = document.getElementById('nt-due').value;
  const notes       = document.getElementById('nt-notes').value.trim();
  if (!name) { toast('Escribe el nombre de la tarea', false); return; }
  const body = { name, assignee_tg_id: assignee_id };
  if (due)   body.due_on = due;
  if (notes) body.notes  = notes;
  try {
    const res = await api('POST', '/api/tasks', body);
    toast(`✅ Tarea "${res.name}" creada`);
    closeModal('new-task-modal');
    loadDashboard();
  } catch(e) { toast('Error: ' + e.message, false); }
}

/* ══════════ PERMISOS ══════════ */
const PERM_LABELS = {
  leader_can_create_tasks:       'Líderes pueden crear tareas para su equipo',
  leader_can_assign_to_team:     'Líderes pueden asignar tareas a todo el equipo',
  leader_receives_reports:       'Líderes reciben reportes de su área',
  leader_can_request_delegation: 'Líderes pueden solicitar tareas al manager',
  members_can_create_own_tasks:  'Miembros pueden crear sus propias tareas',
};

async function loadPermisos() {
  const body = document.getElementById('permisos-body');
  body.innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  try {
    const perms = await api('GET', '/api/permissions');
    body.innerHTML = Object.entries(PERM_LABELS).map(([key, label]) => `
      <div style="display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--border)">
        <label class="toggle-wrap" style="cursor:pointer;display:flex;align-items:center;gap:10px;flex:1">
          <input type="checkbox" id="perm-${key}" ${perms[key] ? 'checked' : ''} style="width:18px;height:18px;cursor:pointer">
          <span style="font-size:14px">${label}</span>
        </label>
      </div>`).join('');
  } catch(e) {
    body.innerHTML = `<p style="color:var(--danger)">Error cargando permisos: ${e.message}</p>`;
  }
}

async function savePermisos() {
  const keys = Object.keys(PERM_LABELS);
  const perms = {};
  keys.forEach(k => {
    const el = document.getElementById('perm-' + k);
    if (el) perms[k] = el.checked;
  });
  try {
    await api('POST', '/api/permissions', perms);
    toast('✅ Permisos guardados');
  } catch(e) {
    toast('Error: ' + e.message, false);
  }
}

/* ══════════ WEBHOOKS ══════════ */
async function registerWebhook() {
  let url = document.getElementById('wh-url').value.trim();
  if (!url) { toast('Escribe la URL del webhook', false); return; }
  // Auto-completar path si el usuario sólo pegó la URL base
  if (!url.includes('/api/webhooks/asana')) {
    url = url.replace(/\/$/, '') + '/api/webhooks/asana';
    document.getElementById('wh-url').value = url;
  }
  const st = document.getElementById('wh-status');
  st.textContent = 'Registrando...';
  try {
    const r = await api('POST', '/api/webhooks/register', { url });
    st.innerHTML = `✅ Webhook registrado — GID: <code>${r.webhook?.gid || '?'}</code>`;
    toast('✅ Webhook de Asana registrado');
    loadWebhooks();
  } catch(e) {
    st.textContent = '❌ Error: ' + e.message;
    toast('Error: ' + e.message, false);
  }
}

async function loadWebhooks() {
  const list = document.getElementById('wh-list');
  const st   = document.getElementById('wh-status');
  list.innerHTML = '<span style="color:var(--text2);font-size:12px">Cargando...</span>';
  try {
    const hooks = await api('GET', '/api/webhooks');
    if (!hooks.length) {
      list.innerHTML = '';
      st.textContent = 'No hay webhooks registrados.';
      return;
    }
    st.textContent = `${hooks.length} webhook(s) activo(s):`;
    list.innerHTML = hooks.map(h => `
      <div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px">
        <span style="flex:1;font-family:monospace;color:var(--text2)">${h.target || h.gid}</span>
        <span style="color:${h.active?'#1D9E75':'#B91C1C'}">${h.active?'✅ Activo':'❌ Inactivo'}</span>
        <button class="btn btn-sm btn-danger" onclick="deleteWebhook('${h.gid}',this)">✕</button>
      </div>`).join('');
  } catch(e) {
    list.innerHTML = `<p style="color:var(--danger);font-size:12px">Error: ${e.message}</p>`;
  }
}

async function deleteWebhook(gid, btn) {
  if (!confirm('¿Eliminar este webhook?')) return;
  btn.disabled = true;
  try {
    await api('DELETE', `/api/webhooks/${gid}`);
    toast('🗑 Webhook eliminado');
    loadWebhooks();
  } catch(e) { toast('Error: ' + e.message, false); btn.disabled = false; }
}

loadDashboard();
loadTeam();  // pre-carga para el modal de recurrentes
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def dashboard_home(_=Depends(check_auth)):
    return DASHBOARD_HTML

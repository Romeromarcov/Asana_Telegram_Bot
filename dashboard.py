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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz
from fastapi import FastAPI, HTTPException, Depends, Request
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
        "opt_fields": "name,due_on,permalink_url",
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
    team   = load_team()
    today  = datetime.now(TZ).strftime("%Y-%m-%d")
    result = []
    for tg_id, info in team.items():
        tasks   = await asana_get_tasks(info["asana_gid"])
        overdue = sum(1 for t in tasks if t.get("due_on") and t["due_on"] < today)
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

@app.get("/health")
async def health():
    return {"status": "ok"}

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
    <div class="nav-item" onclick="tab('config',this)"><span class="nav-icon">⚙️</span>Configuración</div>
  </nav>
</aside>

<!-- MAIN -->
<main class="main">

<!-- ═══ DASHBOARD ═══ -->
<div id="tab-dashboard" class="tab-content active">
  <div class="page-header">
    <div><div class="page-title">Dashboard</div>
    <div class="page-sub" id="dash-ts">Cargando desde Asana...</div></div>
    <button class="btn" onclick="loadDashboard()">↻ Actualizar</button>
  </div>
  <div id="dash-cards" class="summary-grid"><div class="loader"><span class="spin">⟳</span></div></div>
  <div class="section-title">📋 Tareas por persona</div>
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
    <div class="page-sub">Para agregar miembros usa ➕ en el menú de Telegram.</div></div>
    <button class="btn" onclick="loadTeam()">↻ Actualizar</button>
  </div>
  <div id="team-body" class="team-grid"><div class="loader"><span class="spin">⟳</span></div></div>
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
    recurrentes:'loadRecurrentes', equipo:'loadTeam', config:'loadConfig'
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
async function loadDashboard() {
  document.getElementById('dash-cards').innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  document.getElementById('dash-tasks').innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  document.getElementById('dash-ts').textContent  = 'Actualizando...';
  try {
    const data = await api('GET','/api/summary');
    // Cards
    const cards = data.map(p => {
      const area = (p.name.match(/\((.+)\)/) || ['',''])[1];
      return `<div class="card">
        <div class="card-top">${avt(p.initials,p.color)}
          <div><div class="card-name">${p.name.split('(')[0].trim()}</div>
          <div class="card-area">${area}</div></div>
        </div>
        <div class="card-count">${p.total}</div>
        <div class="card-label">tarea${p.total!==1?'s':''} pendiente${p.total!==1?'s':''}</div>
        ${p.overdue>0?`<div class="badge-overdue">⚠ ${p.overdue} vencida${p.overdue>1?'s':''}</div>`:''}
      </div>`;
    }).join('');
    document.getElementById('dash-cards').innerHTML = cards || '<p class="empty-msg">Sin datos</p>';
    // Task blocks
    const blocks = data.map(p => {
      const area = (p.name.match(/\((.+)\)/) || ['',''])[1];
      const rows = p.tasks.map(t => {
        const od = t.due_on && t.due_on < TODAY;
        return `<div class="task-row">
          <span class="task-name">${t.name}</span>
          <span class="task-due${od?' overdue':''}">${fmt(t.due_on)}</span>
          ${t.permalink_url?`<a class="task-link" href="${t.permalink_url}" target="_blank">↗</a>`:''}
        </div>`;
      }).join('');
      return `<div class="person-block">
        <div class="person-header" style="border-color:${p.color}25">
          ${avt(p.initials,p.color,28,11)}
          <span style="font-size:15px;font-weight:600">${p.name.split('(')[0].trim()}</span>
          ${area?`<span style="font-size:12px;color:var(--text2)">${area}</span>`:''}
          <span style="margin-left:auto;font-size:12px;color:var(--text3)">${p.total} tarea${p.total!==1?'s':''}</span>
        </div>
        <div class="task-list">${rows||'<div class="empty-msg">✅ Sin tareas pendientes</div>'}</div>
      </div>`;
    }).join('');
    document.getElementById('dash-tasks').innerHTML = blocks || '<p class="empty-msg">No hay datos.</p>';
    document.getElementById('dash-ts').textContent = 'Actualizado: ' + new Date().toLocaleTimeString('es',{hour:'2-digit',minute:'2-digit'});
  } catch(e) {
    document.getElementById('dash-cards').innerHTML = `<p style="color:#B91C1C">Error: ${e.message}</p>`;
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
  document.getElementById('rec-body').innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  try {
    const data = await api('GET','/api/recurring');
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
          <button class="btn btn-sm" onclick="toggleRec(${r.idx},'${r.task_name.replace(/'/g,"\\'")}',this)">${pauseLabel}</button>
          <button class="btn btn-sm btn-danger" onclick="confirmDelRec(${r.idx},'${r.task_name.replace(/'/g,"\\'")}')">✕ Eliminar</button>
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
  document.getElementById('rec-name').value = '';
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
  const body  = { task_name:name, assignee_tg_id:tg_id, freq };
  if (freq==='weekly')  body.weekday = parseInt(document.getElementById('rec-weekday').value);
  if (freq==='intraday') body.hours  = [parseInt(document.getElementById('rec-hour').value)||9];
  try {
    await api('POST','/api/recurring/add', body);
    toast(`✅ "${name}" agregada`);
    closeModal('add-rec-modal');
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
loadDashboard();
loadTeam();  // pre-carga para el modal de recurrentes
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def dashboard_home(_=Depends(check_auth)):
    return DASHBOARD_HTML

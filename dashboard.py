"""
Panel de control web — Bot Lubrikca v6.0
Ruta raíz /  →  HTML del dashboard
/api/*        →  Endpoints JSON consumidos por el frontend
"""

import os
import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytz
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent
ASANA_TOKEN     = os.environ.get("ASANA_TOKEN", "")
ASANA_WORKSPACE = os.environ.get("ASANA_WORKSPACE_ID", "")
TIMEZONE        = os.environ.get("TIMEZONE", "America/Caracas")
DASHBOARD_PASS  = os.environ.get("DASHBOARD_PASSWORD", "")
MANAGER_TG_ID   = int(os.environ.get("MANAGER_CHAT_ID", "0"))
TZ              = pytz.timezone(TIMEZONE)
ASANA_BASE      = "https://app.asana.com/api/1.0"

AREA_COLORS = {
    "Manager":    "#D4537E",
    "Ventas":     "#1D9E75",
    "Logística":  "#D85A30",
    "Almacén":    "#D85A30",
    "Admin":      "#378ADD",
    "Cobranza":   "#7F77DD",
    "Finanzas":   "#EF9F27",
    "default":    "#8B9BAB",
}

WEEKDAY_NAMES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

app = FastAPI(title="Lubrikca Dashboard", docs_url=None, redoc_url=None)

# ── Auth básica opcional ──────────────────────────────────────────────────────
security = HTTPBasic(auto_error=False)

def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    if not DASHBOARD_PASS:
        return True
    if not credentials:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        DASHBOARD_PASS.encode("utf-8"),
    )
    if not ok:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return True

# ── Helpers de datos locales ──────────────────────────────────────────────────
def load_team() -> dict:
    team = {}
    try:
        for line in (BASE_DIR / "team.txt").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3:
                continue
            tg_id = int(parts[0])
            team[tg_id] = {"asana_gid": parts[1], "name": parts[2]}
    except Exception:
        pass
    return team

def load_recurring() -> list:
    try:
        return json.loads((BASE_DIR / "recurring.json").read_text(encoding="utf-8"))
    except Exception:
        return []

def get_area_color(name: str) -> str:
    name_lower = name.lower()
    for key, color in AREA_COLORS.items():
        if key.lower() in name_lower:
            return color
    return AREA_COLORS["default"]

def get_initials(name: str) -> str:
    words = name.split("(")[0].strip().split()
    return "".join(w[0].upper() for w in words[:2])

# ── Asana API ─────────────────────────────────────────────────────────────────
async def asana_get_tasks(asana_gid: str) -> list:
    if not ASANA_TOKEN:
        return []
    headers = {"Authorization": f"Bearer {ASANA_TOKEN}"}
    params = {
        "assignee": asana_gid,
        "workspace": ASANA_WORKSPACE,
        "completed_since": "now",
        "opt_fields": "name,due_on,completed,permalink_url",
        "limit": 50,
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{ASANA_BASE}/tasks", headers=headers, params=params, timeout=10
            )
            r.raise_for_status()
            return r.json().get("data", [])
    except Exception:
        return []

async def asana_check_task_completed(task_gid: str) -> bool:
    if not ASANA_TOKEN or not task_gid:
        return False
    headers = {"Authorization": f"Bearer {ASANA_TOKEN}"}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{ASANA_BASE}/tasks/{task_gid}",
                headers=headers,
                params={"opt_fields": "completed"},
                timeout=8,
            )
            r.raise_for_status()
            return r.json().get("data", {}).get("completed", False)
    except Exception:
        return False

# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/summary")
async def api_summary(_auth=Depends(check_auth)):
    """Resumen de tareas pendientes por persona."""
    team = load_team()
    result = []
    for tg_id, info in team.items():
        tasks = await asana_get_tasks(info["asana_gid"])
        overdue = sum(
            1 for t in tasks
            if t.get("due_on") and t["due_on"] < datetime.now(TZ).strftime("%Y-%m-%d")
        )
        result.append({
            "tg_id":      tg_id,
            "name":       info["name"],
            "asana_gid":  info["asana_gid"],
            "initials":   get_initials(info["name"]),
            "color":      get_area_color(info["name"]),
            "total":      len(tasks),
            "overdue":    overdue,
            "is_manager": tg_id == MANAGER_TG_ID,
            "tasks":      tasks,
        })
    result.sort(key=lambda x: (-x["overdue"], -x["total"]))
    return result

@app.get("/api/recurring")
async def api_recurring(_auth=Depends(check_auth)):
    """Estado del checklist de tareas recurrentes."""
    data  = load_recurring()
    today = datetime.now(TZ).date()
    # Inicio de la semana actual (lunes)
    week_start = today - timedelta(days=today.weekday())

    result = []
    for r in data:
        task_gid  = r.get("last_task_gid", "")
        completed = await asana_check_task_completed(task_gid) if task_gid else False

        last_created_str = r.get("last_created", "")
        try:
            last_d = datetime.strptime(last_created_str, "%Y-%m-%d").date()
            this_week = last_d >= week_start
        except Exception:
            this_week = False

        freq = r.get("freq", "weekly")
        if freq == "intraday":
            freq_label = f"Diario ({', '.join(str(h)+':00' for h in r.get('hours', []))})"
            day_label  = "Diario"
        else:
            wd = r.get("weekday", 0)
            day_label  = WEEKDAY_NAMES[wd]
            freq_label = f"Semanal ({day_label})"

        result.append({
            "task_name":    r["task_name"],
            "assignee":     r["assignee_name"],
            "color":        get_area_color(r["assignee_name"]),
            "initials":     get_initials(r["assignee_name"]),
            "freq_label":   freq_label,
            "day_label":    day_label,
            "pending_count": r.get("pending_count", 0),
            "completed":    completed,
            "this_week":    this_week,
            "last_created": last_created_str,
            "task_gid":     task_gid,
            "status": (
                "completed" if completed
                else ("pending" if this_week else "not_created")
            ),
        })
    return result

@app.get("/api/team")
async def api_team(_auth=Depends(check_auth)):
    team = load_team()
    return [
        {
            "tg_id":      tg_id,
            "name":       info["name"],
            "asana_gid":  info["asana_gid"],
            "initials":   get_initials(info["name"]),
            "color":      get_area_color(info["name"]),
            "is_manager": tg_id == MANAGER_TG_ID,
        }
        for tg_id, info in team.items()
    ]

@app.get("/api/config")
async def api_config(_auth=Depends(check_auth)):
    cfg = {
        "TIMEZONE":             os.environ.get("TIMEZONE", "America/Caracas"),
        "MORNING_HOUR":         int(os.environ.get("MORNING_HOUR", "9")),
        "MORNING_MIN":          int(os.environ.get("MORNING_MIN", "0")),
        "AFTERNOON_HOUR":       int(os.environ.get("AFTERNOON_HOUR", "15")),
        "AFTERNOON_MIN":        int(os.environ.get("AFTERNOON_MIN", "0")),
        "REPORT_HOUR":          int(os.environ.get("REPORT_HOUR", "18")),
        "REPORT_MIN":           int(os.environ.get("REPORT_MIN", "0")),
        "CHECK_INTERVAL_MINUTES": int(os.environ.get("CHECK_INTERVAL_MINUTES", "5")),
    }
    return cfg

# ── HTML del dashboard ─────────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Lubrikca — Panel de Control</title>
<style>
  :root {
    --bg:        #F5F5F4;
    --surface:   #FFFFFF;
    --border:    #E5E7EB;
    --border2:   #D1D5DB;
    --text:      #111827;
    --text2:     #6B7280;
    --text3:     #9CA3AF;
    --radius:    10px;
    --radius-sm: 6px;
    --shadow:    0 1px 3px rgba(0,0,0,.08);
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: var(--bg); color: var(--text); font-size: 14px; }

  /* ── Layout ── */
  .app { display:flex; min-height:100vh; }
  .sidebar { width:220px; background:var(--surface); border-right:1px solid var(--border);
             padding:24px 0; flex-shrink:0; position:sticky; top:0; height:100vh; }
  .main { flex:1; padding:28px; max-width:1100px; }

  /* ── Sidebar ── */
  .logo { padding:0 20px 24px; border-bottom:1px solid var(--border); margin-bottom:16px; }
  .logo-title { font-size:16px; font-weight:600; color:var(--text); }
  .logo-sub   { font-size:12px; color:var(--text2); margin-top:2px; }
  .nav-item { display:flex; align-items:center; gap:10px; padding:9px 20px; cursor:pointer;
              border-radius:0; color:var(--text2); font-size:13px; font-weight:500;
              transition:background .15s, color .15s; }
  .nav-item:hover { background:#F9FAFB; color:var(--text); }
  .nav-item.active { background:#F3F4F6; color:var(--text); border-right:2px solid var(--text); }
  .nav-icon { font-size:16px; width:20px; text-align:center; }

  /* ── Cards de resumen ── */
  .summary-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(170px,1fr));
                  gap:12px; margin-bottom:28px; }
  .card { background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
          padding:16px; box-shadow:var(--shadow); }
  .card-top { display:flex; align-items:center; gap:10px; margin-bottom:12px; }
  .avatar { width:36px; height:36px; border-radius:50%; display:flex; align-items:center;
            justify-content:center; font-weight:600; font-size:13px; color:#fff; flex-shrink:0; }
  .card-name { font-size:13px; font-weight:500; color:var(--text); line-height:1.3; }
  .card-area { font-size:11px; color:var(--text2); }
  .card-count { font-size:28px; font-weight:600; color:var(--text); line-height:1; }
  .card-label { font-size:11px; color:var(--text2); margin-top:3px; }
  .badge-overdue { display:inline-block; font-size:10px; font-weight:600; padding:2px 7px;
                   border-radius:20px; background:#FEE2E2; color:#B91C1C; margin-top:6px; }

  /* ── Tabla de tareas ── */
  .section-title { font-size:16px; font-weight:600; color:var(--text); margin-bottom:14px;
                   display:flex; align-items:center; gap:8px; }
  .person-block { margin-bottom:28px; }
  .person-header { display:flex; align-items:center; gap:10px; margin-bottom:10px;
                   padding-bottom:8px; border-bottom:2px solid; }
  .task-list { display:flex; flex-direction:column; gap:6px; }
  .task-row { background:var(--surface); border:1px solid var(--border);
              border-radius:var(--radius-sm); padding:10px 14px;
              display:flex; align-items:center; gap:10px; }
  .task-row:hover { border-color:var(--border2); }
  .task-name { flex:1; font-size:13px; color:var(--text); }
  .task-due  { font-size:12px; color:var(--text2); white-space:nowrap; }
  .task-due.overdue { color:#B91C1C; font-weight:600; }
  .task-link { font-size:11px; color:#6366F1; text-decoration:none; }
  .task-link:hover { text-decoration:underline; }
  .empty-msg { font-size:13px; color:var(--text3); padding:12px 0; }

  /* ── Checklist ── */
  .checklist-table { width:100%; border-collapse:collapse; background:var(--surface);
                     border:1px solid var(--border); border-radius:var(--radius);
                     overflow:hidden; box-shadow:var(--shadow); }
  .checklist-table th { background:#F9FAFB; padding:10px 14px; text-align:left;
                         font-size:11px; font-weight:600; color:var(--text2);
                         text-transform:uppercase; letter-spacing:.04em;
                         border-bottom:1px solid var(--border); }
  .checklist-table td { padding:10px 14px; border-bottom:1px solid var(--border);
                         font-size:13px; vertical-align:middle; }
  .checklist-table tr:last-child td { border-bottom:none; }
  .status-chip { display:inline-flex; align-items:center; gap:5px; font-size:12px;
                 font-weight:500; padding:3px 10px; border-radius:20px; }
  .status-completed { background:#D1FAE5; color:#065F46; }
  .status-pending   { background:#FEF3C7; color:#92400E; }
  .status-missing   { background:#F3F4F6; color:#6B7280; }

  /* ── Equipo ── */
  .team-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:14px; }
  .member-card { background:var(--surface); border:1px solid var(--border);
                 border-radius:var(--radius); padding:16px; box-shadow:var(--shadow);
                 display:flex; align-items:center; gap:14px; }
  .member-info { flex:1; }
  .member-name { font-size:14px; font-weight:500; color:var(--text); }
  .member-id   { font-size:11px; color:var(--text3); margin-top:2px; font-family:monospace; }
  .badge-manager { font-size:10px; font-weight:600; background:#EDE9FE; color:#5B21B6;
                   padding:2px 8px; border-radius:20px; margin-left:6px; }

  /* ── Config ── */
  .config-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:16px; }
  .config-card { background:var(--surface); border:1px solid var(--border);
                 border-radius:var(--radius); padding:18px; box-shadow:var(--shadow); }
  .config-label { font-size:11px; font-weight:600; color:var(--text2);
                  text-transform:uppercase; letter-spacing:.04em; margin-bottom:8px; }
  .config-value { font-size:20px; font-weight:600; color:var(--text); }
  .config-sub   { font-size:11px; color:var(--text3); margin-top:3px; }
  .env-note { background:#FFFBEB; border:1px solid #FDE68A; border-radius:var(--radius-sm);
              padding:12px 16px; font-size:13px; color:#92400E; margin-top:20px; }

  /* ── Page header ── */
  .page-header { margin-bottom:24px; }
  .page-title  { font-size:22px; font-weight:600; color:var(--text); }
  .page-sub    { font-size:13px; color:var(--text2); margin-top:4px; }

  /* ── Tab content ── */
  .tab-content { display:none; }
  .tab-content.active { display:block; }

  /* ── Refresh btn ── */
  .btn { display:inline-flex; align-items:center; gap:6px; padding:7px 14px;
         border-radius:var(--radius-sm); font-size:13px; font-weight:500;
         cursor:pointer; border:1px solid var(--border2); background:var(--surface);
         color:var(--text); transition:background .15s; }
  .btn:hover { background:#F9FAFB; }
  .btn-primary { background:#111827; color:#fff; border-color:#111827; }
  .btn-primary:hover { background:#374151; border-color:#374151; }

  /* ── Loader ── */
  .loader { text-align:center; padding:40px; color:var(--text2); font-size:13px; }
  .spin { display:inline-block; animation:spin .8s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }

  /* ── Responsive ── */
  @media(max-width:768px) {
    .app { flex-direction:column; }
    .sidebar { width:100%; height:auto; position:static;
               padding:12px 0; border-right:none; border-bottom:1px solid var(--border); }
    .sidebar nav { display:flex; overflow-x:auto; padding:0 12px; gap:4px; }
    .nav-item { white-space:nowrap; border-radius:var(--radius-sm); }
    .main { padding:16px; }
  }
</style>
</head>
<body>
<div class="app">
  <!-- ── Sidebar ── -->
  <aside class="sidebar">
    <div class="logo">
      <div class="logo-title">🔧 Lubrikca</div>
      <div class="logo-sub">Panel de control</div>
    </div>
    <nav>
      <div class="nav-item active" data-tab="dashboard" onclick="switchTab('dashboard', this)">
        <span class="nav-icon">📊</span> Dashboard
      </div>
      <div class="nav-item" data-tab="checklist" onclick="switchTab('checklist', this)">
        <span class="nav-icon">✅</span> Checklist semanal
      </div>
      <div class="nav-item" data-tab="team" onclick="switchTab('team', this)">
        <span class="nav-icon">👥</span> Equipo
      </div>
      <div class="nav-item" data-tab="config" onclick="switchTab('config', this)">
        <span class="nav-icon">⚙️</span> Configuración
      </div>
    </nav>
  </aside>

  <!-- ── Main content ── -->
  <main class="main">

    <!-- ━━━ DASHBOARD ━━━ -->
    <div id="tab-dashboard" class="tab-content active">
      <div class="page-header" style="display:flex;align-items:flex-start;justify-content:space-between;">
        <div>
          <div class="page-title">Dashboard de tareas</div>
          <div class="page-sub" id="dash-updated">Cargando...</div>
        </div>
        <button class="btn" onclick="loadDashboard()">↻ Actualizar</button>
      </div>

      <div id="dash-summary" class="summary-grid">
        <div class="loader"><span class="spin">⟳</span> Cargando...</div>
      </div>

      <div class="section-title">📋 Tareas por persona</div>
      <div id="dash-tasks">
        <div class="loader"><span class="spin">⟳</span> Cargando tareas...</div>
      </div>
    </div>

    <!-- ━━━ CHECKLIST ━━━ -->
    <div id="tab-checklist" class="tab-content">
      <div class="page-header" style="display:flex;align-items:flex-start;justify-content:space-between;">
        <div>
          <div class="page-title">Checklist semanal</div>
          <div class="page-sub">¿Se cumplieron las tareas recurrentes esta semana?</div>
        </div>
        <button class="btn" onclick="loadChecklist()">↻ Actualizar</button>
      </div>
      <div id="checklist-body">
        <div class="loader"><span class="spin">⟳</span> Cargando...</div>
      </div>
    </div>

    <!-- ━━━ EQUIPO ━━━ -->
    <div id="tab-team" class="tab-content">
      <div class="page-header">
        <div class="page-title">Equipo registrado</div>
        <div class="page-sub">Miembros activos en el bot. Para agregar o desactivar, usa el comando desde Telegram.</div>
      </div>
      <div id="team-body" class="team-grid">
        <div class="loader"><span class="spin">⟳</span> Cargando...</div>
      </div>
    </div>

    <!-- ━━━ CONFIG ━━━ -->
    <div id="tab-config" class="tab-content">
      <div class="page-header">
        <div class="page-title">Configuración del bot</div>
        <div class="page-sub">Variables de entorno activas en Railway.</div>
      </div>
      <div id="config-body" class="config-grid">
        <div class="loader"><span class="spin">⟳</span> Cargando...</div>
      </div>
      <div class="env-note" style="margin-top:20px;">
        ⚠️ Para cambiar estos valores, actualiza las <strong>variables de entorno en Railway</strong>
        y reinicia el servicio. Los cambios se aplican al próximo reinicio del bot.
      </div>
    </div>

  </main>
</div>

<script>
const TODAY = new Date().toISOString().slice(0,10);

// ── Tab navigation ───────────────────────────────────────────────────────────
function switchTab(name, el) {
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  el.classList.add('active');
  if (name === 'dashboard')  loadDashboard();
  if (name === 'checklist')  loadChecklist();
  if (name === 'team')       loadTeam();
  if (name === 'config')     loadConfig();
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function fmt(iso) {
  if (!iso) return '—';
  const [y,m,d] = iso.split('-');
  return `${d}/${m}/${y}`;
}
function isOverdue(due_on) {
  return due_on && due_on < TODAY;
}
function initials(name) {
  return name.split('(')[0].trim().split(' ').slice(0,2).map(w=>w[0]).join('').toUpperCase();
}
async function apiFetch(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(r.statusText);
  return r.json();
}

// ── Dashboard ────────────────────────────────────────────────────────────────
async function loadDashboard() {
  document.getElementById('dash-summary').innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  document.getElementById('dash-tasks').innerHTML   = '<div class="loader"><span class="spin">⟳</span></div>';
  document.getElementById('dash-updated').textContent = 'Actualizando desde Asana...';

  try {
    const data = await apiFetch('/api/summary');

    // Cards de resumen
    const cards = data.map(p => `
      <div class="card">
        <div class="card-top">
          <div class="avatar" style="background:${p.color}">${p.initials}</div>
          <div>
            <div class="card-name">${p.name.split('(')[0].trim()}</div>
            <div class="card-area">${(p.name.match(/\\((.+)\\)/) || ['',''])[1] || (p.is_manager ? 'Gerencia' : '')}</div>
          </div>
        </div>
        <div class="card-count">${p.total}</div>
        <div class="card-label">tarea${p.total !== 1 ? 's' : ''} pendiente${p.total !== 1 ? 's' : ''}</div>
        ${p.overdue > 0 ? `<div class="badge-overdue">⚠ ${p.overdue} vencida${p.overdue>1?'s':''}</div>` : ''}
      </div>
    `).join('');
    document.getElementById('dash-summary').innerHTML = cards || '<p class="empty-msg">Sin datos</p>';

    // Bloques por persona
    const blocks = data.map(p => {
      const tasks = p.tasks.map(t => {
        const od = isOverdue(t.due_on);
        return `
          <div class="task-row">
            <span class="task-name">${t.name}</span>
            <span class="task-due ${od ? 'overdue' : ''}">${fmt(t.due_on)}</span>
            ${t.permalink_url ? `<a class="task-link" href="${t.permalink_url}" target="_blank">↗ Asana</a>` : ''}
          </div>`;
      }).join('');
      const areaMatch = p.name.match(/\\((.+)\\)/);
      const area = areaMatch ? areaMatch[1] : (p.is_manager ? 'Gerencia General' : '');
      return `
        <div class="person-block">
          <div class="person-header" style="border-color:${p.color}20;color:${p.color}">
            <div class="avatar" style="background:${p.color};width:28px;height:28px;font-size:11px">${p.initials}</div>
            <span style="font-size:15px;font-weight:600;">${p.name.split('(')[0].trim()}</span>
            ${area ? `<span style="font-size:12px;color:var(--text2);">${area}</span>` : ''}
            <span style="margin-left:auto;font-size:12px;color:var(--text3);">${p.total} tarea${p.total!==1?'s':''}</span>
          </div>
          <div class="task-list">
            ${tasks || '<div class="empty-msg">✅ Sin tareas pendientes</div>'}
          </div>
        </div>`;
    }).join('');
    document.getElementById('dash-tasks').innerHTML = blocks || '<p class="empty-msg">No hay datos.</p>';

    const now = new Date().toLocaleTimeString('es', {hour:'2-digit',minute:'2-digit'});
    document.getElementById('dash-updated').textContent = `Última actualización: ${now}`;
  } catch(e) {
    document.getElementById('dash-summary').innerHTML = `<p style="color:#B91C1C">Error: ${e.message}</p>`;
  }
}

// ── Checklist ────────────────────────────────────────────────────────────────
async function loadChecklist() {
  document.getElementById('checklist-body').innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  try {
    const data = await apiFetch('/api/recurring');
    if (!data.length) {
      document.getElementById('checklist-body').innerHTML = '<p class="empty-msg">No hay tareas recurrentes configuradas.</p>';
      return;
    }
    const rows = data.map(r => {
      let chip, icon;
      if (r.status === 'completed') {
        chip = 'status-completed'; icon = '✅ Completada';
      } else if (r.status === 'pending') {
        chip = 'status-pending';   icon = '⏳ Pendiente';
      } else {
        chip = 'status-missing';   icon = '— Sin crear';
      }
      const overdueWarn = r.pending_count > 1
        ? `<span style="color:#B91C1C;font-size:11px;"> ⚠ ${r.pending_count} acumuladas</span>` : '';
      return `
        <tr>
          <td>
            <div style="display:flex;align-items:center;gap:8px;">
              <div class="avatar" style="background:${r.color};width:26px;height:26px;font-size:10px;flex-shrink:0">${r.initials}</div>
              <div>
                <div style="font-weight:500">${r.task_name}</div>
                <div style="font-size:11px;color:var(--text2)">${r.assignee.split('(')[0].trim()}</div>
              </div>
            </div>
          </td>
          <td><span style="font-size:12px;color:var(--text2)">${r.freq_label}</span></td>
          <td><span class="status-chip ${chip}">${icon}</span>${overdueWarn}</td>
          <td style="font-size:12px;color:var(--text3)">${fmt(r.last_created) || '—'}</td>
        </tr>`;
    }).join('');
    document.getElementById('checklist-body').innerHTML = `
      <table class="checklist-table">
        <thead>
          <tr>
            <th>Tarea / Responsable</th>
            <th>Frecuencia</th>
            <th>Estado esta semana</th>
            <th>Último ciclo</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>`;
  } catch(e) {
    document.getElementById('checklist-body').innerHTML = `<p style="color:#B91C1C">Error: ${e.message}</p>`;
  }
}

// ── Equipo ────────────────────────────────────────────────────────────────────
async function loadTeam() {
  document.getElementById('team-body').innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  try {
    const data = await apiFetch('/api/team');
    const cards = data.map(m => `
      <div class="member-card">
        <div class="avatar" style="background:${m.color}">${m.initials}</div>
        <div class="member-info">
          <div class="member-name">
            ${m.name.split('(')[0].trim()}
            ${m.is_manager ? '<span class="badge-manager">Manager</span>' : ''}
          </div>
          <div class="member-id">TG: ${m.tg_id}</div>
          <div class="member-id">Asana: ${m.asana_gid}</div>
        </div>
      </div>`).join('');
    document.getElementById('team-body').innerHTML = cards || '<p class="empty-msg">Sin miembros.</p>';
    document.getElementById('team-body').className = 'team-grid';
  } catch(e) {
    document.getElementById('team-body').innerHTML = `<p style="color:#B91C1C">Error: ${e.message}</p>`;
  }
}

// ── Configuración ─────────────────────────────────────────────────────────────
async function loadConfig() {
  document.getElementById('config-body').innerHTML = '<div class="loader"><span class="spin">⟳</span></div>';
  try {
    const c = await apiFetch('/api/config');
    const items = [
      { label:'Zona horaria',        value: c.TIMEZONE,                sub:'Afecta todos los recordatorios' },
      { label:'Recordatorio mañana', value: `${c.MORNING_HOUR}:${String(c.MORNING_MIN).padStart(2,'0')}`, sub:'Hora del recordatorio AM' },
      { label:'Recordatorio tarde',  value: `${c.AFTERNOON_HOUR}:${String(c.AFTERNOON_MIN).padStart(2,'0')}`, sub:'Hora del recordatorio PM' },
      { label:'Reporte diario',      value: `${c.REPORT_HOUR}:${String(c.REPORT_MIN||0).padStart(2,'0')}`, sub:'Hora del reporte al manager' },
      { label:'Intervalo de revisión', value: `${c.CHECK_INTERVAL_MINUTES} min`, sub:'Con qué frecuencia revisa Asana' },
    ];
    const html = items.map(i => `
      <div class="config-card">
        <div class="config-label">${i.label}</div>
        <div class="config-value">${i.value}</div>
        <div class="config-sub">${i.sub}</div>
      </div>`).join('');
    document.getElementById('config-body').innerHTML = html;
  } catch(e) {
    document.getElementById('config-body').innerHTML = `<p style="color:#B91C1C">Error: ${e.message}</p>`;
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
loadDashboard();
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def dashboard_home(_auth=Depends(check_auth)):
    return DASHBOARD_HTML

@app.get("/health")
async def health():
    return {"status": "ok", "service": "lubrikca-dashboard"}

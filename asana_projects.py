"""
Módulo de proyectos personales en Asana — Bot Lubrikca v6.0

Cada colaborador tiene su propio proyecto de tablero Kanban:
  "Tareas - {nombre}"
  Secciones fijas: 📌 Pendiente | ⚙️ En ejecución | 🔍 En revisión | ✅ Completado | 🚫 Bloqueado

Este módulo gestiona la creación de esos proyectos y el movimiento de tareas
entre secciones (cambio de estado).
"""

import json
import logging
from pathlib import Path
import httpx

logger = logging.getLogger(__name__)

ASANA_BASE    = "https://app.asana.com/api/1.0"
PROJECTS_FILE = Path(__file__).parent / "projects.json"

STANDARD_SECTIONS = [
    "📌 Pendiente",
    "⚙️ En ejecución",
    "🔍 En revisión",
    "✅ Completado",
    "🚫 Bloqueado",
]

# ── PERSISTENCIA ──────────────────────────────────────────────────────────────

def load_projects() -> dict:
    """Carga el mapa asana_gid → config de proyecto desde projects.json."""
    if not PROJECTS_FILE.exists():
        return {}
    try:
        return json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_projects(data: dict):
    PROJECTS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

# ── API HELPERS ────────────────────────────────────────────────────────────────

async def _asana_post(path: str, data: dict, asana_token: str) -> dict:
    headers = {
        "Authorization": f"Bearer {asana_token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{ASANA_BASE}{path}",
            headers=headers,
            json={"data": data},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("data", {})

# ── CREACIÓN DE PROYECTO ───────────────────────────────────────────────────────

async def setup_member_project(
    asana_gid: str,
    display_name: str,
    workspace_gid: str,
    asana_token: str,
) -> dict:
    """
    Crea el proyecto tablero y las secciones estándar para un colaborador.
    Devuelve el dict de configuración listo para guardar.
    """
    first_name   = display_name.split()[0]
    project_name = f"Tareas - {first_name}"
    logger.info(f"Creando proyecto '{project_name}' para {display_name}...")

    proj = await _asana_post(
        "/projects",
        {"name": project_name, "workspace": workspace_gid, "layout": "board"},
        asana_token,
    )
    project_gid = proj["gid"]
    logger.info(f"  Proyecto: {project_gid}")

    sections = {}
    for sec_name in STANDARD_SECTIONS:
        sec = await _asana_post(
            f"/projects/{project_gid}/sections",
            {"name": sec_name},
            asana_token,
        )
        sections[sec_name] = sec["gid"]
        logger.info(f"  Sección '{sec_name}': {sec['gid']}")

    return {
        "asana_gid":   asana_gid,
        "name":        display_name,
        "project_gid": project_gid,
        "sections":    sections,
    }

async def ensure_member_project(
    asana_gid: str,
    display_name: str,
    workspace_gid: str,
    asana_token: str,
) -> dict:
    """
    Devuelve la config del proyecto del colaborador.
    Si no existe aún, lo crea automáticamente.
    """
    projects = load_projects()
    if asana_gid in projects:
        return projects[asana_gid]

    config = await setup_member_project(
        asana_gid, display_name, workspace_gid, asana_token
    )
    projects[asana_gid] = config
    save_projects(projects)
    logger.info(f"Proyecto guardado para {display_name}")
    return config

# ── OPERACIONES DE TAREA ───────────────────────────────────────────────────────

async def add_task_to_member_project(
    task_gid: str,
    asana_gid: str,
    asana_token: str,
) -> bool:
    """
    Agrega una tarea recién creada a '📌 Pendiente' en el proyecto del colaborador.
    """
    projects = load_projects()
    config   = projects.get(asana_gid)
    if not config:
        logger.warning(f"Sin proyecto para asana_gid={asana_gid}")
        return False

    section_gid = config["sections"].get("📌 Pendiente")
    if not section_gid:
        return False

    await _asana_post(
        f"/sections/{section_gid}/addTask",
        {"task": task_gid},
        asana_token,
    )
    return True

async def move_task_status(
    task_gid: str,
    asana_gid: str,
    section_name: str,
    asana_token: str,
) -> bool:
    """
    Mueve una tarea a la sección de estado indicada en el proyecto del colaborador.
    section_name: debe ser uno de STANDARD_SECTIONS.
    """
    projects = load_projects()
    config   = projects.get(asana_gid)
    if not config:
        return False

    section_gid = config["sections"].get(section_name)
    if not section_gid:
        return False

    await _asana_post(
        f"/sections/{section_gid}/addTask",
        {"task": task_gid},
        asana_token,
    )
    return True

async def add_task_comment(task_gid: str, comment: str, asana_token: str) -> bool:
    """Agrega un comentario (story) a una tarea en Asana."""
    await _asana_post(
        f"/tasks/{task_gid}/stories",
        {"text": comment},
        asana_token,
    )
    return True

def get_member_project(asana_gid: str) -> dict | None:
    """Devuelve la config del proyecto de un colaborador, o None si no existe."""
    return load_projects().get(asana_gid)

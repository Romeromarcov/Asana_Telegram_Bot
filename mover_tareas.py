"""
Módulo para mover tareas entre secciones de un proyecto — Bot Lubrikca v5.0

Flujo desde Telegram:
1. Usuario toca "🔀 Mover tarea"
2. Bot muestra sus tareas pendientes → elige cuál mover
3. Bot muestra los proyectos disponibles → elige proyecto
4. Bot muestra las secciones del proyecto → elige sección
5. Bot mueve la tarea en Asana y confirma

La API de Asana usa "sections" dentro de "projects".
Para mover una tarea a una sección: POST /sections/{section_gid}/addTask
"""

import logging

from utils import ASANA_BASE, http_client

logger = logging.getLogger(__name__)

# ── ASANA: PROYECTOS ──────────────────────────────────────────────────────────

async def get_task_projects(task_gid: str, asana_token: str) -> list[dict]:
    """Devuelve los proyectos a los que pertenece una tarea."""
    headers = {"Authorization": f"Bearer {asana_token}"}
    r = await http_client.get(
        f"{ASANA_BASE}/tasks/{task_gid}/projects",
        headers=headers, params={"opt_fields": "gid,name"},
    )
    r.raise_for_status()
    return r.json().get("data", [])

async def get_workspace_projects(workspace_gid: str, asana_token: str) -> list[dict]:
    """Devuelve todos los proyectos del workspace."""
    headers = {"Authorization": f"Bearer {asana_token}"}
    r = await http_client.get(
        f"{ASANA_BASE}/projects",
        headers=headers,
        params={"workspace": workspace_gid, "opt_fields": "gid,name", "limit": 50},
    )
    r.raise_for_status()
    return r.json().get("data", [])

async def get_project_sections(project_gid: str, asana_token: str) -> list[dict]:
    """Devuelve las secciones de un proyecto."""
    headers = {"Authorization": f"Bearer {asana_token}"}
    r = await http_client.get(
        f"{ASANA_BASE}/projects/{project_gid}/sections",
        headers=headers, params={"opt_fields": "gid,name"},
    )
    r.raise_for_status()
    return r.json().get("data", [])

async def move_task_to_section(task_gid: str, section_gid: str, asana_token: str) -> bool:
    """Mueve una tarea a una sección específica."""
    headers = {
        "Authorization": f"Bearer {asana_token}",
        "Content-Type": "application/json",
    }
    r = await http_client.post(
        f"{ASANA_BASE}/sections/{section_gid}/addTask",
        headers=headers,
        json={"data": {"task": task_gid}},
    )
    r.raise_for_status()
    return True

async def get_task_current_section(task_gid: str, project_gid: str, asana_token: str) -> str | None:
    """Devuelve el nombre de la sección actual de la tarea en un proyecto."""
    headers = {"Authorization": f"Bearer {asana_token}"}
    r = await http_client.get(
        f"{ASANA_BASE}/tasks/{task_gid}",
        headers=headers,
        params={"opt_fields": "memberships.section.name,memberships.project.gid"},
    )
    r.raise_for_status()
    memberships = r.json().get("data", {}).get("memberships", [])
    for m in memberships:
        if m.get("project", {}).get("gid") == project_gid:
            return m.get("section", {}).get("name")
    return None

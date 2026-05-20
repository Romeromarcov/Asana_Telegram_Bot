"""
Script de un solo uso — Registrar tareas existentes en los tableros Kanban.

Por cada miembro del equipo:
  1. Obtiene todas sus tareas pendientes en Asana
  2. Las agrega al tablero "Tareas - {nombre}" en la sección "📌 Pendiente"
     si no están ya en ese proyecto

Uso:
  cd asana_telegram_bot
  python registrar_tableros.py

Variables de entorno requeridas:
  ASANA_TOKEN, ASANA_WORKSPACE_ID
  (se pueden poner en un archivo .env o exportar en la terminal)
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

ASANA_TOKEN     = os.environ.get("ASANA_TOKEN", "")
ASANA_WORKSPACE = os.environ.get("ASANA_WORKSPACE_ID", "")
ASANA_BASE      = "https://app.asana.com/api/1.0"
BASE_DIR        = Path(__file__).parent
PROJECTS_FILE   = BASE_DIR / "projects.json"


def load_team() -> dict:
    team = {}
    team_file = BASE_DIR / "team.txt"
    if not team_file.exists():
        return team
    for line in team_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        try:
            tg_id = int(parts[0])
            team[tg_id] = {
                "asana_gid": parts[1],
                "name":      parts[2] if len(parts) > 2 else f"Usuario {tg_id}",
            }
        except ValueError:
            pass
    return team


def load_projects() -> dict:
    if not PROJECTS_FILE.exists():
        return {}
    try:
        return json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


async def get_pending_tasks(client: httpx.AsyncClient, asana_gid: str) -> list:
    headers = {"Authorization": f"Bearer {ASANA_TOKEN}"}
    params  = {
        "assignee": asana_gid,
        "workspace": ASANA_WORKSPACE,
        "completed_since": "now",
        "opt_fields": "gid,name,due_on,memberships.project.gid",
        "limit": 100,
    }
    r = await client.get(f"{ASANA_BASE}/tasks", headers=headers, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("data", [])


async def get_task_project_gids(client: httpx.AsyncClient, task_gid: str) -> set:
    """Devuelve el set de project_gids en los que ya está la tarea."""
    headers = {"Authorization": f"Bearer {ASANA_TOKEN}"}
    params  = {"opt_fields": "gid,name"}
    r = await client.get(
        f"{ASANA_BASE}/tasks/{task_gid}/projects",
        headers=headers, params=params, timeout=15,
    )
    r.raise_for_status()
    return {p["gid"] for p in r.json().get("data", [])}


async def add_to_section(client: httpx.AsyncClient, section_gid: str, task_gid: str) -> bool:
    headers = {"Authorization": f"Bearer {ASANA_TOKEN}", "Content-Type": "application/json"}
    r = await client.post(
        f"{ASANA_BASE}/sections/{section_gid}/addTask",
        headers=headers,
        json={"data": {"task": task_gid}},
        timeout=15,
    )
    r.raise_for_status()
    return True


async def main():
    if not ASANA_TOKEN or not ASANA_WORKSPACE:
        logger.error("❌ ASANA_TOKEN y ASANA_WORKSPACE_ID son requeridos")
        return

    team     = load_team()
    projects = load_projects()

    if not team:
        logger.error("❌ team.txt vacío o no encontrado")
        return

    if not projects:
        logger.error(
            "❌ projects.json vacío. Ejecuta primero post_init del bot "
            "para que se creen los tableros."
        )
        return

    async with httpx.AsyncClient(timeout=20) as client:
        total_added = 0
        total_skip  = 0

        for tg_id, info in team.items():
            asana_gid   = info["asana_gid"]
            name        = info["name"]
            project_cfg = projects.get(asana_gid)

            if not project_cfg:
                logger.warning(f"⚠️  Sin tablero para {name} — omitido")
                continue

            project_gid  = project_cfg.get("project_gid")
            section_gid  = project_cfg.get("sections", {}).get("📌 Pendiente")

            if not project_gid or not section_gid:
                logger.warning(f"⚠️  Tablero incompleto para {name} — omitido")
                continue

            logger.info(f"\n👤 {name}")

            try:
                tasks = await get_pending_tasks(client, asana_gid)
            except Exception as e:
                logger.error(f"  Error obteniendo tareas: {e}")
                continue

            logger.info(f"  {len(tasks)} tarea(s) pendiente(s)")

            for task in tasks:
                task_gid  = task["gid"]
                task_name = task["name"]

                try:
                    existing_projects = await get_task_project_gids(client, task_gid)
                    if project_gid in existing_projects:
                        logger.info(f"  ✓ Ya en tablero: {task_name[:60]}")
                        total_skip += 1
                        continue

                    await add_to_section(client, section_gid, task_gid)
                    logger.info(f"  ✅ Registrada: {task_name[:60]}")
                    total_added += 1
                    await asyncio.sleep(0.3)   # respetar rate limit Asana

                except Exception as e:
                    logger.error(f"  ❌ Error con '{task_name[:40]}': {e}")

    logger.info(
        f"\n{'─'*50}\n"
        f"✅ Registradas en tableros: {total_added}\n"
        f"⏭  Ya existían:             {total_skip}\n"
        f"{'─'*50}"
    )


if __name__ == "__main__":
    asyncio.run(main())

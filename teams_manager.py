"""
Gestión de áreas de trabajo (equipos) — Bot Lubrikca v6.0

Estructura en DB / teams.json:
{
  "administracion": {
    "name": "Administración",
    "leader_tg_id": 111111,
    "leader_asana_gid": "...",
    "leader_name": "Marco Velasco (Admin)",
    "members": [
      {"tg_id": 222222, "asana_gid": "...", "name": "Melanie"}
    ]
  }
}

Estructura de area_tasks (DB / area_tasks.json):
[
  {
    "task_gid": "...",
    "task_name": "Revisar inventario",
    "area_slug": "almacen",
    "area_name": "Almacén",
    "assigned_to_tg_id": 12345,
    "assigned_to_name": "Pedro",
    "leader_tg_id": 99999,
    "due_on": "2026-05-25",
    "created_at": "2026-05-21",
    "status": "pending"  // pending | completed
  }
]
"""

import json
import re
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

TEAMS_FILE      = Path(__file__).parent / "teams.json"
AREA_TASKS_FILE = Path(__file__).parent / "area_tasks.json"


def _slugify(name: str) -> str:
    """Convierte un nombre a slug: 'Administración' → 'administracion'."""
    name = name.lower()
    name = name.replace("á", "a").replace("é", "e").replace("í", "i") \
               .replace("ó", "o").replace("ú", "u").replace("ñ", "n")
    return re.sub(r"[^a-z0-9]+", "_", name).strip("_")


# ── PERSISTENCIA ───────────────────────────────────────────────────────────────

def load_teams() -> dict:
    """DB primero, archivo local como fallback."""
    try:
        from db import db_get
        data = db_get("teams")
        if data is not None:
            return data
    except Exception:
        pass
    if TEAMS_FILE.exists():
        try:
            return json.loads(TEAMS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_teams(teams: dict):
    """Guarda en DB y en archivo local."""
    try:
        from db import db_set
        db_set("teams", teams)
    except Exception as e:
        logger.warning(f"No se pudo guardar teams en DB: {e}")
    try:
        TEAMS_FILE.write_text(
            json.dumps(teams, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"No se pudo guardar teams en archivo: {e}")


# ── CRUD DE ÁREAS ──────────────────────────────────────────────────────────────

def create_area(name: str, leader_tg_id: int, leader_asana_gid: str,
                leader_name: str) -> str:
    """
    Crea un área nueva. Devuelve el slug.
    Lanza ValueError si ya existe un área con ese nombre.
    """
    teams = load_teams()
    slug  = _slugify(name)
    if slug in teams:
        raise ValueError(f"Ya existe un área con el nombre '{name}' (slug: {slug})")
    teams[slug] = {
        "name":             name,
        "leader_tg_id":     leader_tg_id,
        "leader_asana_gid": leader_asana_gid,
        "leader_name":      leader_name,
        "members":          [],
    }
    save_teams(teams)
    logger.info(f"Área creada: {name} (líder: {leader_name})")
    return slug


def delete_area(slug: str) -> bool:
    teams = load_teams()
    if slug not in teams:
        return False
    del teams[slug]
    save_teams(teams)
    return True


def get_area(slug: str) -> dict | None:
    return load_teams().get(slug)


# ── MIEMBROS ───────────────────────────────────────────────────────────────────

def add_member(slug: str, tg_id: int, asana_gid: str, name: str) -> bool:
    teams = load_teams()
    if slug not in teams:
        return False
    if any(m["tg_id"] == tg_id for m in teams[slug]["members"]):
        return False  # ya existe
    teams[slug]["members"].append({"tg_id": tg_id, "asana_gid": asana_gid, "name": name})
    save_teams(teams)
    return True


def remove_member(slug: str, tg_id: int) -> bool:
    teams = load_teams()
    if slug not in teams:
        return False
    before = len(teams[slug]["members"])
    teams[slug]["members"] = [m for m in teams[slug]["members"] if m["tg_id"] != tg_id]
    if len(teams[slug]["members"]) == before:
        return False
    save_teams(teams)
    return True


def update_leader(slug: str, new_leader_tg_id: int, new_leader_asana_gid: str,
                  new_leader_name: str) -> bool:
    teams = load_teams()
    if slug not in teams:
        return False
    teams[slug]["leader_tg_id"]     = new_leader_tg_id
    teams[slug]["leader_asana_gid"] = new_leader_asana_gid
    teams[slug]["leader_name"]      = new_leader_name
    save_teams(teams)
    return True


def get_areas_for_member(tg_id: int) -> list[dict]:
    """Devuelve lista de áreas donde el tg_id es líder o miembro."""
    result = []
    for slug, t in load_teams().items():
        is_leader = t["leader_tg_id"] == tg_id
        is_member = any(m["tg_id"] == tg_id for m in t.get("members", []))
        if is_leader or is_member:
            result.append({"slug": slug, **t, "is_leader": is_leader})
    return result


# ── REGISTRO DE TAREAS DE ÁREA ────────────────────────────────────────────────

def load_area_tasks() -> list:
    """Carga el registro de tareas de área. DB primero, archivo local como fallback."""
    try:
        from db import db_get
        data = db_get("area_tasks")
        if data is not None:
            return data
    except Exception:
        pass
    if AREA_TASKS_FILE.exists():
        try:
            return json.loads(AREA_TASKS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_area_tasks(tasks: list):
    """Guarda en DB y archivo local. Mantiene máximo 500 registros."""
    if len(tasks) > 500:
        tasks = tasks[-500:]
    try:
        from db import db_set
        db_set("area_tasks", tasks)
    except Exception as e:
        logger.warning(f"No se pudo guardar area_tasks en DB: {e}")
    try:
        AREA_TASKS_FILE.write_text(
            json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"No se pudo guardar area_tasks en archivo: {e}")


def register_area_task(
    task_gid: str,
    task_name: str,
    area_slug: str,
    area_name: str,
    assigned_to_tg_id: int,
    assigned_to_name: str,
    leader_tg_id: int,
    due_on: str = None,
) -> None:
    """Registra una tarea recién creada para un área."""
    tasks = load_area_tasks()
    tasks.append({
        "task_gid":          task_gid,
        "task_name":         task_name,
        "area_slug":         area_slug,
        "area_name":         area_name,
        "assigned_to_tg_id": assigned_to_tg_id,
        "assigned_to_name":  assigned_to_name,
        "leader_tg_id":      leader_tg_id,
        "due_on":            due_on,
        "created_at":        datetime.utcnow().strftime("%Y-%m-%d"),
        "status":            "pending",
    })
    save_area_tasks(tasks)
    logger.info(f"Tarea de área registrada: {task_name} → {area_name} ({assigned_to_name})")


def update_area_task_assignee(task_gid: str, new_tg_id: int, new_name: str) -> bool:
    """Actualiza el responsable de una tarea de área (cuando el líder la delega)."""
    tasks   = load_area_tasks()
    changed = False
    for t in tasks:
        if t["task_gid"] == task_gid:
            t["assigned_to_tg_id"] = new_tg_id
            t["assigned_to_name"]  = new_name
            changed = True
            break
    if changed:
        save_area_tasks(tasks)
    return changed


def complete_area_task(task_gid: str) -> dict | None:
    """
    Marca una tarea de área como completada.
    Devuelve el registro si existía y estaba pendiente, o None si no aplica.
    """
    tasks   = load_area_tasks()
    changed = False
    result  = None
    for t in tasks:
        if t["task_gid"] == task_gid and t.get("status") == "pending":
            t["status"]       = "completed"
            t["completed_at"] = datetime.utcnow().strftime("%Y-%m-%d")
            result  = t
            changed = True
            break
    if changed:
        save_area_tasks(tasks)
    return result


def get_area_task_by_gid(task_gid: str) -> dict | None:
    """Busca una tarea de área por su GID de Asana."""
    for t in load_area_tasks():
        if t["task_gid"] == task_gid:
            return t
    return None


def get_area_tasks_by_slug(slug: str, only_pending: bool = False) -> list:
    """Devuelve tareas de un área. Opcionalmente filtra solo las pendientes."""
    tasks = [t for t in load_area_tasks() if t["area_slug"] == slug]
    if only_pending:
        tasks = [t for t in tasks if t.get("status") == "pending"]
    # Más recientes primero
    return sorted(tasks, key=lambda t: t.get("created_at", ""), reverse=True)


# ── PERMISOS ───────────────────────────────────────────────────────────────────

DEFAULT_PERMISSIONS: dict = {
    "leader_can_create_tasks":       True,   # Líderes pueden crear tareas para su equipo
    "leader_can_assign_to_team":     True,   # Líderes pueden asignar a todo el equipo
    "leader_receives_reports":       True,   # Líderes reciben reportes de su área
    "leader_can_request_delegation": True,   # Líderes pueden pedir tareas al manager
    "members_can_create_own_tasks":  True,   # Miembros pueden crear sus propias tareas
}


def get_permissions() -> dict:
    """Lee permisos de DB. Mezcla con defaults para nuevas claves."""
    try:
        from db import db_get
        perms = db_get("permissions")
        if perms and isinstance(perms, dict):
            return {**DEFAULT_PERMISSIONS, **perms}
    except Exception:
        pass
    return DEFAULT_PERMISSIONS.copy()


def save_permissions(perms: dict) -> None:
    try:
        from db import db_set
        db_set("permissions", perms)
    except Exception as e:
        logger.warning(f"No se pudo guardar permisos: {e}")


# ── ROLES DE MIEMBROS ─────────────────────────────────────────────────────────

def set_member_role(slug: str, tg_id: int, role: str) -> bool:
    """Asigna un rol a un miembro del área. role: 'member' | 'coordinator'"""
    teams = load_teams()
    if slug not in teams:
        return False
    for m in teams[slug]["members"]:
        if m["tg_id"] == tg_id:
            m["role"] = role
            save_teams(teams)
            return True
    return False


# ── SOLICITUDES DE DELEGACIÓN ─────────────────────────────────────────────────

def save_delegation_request(leader_tg_id: int, leader_name: str, task_text: str) -> int:
    """Guarda una solicitud de delegación. Devuelve el ID asignado."""
    try:
        from db import db_get, db_set
        reqs = db_get("delegation_requests") or []
        req_id = max((r["id"] for r in reqs), default=0) + 1
        reqs.append({
            "id":            req_id,
            "leader_tg_id":  leader_tg_id,
            "leader_name":   leader_name,
            "task_text":     task_text,
            "created_at":    datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            "status":        "pending",
        })
        db_set("delegation_requests", reqs[-100:])
        return req_id
    except Exception as e:
        logger.warning(f"No se pudo guardar delegation_request: {e}")
        return 0


def get_delegation_request(req_id: int) -> dict | None:
    try:
        from db import db_get
        reqs = db_get("delegation_requests") or []
        return next((r for r in reqs if r["id"] == req_id), None)
    except Exception:
        return None


def resolve_delegation_request(req_id: int) -> bool:
    """Marca una solicitud como resuelta."""
    try:
        from db import db_get, db_set
        reqs = db_get("delegation_requests") or []
        changed = False
        for r in reqs:
            if r["id"] == req_id:
                r["status"] = "resolved"
                changed = True
                break
        if changed:
            db_set("delegation_requests", reqs)
        return changed
    except Exception:
        return False

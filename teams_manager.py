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
"""

import json
import re
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

TEAMS_FILE = Path(__file__).parent / "teams.json"


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

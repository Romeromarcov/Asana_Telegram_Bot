"""
Módulo para gestión de miembros del equipo desde Telegram — Bot Lubrikca v6.0
Permite agregar y desactivar colaboradores sin editar el repositorio manualmente.
"""

import logging
from pathlib import Path

logger    = logging.getLogger(__name__)
TEAM_FILE = Path(__file__).parent / "team.txt"


def add_member(tg_id: int, asana_gid: str, name: str) -> bool:
    """
    Agrega un nuevo miembro a team.txt.
    Devuelve False si el tg_id ya existe (activo o inactivo).
    """
    try:
        existing = TEAM_FILE.read_text(encoding="utf-8")
        for line in existing.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if not stripped or "|" not in stripped:
                continue
            first_col = stripped.split("|")[0].strip()
            if first_col == str(tg_id):
                logger.warning(f"Miembro {tg_id} ya existe en team.txt")
                return False

        new_line = f"\n{tg_id:<12}| {asana_gid} | {name}"
        with open(TEAM_FILE, "a", encoding="utf-8") as f:
            f.write(new_line)
        logger.info(f"Miembro agregado: {name} ({tg_id})")
        return True
    except Exception as e:
        logger.error(f"Error agregando miembro: {e}")
        return False


def remove_member(tg_id: int) -> str | None:
    """
    Desactiva un miembro comentando su línea en team.txt.
    Devuelve el nombre si se encontró, None si no.
    """
    try:
        lines        = TEAM_FILE.read_text(encoding="utf-8").splitlines()
        new_lines    = []
        removed_name = None
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("#") and "|" in stripped:
                first_col = stripped.split("|")[0].strip()
                if first_col == str(tg_id):
                    new_lines.append("# " + line)
                    parts        = [p.strip() for p in stripped.split("|")]
                    removed_name = parts[2] if len(parts) > 2 else "Miembro"
                    continue
            new_lines.append(line)

        if removed_name:
            TEAM_FILE.write_text("\n".join(new_lines), encoding="utf-8")
            logger.info(f"Miembro desactivado: {removed_name} ({tg_id})")
        return removed_name
    except Exception as e:
        logger.error(f"Error removiendo miembro: {e}")
        return None

"""
Utilidades compartidas — Bot Lubrikca v7.0

Constantes y helpers reutilizados por bot.py, dashboard.py,
asana_projects.py y mover_tareas.py.
Importar desde aquí evita duplicados y concentra la configuración.
"""

from pathlib import Path
import httpx

# ── Constantes Asana ───────────────────────────────────────────────────────────
ASANA_BASE = "https://app.asana.com/api/1.0"

# ── Cliente HTTP compartido ────────────────────────────────────────────────────
# Un único AsyncClient por proceso reutiliza conexiones TCP (connection pooling).
# Se cierra automáticamente al terminar el proceso.
http_client = httpx.AsyncClient(timeout=15)

# ── Equipo ─────────────────────────────────────────────────────────────────────

def _parse_team_file() -> dict:
    """Lee team.txt → {tg_id: {asana_gid, name}}. Sin fallback a DB."""
    team      = {}
    team_file = Path(__file__).parent / "team.txt"
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


def load_team() -> dict:
    """
    Devuelve {tg_id: {"asana_gid": str, "name": str}}.
    Prioridad: 1) PostgreSQL (fuente de verdad compartida entre servicios)
               2) team.txt  (fallback dev local o primer arranque)
    """
    from db import db_get
    db_data = db_get("team")
    if db_data is not None:
        # JSON solo guarda string keys → convertir a int
        return {int(k): v for k, v in db_data.items()}
    return _parse_team_file()


def save_team_data(team: dict):
    """
    Persiste el equipo en PostgreSQL.
    Llamar después de cualquier modificación a team.txt.
    """
    from db import db_set
    # JSON requiere string keys
    db_set("team", {str(k): v for k, v in team.items()})

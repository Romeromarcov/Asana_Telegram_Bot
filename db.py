"""
Capa de acceso a base de datos — Bot Lubrikca v7.0

KV store simple sobre PostgreSQL compartido entre los servicios Web y Worker.
Permite que ambos lean y escriban el mismo estado en tiempo real.

Cada "archivo" se convierte en una fila:
  key="team"         → dict {tg_id_str: {asana_gid, name}}
  key="recurring"    → list  [...]
  key="alert_state"  → dict  {task_gid: {alerts_sent, blocked}}
  key="task_meta"    → dict  {task_gid: {created_on, due_on, total_days}}
  key="known_tasks"  → dict  {asana_gid: [task_gid, ...]}
  key="projects"     → dict  {asana_gid: {project_gid, sections, ...}}

Fallback: si DATABASE_URL no está configurada (dev local), todas las
operaciones devuelven None / no-op y el código usa los archivos locales.
"""

import os
import json
import logging

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ── Conexión ───────────────────────────────────────────────────────────────────

_conn = None

def _get_conn():
    """Devuelve una conexión psycopg2 activa, reconectando si hace falta."""
    global _conn
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        # Reconectar si la conexión fue cerrada o expiró
        if _conn is None or _conn.closed:
            _conn = psycopg2.connect(DATABASE_URL)
            _conn.autocommit = True
        else:
            # Ping rápido para detectar conexión zombie
            try:
                _conn.cursor().execute("SELECT 1")
            except Exception:
                _conn = psycopg2.connect(DATABASE_URL)
                _conn.autocommit = True
        return _conn
    except Exception as e:
        logger.error(f"❌ PostgreSQL — no se pudo conectar: {e}")
        return None


# ── Inicialización ─────────────────────────────────────────────────────────────

def setup_db() -> bool:
    """
    Crea la tabla kv_store si no existe y hace seed inicial desde archivos
    locales si la DB está vacía (primer arranque tras agregar la DB).
    Debe llamarse una vez al iniciar cada servicio.
    Devuelve True si la DB está lista, False si no hay DATABASE_URL.
    """
    conn = _get_conn()
    if not conn:
        logger.info("DATABASE_URL no configurada — estado guardado en archivos locales")
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    key        TEXT PRIMARY KEY,
                    value      JSONB NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        logger.info("✅ PostgreSQL listo — kv_store OK")
        _seed_from_files(conn)
        return True
    except Exception as e:
        logger.error(f"❌ setup_db: {e}")
        return False


def _seed_from_files(conn):
    """
    Carga team.txt y recurring.json al DB solo si esas claves no existen aún.
    Esto asegura que el primer deploy con DB no pierda la configuración existente.
    """
    import json
    from pathlib import Path

    base = Path(__file__).parent

    seeds = {
        "team":      _load_team_from_file(base),
        "recurring": _load_json_file(base / "recurring.json", []),
        "projects":  _load_json_file(base / "projects.json",  {}),
    }

    for key, value in seeds.items():
        if not value:
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO kv_store (key, value, updated_at)
                    VALUES (%s, %s::jsonb, NOW())
                    ON CONFLICT (key) DO NOTHING
                    """,
                    (key, json.dumps(value, ensure_ascii=False)),
                )
                if cur.rowcount:
                    logger.info(f"  Seed inicial '{key}' → DB ✓")
        except Exception as e:
            logger.warning(f"Seed '{key}' falló: {e}")


def _load_team_from_file(base) -> dict:
    """Lee team.txt → {tg_id_str: {asana_gid, name}} para guardar en DB."""
    team      = {}
    team_file = base / "team.txt"
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
            # JSON requiere string keys
            team[str(tg_id)] = {
                "asana_gid": parts[1],
                "name":      parts[2] if len(parts) > 2 else f"Usuario {tg_id}",
            }
        except ValueError:
            pass
    return team


def _load_json_file(path, default):
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# ── Operaciones KV ─────────────────────────────────────────────────────────────

def db_get(key: str, default=None):
    """
    Lee el valor JSON para `key`. Devuelve `default` si no existe o sin DB.
    NOTA: las claves numéricas de dicts se guardan como strings en JSON;
    el llamador debe convertirlas si necesita int keys.
    """
    conn = _get_conn()
    if not conn:
        return default
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM kv_store WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else default
    except Exception as e:
        logger.error(f"db_get({key}): {e}")
        return default


def db_set(key: str, value) -> bool:
    """
    Guarda `value` (serializable a JSON) bajo `key`.
    Actualiza updated_at si ya existe (upsert).
    Devuelve True si OK, False si falló o sin DB.
    """
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kv_store (key, value, updated_at)
                VALUES (%s, %s::jsonb, NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value      = EXCLUDED.value,
                        updated_at = EXCLUDED.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False)),
            )
        return True
    except Exception as e:
        logger.error(f"db_set({key}): {e}")
        return False


def db_has(key: str) -> bool:
    """Devuelve True si la clave existe en la DB."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM kv_store WHERE key = %s", (key,))
            return cur.fetchone() is not None
    except Exception:
        return False

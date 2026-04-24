"""
Motor de procesamiento de minutas — Bot Lubrikca v4.2

Flujo:
  /minuta → usuario envía texto / foto / PDF
  → Gemini extrae tareas estructuradas
  → revisión + corrección manual en Telegram
  → creación en Asana + notificaciones
  → historial guardado en minutas.json
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

import google.generativeai as genai

logger = logging.getLogger(__name__)

MINUTAS_FILE = Path(__file__).parent / "minutas.json"

# ── PERSISTENCIA ──────────────────────────────────────────────────────────────

def load_minutas() -> list:
    if not MINUTAS_FILE.exists():
        return []
    try:
        return json.loads(MINUTAS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_minuta(minuta: dict):
    """Agrega una minuta al historial (máx. 50 registros)."""
    data = load_minutas()
    data.append(minuta)
    if len(data) > 50:
        data = data[-50:]
    MINUTAS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

# ── PROMPT PARA GEMINI ────────────────────────────────────────────────────────

def build_prompt(text: str, team_names: list[str], today_str: str) -> str:
    names = ", ".join(team_names)
    return (
        f"Hoy es {today_str}. El equipo disponible es: {names}.\n\n"
        "Eres un asistente experto en gestión de proyectos. "
        "Extrae TODAS las tareas, compromisos y acciones pendientes de la minuta.\n\n"
        "Reglas estrictas:\n"
        "1. Devuelve ÚNICAMENTE un JSON array válido. Sin texto adicional, sin markdown.\n"
        "2. assignee_name: usa uno de los nombres del equipo (el más cercano), o null.\n"
        "3. due_on: fecha en formato YYYY-MM-DD. Interpreta expresiones como 'viernes', "
        "'próxima semana', 'en 3 días' usando hoy como referencia. Si no hay fecha: null.\n"
        "4. notes: contexto adicional útil para ejecutar la tarea, o null.\n"
        "5. Si no hay tareas claras en el texto, devuelve [].\n\n"
        "Formato de respuesta (solo esto, nada más):\n"
        '[{"task_name":"...","assignee_name":"...","due_on":"YYYY-MM-DD","notes":"..."}]\n\n'
        f"MINUTA:\n{text}"
    )

# ── LLAMADA A GEMINI ──────────────────────────────────────────────────────────

async def call_gemini(
    text: str,
    image_bytes: bytes | None,
    mime_type: str | None,
    team_names: list[str],
    today_str: str,
) -> list[dict]:
    """
    Llama a Gemini 1.5 Flash y devuelve la lista cruda de tareas extraídas.
    Lanza excepción si la llamada falla (el caller debe manejarla).
    """
    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = build_prompt(
        text or "(extraer tareas de la imagen/documento adjunto)",
        team_names,
        today_str,
    )

    if image_bytes:
        content = [prompt, {"mime_type": mime_type, "data": image_bytes}]
    else:
        content = prompt

    response = await model.generate_content_async(content)
    raw = response.text.strip()

    # Eliminar posibles code fences de markdown
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```\s*$",       "", raw, flags=re.MULTILINE)
    raw = raw.strip()

    tasks = json.loads(raw)
    return tasks if isinstance(tasks, list) else [tasks]

# ── MATCH DE RESPONSABLES ─────────────────────────────────────────────────────

def match_assignee(name_hint: str | None, team: dict) -> tuple:
    """
    Busca el miembro del equipo más cercano a name_hint.
    Devuelve (tg_id, info_dict) o (None, None).
    """
    if not name_hint:
        return None, None

    hint = name_hint.lower().strip()

    for tg_id, info in team.items():
        full  = info["name"].lower()
        first = full.split()[0]
        if hint == first or hint == full or hint in full or first in hint:
            return tg_id, info

    return None, None

def enrich_tasks(raw_tasks: list[dict], team: dict) -> list[dict]:
    """
    Normaliza y enriquece cada tarea con tg_id y asana_gid del responsable.
    Valida también el formato de la fecha.
    """
    result = []
    for t in raw_tasks:
        task = {
            "task_name":     (t.get("task_name") or "Sin nombre").strip(),
            "assignee_name": t.get("assignee_name"),
            "assignee_tg_id": None,
            "assignee_gid":   None,
            "due_on":         t.get("due_on"),
            "notes":          t.get("notes"),
        }

        # Validar formato de fecha
        if task["due_on"]:
            try:
                datetime.strptime(task["due_on"], "%Y-%m-%d")
            except ValueError:
                task["due_on"] = None

        # Enriquecer con datos del equipo
        tg_id, info = match_assignee(task["assignee_name"], team)
        if tg_id:
            task["assignee_tg_id"] = tg_id
            task["assignee_gid"]   = info["asana_gid"]
            task["assignee_name"]  = info["name"]   # nombre canónico

        result.append(task)
    return result

# ── FORMATO PARA TELEGRAM ─────────────────────────────────────────────────────

def format_tasks_preview(tasks: list[dict]) -> str:
    """Genera el mensaje de resumen de tareas para mostrar en Telegram."""
    lines = []
    for i, t in enumerate(tasks, 1):
        complete = bool(t.get("assignee_tg_id") and t.get("due_on"))
        icon     = "✅" if complete else "⚠️"
        who      = t.get("assignee_name") or "❌ Sin responsable"
        due      = t.get("due_on")        or "❌ Sin fecha"
        name     = t["task_name"]
        lines.append(f"{icon} {i}. *{name}*\n   👤 {who}  |  📅 {due}")
    return "\n\n".join(lines)

def tasks_need_fixing(tasks: list[dict]) -> bool:
    """True si alguna tarea le falta responsable o fecha."""
    return any(
        not t.get("assignee_tg_id") or not t.get("due_on")
        for t in tasks
    )

def next_incomplete_idx(tasks: list[dict], start: int = 0) -> int | None:
    """Devuelve el índice de la próxima tarea incompleta, o None si no hay."""
    for i in range(start, len(tasks)):
        t = tasks[i]
        if not t.get("assignee_tg_id") or not t.get("due_on"):
            return i
    return None

# ── HELPERS DE HISTORIAL ──────────────────────────────────────────────────────

def build_minuta_record(
    submitter_tg_id: int,
    submitter_name: str,
    raw_text: str,
    tasks_created: list[dict],
    tz,
) -> dict:
    """Construye el registro de historial para una minuta procesada."""
    now = datetime.now(tz)
    return {
        "id":                now.strftime("%Y%m%d-%H%M%S"),
        "date":              now.strftime("%Y-%m-%d"),
        "time":              now.strftime("%H:%M"),
        "submitted_by":      submitter_tg_id,
        "submitted_by_name": submitter_name,
        "raw_text":          raw_text[:2000],   # límite para no inflar el JSON
        "tasks_created": [
            {
                "task_name":    t["task_name"],
                "assignee_name": t.get("assignee_name"),
                "due_on":       t.get("due_on"),
                "asana_gid":    t.get("created_gid", ""),
            }
            for t in tasks_created
        ],
    }

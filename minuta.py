"""
Motor de procesamiento de minutas — Bot Lubrikca v4.2
Flujo:
  📝 Subir minuta → usuario envía texto / foto / PDF
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
    data = load_minutas()
    data.append(minuta)
    if len(data) > 50:
        data = data[-50:]
    MINUTAS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# ── PROMPT PARA GEMINI ────────────────────────────────────────────────────────

def build_prompt(text: str, team_names: list[str], today_str: str) -> str:
    team_desc = "\n".join(f"  - {n}" for n in team_names)
    return (
        f"Hoy es {today_str}.\n\n"
        f"Equipo disponible (nombre + área entre paréntesis):\n{team_desc}\n\n"
        "Eres un asistente experto en gestión de proyectos. "
        "Extrae TODAS las tareas, compromisos y acciones pendientes de la minuta.\n\n"
        "Reglas estrictas:\n"
        "1. Devuelve ÚNICAMENTE un JSON array válido. Sin texto adicional, sin markdown.\n"
        "2. assignee_name: usa el nombre base (sin área) de uno de los miembros del equipo.\n"
        "   - Si se menciona un nombre explícitamente, úsalo.\n"
        "   - Si NO se menciona nombre pero el contexto indica un área "
        "(ej: 'ventas' → Ventas, 'almacén'/'logística' → Almacén, "
        "'cobranza'/'cobro' → Cobranza/Atención, 'administración'/'admin' → Administración), "
        "asigna al miembro de esa área.\n"
        "   - Si no puedes inferir el responsable con razonable certeza, usa null.\n"
        "3. due_on: fecha en formato YYYY-MM-DD. Interpreta 'viernes', 'próxima semana', "
        "'en 3 días' usando hoy como referencia. Si no hay fecha: null.\n"
        "4. notes: contexto adicional útil para ejecutar la tarea, o null.\n"
        "5. Si no hay tareas claras en el texto, devuelve [].\n\n"
        "Formato de respuesta (solo esto, nada más):\n"
        '[{"task_name":"...","assignee_name":"...","due_on":"YYYY-MM-DD","notes":"..."}]\n\n'
        f"MINUTA:\n{text}"
    )

# ── LLAMADA A GEMINI ──────────────────────────────────────────────────────────

# Mensajes de error claros según el tipo de fallo
GEMINI_ERROR_MESSAGES = {
    "image_quality": (
        "📷 *No pude leer bien la imagen.*\n\n"
        "Posibles causas:\n"
        "• La foto está muy oscura o desenfocada\n"
        "• El texto es muy pequeño o está cortado\n"
        "• El ángulo hace difícil leer el contenido\n\n"
        "💡 *¿Qué puedes hacer?*\n"
        "• Toma la foto con mejor luz y de frente\n"
        "• O copia el texto de la minuta y envíalo directamente"
    ),
    "no_tasks": (
        "🤔 *Gemini no encontró tareas en el contenido enviado.*\n\n"
        "Puede que:\n"
        "• El texto no tiene compromisos o acciones claras\n"
        "• La minuta está en un formato muy diferente al esperado\n\n"
        "💡 *Sugerencia:* Envía el texto directamente con verbos de acción como "
        "'llamar', 'enviar', 'revisar', 'completar'."
    ),
    "parse_error": (
        "⚙️ *Hubo un error procesando la respuesta de Gemini.*\n\n"
        "Esto puede pasar cuando la imagen o el documento tiene:\n"
        "• Múltiples idiomas mezclados\n"
        "• Tablas o formatos muy complejos\n"
        "• Texto manuscrito difícil de leer\n\n"
        "💡 *Solución:* Copia el texto de la minuta y envíalo como mensaje de texto."
    ),
    "api_error": (
        "🌐 *No pude conectarme al servicio de IA en este momento.*\n\n"
        "Esto es temporal. Por favor:\n"
        "• Espera un minuto e intenta de nuevo\n"
        "• Si el problema persiste, avísale a Marco"
    ),
}

async def call_gemini(
    text: str,
    image_bytes: bytes | None,
    mime_type: str | None,
    team_names: list[str],
    today_str: str,
) -> list[dict]:
    """
    Llama a Gemini 1.5 Flash y devuelve la lista cruda de tareas extraídas.
    Lanza GeminiError con tipo específico para que el caller muestre el mensaje correcto.
    """
    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = build_prompt(
        text or "(extraer tareas de la imagen/documento adjunto)",
        team_names,
        today_str,
    )

    try:
        if image_bytes:
            content = [prompt, {"mime_type": mime_type, "data": image_bytes}]
        else:
            content = prompt

        response = await model.generate_content_async(content)
        raw = response.text.strip()

    except Exception as e:
        error_str = str(e).lower()
        if "image" in error_str or "vision" in error_str or "media" in error_str:
            raise GeminiError("image_quality", str(e))
        raise GeminiError("api_error", str(e))

    # Limpiar posibles code fences de markdown
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```\s*$", "", raw, flags=re.MULTILINE)
    raw = raw.strip()

    # Verificar que no esté vacío o sea un mensaje de error de Gemini
    if not raw or raw.startswith("Lo siento") or raw.startswith("No puedo") or raw == "[]":
        if image_bytes and (not raw or raw == "[]"):
            raise GeminiError("image_quality", "Gemini returned empty response for image")
        raise GeminiError("no_tasks", f"Empty or refusal response: {raw[:100]}")

    try:
        tasks = json.loads(raw)
    except json.JSONDecodeError as e:
        # Si falla el parse y había imagen, probablemente es calidad de imagen
        if image_bytes:
            raise GeminiError("image_quality", f"JSON parse error: {e}")
        raise GeminiError("parse_error", f"JSON parse error: {e} | Raw: {raw[:200]}")

    if not isinstance(tasks, list):
        tasks = [tasks]

    if len(tasks) == 0:
        raise GeminiError("no_tasks", "Gemini returned empty task list")

    return tasks


class GeminiError(Exception):
    """Error con tipo específico para mostrar mensaje amigable al usuario."""
    def __init__(self, error_type: str, detail: str = ""):
        self.error_type = error_type
        self.detail = detail
        super().__init__(f"{error_type}: {detail}")

    def user_message(self) -> str:
        return GEMINI_ERROR_MESSAGES.get(self.error_type, GEMINI_ERROR_MESSAGES["api_error"])


# ── MATCH DE RESPONSABLES ─────────────────────────────────────────────────────

def match_assignee(name_hint: str | None, team: dict) -> tuple:
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
    result = []
    for t in raw_tasks:
        task = {
            "task_name":      (t.get("task_name") or "Sin nombre").strip(),
            "assignee_name":  t.get("assignee_name"),
            "assignee_tg_id": None,
            "assignee_gid":   None,
            "due_on":         t.get("due_on"),
            "notes":          t.get("notes"),
        }
        if task["due_on"]:
            try:
                datetime.strptime(task["due_on"], "%Y-%m-%d")
            except ValueError:
                task["due_on"] = None
        tg_id, info = match_assignee(task["assignee_name"], team)
        if tg_id:
            task["assignee_tg_id"] = tg_id
            task["assignee_gid"]   = info["asana_gid"]
            task["assignee_name"]  = info["name"]
        result.append(task)
    return result

# ── FORMATO PARA TELEGRAM ─────────────────────────────────────────────────────

def format_tasks_preview(tasks: list[dict]) -> str:
    lines = []
    for i, t in enumerate(tasks, 1):
        complete = bool(t.get("assignee_tg_id") and t.get("due_on"))
        icon = "✅" if complete else "⚠️"
        who  = t.get("assignee_name") or "❌ Sin responsable"
        due  = t.get("due_on")        or "❌ Sin fecha"
        lines.append(f"{icon} {i}. *{t['task_name']}*\n   👤 {who} | 📅 {due}")
    return "\n\n".join(lines)

def tasks_need_fixing(tasks: list[dict]) -> bool:
    return any(not t.get("assignee_tg_id") or not t.get("due_on") for t in tasks)

def next_incomplete_idx(tasks: list[dict], start: int = 0) -> int | None:
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
    now = datetime.now(tz)
    return {
        "id":                 now.strftime("%Y%m%d-%H%M%S"),
        "date":               now.strftime("%Y-%m-%d"),
        "time":               now.strftime("%H:%M"),
        "submitted_by":       submitter_tg_id,
        "submitted_by_name":  submitter_name,
        "raw_text":           raw_text[:2000],
        "tasks_created": [
            {
                "task_name":     t["task_name"],
                "assignee_name": t.get("assignee_name"),
                "due_on":        t.get("due_on"),
                "asana_gid":     t.get("created_gid", ""),
            }
            for t in tasks_created
        ],
    }

"""
Entry point — corre el bot de Telegram + el panel web en el mismo proceso.

  Bot polling  → hilo principal (blocking)
  Dashboard    → hilo de fondo (FastAPI + uvicorn en daemon thread)

Railway expone el puerto $PORT al dashboard; el bot usa long polling.
"""

import os
import threading
import logging

import uvicorn
from dashboard import app as dashboard_app

logger = logging.getLogger(__name__)


def run_dashboard() -> None:
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        dashboard_app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    # Dashboard en hilo demonio (muere si el bot muere)
    t = threading.Thread(target=run_dashboard, daemon=True, name="dashboard")
    t.start()
    logger.info(f"🌐 Dashboard iniciado en puerto {os.environ.get('PORT', '8000')}")

    # Bot en hilo principal (blocking — Railway lo reinicia si falla)
    from bot import main as bot_main
    bot_main()

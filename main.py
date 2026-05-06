"""
Entry point — corre el bot de Telegram + el panel web en el mismo proceso.

  Bot polling  → hilo principal (blocking)
  Dashboard    → hilo de fondo (FastAPI + uvicorn en daemon thread)

Railway expone el puerto $PORT al dashboard; el bot usa long polling.

NOTA: Uvicorn sólo puede registrar signal handlers desde el hilo principal.
      Por eso usamos DashboardServer que sobrescribe install_signal_handlers.
"""

import os
import asyncio
import threading
import logging

import uvicorn
from dashboard import app as dashboard_app

logger = logging.getLogger(__name__)


class DashboardServer(uvicorn.Server):
    """Uvicorn server que puede correr en un hilo no-principal sin crashear."""

    def install_signal_handlers(self) -> None:
        # Los signal handlers (SIGTERM/SIGINT) solo se pueden instalar desde
        # el hilo principal. Al omitirlos aquí evitamos el ValueError que
        # hace que uvicorn falle silenciosamente en el daemon thread.
        pass


def run_dashboard() -> None:
    port = int(os.environ.get("PORT", "8000"))
    config = uvicorn.Config(
        dashboard_app,
        host="0.0.0.0",
        port=port,
        log_level="info",      # info para ver "Uvicorn running on …" en logs
    )
    server = DashboardServer(config=config)
    asyncio.run(server.serve())


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    # Dashboard en hilo demonio (muere si el bot muere)
    t = threading.Thread(target=run_dashboard, daemon=True, name="dashboard")
    t.start()
    logger.info(f"🌐 Dashboard arrancando en puerto {os.environ.get('PORT', '8000')}")

    # Bot en hilo principal (blocking — Railway lo reinicia si falla)
    from bot import main as bot_main
    bot_main()

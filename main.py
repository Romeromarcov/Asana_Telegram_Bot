"""
Entry point — corre el bot de Telegram + el panel web en el mismo proceso.

  Dashboard  → hilo PRINCIPAL  (uvicorn + FastAPI, Railway expone el PORT aquí)
  Bot        → hilo DAEMON      (PTB long-polling, crea su propio event loop)

Por qué así:
  - Railway hace health-check al PORT inmediatamente al arrancar.
  - Uvicorn solo puede registrar SIGTERM/SIGINT desde el hilo principal.
  - PTB internamente llama asyncio.run() → funciona perfecto en un thread aparte.
"""

import os
import threading
import logging

import uvicorn
from dashboard import app as dashboard_app

logger = logging.getLogger(__name__)


def run_bot() -> None:
    """Corre el bot de Telegram en su propio hilo (crea su propio event loop)."""
    try:
        from bot import main as bot_main
        bot_main()
    except Exception as e:
        logger.error(f"❌ Bot error: {e}", exc_info=True)


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    # Bot en hilo daemon (muere si el proceso principal muere)
    t = threading.Thread(target=run_bot, daemon=True, name="bot")
    t.start()
    logger.info("🤖 Bot Telegram iniciando en hilo daemon…")

    # Dashboard en hilo PRINCIPAL — Railway ve el PORT aquí
    port = int(os.environ.get("PORT", "8000"))
    logger.info(f"🌐 Dashboard arrancando en puerto {port}…")
    uvicorn.run(
        dashboard_app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )

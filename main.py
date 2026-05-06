"""
Desarrollo local: arranca dashboard + bot en el mismo proceso.
En Railway se usan dos servicios separados:
  - Web    → uvicorn dashboard:app --host 0.0.0.0 --port $PORT
  - Worker → python bot_runner.py
"""
import os
import threading
import logging
import uvicorn
from dashboard import app


def run_bot():
    from bot import main as bot_main
    bot_main()


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    t = threading.Thread(target=run_bot, daemon=True, name="bot")
    t.start()
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

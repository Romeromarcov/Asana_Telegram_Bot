"""
Entry point del Bot de Telegram — Railway Worker service.
No expone ningún puerto web. Solo hace long-polling a Telegram.
"""
import logging
from bot import main as bot_main

if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    bot_main()

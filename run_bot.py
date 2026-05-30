"""Run the Telegram bot."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.makedirs("data", exist_ok=True)
os.makedirs("logs", exist_ok=True)

import asyncio
from bot.main import main

if __name__ == "__main__":
    asyncio.run(main())

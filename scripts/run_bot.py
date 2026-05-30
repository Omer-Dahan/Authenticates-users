"""Run the Telegram bot."""
import sys
import os

# Set up project root directory in python path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

# Create data and logs directories in project root
os.makedirs(os.path.join(project_root, "data"), exist_ok=True)
os.makedirs(os.path.join(project_root, "logs"), exist_ok=True)

import asyncio
from bot.main import main

if __name__ == "__main__":
    asyncio.run(main())

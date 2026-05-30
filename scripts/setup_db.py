"""Quick setup: initializes the database with default data."""
import asyncio
import sys
import os

# Set up project root directory in python path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

# Create data and logs directories in project root
os.makedirs(os.path.join(project_root, "data"), exist_ok=True)
os.makedirs(os.path.join(project_root, "logs"), exist_ok=True)


async def main():
    print("Initializing database...")
    from database.init_db import init_db
    await init_db()
    print("Database initialized successfully with default rules, languages, and config.")
    print("\nDefault configuration loaded:")
    print("  - 4 moderation rules (Hebrew/Arabic name detection, suspicious username, empty name)")
    print("  - 10 language filters (Hebrew, Arabic, Russian, etc.)")
    print("  - 50 Israeli names for fuzzy matching")
    print("  - 15 blacklist keywords (crypto, forex, casino, etc.)")
    print("  - 1 verification question")
    print("  - Full system configuration")


if __name__ == "__main__":
    asyncio.run(main())

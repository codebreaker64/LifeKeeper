"""set_commands.py — register the bot's command menu with Telegram.

Commands persist on Telegram's servers, so this only needs to run once per
bot (deploy.sh also does it via curl on deploy).

Usage:
    python scripts/set_commands.py
"""

import asyncio
import sys

sys.path.insert(0, ".")  # run from repo root

from app import telegram_client as tg  # noqa: E402

if __name__ == "__main__":
    ok = asyncio.run(tg.set_my_commands())
    # Plain ASCII: Windows consoles often run cp1252, which chokes on symbols.
    print("Command menu registered" if ok else "setMyCommands failed, see log")

import time
import asyncio
import logging

logger = logging.getLogger(__name__)

# Updated every successful ping; the health route considers the bot
# "down" only if this hasn't been refreshed in a long while. This avoids
# treating Pyrogram's normal brief reconnects as an outage and triggering
# needless platform restarts (which was itself causing a restart loop).
_last_ok = time.time()

# How long the bot may go without a successful ping before we call it
# genuinely stuck. Keep this well above Pyrogram's normal reconnect time.
STALE_AFTER_SECONDS = 150


def mark_ok():
    global _last_ok
    _last_ok = time.time()


def is_bot_healthy():
    return (time.time() - _last_ok) < STALE_AFTER_SECONDS


async def run_watchdog(bot):
    """Periodically confirms the bot can actually talk to Telegram."""
    while True:
        try:
            await asyncio.wait_for(bot.get_me(), timeout=15)
            mark_ok()
        except Exception as e:
            logger.warning(f"[WATCHDOG] ping failed: {e}")
        await asyncio.sleep(30)

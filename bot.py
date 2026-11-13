import asyncio
import random
import logging
import logging.config
from datetime import date, datetime

import pytz
from aiohttp import web
from pyrogram import idle
from pyrogram.errors import FloodWait
from pyrogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

# Get logging configurations
logging.config.fileConfig('logging.conf')
logging.getLogger().setLevel(logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.ERROR)

from config import REPORT_CHANNEL, ON_HEROKU, PORT, ADMINS
from Script import script
from Peter.bot import StreamBot
from Peter.server import web_server
from Peter.utils.keepalive import ping_server
from Peter.utils.watchdog import run_watchdog, mark_ok
from Peter.utils.pending_deletes import restore_pending_deletes


loop = asyncio.get_event_loop()


async def start_bot_with_flood_wait():
    """Log the bot in. If Telegram answers FloodWait (too many recent logins,
    usually caused by a crash/restart loop) we wait it out here instead of
    crashing - every crash makes the platform restart us, which logs in again
    and keeps the flood going."""
    while True:
        try:
            await StreamBot.start()
            return
        except FloodWait as e:
            wait = int(e.value) + 5
            logging.warning(f"Telegram FloodWait on login: waiting {wait} seconds before retrying")
            while wait > 0:
                step = min(wait, 30)
                await asyncio.sleep(step)
                wait -= step
                mark_ok()  # we are alive, just waiting - don't let the health check call us stuck


USER_COMMANDS = [
    BotCommand("start", "🚀 Start The Bot"),
    BotCommand("myplan", "💎 My Plan"),
    BotCommand("support", "💬 Contact Support"),
]
ADMIN_COMMANDS = [
    BotCommand("start", "🚀 Start The Bot"),
    BotCommand("link", "🔗 Create A File Link"),
    BotCommand("batch", "📦 Create A Batch Link"),
    BotCommand("myplan", "💎 My Plan"),
    BotCommand("support", "💬 Contact Support"),
    BotCommand("broadcast", "📢 Send To All Users"),
    BotCommand("send", "✉️ Send To One User"),
    BotCommand("unpin", "📌 Remove Pinned Messages"),
    BotCommand("ban", "🚫 Ban A User"),
    BotCommand("unban", "✅ Unban A User"),
    BotCommand("banlist", "📋 Get The Ban List"),
    BotCommand("status", "📊 Bot Status"),
    BotCommand("settings", "⚙️ Open Settings"),
    BotCommand("padmin", "🛠 Premium Admin"),
    BotCommand("promo", "🎟 Promo Codes"),
    BotCommand("restart", "🔄 Restart The Bot"),
]


async def set_command_menus(bot):
    """Everyone sees only /start, /myplan and /support in the command menu; the owners and the admins added
    from /settings see the full list. Commands still work when typed, this only changes the menu."""
    try:
        await bot.set_bot_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    except Exception as e:
        logging.warning(f"Couldn't set the default command menu: {type(e).__name__}: {e}")
    ids = {a for a in ADMINS if isinstance(a, int)}
    try:
        from plugins.admins_db import get_all_admins
        ids |= {int(a["_id"]) for a in await get_all_admins()}
    except Exception:
        pass
    for uid in ids:
        try:
            await bot.set_bot_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=uid))
        except Exception as e:
            logging.info(f"Admin command menu skipped for {uid}: {type(e).__name__}")


async def start():
    print('\n')
    print('Initializing Peter Bot')
    mark_ok()

    # Start the web server FIRST so the host's health check (/) answers while
    # the bot is still logging in (or waiting out a FloodWait).
    StreamBot.username = None
    app = web.AppRunner(await web_server())
    await app.setup()
    bind_address = "0.0.0.0"
    await web.TCPSite(app, bind_address, PORT).start()

    await start_bot_with_flood_wait()
    me = await StreamBot.get_me()
    StreamBot.username = me.username
    mark_ok()
    asyncio.create_task(set_command_menus(StreamBot))
    asyncio.create_task(run_watchdog(StreamBot))
    # Files that were due for auto-delete while the bot was down / restarting
    asyncio.create_task(restore_pending_deletes(StreamBot))
    # Heartbeat + this bot's part of "All Bots" broadcasts (plugins/broadcast.py)
    from plugins.broadcast import cross_bot_loop
    asyncio.create_task(cross_bot_loop(StreamBot))
    # Premium payments: checks the FamApp emails for pending orders (plugins/premium.py)
    from plugins.premium import start_poller
    start_poller(StreamBot)
    # NOTE: plugins are already loaded by Pyrogram itself (plugins={"root": "plugins"}
    # in Peter/bot/__init__.py) when StreamBot.start() runs above. The old manual
    # importlib loop re-executed every plugin a second time (duplicate Mongo clients).
    if ON_HEROKU:
        asyncio.create_task(ping_server())
    tz = pytz.timezone('Asia/Kolkata')
    today = date.today()
    now = datetime.now(tz)
    time = now.strftime("%H:%M:%S %p")
    try:
        await StreamBot.send_message(chat_id=REPORT_CHANNEL, text=script.RESTART_TXT.format(today, time))
    except Exception as e:
        # A problem with the log channel must not take the whole bot down.
        logging.warning(f"Couldn't send the restart message to REPORT_CHANNEL: {type(e).__name__}: {e}")
    print("Bot Started Successfully!")
    await idle()
    # idle() returns on SIGTERM / SIGINT (docker stop / down / restart, server shutdown).
    # All bots stop at the same moment and share one report channel, so each bot waits a
    # random 0-6 seconds first and the send may take up to 20 seconds (a short FloodWait is
    # slept out by Pyrogram). docker-compose needs stop_grace_period: 30s for this.
    try:
        await asyncio.sleep(random.uniform(0, 6))
        await asyncio.wait_for(
            StreamBot.send_message(chat_id=REPORT_CHANNEL, text="<b>🛑 Bot Stopped</b>"), timeout=20)
    except Exception as e:
        logging.warning(f"Couldn't send the stopped message to REPORT_CHANNEL: {type(e).__name__}: {e}")
    try:
        await asyncio.wait_for(StreamBot.stop(), timeout=4)
    except Exception:
        pass


if __name__ == '__main__':
    try:
        loop.run_until_complete(start())
    except KeyboardInterrupt:
        logging.info('Service Stopped Bye 👋')

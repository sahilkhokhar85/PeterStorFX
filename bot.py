import asyncio
import logging
import logging.config
from datetime import date, datetime

import pytz
from aiohttp import web
from pyrogram import idle
from pyrogram.errors import FloodWait

# Get logging configurations
logging.config.fileConfig('logging.conf')
logging.getLogger().setLevel(logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.ERROR)

from config import LOG_CHANNEL, ON_HEROKU, PORT
from Script import script
from TechVJ.bot import StreamBot
from TechVJ.server import web_server
from TechVJ.utils.keepalive import ping_server
from TechVJ.utils.watchdog import run_watchdog, mark_ok


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


async def start():
    print('\n')
    print('Initalizing Tech VJ Bot')
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
    asyncio.create_task(run_watchdog(StreamBot))
    # NOTE: plugins are already loaded by Pyrogram itself (plugins={"root": "plugins"}
    # in TechVJ/bot/__init__.py) when StreamBot.start() runs above. The old manual
    # importlib loop re-executed every plugin a second time (duplicate Mongo clients).
    if ON_HEROKU:
        asyncio.create_task(ping_server())
    tz = pytz.timezone('Asia/Kolkata')
    today = date.today()
    now = datetime.now(tz)
    time = now.strftime("%H:%M:%S %p")
    try:
        await StreamBot.send_message(chat_id=LOG_CHANNEL, text=script.RESTART_TXT.format(today, time))
    except Exception as e:
        # A problem with the log channel must not take the whole bot down.
        logging.warning(f"Couldn't send the restart message to LOG_CHANNEL: {type(e).__name__}: {e}")
    print("Bot Started Successfully!")
    await idle()


if __name__ == '__main__':
    try:
        loop.run_until_complete(start())
    except KeyboardInterrupt:
        logging.info('Service Stopped Bye 👋')

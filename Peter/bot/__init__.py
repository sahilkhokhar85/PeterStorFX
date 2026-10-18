from pyrogram import Client, types
from config import *
from typing import Union, Optional, AsyncGenerator
from contextvars import ContextVar

# Set to True around copies that must reach users unchanged (broadcast staging)
NO_SIGN = ContextVar("NO_SIGN", default=False)

class StreamXBot(Client):

    def __init__(self):
        super().__init__(
            name=f"peterfilestore_{BOT_ID}",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            workers=50,
            plugins={"root": "plugins"},
            sleep_threshold=10,
        )
    async def send_message(self, chat_id, text=None, *args, **kwargs):
        """Reports go to one shared REPORT_CHANNEL, so with several bots every
        report is signed with the bot it came from."""
        if chat_id == REPORT_CHANNEL and isinstance(text, str) and not NO_SIGN.get():
            username = getattr(self.me, "username", None) if getattr(self, "me", None) else None
            if username:
                text = f"{text.rstrip()}\n\n<b>🤖 Bot: @{username}</b>"
        return await super().send_message(chat_id, text, *args, **kwargs)

    async def iter_messages(
        self,
        chat_id: Union[int, str],
        limit: int,
        offset: int = 0,
    ) -> Optional[AsyncGenerator["types.Message", None]]:
        """Iterate through a chat sequentially.
        This convenience method does the same as repeatedly calling :meth:`~pyrogram.Client.get_messages` in a loop, thus saving
        you from the hassle of setting up boilerplate code. It is useful for getting the whole chat messages with a
        single call.
        Parameters:
            chat_id (``int`` | ``str``):
                Unique identifier (int) or username (str) of the target chat.
                For your personal cloud (Saved Messages) you can simply use "me" or "self".
                For a contact that exists in your Telegram address book you can use his phone number (str).
                
            limit (``int``):
                Identifier of the last message to be returned.
                
            offset (``int``, *optional*):
                Identifier of the first message to be returned.
                Defaults to 0.
        Returns:
            ``Generator``: A generator yielding :obj:`~pyrogram.types.Message` objects.
        Example:
            .. code-block:: python
                for message in app.iter_messages("pyrogram", 1, 15000):
                    print(message.text)
        """
        current = offset
        while True:
            new_diff = min(200, limit - current)
            if new_diff <= 0:
                return
            messages = await self.get_messages(chat_id, list(range(current, current+new_diff+1)))
            for message in messages:
                yield message
                current += 1
      
StreamBot = StreamXBot()

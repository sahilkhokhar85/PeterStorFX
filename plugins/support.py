from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

SUPPORT_BOT = "ChillFlizOwnerBot"
OWNER = "RudraBlaze"


@Client.on_message(filters.command("support") & filters.private)
async def support_cmd(bot, message):
    await message.reply_text(
        "<b>💬 Contact Support !!\n\n"
        "Need Help, Have A Request, Query Or Feedback?\n"
        "Payment Or Premium Issue? We're Here To Help."
        "👇 Let's Talk, Message Me Below.</b>",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🤖 Support Bot", url=f"https://t.me/{SUPPORT_BOT}"),
            InlineKeyboardButton("👨‍💻 Owner", url=f"https://t.me/{OWNER}")]]))

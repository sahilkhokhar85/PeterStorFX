

class script(object):
    START_TXT = """<b>Hey {} !! 💌

Welcome To ChillFliz File Store Bot! ✨❤️

📂 Access Your Content Easily Through The Links Provided. ⚡

👨‍💻 Powered By @ChillFlizX

💬 Need Help, Have A Request, Query, Or Feedback? We're Here To Help.

🧑‍🔧 Contact >>> @ChillFlizOwnerBot</b>"""

    
    CAPTION = """<b>{file_caption}</b>
<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>
<b>😘Join - Share - Like >>> @ChillFlizX⚡</b>"""



    ABOUT_TXT = """<b>ɪ ᴀᴍ ᴀ ᴄʜɪʟʟғʟɪᴢ ғɪʟᴇ sᴛᴏʀᴇ ʙᴏᴛ ғᴏʀ ᴀʟʟ ᴄᴏɴᴛᴇɴᴛs. ᴀᴄᴄᴇss ʏᴏᴜʀ ᴄᴏɴᴛᴇɴᴛ ᴇᴀsɪʟʏ ᴛʜʀᴏᴜɢʜ ᴛʜᴇ ɪɪɴᴋs ᴘʀᴏᴠɪᴅᴇᴅ.

🤖 ᴍʏ ɴᴀᴍᴇ: {}

📝 ʟᴀɴɢᴜᴀɢᴇ: <a href=https://www.python.org>Python3</a>

📚 ʟɪʙʀᴀʀʏ: <a href=https://docs.pyrogram.org>Pyrogram</a>

👨‍💻 ᴅᴇᴠᴇʟᴏᴘᴇʀ: <a href=https://t.me/RudraBlaze>DEVIL</a>

👥 Support: <a href=https://t.me/ChillFlizOwnerBot>Contact Bot</a>

📢 ᴜᴘᴅᴀᴛᴇ ᴄʜᴀɴɴᴇʟ: <a href=https://t.me/ChillFlizX>ChillFliz</a></b>
"""

    CABOUT_TXT = """<b>ʜɪ ɪ ᴀᴍ ᴘᴇʀᴍᴀɴᴇɴᴛ ғɪʟᴇ sᴛᴏʀᴇ ʙᴏᴛ ᴡɪᴛʜ ᴄʟᴏɴᴇ ғᴇᴀᴛᴜʀᴇ + ᴄᴜsᴛᴏᴍ ᴜʀʟ sʜᴏʀᴛɴᴇʀ ɪᴛ ᴍᴇᴀɴs ᴀɴʏ ᴜsᴇʀ ᴄᴀɴ sᴇᴛ ʜɪs ᴜʀʟ sʜᴏʀᴛɴᴇʀ ᴀɴᴅ + ᴀᴜᴛᴏ ᴅᴇʟᴇᴛᴇ.

🤖 ᴍʏ ɴᴀᴍᴇ: {}

📝 ʟᴀɴɢᴜᴀɢᴇ: <a href=https://www.python.org>Python3</a>

📚 ʟɪʙʀᴀʀʏ: <a href=https://docs.pyrogram.org>Pyrogram</a>

🧑🏻‍💻 ᴅᴇᴠᴇʟᴏᴘᴇʀ: <a href=tg://user?id={}>ᴅᴇᴠᴇʟᴏᴘᴇʀ</a></b>
"""


    HELP_TXT = """<b><u>💢 HOW TO USE THE BOT ☺️</u>

🔻 /link - ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴠɪᴅᴇᴏ ᴏʀ ғɪʟᴇ ᴛᴏ ɢᴇᴛ sʜᴀʀᴀʙʟᴇ ʟɪɴᴋ

🔻 /batch - ᴄʜᴏᴏsᴇ ᴀ ᴍᴇᴛʜᴏᴅ: sᴇɴᴅ ᴍᴇssᴀɢᴇs ᴏɴᴇ ʙʏ ᴏɴᴇ ᴀɴᴅ ᴘʀᴇss ᴅᴏɴᴇ, ᴏʀ sᴇɴᴅ ᴛʜᴇ ғɪʀsᴛ ᴀɴᴅ ʟᴀsᴛ ᴍᴇssᴀɢᴇ (ғᴏʀᴡᴀʀᴅ ᴏʀ ʟɪɴᴋ) ᴏғ ᴀ ᴄʜᴀɴɴᴇʟ ʀᴀɴɢᴇ. ʙᴏᴛ ᴍᴜsᴛ ʙᴇ ᴀᴅᴍɪɴ ɪɴ ᴛʜᴀᴛ ᴄʜᴀɴɴᴇʟ.

🔻 /broadcast - ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴍᴇssᴀɢᴇ ᴛᴏ ʙʀᴏᴀᴅᴄᴀsᴛ (ʙᴏᴛ ᴏᴡɴᴇʀ ᴏɴʟʏ)

🔻 /ban (user_id) - ʙᴀɴ ᴀ ᴜsᴇʀ ғʀᴏᴍ ᴛʜᴇ ʙᴏᴛ (ᴀᴅᴍɪɴ ᴏɴʟʏ)

🔻 /unban (user_id) - ᴜɴʙᴀɴ ᴀ ᴘʀᴇᴠɪᴏᴜsʟʏ ʙᴀɴɴᴇᴅ ᴜsᴇʀ (ᴀᴅᴍɪɴ ᴏɴʟʏ)

🔻 /status - ᴠɪᴇᴡ ʙᴏᴛ sᴛᴀᴛᴜs: ᴜsᴇʀs, ʙᴀɴ ᴜsᴇʀs, ᴄᴘᴜ, ʀᴀᴍ, ᴜᴘᴛɪᴍᴇ (ᴀᴅᴍɪɴ ᴏɴʟʏ)

🔻 /settings - ᴏᴘᴇɴ ᴀᴅᴍɪɴ sᴇᴛᴛɪɴɢs ᴘᴀɴᴇʟ: ꜰᴏʀᴄᴇ sᴜʙsᴄʀɪʙᴇ, ᴀᴅᴍɪɴs, ʙᴏᴛ sᴛᴀᴛᴜs, ʀᴇsᴛᴀʀᴛ (ᴀᴅᴍɪɴ ᴏɴʟʏ)</b>"""


    CHELP_TXT = """<b>💢 Hᴏᴡ Tᴏ Usᴇ Tʜɪs Bᴏᴛ ☺️

🔻 /link - ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴠɪᴅᴇᴏ ᴏʀ ғɪʟᴇ ᴛᴏ ɢᴇᴛ sʜᴀʀᴀʙʟᴇ ʟɪɴᴋ

🔻 /broadcast - ʀᴇᴘʟʏ ᴛᴏ ᴛʜɪs ᴄᴏᴍᴍᴀɴᴅ ᴛᴏ ᴀ ᴍᴇssᴀɢᴇ ᴛᴏ ʙʀᴏᴀᴅᴄᴀsᴛ (ʙᴏᴛ ᴏᴡɴᴇʀ ᴏɴʟʏ)</b>"""


    LOG_TEXT = """<b>#NewUser</b>

<b>ID -</b> <code>{}</code>

<b>Nᴀᴍᴇ - {}</b>"""
    RESTART_TXT = """<b>Bᴏᴛ Rᴇsᴛᴀʀᴛᴇᴅ !</b>

<b>📅 Dᴀᴛᴇ :</b> <code>{}</code>
<b>⏰ Tɪᴍᴇ :</b> <code>{}</code>
<b>🌐 Tɪᴍᴇᴢᴏɴᴇ :</b> <code>Asia/Kolkata</code>
<b>🛠️ Bᴜɪʟᴅ Sᴛᴀᴛᴜs:</b> <code>v2.7.1 [ Sᴛᴀʙʟᴇ ]</code>"""


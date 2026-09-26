

class script(object):
    START_TXT = """<b>Hey {} !! 💌

Welcome To ChillFliz File Store Bot! ✨❤️

📂 Access Your Content Easily Through The Links Provided. ⚡

👨‍💻 Powered By @ChillFlizX

💬 Need Help, Have A Request, Query, Or Feedback? We're Here To Help.

🧑‍🔧 Contact >>> @ChillFlizOwnerBot</b>"""

    
    CAPTION = """<b>{file_caption}</b>
<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>
<b>😘Join - Share - Like >>> @ChillFlizX⚡</b>"""


    SHORTENER_API_MESSAGE = """<b>Tᴏ ᴀᴅᴅ ᴏʀ ᴜᴘᴅᴀᴛᴇ ʏᴏᴜʀ Sʜᴏʀᴛɴᴇʀ Wᴇʙsɪᴛᴇ API, /api (ᴀᴘɪ)
            
<b>Ex: /api 2819a959174f367d970d3fab75634fa88f8b8f25

<b>Cᴜʀʀᴇɴᴛ Wᴇʙsɪᴛᴇ: {base_site}

Cᴜʀʀᴇɴᴛ Sʜᴏʀᴛᴇɴᴇʀ API:</b> `{shortener_api}`

If You Want To Remove Api Then Copy This And Send To Bot - `/api None`"""


    ABOUT_TXT = """<b>ɪ ᴀᴍ ᴀ ᴍʀɴ ꜱᴛᴏʀᴛᴠ ꜰᴏʀ ᴛᴠ ꜱʜᴏᴡꜱ. ɪ ᴄᴀɴ ꜱᴛᴏʀᴇ ꜰɪʟᴇꜱ ᴀɴᴅ ᴏᴛʜᴇʀ ᴜꜱᴇʀꜱ ᴄᴀɴ ᴀᴄᴄᴇꜱꜱ ɪᴛ ꜰʀᴏᴍ ᴘʀᴏᴠɪᴅᴇᴅ ʟɪɴᴋ.

🤖 ᴍʏ ɴᴀᴍᴇ: {}

📝 ʟᴀɴɢᴜᴀɢᴇ: <a href=https://www.python.org>Python3</a>

📚 ʟɪʙʀᴀʀʏ: <a href=https://docs.pyrogram.org>Pyrogram</a>

👨‍💻 ᴅᴇᴠᴇʟᴏᴘᴇʀ: <a href=https://t.me/RudraBlaze>DEVIL</a>

👥 Support: <a href=https://t.me/ChillFlizOwnerBot>Contact Bot</a>

📢 ᴜᴘᴅᴀᴛᴇ ᴄʜᴀɴɴᴇʟ: <a href=https://t.me/ChillFlizX></a></b>
"""

    CABOUT_TXT = """<b>ʜɪ ɪ ᴀᴍ ᴘᴇʀᴍᴀɴᴇɴᴛ ғɪʟᴇ sᴛᴏʀᴇ ʙᴏᴛ ᴡɪᴛʜ ᴄʟᴏɴᴇ ғᴇᴀᴛᴜʀᴇ + ᴄᴜsᴛᴏᴍ ᴜʀʟ sʜᴏʀᴛɴᴇʀ ɪᴛ ᴍᴇᴀɴs ᴀɴʏ ᴜsᴇʀ ᴄᴀɴ sᴇᴛ ʜɪs ᴜʀʟ sʜᴏʀᴛɴᴇʀ ᴀɴᴅ + ᴀᴜᴛᴏ ᴅᴇʟᴇᴛᴇ.

🤖 ᴍʏ ɴᴀᴍᴇ: {}

📝 ʟᴀɴɢᴜᴀɢᴇ: <a href=https://www.python.org>Python3</a>

📚 ʟɪʙʀᴀʀʏ: <a href=https://docs.pyrogram.org>Pyrogram</a>

🧑🏻‍💻 ᴅᴇᴠᴇʟᴏᴘᴇʀ: <a href=tg://user?id={}>ᴅᴇᴠᴇʟᴏᴘᴇʀ</a></b>
"""


    HELP_TXT = """<b><u>💢 HOW TO USE THE BOT ☺️</u>

🔻 /link - ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴠɪᴅᴇᴏ ᴏʀ ғɪʟᴇ ᴛᴏ ɢᴇᴛ sʜᴀʀᴀʙʟᴇ ʟɪɴᴋ

🔻 /batch - sᴇɴᴅ ғɪʀsᴛ ʟɪɴᴋ ᴏғ ғɪʟᴇ sᴛᴏʀᴇ ᴄʜᴀɴɴᴇʟ ᴘᴏsᴛ ᴛʜᴇɴ ʟᴀsᴛ ᴘᴏsᴛ ʟɪɴᴋ ᴀɴᴅ ᴍᴀᴋᴇ sᴜʀᴇ ʙᴏᴛ ɪs ᴀᴅᴍɪɴ ɪɴ ʏᴏᴜʀ ғɪʟᴇ sᴛᴏʀᴇ ᴄʜᴀɴɴᴇʟ.
ᴇx - /batch https://t.me/Mrn_Officialx

🔻 /base_site - ᴜsᴇ ᴛʜɪs ᴄᴏᴍᴍᴀɴᴅ ᴛᴏ sᴇᴛ ᴜʀʟ sʜᴏʀᴛɴᴇʀ ʟɪɴᴋ ᴅᴏᴍᴀɪɴ 
ᴇx - /base_site ʏᴏᴜʀᴅᴏᴍᴀɪɴ.ᴄᴏᴍ

🔻 /api - sᴇᴛ ʏᴏᴜʀ ᴜʀʟ sʜᴏʀᴛɴᴇʀ ᴀᴄᴄᴏᴜɴᴛ ᴀᴘɪ 
ᴇx - /api ʙᴀᴏᴡɢᴡᴋʟᴀᴀʙᴀᴋʟ

🔻 /broadcast - ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴍᴇssᴀɢᴇ ᴛᴏ ʙʀᴏᴀᴅᴄᴀsᴛ (ʙᴏᴛ ᴏᴡɴᴇʀ ᴏɴʟʏ)

🔻 /ban (user_id) - ʙᴀɴ ᴀ ᴜsᴇʀ ғʀᴏᴍ ᴛʜᴇ ʙᴏᴛ (ᴀᴅᴍɪɴ ᴏɴʟʏ)

🔻 /unban (user_id) - ᴜɴʙᴀɴ ᴀ ᴘʀᴇᴠɪᴏᴜsʟʏ ʙᴀɴɴᴇᴅ ᴜsᴇʀ (ᴀᴅᴍɪɴ ᴏɴʟʏ)

🔻 /status - ᴠɪᴇᴡ ʙᴏᴛ sᴛᴀᴛᴜs: ᴜsᴇʀs, ʙᴀɴ ᴜsᴇʀs, ᴄᴘᴜ, ʀᴀᴍ, ᴜᴘᴛɪᴍᴇ (ᴀᴅᴍɪɴ ᴏɴʟʏ)

🔻 /settings - ᴏᴘᴇɴ ᴀᴅᴍɪɴ sᴇᴛᴛɪɴɢs ᴘᴀɴᴇʟ: ꜰᴏʀᴄᴇ sᴜʙsᴄʀɪʙᴇ, ᴘʀᴏᴛᴇᴄᴛ ᴄᴏɴᴛᴇɴᴛ, ᴀᴜᴛᴏ ᴅᴇʟᴇᴛᴇ, ᴄᴀᴘᴛɪᴏɴ, sᴛᴀʀᴛ ᴍᴇssᴀɢᴇ (ᴀᴅᴍɪɴ ᴏɴʟʏ)</b>"""


    CHELP_TXT = """<b>💢 Hᴏᴡ Tᴏ Usᴇ Tʜɪs Bᴏᴛ ☺️

🔻 /link - ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴠɪᴅᴇᴏ ᴏʀ ғɪʟᴇ ᴛᴏ ɢᴇᴛ sʜᴀʀᴀʙʟᴇ ʟɪɴᴋ

🔻 /base_site - ᴜsᴇ ᴛʜɪs ᴄᴏᴍᴍᴀɴᴅ ᴛᴏ sᴇᴛ ᴜʀʟ sʜᴏʀᴛɴᴇʀ ʟɪɴᴋ ᴅᴏᴍᴀɪɴ
ᴇx - /base_site ʏᴏᴜʀᴅᴏᴍᴀɪɴ.ᴄᴏᴍ

🔻 /api - sᴇᴛ ʏᴏᴜʀ ᴜʀʟ sʜᴏʀᴛɴᴇʀ ᴀᴄᴄᴏᴜɴᴛ ᴀᴘɪ
ᴇx - /api ʙᴀᴏᴡɢᴡᴋʟᴀᴀʙᴀᴋʟ

🔻 /broadcast - ʀᴇᴘʟʏ ᴛᴏ ᴛʜɪs ᴄᴏᴍᴍᴀɴᴅ ᴛᴏ ᴀ ᴍᴇssᴀɢᴇ ᴛᴏ ʙʀᴏᴀᴅᴄᴀsᴛ (ʙᴏᴛ ᴏᴡɴᴇʀ ᴏɴʟʏ)</b>"""


    LOG_TEXT = """<b>#NewUser
    
ID - <code>{}</code>

Nᴀᴍᴇ - {}</b>
"""
    RESTART_TXT = """
<b>Bᴏᴛ Rᴇsᴛᴀʀᴛᴇᴅ !

📅 Dᴀᴛᴇ : <code>{}</code>
⏰ Tɪᴍᴇ : <code>{}</code>
🌐 Tɪᴍᴇᴢᴏɴᴇ : <code>Asia/Kolkata</code>
🛠️ Bᴜɪʟᴅ Sᴛᴀᴛᴜs: <code>v2.7.1 [ Sᴛᴀʙʟᴇ ]</code></b>"""


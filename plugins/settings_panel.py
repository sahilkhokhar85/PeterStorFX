# /settings admin panel — Force Subscribe, Admins, Bot Status and Restart.
# Everything else (start message, caption, buttons, protect content,
# auto delete, private mode) is fixed in the code / environment variables.

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from config import ADMINS
from plugins.settings_db import (
    get_settings, update_setting, add_force_sub_channel, remove_force_sub_channel,
    touch_last_used, readable_ago,
    force_sub_channel_id, force_sub_channel_mode, force_sub_channel_link, set_force_sub_link
)
from plugins.admins_db import dynamic_admin_filter, is_admin, has_permission, get_all_admins, add_admin, remove_admin, set_permission, PERMISSIONS


def main_menu_text(settings):
    last_used = readable_ago(settings.get("last_used"))
    return (
        "⚙️ <b>ᴄʜɪʟʟꜰʟɪᴢx ʙᴏᴛ ꜱᴇᴛᴛɪɴɢꜱ ᴘᴀɴᴇʟ</b>\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖➖➖\n\n"
        "🍿 <i>Manage force subscribe, admins and bot status from here.</i>\n\n"
        f"⏰ <b>Last Used :</b> {last_used} ago\n\n"
        "👇 <b>Choose an option below</b>"
    )


def main_menu_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 ꜰᴏʀᴄᴇ ꜱᴜʙꜱᴄʀɪʙᴇ", callback_data="adm_fsub"),
         InlineKeyboardButton("👥 ᴀᴅᴍɪɴꜱ", callback_data="adm_admins")],
        [InlineKeyboardButton("📊 ʙᴏᴛ ꜱᴛᴀᴛᴜꜱ", callback_data="adm_status"),
         InlineKeyboardButton("⏱ ʀᴇꜱᴛᴀʀᴛ ʙᴏᴛ", callback_data="adm_restart")],
        [InlineKeyboardButton("✖ ᴄʟᴏꜱᴇ", callback_data="adm_close")],
    ])


@Client.on_message(filters.command(["settings", "customize"]) & dynamic_admin_filter("can_settings"))
async def settings_cmd(client, message: Message):
    settings = await get_settings()
    await touch_last_used()
    await message.reply_text(main_menu_text(settings), reply_markup=main_menu_markup())


@Client.on_callback_query(filters.regex(r"^adm_"))
async def settings_cb(client: Client, query: CallbackQuery):
    try:
        await _settings_cb_inner(client, query)
    finally:
        try:
            await query.answer()
        except Exception:
            pass


async def _settings_cb_inner(client: Client, query: CallbackQuery):
    user = query.from_user
    if not (user.id in ADMINS or await is_admin(user.id)):
        return await query.answer("Admins only!", show_alert=True)

    data = query.data
    settings = await get_settings()

    # Restart and admin management need the "Can Manage Admins" permission
    # (the owners listed in ADMINS always have it).
    if data == "adm_restart" or data.startswith("adm_admin"):
        if not (user.id in ADMINS or await has_permission(user.id, "can_manage_admins")):
            return await query.answer("You don't have permission for this.", show_alert=True)

    # ---------------- Main menu ----------------
    if data == "adm_menu":
        await touch_last_used()
        await query.message.edit_text(
            main_menu_text(settings),
            reply_markup=main_menu_markup()
        )

    elif data == "adm_close":
        await query.message.delete()

    elif data == "adm_status":
        import time, psutil, asyncio
        from plugins.dbusers import db
        from plugins.moderation import BOT_START_TIME, get_readable_time
        total_users = await db.total_users_count()
        total_banned = await db.total_banned_count()
        try:
            cpu = await asyncio.to_thread(psutil.cpu_percent, 0.5)
            ram = psutil.virtual_memory().percent
        except Exception:
            cpu = ram = 0
        uptime = get_readable_time(int(time.time() - BOT_START_TIME))
        text = (
            "<b>🤖 BOT STATUS</b>\n\n"
            f"👤 Users - <code>{total_users}</code>\n"
            f"🚫 Ban Users - <code>{total_banned}</code>\n"
            f"⚙️ CPU - <code>{cpu}%</code>\n"
            f"💾 RAM - <code>{ram}%</code>\n"
            f"⚡ Uptime - <code>{uptime}</code>"
        )
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« ʙᴀᴄᴋ", callback_data="adm_menu")]]
        ))

    # ---------------- Restart Bot ----------------
    elif data == "adm_restart":
        await query.message.edit_text("<b>♻️ Restarting bot, please wait...</b>")
        import os, sys
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # ---------------- Admins ----------------
    elif data == "adm_admins":
        await render_admins_menu(query)

    elif data == "adm_admins_add":
        await query.message.reply_text(
            "<b>Send the user ID to make admin, or forward a message from them.</b>\n/cancel to cancel."
        )
        ans = await client.ask(query.message.chat.id, "")
        if ans.text and ans.text.strip() == "/cancel":
            await ans.reply_text("Cancelled.")
        else:
            target_id = None
            if ans.forward_from:
                target_id = ans.forward_from.id
            elif ans.text:
                try:
                    target_id = int(ans.text.strip())
                except ValueError:
                    pass
            if not target_id:
                await ans.reply_text("<b>❌ Invalid input.</b>")
            elif target_id in ADMINS:
                await ans.reply_text("<b>This user is already an owner-level admin.</b>")
            else:
                await add_admin(target_id)
                await ans.reply_text(f"<b>✅ <code>{target_id}</code> added as admin.</b>")
        await render_admins_menu(query, edit=False)

    elif data == "adm_admins_list":
        admins = await get_all_admins()
        if not admins:
            return await query.answer("No admins added yet (besides the owner).", show_alert=True)
        buttons = []
        for adm in admins:
            buttons.append([InlineKeyboardButton(f"👤 {adm['_id']}", callback_data=f"adm_admin_view_{adm['_id']}")])
        buttons.append([InlineKeyboardButton("« ʙᴀᴄᴋ", callback_data="adm_admins")])
        await query.message.edit_text("<b>Tap an admin to manage:</b>", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("adm_admin_view_"):
        target_id = int(data.replace("adm_admin_view_", "", 1))
        await render_admin_detail(query, target_id)

    elif data.startswith("adm_admin_toggle_"):
        # data format: adm_admin_toggle_<perm>_<id>
        parts = data.split("_")
        target_id = int(parts[-1])
        perm = "_".join(parts[3:-1])
        from plugins.admins_db import get_admin
        adm = await get_admin(target_id)
        current = bool(adm.get(perm, False)) if adm else False
        await set_permission(target_id, perm, not current)
        await render_admin_detail(query, target_id)

    elif data.startswith("adm_admin_remove_"):
        target_id = int(data.replace("adm_admin_remove_", "", 1))
        await remove_admin(target_id)
        await query.answer("Admin removed.", show_alert=True)
        await render_admins_menu(query)

    # ---------------- Force Subscribe ----------------
    elif data == "adm_fsub":
        await render_fsub_menu(query, settings)

    elif data == "adm_fsub_toggle":
        new_state = not settings.get("force_sub", False)
        await update_setting("force_sub", new_state)
        settings["force_sub"] = new_state
        await render_fsub_menu(query, settings)

    elif data == "adm_fsub_add":
        prompt = await query.message.reply_text(
            "<b>Send the channel username (e.g. @ChillFlizX) or channel ID.</b>\n\n"
            "Make sure the bot is an <b>admin</b> in that channel.\n/cancel to cancel."
        )
        ans = await client.ask(query.message.chat.id, "")
        if ans.text and ans.text.strip() == "/cancel":
            await ans.reply_text("Cancelled.")
            settings = await get_settings()
            await render_fsub_menu(query, settings, edit=False)
        elif ans.text:
            channel = ans.text.strip()
            try:
                chat = await client.get_chat(channel)
                member = await client.get_chat_member(chat.id, "me")
                if not member.privileges:
                    await ans.reply_text("<b>⚠️ I must be an admin in that channel first.</b>")
                    settings = await get_settings()
                    await render_fsub_menu(query, settings, edit=False)
                else:
                    await ans.reply_text(
                        f"<i>Choose Force Sub Mode</i>",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("ɴᴏʀᴍᴀʟ ᴍᴏᴅᴇ", callback_data=f"adm_fsub_mode_normal_{chat.id}")],
                            [InlineKeyboardButton("ᴊᴏɪɴ ʀᴇǫᴜᴇꜱᴛ ᴍᴏᴅᴇ", callback_data=f"adm_fsub_mode_request_{chat.id}")],
                        ])
                    )
            except Exception as e:
                await ans.reply_text(f"<b>❌ Couldn't verify that channel.</b>\n<code>{e}</code>")
                settings = await get_settings()
                await render_fsub_menu(query, settings, edit=False)

    elif data.startswith("adm_fsub_mode_"):
        # data format: adm_fsub_mode_<normal|request>_<chat_id>
        parts = data.split("_")
        mode = parts[3]
        chat_id = int(parts[4])
        try:
            chat = await client.get_chat(chat_id)
            title = chat.title
        except Exception:
            title = str(chat_id)

        link = None
        if mode == "request":
            # Join Request Mode needs a link that actually creates a join
            # request (not a normal instant-join link), so the bot can
            # auto-approve it. A plain chat.invite_link would just add the
            # user instantly and never trigger the approval flow.
            try:
                invite = await client.create_chat_invite_link(chat_id, creates_join_request=True, name="Force Sub (Join Request)")
                link = invite.invite_link
            except Exception as e:
                await query.message.edit_text(
                    f"<b>❌ Couldn't create a join-request link for {title}.</b>\n<code>{e}</code>\n\n"
                    "Make sure I'm an admin there with the <b>Invite Users via Link</b> permission.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❮ ʙᴀᴄᴋ", callback_data="adm_fsub")]])
                )
                return

        await add_force_sub_channel(chat_id, mode, link)
        mode_label = "Join Request Mode" if mode == "request" else "Normal Mode"
        await query.message.edit_text(
            f"✨ <i>Successfully Added {title} As Your Force Sub Channel</i>\n<b>Mode:</b> {mode_label}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❮ ʙᴀᴄᴋ", callback_data="adm_fsub")]])
        )

    elif data == "adm_fsub_remove":
        channels = settings.get("force_sub_channels") or []
        if not channels:
            return await query.answer("No channels added yet.", show_alert=True)
        buttons = []
        for entry in channels:
            ch = force_sub_channel_id(entry)
            mode = force_sub_channel_mode(entry)
            try:
                chat = await client.get_chat(ch)
                label = chat.title
            except Exception:
                label = str(ch)
            tag = " (Request)" if mode == "request" else ""
            buttons.append([InlineKeyboardButton(f"❌ {label}{tag}", callback_data=f"adm_fsub_rm_{ch}")])
        buttons.append([InlineKeyboardButton("« ʙᴀᴄᴋ", callback_data="adm_fsub")])
        await query.message.edit_text("<b>Tap a channel to remove it:</b>", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("adm_fsub_rm_"):
        ch = data.replace("adm_fsub_rm_", "", 1)
        try:
            ch = int(ch)
        except ValueError:
            pass
        await remove_force_sub_channel(ch)
        settings = await get_settings()
        await render_fsub_menu(query, settings)

    elif data == "adm_fsub_msg":
        await query.message.reply_text("<b>Send the new Force Subscribe message text.</b>\n/cancel to cancel.")
        ans = await client.ask(query.message.chat.id, "")
        if ans.text and ans.text.strip() != "/cancel":
            await update_setting("force_sub_message", ans.text)
            await ans.reply_text("<b>✅ Force Subscribe message updated.</b>")
        settings = await get_settings()
        await render_fsub_menu(query, settings, edit=False)

    elif data == "adm_fsub_pic":
        await query.message.reply_text("<b>Send a photo to show with the Force Subscribe message.</b>\n/cancel to cancel.")
        ans = await client.ask(query.message.chat.id, "")
        if ans.photo:
            await update_setting("force_sub_photo", ans.photo.file_id)
            await ans.reply_text("<b>✅ Force Subscribe photo updated.</b>")
        elif ans.text and ans.text.strip() == "/cancel":
            await ans.reply_text("Cancelled.")
        else:
            await ans.reply_text("<b>⚠️ That wasn't a photo, nothing changed.</b>")
        settings = await get_settings()
        await render_fsub_menu(query, settings, edit=False)

    elif data == "adm_fsub_pic_rm":
        await update_setting("force_sub_photo", None)
        settings = await get_settings()
        await render_fsub_menu(query, settings)


async def render_fsub_menu(query, settings, edit=True):
    state = "✅ ON" if settings.get("force_sub") else "❌ OFF"
    channels = settings.get("force_sub_channels") or []
    pic_state = "✅ Set" if settings.get("force_sub_photo") else "❌ Not set"
    text = (
        "<b>📢 FORCE SUBSCRIBE</b>\n\n"
        "Users must join the added channel(s) before using the bot.\n\n"
        f"<b>Status:</b> {state}\n"
        f"<b>Channels added:</b> {len(channels)}\n"
        f"<b>Force Pic:</b> {pic_state}"
    )
    buttons = [
        [InlineKeyboardButton("➕ ᴀᴅᴅ ᴄʜᴀɴɴᴇʟ", callback_data="adm_fsub_add"),
         InlineKeyboardButton("➖ ʀᴇᴍᴏᴠᴇ ᴄʜᴀɴɴᴇʟ", callback_data="adm_fsub_remove")],
        [InlineKeyboardButton("ᴛᴏɢɢʟᴇ ᴏɴ/ᴏꜰꜰ", callback_data="adm_fsub_toggle")],
        [InlineKeyboardButton("✏️ ᴇᴅɪᴛ ᴍᴇꜱꜱᴀɢᴇ", callback_data="adm_fsub_msg")],
        [InlineKeyboardButton("🖼 ꜱᴇᴛ ꜰᴏʀᴄᴇ ᴘɪᴄ", callback_data="adm_fsub_pic"),
         InlineKeyboardButton("🗑 ʀᴇᴍᴏᴠᴇ ᴘɪᴄ", callback_data="adm_fsub_pic_rm")],
        [InlineKeyboardButton("« ʙᴀᴄᴋ", callback_data="adm_menu")],
    ]
    markup = InlineKeyboardMarkup(buttons)
    if edit:
        await query.message.edit_text(text, reply_markup=markup)
    else:
        await query.message.reply_text(text, reply_markup=markup)


async def render_admins_menu(query, edit=True):
    admins = await get_all_admins()
    text = (
        "<b>👥 ADMINS</b>\n\n"
        "Add extra admins and control what each one can do.\n"
        f"<b>Extra admins added:</b> {len(admins)}"
    )
    buttons = [
        [InlineKeyboardButton("➕ ᴀᴅᴅ ᴀᴅᴍɪɴ", callback_data="adm_admins_add")],
        [InlineKeyboardButton("📋 ᴍᴀɴᴀɢᴇ ᴀᴅᴍɪɴꜱ", callback_data="adm_admins_list")],
        [InlineKeyboardButton("« ʙᴀᴄᴋ", callback_data="adm_menu")],
    ]
    markup = InlineKeyboardMarkup(buttons)
    if edit:
        await query.message.edit_text(text, reply_markup=markup)
    else:
        await query.message.reply_text(text, reply_markup=markup)


async def render_admin_detail(query, target_id):
    from plugins.admins_db import get_admin
    adm = await get_admin(target_id) or {}
    text = f"<b>👤 Admin: <code>{target_id}</code></b>\n\nTap a permission to toggle it:"
    buttons = []
    for perm, label in PERMISSIONS.items():
        state = "✅" if adm.get(perm) else "❌"
        buttons.append([InlineKeyboardButton(f"{state} {label}", callback_data=f"adm_admin_toggle_{perm}_{target_id}")])
    buttons.append([InlineKeyboardButton("🗑 ʀᴇᴍᴏᴠᴇ ᴀᴅᴍɪɴ", callback_data=f"adm_admin_remove_{target_id}")])
    buttons.append([InlineKeyboardButton("« ʙᴀᴄᴋ", callback_data="adm_admins_list")])
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))

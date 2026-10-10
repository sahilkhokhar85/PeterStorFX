<p align="center">
  <img src="https://files.catbox.moe/7121om.jpg" alt="ChillFlizX Logo" width="320">
</p>

<h1 align="center">🎬 ChillFlizX File Store Bot</h1>

<p align="center">
  <img src="https://readme-typing-svg.herokuapp.com/?lines=Welcome+To+ChillFlizX;Private+Telegram+File+Store+Bot;Permanent+Links+%7C+Force+Subscribe+%7C+Auto+Delete;Fast+Download+%26+Watch+Online&center=true&width=520" alt="Typing SVG">
</p>

---

## ✨ Features

<details>
<summary><b>Tap to see bot features</b></summary>

<br>

- 🔒 Private bot: only owners and bot admins can create links
- 🔗 Single file links and `/batch` links for many files
- 🌐 Permanent links through the ChillFlizX website / Worker
- 🖼 Video cover (thumbnail) is kept when videos are stored and delivered
- 📢 Force Subscribe (normal mode + join request mode)
- ♻️ Auto delete of delivered files
- 🛡 Token verification with a URL shortener (optional)
- ⚡ Fast Download / Watch Online, built-in streaming
- 👥 Admin panel with permissions, ban / unban, status and broadcast

</details>

## ⚙️ Environment Variables

> ⚠️ `API_HASH`, `DB_URI` and `SHORTLINK_API` have no default value. Set them in your host's variables and never commit them.

<details>
<summary><b>Tap to see environment variables</b></summary>

<br>

**Required**

| Variable | What it is |
|---|---|
| `API_ID`, `API_HASH` | From [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN` | From [BotFather](https://t.me/BotFather) |
| `BOT_USERNAME` | Bot username without @ |
| `DB_URI`, `DB_NAME` | MongoDB connection string and database name |
| `ADMINS` | Owner user ids, space separated |
| `LOG_CHANNEL` | Storage / log channel id, like `-100xxxxxxxxxx` |
| `WEBSITE_URL` | Website / Worker URL used for permanent links |
| `URL` | This bot's public server URL, with `https://` and a trailing `/` |

**Optional**

| Variable | What it is |
|---|---|
| `STREAM_MODE` | `True` / `False`: Fast Download / Watch Online buttons |
| `AUTO_DELETE_MODE` | `True` / `False`: auto delete delivered files |
| `AUTO_DELETE_TIME` | Delete delay in seconds (default `1800`) |
| `CUSTOM_FILE_CAPTION`, `BATCH_FILE_CAPTION` | Caption templates |
| `VERIFY_MODE` | `True` / `False`: token verification |
| `SHORTLINK_URL`, `SHORTLINK_API` | Shortener domain (no `https://`) and API key, only when `VERIFY_MODE` is `True` |
| `VERIFY_TUTORIAL` | Verification tutorial link |
| `PING_INTERVAL` | Keep-alive ping interval in seconds (default `1200`) |
| `PORT` | Web server port (default `8080`) |

If `URL` is empty, `RAILWAY_PUBLIC_DOMAIN` is used.

</details>

## 🤖 Commands

<details>
<summary><b>Tap to see bot commands</b></summary>

<br>

| Command | What it does |
|---|---|
| `/start` | Check the bot is alive / open a file link |
| `/link` | Reply to a file to get its shareable link |
| `/batch` | Link for many files (bot admins only) |
| `/base_site` `/api` | Set your own shortener domain and API key |
| `/broadcast` | Message all users (owner only, reply to a message) |
| `/ban` `/unban` | Ban or unban a user (user id, or reply) |
| `/status` | Users, banned users, CPU, RAM and uptime |
| `/settings` | Admin panel: Force Subscribe, Admins, Status, Restart |
| `/delreq` | Clear recorded join requests |
| `/restart` | Restart the bot |

</details>

## 📄 About

ChillFlizX File Store Bot is based on an open-source file store bot released under the GNU GPL v3 (see `LICENSE`).

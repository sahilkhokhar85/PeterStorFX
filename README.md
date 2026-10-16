<p align="center">
  <img src="https://files.catbox.moe/rgvlnf.jpg" alt="ChillFlizX Logo">
</p>

<h1 align="center">🎬 ChillFlizX File Store Bot</h1>

<p align="center">
  <img src="https://readme-typing-svg.herokuapp.com/?lines=Welcome+To+ChillFlizX;A+Powerful+Telegram+File+Store+Bot;Permanent+Links+%7C+Force+Subscribe+%7C+Auto+Delete;Packed+With+Advanced+Features!;Thank+You!&center=true&width=560" alt="Typing SVG">
</p>

---

## ✨ What This Bot Can Do

<details>
<summary><b>👉 Tap to open the feature list</b></summary>

<br>

| | Feature |
|---|---|
| 🌐 | Permanent links through the ChillFlizX website (premium feature) |
| 📦 | Batch support: link many files at once (bot admins only, from a channel the bot is admin in) |
| ♻️ | Auto delete of delivered files |
| 🖼 | Start message with a picture |
| 📢 | Force Subscribe (normal mode + join request mode) |
| 👥 | Admin settings panel (`/settings`) and dynamic admins with permissions |
| 🚫 | Ban / unban users, bot status and fast broadcast (one click can reach every bot) |
| 🗂 | The database channel shows every stored file with its links right under it |
| ⚡ | Fast Download / Watch Online: built-in streaming, no third-party bin channel needed |
| 🎞 | Video cover (thumbnail) is kept when videos are stored and delivered |
| 🔒 | Private bot: only owners and bot admins can create links |

</details>

## 🔧 Environment Variables

> ⚠️ `API_HASH` and `DB_URI` have no default value. Set them in your host's variables and never commit them.

<details>
<summary><b>👉 Tap to open the variable list</b></summary>

<br>

### Telegram and database

| Variable | Meaning |
|---|---|
| `API_ID` | From [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | From [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN` | From [BotFather](https://t.me/BotFather) |
| `BOT_USERNAME` | Your bot username without @ |
| `DB_URI` | MongoDB connection URL for the main bot |
| `DB_NAME` | MongoDB database name |
| `ADMINS` | Admin / owner ids, space separated (also allowed to broadcast) |
| `LOG_CHANNEL` | Database channel id (starts with `-100`): stored files and batch data only |
| `REPORT_CHANNEL` | Reports channel id (starts with `-100`): new users, file access, restarts, errors. Optional, if empty reports go to `LOG_CHANNEL` |
| `REPORT_COPY_FILES` | `True` (default): the report channel also gets the same file(s) a user received, with the same buttons. `False`: text reports only |

### Links and website

| Variable | Meaning |
|---|---|
| `WEBSITE_URL_MODE` | `True` / `False`: permanent links through the website |
| `WEBSITE_URL` | Your redirect website URL, only if `WEBSITE_URL_MODE` is `True` |
| `STREAM_MODE` | `True` / `False`: Fast Download / Watch Online buttons on delivered files |
| `STREAM_SECRET` | Optional secret for signing links (if empty, derived from `BOT_TOKEN`) |
| `URL` | Your server app link with `https://` and one `/` at the end (also used for Fast Download / Watch Online links) |

### Auto delete and hosting

| Variable | Meaning |
|---|---|
| `AUTO_DELETE_MODE` | `True` / `False`: auto delete delivered files |
| `AUTO_DELETE_TIME` | Delete time in seconds (this one is used by the bot) |
| `PYTHON_VERSION` | Only for Render, value `3.10.8` |
| `PORT` | Only for Render, value `8080` (default `8080`) |

If `URL` is empty, Railway's `RAILWAY_PUBLIC_DOMAIN` is used.

</details>

## 🤖🤖 Running Several Bots

The same code can run as many bots as you like (one Railway service per bot, one BotFather token each).
Visitors who open a permanent link are sent to a **random** bot from your list by the Cloudflare Worker.

Every bot needs the **same** values for: `API_ID`, `API_HASH`, `ADMINS`, `DB_URI`, `DB_NAME`, `LOG_CHANNEL`, `REPORT_CHANNEL`, `WEBSITE_URL`, `PERMANENT_LINK_WORKER_URL`, `PERMANENT_LINK_ADMIN_KEY`.
Every bot needs its **own**: `BOT_TOKEN`, `BOT_USERNAME`, `URL` (its own Railway domain).

- Add every bot as an admin in `LOG_CHANNEL`, `REPORT_CHANNEL` and your force-sub channels.
- Bans are shared: a `/ban` on one bot reaches the others within about 15 seconds.
- Users and auto-delete jobs are kept per bot: a broadcast from one bot reaches the people who started that bot.
- `/broadcast` asks "This Bot Only" or "All Bots". All Bots sends it through every bot that is online, each to its own users, and shows one combined result. The bots need to be admins in `REPORT_CHANNEL` (the message is staged there for a moment).
- Reports in `REPORT_CHANNEL` end with the name of the bot they came from.
- In the Worker, set the `BOT_USERNAMES` variable, for example `bot_one bot_two bot_three` (no @).

## 🤖 Commands

<details>
<summary><b>👉 Tap to open the command list</b></summary>

<br>

| Command | What it does |
|---|---|
| `/start` | Check that the bot is alive, and open file links |
| `/link` | Reply to a file with this to get its shareable link |
| `/batch` | Make one link for many files: `/batch (first post link) (last post link)` |
| `/broadcast` | Broadcast to all users: reply to a message, or send `/broadcast`, then any number of messages, then `/done`. You get a preview first, then choose This Bot Only / All Bots |
| `/send` | `/send user_id`: send messages to one user only |
| `/ban` `/unban` | Ban or unban users: `/ban id1 id2 id3`, reply to a user, or send a `.txt` file of ids. Banned ids (even ones that never used the bot) get no reply at all |
| `/banlist` | Download the full ban list as a `.txt` file |
| `/status` | Users, banned users, CPU, RAM and uptime |
| `/settings` | Admin panel: Force Subscribe, Admins, Bot Status, Restart |
| `/restart` | Restart the bot |

</details>

## 💝 Credits

<details>
<summary><b>👉 Tap to see the credits</b></summary>

<br>

- 🎬 Built, customised and maintained for the **ChillFlizX** community
- 🧩 Based on an open-source file store bot released under the GNU GPL v3 (see `LICENSE`)
- ❤️ Thank you to everyone who tested, reported bugs and supported this journey

</details>

---

<p align="center">© ChillFlizX. All rights reserved.</p>

<p align="center"><b>🚫 Selling this repo, or the code of this repo, for money is strictly prohibited.</b></p>

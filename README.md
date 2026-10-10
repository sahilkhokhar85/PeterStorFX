<p align="center">
  <img src="https://files.catbox.moe/yhzga3.jpg" alt="ChillFlizX Logo">
</p>
<h1 align="center">
  ChillFlizX File Store Bot
</h1>

![Typing SVG](https://readme-typing-svg.herokuapp.com/?lines=Welcome+To+ChillFlizX+File+Store+Bot;A+Highly+Advance+File+Store+Bot;Custom+Url+Shortner+Auto+Delete+Feature;A+Bot+With+Fully+Advanced+Feature!;Thank+You!)
</p>

## Features

<b><details><summary>Tap On Me For Bot Features</summary>
 
- [x] Permanent Link By Using Website [ Premium Feature] 
- [x] Token Verification Feature 
- [x] Custom Url Shortner Support Any User Can Add His Own Shortner
- [x] Batch Support Added, Any User Can Use Batch By Making Bot Admin In His File Store Channel
- [x] Auto Delete Feature Added
- [x] Custom Start Message With Picture And Buttons
- [x] Force Subscribe (Normal Mode + Join Request Mode)
- [x] Admin Settings Panel (`/settings`) + Dynamic Admins With Permissions
- [x] Ban / Unban Users, Bot Status, Fast Broadcast
- [x] Fast Download / Watch Online (built-in streaming, no third-party bin channel required)
</b>
</details>

## Environment Variables

> ⚠️ `API_HASH`, `DB_URI` And `SHORTLINK_API` Have No Default Value Anymore, You Must Set Them In Your Host's Environment Variables.

<b><details><summary>Tap On Me For Environment Variable</summary>

- `API_ID` : Get From [my.telegram.org](https://my.telegram.org)
- `API_HASH` : Get From [my.telegram.org](https://my.telegram.org)
- `BOT_TOKEN` : Get From [BotFather](https://telegram.me/BotFather)
- `BOT_USERNAME` : Your Bot Username Without @
- `DB_URI` : Mongodb Database Url For Main Bot 
- `DB_NAME` : Mongodb Database Name
- `SHORTLINK_URL` : Verify Shortener Domain Without https:// [Only If VERIFY_MODE Is True]
- `SHORTLINK_API` : Verify Shortener API Key [Only If VERIFY_MODE Is True]
- `VERIFY_MODE` : `True` Or `False` (Token Verification)
- `WEBSITE_URL_MODE` : `True` Or `False` (Permanent Link By Website)
- `WEBSITE_URL` : Your Redirect Website Url [Only If WEBSITE_URL_MODE Is True]
- `STREAM_MODE` : `True` Or `False` (Fast Download / Watch Online buttons on delivered files)
- `ADMINS` : It mean Admin/Owner Id For Broadcasting Message.
- `LOG_CHANNEL` : Log channel id start with -100xxxxxx
- `URL` : Your Server App Link With https:// and in last make sure one / is given. [Also Used For Fast Download / Watch Online Links]
- `AUTO_DELETE` : Time In Minutes
- `AUTO_DELETE_TIME` : Time In Seconds
- `PYTHON_VERSION` : This Variable Is Only For Render, Value IS `3.10.8`
- `PORT` : This Variable Is Only For Render, Value IS `8080`
</b>
</details>


## Command To Use Bot

<b><details><summary>Tap On Me For Bot Commands</summary>

🖍️ Main Bot Commands :-

- `/start` : By This Command You Can Check Bot Is Alive Or Not
- `/link` : By This Command You Can Generate A Shareable Link Of File By Replying This Command To That File
- `/batch` : By This Command You Can Generate Multiple File Shareable Link At A Time [Use Like This /batch (first post link) (last post link)]
- `/base_site` : By This Command You Can Set Your Url Shortner Domain [Use Like This /base_site domain.com]
- `/api` : By This Command You Can Set Your Url Shortner Api [Use Like This /api (your api key)]
- `/broadcast` : By Using This Command You Can Broadcast A Message To Your Bot User, Reply This Command To Broadcast Message [Owner Only Command]
- `/ban` `/unban` : Ban Or Unban A User [Use Like This /ban (user id) Or Reply To The User]
- `/status` : Users, Banned Users, CPU, RAM And Uptime
- `/settings` : Admin Settings Panel (Force Sub, Protect Content, Auto Delete, Caption, Start Message, Admins, Bot Mode)
- `/delreq` : Clear All Recorded Join Requests
- `/restart` : Restart The Bot

</b>
</details>

## About

ChillFlizX File Store Bot. Based on an open-source file store bot released under the GNU GPL v3 (see LICENSE).

import re
import os
from os import environ
from Script import script

id_pattern = re.compile(r'^.\d+$')
def is_enabled(value, default):
    if value.lower() in ["true", "yes", "1", "enable", "y"]:
        return True
    elif value.lower() in ["false", "no", "0", "disable", "n"]:
        return False
    else:
        return default
      
# Bot Information
API_ID = int(environ.get("API_ID", ""))
API_HASH = environ.get("API_HASH", "")  # set this in your host's environment variables
BOT_TOKEN = environ.get("BOT_TOKEN", "")

PICS = (environ.get('PICS', 'https://files.catbox.moe/ckoi9u.jpg https://i.postimg.cc/8C15CQ5y/1.png https://i.postimg.cc/gcNtrv0m/2.png https://i.postimg.cc/cHD71BBz/3.png https://i.postimg.cc/F1XYhY8q/4.png https://i.postimg.cc/1tNwGVxC/5.png https://i.postimg.cc/dtW30QpL/6.png https://i.postimg.cc/139dvs3c/7.png https://i.postimg.cc/QtXVtB8K/8.png https://i.postimg.cc/y8j8G1XV/9.png https://i.postimg.cc/zDF6KyJX/10.png https://i.postimg.cc/fyycVqzd/11.png https://i.postimg.cc/26ZBtBZr/13.png https://i.postimg.cc/PJn8nrWZ/14.png https://i.postimg.cc/cC7txyhz/15.png https://i.postimg.cc/kX9tjGXP/16.png https://i.postimg.cc/zXjH4NVb/17.png https://i.postimg.cc/sggGrLhn/18.png https://i.postimg.cc/y8pgYTh7/19.png')).split() # Bot Start Picture
ADMINS = [int(admin) if id_pattern.search(admin) else admin for admin in environ.get('ADMINS', '').split()]
BOT_USERNAME = environ.get("BOT_USERNAME", "") # without @
PORT = environ.get("PORT", "8080")

# Database Information
DB_URI = environ.get("DB_URI", "")  # set this in your host's environment variables
DB_NAME = environ.get("DB_NAME", "")

# Auto Delete Information
AUTO_DELETE_MODE = is_enabled(environ.get('AUTO_DELETE_MODE', "True"), True) # Set True or False

# If Auto Delete Mode Is True Then Fill All Required Variable, If False Then Don't Fill.
AUTO_DELETE = int(environ.get("AUTO_DELETE", "30")) # Time in Minutes
AUTO_DELETE_TIME = int(environ.get("AUTO_DELETE_TIME", "1800")) # Time in Seconds

# Channel Information
LOG_CHANNEL = int(environ.get("LOG_CHANNEL", ""))

# File Caption Information
CUSTOM_FILE_CAPTION = environ.get("CUSTOM_FILE_CAPTION", f"{script.CAPTION}")
BATCH_FILE_CAPTION = environ.get("BATCH_FILE_CAPTION", CUSTOM_FILE_CAPTION)

# Enable - True or Disable - False
PUBLIC_FILE_STORE = is_enabled((environ.get('MZAUTOFILTER', "True")), True)



# Website Info:
WEBSITE_URL_MODE = is_enabled(environ.get('WEBSITE_URL_MODE', "True"), True) # Set True or False

# If Website Url Mode Is True Then Fill All Required Variable, If False Then Don't Fill.
WEBSITE_URL = environ.get("WEBSITE_URL", "")

# Fast Download / Watch Online Info:
STREAM_MODE = is_enabled(environ.get('STREAM_MODE', "True"), True) # Set True or False
# Uses the URL variable below as the public base URL for stream/download links.

PING_INTERVAL = int(environ.get("PING_INTERVAL", "1200"))  # 20 minutes
if 'DYNO' in environ:
    ON_HEROKU = True
else:
    ON_HEROKU = False
# Public base URL of this bot's web server (stream/download links + keep-alive ping).
# Set the URL env var; on Railway we fall back to the service's own public domain.
_railway_domain = environ.get("RAILWAY_PUBLIC_DOMAIN", "")
URL = environ.get("URL") or (f"https://{_railway_domain}/" if _railway_domain else "")

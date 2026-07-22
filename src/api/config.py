"""
Central configuration for the bot. Every secret and every deployment-specific
ID (guild, roles, channels, target GitHub repo) is loaded from the
environment -- populated from the .env file at the project root via
python-dotenv -- so nothing here is hardcoded.
"""

import os
from datetime import timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} is not set. Add it to your .env file.")
    return value


def _require_int(name: str) -> int:
    value = _require(name)
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {value!r}.")


# Secrets
DISCORD_TOKEN = _require("DISCORD_TOKEN")
GITHUB_TOKEN = _require("GITHUB_TOKEN")

# Discord IDs
GUILD_ID = _require_int("GUILD_ID")
REQUIRED_ROLE_ID = _require_int("REQUIRED_ROLE_ID")
REGISTRATION_CHANNEL_ID = _require_int("REGISTRATION_CHANNEL_ID")
REACTION_ROLE_CHANNEL_ID = _require_int("REACTION_ROLE_CHANNEL_ID")
PANEL_CHANNEL_ID = _require_int("PANEL_CHANNEL_ID")
# Role granted by the control panel's "Get Role" button to whitelisted users.
BUYER_ROLE_ID = _require_int("BUYER_ROLE_ID")
# Staff-only channel that receives "Key Redeemed" and "Potential Breach"
# alerts from the control panel's Redeem Key / Reset HWID flows.
REDEEM_ALERTS_CHANNEL_ID = _require_int("REDEEM_ALERTS_CHANNEL_ID")

# Timezone JoinDate values are displayed/stored in (handles EST/EDT automatically)
LOCAL_TZ = ZoneInfo("America/New_York")

# How long a whitelisted user must wait between self-service HWID resets via
# the control panel's "Reset HWID" button.
RESET_HWID_COOLDOWN = timedelta(weeks=1)

# GitHub repo the whitelist database (Users.json) lives in
OWNER = _require("GITHUB_OWNER")
REPO = _require("GITHUB_REPO")
FILE_PATH = "Users.json"
BRANCH = os.getenv("GITHUB_BRANCH", "main")

RAW_URL = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/refs/heads/{BRANCH}/{FILE_PATH}"
API_URL = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{FILE_PATH}?ref={BRANCH}"

# GitHub repo permittedKeys.txt / storedscript.lua live in (this bot's own repo)
STORAGE_REPO = os.getenv("GITHUB_STORAGE_REPO", "Discord-Bot")
STORAGE_BRANCH = os.getenv("GITHUB_STORAGE_BRANCH", "main")

# permittedKeys.txt -- one key per line, checked (read-only) by /createpanel's
# "Redeem Key" flow.
PERMITTED_KEYS_FILE_PATH = "storage/permittedKeys.txt"
PERMITTED_KEYS_RAW_URL = f"https://raw.githubusercontent.com/{OWNER}/{STORAGE_REPO}/refs/heads/{STORAGE_BRANCH}/{PERMITTED_KEYS_FILE_PATH}"
PERMITTED_KEYS_API_URL = f"https://api.github.com/repos/{OWNER}/{STORAGE_REPO}/contents/{PERMITTED_KEYS_FILE_PATH}?ref={STORAGE_BRANCH}"

# storedscript.lua -- the base script /createpanel's "Get Script" button hands
# out, with each user's Key spliced into its getgenv().script_key line.
# /updatescript writes this back via commit_stored_script().
STORED_SCRIPT_FILE_PATH = "storage/storedscript.lua"
STORED_SCRIPT_RAW_URL = f"https://raw.githubusercontent.com/{OWNER}/{STORAGE_REPO}/refs/heads/{STORAGE_BRANCH}/{STORED_SCRIPT_FILE_PATH}"
STORED_SCRIPT_API_URL = f"https://api.github.com/repos/{OWNER}/{STORAGE_REPO}/contents/{STORED_SCRIPT_FILE_PATH}?ref={STORAGE_BRANCH}"

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}

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
# Staff-only channel that receives an alert for every meaningful whitelist/
# key/HWID/access change across the bot -- whitelisting, unwhitelisting,
# bulk operations, edits, HWID resets, key generation/clearing, temp
# whitelists, database rollbacks/uploads, and Bot Access role changes -- on
# top of the control panel's self-service "Key Redeemed" and "Potential
# Breach" alerts. One shared channel so staff can watch everything that
# happens in one place. Still read from REDEEM_ALERTS_CHANNEL_ID in the .env
# file so existing deployments don't need to change anything.
ALERTS_CHANNEL_ID = _require_int("REDEEM_ALERTS_CHANNEL_ID")

# Staff-only channel that receives an alert for every moderation action --
# bans, kicks, mutes, unmutes, unbans, purges, temp roles, DMs, ghost pings,
# slowmode changes, and channel/server lock toggles. Kept separate from
# ALERTS_CHANNEL_ID above so a busy moderation channel doesn't drown out
# whitelist/key/access alerts (or vice versa), and so either stream can be
# muted independently via /togglealerts whitelist|moderation.
MODERATION_ALERTS_CHANNEL_ID = _require_int("MODERATION_ALERTS_CHANNEL_ID")

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

# storage/BotState.json -- durable checkpoint for everything that used to
# live only in process memory: temp ban unban timers, server lockdown/
# per-channel lock snapshots+timers, temp Bot Access grants, pending HWID-
# breach alert buttons, the reaction-role panel message pointer, temp role
# auto-removal timers, ghost ping detection mode, the autorole toggle+role,
# and the /togglealerts whitelist/moderation mute switches. Read back on
# every startup (see each cog's reconcile_*() function, called from
# start.py's on_ready) so a restart degrades to "resume where it left off"
# instead of "silently forget this was ever temporary." Lives in this bot's
# own storage repo, same as permittedKeys.txt/storedscript.lua above.
BOTSTATE_FILE_PATH = "storage/BotState.json"
BOTSTATE_RAW_URL = f"https://raw.githubusercontent.com/{OWNER}/{STORAGE_REPO}/refs/heads/{STORAGE_BRANCH}/{BOTSTATE_FILE_PATH}"
BOTSTATE_API_URL = f"https://api.github.com/repos/{OWNER}/{STORAGE_REPO}/contents/{BOTSTATE_FILE_PATH}?ref={STORAGE_BRANCH}"

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}

# Shared secret for keep_alive.py's /github-webhook route -- must match the
# "Secret" configured on the GitHub push webhook (repo Settings > Webhooks)
# so incoming requests can be verified as actually coming from GitHub (via
# the X-Hub-Signature-256 header) rather than anyone who finds the URL.
# Optional: if unset, the webhook route refuses all requests (fails closed)
# rather than accepting unverifiable ones, and the bot falls back to the
# periodic poll in start.py alone.
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")

# Used by api/webhook_sync.py to figure out where this process is currently
# reachable, so it can keep the GitHub webhook's Payload URL pointed at the
# right place without manual editing on every restart. See that module for
# the full explanation of when each of these is actually used.
#
# Render sets RENDER_EXTERNAL_URL itself at runtime for every web service,
# so this is only a fallback for the (essentially never) case that's unset.
RENDER_FALLBACK_URL = os.getenv("RENDER_FALLBACK_URL", "https://discord-bot-lee1.onrender.com")
# The local ngrok agent's own API -- exists automatically whenever `ngrok
# http ...` is running, no auth needed since it's localhost-only.
NGROK_API_URL = os.getenv("NGROK_API_URL", "http://127.0.0.1:4040/api/tunnels")
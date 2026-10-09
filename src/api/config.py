"""
Central configuration for the bot. Every secret and every deployment-specific
ID (guild, roles, channels, database/storage settings) is loaded from the
environment -- populated from the .env file at the project root via
python-dotenv -- so nothing here is hardcoded.
"""

import os
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
# e-z.host "upload key" (dashboard-issued) -- see api/providers/ez_host.py
# for the API calls this authenticates. e-z.host is the default provider for
# /url shorten, /paste, and /file (see api/providers/registry.py), so unlike
# the optional provider keys below, this one's still _require()d -- the bot
# shouldn't boot into a state where its own default provider is unusable.
EZ_HOST_API_KEY = _require("EZ_HOST_API_KEY")

# Supabase private storage used for protected game scripts. The secret/service
# key is server-only and must never be exposed to the Roblox client.
SUPABASE_URL = _require("SUPABASE_URL")
SUPABASE_SECRET_KEY = _require("SUPABASE_SECRET_KEY")
SUPABASE_GAME_SCRIPTS_BUCKET = os.getenv("SUPABASE_GAME_SCRIPTS_BUCKET", "game-scripts")

# Multi-provider expansion (see api/providers/registry.py) -- every key below
# is optional, unlike EZ_HOST_API_KEY above. Each backs one non-default
# `provider` choice on /url shorten, /paste, or /file; a deployment that
# never sets one just never offers/uses that provider. Left unset, each
# provider module raises a clear, friendly ProviderAPIError the moment
# someone actually picks that provider -- not at boot, and not for anyone
# who sticks with e-z.host.
#
# is.gd/v.gd and Litterbox need no key at all (fully anonymous, public
# APIs) so they have no entry here. Catbox's uploads are anonymous by
# default too; CATBOX_USERHASH is optional purely so this bot's own
# uploads land in one catbox.moe account (dashboard-manageable there)
# instead of scattering across anonymous, undeletable ones.
TINYURL_API_KEY = os.getenv("TINYURL_API_KEY")
CATBOX_USERHASH = os.getenv("CATBOX_USERHASH")
# pastee.dev "Application key" (or a "User Application key", if you want
# uploads tied to a pastee.dev account) -- see api/providers/pastee_dev.py.
# Renamed from PASTE_EE_API_KEY to match that module's rename -- update
# this variable's name in your own .env/deployment secrets too, or
# config.PASTEE_DEV_API_KEY below will read as unset.
# Overridable per-call via /paste's `access_key` option (pastee.dev is one
# of the providers with supports_access_key=True in the registry), so this
# is just the fallback when nobody supplies their own.
PASTEE_DEV_API_KEY = os.getenv("PASTEE_DEV_API_KEY")
# Pastebin.com's "Developer API Key" (pastebin.com/doc_api section 1 --
# dashboard-issued, and mandatory for every call this bot makes to it,
# unlike every optional key on this page: Pastebin has no anonymous/keyless
# path at all) -- see api/providers/pastebin.py. Still just os.getenv() here
# rather than _require()d though, same as every other non-default provider's
# key: the bot boots fine without it, and only a /paste call that actually
# picks provider=pastebin raises a friendly ProviderAPIError naming this var.
PASTEBIN_API_DEV_KEY = os.getenv("PASTEBIN_API_DEV_KEY")
# Pastebin.com's "User API Key" (api_user_key -- pastebin.com/doc_api section
# 9), obtained once, out of band, by POSTing a Pastebin username/password to
# https://pastebin.com/api/api_login.php and caching the result (this bot
# doesn't perform that login itself -- see api/providers/pastebin.py's module
# docstring, "Free-plan scope"). Fully optional, unlike PASTEBIN_API_DEV_KEY
# above -- Pastebin allows anonymous "guest" pastes with no api_user_key at
# all; this is only needed to post under a real account, itself only
# required for a `private` paste or a `folder_key` (see pastebin.py).
# Overridable per-call via /paste's `access_key` option, same convention as
# PASTEE_DEV_API_KEY above.
PASTEBIN_API_USER_KEY = os.getenv("PASTEBIN_API_USER_KEY")

# Discord IDs
GUILD_ID = _require_int("GUILD_ID")
REQUIRED_ROLE_ID = _require_int("REQUIRED_ROLE_ID")
REACTION_ROLE_CHANNEL_ID = _require_int("REACTION_ROLE_CHANNEL_ID")
PANEL_CHANNEL_ID = _require_int("PANEL_CHANNEL_ID")
# Role granted by the control panel's "Get Role" button to whitelisted users.
BUYER_ROLE_ID = _require_int("BUYER_ROLE_ID")
# Staff-only channel that receives an alert for every meaningful whitelist/
# key/access change across the bot -- whitelisting, unwhitelisting,
# bulk operations, edits, key generation/clearing, temporary licenses,
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

# Timezone-local timestamps such as Activated are displayed in the configured local timezone.
LOCAL_TZ = ZoneInfo("America/New_York")

# Persistent cooldown applied whenever the control-panel Reset HWID button is used.
from datetime import timedelta
RESET_HWID_COOLDOWN = timedelta(days=7)



# Public license-service settings. The License Server is always enabled.
# Render exposes RENDER_EXTERNAL_URL automatically; local development falls
# back to the same listener used by the Flask server.
LICENSE_SERVER_BASE_URL = (
    os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    or f"http://127.0.0.1:{os.getenv('PORT', '8080')}"
)

# Server-side execution monitoring. The webhook URL never reaches the Roblox client.
LICENSE_EXECUTION_LOGGING_ENABLED = os.getenv("LICENSE_EXECUTION_LOGGING_ENABLED", "false").strip().lower() not in ("false", "0", "no", "off")
LICENSE_EXECUTION_WEBHOOK_URL = os.getenv("LICENSE_EXECUTION_WEBHOOK_URL", "").strip()

# Server-side security monitoring. Breach alerts use their own webhook. The
# webhook URL remains completely separate from execution logging and the bot's
# normal Alerts channel. The webhook is never exposed to the Roblox client.
LICENSE_BREACH_LOGGING_ENABLED = os.getenv(
    "LICENSE_BREACH_LOGGING_ENABLED", "true"
).strip().lower() not in ("false", "0", "no", "off")
LICENSE_BREACH_WEBHOOK_URL = os.getenv("LICENSE_BREACH_WEBHOOK_URL", "").strip()

# Client-side function tampering detection. This controls the request/kick
# integrity monitor in the public licensing client. The server still keeps
# its own breach logging toggle above.
LICENSE_TAMPER_DETECTION_ENABLED = os.getenv(
    "LICENSE_TAMPER_DETECTION_ENABLED", "true"
).strip().lower() not in ("false", "0", "no", "off")

# Server-side historical key-sharing detection. Roblox UserId, HWID, country,
# executor, device, and IP are supporting signals rather than identity locks.
# Enforcement is based on persistent session history and, optionally, overlapping
# active sessions.
LICENSE_KEY_SHARING_DETECTION_ENABLED = os.getenv(
    "LICENSE_KEY_SHARING_DETECTION_ENABLED", "true"
).strip().lower() not in ("false", "0", "no", "off")

# Persistent license identity clustering.
LICENSE_IDENTITY_CLUSTERING_ENABLED = os.getenv(
    "LICENSE_IDENTITY_CLUSTERING_ENABLED", "true"
).strip().lower() not in ("false", "0", "no", "off")

# A session is active while its last heartbeat is inside this window.
LICENSE_SESSION_HEARTBEAT_ENABLED = os.getenv(
    "LICENSE_SESSION_HEARTBEAT_ENABLED", "true"
).strip().lower() not in ("false", "0", "no", "off")
LICENSE_SESSION_HEARTBEAT_INTERVAL_SECONDS = int(
    os.getenv("LICENSE_SESSION_HEARTBEAT_INTERVAL_SECONDS", "20")
)
LICENSE_SESSION_TIMEOUT_SECONDS = int(
    os.getenv("LICENSE_SESSION_TIMEOUT_SECONDS", "75")
)

LICENSE_CONCURRENT_SESSION_ENFORCEMENT_ENABLED = os.getenv(
    "LICENSE_CONCURRENT_SESSION_ENFORCEMENT_ENABLED", "true"
).strip().lower() not in ("false", "0", "no", "off")
LICENSE_REPEATED_ALTERNATION_DETECTION_ENABLED = os.getenv(
    "LICENSE_REPEATED_ALTERNATION_DETECTION_ENABLED", "true"
).strip().lower() not in ("false", "0", "no", "off")

# Historical-pattern thresholds. These require repeated evidence before
# `key_sharing_detected` disables a license.
LICENSE_SHARING_LOOKBACK_SESSIONS = int(
    os.getenv("LICENSE_SHARING_LOOKBACK_SESSIONS", "12")
)
LICENSE_SHARING_SUSPECTED_REVISITS = int(
    os.getenv("LICENSE_SHARING_SUSPECTED_REVISITS", "1")
)
LICENSE_SHARING_DETECTED_REVISITS = int(
    os.getenv("LICENSE_SHARING_DETECTED_REVISITS", "6")
)
LICENSE_SHARING_DETECTED_MIN_RUNS = int(
    os.getenv("LICENSE_SHARING_DETECTED_MIN_RUNS", "8")
)
LICENSE_SHARING_DETECTED_MIN_DIVERGENCE = int(
    os.getenv("LICENSE_SHARING_DETECTED_MIN_DIVERGENCE", "6")
)


# Sentivel heartbeat used by the bot status page. By default it is enabled
# only on Render, since local development intentionally goes offline during
# restarts/debugging. Set SENTIVEL_ENABLED explicitly to true/false to
# override that automatic environment detection.
_SENTIVEL_ENABLED_ENV = os.getenv("SENTIVEL_ENABLED")
if _SENTIVEL_ENABLED_ENV is None or not _SENTIVEL_ENABLED_ENV.strip():
    SENTIVEL_ENABLED = bool(os.getenv("RENDER_EXTERNAL_URL", "").strip())
else:
    SENTIVEL_ENABLED = _SENTIVEL_ENABLED_ENV.strip().lower() not in ("false", "0", "no", "off")
SENTIVEL_HEARTBEAT_URL = os.getenv("SENTIVEL_HEARTBEAT_URL", "").strip()

# Render sets RENDER_EXTERNAL_URL itself at runtime for every web service,
# so this is only a fallback for the (essentially never) case that's unset.
RENDER_FALLBACK_URL = os.getenv("RENDER_FALLBACK_URL", "https://discord-bot-lee1.onrender.com")
# The local ngrok agent's own API -- exists automatically whenever `ngrok
# http ...` is running, no auth needed since it's localhost-only.
NGROK_API_URL = os.getenv("NGROK_API_URL", "http://127.0.0.1:4040/api/tunnels")
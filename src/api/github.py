"""
GitHub Contents-API helpers. Every command used to repeat the same "fetch
Users.json -> decode base64 -> json.loads -> mutate -> json.dumps -> base64 ->
commit" dance -- this module centralizes all of that so command files only
have to describe *what* changes, not *how* to talk to GitHub.
"""

import asyncio
import base64
import json
import re
import secrets
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp

from . import config
from .tls import get_ssl_context


class GitHubAPIError(Exception):
    """Raised whenever a GitHub API call doesn't return a success status.

    `str(error)` already contains a user-presentable message (including the
    HTTP status), so most commands can just do:

        except GitHubAPIError as e:
            return await send_error(interaction, str(e))
    """

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


async def _get_session(session: Optional[aiohttp.ClientSession]):
    """Reuses a passed-in session, or opens (and flags for closing) a new
    one -- wired to api.tls.get_ssl_context()'s certifi-backed CA bundle
    rather than aiohttp's OS-trust-store-dependent default (see that
    module's docstring)."""
    if session is not None:
        return session, False
    connector = aiohttp.TCPConnector(ssl=get_ssl_context())
    return aiohttp.ClientSession(connector=connector), True


# =========================================================================
# Users.json
# =========================================================================

async def fetch_raw_text(url: str, session: Optional[aiohttp.ClientSession] = None) -> str:
    """Generic raw-text GET -- used for pulling file contents at an arbitrary commit SHA."""
    sess, should_close = await _get_session(session)
    try:
        async with sess.get(url) as resp:
            if resp.status != 200:
                raise GitHubAPIError(f"Failed to fetch content (HTTP {resp.status})", resp.status)
            return await resp.text()
    finally:
        if should_close:
            await sess.close()



async def fetch_storage_file(path: str, session: Optional[aiohttp.ClientSession] = None) -> str:
    """Fetch a protected file from the configured storage repository via the
    authenticated GitHub Contents API. The file itself is never exposed via
    the public raw GitHub URL to the Roblox client."""
    path = path.lstrip("/")
    url = f"https://api.github.com/repos/{config.OWNER}/{config.STORAGE_REPO}/contents/{path}?ref={config.STORAGE_BRANCH}"
    sess, should_close = await _get_session(session)
    try:
        async with sess.get(url, headers=config.HEADERS) as resp:
            if resp.status != 200:
                raise GitHubAPIError(f"Failed to fetch protected game script (HTTP {resp.status})", resp.status)
            data = await resp.json()
    finally:
        if should_close:
            await sess.close()
    try:
        return base64.b64decode(data["content"]).decode("utf-8")
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        raise GitHubAPIError("Protected game script returned invalid content") from exc

async def fetch_api_file(session: Optional[aiohttp.ClientSession] = None) -> Dict[str, Any]:
    """
    Returns the raw GitHub Contents API response for Users.json (a dict with
    base64 `content`, `sha`, etc). Use this whenever you'll need the `sha`
    to write a change back, or need the exact original bytes (e.g. /export).
    """
    sess, should_close = await _get_session(session)
    try:
        async with sess.get(config.API_URL, headers=config.HEADERS) as resp:
            if resp.status != 200:
                raise GitHubAPIError(f"Failed to fetch Users.json metadata (HTTP {resp.status})", resp.status)
            return await resp.json()
    finally:
        if should_close:
            await sess.close()


async def get_current_sha(session: Optional[aiohttp.ClientSession] = None) -> str:
    """Convenience wrapper when all you need is the current file sha (e.g. /upload, /rollback)."""
    data = await fetch_api_file(session)
    return data["sha"]


async def fetch_users_with_sha(session: Optional[aiohttp.ClientSession] = None) -> Tuple[List[Dict[str, Any]], str]:
    """Fetches Users.json + its sha via the Contents API. Use this before any write."""
    data = await fetch_api_file(session)
    sha = data["sha"]
    users = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
    return users, sha


async def fetch_api_text_and_sha(session: Optional[aiohttp.ClientSession] = None) -> Tuple[str, str]:
    """Like fetch_users_with_sha, but returns the raw decoded text instead of parsed JSON (e.g. /verifydata, /editwhitelist)."""
    data = await fetch_api_file(session)
    sha = data["sha"]
    text = base64.b64decode(data["content"]).decode("utf-8")
    return text, sha


async def commit_content(content_str: str, sha: str, message: str, session: Optional[aiohttp.ClientSession] = None) -> Dict[str, Any]:
    """Commits a raw string as the new Users.json content, then updates the
    in-memory users cache to match.

    The cache update lives here (rather than only in commit_users() below)
    because /editwhitelist, /rollback, and /upload all commit through this
    function directly with a hand-built content string, bypassing
    commit_users() entirely -- centralizing the cache update here means
    every write path is covered with no risk of a new one forgetting to
    keep the cache in sync."""
    sess, should_close = await _get_session(session)
    try:
        payload = {
            "message": message,
            "content": base64.b64encode(content_str.encode()).decode("utf-8"),
            "branch": config.BRANCH,
            "sha": sha,
        }
        async with sess.put(config.API_URL, headers=config.HEADERS, json=payload) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise GitHubAPIError(f"Failed to commit changes (HTTP {resp.status}): {err}", resp.status)
            result = await resp.json()
    finally:
        if should_close:
            await sess.close()

    try:
        set_users_cache(json.loads(content_str))
    except (json.JSONDecodeError, TypeError):
        # Shouldn't happen for any real caller (API_URL is always
        # Users.json), but leave the existing cache alone rather than
        # poison it with something unparseable.
        pass

    return result


async def commit_users(users: List[Dict[str, Any]], sha: str, message: str, session: Optional[aiohttp.ClientSession] = None) -> Dict[str, Any]:
    """Serializes `users` to indented JSON and commits it as the new
    Users.json. commit_content() takes care of updating the in-memory cache."""
    content_str = json.dumps(users, indent=4)
    return await commit_content(content_str, sha, message, session)


# =========================================================================
# In-memory Users.json cache
# =========================================================================
#
# Read-only "is this person whitelisted / off cooldown" pre-checks (e.g. the
# control panel's Reset HWID button) need an answer within Discord's ~3s
# interaction-ack window, which a live network call can't reliably guarantee.
# This cache removes the network call from that critical path entirely.
# `refresh_users_cache()` is polled periodically by a background task (see
# start.py), and `commit_content()` above also updates it immediately after
# any successful write, so it never has to wait for the next poll to reflect
# the bot's own changes. `get_cached_users()` never makes a network call and
# can't time out.

_users_cache: Optional[List[Dict[str, Any]]] = None
_users_cache_updated_at: Optional[datetime] = None


def get_cached_users() -> Optional[List[Dict[str, Any]]]:
    """Returns the last-known Users.json contents from memory, or None if the
    cache hasn't been populated yet (e.g. the first refresh hasn't completed
    since bot startup). Never makes a network call."""
    return _users_cache


def cached_users_updated_at() -> Optional[datetime]:
    """The exact UTC timestamp of the last successful cache refresh, or None
    if it's never been populated."""
    return _users_cache_updated_at


def set_users_cache(users: List[Dict[str, Any]]) -> None:
    """Overwrites the in-memory cache directly. Called by commit_content()
    (and, in turn, refresh_users_cache()) so writes and periodic refreshes
    are reflected immediately."""
    global _users_cache, _users_cache_updated_at
    _users_cache = users
    _users_cache_updated_at = datetime.now(timezone.utc)


async def refresh_users_cache(session: Optional[aiohttp.ClientSession] = None) -> List[Dict[str, Any]]:
    """Fetches the current Users.json via the Contents API and stores it as
    the cache.

    Raises GitHubAPIError on failure -- the cache is left untouched
    (stale-but-known beats throwing it away), so callers should catch and
    log rather than let this take down the polling loop."""
    users, _sha = await fetch_users_with_sha(session)
    set_users_cache(users)
    return users


# The actual `@tasks.loop` object lives in start.py (it needs the `bot`
# instance to guard start()/before_loop against on_ready firing twice). That
# module can't be imported from here, or from any command module, without
# also re-running its top-level side effects (keep_alive(), constructing a
# second Client, etc.) -- it's the entry point, not a library. So start.py
# hands us a reference once the loop object exists, and anything that wants
# to report "when does the cache refresh next" (e.g. /botstatus) reads it
# back through here instead.
_refresh_task = None


def register_refresh_task(task) -> None:
    """Called once from start.py with the refresh_users_cache_task loop
    object, so next_cache_refresh() below has something to read."""
    global _refresh_task
    _refresh_task = task


def next_cache_refresh() -> Optional[datetime]:
    """When the background task is next scheduled to refresh the cache, or
    None if the task hasn't been registered yet (shouldn't happen once the
    bot is running) or isn't currently running (e.g. before the bot's first
    on_ready)."""
    if _refresh_task is None:
        return None
    return _refresh_task.next_iteration


# --- Webhook-triggered refresh ---
#
# keep_alive.py's /github-webhook route runs inside Flask's own thread (via
# app.run() in a Thread -- see keep_alive()), completely outside the bot's
# asyncio event loop. It can't just `await refresh_users_cache()` directly
# -- there's no running loop on that thread to await it on. Instead, once
# the bot's real event loop exists (registered below via register_bot_loop,
# called from Client.setup_hook in start.py), the webhook thread hands the
# refresh off to it with asyncio.run_coroutine_threadsafe, which is the
# supported way to schedule a coroutine onto a loop from another thread.
_bot_loop: Optional[asyncio.AbstractEventLoop] = None


def register_bot_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called once from start.py (Client.setup_hook) with the bot's running
    event loop, so trigger_cache_refresh_threadsafe() below has somewhere
    to schedule onto."""
    global _bot_loop
    _bot_loop = loop


async def _webhook_triggered_refresh() -> None:
    """The coroutine actually scheduled by trigger_cache_refresh_threadsafe.
    Mirrors refresh_users_cache_task's own error handling in start.py -- a
    failed refresh just leaves the existing cache in place; the next push
    (or the periodic fallback poll) will catch it."""
    try:
        await refresh_users_cache()
        print("Users.json cache refreshed via GitHub webhook.")
    except GitHubAPIError as e:
        print(f"Failed to refresh Users.json cache from webhook: {e}")


def trigger_cache_refresh_threadsafe() -> bool:
    """Schedules a cache refresh onto the bot's event loop from any other
    thread -- meant to be called from keep_alive.py's webhook route once
    it's confirmed Users.json actually changed. Returns True once
    scheduling succeeds (the refresh itself still happens asynchronously,
    not before this returns), or False if the bot's loop hasn't been
    registered yet (e.g. a webhook arrives in the brief window before
    setup_hook runs) -- callers should treat False as "the periodic
    fallback poll will pick this up instead", not as an error."""
    if _bot_loop is None:
        return False
    asyncio.run_coroutine_threadsafe(_webhook_triggered_refresh(), _bot_loop)
    return True


# =========================================================================
# Rate limit status (shared by /ratelimits)
# =========================================================================

RATE_LIMIT_URL = "https://api.github.com/rate_limit"


async def fetch_rate_limit(session: Optional[aiohttp.ClientSession] = None) -> Dict[str, Any]:
    """
    Fetches the full GitHub Rate Limit API response for the configured PAT
    (config.GITHUB_TOKEN) -- every resource category this token has a quota
    for (core, search, graphql, etc), each with its own limit/used/
    remaining/reset. Hitting this endpoint is explicitly free per GitHub's
    docs (it doesn't count against any of the limits it reports), so it's
    always safe to call on demand from a live command.
    """
    sess, should_close = await _get_session(session)
    try:
        async with sess.get(RATE_LIMIT_URL, headers=config.HEADERS) as resp:
            if resp.status != 200:
                raise GitHubAPIError(f"Failed to fetch rate limit status (HTTP {resp.status})", resp.status)
            return await resp.json()
    finally:
        if should_close:
            await sess.close()


# =========================================================================
# Commit-history helpers (shared by /commithistory and /fetchcommit)
# =========================================================================

async def list_commits(per_page: int = 5, path: str = config.FILE_PATH, session: Optional[aiohttp.ClientSession] = None) -> List[Dict[str, Any]]:
    sess, should_close = await _get_session(session)
    try:
        url = f"https://api.github.com/repos/{config.OWNER}/{config.REPO}/commits"
        params = {"path": path, "sha": config.BRANCH, "per_page": per_page}
        async with sess.get(url, headers=config.HEADERS, params=params) as resp:
            if resp.status != 200:
                raise GitHubAPIError(f"Failed to fetch commits (HTTP {resp.status})", resp.status)
            return await resp.json()
    finally:
        if should_close:
            await sess.close()


async def get_commit(sha: str, session: Optional[aiohttp.ClientSession] = None) -> Dict[str, Any]:
    sess, should_close = await _get_session(session)
    try:
        url = f"https://api.github.com/repos/{config.OWNER}/{config.REPO}/commits/{sha}"
        async with sess.get(url, headers=config.HEADERS) as resp:
            if resp.status != 200:
                raise GitHubAPIError(f"Commit not found or an unexpected error occurred (HTTP {resp.status})", resp.status)
            return await resp.json()
    finally:
        if should_close:
            await sess.close()


# =========================================================================
# Permitted keys (permittedKeys.txt)
# =========================================================================

class PermittedKey(str):
    """String-compatible pending license key with optional game restrictions.

    Existing callers can still compare/use it exactly like a normal string;
    the metadata survives a fetch/commit round-trip through `key.serialize()`.
    """
    def __new__(cls, key: str, games: Optional[List[str]] = None):
        obj = str.__new__(cls, key)
        obj.games = list(games or ["*"])
        return obj

    def serialize(self) -> str:
        if not self.games or "*" in self.games:
            return str(self)
        return f"{self}|{','.join(self.games)}"


def parse_permitted_key_line(line: str) -> PermittedKey:
    parts = [part.strip() for part in line.split("|", 1)]
    key = parts[0]
    games = ["*"]
    if len(parts) == 2 and parts[1]:
        games = [x.strip() for x in parts[1].split(",") if x.strip()] or ["*"]
    return PermittedKey(key, games)


async def fetch_permitted_keys_with_sha(session: Optional[aiohttp.ClientSession] = None) -> Tuple[List[str], str]:
    """
    Fetches permittedKeys.txt + its sha via the Contents API, parsed into a
    list of keys.
    """
    sess, should_close = await _get_session(session)
    try:
        async with sess.get(config.PERMITTED_KEYS_API_URL, headers=config.HEADERS) as resp:
            if resp.status != 200:
                raise GitHubAPIError(f"Failed to fetch permittedKeys.txt metadata (HTTP {resp.status})", resp.status)
            data = await resp.json()
    finally:
        if should_close:
            await sess.close()

    sha = data["sha"]
    text = base64.b64decode(data["content"]).decode("utf-8")
    keys = [parse_permitted_key_line(line.strip()) for line in text.splitlines() if line.strip()]
    return keys, sha


async def commit_permitted_keys(keys: List[str], sha: str, message: str, session: Optional[aiohttp.ClientSession] = None) -> Dict[str, Any]:
    """Serializes `keys` back to permittedKeys.txt (one per line) and commits it."""
    content_str = "\n".join(k.serialize() if isinstance(k, PermittedKey) else str(k) for k in keys) + ("\n" if keys else "")
    sess, should_close = await _get_session(session)
    try:
        payload = {
            "message": message,
            "content": base64.b64encode(content_str.encode()).decode("utf-8"),
            "branch": config.STORAGE_BRANCH,
            "sha": sha,
        }
        async with sess.put(config.PERMITTED_KEYS_API_URL, headers=config.HEADERS, json=payload) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise GitHubAPIError(f"Failed to commit permittedKeys.txt changes (HTTP {resp.status}): {err}", resp.status)
            return await resp.json()
    finally:
        if should_close:
            await sess.close()


def remove_permitted_key(permitted_keys: List[str], key: str) -> List[str]:
    """Returns a new list with every exact match of `key` removed, ready to hand to commit_permitted_keys()."""
    return [k for k in permitted_keys if k != key]


def remove_permitted_keys(permitted_keys: List[str], keys_to_remove: List[str]) -> Tuple[List[str], List[str]]:
    """
    Returns (remaining_keys, actually_removed) after removing every exact
    match of anything in `keys_to_remove` from `permitted_keys`. Used by
    /key clear's explicit-list mode; `actually_removed` only contains keys
    that were actually present, so the caller can report any requested key
    that wasn't found.
    """
    to_remove = set(keys_to_remove)
    remaining = [k for k in permitted_keys if k not in to_remove]
    actually_removed = [k for k in permitted_keys if k in to_remove]
    return remaining, actually_removed


def remove_first_n_permitted_keys(permitted_keys: List[str], n: int) -> Tuple[List[str], List[str]]:
    """
    Returns (remaining_keys, removed_keys) after removing the first `n`
    entries from `permitted_keys` (file order). Used by /key clear's amount
    mode. `n` is clamped to len(permitted_keys) -- clearing more than exist
    just clears all of them.
    """
    n = min(max(n, 0), len(permitted_keys))
    return permitted_keys[n:], permitted_keys[:n]


# =========================================================================
# Stored script (storedscript.lua)
# =========================================================================

async def fetch_stored_script(session: Optional[aiohttp.ClientSession] = None) -> str:
    """
    Fetches storedscript.lua via the Contents API: "Get Script"
    should always hand out whatever the current script actually is.
    """
    sess, should_close = await _get_session(session)
    try:
        async with sess.get(config.STORED_SCRIPT_API_URL, headers=config.HEADERS) as resp:
            if resp.status != 200:
                raise GitHubAPIError(f"Failed to fetch storedscript.lua (HTTP {resp.status})", resp.status)
            data = await resp.json()
    finally:
        if should_close:
            await sess.close()
    return base64.b64decode(data["content"]).decode("utf-8")


async def fetch_stored_script_with_sha(session: Optional[aiohttp.ClientSession] = None) -> Tuple[str, str]:
    """
    Fetches storedscript.lua + its sha via the Contents API. Use this
    (instead of fetch_stored_script()) whenever the script is about to be
    written back -- e.g. /updatescript -- since commit_stored_script() needs
    the current sha.
    """
    sess, should_close = await _get_session(session)
    try:
        async with sess.get(config.STORED_SCRIPT_API_URL, headers=config.HEADERS) as resp:
            if resp.status != 200:
                raise GitHubAPIError(f"Failed to fetch storedscript.lua metadata (HTTP {resp.status})", resp.status)
            data = await resp.json()
    finally:
        if should_close:
            await sess.close()

    sha = data["sha"]
    text = base64.b64decode(data["content"]).decode("utf-8")
    return text, sha


async def commit_stored_script(script_text: str, sha: str, message: str, session: Optional[aiohttp.ClientSession] = None) -> Dict[str, Any]:
    """Commits `script_text` as the new storedscript.lua content."""
    sess, should_close = await _get_session(session)
    try:
        payload = {
            "message": message,
            "content": base64.b64encode(script_text.encode()).decode("utf-8"),
            "branch": config.STORAGE_BRANCH,
            "sha": sha,
        }
        async with sess.put(config.STORED_SCRIPT_API_URL, headers=config.HEADERS, json=payload) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise GitHubAPIError(f"Failed to commit storedscript.lua changes (HTTP {resp.status}): {err}", resp.status)
            return await resp.json()
    finally:
        if should_close:
            await sess.close()


# Matches a `getgenv().script_key = "..."` (or '...') line so its value can
# be swapped out for a specific user's key. Non-greedy + backreference to
# the opening quote so it doesn't over-match into the rest of the file.
SCRIPT_KEY_RE = re.compile(r'(getgenv\(\)\.script_key\s*=\s*)(["\'])(.*?)\2')


def inject_script_key(script_text: str, key: str) -> str:
    """
    Returns a copy of `script_text` with the value inside its
    getgenv().script_key = "..." line replaced by `key`, so each user gets a
    script keyed to their own account. Raises ValueError if no such line is
    found (e.g. storedscript.lua was edited into an unexpected format).
    """
    def _replace(match: re.Match) -> str:
        return f"{match.group(1)}{match.group(2)}{key}{match.group(2)}"

    new_text, count = SCRIPT_KEY_RE.subn(_replace, script_text, count=1)
    if count == 0:
        raise ValueError("`storedscript.lua` doesn't contain a `getgenv().script_key` line to inject the key into.")
    return new_text


def validate_stored_script(script_text: str) -> Optional[str]:
    """
    Checks that `script_text` matches the shape storedscript.lua is expected
    to have -- exactly 2 lines, a script key line first and a loader line
    second. Used by /updatescript before committing a replacement.

    Returns None if valid, or a human-readable reason if not.
    """
    lines = script_text.strip().splitlines()
    if len(lines) != 2:
        return f"Must be exactly 2 lines (the script key line, then the loading line) -- got {len(lines)}."

    key_line, load_line = lines

    if not SCRIPT_KEY_RE.search(key_line):
        return 'Line 1 must be a `getgenv().script_key = "..."` line -- Get Script relies on that to inject each user\'s key.'

    if "loadstring(" not in load_line:
        return "Line 2 must be the loading line (containing `loadstring(`)."

    return None


# =========================================================================
# Bot state (storage/BotState.json)
# =========================================================================
#
# Durable checkpoint for everything that used to live only in process
# memory -- temp ban unban timers, server lockdown / per-channel lock
# snapshots+timers, temp Bot Access grants, pending HWID-breach alert
# buttons, the reaction-role panel message pointer, temp role auto-removal
# timers, ghost ping detection mode, the autorole toggle+role, the
# /togglealerts whitelist/moderation mute switches, and the /toggledms
# switch. Also home to /warnings' warning records -- those carry no timer,
# but live here anyway rather than in Users.json since they're bot-side
# moderation history, not whitelist data. Same "fetch -> get sha -> mutate
# -> commit" shape as Users.json above, with one addition:
# BotState.json is written to from many independent places that can
# legitimately race each other (a ban timer firing at the same moment as a
# moderator running /togglelock, for instance) rather than Users.json's
# mostly-one-moderator-at-a-time pattern, so update_botstate() below adds a
# fetch/mutate/commit retry loop on top of the plain fetch+commit pair.

BOTSTATE_SCHEMA_VERSION = 1

# Every key any BotState-backed feature reads/writes, with the "nothing
# going on" value for each. fetch_botstate_with_sha() shallow-merges this
# under whatever's actually in the file, so every reader can assume the
# full shape exists (state.get("temp_bans", [])-with-a-fallback isn't
# needed) even against a hand-edited or older-schema file that's missing a
# key entirely.
DEFAULT_BOTSTATE: Dict[str, Any] = {
    "schema_version": BOTSTATE_SCHEMA_VERSION,
    "last_updated": None,
    "temp_bans": [],
    # Snapshot of the guild's ban list -- [{"discord_id", "tag", "reason"}, ...]
    # -- kept purely so /checkban and /unban's autocomplete has something
    # fast to search. Not authoritative: a real guild.bans() lookup still
    # backs the actual check/unban decision. See moderation.py's
    # reconcile_banned_users_cache() for the full reasoning.
    "banned_users": [],
    "lockdown": None,
    "channel_locks": [],
    "temp_bot_access": [],
    "pending_breach_alerts": [],
    "reaction_role_panel": None,
    "temp_roles": [],
    "ghostping_mode": "nothing",
    "autorole": {"enabled": False, "role_id": None},
    "alerts_enabled": {"whitelist": True, "moderation": True},
    "dms_enabled": True,
    "warnings": [],
    # /warnings config's auto-action preferences -- see commands/warnings.py's
    # DEFAULT_WARNING_CONFIG for the authoritative copy of these defaults and
    # what each key means. Kept in sync manually (like autorole/alerts_enabled
    # above) since importing commands.warnings from here would be circular.
    "warning_config": {
        "enabled": False,
        "threshold": 3,
        "action": "timeout",
        "timeout_minutes": 60,
        "reset_after_action": True,
        "notify_target": True,
    },
}


def new_state_id(prefix: str) -> str:
    """Short random id for a BotState.json entry (e.g. 'tb_9f2a1c'), used to
    find-and-remove a specific temp ban / channel lock / temp access grant /
    breach alert later without relying on list position."""
    return f"{prefix}_{secrets.token_hex(3)}"


async def fetch_botstate_with_sha(session: Optional[aiohttp.ClientSession] = None) -> Tuple[Dict[str, Any], str]:
    """Fetches storage/BotState.json + its sha via the Contents API. Use
    this before any write (see update_botstate() below for the usual way to
    do that), or on its own for a read-only reconcile_*() pass on startup."""
    sess, should_close = await _get_session(session)
    try:
        async with sess.get(config.BOTSTATE_API_URL, headers=config.HEADERS) as resp:
            if resp.status != 200:
                raise GitHubAPIError(f"Failed to fetch BotState.json metadata (HTTP {resp.status})", resp.status)
            data = await resp.json()
    finally:
        if should_close:
            await sess.close()

    sha = data["sha"]
    try:
        state = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        raise GitHubAPIError(f"BotState.json is not valid JSON: {e}")

    if not isinstance(state, dict):
        raise GitHubAPIError("BotState.json's contents aren't a JSON object.")

    return {**DEFAULT_BOTSTATE, **state}, sha


async def commit_botstate(state: Dict[str, Any], sha: str, message: str, session: Optional[aiohttp.ClientSession] = None) -> Dict[str, Any]:
    """Serializes `state` to indented JSON (stamping schema_version and
    last_updated) and commits it as the new storage/BotState.json.

    Most callers should go through update_botstate() instead, which wraps
    this together with fetch_botstate_with_sha() and retries on a stale
    sha -- call this directly only when you already hold a freshly-fetched
    sha and know nothing else could have written in between."""
    to_write = {
        **state,
        "schema_version": BOTSTATE_SCHEMA_VERSION,
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    content_str = json.dumps(to_write, indent=2) + "\n"

    sess, should_close = await _get_session(session)
    try:
        payload = {
            "message": message,
            "content": base64.b64encode(content_str.encode()).decode("utf-8"),
            "branch": config.STORAGE_BRANCH,
            "sha": sha,
        }
        async with sess.put(config.BOTSTATE_API_URL, headers=config.HEADERS, json=payload) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise GitHubAPIError(f"Failed to commit BotState.json changes (HTTP {resp.status}): {err}", resp.status)
            return await resp.json()
    finally:
        if should_close:
            await sess.close()


async def update_botstate(
    mutate: Callable[[Dict[str, Any]], Dict[str, Any]],
    message: str,
    session: Optional[aiohttp.ClientSession] = None,
    max_retries: int = 3,
) -> Dict[str, Any]:
    """
    Read-modify-write helper for BotState.json: fetches the current state +
    sha, calls `mutate(state)` (may mutate the dict in place and return it,
    or return a fresh one), and commits the result.

    If another write landed in between (the sha this attempt started with
    is now stale -- GitHub returns HTTP 409), this re-fetches the now-
    current state, re-applies `mutate`, and retries, up to `max_retries`
    attempts total. This is the one place BotState.json's write path
    diverges from Users.json's plain fetch-then-commit: BotState.json is
    written from many independent places that can legitimately race each
    other (a ban timer firing while a moderator runs /togglelock, two
    background reconcile tasks resolving around the same moment, etc.),
    where Users.json is mostly one moderator command at a time.

    Raises GitHubAPIError if every attempt fails (a stale-sha conflict on
    the last attempt, or any other failure at any point) -- callers should
    catch this and log it (the Discord-side action, e.g. the actual ban/
    lock/role change, has almost always already happened by this point and
    shouldn't be rolled back over a bookkeeping failure) rather than let it
    bubble up as a command error.
    """
    sess, should_close = await _get_session(session)
    try:
        last_error: Optional[GitHubAPIError] = None
        for attempt in range(max_retries):
            state, sha = await fetch_botstate_with_sha(sess)
            new_state = mutate(state)
            try:
                await commit_botstate(new_state, sha, message, sess)
                return new_state
            except GitHubAPIError as e:
                last_error = e
                if e.status == 409 and attempt < max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise last_error  # pragma: no cover -- loop always returns or raises
    finally:
        if should_close:
            await sess.close()


# =========================================================================
# Shortened URLs (storage/shortened-urls.json)
# =========================================================================
#
# Backs /url shorten, /paste, and /file. One file shared
# across every provider this bot ever wires up (see
# api/providers/registry.py for the full multi-provider list -- e-z.host,
# is.gd, TinyURL, Catbox, Litterbox, Pastebin, pastee.dev, pastey.gg,
# Rubiš, as of the multi-provider expansion), rather than one file per
# provider -- each provider gets its own top-level key (e.g. "ez_host",
# "tinyurl", "catbox") inside it, so adding a new provider is a new key
# here (added lazily by save_shortened_url()'s setdefault()s the first
# time that provider is actually used -- nothing needs pre-seeding), not
# a new file plus a new set of fetch/commit/update helpers. Same
# "fetch -> get sha -> mutate -> commit" shape as BotState.json above,
# including the same fetch/mutate/commit retry-on-409 loop via
# update_shortened_urls() -- two people running /url shorten close
# together is exactly the kind of race BotState.json's update_botstate()
# already exists to handle, so this reuses that shape rather than
# reaching for an asyncio.Lock, which would only ever protect writes
# within a single process.
#
# Each provider's namespace is further split by *kind* -- "shorten",
# "paste", "file" -- each a dict keyed by that kind's own short code, e.g.
# under "ez_host":
#     "shorten": {
#         "abc123": {
#             "original_url": "https://example.com/some/long/path",
#             "shortened_url": "https://i.e-z.host/abc123",
#             "deletion_url": "https://api.e-z.host/shortener/delete/...",
#             "creator_id": "123456789012345678",
#             "created_at": "2026-08-13T12:00:00Z",
#         }
#     },
#     "paste": {
#         "xyz789": {
#             "title": "My paste",
#             "language": "lua",
#             "paste_url": "https://e-z.host/paste/xyz789",
#             "raw_url": "https://e-z.host/paste/raw/xyz789",
#             "deletion_url": "https://api.e-z.host/paste/delete/...",
#             "creator_id": "123456789012345678",
#             "created_at": "2026-08-13T12:00:00Z",
#         }
#     },
#     "file": {
#         "img456": {
#             "original_filename": "logo.png",
#             "content_type": "image/png",
#             "size": 20481,
#             "file_url": "https://i.e-z.host/img456.png",
#             "deletion_url": "https://api.e-z.host/files/delete/...",
#             "creator_id": "123456789012345678",
#             "created_at": "2026-08-13T12:00:00Z",
#         }
#     }
#
# Every other provider's namespace (e.g. "tinyurl", "catbox", "pastebin",
# "pastee_dev") follows this exact same {kind: {short_code: entry}} shape -- only the
# entry's own field values differ (and "deletion_url" may be null instead
# of a string; see api/providers/registry.py's module docstring and each
# provider module's own docstring for which providers never hand one
# back). commands/url.py's _persist_or_degrade() and this section's own
# helpers below (get_shortened_urls(), find_shortened_url_entry(), etc.)
# are all written generically against "whatever provider key is there",
# not hardcoded to "ez_host" -- that's what let the multi-provider
# expansion add eight more providers with zero changes to this file's
# actual read/write logic, only to this comment and DEFAULT_SHORTENED_URLS
# below.
#
# Split by kind (not just by provider) so a short code minted for one kind
# can never silently collide with -- and overwrite -- an entry of a
# different kind in the same provider's namespace, even if a provider's
# shortener/paste/file features turn out to draw their codes from a
# shared space. Doing this now costs nothing (storage/shortened-urls.json
# has no real entries in it yet) -- flattening a provider's kinds into one
# dict the way /url shorten's first cut did would have needed a real data
# migration the moment /paste or /file actually collided with a /url
# shorten entry.

SHORTENED_URLS_SCHEMA_VERSION = 1

DEFAULT_SHORTENED_URLS: Dict[str, Any] = {
    "schema_version": SHORTENED_URLS_SCHEMA_VERSION,
    "last_updated": None,
    "ez_host": {"shorten": {}, "paste": {}, "file": {}},
}


async def fetch_shortened_urls_with_sha(session: Optional[aiohttp.ClientSession] = None) -> Tuple[Dict[str, Any], str]:
    """Fetches storage/shortened-urls.json + its sha via the Contents API.
    Use this before any write (see update_shortened_urls() below for the
    usual way to do that), or on its own for a read-only lookup."""
    sess, should_close = await _get_session(session)
    try:
        async with sess.get(config.SHORTENED_URLS_API_URL, headers=config.HEADERS) as resp:
            if resp.status != 200:
                raise GitHubAPIError(f"Failed to fetch shortened-urls.json metadata (HTTP {resp.status})", resp.status)
            data = await resp.json()
    finally:
        if should_close:
            await sess.close()

    sha = data["sha"]
    try:
        state = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        raise GitHubAPIError(f"shortened-urls.json is not valid JSON: {e}")

    if not isinstance(state, dict):
        raise GitHubAPIError("shortened-urls.json's contents aren't a JSON object.")

    return {**DEFAULT_SHORTENED_URLS, **state}, sha


async def commit_shortened_urls(state: Dict[str, Any], sha: str, message: str, session: Optional[aiohttp.ClientSession] = None) -> Dict[str, Any]:
    """Serializes `state` to indented JSON (stamping schema_version and
    last_updated) and commits it as the new storage/shortened-urls.json.

    Most callers should go through update_shortened_urls() instead, which
    wraps this together with fetch_shortened_urls_with_sha() and retries
    on a stale sha -- call this directly only when you already hold a
    freshly-fetched sha and know nothing else could have written in
    between."""
    to_write = {
        **state,
        "schema_version": SHORTENED_URLS_SCHEMA_VERSION,
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    content_str = json.dumps(to_write, indent=2) + "\n"

    sess, should_close = await _get_session(session)
    try:
        payload = {
            "message": message,
            "content": base64.b64encode(content_str.encode()).decode("utf-8"),
            "branch": config.STORAGE_BRANCH,
            "sha": sha,
        }
        async with sess.put(config.SHORTENED_URLS_API_URL, headers=config.HEADERS, json=payload) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise GitHubAPIError(f"Failed to commit shortened-urls.json changes (HTTP {resp.status}): {err}", resp.status)
            return await resp.json()
    finally:
        if should_close:
            await sess.close()


async def update_shortened_urls(
    mutate: Callable[[Dict[str, Any]], Dict[str, Any]],
    message: str,
    session: Optional[aiohttp.ClientSession] = None,
    max_retries: int = 3,
) -> Dict[str, Any]:
    """
    Read-modify-write helper for shortened-urls.json: fetches the current
    state + sha, calls `mutate(state)` (may mutate the dict in place and
    return it, or return a fresh one), and commits the result.

    If another write landed in between (the sha this attempt started with
    is now stale -- GitHub returns HTTP 409), this re-fetches the now-
    current state, re-applies `mutate`, and retries, up to `max_retries`
    attempts total -- same retry shape as update_botstate() above, for the
    same reason: this file can legitimately be written from more than one
    place close together (two people running /url shorten at once, a
    future /url delete racing a /url shorten, etc).

    Raises GitHubAPIError if every attempt fails. Callers whose provider
    call has already succeeded (e.g. /url shorten, after e-z.host has
    already created the link) should catch this and log it rather than
    let it look like the whole command failed -- the link itself already
    exists at that point, only the local bookkeeping record didn't save.
    """
    sess, should_close = await _get_session(session)
    try:
        last_error: Optional[GitHubAPIError] = None
        for attempt in range(max_retries):
            state, sha = await fetch_shortened_urls_with_sha(sess)
            new_state = mutate(state)
            try:
                await commit_shortened_urls(new_state, sha, message, sess)
                return new_state
            except GitHubAPIError as e:
                last_error = e
                if e.status == 409 and attempt < max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise last_error  # pragma: no cover -- loop always returns or raises
    finally:
        if should_close:
            await sess.close()


async def get_shortened_urls(provider: str, kind: str, session: Optional[aiohttp.ClientSession] = None) -> Dict[str, Any]:
    """Returns {short_code: entry, ...} for the given provider+kind
    sub-namespace (e.g. "ez_host" / "paste") in storage/shortened-urls.json.
    Always a live GitHub fetch -- no in-memory cache yet, since nothing
    here needs a ~3s-budget lookup. That changes the moment a /url list or
    /url delete autocomplete shows up (same reasoning as /warnings'
    _warnings_cache in commands/warnings.py), at which point this is the
    natural place to add one."""
    state, _sha = await fetch_shortened_urls_with_sha(session)
    return state.get(provider, {}).get(kind, {})


# Kinds checked (in this order) by find_shortened_url_entry() below --
# module-level so it's defined once rather than re-literaled at every
# call site, same convention as DEFAULT_SHORTENED_URLS above.
_SHORTENED_URL_KINDS = ("shorten", "paste", "file")


async def find_shortened_url_entry(
    short_code: str,
    session: Optional[aiohttp.ClientSession] = None,
) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """Looks up `short_code` across every kind, across every provider
    currently in storage/shortened-urls.json, via a single fetch --
    looping get_shortened_urls() across every kind (let alone every
    provider too) would mean redundant fetch_shortened_urls_with_sha()
    round trips for the exact same file, since that function always does
    a fresh full-file fetch.

    Built for /url unshorten: the fast, authoritative path for anything
    this bot itself created (a shortened link, paste, or file upload,
    from any provider in api/providers/registry.py) -- creator_id/
    created_at came from this bot at creation time, so there's no need to
    fetch the destination URL at all, unlike the live-redirect-following
    fallback for short codes that aren't in here.

    Prior to the multi-provider expansion this took a `provider` param
    (defaulting to "ez_host", the only provider that existed yet) instead
    of searching all of them -- removed once a second provider existed to
    search, since a short code found under, say, "tinyurl" is exactly as
    findable-and-relevant to /url unshorten as one under "ez_host".

    Iterates state.items() in whatever order the JSON file itself stores
    providers in (insertion order -- effectively "most recently added
    provider last", since save_shortened_url()'s setdefault() only adds a
    provider's key the first time that provider is actually used), then
    _SHORTENED_URL_KINDS order within each provider; neither ordering is
    meaningful beyond that, since short codes are namespaced separately
    per provider+kind and should only ever match one, barring an
    unlikely cross-provider code collision.

    Returns (provider, kind, entry) for the first provider/kind whose
    sub-namespace has `short_code` as a key, or None if it isn't found
    anywhere.
    """
    state, _sha = await fetch_shortened_urls_with_sha(session)
    for provider, provider_state in state.items():
        if not isinstance(provider_state, dict):
            continue
        for kind in _SHORTENED_URL_KINDS:
            entry = provider_state.get(kind, {}).get(short_code)
            if entry is not None:
                return provider, kind, entry
    return None


async def save_shortened_url(
    provider: str,
    kind: str,
    short_code: str,
    entry: Dict[str, Any],
    message: str,
    session: Optional[aiohttp.ClientSession] = None,
) -> Dict[str, Any]:
    """Adds (or overwrites) one entry under `provider`'s `kind` sub-
    namespace (e.g. "ez_host" / "shorten") in storage/shortened-urls.json
    and commits it, via update_shortened_urls()'s fetch/mutate/commit
    retry loop. Returns the newly committed full state (every provider's
    namespace, not just `provider`'s).

    setdefault()s both levels rather than assuming they already exist, so
    this self-heals against an older, pre-kind-split shortened-urls.json
    (e.g. a bare "ez_host": {}) instead of raising."""
    def _mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        state.setdefault(provider, {})
        state[provider].setdefault(kind, {})
        state[provider][kind][short_code] = entry
        return state
    return await update_shortened_urls(_mutate, message, session)



def _iter_shortened_url_entries(state: Dict[str, Any]):
    """Yields (provider, kind, short_code, entry) for every entry across
    every provider and kind currently in a fetched shortened-urls.json
    state dict. Skips the top-level \"schema_version\"/\"last_updated\"
    bookkeeping keys (and anything else that isn't shaped like a
    provider's kind-namespace dict) automatically rather than hardcoding
    the provider list -- so this (and everything built on it, like /url
    clear below) picks up a future second provider with no changes
    needed."""
    for provider, provider_state in state.items():
        if not isinstance(provider_state, dict):
            continue
        for kind, entries in provider_state.items():
            if not isinstance(entries, dict):
                continue
            for short_code, entry in entries.items():
                yield provider, kind, short_code, entry


async def find_matching_shortened_urls(
    predicate: Callable[[Dict[str, Any], str, str], bool],
    session: Optional[aiohttp.ClientSession] = None,
) -> List[Tuple[str, str, str, Dict[str, Any]]]:
    """Read-only preview for /url clear's confirmation step: fetches
    storage/shortened-urls.json once and returns every
    (provider, kind, short_code, entry) tuple for which
    predicate(entry, kind, provider) is True, without modifying anything.
    `predicate` is called with the entry first so the common \"filter by a
    field on the entry itself\" case (creator_id, created_at) doesn't need
    to unpack a differently-ordered tuple.

    A caller that goes on to actually delete these should re-run
    clear_shortened_urls() below with the same predicate rather than reuse
    this result directly -- the file may have changed between this preview
    and that write, same reasoning as every other fetch-then-maybe-write
    pair in this module."""
    state, _sha = await fetch_shortened_urls_with_sha(session)
    return [
        (provider, kind, short_code, entry)
        for provider, kind, short_code, entry in _iter_shortened_url_entries(state)
        if predicate(entry, kind, provider)
    ]


async def clear_shortened_urls(
    predicate: Callable[[Dict[str, Any], str, str], bool],
    message: str,
    session: Optional[aiohttp.ClientSession] = None,
) -> List[Tuple[str, str, str, Dict[str, Any]]]:
    """Removes every entry (across every provider and kind) in
    storage/shortened-urls.json for which predicate(entry, kind, provider)
    is True, via update_shortened_urls()'s fetch/mutate/commit retry loop
    -- same reasoning as save_shortened_url() for going through that
    instead of a plain fetch+commit: a /url clear racing a /url shorten
    (or a second /url clear) is exactly the kind of conflict that retry
    loop exists to absorb.

    Returns what was actually removed, as
    [(provider, kind, short_code, entry), ...]. Rebuilt fresh on every
    retry attempt (`removed.clear()` at the top of `_mutate`), so on a
    409-triggered retry this always reflects what actually got committed
    rather than a stale first-attempt preview -- important since a second
    write landing in between could mean a short code this predicate
    matched on attempt 1 no longer exists (or no longer matches) by the
    time attempt 2 re-fetches and re-applies it."""
    removed: List[Tuple[str, str, str, Dict[str, Any]]] = []

    def _mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        removed.clear()
        for provider, provider_state in state.items():
            if not isinstance(provider_state, dict):
                continue
            for kind, entries in provider_state.items():
                if not isinstance(entries, dict):
                    continue
                to_delete = [code for code, entry in entries.items() if predicate(entry, kind, provider)]
                for code in to_delete:
                    removed.append((provider, kind, code, entries.pop(code)))
        return state

    await update_shortened_urls(_mutate, message, session)
    return removed
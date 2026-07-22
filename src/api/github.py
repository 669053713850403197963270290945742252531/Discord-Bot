"""
GitHub Contents-API helpers. Every command used to repeat the same "fetch
Users.json -> decode base64 -> json.loads -> mutate -> json.dumps -> base64 ->
commit" dance -- this module centralizes all of that so command files only
have to describe *what* changes, not *how* to talk to GitHub.
"""

import base64
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from . import config


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
    """Reuses a passed-in session, or opens (and flags for closing) a new one."""
    if session is not None:
        return session, False
    return aiohttp.ClientSession(), True


# =========================================================================
# Users.json
# =========================================================================

async def fetch_raw_users(session: Optional[aiohttp.ClientSession] = None) -> List[Dict[str, Any]]:
    """
    Reads Users.json straight off the raw.githubusercontent.com CDN.
    Fast and simple, but has no `sha` -- only use this for read-only commands.
    """
    sess, should_close = await _get_session(session)
    try:
        async with sess.get(config.RAW_URL, headers=config.HEADERS) as resp:
            if resp.status != 200:
                raise GitHubAPIError(f"Failed to fetch raw Users.json (HTTP {resp.status})", resp.status)
            text = await resp.text()
            return json.loads(text)
    finally:
        if should_close:
            await sess.close()


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


def cached_users_age() -> Optional[timedelta]:
    """How long ago the cache was last successfully refreshed, or None if
    it's never been populated."""
    if _users_cache_updated_at is None:
        return None
    return datetime.now(timezone.utc) - _users_cache_updated_at


def set_users_cache(users: List[Dict[str, Any]]) -> None:
    """Overwrites the in-memory cache directly. Called by commit_content()
    (and, in turn, refresh_users_cache()) so writes and periodic refreshes
    are reflected immediately."""
    global _users_cache, _users_cache_updated_at
    _users_cache = users
    _users_cache_updated_at = datetime.now(timezone.utc)


async def refresh_users_cache(session: Optional[aiohttp.ClientSession] = None) -> List[Dict[str, Any]]:
    """Fetches the current Users.json via the Contents API and stores it as
    the cache. Deliberately uses fetch_users_with_sha() here instead of the
    faster fetch_raw_users() -- the raw.githubusercontent.com CDN endpoint
    can lag behind the actual repo content for a while after a commit (this
    is exactly the drift /verifydata exists to catch), and this cache backs
    the control panel's whitelist/cooldown pre-checks, so it needs to be
    right, not just fast -- this runs on a periodic background loop, not an
    interaction's critical path.

    Raises GitHubAPIError on failure -- the cache is left untouched
    (stale-but-known beats throwing it away), so callers should catch and
    log rather than let this take down the polling loop."""
    users, _sha = await fetch_users_with_sha(session)
    set_users_cache(users)
    return users


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

async def fetch_permitted_keys(session: Optional[aiohttp.ClientSession] = None) -> List[str]:
    """
    Fetches permittedKeys.txt and returns the permitted keys, one per line
    (blank lines ignored). Read straight off the raw CDN like
    fetch_raw_users(), since validating a redeemed key only ever needs to
    check membership -- nothing here writes back to this file.
    """
    text = await fetch_raw_text(config.PERMITTED_KEYS_RAW_URL, session)
    return [line.strip() for line in text.splitlines() if line.strip()]


async def fetch_permitted_keys_with_sha(session: Optional[aiohttp.ClientSession] = None) -> Tuple[List[str], str]:
    """
    Fetches permittedKeys.txt + its sha via the Contents API, parsed into a
    list of keys. Unlike fetch_permitted_keys(), use this whenever a
    redeemed key is about to be removed from the file, since writing it back
    (commit_permitted_keys) needs the current sha.
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
    keys = [line.strip() for line in text.splitlines() if line.strip()]
    return keys, sha


async def commit_permitted_keys(keys: List[str], sha: str, message: str, session: Optional[aiohttp.ClientSession] = None) -> Dict[str, Any]:
    """Serializes `keys` back to permittedKeys.txt (one per line) and commits it."""
    content_str = "\n".join(keys) + ("\n" if keys else "")
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
    /clearkeys' explicit-list mode; `actually_removed` only contains keys
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
    entries from `permitted_keys` (file order). Used by /clearkeys' amount
    mode. `n` is clamped to len(permitted_keys) -- clearing more than exist
    just clears all of them.
    """
    n = min(max(n, 0), len(permitted_keys))
    return permitted_keys[n:], permitted_keys[:n]


def is_key_permitted(key: str, permitted_keys: List[str]) -> bool:
    """Exact (case-sensitive) membership check against fetch_permitted_keys()'s result."""
    return key in permitted_keys


# =========================================================================
# Stored script (storedscript.lua)
# =========================================================================

async def fetch_stored_script(session: Optional[aiohttp.ClientSession] = None) -> str:
    """
    Fetches storedscript.lua via the Contents API rather than the raw CDN --
    same reasoning as fetch_users_with_sha() vs. fetch_raw_users(): the raw
    endpoint can serve a stale copy for a while after an edit, and "Get
    Script" should always hand out whatever the current script actually is.
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

"""
bot_api.py

Shared constants, GitHub Contents-API helpers, validation utilities, and
Discord helper functions used across the Celestial bot's slash commands.

Every command in the main bot file used to repeat the same "fetch
Users.json -> decode base64 -> json.loads -> mutate -> json.dumps ->
base64 -> commit" dance, plus its own copy of the moderation checks and
key/HWID validation. This module centralizes all of that so the command
file only has to describe *what* changes, not *how* to talk to GitHub.
"""

import os
import json
import base64
import re
import string
import random
import hashlib
import subprocess
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp
import discord
from discord import app_commands


# =========================================================================
# Discord constants
# =========================================================================

GUILD_ID = 1263334150018961559
REQUIRED_ROLE_ID = 1368809009456615434
REGISTRATION_CHANNEL_ID = 1325394667918987266
REACTION_ROLE_CHANNEL_ID = 1403125677925863484
PANEL_CHANNEL_ID = 1528224800579915806
# Role granted by the control panel's "Get Role" button to whitelisted users.
BUYER_ROLE_ID = 1405278377912303778
# Staff-only channel that receives "Key Redeemed" and "Potential Breach"
# alerts from the control panel's Redeem Key / Reset HWID flows.
REDEEM_ALERTS_CHANNEL_ID = 1528301092826517595

# Timezone JoinDate values are displayed/stored in (handles EST/EDT automatically)
LOCAL_TZ = ZoneInfo("America/New_York")

# How long a whitelisted user must wait between self-service HWID resets via
# the control panel's "Reset HWID" button. Edit this to change the cooldown.
RESET_HWID_COOLDOWN = timedelta(weeks=1)


# =========================================================================
# GitHub constants
# =========================================================================

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

OWNER = "669053713850403197963270290945742252531"
REPO = "Celestial"
FILE_PATH = "Users.json"
BRANCH = "main"

RAW_URL = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/refs/heads/{BRANCH}/{FILE_PATH}"
API_URL = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{FILE_PATH}?ref={BRANCH}"

STORAGE_REPO = "Discord-Bot"
STORAGE_BRANCH = "main"

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


class GitHubAPIError(Exception):
    """Raised whenever a GitHub API call doesn't return a success status.

    `str(error)` already contains a user-presentable message (including the
    HTTP status), so most commands can just do:

        except GitHubAPIError as e:
            return await interaction.followup.send(str(e), ephemeral=True)
    """

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


# =========================================================================
# Low level GitHub content helpers
# =========================================================================

async def _get_session(session: Optional[aiohttp.ClientSession]):
    """Reuses a passed-in session, or opens (and flags for closing) a new one."""
    if session is not None:
        return session, False
    return aiohttp.ClientSession(), True


async def fetch_raw_users(session: Optional[aiohttp.ClientSession] = None) -> List[Dict[str, Any]]:
    """
    Reads Users.json straight off the raw.githubusercontent.com CDN.
    Fast and simple, but has no `sha` - only use this for read-only commands.
    """
    sess, should_close = await _get_session(session)
    try:
        async with sess.get(RAW_URL, headers=HEADERS) as resp:
            if resp.status != 200:
                raise GitHubAPIError(f"Failed to fetch raw Users.json (HTTP {resp.status})", resp.status)
            text = await resp.text()
            return json.loads(text)
    finally:
        if should_close:
            await sess.close()


# =========================================================================
# In-memory Users.json cache
# =========================================================================
#
# Read-only "is this person whitelisted / off cooldown" pre-checks (e.g. the
# control panel's Reset HWID button) used to call fetch_raw_users() live,
# under a tight ~2s budget so the modal could still be shown as the
# interaction's first response in time. Whenever that network call was slow
# or errored -- which a fresh CDN connection with no pooling hits more often
# than you'd expect -- the check silently fell back to "allow", showing the
# modal even to non-whitelisted users. It was never a security hole (writes
# always re-check against a fresh fetch, e.g. ResetHWIDModal.on_submit), but
# it looked broken.
#
# This cache removes the network call from that critical path entirely.
# `refresh_users_cache()` is polled periodically by a background task
# (see main.py), and `commit_content()` below also updates it immediately
# after any successful write -- every write path (commit_users() included)
# funnels through commit_content(), so this covers all of them -- so it
# never has to wait for the next poll to reflect the bot's own changes.
# `get_cached_users()` never makes a network call and can't time out.

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
    the control panel's whitelist/cooldown pre-checks (Reset HWID, Redeem
    Key, Get Script/Role), so it needs to be right, not just fast -- this
    runs on a 60s background loop, not an interaction's critical path, so
    there's no reason to take the CDN's staleness risk here.

    Raises GitHubAPIError on failure -- the cache is left untouched
    (stale-but-known beats throwing it away), so callers should catch and
    log rather than let this take down the polling loop."""
    users, _sha = await fetch_users_with_sha(session)
    set_users_cache(users)
    return users


async def fetch_raw_text(url: str, session: Optional[aiohttp.ClientSession] = None) -> str:
    """Generic raw-text GET - used for pulling file contents at an arbitrary commit SHA."""
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
        async with sess.get(API_URL, headers=HEADERS) as resp:
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
    commit_users() entirely -- previously that meant those three writes left
    the cache stale until the next periodic refresh_users_cache_task tick,
    so a Reset HWID click right after e.g. a /rollback could still see the
    pre-rollback data. Centralizing the cache update here means every write
    path is covered with no risk of a new one forgetting to keep the cache
    in sync."""
    sess, should_close = await _get_session(session)
    try:
        payload = {
            "message": message,
            "content": base64.b64encode(content_str.encode()).decode("utf-8"),
            "branch": BRANCH,
            "sha": sha,
        }
        async with sess.put(API_URL, headers=HEADERS, json=payload) as resp:
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
# Commit-history helpers (shared by /commithistory and /fetchcommit)
# =========================================================================

async def list_commits(per_page: int = 5, path: str = FILE_PATH, session: Optional[aiohttp.ClientSession] = None) -> List[Dict[str, Any]]:
    sess, should_close = await _get_session(session)
    try:
        url = f"https://api.github.com/repos/{OWNER}/{REPO}/commits"
        params = {"path": path, "sha": BRANCH, "per_page": per_page}
        async with sess.get(url, headers=HEADERS, params=params) as resp:
            if resp.status != 200:
                raise GitHubAPIError(f"Failed to fetch commits (HTTP {resp.status})", resp.status)
            return await resp.json()
    finally:
        if should_close:
            await sess.close()


async def get_commit(sha: str, session: Optional[aiohttp.ClientSession] = None) -> Dict[str, Any]:
    sess, should_close = await _get_session(session)
    try:
        url = f"https://api.github.com/repos/{OWNER}/{REPO}/commits/{sha}"
        async with sess.get(url, headers=HEADERS) as resp:
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
    Fetches permittedKeys.txt from the Celestial GitHub repo and returns the
    permitted keys, one per line (blank lines ignored). Read straight off the
    raw CDN like fetch_raw_users(), since validating a redeemed key only ever
    needs to check membership -- nothing here writes back to this file.
    """
    text = await fetch_raw_text(PERMITTED_KEYS_RAW_URL, session)
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
        async with sess.get(PERMITTED_KEYS_API_URL, headers=HEADERS) as resp:
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
            "branch": STORAGE_BRANCH,
            "sha": sha,
        }
        async with sess.put(PERMITTED_KEYS_API_URL, headers=HEADERS, json=payload) as resp:
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
    Fetches storedscript.lua from the Celestial GitHub repo via the Contents
    API rather than the raw CDN -- same reasoning as fetch_users_with_sha()
    vs. fetch_raw_users(): the raw endpoint can serve a stale copy for a
    while after an edit, and "Get Script" should always hand out whatever
    the current script actually is.
    """
    sess, should_close = await _get_session(session)
    try:
        async with sess.get(STORED_SCRIPT_API_URL, headers=HEADERS) as resp:
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
        async with sess.get(STORED_SCRIPT_API_URL, headers=HEADERS) as resp:
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
            "branch": STORAGE_BRANCH,
            "sha": sha,
        }
        async with sess.put(STORED_SCRIPT_API_URL, headers=HEADERS, json=payload) as resp:
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
    second -- since every whitelisted user's script (via Get Script ->
    inject_script_key()) depends on that shape. Used by /updatescript before
    committing a replacement.

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
# User-record helpers
# =========================================================================

def find_user_by_discord_id(users: List[Dict[str, Any]], discord_id) -> Optional[Dict[str, Any]]:
    """Looks up a user entry by DiscordId. `discord_id` can be an int or a string."""
    discord_id = str(discord_id)
    return next((u for u in users if str(u.get("DiscordId")) == discord_id), None)


def find_user_by_hwid(users: List[Dict[str, Any]], hwid: str) -> Optional[Dict[str, Any]]:
    """Looks up a user entry by HWID (case-insensitive, since SHA-256 hex can be mixed case)."""
    hwid = (hwid or "").lower()
    return next((u for u in users if str(u.get("HWID", "")).lower() == hwid), None)


def find_user_by_key(users: List[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
    """Looks up a user entry by Key (exact match)."""
    return next((u for u in users if u.get("Key") == key), None)


def remove_user_by_discord_id(users: List[Dict[str, Any]], discord_id) -> Tuple[List[Dict[str, Any]], bool]:
    """Returns (filtered_users, was_removed)."""
    discord_id = str(discord_id)
    filtered = [u for u in users if str(u.get("DiscordId")) != discord_id]
    return filtered, len(filtered) != len(users)


def build_user_entry(
    hwid: str,
    identifier: str,
    rank: str,
    discord_id: str,
    key: str,
    notes: Optional[str] = None,
    join_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Builds a new Users.json entry with the standard field ordering."""
    return {
        "Identifier": identifier,
        "HWID": hwid,
        "DiscordId": discord_id,
        "Rank": rank,
        "JoinDate": join_date or format_join_date(),
        "Key": key,
        "Notes": notes,
        "LastHwidReset": None,
        "totalHwidResets": 0,
    }


# =========================================================================
# Validation / key generation
# =========================================================================

def generate_key(min_length: int = 25, max_length: int = 40) -> str:
    chars = string.ascii_letters + string.digits
    length = random.randint(min_length, max_length)
    return "".join(random.choices(chars, k=length))


def generate_unique_key(users: List[Dict[str, Any]], min_length: int = 25, max_length: int = 40) -> str:
    """Generates a key guaranteed not to collide with any existing user's Key."""
    existing_keys = {u.get("Key") for u in users}
    key = generate_key(min_length, max_length)
    while key in existing_keys:
        key = generate_key(min_length, max_length)
    return key


def generate_unique_keys(count: int, existing_keys: Set[str], min_length: int = 25, max_length: int = 40) -> List[str]:
    """
    Generates `count` keys, each guaranteed unique against `existing_keys`
    and against every other key generated in this same call. Used by
    /genkey's bulk path -- `existing_keys` should be the union of every
    whitelisted user's Key *and* every key currently sitting in
    permittedKeys.txt, so a freshly generated key can never collide with
    one that's already assigned or already pending redemption.
    """
    seen = set(existing_keys)
    new_keys = []
    for _ in range(count):
        key = generate_key(min_length, max_length)
        while key in seen:
            key = generate_key(min_length, max_length)
        seen.add(key)
        new_keys.append(key)
    return new_keys


def parse_key_length_range(length_str: str) -> Tuple[int, int]:
    """
    Parses a /genkey `length` option into a (min_length, max_length) pair
    for generate_key()/generate_unique_keys(). Accepts either a single
    number ("20", a fixed length) or a range ("5-10", inclusive on both
    ends) -- one option covers both instead of separate min/max options.

    Raises ValueError with a human-readable reason on anything malformed,
    so callers can hand that straight to send_error().
    """
    length_str = length_str.strip()

    range_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", length_str)
    if range_match:
        min_length, max_length = int(range_match.group(1)), int(range_match.group(2))
    elif length_str.isdigit():
        min_length = max_length = int(length_str)
    else:
        raise ValueError("`length` must be a single number (e.g. `20`) or a range (e.g. `5-10`).")

    if min_length < 1:
        raise ValueError("`length` must be at least 1.")
    if min_length > max_length:
        raise ValueError(f"`length`'s minimum ({min_length}) can't be greater than its maximum ({max_length}).")
    if max_length > 256:
        raise ValueError("`length`'s maximum can't exceed 256.")

    return min_length, max_length


def is_valid_hwid(hwid: str) -> bool:
    # sha256 hash = 64 hex characters
    return bool(re.fullmatch(r"[a-fA-F0-9]{64}", hwid))


def is_valid_discord_id(discord_id: str) -> bool:
    if not discord_id.isdigit():
        return False
    snowflake = int(discord_id)
    return 1 << 17 < snowflake < 2**64


def is_valid_date(d: str) -> bool:
    try:
        datetime.strptime(d, "%m/%d/%Y, %I:%M:%S %p")
        return True
    except ValueError:
        return False


def get_hwid() -> Optional[str]:
    """Reads the HWID of the machine this code is executing on (Windows-only, via wmic)."""
    try:
        output = subprocess.check_output("wmic csproduct get uuid", shell=True)
        lines = output.decode().splitlines()
        uuid = next((line.strip() for line in lines if line.strip() and line.strip() != "UUID"), None)
        if uuid:
            return uuid
    except Exception as e:
        print(f"Failed to retrieve HWID: {e}")
    return None


def format_join_date(dt: Optional[datetime] = None) -> str:
    """Formats a datetime as m/d/yyyy, h:mm:ss AM/PM in LOCAL_TZ, e.g. '6/19/2026, 3:24:53 AM'.

    Month, day, and hour are not zero-padded; minutes and seconds are.
    Automatically accounts for EST/EDT.
    """
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(LOCAL_TZ)
    hour_12 = dt.hour % 12 or 12
    period = "AM" if dt.hour < 12 else "PM"
    return f"{dt.month}/{dt.day}/{dt.year}, {hour_12}:{dt.minute:02d}:{dt.second:02d} {period}"


def parse_join_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parses a JoinDate-style 'm/d/yyyy, h:mm:ss AM/PM' string back into a
    tz-aware datetime in LOCAL_TZ. Also accepts the older 'yyyy-mm-dd'
    format for entries created before the JoinDate format change (assumed
    UTC, since that's how it was originally stored).

    Returns None if `date_str` is empty or doesn't match either format --
    meaning there's nothing to parse, not that something is broken.
    """
    if not date_str:
        return None

    try:
        return datetime.strptime(date_str, "%m/%d/%Y, %I:%M:%S %p").replace(tzinfo=LOCAL_TZ)
    except ValueError:
        pass

    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    return None


def format_discord_timestamp(date_str: Optional[str], fmt: str = "D") -> str:
    """Converts a JoinDate-style string into a Discord <t:...:fmt> timestamp, falling back to the raw string on failure."""
    if not date_str:
        return "N/A"

    dt = parse_join_date(date_str)
    if dt is None:
        return date_str

    return f"<t:{int(dt.timestamp())}:{fmt}>"


# =========================================================================
# Hash utilities (for /hash)
# =========================================================================
#
# Algorithm names are pulled live from hashlib rather than hardcoded, so
# this automatically tracks whatever md5/sha2/sha3/blake2/etc. -- plus
# anything OpenSSL adds on top -- the Python this bot happens to be running
# on actually supports. No list to keep in sync by hand as Python/OpenSSL
# versions change.

# Output length (in bytes) used for the two SHAKE algorithms. SHAKE-128/256
# are variable-length (XOF) digests with no fixed size of their own --
# hexdigest()/digest() raise TypeError without an explicit length here.
# 32 bytes (256 bits) matches the strength of a typical SHA-256 digest and
# is called out explicitly in the /hash output so it's never ambiguous.
SHAKE_OUTPUT_BYTES = 32


def get_available_hash_algorithms() -> List[str]:
    """
    Every hash algorithm this Python build's hashlib can actually construct
    right now: the cross-platform-guaranteed set
    (hashlib.algorithms_guaranteed) plus whatever this build's OpenSSL
    binding adds on top (hashlib.algorithms_available). The latter can
    report the same algorithm more than once in different cases (e.g. both
    "sha256" and "SHA256") depending on the OpenSSL build, so this
    lowercases and de-duplicates before sorting.
    """
    names = hashlib.algorithms_guaranteed | hashlib.algorithms_available
    return sorted({name.lower() for name in names})


def hash_text(algorithm: str, text: str) -> str:
    """
    Hashes `text` (UTF-8 encoded) with `algorithm` and returns the hex
    digest. `algorithm` is matched case-insensitively against
    get_available_hash_algorithms().

    Raises ValueError if `algorithm` isn't recognized, or whatever
    TypeError/ValueError hashlib itself raises if construction fails
    despite the name being recognized (e.g. a SHAKE variant used wrong).
    """
    algo = algorithm.lower().strip()
    if algo not in get_available_hash_algorithms():
        raise ValueError(f"'{algorithm}' isn't a supported hash algorithm on this bot's Python build.")

    hasher = hashlib.new(algo)
    hasher.update(text.encode("utf-8"))

    # SHAKE-128/256 are variable-length XOFs -- hexdigest() requires an
    # explicit output length that fixed-size algorithms don't take.
    if algo.startswith("shake_"):
        return hasher.hexdigest(SHAKE_OUTPUT_BYTES)
    return hasher.hexdigest()


# =========================================================================
# Text transforms (/transform)
# =========================================================================
#
# /transform's `format` option is a fixed Discord choice list built from
# TRANSFORM_FORMAT_CHOICES below, not user-typed + autocompleted text like
# /hash's `algorithm` -- Discord only ever sends back one of the `value`s
# this module defines, so transform_text() only needs to validate against
# a dict lookup rather than fuzzy-matching free text.
#
# Most styles are a straight per-character swap into one of Unicode's
# "Mathematical Alphanumeric Symbols" blocks (bold, italic, fraktur,
# double-struck, monospace...), which are laid out A-Z/a-z/0-9 in order
# with a handful of exceptions where Unicode reserves that code point for
# a legacy Letterlike Symbol instead (e.g. italic h -> ℎ). A few styles
# (mirror, zalgo, inverted, sparkle) don't map to a clean Unicode block and
# get their own small bespoke function instead. Characters a style has no
# mapping for (punctuation under "Circled Letters", digits under an italic
# style, etc.) are always passed through unchanged rather than dropped, so
# spacing/punctuation survives every style intact.

def _build_alphabet_map(
    upper_start: Optional[int],
    lower_start: Optional[int],
    digit_start: Optional[int] = None,
    upper_exceptions: Optional[Dict[str, str]] = None,
    lower_exceptions: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    Builds an A-Z / a-z / 0-9 -> Unicode-block character lookup table from
    a block's starting code point, assuming the block is laid out
    sequentially (true of every Mathematical Alphanumeric Symbols block
    used below). `upper_exceptions`/`lower_exceptions` patch in the
    letters Unicode reserves in favor of a pre-existing Letterlike Symbol
    instead, keyed by the plain letter (e.g. {"H": "ℌ"}).
    """
    mapping: Dict[str, str] = {}
    if upper_start is not None:
        for i, ch in enumerate(string.ascii_uppercase):
            mapping[ch] = chr(upper_start + i)
    if lower_start is not None:
        for i, ch in enumerate(string.ascii_lowercase):
            mapping[ch] = chr(lower_start + i)
    if digit_start is not None:
        for i, ch in enumerate(string.digits):
            mapping[ch] = chr(digit_start + i)
    mapping.update(upper_exceptions or {})
    mapping.update(lower_exceptions or {})
    return mapping


def _apply_char_map(text: str, mapping: Dict[str, str]) -> str:
    """Passes each character through `mapping`, leaving anything unmapped untouched."""
    return "".join(mapping.get(ch, ch) for ch in text)


def _apply_combining_mark(text: str, mark: str) -> str:
    """
    Threads a combining diacritic after every non-whitespace character.
    Used for the line/underline-style transforms, which are all "one
    combining mark, repeated" -- whitespace is skipped since a mark with
    nothing to attach to just floats visibly on its own.
    """
    return "".join(ch + mark if not ch.isspace() else ch for ch in text)


def _transform_regional_indicators(text: str) -> str:
    """
    A-Z/a-z -> 🇦-🇿 Regional Indicator Symbols. Two of these placed
    directly next to each other render as a single flag emoji (e.g. "US"
    -> 🇺🇸) instead of two separate letters, so a zero-width space is
    threaded between consecutive indicator characters to keep every
    letter rendering on its own.
    """
    out = []
    prev_was_indicator = False
    for ch in text:
        if ch.isalpha() and ch.upper() in string.ascii_uppercase:
            if prev_was_indicator:
                out.append("\u200b")
            out.append(chr(0x1F1E6 + (ord(ch.upper()) - ord("A"))))
            prev_was_indicator = True
        else:
            out.append(ch)
            prev_was_indicator = False
    return "".join(out)


def _transform_emoji_letters(text: str) -> str:
    """A-Z/a-z -> 🅐-🅩 (Negative Circled Latin Capital Letter), the colorful 'badge' alphabet emoji clients render these as."""
    mapping = _build_alphabet_map(0x1F150, 0x1F150)  # only one case of glyph exists; same result regardless of input case
    return _apply_char_map(text, mapping)


_CURSIVE_UPPER_EXCEPTIONS = {"B": "ℬ", "E": "ℰ", "F": "ℱ", "H": "ℋ", "I": "ℐ", "L": "ℒ", "M": "ℳ", "R": "ℛ"}
_CURSIVE_LOWER_EXCEPTIONS = {"e": "ℯ", "g": "ℊ", "o": "ℴ"}

def _transform_cursive(text: str) -> str:
    """A-Z/a-z -> Mathematical Script Letters, patched with the legacy Letterlike Symbols Unicode substitutes for the letters it reserves."""
    mapping = _build_alphabet_map(0x1D49C, 0x1D4B6, upper_exceptions=_CURSIVE_UPPER_EXCEPTIONS, lower_exceptions=_CURSIVE_LOWER_EXCEPTIONS)
    return _apply_char_map(text, mapping)


_SUPERSCRIPT_LOWER = {
    "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ", "h": "ʰ", "i": "ⁱ", "j": "ʲ",
    "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "n": "ⁿ", "o": "ᵒ", "p": "ᵖ", "q": "q", "r": "ʳ", "s": "ˢ", "t": "ᵗ",
    "u": "ᵘ", "v": "ᵛ", "w": "ʷ", "x": "ˣ", "y": "ʸ", "z": "ᶻ",
}
_SUPERSCRIPT_UPPER = {
    "A": "ᴬ", "B": "ᴮ", "D": "ᴰ", "E": "ᴱ", "G": "ᴳ", "H": "ᴴ", "I": "ᴵ", "J": "ᴶ", "K": "ᴷ", "L": "ᴸ",
    "M": "ᴹ", "N": "ᴺ", "O": "ᴼ", "P": "ᴾ", "R": "ᴿ", "T": "ᵀ", "U": "ᵁ", "V": "ⱽ", "W": "ᵂ",
}
_SUPERSCRIPT_OTHER = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
}

def _transform_superscript(text: str) -> str:
    """
    Unicode only defines superscript capitals for about two-thirds of the
    alphabet (C, F, Q, S, X, Y, Z are missing) -- the ones it's missing
    fall back to the lowercase superscript glyph rather than the
    full-size letter, since "small and raised" matters more here than
    preserving case.
    """
    out = []
    for ch in text:
        if ch in _SUPERSCRIPT_UPPER:
            out.append(_SUPERSCRIPT_UPPER[ch])
        elif ch.lower() in _SUPERSCRIPT_LOWER:
            out.append(_SUPERSCRIPT_LOWER[ch.lower()])
        else:
            out.append(_SUPERSCRIPT_OTHER.get(ch, ch))
    return "".join(out)


_SUBSCRIPT_LOWER = {
    "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "l": "ₗ", "m": "ₘ", "n": "ₙ",
    "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ", "u": "ᵤ", "v": "ᵥ", "x": "ₓ",
}
_SUBSCRIPT_OTHER = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
    "+": "₊", "-": "₋", "=": "₌", "(": "₍", ")": "₎",
}

def _transform_subscript(text: str) -> str:
    """
    Subscript coverage is much sparser than superscript -- only 17 letters
    have a defined Unicode subscript glyph at all (no b, c, d, f, g, q, w,
    y, z...). Letters outside that set are left as their normal-size
    original rather than dropped or faked.
    """
    out = []
    for ch in text:
        lower = ch.lower()
        if lower in _SUBSCRIPT_LOWER:
            out.append(_SUBSCRIPT_LOWER[lower])
        else:
            out.append(_SUBSCRIPT_OTHER.get(ch, ch))
    return "".join(out)


_MIRROR_LETTERS = {
    "b": "d", "d": "b", "p": "q", "q": "p",
    "B": "d", "D": "b", "P": "q", "Q": "p",  # no distinct uppercase mirror glyphs exist -- closest visual match reuses the lowercase swap
}
_MIRROR_PUNCTUATION = {"(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{", "<": ">", ">": "<", "/": "\\", "\\": "/"}

def _transform_mirror(text: str) -> str:
    """
    A left-right ("held up to a mirror") flip: the string order is
    reversed and a small set of naturally mirror-symmetric characters
    (b/d, p/q, brackets, slashes) are swapped so they still point the
    right way once reversed. Most letters have no true horizontally-
    mirrored Unicode lookalike, so they're otherwise left alone -- see
    "Inverted" for a full upside-down (180°-rotated) character remap.
    """
    swapped = [_MIRROR_LETTERS.get(ch, _MIRROR_PUNCTUATION.get(ch, ch)) for ch in text]
    return "".join(reversed(swapped))


_ZALGO_UP = ["\u0300", "\u0301", "\u0302", "\u0303", "\u0304", "\u0305", "\u0306", "\u0307", "\u0308", "\u030a", "\u030f", "\u0311", "\u0323", "\u0327", "\u0328"]
_ZALGO_MID = ["\u0334", "\u0335", "\u0336", "\u0337", "\u0338", "\u034e"]
_ZALGO_DOWN = ["\u0316", "\u0317", "\u0318", "\u0319", "\u031c", "\u031d", "\u031e", "\u031f", "\u0320", "\u0324", "\u0325", "\u0326", "\u0329", "\u032a", "\u032b", "\u032c", "\u032d"]

def _transform_zalgo(text: str, *, intensity: int = 3) -> str:
    """
    Layers random combining diacritical marks above, through, and below
    each non-whitespace character. `intensity` caps how many marks of
    each type can stack per character -- kept modest (rather than the
    "hundreds of marks" some zalgo generators do) so results stay
    reasonably within Discord's 1024-char embed field limit and don't
    turn into unreadable noise.
    """
    out = []
    for ch in text:
        out.append(ch)
        if ch.isspace():
            continue
        out.extend(random.choices(_ZALGO_UP, k=random.randint(0, intensity)))
        out.extend(random.choices(_ZALGO_MID, k=random.randint(0, max(1, intensity - 2))))
        out.extend(random.choices(_ZALGO_DOWN, k=random.randint(0, intensity)))
    return "".join(out)


def _transform_monospace(text: str) -> str:
    """A-Z/a-z/0-9 -> Mathematical Monospace (fully sequential, no exceptions)."""
    mapping = _build_alphabet_map(0x1D670, 0x1D68A, 0x1D7F6)
    return _apply_char_map(text, mapping)


_INVERTED_LOWER = {
    "a": "ɐ", "b": "q", "c": "ɔ", "d": "p", "e": "ǝ", "f": "ɟ", "g": "ƃ", "h": "ɥ", "i": "ᴉ", "j": "ɾ",
    "k": "ʞ", "l": "l", "m": "ɯ", "n": "u", "o": "o", "p": "d", "q": "b", "r": "ɹ", "s": "s", "t": "ʇ",
    "u": "n", "v": "ʌ", "w": "ʍ", "x": "x", "y": "ʎ", "z": "z",
}
_INVERTED_OTHER = {
    "0": "0", "1": "Ɩ", "2": "ᄅ", "3": "Ɛ", "4": "ㄣ", "5": "5", "6": "9", "7": "ㄥ", "8": "8", "9": "6",
    ".": "˙", ",": "'", "'": ",", '"': "„", "?": "¿", "!": "¡",
    "(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{", "<": ">", ">": "<", "&": "⅋", "_": "‾",
}

def _transform_inverted(text: str) -> str:
    """
    Classic 'upside-down text': every character is swapped for its
    rotated-180° lookalike and the whole string is reversed, since
    flipping a word vertically also reverses its reading order. Case is
    folded to lowercase for letters -- Unicode has no clean rotated
    glyph for most capitals, so generators conventionally flatten case
    rather than reach for obscure/poorly-supported code points.
    """
    out = [_INVERTED_LOWER.get(ch.lower(), _INVERTED_OTHER.get(ch, ch)) for ch in text]
    return "".join(reversed(out))


_CIRCLED_DIGIT_START = 0x2460  # ①..⑨

def _transform_circled(text: str) -> str:
    """A-Z/a-z -> Ⓐ-Ⓩ/ⓐ-ⓩ, 1-9 -> ①-⑨, 0 -> ⓪ (0 sits outside the 1-9 block, so it's patched in separately)."""
    mapping = _build_alphabet_map(0x24B6, 0x24D0)
    mapping["0"] = "⓪"
    for i, d in enumerate("123456789"):
        mapping[d] = chr(_CIRCLED_DIGIT_START + i)
    return _apply_char_map(text, mapping)


def _transform_squared(text: str) -> str:
    """A-Z/a-z -> 🄰-🆉 (Squared Latin Capital Letter). No separate lowercase squared block exists, so both cases map to the same glyph."""
    mapping = _build_alphabet_map(0x1F130, 0x1F130)
    return _apply_char_map(text, mapping)


def _transform_serif_bold(text: str) -> str:
    """A-Z/a-z/0-9 -> Mathematical Bold (fully sequential, no exceptions)."""
    mapping = _build_alphabet_map(0x1D400, 0x1D41A, 0x1D7CE)
    return _apply_char_map(text, mapping)


def _transform_serif_italic(text: str) -> str:
    """A-Z/a-z -> Mathematical Italic. Digits have no italic variant in Unicode, so they pass through unchanged; italic h is reserved in favor of the pre-existing ℎ (PLANCK CONSTANT)."""
    mapping = _build_alphabet_map(0x1D434, 0x1D44E, lower_exceptions={"h": "ℎ"})
    return _apply_char_map(text, mapping)


def _transform_sans_bold_italic(text: str) -> str:
    """A-Z/a-z -> Mathematical Sans-Serif Bold Italic. Digits have no bold-italic sans variant in Unicode, so they pass through unchanged."""
    mapping = _build_alphabet_map(0x1D63C, 0x1D656)
    return _apply_char_map(text, mapping)


_BLACKLETTER_UPPER_EXCEPTIONS = {"C": "ℭ", "H": "ℌ", "I": "ℑ", "R": "ℜ", "Z": "ℨ"}

def _transform_blackletter(text: str) -> str:
    """A-Z/a-z -> Mathematical Fraktur, patched with the legacy Letterlike Symbols Unicode substitutes for the 5 capitals it reserves."""
    mapping = _build_alphabet_map(0x1D504, 0x1D51E, upper_exceptions=_BLACKLETTER_UPPER_EXCEPTIONS)
    return _apply_char_map(text, mapping)


_DOUBLE_STRUCK_UPPER_EXCEPTIONS = {"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"}

def _transform_double_struck(text: str) -> str:
    """A-Z/a-z/0-9 -> Mathematical Double-Struck ('blackboard bold'), patched with the legacy Letterlike Symbols substitutes for the 7 capitals it reserves."""
    mapping = _build_alphabet_map(0x1D538, 0x1D552, 0x1D7D8, upper_exceptions=_DOUBLE_STRUCK_UPPER_EXCEPTIONS)
    return _apply_char_map(text, mapping)


def _transform_fullwidth(text: str) -> str:
    """
    Shifts every printable ASCII character (0x21-0x7E) up by 0xFEE0 into
    its Fullwidth Form equivalent -- covers letters, digits, AND
    punctuation in one pass, unlike the letter-only styles above. The
    plain space is mapped separately to U+3000 IDEOGRAPHIC SPACE, since
    0xFEE0 lands outside the printable range for it.
    """
    out = []
    for ch in text:
        if ch == " ":
            out.append("\u3000")
        elif "!" <= ch <= "~":
            out.append(chr(ord(ch) + 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def _transform_sparkle(text: str) -> str:
    """Bookends the text with ✨ and threads one between each word for a decorative 'aesthetic' look."""
    words = [w for w in text.split(" ") if w != ""] or [text]
    return "✨ " + " ✨ ".join(words) + " ✨"


def _transform_text_to_binary(text: str) -> str:
    """UTF-8 encodes `text`, then renders each byte as an 8-bit binary string, space-separated (e.g. 'Hi' -> '01001000 01101001'). UTF-8 means multi-byte characters (emoji, accents, etc.) round-trip correctly, just as more than one 8-bit group."""
    return " ".join(f"{byte:08b}" for byte in text.encode("utf-8"))


def _transform_binary_to_text(text: str) -> str:
    """
    Reverses text_to_binary(): parses `text` as either whitespace-separated
    8-bit groups ("01001000 01101001") or one unbroken run of bits
    ("0100100001101001", sliced into 8-bit chunks), turns those back into
    raw bytes, and UTF-8 decodes them.

    Raises ValueError if the input has no non-whitespace content, contains
    anything besides 0s and 1s, doesn't come in complete 8-bit bytes, or
    decodes to invalid UTF-8 -- all of which point at a copy/paste mistake
    rather than something this function should silently guess around.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("No binary data to convert -- provide 8-bit groups like '01001000 01101001' (spaces optional).")

    if any(ch.isspace() for ch in stripped):
        groups = stripped.split()
    else:
        groups = [stripped[i:i + 8] for i in range(0, len(stripped), 8)]
    bits = "".join(groups)

    if any(ch not in "01" for ch in bits):
        raise ValueError("That doesn't look like valid binary -- it should only contain 0s and 1s (optionally grouped in 8-bit bytes separated by spaces).")
    if len(bits) % 8 != 0:
        raise ValueError(f"Binary input must come in complete 8-bit bytes -- got {len(bits)} bits total, which isn't a multiple of 8.")

    byte_values = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
    try:
        return byte_values.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Those bytes aren't valid UTF-8 once decoded -- double check the binary was copied correctly and hasn't been split into the wrong byte boundaries.")


TRANSFORM_FORMATS: Dict[str, Dict[str, Any]] = {
    "regional_indicators": {"name": "Regional Indicators", "func": _transform_regional_indicators},
    "emoji_letters": {"name": "Emoji Letters", "func": _transform_emoji_letters},
    "cursive": {"name": "Cursive", "func": _transform_cursive},
    "superscript": {"name": "Superscript", "func": _transform_superscript},
    "subscript": {"name": "Subscript", "func": _transform_subscript},
    "mirror": {"name": "Mirror", "func": _transform_mirror},
    "zalgo": {"name": "Zalgo", "func": _transform_zalgo},
    "monospace": {"name": "Monospace", "func": _transform_monospace},
    "inverted": {"name": "Inverted", "func": _transform_inverted},
    "middle_line": {"name": "Middle Line", "func": lambda t: _apply_combining_mark(t, "\u0336")},
    "overlined": {"name": "Overlined", "func": lambda t: _apply_combining_mark(t, "\u0305")},
    "true_underline": {"name": "True Underline", "func": lambda t: _apply_combining_mark(t, "\u0332")},
    "double_underline": {"name": "Double Underline", "func": lambda t: _apply_combining_mark(t, "\u0333")},
    "circled_letters": {"name": "Circled Letters", "func": _transform_circled},
    "squared_letters": {"name": "Squared Letters", "func": _transform_squared},
    "serif_bold": {"name": "Serif Bold", "func": _transform_serif_bold},
    "serif_italic": {"name": "Serif Italic", "func": _transform_serif_italic},
    "sans_bold_italic": {"name": "Sans Serif Bold Italic", "func": _transform_sans_bold_italic},
    "blackletter": {"name": "Blackletter", "func": _transform_blackletter},
    "double_struck": {"name": "Double-Struck", "func": _transform_double_struck},
    "fullwidth": {"name": "Fullwidth", "func": _transform_fullwidth},
    "sparkle": {"name": "Sparkle", "func": _transform_sparkle},
    "binary": {"name": "Binary (Text \u2192 Binary)", "func": _transform_text_to_binary},
    "binary_decode": {"name": "Binary Decode (Binary \u2192 Text)", "func": _transform_binary_to_text},
}

# (label, value) pairs in display order, ready to drop straight into
# main.py's @app_commands.choices(format=[...]) so this dict stays the one
# source of truth for both the Discord option list and the implementation.
TRANSFORM_FORMAT_CHOICES: List[Tuple[str, str]] = [(v["name"], key) for key, v in TRANSFORM_FORMATS.items()]


def transform_text(format_key: str, text: str) -> str:
    """
    Applies the named /transform style to `text`. `format_key` must be one
    of TRANSFORM_FORMATS' keys -- Discord only ever sends one of these
    back since /transform's `format` option is a fixed choice list rather
    than free-typed + autocompleted text.

    Raises ValueError if `format_key` isn't recognized.
    """
    entry = TRANSFORM_FORMATS.get(format_key)
    if entry is None:
        raise ValueError(f"'{format_key}' isn't a supported transform format.")
    return entry["func"](text)


# =========================================================================
# Temporary whitelist expiration (stored in the Notes field)
# =========================================================================
#
# /tempwhitelist used to only track expirations in the in-memory
# active_temp_whitelists dict, which is wiped every time the bot restarts --
# the JSON entry itself gave no indication it was ever temporary. Instead,
# the expiration is now written straight into the Notes field (reusing the
# same date format as JoinDate), so it survives restarts and can be read
# back by anything that looks at the whitelist, not just this bot process.

EXPIRATION_NOTE_RE = re.compile(r"^Expires on (.+)$")


def format_expiration_note(expiration_dt: datetime) -> str:
    """
    Builds the Notes-field string that marks a whitelist entry as temporary
    and records exactly when it expires, e.g. 'Expires on 7/19/2026, 5:51:12 AM'.
    Reuses format_join_date()'s format so it round-trips through
    parse_expiration_note().
    """
    return f"Expires on {format_join_date(expiration_dt)}"


def parse_expiration_note(notes: Optional[str]) -> Optional[datetime]:
    """
    Reverses format_expiration_note(): pulls the expiration datetime back out
    of a Notes field, returned as a tz-aware datetime in LOCAL_TZ.

    Returns None if `notes` is empty, doesn't match the "Expires on ..."
    pattern, or has an unparseable date -- meaning it's an unrelated/manual
    note rather than a temp-whitelist marker, not that something is broken.
    """
    if not notes:
        return None
    match = EXPIRATION_NOTE_RE.match(notes.strip())
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%m/%d/%Y, %I:%M:%S %p").replace(tzinfo=LOCAL_TZ)
    except ValueError:
        return None


def is_notes_locked(entry: Dict[str, Any]) -> bool:
    """
    True if `entry`'s Notes field currently holds an unexpired temporary-
    whitelist expiration marker (see format_expiration_note /
    parse_expiration_note above).

    While this is True, nothing should overwrite or clear this entry's
    Notes field -- not to blank, and not to some other custom value --
    since that field is the *only* record of when this entry's temporary
    whitelist expires. Silently overwriting it (e.g. via /edituser,
    /clearnotes, the Edit User button on /viewwhitelist, or a raw
    /editwhitelist JSON edit) would leave the temp-whitelist system with no
    way to know when to auto-remove this entry.

    Returns False (unlocked) once the marker has expired -- an expired temp
    whitelist's Notes are just as removable/editable as a normal note.
    """
    expires_at = parse_expiration_note(entry.get("Notes"))
    return expires_at is not None and expires_at > datetime.now(timezone.utc)


def humanize_timeleft(delta: timedelta, *, suffix: bool = True) -> str:
    """
    Renders a timedelta as a single friendly '<value> <unit>' string using
    the largest whole unit that fits (e.g. '1 month', '3 weeks',
    '5 seconds'), so it reads naturally regardless of whether the whitelist
    duration was 5 minutes or a full year.

    By default appends " left" (e.g. "1 month left") for standalone use
    like a "Time Left" field. Pass suffix=False for call sites that already
    supply their own framing -- e.g. "You can reset your HWID again in
    {...}" reads better as "... in 6 days." than "... in 6 days left."

    Month/year lengths are approximate (30/365 days) since this is a
    human-readable countdown, not a calendar calculation.
    """
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "Expired"

    units = [
        ("year", 31536000),
        ("month", 2592000),
        ("week", 604800),
        ("day", 86400),
        ("hour", 3600),
        ("minute", 60),
        ("second", 1),
    ]
    for name, seconds_per_unit in units:
        value = total_seconds // seconds_per_unit
        if value >= 1:
            label = name if value == 1 else f"{name}s"
            return f"{value} {label} left" if suffix else f"{value} {label}"
    return "Expired"


def hwid_reset_cooldown_remaining(entry: Dict[str, Any]) -> Optional[timedelta]:
    """Returns how much time is left before `entry` can use the control
    panel's "Reset HWID" button again, based on its LastHwidReset field and
    RESET_HWID_COOLDOWN. Returns None if a reset is allowed right now --
    either because LastHwidReset is missing/unparseable (never reset
    before), or because RESET_HWID_COOLDOWN has already elapsed since the
    last one."""
    last_reset = parse_join_date(entry.get("LastHwidReset"))
    if not last_reset:
        return None

    remaining = RESET_HWID_COOLDOWN - (datetime.now(timezone.utc) - last_reset)
    return remaining if remaining.total_seconds() > 0 else None


# =========================================================================
# Embed helpers
# =========================================================================
#
# Every command used to hand-roll its own success/error messages - some as
# plain strings, some as one-off discord.Embed() calls with inconsistent
# colors/titles. These helpers standardize that: build_embed() is the base
# builder, success_embed()/error_embed() are thin presets on top of it, and
# send_success()/send_error() build + dispatch in one call (respecting
# whether the interaction has already been responded to, same as
# safe_respond).

DEFAULT_SUCCESS_COLOR = discord.Color.green()
DEFAULT_ERROR_COLOR = discord.Color.red()


def build_embed(
    title: Optional[str] = None,
    description: Optional[str] = None,
    *,
    color: discord.Color = discord.Color.blue(),
    fields: Optional[List[Tuple[str, Any, bool]]] = None,
    footer: Optional[str] = None,
    thumbnail: Optional[str] = None,
    author: Optional[str] = None,
    author_icon: Optional[str] = None,
    url: Optional[str] = None,
    timestamp: Optional[datetime] = None,
) -> discord.Embed:
    """
    General-purpose embed builder used under the hood by success_embed()/
    error_embed(), but also handy on its own for anything that doesn't
    neatly fit the success/error mold (e.g. informational lookups).

    `fields` accepts (name, value) or (name, value, inline) tuples so
    callers don't have to chain .add_field() themselves.
    """
    embed = discord.Embed(title=title, description=description, color=color, url=url)
    if timestamp is not None:
        embed.timestamp = timestamp

    for field in fields or []:
        if len(field) == 3:
            name, value, inline = field
        else:
            name, value = field
            inline = False
        embed.add_field(name=name, value=value if value not in (None, "") else "N/A", inline=inline)

    if footer:
        embed.set_footer(text=footer)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if author:
        embed.set_author(name=author, icon_url=author_icon)

    return embed


def success_embed(
    description: Optional[str] = None,
    *,
    title: str = "Success",
    color: discord.Color = DEFAULT_SUCCESS_COLOR,
    **kwargs,
) -> discord.Embed:
    """Green-flagged embed for confirming a command completed as expected."""
    return build_embed(title, description, color=color, **kwargs)


def error_embed(
    description: Optional[str] = None,
    *,
    title: str = "Error",
    color: discord.Color = DEFAULT_ERROR_COLOR,
    **kwargs,
) -> discord.Embed:
    """Red-flagged embed for validation failures, exceptions, or 'not found' results."""
    return build_embed(title, description, color=color, **kwargs)


# =========================================================================
# Discord interaction helpers
# =========================================================================

async def safe_respond(interaction: discord.Interaction, content: Optional[str] = None, **kwargs):
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content=content, **kwargs)
        else:
            await interaction.followup.send(content=content, **kwargs)
    except discord.NotFound:
        print("Interaction expired before it could be responded to.")
    except discord.HTTPException as e:
        # interaction.response.is_done() only reflects *this* Interaction
        # object's local state, which can be wrong if some other response
        # already reached Discord for the same underlying interaction (e.g.
        # two bot processes briefly running at once, or a duplicate gateway
        # dispatch) -- Discord then rejects the "initial response" slot as
        # already used (error code 40060), even though this object never
        # saw that happen. The followup webhook still works regardless of
        # who used the initial response, so retry through that instead of
        # just dropping the message.
        if getattr(e, "code", None) == 40060:
            try:
                await interaction.followup.send(content=content, **kwargs)
            except Exception as e2:
                print(f"Failed to respond via followup after an already-acknowledged error: {e2}")
        else:
            print(f"Failed to respond: {e}")
    except Exception as e:
        print(f"Failed to respond: {e}")


async def send_success(
    interaction: discord.Interaction,
    description: Optional[str] = None,
    *,
    title: str = "Success",
    ephemeral: bool = True,
    fields: Optional[List[Tuple[str, Any, bool]]] = None,
    footer: Optional[str] = None,
    thumbnail: Optional[str] = None,
    embeds: Optional[List[discord.Embed]] = None,
    **kwargs,
):
    """
    Builds a success_embed() and sends it via safe_respond() in one call.
    Pass `embeds=[...]` to ship the success embed alongside another (e.g. a
    data embed) in the same message.
    """
    embed = success_embed(description, title=title, fields=fields, footer=footer, thumbnail=thumbnail)
    if embeds is not None:
        await safe_respond(interaction, embeds=[embed, *embeds], ephemeral=ephemeral, **kwargs)
    else:
        await safe_respond(interaction, embed=embed, ephemeral=ephemeral, **kwargs)


async def send_error(
    interaction: discord.Interaction,
    description: Optional[str] = None,
    *,
    title: str = "Error",
    ephemeral: bool = True,
    fields: Optional[List[Tuple[str, Any, bool]]] = None,
    footer: Optional[str] = None,
    thumbnail: Optional[str] = None,
    **kwargs,
):
    """Builds an error_embed() and sends it via safe_respond() in one call."""
    embed = error_embed(description, title=title, fields=fields, footer=footer, thumbnail=thumbnail)
    await safe_respond(interaction, embed=embed, ephemeral=ephemeral, **kwargs)


async def edit_or_send_error(
    interaction: discord.Interaction,
    description: Optional[str] = None,
    *,
    title: str = "Error",
    fields: Optional[List[Tuple[str, Any, bool]]] = None,
    footer: Optional[str] = None,
    thumbnail: Optional[str] = None,
):
    """
    Reports a failure without leaving a stray placeholder message behind.

    Commands like /ban or /mute send a visible "Processing..." message via
    interaction.response.send_message() before doing the real work. If that
    work then fails, calling send_error() would just post a brand new
    followup message underneath the still-visible "Processing..." message,
    since the interaction has already been responded to. This instead edits
    that original response in place to show the error, since the operation
    failed anyway and there's nothing left to preserve in it.

    Falls back to send_error() if there's no original response yet (or it's
    since been deleted), so this is always safe to call from an except block.
    """
    embed = error_embed(description, title=title, fields=fields, footer=footer, thumbnail=thumbnail)
    if not interaction.response.is_done():
        await send_error(interaction, description, title=title, fields=fields, footer=footer, thumbnail=thumbnail)
        return
    try:
        await interaction.edit_original_response(content=None, embed=embed)
    except discord.NotFound:
        await send_error(interaction, description, title=title, fields=fields, footer=footer, thumbnail=thumbnail)


async def notify_user(user, action: str, moderator, reason: str, guild_name: str):
    titles = {
        "muted": (f"You have been muted in {guild_name}", discord.Color.red()),
        "banned": (f"You have been banned from {guild_name}", discord.Color.red()),
        "unmuted": (f"You have been unmuted in {guild_name}", discord.Color.green()),
        "kicked": (f"You have been kicked from {guild_name}", discord.Color.red()),
    }
    title, color = titles.get(action, (f"Notification from {guild_name}", discord.Color.blue()))

    try:
        embed = discord.Embed(
            title=title,
            description=f"**Reason:** {reason}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Moderator: {moderator}")
        await user.send(embed=embed)
    except Exception as e:
        print(f"Failed to send DM to {user}: {e}")


async def notify_permission_error(user, action: str, guild_name: str):
    """
    DMs a user to let them know something the bot tried to do on their
    behalf failed because the bot itself is missing permissions (e.g. its
    role sits below the target role, or it lacks Manage Roles entirely).

    Meant for raw gateway event handlers (reaction roles, etc.) where
    there's no interaction to reply to, so a discord.Forbidden would
    otherwise vanish into the console with no feedback to anyone.
    """
    embed = error_embed(
        title="Action Failed",
        description=(
            f"I couldn't {action} in **{guild_name}** because I'm missing permissions there. "
            "Please let a staff member know so they can fix my role/permissions."
        ),
    )
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        pass
    except Exception as e:
        print(f"Failed to DM {user} about a permission error: {e}")


def has_role(role_id: int):
    async def predicate(interaction: discord.Interaction):
        if role_id in [role.id for role in interaction.user.roles]:
            return True
        raise app_commands.CheckFailure("You do not have the required permissions to run this command.")
    return app_commands.check(predicate)


def is_in_guild(guild_id: int):
    async def predicate(interaction: discord.Interaction):
        if interaction.guild and interaction.guild.id == guild_id:
            return True
        raise app_commands.CheckFailure("This command cannot be used in this server.")
    return app_commands.check(predicate)


async def can_moderate(interaction: discord.Interaction, target: discord.Member):
    author = interaction.user
    bot_member = interaction.guild.me

    if target == author:
        raise app_commands.CheckFailure("You cannot moderate yourself.")
    if target == bot_member:
        raise app_commands.CheckFailure("You cannot moderate the bot.")
    if target.top_role >= author.top_role and author != interaction.guild.owner:
        raise app_commands.CheckFailure("Target has equal or higher role than you.")
    if target.top_role >= bot_member.top_role:
        raise app_commands.CheckFailure("Target has equal or higher role than the bot.")
    return True


# =========================================================================
# Buyer role revocation
# =========================================================================
#
# Whatever gets someone off the whitelist -- /unwhitelist, the Delete button
# on /viewwhitelist, an HWID-breach unwhitelist, a temp whitelist expiring,
# or a bulk replacement via /editwhitelist, /upload, or /rollback -- should
# never leave them holding the Buyer role afterward. Looked up by
# BUYER_ROLE_ID (declared above alongside the other Discord constants) via
# guild.get_role() rather than by name, so this keeps working even if the
# role is later renamed.


async def revoke_buyer_role(guild: Optional[discord.Guild], discord_id) -> None:
    """
    Removes the Buyer role from the given user (by Discord ID) if they're
    currently a member of `guild` and currently hold it. Silent no-op if
    the guild/member/role can't be found or the bot lacks permission --
    same "don't let this block the actual unwhitelist" philosophy as the
    best-effort DM-failure paths elsewhere in this file. Every call site
    already commits the unwhitelist itself before calling this, so a
    failure here never leaves Users.json and Discord roles in a
    contradictory state -- worst case is just a stale role that can be
    manually removed.
    """
    if guild is None:
        return

    try:
        member = guild.get_member(int(discord_id))
    except (TypeError, ValueError):
        return
    if member is None:
        return

    role = guild.get_role(BUYER_ROLE_ID)
    if role is None or role not in member.roles:
        return

    try:
        await member.remove_roles(role, reason="Unwhitelisted -- Buyer role revoked")
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"Failed to revoke Buyer role (ID {BUYER_ROLE_ID}) from {discord_id}: {e}")


def find_removed_discord_ids(old_users: List[Dict[str, Any]], new_users: List[Dict[str, Any]]) -> List[str]:
    """
    Returns every DiscordId present in `old_users` but absent from
    `new_users` -- i.e. everyone a bulk replacement (/editwhitelist,
    /upload, /rollback) implicitly unwhitelisted by committing an entire
    new Users.json rather than removing one targeted entry. Used to drive
    revoke_buyer_role() for those three commands, since they have no single
    "the target" the way /unwhitelist does.
    """
    if not isinstance(old_users, list) or not isinstance(new_users, list):
        return []
    old_ids = {u.get("DiscordId") for u in old_users if isinstance(u, dict) and u.get("DiscordId")}
    new_ids = {u.get("DiscordId") for u in new_users if isinstance(u, dict) and u.get("DiscordId")}
    return list(old_ids - new_ids)
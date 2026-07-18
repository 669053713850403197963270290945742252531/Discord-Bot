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
from typing import Any, Dict, List, Optional, Tuple

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

# Timezone JoinDate values are displayed/stored in (handles EST/EDT automatically)
LOCAL_TZ = ZoneInfo("America/New_York")


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

# Alias kept so any code that still refers to the old name keeps working.
GITHUB_FILE_URL = RAW_URL

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
    """Commits a raw string as the new Users.json content."""
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
            return await resp.json()
    finally:
        if should_close:
            await sess.close()


async def commit_users(users: List[Dict[str, Any]], sha: str, message: str, session: Optional[aiohttp.ClientSession] = None) -> Dict[str, Any]:
    """Serializes `users` to indented JSON and commits it as the new Users.json."""
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


def format_discord_timestamp(date_str: Optional[str], fmt: str = "D") -> str:
    """Converts an 'm/d/yyyy, h:mm:ss AM/PM' string into a Discord <t:...:fmt> timestamp, falling back to the raw string on failure.

    Also accepts the older 'yyyy-mm-dd' format for entries created before the JoinDate format change (assumed UTC, since that's how it was originally stored).
    """
    if not date_str:
        return "N/A"

    try:
        dt = datetime.strptime(date_str, "%m/%d/%Y, %I:%M:%S %p").replace(tzinfo=LOCAL_TZ)
        return f"<t:{int(dt.timestamp())}:{fmt}>"
    except ValueError:
        pass

    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return f"<t:{int(dt.timestamp())}:{fmt}>"
    except ValueError:
        pass

    return date_str


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


def humanize_timeleft(delta: timedelta) -> str:
    """
    Renders a timedelta as a single friendly '<value> <unit> left' string
    using the largest whole unit that fits (e.g. '1 month left',
    '3 weeks left', '5 seconds left'), so it reads naturally regardless of
    whether the whitelist duration was 5 minutes or a full year.

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
            return f"{value} {label} left"
    return "Expired"


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
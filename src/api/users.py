"""User-record helpers: lookups, building new entries, and buyer-role revocation."""

from typing import Any, Dict, List, Optional, Tuple

import discord

from . import config
from .time_utils import format_join_date


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
# Buyer role revocation
# =========================================================================
#
# Whatever gets someone off the whitelist -- /unwhitelist, the Delete button
# on /viewwhitelist, an HWID-breach unwhitelist, a temp whitelist expiring,
# or a bulk replacement via /editwhitelist, /upload, or /rollback -- should
# never leave them holding the Buyer role afterward.

async def revoke_buyer_role(guild: Optional[discord.Guild], discord_id) -> None:
    """
    Removes the Buyer role from the given user (by Discord ID) if they're
    currently a member of `guild` and currently hold it. Silent no-op if
    the guild/member/role can't be found or the bot lacks permission --
    every call site already commits the unwhitelist itself before calling
    this, so a failure here never leaves Users.json and Discord roles in a
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

    role = guild.get_role(config.BUYER_ROLE_ID)
    if role is None or role not in member.roles:
        return

    try:
        await member.remove_roles(role, reason="Unwhitelisted -- Buyer role revoked")
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"Failed to revoke Buyer role (ID {config.BUYER_ROLE_ID}) from {discord_id}: {e}")


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

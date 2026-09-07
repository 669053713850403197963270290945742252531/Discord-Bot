"""User/licence presentation helpers.

Persistence lives in :mod:`api.supabase_db`; this module only handles
Discord-facing record helpers and role cleanup.
"""

from typing import Any, Dict, List, Optional, Tuple

import discord

from . import config


def find_user_by_discord_id(users: List[Dict[str, Any]], discord_id) -> Optional[Dict[str, Any]]:
    discord_id = str(discord_id)
    return next((u for u in users if str(u.get("DiscordId")) == discord_id), None)


def find_user_by_key(users: List[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
    return next((u for u in users if str(u.get("Key") or "") == str(key)), None)


def remove_user_by_discord_id(users: List[Dict[str, Any]], discord_id) -> Tuple[List[Dict[str, Any]], bool]:
    discord_id = str(discord_id)
    filtered = [u for u in users if str(u.get("DiscordId")) != discord_id]
    return filtered, len(filtered) != len(users)


def build_user_entry(
    identifier: str,
    rank: str,
    discord_id: Optional[str],
    key: str,
    notes: Optional[str] = None,
    activated: Optional[str] = None,
    games: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "Identifier": identifier,
        "DiscordId": str(discord_id) if discord_id not in (None, "") else None,
        "Rank": rank,
        "Activated": activated,
        "Key": key,
        "Notes": notes,
        "Games": games if games is not None else ["*"],
        "Executions": 0,
        "Enabled": True,
        "ExpiresAt": None,
        "CreatedAt": None,
        "UpdatedAt": None,
    }


async def revoke_buyer_role(guild: Optional[discord.Guild], discord_id) -> None:
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
    if not isinstance(old_users, list) or not isinstance(new_users, list):
        return []
    old_ids = {str(u.get("DiscordId")) for u in old_users if isinstance(u, dict) and u.get("DiscordId")}
    new_ids = {str(u.get("DiscordId")) for u in new_users if isinstance(u, dict) and u.get("DiscordId")}
    return list(old_ids - new_ids)

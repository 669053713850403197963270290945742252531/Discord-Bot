"""
Central logging for the staff Alerts channel.

Every command that changes the whitelist, keys, HWIDs, temp access, or the
Bot Access role posts one compact embed here via send_alert() -- so staff
can watch everything happening across the bot from one place, the same way
the control panel's "Key Redeemed" / "Potential Breach" alerts already
worked before this was generalized.

Kept deliberately small per-alert (one description line naming who did
what to whom, plus a couple of essential fields at most) since this
channel can see a lot of traffic -- full context belongs in the commit
message on GitHub, not in every embed here.
"""

from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

import discord
from discord.ext import commands
from discord.ui import View

from . import config
from .discord_helpers import build_embed

# Standard colors so every alert's severity reads consistently at a glance.
ALERT_COLOR_ADD = discord.Color.green()      # whitelisted, key(s) generated, access granted
ALERT_COLOR_REMOVE = discord.Color.red()     # unwhitelisted, deleted, keys cleared, access removed
ALERT_COLOR_EDIT = discord.Color.blue()      # field edits, cooldown clears, notes cleared
ALERT_COLOR_TEMP = discord.Color.gold()      # temp whitelist grants/extensions
ALERT_COLOR_CAUTION = discord.Color.orange() # force-actions, bulk JSON edits, rollbacks/uploads

# Runtime mute switch for /togglealerts (commands.access). In-memory only
# and process-local -- deliberately not persisted anywhere (no file, no
# GitHub commit) since this exists purely to quiet the channel during a
# testing session, not to be a durable setting. Defaults to enabled so a
# restart mid-testing fails open (alerts resume) instead of silently
# staying muted with no indication why.
_alerts_enabled = True


def alerts_enabled() -> bool:
    """Current mute state, for anything that wants to reflect it (e.g. a
    status command) without importing the private flag directly."""
    return _alerts_enabled


def set_alerts_enabled(value: bool) -> bool:
    """Sets the mute state. Returns the new state for convenience at call
    sites that want to immediately report it back."""
    global _alerts_enabled
    _alerts_enabled = value
    return _alerts_enabled


async def send_alert(bot: commands.Bot, embed: discord.Embed, view: Optional[View] = None, *, bypass_mute: bool = False) -> Optional[discord.Message]:
    """Best-effort delivery to the staff Alerts channel. A missing channel
    or delivery failure here is logged and swallowed rather than surfaced
    to the acting user -- their command already succeeded or failed on its
    own, independent of whether staff got notified about it.

    Silently no-ops while alerts are muted via /togglealerts, unless
    `bypass_mute` is set -- used only by /togglealerts itself, so the
    on/off toggle is always visible even though everything else it would
    otherwise trigger is suppressed.

    Returns the sent Message (so a caller that needs its id -- e.g. to
    persist a breach alert's message_id to BotState.json for reconciliation
    on restart -- can capture it), or None if nothing was actually sent
    (muted, missing channel, or a delivery failure)."""
    if not _alerts_enabled and not bypass_mute:
        return None

    channel = bot.get_channel(config.ALERTS_CHANNEL_ID)
    if not channel:
        print(f"Alerts channel not found (ALERTS_CHANNEL_ID={config.ALERTS_CHANNEL_ID}).")
        return None

    try:
        if view is not None:
            return await channel.send(embed=embed, view=view)
        else:
            return await channel.send(embed=embed)
    except Exception as e:
        print(f"Failed to send alert to Alerts channel: {e}")
        return None


def alert_embed(
    title: str,
    description: Optional[str] = None,
    *,
    color: discord.Color,
    fields: Optional[List[Tuple[str, Any, bool]]] = None,
) -> discord.Embed:
    """Thin wrapper around build_embed() with the timestamp every alert
    should carry always set, so call sites don't have to repeat it."""
    return build_embed(title=title, description=description, color=color, fields=fields, timestamp=datetime.now(timezone.utc))
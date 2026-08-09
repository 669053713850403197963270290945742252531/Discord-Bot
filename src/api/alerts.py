"""
Central logging for the staff alerts channels.

Every command that changes the whitelist, keys, HWIDs, temp access, or the
Bot Access role posts one compact embed to the (whitelist) Alerts channel via
send_alert() -- so staff can watch everything happening across the bot from
one place, the same way the control panel's "Key Redeemed" / "Potential
Breach" alerts already worked before this was generalized.

Every moderation action -- bans, kicks, mutes, unmutes, unbans, purges, temp
roles, DMs, ghost pings, slowmode changes, and channel/server lock toggles --
posts to a separate Moderation Alerts channel via send_moderation_alert()
instead. Kept apart from the whitelist stream so a busy moderation channel
doesn't drown out whitelist traffic (or vice versa), and so either one can be
muted independently via /togglealerts whitelist|moderation.

Kept deliberately small per-alert (one description line naming who did
what to whom, plus a couple of essential fields at most) since these
channels can see a lot of traffic -- full context belongs in the commit
message on GitHub, not in every embed here.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands
from discord.ui import View

from . import config
from .discord_helpers import build_embed
from .github import GitHubAPIError, fetch_botstate_with_sha, update_botstate

# Standard colors so every alert's severity reads consistently at a glance,
# regardless of which channel it ends up in.
ALERT_COLOR_ADD = discord.Color.green()      # whitelisted, key(s) generated, access granted, unbanned/unmuted
ALERT_COLOR_REMOVE = discord.Color.red()     # unwhitelisted, deleted, keys cleared, access removed, banned/kicked/muted
ALERT_COLOR_EDIT = discord.Color.blue()      # field edits, cooldown clears, notes cleared, neutral setting changes
ALERT_COLOR_TEMP = discord.Color.gold()      # temp whitelist/role grants/extensions
ALERT_COLOR_CAUTION = discord.Color.orange() # force-actions, bulk JSON edits, rollbacks/uploads, locks, purges

# Runtime mute switches for /togglealerts whitelist and /togglealerts
# moderation, respectively. Kept in-memory for fast access from every
# send_alert()/send_moderation_alert() call, and mirrored to BotState.json's
# "alerts_enabled" key ({"whitelist": ..., "moderation": ...}) on every
# toggle -- see persist_alerts_enabled_state()/reconcile_alerts_enabled()
# below -- so a restart resumes whichever channel(s) staff last muted
# instead of silently reopening them. Both default to enabled so a restart
# before reconciliation runs (or if reconciliation itself fails) fails open
# (alerts resume) instead of silently staying muted with no indication why.
# Two independent flags -- rather than one shared switch -- so muting one
# stream never silences the other.
_alerts_enabled = True
_moderation_alerts_enabled = True


def alerts_enabled() -> bool:
    """Current mute state of the (whitelist) Alerts channel, for anything
    that wants to reflect it (e.g. a status command) without importing the
    private flag directly."""
    return _alerts_enabled


def set_alerts_enabled(value: bool) -> bool:
    """Sets the (whitelist) Alerts channel's mute state. Returns the new
    state for convenience at call sites that want to immediately report it
    back. In-memory only -- callers that want the change to survive a
    restart should follow up with persist_alerts_enabled_state()."""
    global _alerts_enabled
    _alerts_enabled = value
    return _alerts_enabled


def moderation_alerts_enabled() -> bool:
    """Current mute state of the Moderation Alerts channel -- the
    moderation-side counterpart to alerts_enabled() above."""
    return _moderation_alerts_enabled


def set_moderation_alerts_enabled(value: bool) -> bool:
    """Sets the Moderation Alerts channel's mute state. Returns the new
    state for convenience at call sites that want to immediately report it
    back. In-memory only -- callers that want the change to survive a
    restart should follow up with persist_alerts_enabled_state()."""
    global _moderation_alerts_enabled
    _moderation_alerts_enabled = value
    return _moderation_alerts_enabled


async def persist_alerts_enabled_state(message: str):
    """Mirrors both in-memory mute switches to BotState.json in one commit.
    Called after set_alerts_enabled()/set_moderation_alerts_enabled() so
    either toggle's new state survives a restart. Best-effort -- logged
    rather than raised, since the mute switch itself has already taken
    effect in-process by the time this runs; a failure here only means it
    would fall back to enabled on the next restart instead of resuming
    where it left off."""
    def _mutate(state):
        state["alerts_enabled"] = {
            "whitelist": _alerts_enabled,
            "moderation": _moderation_alerts_enabled,
        }
        return state
    try:
        await update_botstate(_mutate, message)
    except GitHubAPIError as e:
        print(f"Failed to persist alert mute state to BotState.json: {e}")


async def reconcile_alerts_enabled(bot: commands.Bot, state: Optional[Dict[str, Any]] = None):
    """Called once from on_ready: restores both mute switches from
    BotState.json, so a restart resumes whichever /togglealerts channel(s)
    staff last muted instead of silently reopening them.

    `state` lets a caller that's already fetched BotState.json hand it over
    directly instead of this making its own redundant fetch -- see
    commands.moderation.reconcile_temp_bans() for the full reasoning."""
    global _alerts_enabled, _moderation_alerts_enabled
    if state is None:
        try:
            state, _sha = await fetch_botstate_with_sha()
        except GitHubAPIError as e:
            print(f"Failed to fetch BotState.json for alert mute state reconciliation: {e}")
            return

    saved = state.get("alerts_enabled") or {}
    _alerts_enabled = bool(saved.get("whitelist", True))
    _moderation_alerts_enabled = bool(saved.get("moderation", True))

    if not _alerts_enabled or not _moderation_alerts_enabled:
        print(
            "Reconciled alert mute state from BotState.json "
            f"(whitelist={_alerts_enabled}, moderation={_moderation_alerts_enabled})."
        )


async def _deliver_alert(
    bot: commands.Bot,
    channel_id: int,
    channel_label: str,
    embed: discord.Embed,
    view: Optional[View],
) -> Optional[discord.Message]:
    """Shared delivery mechanics for send_alert()/send_moderation_alert() --
    both are just this pointed at a different channel_id, after each has
    already checked its own mute switch. A missing channel or delivery
    failure here is logged and swallowed rather than surfaced to the acting
    user -- their command already succeeded or failed on its own,
    independent of whether staff got notified about it."""
    channel = bot.get_channel(channel_id)
    if not channel:
        print(f"{channel_label} channel not found (channel_id={channel_id}).")
        return None

    try:
        if view is not None:
            return await channel.send(embed=embed, view=view)
        else:
            return await channel.send(embed=embed)
    except Exception as e:
        print(f"Failed to send alert to {channel_label} channel: {e}")
        return None


async def send_alert(bot: commands.Bot, embed: discord.Embed, view: Optional[View] = None, *, bypass_mute: bool = False) -> Optional[discord.Message]:
    """Best-effort delivery to the staff (whitelist) Alerts channel.

    Silently no-ops while whitelist alerts are muted via /togglealerts
    whitelist, unless `bypass_mute` is set -- used only by that toggle
    itself, so the on/off state is always visible even though everything
    else it would otherwise trigger is suppressed.

    Returns the sent Message (so a caller that needs its id -- e.g. to
    persist a breach alert's message_id to BotState.json for reconciliation
    on restart -- can capture it), or None if nothing was actually sent
    (muted, missing channel, or a delivery failure)."""
    if not _alerts_enabled and not bypass_mute:
        return None
    return await _deliver_alert(bot, config.ALERTS_CHANNEL_ID, "Alerts", embed, view)


async def send_moderation_alert(bot: commands.Bot, embed: discord.Embed, view: Optional[View] = None, *, bypass_mute: bool = False) -> Optional[discord.Message]:
    """Best-effort delivery to the staff Moderation Alerts channel -- the
    moderation-side counterpart to send_alert() above.

    Silently no-ops while moderation alerts are muted via /togglealerts
    moderation, unless `bypass_mute` is set -- used only by that toggle
    itself, for the same reason send_alert()'s bypass_mute exists.

    Returns the sent Message, or None if nothing was actually sent (muted,
    missing channel, or a delivery failure)."""
    if not _moderation_alerts_enabled and not bypass_mute:
        return None
    return await _deliver_alert(bot, config.MODERATION_ALERTS_CHANNEL_ID, "Moderation Alerts", embed, view)


def alert_embed(
    title: str,
    description: Optional[str] = None,
    *,
    color: discord.Color,
    fields: Optional[List[Tuple[str, Any, bool]]] = None,
) -> discord.Embed:
    """Thin wrapper around build_embed() with the timestamp every alert
    should carry always set, so call sites don't have to repeat it. Shared
    by both send_alert() and send_moderation_alert() -- an alert embed looks
    the same regardless of which channel it's headed to."""
    return build_embed(title=title, description=description, color=color, fields=fields, timestamp=datetime.now(timezone.utc))

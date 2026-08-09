import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, send_success, send_error
from api.alerts import (
    send_alert, send_moderation_alert, alert_embed,
    ALERT_COLOR_ADD, ALERT_COLOR_REMOVE, ALERT_COLOR_TEMP, ALERT_COLOR_CAUTION,
    alerts_enabled, set_alerts_enabled,
    moderation_alerts_enabled, set_moderation_alerts_enabled,
    persist_alerts_enabled_state,
)
from api.github import GitHubAPIError, fetch_botstate_with_sha, update_botstate, new_state_id
from api.time_utils import format_iso, parse_iso, seconds_until

GUILD = discord.Object(id=config.GUILD_ID)

# =========================================================================
# /tempaccess -- persisted to BotState.json's "temp_bot_access" list so a
# restart before the auto-removal timer fires no longer leaves the role on
# the user permanently, with zero durable trail to notice it later (unlike
# temp whitelist, which at least records its expiration in Users.json's
# Notes field even without any of this). Same "fetch -> mutate -> commit"
# shape as moderation.py's temp_bans -- see that module's comments for the
# full reasoning.
# =========================================================================

# Discord IDs currently holding the Bot Access role via /tempaccess, so a
# second grant for the same user can be rejected instead of stacking
# timers. Fast in-memory membership check; BotState.json's
# "temp_bot_access" list is the durable source of truth.
_active_temp_access: set = set()

# Running removal tasks, keyed by Discord ID (only one active grant per
# user is ever allowed -- see the _active_temp_access check in
# _tempaccess_impl below) -- lets a manual /toggleaccess that turns the
# role back off early cancel a still-pending auto-removal instead of
# leaving it to fire harmlessly-but-uselessly against an already-roleless
# user later.
_temp_access_tasks: dict = {}


async def _clear_temp_access_state(discord_id: int):
    """Removes the persisted BotState.json entry (if any) for `discord_id`.
    Best-effort -- logged rather than raised, since the Discord-side role
    change has already happened by the time this runs."""
    def _mutate(state):
        state["temp_bot_access"] = [e for e in state.get("temp_bot_access", []) if str(e.get("discord_id")) != str(discord_id)]
        return state
    try:
        await update_botstate(_mutate, f"Temp Bot Access resolved: {discord_id}")
    except GitHubAPIError as e:
        print(f"Failed to clear resolved temp Bot Access for {discord_id} from BotState.json: {e}")


async def _run_temp_access_removal(bot: commands.Bot, entry: dict):
    """Sleeps until `entry`'s expires_at (or fires almost immediately if
    that's already in the past -- e.g. the bot was down past it), then
    removes the role and clears the BotState entry. Shared by both a fresh
    /tempaccess grant and startup reconciliation, so there's exactly one
    code path for "what happens when a temp access timer goes off.\""""
    discord_id = int(entry["discord_id"])
    try:
        await asyncio.sleep(seconds_until(parse_iso(entry.get("expires_at"))))

        guild = bot.get_guild(config.GUILD_ID)
        role = guild.get_role(config.REQUIRED_ROLE_ID) if guild else None
        member = guild.get_member(discord_id) if guild else None

        if guild is None or role is None:
            print(f"Could not find guild/Bot Access role to auto-remove temp access from {discord_id}.")
        elif member and role in member.roles:
            try:
                await member.remove_roles(role, reason="Temporary Bot Access expired")
            except discord.Forbidden:
                print(f"Missing permissions to remove expired temp Bot Access role from {member}")
            else:
                await send_alert(bot, alert_embed(
                    "⌛ Temp Bot Access Expired",
                    f"{member.mention}'s temporary {role.mention} role expired and was auto-removed.",
                    color=ALERT_COLOR_REMOVE,
                ))
    except asyncio.CancelledError:
        raise
    finally:
        _active_temp_access.discard(discord_id)
        _temp_access_tasks.pop(discord_id, None)
        await _clear_temp_access_state(discord_id)


def _schedule_temp_access(bot: commands.Bot, entry: dict):
    """(Re)schedules the auto-removal task for `entry`. Cancels whatever
    task was already tracked for this user first (shouldn't normally
    happen -- each entry only gets scheduled once, at grant time or at
    startup -- but keeps this safe to call more than once for the same
    user)."""
    discord_id = int(entry["discord_id"])
    existing = _temp_access_tasks.get(discord_id)
    if existing and not existing.done():
        existing.cancel()
    _active_temp_access.add(discord_id)
    _temp_access_tasks[discord_id] = bot.loop.create_task(_run_temp_access_removal(bot, entry))


async def reconcile_temp_access(bot: commands.Bot, state: Optional[Dict[str, Any]] = None):
    """Called once from on_ready: re-schedules every temp Bot Access
    grant's auto-removal timer using the durable expires_at recorded in
    BotState.json, so a restart before the original timer fired no longer
    leaves the role on the user permanently with nothing anywhere to
    indicate that wasn't intentional. Entries whose expires_at has already
    passed fire (almost) immediately via seconds_until()'s clamp-to-zero,
    rather than staying granted indefinitely until someone notices.

    `state` lets a caller that's already fetched BotState.json (e.g.
    start.py's on_ready, reconciling several categories back to back) hand
    it over directly instead of this making its own redundant fetch of the
    exact same file. Falls back to fetching it itself when called on its
    own with nothing passed in."""
    if state is None:
        try:
            state, _sha = await fetch_botstate_with_sha()
        except GitHubAPIError as e:
            print(f"Failed to fetch BotState.json for temp Bot Access reconciliation: {e}")
            return

    entries = state.get("temp_bot_access", [])
    for entry in entries:
        _schedule_temp_access(bot, entry)

    if entries:
        print(f"Reconciled {len(entries)} temp Bot Access grant(s) from BotState.json.")


async def _toggleaccess_impl(interaction: discord.Interaction, user: discord.Member):
    guild = interaction.guild
    role = guild.get_role(config.REQUIRED_ROLE_ID)
    if not role:
        return await send_error(interaction, "Bot Access role not found.")

    if role in user.roles:
        await user.remove_roles(role, reason=f"Toggled off Bot Access role by {interaction.user}")

        # If this role came from a still-pending /tempaccess grant, cancel
        # its auto-removal timer and clear the BotState.json entry -- same
        # reasoning as /unban cancelling a temp ban in moderation.py --
        # otherwise the stale timer fires harmlessly-but-uselessly later
        # and leaves a confusing stray entry in BotState.json until then.
        task = _temp_access_tasks.pop(user.id, None)
        if task:
            task.cancel()
        _active_temp_access.discard(user.id)
        await _clear_temp_access_state(user.id)

        await send_alert(interaction.client, alert_embed(
            "🔒 Bot Access Removed",
            f"{interaction.user.mention} removed the {role.mention} role from {user.mention} via `/toggleaccess`.",
            color=ALERT_COLOR_REMOVE,
        ))
        await send_success(interaction, f"Removed {role.name} role from {user.mention}.")
    else:
        await user.add_roles(role, reason=f"Toggled on Bot Access role by {interaction.user}")
        await send_alert(interaction.client, alert_embed(
            "🔓 Bot Access Granted",
            f"{interaction.user.mention} granted the {role.mention} role to {user.mention} via `/toggleaccess`.",
            color=ALERT_COLOR_ADD,
        ))
        await send_success(interaction, f"Granted {role.name} role to {user.mention}.")


async def _tempaccess_impl(interaction: discord.Interaction, user: discord.Member, minutes: int):
    await interaction.response.defer(ephemeral=True)

    if minutes <= 0:
        return await send_error(interaction, "Duration must be a positive integer.")

    guild = interaction.client.get_guild(config.GUILD_ID)
    role = guild.get_role(config.REQUIRED_ROLE_ID)
    if not role:
        return await send_error(interaction, "Bot Access role not found.")

    if role in user.roles:
        return await send_error(interaction, f"{user.mention} already has the Bot Access role.")

    if user.id in _active_temp_access:
        return await send_error(interaction, f"{user.mention} already has a temporary access timer running.")

    try:
        await user.add_roles(role, reason=f"Temporary Bot Access for {minutes} minutes")
    except Exception as e:
        return await send_error(interaction, f"Failed to give Bot Access role: {e}")

    granted_at = datetime.now(timezone.utc)
    expires_at = granted_at + timedelta(minutes=minutes)
    entry = {
        "id": new_state_id("ta"),
        "discord_id": str(user.id),
        "granted_at": format_iso(granted_at),
        "expires_at": format_iso(expires_at),
        "granted_by_id": str(interaction.user.id),
        "granted_by_tag": str(interaction.user),
    }

    try:
        def _mutate(state, entry=entry):
            state.setdefault("temp_bot_access", []).append(entry)
            return state
        await update_botstate(_mutate, f"Temp Bot Access recorded: {user} ({user.id})")
    except GitHubAPIError as e:
        # The role grant itself already succeeded (add_roles() above) --
        # this only means the auto-removal timer won't survive a restart
        # until BotState.json can be reached again. Still schedule the
        # in-memory task below so this process's own timer works
        # regardless, and flag it to staff since temp access silently
        # becoming permanent on the next restart is exactly the failure
        # mode this persistence exists to prevent.
        print(f"Failed to persist temp Bot Access for {user} to BotState.json: {e}")
        await send_alert(interaction.client, alert_embed(
            "⚠️ Temp Bot Access Not Persisted",
            f"{user.mention}'s temporary Bot Access couldn't be saved to BotState.json ({e}). "
            "It will still auto-remove on schedule *this session*, but would stay on the user "
            "permanently if the bot restarts before then.",
            color=ALERT_COLOR_CAUTION,
        ))

    _schedule_temp_access(interaction.client, entry)

    await send_alert(interaction.client, alert_embed(
        "🔓 Temp Bot Access Granted",
        f"{interaction.user.mention} granted {user.mention} the {role.mention} role for {minutes} minute(s) via `/tempaccess`.",
        color=ALERT_COLOR_TEMP,
    ))
    await send_success(interaction, f"Given Bot Access role to {user.mention} for {minutes} minutes.")


async def _togglealerts_whitelist_impl(interaction: discord.Interaction):
    """Flips api.alerts's whitelist-side mute switch (the Alerts channel --
    whitelist/keys/HWID/temp access/Bot Access role changes). The toggle
    itself always posts to the Alerts channel (bypass_mute=True) even when
    turning alerts *off*, so there's a visible record of exactly when/why
    the channel went quiet instead of it just stopping with no trace.
    Persisted to BotState.json so the mute state survives a restart
    instead of failing back open."""
    now_enabled = set_alerts_enabled(not alerts_enabled())
    await persist_alerts_enabled_state(
        f"Whitelist alerts {'enabled' if now_enabled else 'disabled'} by {interaction.user} ({interaction.user.id})"
    )

    if now_enabled:
        await send_alert(interaction.client, alert_embed(
            "🔔 Alerts Re-Enabled",
            f"{interaction.user.mention} re-enabled whitelist alerts via `/togglealerts whitelist`.",
            color=ALERT_COLOR_ADD,
        ), bypass_mute=True)
        await send_success(interaction, "Whitelist alerts have been **re-enabled** -- staff will see alert embeds again.")
    else:
        await send_alert(interaction.client, alert_embed(
            "🔕 Alerts Disabled",
            f"{interaction.user.mention} disabled whitelist alerts via `/togglealerts whitelist`. "
            "No further alert embeds will post here until this is toggled back on.",
            color=ALERT_COLOR_CAUTION,
        ), bypass_mute=True)
        await send_success(interaction, "Whitelist alerts have been **disabled** -- no alert embeds will post to the Alerts channel until this is toggled back on.")


async def _togglealerts_moderation_impl(interaction: discord.Interaction):
    """Moderation-side counterpart to _togglealerts_whitelist_impl() above --
    flips api.alerts's moderation-side mute switch (the separate Moderation
    Alerts channel that /ban, /kick, /mute, lock/lockdown toggles, and the
    rest of commands.moderation post to) instead of the whitelist one. Same
    always-visible-toggle, persisted-to-BotState.json conventions as the
    whitelist version -- see that function's docstring for the full
    reasoning."""
    now_enabled = set_moderation_alerts_enabled(not moderation_alerts_enabled())
    await persist_alerts_enabled_state(
        f"Moderation alerts {'enabled' if now_enabled else 'disabled'} by {interaction.user} ({interaction.user.id})"
    )

    if now_enabled:
        await send_moderation_alert(interaction.client, alert_embed(
            "🔔 Alerts Re-Enabled",
            f"{interaction.user.mention} re-enabled moderation alerts via `/togglealerts moderation`.",
            color=ALERT_COLOR_ADD,
        ), bypass_mute=True)
        await send_success(interaction, "Moderation alerts have been **re-enabled** -- staff will see alert embeds again.")
    else:
        await send_moderation_alert(interaction.client, alert_embed(
            "🔕 Alerts Disabled",
            f"{interaction.user.mention} disabled moderation alerts via `/togglealerts moderation`. "
            "No further alert embeds will post here until this is toggled back on.",
            color=ALERT_COLOR_CAUTION,
        ), bypass_mute=True)
        await send_success(interaction, "Moderation alerts have been **disabled** -- no alert embeds will post to the Moderation Alerts channel until this is toggled back on.")


class Access(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="toggleaccess", description="Toggle the Bot Access role for a user.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="User to toggle the role for")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def toggleaccess(self, interaction: discord.Interaction, user: discord.Member):
        await _toggleaccess_impl(interaction, user)

    @app_commands.command(name="tempaccess", description="Temporarily applies the Bot Access role to a user (in minutes).")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="User to give temporary access", minutes="Duration in minutes")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def tempaccess(self, interaction: discord.Interaction, user: discord.Member, minutes: int):
        await _tempaccess_impl(interaction, user, minutes)

    # /togglealerts whitelist and /togglealerts moderation share a single
    # guild-scoped group -- same pattern as afk.py's afk_group / moderation.py's
    # ghostping_group -- so the guild restriction only needs to live once,
    # here on the top-level group, for both subcommands to inherit it.
    togglealerts_group = app_commands.guilds(GUILD)(
        app_commands.Group(
            name="togglealerts",
            description="Toggles whether alert embeds post to a staff alerts channel (e.g. during testing).",
        )
    )

    @togglealerts_group.command(name="whitelist", description="Toggles whether alert embeds post to the staff Alerts channel (whitelist/keys/HWID/access changes).")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def togglealerts_whitelist(self, interaction: discord.Interaction):
        await _togglealerts_whitelist_impl(interaction)

    @togglealerts_group.command(name="moderation", description="Toggles whether alert embeds post to the staff Moderation Alerts channel (bans/kicks/mutes/locks).")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def togglealerts_moderation(self, interaction: discord.Interaction):
        await _togglealerts_moderation_impl(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(Access(bot))
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import (
    has_role, is_in_guild, can_moderate, notify_user, build_embed,
    send_success, send_error, edit_or_send_error, error_embed, success_embed,
    dms_enabled,
)
from api.alerts import (
    send_moderation_alert, alert_embed,
    ALERT_COLOR_ADD, ALERT_COLOR_REMOVE, ALERT_COLOR_EDIT, ALERT_COLOR_TEMP, ALERT_COLOR_CAUTION,
)
from api.github import GitHubAPIError, fetch_botstate_with_sha, update_botstate, new_state_id
from api.time_utils import format_iso, parse_iso, seconds_until

GUILD = discord.Object(id=config.GUILD_ID)


# =========================================================================
# Implementations (standalone functions so context_menus.py can call them
# directly, without needing a bound cog instance)
# =========================================================================

# =========================================================================
# /ban's temp-ban duration -- persisted to BotState.json's "temp_bans" list
# so a restart before the countdown fires reschedules the auto-unban
# instead of the "temp" ban silently becoming permanent forever. Keyed by a
# short random id (see api.github.new_state_id) rather than discord_id
# alone, since -- unlike temp whitelist/temp access -- nothing stops the
# same user from theoretically being temp-banned again after an earlier
# temp ban already resolved.
# =========================================================================

# Running unban tasks, keyed by the BotState entry's id -- lets a manual
# /unban cancel a still-pending auto-unban instead of leaving it to fire
# harmlessly-but-uselessly against an already-unbanned user later.
_temp_ban_tasks: dict = {}


async def _clear_temp_ban_state(entry_id: str):
    """Removes a resolved (fired or manually reversed) temp ban entry from
    BotState.json. Best-effort -- logged rather than raised, since the
    Discord-side ban/unban has already happened by the time this runs."""
    def _mutate(state):
        state["temp_bans"] = [e for e in state.get("temp_bans", []) if e.get("id") != entry_id]
        return state
    try:
        await update_botstate(_mutate, f"Temp ban resolved: {entry_id}")
    except GitHubAPIError as e:
        print(f"Failed to clear resolved temp ban {entry_id} from BotState.json: {e}")


async def _run_temp_ban_unban(bot: commands.Bot, entry: dict):
    """Sleeps until `entry`'s unban_at (or fires almost immediately if
    that's already in the past -- e.g. the bot was down past it), then
    unbans and clears the BotState entry. Shared by both a fresh /ban
    duration grant and startup reconciliation, so there's exactly one code
    path for "what happens when a temp ban's timer goes off.\""""
    entry_id = entry["id"]
    try:
        await asyncio.sleep(seconds_until(parse_iso(entry.get("unban_at"))))

        guild = bot.get_guild(int(entry["guild_id"]))
        if guild is None:
            print(f"Could not find guild {entry['guild_id']} to auto-unban temp ban {entry_id}.")
        else:
            try:
                await guild.unban(discord.Object(id=int(entry["discord_id"])), reason="Temporary ban expired")
            except discord.NotFound:
                pass  # Already unbanned (manually, or by a duplicate timer) -- nothing left to do.
            except Exception as e:
                print(f"Failed to auto-unban {entry['discord_id']} in guild {entry['guild_id']} (temp ban {entry_id}): {e}")
    except asyncio.CancelledError:
        raise
    finally:
        _temp_ban_tasks.pop(entry_id, None)
        await _clear_temp_ban_state(entry_id)


def _schedule_temp_ban(bot: commands.Bot, entry: dict):
    """(Re)schedules the auto-unban task for `entry`. Cancels whatever task
    was already tracked for this entry id first (shouldn't normally happen
    -- each entry only gets scheduled once, at grant time or at startup --
    but keeps this safe to call more than once for the same entry)."""
    entry_id = entry["id"]
    existing = _temp_ban_tasks.get(entry_id)
    if existing and not existing.done():
        existing.cancel()
    _temp_ban_tasks[entry_id] = bot.loop.create_task(_run_temp_ban_unban(bot, entry))


async def _cancel_temp_ban_for(discord_id, guild_id) -> bool:
    """Cancels and clears any pending temp-ban auto-unban entry for
    `discord_id` in `guild_id` -- called from /unban so a manual early
    unban doesn't leave a stale (harmless, but confusing) BotState entry
    and in-memory task sitting around until its original timer fires.
    Returns True if an entry was found and cleared."""
    try:
        state, _sha = await fetch_botstate_with_sha()
    except GitHubAPIError as e:
        print(f"Failed to fetch BotState.json while checking for a temp ban to cancel: {e}")
        return False

    match = next(
        (e for e in state.get("temp_bans", []) if e.get("discord_id") == str(discord_id) and e.get("guild_id") == str(guild_id)),
        None,
    )
    if not match:
        return False

    task = _temp_ban_tasks.pop(match["id"], None)
    if task:
        task.cancel()
    await _clear_temp_ban_state(match["id"])
    return True


async def reconcile_temp_bans(bot: commands.Bot, state: Optional[Dict[str, Any]] = None):
    """Called once from on_ready: re-schedules every temp ban's auto-unban
    timer using the durable unban_at recorded in BotState.json, so a
    restart before the original timer fired no longer leaves a "temp" ban
    permanent. Entries whose unban_at has already passed fire (almost)
    immediately via seconds_until()'s clamp-to-zero, rather than staying
    banned indefinitely until someone notices.

    `state` lets a caller that's already fetched BotState.json (e.g.
    start.py's on_ready, reconciling several categories back to back) hand
    it over directly instead of this making its own redundant fetch of the
    exact same file. Falls back to fetching it itself when called on its
    own with nothing passed in."""
    if state is None:
        try:
            state, _sha = await fetch_botstate_with_sha()
        except GitHubAPIError as e:
            print(f"Failed to fetch BotState.json for temp ban reconciliation: {e}")
            return

    entries = state.get("temp_bans", [])
    for entry in entries:
        _schedule_temp_ban(bot, entry)

    if entries:
        print(f"Reconciled {len(entries)} temp ban(s) from BotState.json.")


async def _ban_impl(interaction: discord.Interaction, target: discord.User, reason: str = "None", duration: int = None, preserve_messages: bool = True):
    try:
        await interaction.response.send_message(f"Processing ban for {target.mention}...", ephemeral=True)

        member = interaction.guild.get_member(target.id)

        # Computed once up front (rather than inside the `if member:` DM
        # block below) so the same value backs the DM, the summary embed,
        # and the BotState.json entry persisted further down -- regardless
        # of whether `target` is a current member.
        unban_time = datetime.now(timezone.utc) + timedelta(minutes=duration) if duration else None

        # Only run moderation checks and message deletion for members
        if member:
            await can_moderate(interaction, member)

            try:
                embed = discord.Embed(title=f"You have been banned from {interaction.guild.name}", description=f"**Reason:** {reason}", color=discord.Color.red(), timestamp=datetime.now(timezone.utc))

                if duration:
                    timestamp = int(unban_time.timestamp())
                    minute_label = "minute" if duration == 1 else "minutes"

                    embed.add_field(name="Duration", value=f"{duration} {minute_label}", inline=True)
                    embed.add_field(name="Unban Time", value=f"<t:{timestamp}:F>\n<t:{timestamp}:T> (<t:{timestamp}:R>)", inline=True)

                if dms_enabled():
                    await target.send(embed=embed)
            except Exception as e:
                print(f"Could not DM {member}: {e}")

            if not preserve_messages:
                print(f"Deleting messages for {member}...")
                for channel in interaction.guild.text_channels:
                    try:
                        async for msg in channel.history(limit=1000):
                            if msg.author == member:
                                await msg.delete()
                    except discord.Forbidden:
                        print(f"Missing permissions to delete messages in {channel.name}")
                    except Exception as e:
                        print(f"Error deleting messages in {channel.name}: {e}")
        else:
            # Banning globally (user isn't a current member)
            try:
                await notify_user(target, "banned", interaction.user, reason, interaction.guild.name)
            except Exception as e:
                print(f"Failed to dm {target}: {e}")
            print(f"{target} was not found in server. Moderation checks and message deletion have been skipped.")

        await interaction.guild.ban(target, reason=reason, delete_message_seconds=0 if preserve_messages else 86400)

        summary_fields = [
            ("User", f"{target} ({target.id})", False),
            ("Reason", reason, False),
            ("Messages", "Preserved" if preserve_messages else "Deleted", False),
        ]
        if duration:
            minute_label = "minute" if duration == 1 else "minutes"
            summary_fields.append(("Duration", f"{duration} {minute_label}", False))

        summary_embed = success_embed(title="Ban Summary", fields=summary_fields)
        await interaction.edit_original_response(content=None, embed=summary_embed)

        await send_moderation_alert(interaction.client, alert_embed(
            "🔨 Member Banned",
            f"{interaction.user.mention} banned {target.mention} via `/ban`.",
            color=ALERT_COLOR_REMOVE,
            fields=summary_fields,
        ))

        if duration:
            entry = {
                "id": new_state_id("tb"),
                "discord_id": str(target.id),
                "guild_id": str(interaction.guild.id),
                "reason": reason,
                "banned_at": format_iso(datetime.now(timezone.utc)),
                "unban_at": format_iso(unban_time),
                "banned_by_id": str(interaction.user.id),
                "banned_by_tag": str(interaction.user),
            }
            try:
                def _mutate(state, entry=entry):
                    state.setdefault("temp_bans", []).append(entry)
                    return state
                await update_botstate(_mutate, f"Temp ban recorded: {target} ({target.id})")
            except GitHubAPIError as e:
                # The ban itself already succeeded (guild.ban() above) --
                # this only means the auto-unban timer won't survive a
                # restart until BotState.json can be reached again. Still
                # schedule the in-memory task below so this process's own
                # timer works regardless, and flag it to staff since a
                # "temp" ban silently becoming permanent on the next
                # restart is exactly the failure mode this persistence
                # exists to prevent.
                print(f"Failed to persist temp ban for {target} to BotState.json: {e}")
                await send_moderation_alert(interaction.client, alert_embed(
                    "⚠️ Temp Ban Not Persisted",
                    f"{target.mention}'s temporary ban couldn't be saved to BotState.json ({e}). "
                    "It will still auto-unban on schedule *this session*, but would become permanent "
                    "if the bot restarts before then.",
                    color=ALERT_COLOR_CAUTION,
                ))

            _schedule_temp_ban(interaction.client, entry)

    except app_commands.CheckFailure as e:
        await edit_or_send_error(interaction, str(e))
    except discord.Forbidden:
        await edit_or_send_error(interaction, "Missing permissions to ban.")
    except Exception as e:
        await edit_or_send_error(interaction, str(e))


async def _kick_impl(interaction: discord.Interaction, target: discord.Member, reason: str = "Unspecified"):
    try:
        await can_moderate(interaction, target)
        await notify_user(target, "kicked", interaction.user, reason, interaction.guild.name)
        await target.kick(reason=reason)
        await send_moderation_alert(interaction.client, alert_embed(
            "👢 Member Kicked",
            f"{interaction.user.mention} kicked {target.mention} via `/kick`.",
            color=ALERT_COLOR_REMOVE,
            fields=[("Reason", reason, False)],
        ))
        await send_success(interaction, f"{target.mention} has been kicked.", fields=[("Reason", reason, False)])
    except app_commands.CheckFailure as e:
        await send_error(interaction, str(e))
    except discord.Forbidden:
        await send_error(interaction, "Missing permissions to kick.")
    except Exception as e:
        await send_error(interaction, f"Failed to kick: {e}")


_MUTE_ALLOWED_PERMS = {
    "view_channel",
    "manage_channels",
    "manage_permissions",
    "manage_webhooks",
    "create_instant_invite",
}

_MUTE_ALL_CHANNEL_PERMS = [
    "add_reactions", "attach_files", "connect", "create_instant_invite", "deafen_members",
    "embed_links", "external_emojis", "manage_channels", "manage_messages", "manage_permissions",
    "manage_webhooks", "mention_everyone", "move_members", "mute_members", "priority_speaker",
    "read_message_history", "send_messages", "send_tts_messages", "speak", "stream",
    "use_external_emojis", "view_channel", "create_public_threads", "create_private_threads",
    "send_messages_in_threads", "use_external_stickers", "send_voice_messages", "create_polls",
]


async def _mute_impl(interaction: discord.Interaction, target: discord.Member, reason: str = "Unspecified"):
    try:
        await interaction.response.send_message(f"Muting {target.mention}...", ephemeral=True)

        guild = interaction.guild
        muted_role = discord.utils.get(guild.roles, name="Muted")

        if not muted_role:
            try:
                muted_role = await guild.create_role(name="Muted", reason="Mute role required")
            except discord.Forbidden:
                await interaction.edit_original_response(content=None, embed=error_embed("Missing permission to create the muted role."))
                return

        # Overwrite permissions on every channel to accommodate the muted role
        for channel in guild.channels:
            overwrite = channel.overwrites_for(muted_role)
            for perm_name in _MUTE_ALL_CHANNEL_PERMS:
                if perm_name not in _MUTE_ALLOWED_PERMS:
                    setattr(overwrite, perm_name, False)
                else:
                    setattr(overwrite, perm_name, None)  # Keep allowed perms untouched

            try:
                await channel.set_permissions(muted_role, overwrite=overwrite)
            except Exception as e:
                print(f"Failed to update permissions for channel {channel.name}: {e}")

        if muted_role in target.roles:
            await interaction.edit_original_response(content=None, embed=error_embed(f"{target.mention} is already muted."))
            return

        await target.add_roles(muted_role, reason=f"Muted by {interaction.user} - Reason: {reason}")
        await interaction.edit_original_response(
            content=None,
            embed=success_embed(f"{target.mention} has been muted.", fields=[("Reason", reason, False)]),
        )

        await send_moderation_alert(interaction.client, alert_embed(
            "🔇 Member Muted",
            f"{interaction.user.mention} muted {target.mention} via `/mute`.",
            color=ALERT_COLOR_REMOVE,
            fields=[("Reason", reason, False)],
        ))

        await notify_user(target, "muted", interaction.user, reason, guild.name)

    except Exception as e:
        await edit_or_send_error(interaction, f"Failed to mute: {e}")


async def _unmute_impl(interaction: discord.Interaction, target: discord.Member, reason: str = "No reason provided"):
    try:
        await can_moderate(interaction, target)
    except app_commands.CheckFailure as e:
        await send_error(interaction, str(e))
        return

    muted_role = discord.utils.get(interaction.guild.roles, name="Muted")
    if not muted_role:
        await send_error(interaction, "Muted role missing.")
        return

    if muted_role not in target.roles:
        await send_error(interaction, f"{target.mention} is not muted.")
        return

    try:
        await target.remove_roles(muted_role, reason=f"Unmuted by {interaction.user}")
        await send_moderation_alert(interaction.client, alert_embed(
            "🔊 Member Unmuted",
            f"{interaction.user.mention} unmuted {target.mention} via `/unmute`.",
            color=ALERT_COLOR_ADD,
            fields=[("Reason", reason, False)],
        ))
        await send_success(interaction, f"{target.mention} has been unmuted.")
        await notify_user(target, "unmuted", interaction.user, reason, interaction.guild.name)
    except discord.Forbidden:
        await send_error(interaction, "Missing permissions to remove roles.")


# =========================================================================
# /temprole -- generalizes access.py's /tempaccess (which only ever grants
# the single, fixed Bot Access role) to any role at all.
#
# The pending-removal timer used to live only in process memory -- a
# restart mid-duration meant that particular auto-removal simply never
# fired again, silently leaving the role on the member until someone
# noticed and removed it by hand. Persisted to BotState.json's
# "temp_roles" list now, same "fetch -> mutate -> commit" shape as
# moderation.py's temp_bans -- see that section's comments for the full
# reasoning. Keyed by a short random id (see api.github.new_state_id)
# rather than (member_id, role_id) alone, since nothing stops the same
# member+role pair from theoretically getting a fresh /temprole grant
# after an earlier one already resolved.
# =========================================================================

# (member_id, role_id) pairs currently holding a role granted via
# /temprole, so a second grant for the same member+role can be rejected
# instead of stacking timers -- same convention as access.py's
# _active_temp_access, just keyed on the role too since this isn't scoped
# to one fixed role. Fast in-memory membership check; BotState.json's
# "temp_roles" list is the durable source of truth.
_active_temp_roles: set = set()

# Running removal tasks, keyed by the BotState entry's id -- mirrors
# moderation.py's _temp_ban_tasks / access.py's _temp_access_tasks, so a
# reconciled (post-restart) task can be tracked the same way as a
# freshly-granted one.
_temp_role_tasks: dict = {}


async def _clear_temp_role_state(entry_id: str):
    """Removes a resolved (fired, or the role/member disappeared) temp role
    entry from BotState.json. Best-effort -- logged rather than raised,
    since the Discord-side role removal (or the discovery that there was
    nothing left to remove) has already happened by the time this runs."""
    def _mutate(state):
        state["temp_roles"] = [e for e in state.get("temp_roles", []) if e.get("id") != entry_id]
        return state
    try:
        await update_botstate(_mutate, f"Temp role resolved: {entry_id}")
    except GitHubAPIError as e:
        print(f"Failed to clear resolved temp role {entry_id} from BotState.json: {e}")


async def _run_temp_role_removal(bot: commands.Bot, entry: dict):
    """Sleeps until `entry`'s expires_at (or fires almost immediately if
    that's already in the past -- e.g. the bot was down past it), then
    removes the role and clears the BotState entry. Shared by both a fresh
    /temprole grant and startup reconciliation, so there's exactly one code
    path for "what happens when a temp role's timer goes off.\""""
    entry_id = entry["id"]
    key = (int(entry["discord_id"]), int(entry["role_id"]))
    try:
        await asyncio.sleep(seconds_until(parse_iso(entry.get("expires_at"))))

        guild = bot.get_guild(int(entry["guild_id"]))
        role = guild.get_role(int(entry["role_id"])) if guild else None

        # Fetch a fresh member since roles aren't always reflected on the
        # cached object right away, and the member may have left and
        # rejoined (or the role may have been removed manually) in the
        # meantime.
        fresh_member = guild.get_member(int(entry["discord_id"])) if guild else None

        if guild is None or role is None:
            print(f"Could not find guild/role to auto-remove expired temp role for {entry['discord_id']} (temp role {entry_id}).")
        elif fresh_member and role in fresh_member.roles:
            try:
                await fresh_member.remove_roles(role, reason="Temporary role expired")
            except discord.Forbidden:
                print(f"Missing permissions to remove expired temp role {role} from {fresh_member}")
            else:
                await send_moderation_alert(bot, alert_embed(
                    "⌛ Temp Role Expired",
                    f"{fresh_member.mention}'s temporary {role.mention} role expired and was auto-removed.",
                    color=ALERT_COLOR_REMOVE,
                ))
                try:
                    dm_embed = discord.Embed(
                        title="Role Removed!",
                        description=f"Your temporary role **{role.name}** in **{guild.name}** has expired and been removed.",
                        color=discord.Color.red(),
                        timestamp=datetime.now(timezone.utc),
                    )
                    if dms_enabled():
                        await fresh_member.send(embed=dm_embed)
                except discord.Forbidden:
                    pass
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"Error removing temporary role for {entry.get('discord_id')} (temp role {entry_id}): {e}")
    finally:
        _active_temp_roles.discard(key)
        _temp_role_tasks.pop(entry_id, None)
        await _clear_temp_role_state(entry_id)


def _schedule_temp_role(bot: commands.Bot, entry: dict):
    """(Re)schedules the auto-removal task for `entry`. Cancels whatever
    task was already tracked for this entry id first (shouldn't normally
    happen -- each entry only gets scheduled once, at grant time or at
    startup -- but keeps this safe to call more than once for the same
    entry)."""
    entry_id = entry["id"]
    key = (int(entry["discord_id"]), int(entry["role_id"]))
    existing = _temp_role_tasks.get(entry_id)
    if existing and not existing.done():
        existing.cancel()
    _active_temp_roles.add(key)
    _temp_role_tasks[entry_id] = bot.loop.create_task(_run_temp_role_removal(bot, entry))


async def reconcile_temp_roles(bot: commands.Bot, state: Optional[Dict[str, Any]] = None):
    """Called once from on_ready: re-schedules every temp role's
    auto-removal timer using the durable expires_at recorded in
    BotState.json, so a restart before the original timer fired no longer
    leaves the role on the member indefinitely. Entries whose expires_at
    has already passed fire (almost) immediately via seconds_until()'s
    clamp-to-zero, rather than staying granted until someone notices.

    `state` lets a caller that's already fetched BotState.json (e.g.
    start.py's on_ready, reconciling several categories back to back) hand
    it over directly instead of this making its own redundant fetch of the
    exact same file. Falls back to fetching it itself when called on its
    own with nothing passed in."""
    if state is None:
        try:
            state, _sha = await fetch_botstate_with_sha()
        except GitHubAPIError as e:
            print(f"Failed to fetch BotState.json for temp role reconciliation: {e}")
            return

    entries = state.get("temp_roles", [])
    for entry in entries:
        _schedule_temp_role(bot, entry)

    if entries:
        print(f"Reconciled {len(entries)} temp role(s) from BotState.json.")


async def _temprole_impl(interaction: discord.Interaction, target: discord.Member, role: discord.Role, duration: int, reason: str = "No reason provided"):
    await interaction.response.defer(ephemeral=True)

    if duration <= 0:
        return await send_error(interaction, "Duration must be a positive integer.")

    if role in target.roles:
        return await send_error(interaction, f"{target.mention} already has the {role.mention} role.")

    key = (target.id, role.id)
    if key in _active_temp_roles:
        return await send_error(interaction, f"{target.mention} already has a temporary {role.mention} timer running.")

    try:
        await target.add_roles(role, reason=f"Temporary role ({duration}m) by {interaction.user} -- Reason: {reason}")
    except discord.Forbidden:
        return await send_error(interaction, f"Missing permissions to assign {role.mention} -- check that my top role sits above it.")
    except discord.HTTPException as e:
        return await send_error(interaction, f"Failed to assign role: {e}")

    expiry = datetime.now(timezone.utc) + timedelta(minutes=duration)
    timestamp = int(expiry.timestamp())
    minute_label = "minute" if duration == 1 else "minutes"

    entry = {
        "id": new_state_id("tr"),
        "discord_id": str(target.id),
        "guild_id": str(interaction.guild.id),
        "role_id": str(role.id),
        "reason": reason,
        "granted_at": format_iso(datetime.now(timezone.utc)),
        "expires_at": format_iso(expiry),
        "granted_by_id": str(interaction.user.id),
        "granted_by_tag": str(interaction.user),
    }

    try:
        def _mutate(state, entry=entry):
            state.setdefault("temp_roles", []).append(entry)
            return state
        await update_botstate(_mutate, f"Temp role recorded: {target} <- {role} ({target.id})")
    except GitHubAPIError as e:
        # The role grant itself already succeeded (add_roles() above) --
        # this only means the auto-removal timer won't survive a restart
        # until BotState.json can be reached again. Still schedule the
        # in-memory task below so this process's own timer works
        # regardless, and flag it to staff since a "temp" role silently
        # becoming permanent on the next restart is exactly the failure
        # mode this persistence exists to prevent.
        print(f"Failed to persist temp role for {target} to BotState.json: {e}")
        await send_moderation_alert(interaction.client, alert_embed(
            "⚠️ Temp Role Not Persisted",
            f"{target.mention}'s temporary {role.mention} role couldn't be saved to BotState.json ({e}). "
            "It will still auto-remove on schedule *this session*, but would stay on the member "
            "permanently if the bot restarts before then.",
            color=ALERT_COLOR_CAUTION,
        ))

    await send_success(
        interaction,
        f"Gave {target.mention} the {role.mention} role for {duration} {minute_label}.",
        fields=[
            ("Reason", reason, False),
            ("Expires", f"<t:{timestamp}:F>\n<t:{timestamp}:T> (<t:{timestamp}:R>)", False),
        ],
    )

    await send_moderation_alert(interaction.client, alert_embed(
        "⏳ Temp Role Granted",
        f"{interaction.user.mention} granted {target.mention} the {role.mention} role for {duration} {minute_label} via `/temprole`.",
        color=ALERT_COLOR_TEMP,
        fields=[("Reason", reason, False)],
    ))

    try:
        dm_embed = discord.Embed(
            title="Role Added!",
            description=f"You have been **granted** the role **{role.name}** in **{interaction.guild.name}** for {duration} {minute_label}.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        dm_embed.add_field(name="Expires", value=f"<t:{timestamp}:F>", inline=False)
        dm_embed.set_thumbnail(url=role.icon.url if role.icon else interaction.guild.icon.url if interaction.guild.icon else None)
        dm_embed.set_footer(text="Temporary Role")
        if dms_enabled():
            await target.send(embed=dm_embed)
    except discord.Forbidden:
        pass

    _schedule_temp_role(interaction.client, entry)


# =========================================================================
# /slowmode, /togglelock, and /togglelockdown -- grouped here since they
# all work by editing @everyone channel-permission overwrites (or
# slowmode_delay).
#
# /slowmode, /togglelock, and /togglelockdown all behave correctly across
# every non-thread, non-category channel type: text, voice, stage, and
# forum. Threads and categories are excluded on purpose -- threads already
# have Discord's own native lock/slowmode controls that don't go through
# @everyone overwrites, and categories have no "post a message" concept of
# their own to lock or throttle. Forum channels are fully disabled by also
# denying `send_messages` -- which Discord's own permission UI labels
# "Create Posts" for a forum channel, since there's no separate bit for it
# -- on top of the thread-reply perms.
#
# /togglelockdown was moved here from access.py, since access.py is scoped
# to Bot Access role commands rather than general moderation.
#
# The /togglelock here supersedes an older, text-channel-only /togglelock
# that used to live in access.py.
#
# Both commands also accept an optional `duration` (minutes), which
# schedules an automatic unlock/lockdown-lift after that time, and an
# optional `message`, which gets posted as a public announcement embed
# (title depends on the command, and on whether it just locked or unlocked).
# =========================================================================

# Forum/media channels resolve to ForumChannel, and announcement channels
# resolve to TextChannel, in discord.py -- so this tuple already covers
# every "postable" channel type without needing to list those separately.
_SLOWMODE_LOCK_CHANNEL_TYPES = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel)

# Type alias for the channel option on both commands -- restricts Discord's
# channel picker to exactly the types above (categories/threads won't show
# up as options at all, rather than being selectable and then rejected).
_LockableChannel = Union[discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel]

# Discord's own hard ceiling on slowmode_delay (6 hours), enforced here too
# so a bad value gets caught before it's sent off to the API.
_MAX_SLOWMODE_SECONDS = 21600


def _unsupported_channel_message(channel) -> str:
    """Friendlier-than-generic explanation for why a given channel can't be
    targeted, used by both /slowmode and /lock."""
    if isinstance(channel, discord.Thread):
        return f"{channel.mention} is a thread -- use Discord's built-in thread Slowmode/Lock controls instead."
    if isinstance(channel, discord.CategoryChannel):
        return f"{channel.mention} is a category -- lock or set slowmode on its channels individually instead."
    mention = getattr(channel, "mention", "That channel")
    return f"{mention} doesn't support this command."


def _format_slowmode(seconds: int) -> str:
    """Renders a slowmode_delay value the way Discord's own UI would --
    e.g. 90 -> '1 minute, 30 seconds', 0 -> 'Off'."""
    if seconds <= 0:
        return "Off"

    remaining = seconds
    parts = []
    for name, unit_seconds in (("hour", 3600), ("minute", 60), ("second", 1)):
        value, remaining = divmod(remaining, unit_seconds)
        if value:
            parts.append(f"{value} {name}{'s' if value != 1 else ''}")
    return ", ".join(parts)


async def _slowmode_impl(interaction: discord.Interaction, seconds: int, channel: Optional[_LockableChannel] = None):
    target = channel or interaction.channel

    if not isinstance(target, _SLOWMODE_LOCK_CHANNEL_TYPES):
        await send_error(interaction, _unsupported_channel_message(target))
        return

    if not 0 <= seconds <= _MAX_SLOWMODE_SECONDS:
        await send_error(interaction, f"Slowmode must be between 0 and {_MAX_SLOWMODE_SECONDS} seconds (6 hours).")
        return

    try:
        await target.edit(slowmode_delay=seconds, reason=f"Slowmode set by {interaction.user}")
        await send_moderation_alert(interaction.client, alert_embed(
            "🐌 Slowmode Changed",
            f"{interaction.user.mention} set slowmode for {target.mention} to "
            f"**{_format_slowmode(seconds)}** via `/slowmode`.",
            color=ALERT_COLOR_EDIT,
        ))
        await send_success(
            interaction,
            f"Slowmode for {target.mention} set to **{_format_slowmode(seconds)}**.",
        )
    except discord.Forbidden:
        await send_error(interaction, f"Missing permissions to edit {target.mention}.")
    except discord.HTTPException as e:
        await send_error(interaction, f"Failed to set slowmode: {e}")


def _lock_perms(channel) -> tuple:
    """Which @everyone overwrite permissions get denied to lock a given
    channel type. Voice/stage lock on `connect` (same as /togglelockdown);
    forums lock on `send_messages` (the "Create Posts" toggle in a forum
    channel's permission UI) plus the thread-reply perms, so a locked forum
    can neither start new posts nor reply in existing ones."""
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return ("connect", "send_messages")
    if isinstance(channel, discord.ForumChannel):
        return ("send_messages", "create_public_threads", "create_private_threads", "send_messages_in_threads")
    return ("send_messages",)


_LOCK_ANNOUNCEMENT_TITLES = {
    ("togglelock", "locked"): "Channel Locked",
    ("togglelock", "unlocked"): "Channel Unlocked",
    ("togglelockdown", "locked"): "Server Locked Down",
    ("togglelockdown", "unlocked"): "Server Unlocked",
}


def _lock_announcement_embed(title: str, message: str, *, duration: Optional[int] = None) -> discord.Embed:
    """Public-facing embed for a lock/unlock/lockdown announcement. `duration`
    only makes sense to show alongside a "locked" title -- callers pass None
    for unlock announcements."""
    color = discord.Color.green() if "Unlock" in title else discord.Color.orange()
    embed = discord.Embed(title=title, description=message, color=color, timestamp=datetime.now(timezone.utc))

    if duration:
        minute_label = "minute" if duration == 1 else "minutes"
        unlock_time = datetime.now(timezone.utc) + timedelta(minutes=duration)
        timestamp = int(unlock_time.timestamp())
        embed.add_field(name="Duration", value=f"{duration} {minute_label}", inline=True)
        embed.add_field(name="Unlocks", value=f"<t:{timestamp}:F>\n<t:{timestamp}:T> (<t:{timestamp}:R>)", inline=True)

    return embed


async def _send_lock_announcement(channel, *, title: str, message: str, duration: Optional[int] = None):
    """Posts a public (non-ephemeral) announcement embed to `channel` --
    used by /togglelock and /togglelockdown, both on manual toggle and on
    automatic duration expiry. Best-effort: the actual permission change has
    already happened by the time this is called, so failures here are
    logged rather than surfaced as a command error.

    ForumChannel has no top-level message to send an embed into -- unlike
    every other channel type this module deals with, it has no `send()` at
    all. The closest equivalent there is a new post (thread), so the
    announcement becomes that post's starter message instead. That post is
    then locked so it stays a read-only announcement: per Discord's own
    thread model, `locked` alone only controls who can *unarchive* a
    thread -- what actually stops members from posting in it is `archived`,
    so both need to be set together, not just `locked`."""
    if channel is None:
        return
    embed = _lock_announcement_embed(title, message, duration=duration)
    try:
        if isinstance(channel, discord.ForumChannel):
            thread_with_message = await channel.create_thread(name=title, embed=embed, reason="Lock/lockdown announcement")
            try:
                await thread_with_message.thread.edit(archived=True, locked=True, reason="Lock/lockdown announcement -- read-only")
            except Exception as e:
                print(f"Failed to lock lock-announcement post in {getattr(channel, 'name', channel)}: {e}")
        else:
            await channel.send(embed=embed)
    except discord.Forbidden:
        print(f"Missing permissions to send lock announcement in {getattr(channel, 'name', channel)}")
    except Exception as e:
        print(f"Failed to send lock announcement in {getattr(channel, 'name', channel)}: {e}")


# Pending auto-unlock tasks scheduled by a `duration` on /togglelock, keyed
# by channel id. Popped and cancelled the moment that channel's lock state
# changes again for any reason, so a stale timer never fires after someone
# has already manually toggled it back. Persisted to BotState.json's
# "channel_locks" list (same shape/reasoning as "temp_bans" above) so a
# restart mid-timer reschedules the auto-unlock instead of leaving the
# channel locked forever until someone happens to notice.
_lock_duration_tasks: dict = {}


async def _clear_channel_lock_state(channel_id: int):
    """Removes the persisted BotState.json entry (if any) for `channel_id`.
    Best-effort -- logged rather than raised, since the Discord-side
    permission change (a manual re-toggle, or the timer itself firing) has
    already happened by the time this runs. Safe to call even when the
    channel was never in "channel_locks" to begin with (e.g. it was locked
    with no duration) -- filtering an already-empty match is a no-op."""
    def _mutate(state):
        state["channel_locks"] = [e for e in state.get("channel_locks", []) if str(e.get("channel_id")) != str(channel_id)]
        return state
    try:
        await update_botstate(_mutate, f"Channel lock resolved: {channel_id}")
    except GitHubAPIError as e:
        print(f"Failed to clear resolved channel lock for channel {channel_id} from BotState.json: {e}")


async def _run_channel_lock_auto_unlock(bot: commands.Bot, channel_id: int, unlock_at: datetime):
    """Sleeps until `unlock_at` (or fires almost immediately if that's
    already passed), then restores the channel's @everyone overwrite and
    clears the persisted BotState entry. Shared by both a fresh /togglelock
    duration grant and startup reconciliation."""
    try:
        await asyncio.sleep(seconds_until(unlock_at))

        guild = bot.get_guild(config.GUILD_ID)
        channel = guild.get_channel(channel_id) if guild else None
        if channel is None:
            print(f"Could not find channel {channel_id} to auto-unlock.")
            return

        everyone_role = guild.default_role
        overwrite = channel.overwrites_for(everyone_role)
        perm_names = _lock_perms(channel)
        for perm in perm_names:
            setattr(overwrite, perm, None)
        try:
            await channel.set_permissions(everyone_role, overwrite=overwrite, reason="Lock duration expired")
            await _send_lock_announcement(
                channel,
                title=_LOCK_ANNOUNCEMENT_TITLES[("togglelock", "unlocked")],
                message="Lock duration expired -- channel automatically unlocked.",
            )
        except Exception as e:
            print(f"Failed to auto-unlock {getattr(channel, 'name', channel)}: {e}")
    except asyncio.CancelledError:
        raise
    finally:
        _lock_duration_tasks.pop(channel_id, None)
        await _clear_channel_lock_state(channel_id)


def _schedule_channel_lock(bot: commands.Bot, channel_id: int, entry: dict):
    """(Re)schedules the auto-unlock task for `entry` (a BotState.json
    "channel_locks" entry). Cancels whatever task was already tracked for
    this channel first -- shouldn't normally happen (each entry only gets
    scheduled once, at lock time or at startup), but keeps this safe to
    call more than once for the same channel."""
    existing = _lock_duration_tasks.pop(channel_id, None)
    if existing and not existing.done():
        existing.cancel()
    unlock_at = parse_iso(entry.get("unlock_at"))
    _lock_duration_tasks[channel_id] = bot.loop.create_task(_run_channel_lock_auto_unlock(bot, channel_id, unlock_at))


async def reconcile_channel_locks(bot: commands.Bot, state: Optional[Dict[str, Any]] = None):
    """Called once from on_ready: re-schedules every per-channel lock's
    auto-unlock timer using the durable unlock_at recorded in
    BotState.json, so a restart before the original timer fired no longer
    leaves the channel locked forever until someone happens to notice and
    manually toggles it.

    `state` lets a caller that's already fetched BotState.json hand it
    over directly instead of this making its own redundant fetch -- see
    reconcile_temp_bans() above for the full reasoning."""
    if state is None:
        try:
            state, _sha = await fetch_botstate_with_sha()
        except GitHubAPIError as e:
            print(f"Failed to fetch BotState.json for channel lock reconciliation: {e}")
            return

    entries = state.get("channel_locks", [])
    reconciled = 0
    for entry in entries:
        try:
            channel_id = int(entry["channel_id"])
        except (KeyError, TypeError, ValueError):
            continue
        _schedule_channel_lock(bot, channel_id, entry)
        reconciled += 1

    if reconciled:
        print(f"Reconciled {reconciled} channel lock(s) from BotState.json.")


async def _togglelock_impl(
    interaction: discord.Interaction,
    channel: Optional[_LockableChannel] = None,
    duration: Optional[int] = None,
    message: Optional[str] = None,
):
    target = channel or interaction.channel

    if not isinstance(target, _SLOWMODE_LOCK_CHANNEL_TYPES):
        await send_error(interaction, _unsupported_channel_message(target))
        return

    everyone_role = interaction.guild.default_role
    overwrite = target.overwrites_for(everyone_role)
    perm_names = _lock_perms(target)

    # Locked means *any* of the relevant perms are explicitly denied -- not
    # necessarily all of them, since a channel could've been locked before a
    # permission was added to _lock_perms, or had one perm manually restored
    # by staff in between.
    is_locked = any(getattr(overwrite, perm) is False for perm in perm_names)

    # Whatever timer was previously scheduled for this channel no longer
    # applies once its lock state is about to change again -- cancel the
    # in-memory task and clear any persisted BotState.json entry so a stale
    # timer can't resurrect itself (or clash with a fresh one below) on the
    # next restart.
    pending_task = _lock_duration_tasks.pop(target.id, None)
    if pending_task:
        pending_task.cancel()
    await _clear_channel_lock_state(target.id)

    if is_locked:
        # None clears the explicit deny rather than granting an explicit
        # allow, so @everyone falls back to whatever it would otherwise have
        # (role perms, category sync, etc.) -- same convention the old
        # access.py /togglelock used.
        for perm in perm_names:
            setattr(overwrite, perm, None)
        action = "unlocked"
    else:
        for perm in perm_names:
            setattr(overwrite, perm, False)
        action = "locked"

    verb = "unlock" if is_locked else "lock"
    try:
        await target.set_permissions(everyone_role, overwrite=overwrite, reason=f"Channel {action} by {interaction.user}")
    except discord.Forbidden:
        await send_error(interaction, f"Missing permissions to {verb} {target.mention}.")
        return
    except discord.HTTPException as e:
        await send_error(interaction, f"Failed to {verb} {target.mention}: {e}")
        return

    # Duration only means anything when this toggle just locked the channel;
    # flag it rather than silently ignoring it if it was passed on an unlock.
    ignored_duration = duration is not None and action == "unlocked"
    confirmation_fields = [("Note", "Duration is ignored when unlocking.", False)] if ignored_duration else None
    await send_success(interaction, f"{target.mention} has been {action}.", fields=confirmation_fields)

    alert_fields = []
    if duration and action == "locked":
        minute_label = "minute" if duration == 1 else "minutes"
        alert_fields.append(("Duration", f"{duration} {minute_label}", False))
    if message:
        alert_fields.append(("Announcement", message, False))
    await send_moderation_alert(interaction.client, alert_embed(
        "🔒 Channel Locked" if action == "locked" else "🔓 Channel Unlocked",
        f"{interaction.user.mention} {action} {target.mention} via `/togglelock`.",
        color=ALERT_COLOR_CAUTION if action == "locked" else ALERT_COLOR_ADD,
        fields=alert_fields or None,
    ))

    if message:
        await _send_lock_announcement(
            target,
            title=_LOCK_ANNOUNCEMENT_TITLES[("togglelock", action)],
            message=message,
            duration=duration if action == "locked" else None,
        )

    if action == "locked" and duration:
        locked_at = datetime.now(timezone.utc)
        unlock_at = locked_at + timedelta(minutes=duration)
        entry = {
            "id": new_state_id("cl"),
            "channel_id": str(target.id),
            "locked_at": format_iso(locked_at),
            "unlock_at": format_iso(unlock_at),
            "locked_by_id": str(interaction.user.id),
            "locked_by_tag": str(interaction.user),
        }
        try:
            def _mutate(state, entry=entry):
                state.setdefault("channel_locks", []).append(entry)
                return state
            await update_botstate(_mutate, f"Channel lock recorded: {getattr(target, 'name', target.id)} ({target.id})")
        except GitHubAPIError as e:
            # The lock itself already succeeded (set_permissions() above) --
            # this only means the auto-unlock timer won't survive a restart
            # until BotState.json can be reached again. Still schedule the
            # in-memory task below so this process's own timer works
            # regardless, and flag it to staff since a timed lock silently
            # becoming permanent on the next restart is exactly the failure
            # mode this persistence exists to prevent.
            print(f"Failed to persist channel lock for {getattr(target, 'name', target.id)} to BotState.json: {e}")
            await send_moderation_alert(interaction.client, alert_embed(
                "⚠️ Channel Lock Not Persisted",
                f"{target.mention}'s timed lock couldn't be saved to BotState.json ({e}). "
                "It will still auto-unlock on schedule *this session*, but would stay locked "
                "permanently if the bot restarts before then.",
                color=ALERT_COLOR_CAUTION,
            ))

        _schedule_channel_lock(interaction.client, target.id, entry)


# Forum channels are now included here too (see the module-level note above
# on Create Posts), so lockdown reaches every "postable" channel type --
# same coverage as _SLOWMODE_LOCK_CHANNEL_TYPES / /togglelock.
LOCKDOWN_CHANNEL_TYPES = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel)

# Snapshot of each channel's @everyone overwrite permissions from right
# before /togglelockdown was last enabled. Non-empty while lockdown is
# active; used to restore channels to their exact prior state (instead of
# blanket unlocking) so channels that were already locked beforehand stay
# locked.
_lockdown_snapshots: dict = {}

# Pending auto-lift task for /togglelockdown's `duration`, or None if no
# lockdown timer is currently scheduled. Cancelled the moment lockdown state
# changes again for any reason, same convention as _lock_duration_tasks.
_lockdown_duration_task: Optional[asyncio.Task] = None


def _lockdown_perms(channel: discord.abc.GuildChannel) -> tuple:
    """Which @everyone overwrite permissions get locked/restored for a given
    channel type. Identical to /togglelock's per-channel-type rules, so this
    just delegates to _lock_perms rather than duplicating them."""
    return _lock_perms(channel)


async def _lockdown_apply(channels, everyone_role) -> int:
    """Locks every channel in `channels`, snapshotting each one's prior
    @everyone overwrite state first so it can be restored exactly later."""
    count = 0
    for channel in channels:
        overwrite = channel.overwrites_for(everyone_role)
        perm_names = _lockdown_perms(channel)

        _lockdown_snapshots[channel.id] = {perm: getattr(overwrite, perm) for perm in perm_names}

        changed = False
        for perm_name in perm_names:
            if getattr(overwrite, perm_name) is not False:
                setattr(overwrite, perm_name, False)
                changed = True

        if changed:
            try:
                await channel.set_permissions(everyone_role, overwrite=overwrite, reason="Server lockdown enabled")
                count += 1
            except discord.Forbidden:
                print(f"Missing permissions to lock {channel.name}")
            except Exception as e:
                print(f"Failed to lock {channel.name}: {e}")
    return count


async def _lockdown_restore(channels, everyone_role) -> int:
    """Restores every channel in `channels` to its pre-lockdown @everyone
    overwrite state (from _lockdown_snapshots), then clears the snapshots.
    Used both when lockdown is manually toggled off and when its `duration`
    expires on its own."""
    count = 0
    for channel in channels:
        snapshot = _lockdown_snapshots.get(channel.id)
        if snapshot is None:
            continue  # channel didn't exist yet / wasn't touched during lockdown

        overwrite = channel.overwrites_for(everyone_role)
        changed = False
        for perm_name, original_value in snapshot.items():
            if getattr(overwrite, perm_name) != original_value:
                setattr(overwrite, perm_name, original_value)
                changed = True

        if changed:
            try:
                await channel.set_permissions(everyone_role, overwrite=overwrite, reason="Server lockdown disabled")
                count += 1
            except discord.Forbidden:
                print(f"Missing permissions to restore {channel.name}")
            except Exception as e:
                print(f"Failed to restore {channel.name}: {e}")

    _lockdown_snapshots.clear()
    return count


async def _persist_lockdown_state(
    active: bool,
    *,
    started_at: Optional[datetime] = None,
    unlock_at: Optional[datetime] = None,
    started_by_id=None,
    started_by_tag: Optional[str] = None,
    announce_channel_id=None,
    message: str,
):
    """Writes (or clears) BotState.json's "lockdown" key. `active=True`
    snapshots the *current* in-memory _lockdown_snapshots dict (so this must
    be called right after _lockdown_apply() populates it); `active=False`
    just clears it back to null."""
    def _mutate(state):
        if active:
            state["lockdown"] = {
                "active": True,
                "started_at": format_iso(started_at),
                "unlock_at": format_iso(unlock_at) if unlock_at else None,
                "started_by_id": str(started_by_id),
                "started_by_tag": started_by_tag,
                "announce_channel_id": str(announce_channel_id) if announce_channel_id else None,
                "channel_snapshots": {str(cid): snap for cid, snap in _lockdown_snapshots.items()},
            }
        else:
            state["lockdown"] = None
        return state

    try:
        await update_botstate(_mutate, message)
    except GitHubAPIError as e:
        print(f"Failed to persist lockdown state to BotState.json: {e}")


async def _run_lockdown_auto_unlock(bot: commands.Bot, guild_id: int, unlock_at: datetime, announce_channel_id: Optional[int]):
    """Sleeps until `unlock_at` (or fires almost immediately if that's
    already passed), then restores every channel and clears the persisted
    lockdown state. Shared by both a fresh /togglelockdown duration grant
    and startup reconciliation."""
    global _lockdown_duration_task
    try:
        await asyncio.sleep(seconds_until(unlock_at))

        guild = bot.get_guild(guild_id)
        if guild is None:
            print(f"Could not find guild {guild_id} to auto-lift lockdown.")
            return

        everyone_role = guild.default_role
        current_channels = [ch for ch in guild.channels if isinstance(ch, LOCKDOWN_CHANNEL_TYPES)]
        await _lockdown_restore(current_channels, everyone_role)
        await _persist_lockdown_state(False, message="Lockdown duration expired -- auto-lifted")

        if announce_channel_id:
            channel = bot.get_channel(announce_channel_id)
            await _send_lock_announcement(
                channel,
                title=_LOCK_ANNOUNCEMENT_TITLES[("togglelockdown", "unlocked")],
                message="Lockdown duration expired -- server automatically unlocked.",
            )
    except asyncio.CancelledError:
        raise
    finally:
        _lockdown_duration_task = None


def _schedule_lockdown_auto_unlock(bot: commands.Bot, guild_id: int, unlock_at: datetime, announce_channel_id: Optional[int]):
    global _lockdown_duration_task
    if _lockdown_duration_task:
        _lockdown_duration_task.cancel()
    _lockdown_duration_task = bot.loop.create_task(_run_lockdown_auto_unlock(bot, guild_id, unlock_at, announce_channel_id))


async def reconcile_lockdown(bot: commands.Bot, state: Optional[Dict[str, Any]] = None):
    """Called once from on_ready: restores _lockdown_snapshots from
    BotState.json if a lockdown was active when the bot last stopped (so
    /togglelockdown correctly recognizes it's still active and can restore
    each channel's exact prior state), and reschedules the auto-lift timer
    if one was running. Without this, a restart mid-lockdown would forget
    lockdown was even happening -- any scheduled auto-lift is lost, and
    there's no way left to restore each channel's original per-channel
    state short of reconstructing it by hand.

    `state` lets a caller that's already fetched BotState.json hand it
    over directly instead of this making its own redundant fetch -- see
    reconcile_temp_bans() above for the full reasoning."""
    if state is None:
        try:
            state, _sha = await fetch_botstate_with_sha()
        except GitHubAPIError as e:
            print(f"Failed to fetch BotState.json for lockdown reconciliation: {e}")
            return

    lockdown = state.get("lockdown")
    if not lockdown or not lockdown.get("active"):
        return

    _lockdown_snapshots.clear()
    for cid, snapshot in (lockdown.get("channel_snapshots") or {}).items():
        try:
            _lockdown_snapshots[int(cid)] = snapshot
        except (TypeError, ValueError):
            continue

    unlock_at = parse_iso(lockdown.get("unlock_at"))
    raw_announce_id = lockdown.get("announce_channel_id")
    announce_channel_id = int(raw_announce_id) if raw_announce_id else None

    if unlock_at is None:
        # Indefinite lockdown (no duration was set) -- the snapshot restore
        # above is all reconciliation needs to do; there's no timer to
        # reschedule, same as it would've had no timer before the restart.
        print("Reconciled an active indefinite lockdown from BotState.json.")
        return

    _schedule_lockdown_auto_unlock(bot, config.GUILD_ID, unlock_at, announce_channel_id)
    print(f"Reconciled an active lockdown from BotState.json (auto-lift at {lockdown.get('unlock_at')}).")


async def _togglelockdown_impl(
    interaction: discord.Interaction,
    duration: Optional[int] = None,
    message: Optional[str] = None,
):
    global _lockdown_duration_task

    # Defer immediately -- looping + editing permissions on every
    # channel in the server can easily take longer than the 3 second
    # window Discord gives an interaction before it expires.
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    everyone_role = guild.default_role

    channels = [ch for ch in guild.channels if isinstance(ch, LOCKDOWN_CHANNEL_TYPES)]
    if not channels:
        return await send_error(interaction, "No text, voice, stage, or forum channels found.")

    # Whatever timer was previously scheduled no longer applies once
    # lockdown state is about to change again.
    if _lockdown_duration_task:
        _lockdown_duration_task.cancel()
        _lockdown_duration_task = None

    # There's no single "the channel" a lockdown applies to, so the public
    # announcement (if any) -- and, on a duration-driven auto-lift, the
    # "lockdown expired" notice -- is posted wherever the command was run
    # rather than fanned out across every affected channel.
    announce_channel = interaction.channel

    if _lockdown_snapshots:
        # Lockdown is currently active -> disable it by restoring each
        # channel to whatever state it actually had *before* lockdown was
        # enabled, rather than blanket-unlocking everything. This keeps
        # channels that were already manually locked beforehand locked.
        count = await _lockdown_restore(channels, everyone_role)
        action = "unlocked"
        await _persist_lockdown_state(False, message=f"Lockdown lifted by {interaction.user}")
    else:
        # Not currently in lockdown -> enable it.
        count = await _lockdown_apply(channels, everyone_role)
        action = "locked"
        started_at = datetime.now(timezone.utc)
        unlock_at = started_at + timedelta(minutes=duration) if duration else None
        await _persist_lockdown_state(
            True,
            started_at=started_at,
            unlock_at=unlock_at,
            started_by_id=interaction.user.id,
            started_by_tag=str(interaction.user),
            announce_channel_id=announce_channel.id if announce_channel else None,
            message=f"Lockdown enabled by {interaction.user}",
        )

    # Duration only means anything when this toggle just enabled lockdown;
    # flag it rather than silently ignoring it if it was passed on a lift.
    ignored_duration = duration is not None and action == "unlocked"
    confirmation_fields = [("Note", "Duration is ignored when unlocking.", False)] if ignored_duration else None
    await send_success(interaction, f"{action.capitalize()} {count} channel(s).", fields=confirmation_fields)

    alert_fields = [("Channels Affected", str(count), True)]
    if duration and action == "locked":
        minute_label = "minute" if duration == 1 else "minutes"
        alert_fields.append(("Duration", f"{duration} {minute_label}", True))
    if message:
        alert_fields.append(("Announcement", message, False))
    await send_moderation_alert(interaction.client, alert_embed(
        "🔒 Server Locked Down" if action == "locked" else "🔓 Server Unlocked",
        f"{interaction.user.mention} {'locked down' if action == 'locked' else 'lifted the lockdown on'} "
        f"the server via `/togglelockdown`.",
        color=ALERT_COLOR_CAUTION if action == "locked" else ALERT_COLOR_ADD,
        fields=alert_fields,
    ))

    if message:
        await _send_lock_announcement(
            announce_channel,
            title=_LOCK_ANNOUNCEMENT_TITLES[("togglelockdown", action)],
            message=message,
            duration=duration if action == "locked" else None,
        )

    if action == "locked" and duration:
        _schedule_lockdown_auto_unlock(
            interaction.client, guild.id, unlock_at, announce_channel.id if announce_channel else None,
        )


# =========================================================================
# /ghostping group -- /ghostping user (manually ping-then-delete someone)
# and /ghostping toggle (flip passive ghost-ping-deletion detection on/off).
#
# Detection rides entirely on discord.py's own on_message_delete event,
# which only fires for messages discord.py already had in its internal
# message cache -- effectively "messages sent recently enough that the bot
# was already running and saw them go by." That's exactly the population a
# ghost ping cares about (a mention posted and deleted shortly after), so
# there's no separate raw-event fallback for messages the bot never saw.
#
# Note that /purge's bulk deletions won't trigger this at all -- Discord
# dispatches a single on_bulk_message_delete for those (which this module
# intentionally doesn't hook), not one on_message_delete per message. Only
# the rare fallback path for messages older than 14 days (deleted one at a
# time) could still fire this listener during a purge.
# =========================================================================

GHOSTPING_MODE_NOTHING = "nothing"
GHOSTPING_MODE_ANNOUNCED = "announced"

# Detection mode for /ghostping toggle. Kept in-memory for fast access from
# on_message_delete below, mirrored to BotState.json's "ghostping_mode" key
# on every toggle so it survives a restart instead of silently resetting to
# Nothing -- reconcile_ghostping_mode() reads it back in on_ready.
_ghostping_mode = GHOSTPING_MODE_NOTHING


async def _persist_ghostping_mode(message: str):
    """Mirrors the in-memory `_ghostping_mode` to BotState.json. Best-effort
    -- logged rather than raised, since the toggle itself has already taken
    effect in-process by the time this runs; a failure here only means the
    mode would fall back to Nothing on the next restart instead of resuming
    where it left off."""
    def _mutate(state):
        state["ghostping_mode"] = _ghostping_mode
        return state
    try:
        await update_botstate(_mutate, message)
    except GitHubAPIError as e:
        print(f"Failed to persist ghost ping detection mode to BotState.json: {e}")


async def reconcile_ghostping_mode(bot: commands.Bot, state: Optional[Dict[str, Any]] = None):
    """Called once from on_ready: restores `_ghostping_mode` from
    BotState.json, so a restart resumes whichever mode staff last set via
    /ghostping toggle instead of silently reverting to Nothing.

    `state` lets a caller that's already fetched BotState.json hand it over
    directly instead of this making its own redundant fetch -- see
    reconcile_temp_bans() above for the full reasoning."""
    global _ghostping_mode
    if state is None:
        try:
            state, _sha = await fetch_botstate_with_sha()
        except GitHubAPIError as e:
            print(f"Failed to fetch BotState.json for ghost ping mode reconciliation: {e}")
            return

    mode = state.get("ghostping_mode", GHOSTPING_MODE_NOTHING)
    if mode not in (GHOSTPING_MODE_NOTHING, GHOSTPING_MODE_ANNOUNCED):
        mode = GHOSTPING_MODE_NOTHING

    _ghostping_mode = mode
    if _ghostping_mode != GHOSTPING_MODE_NOTHING:
        print(f"Reconciled ghost ping detection mode from BotState.json: {_ghostping_mode}.")


async def _find_message_deleter(message: discord.Message) -> Optional[discord.abc.User]:
    """
    Best-effort identification of who deleted `message`, via the guild's
    audit log.

    Discord only writes a "Message Delete" audit log entry when someone
    *other* than the message's own author deletes it (e.g. a moderator
    using Manage Messages) -- an author deleting their own message leaves
    no audit trail at all. So finding no matching entry here doesn't mean
    the lookup failed; it's the expected, common case of a self-delete,
    and callers should treat a None return that way.
    """
    guild = message.guild
    if guild is None:
        return None

    me = guild.me
    if me is None or not me.guild_permissions.view_audit_log:
        return None

    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.message_delete):
            # audit_logs() yields newest-first. Entries land a moment after
            # the delete itself, so this doesn't wait around for one -- it
            # just checks what's already there -- but a generous 10 second
            # window guards against matching some earlier, unrelated
            # deletion of the same author's messages in the same channel.
            age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
            if age > 10:
                break
            target_matches = entry.target and entry.target.id == message.author.id
            channel = getattr(entry.extra, "channel", None)
            channel_matches = channel and channel.id == message.channel.id
            if target_matches and channel_matches:
                return entry.user
    except discord.Forbidden:
        return None
    except Exception as e:
        print(f"Failed to check audit log for a deleted message: {e}")
        return None

    return None


def _ghostping_announcement_embed(
    *,
    sender: discord.abc.User,
    mentioned: List[str],
    deleter: Optional[discord.abc.User],
    content: Optional[str],
) -> discord.Embed:
    """Public callout embed for a detected ghost ping -- names who sent the
    mention, what got mentioned, and who actually deleted it. Per the spec
    this was built against, explicitly calls out whether that deleter is
    the same person as the sender, since staff fixing another staffer's
    announcement wording is a very different situation than someone
    quietly deleting their own ping."""
    same_person = deleter is None or deleter.id == sender.id
    deleted_by_value = (
        deleter.mention if deleter is not None
        else f"{sender.mention} *(assumed -- no audit log entry found, so the sender likely deleted their own message)*"
    )

    fields = [
        ("Ghost Pinged By", sender.mention, True),
        ("Deleted By", deleted_by_value, True),
        ("Same Person?", "✅ Yes" if same_person else "❌ No", False),
        ("Mentioned", ", ".join(mentioned), False),
    ]
    if content:
        trimmed = content if len(content) <= 500 else content[:497] + "..."
        fields.append(("Original Message", trimmed, False))

    return build_embed(
        title="👻 Ghost Ping Detected",
        color=discord.Color.orange(),
        fields=fields,
        timestamp=datetime.now(timezone.utc),
    )


async def _ghostping_user_impl(interaction: discord.Interaction, user: discord.User):
    # channel.send() is used directly (rather than
    # interaction.response.send_message() + interaction.original_response())
    # since send() already hands back the created Message with its id
    # populated -- no extra fetch needed just to get something to delete.
    # Sending immediately followed by deleting keeps the mention live for
    # exactly as long as these two HTTP round trips take.
    try:
        msg = await interaction.channel.send(
            user.mention,
            allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False),
        )
        await msg.delete()
    except discord.Forbidden:
        return await send_error(interaction, "Missing permissions to send or delete messages in this channel.")
    except discord.HTTPException as e:
        return await send_error(interaction, f"Failed to ghost ping: {e}")

    await send_moderation_alert(interaction.client, alert_embed(
        "👻 Ghost Ping Sent",
        f"{interaction.user.mention} ghost pinged {user.mention} in {interaction.channel.mention} via `/ghostping user`.",
        color=ALERT_COLOR_CAUTION,
    ))
    await send_success(interaction, f"Ghost pinged {user.mention}.")


async def _ghostping_toggle_impl(interaction: discord.Interaction):
    """Flips between GHOSTPING_MODE_NOTHING and GHOSTPING_MODE_ANNOUNCED,
    persisting the new mode to BotState.json so it survives a restart --
    see _ghostping_mode above."""
    global _ghostping_mode
    _ghostping_mode = GHOSTPING_MODE_ANNOUNCED if _ghostping_mode == GHOSTPING_MODE_NOTHING else GHOSTPING_MODE_NOTHING
    await _persist_ghostping_mode(f"Ghost ping detection mode set to {_ghostping_mode} by {interaction.user} ({interaction.user.id})")

    if _ghostping_mode == GHOSTPING_MODE_ANNOUNCED:
        await send_moderation_alert(interaction.client, alert_embed(
            "👻 Ghost Ping Detection Enabled",
            f"{interaction.user.mention} set ghost ping detection to **Announced** via `/ghostping toggle`.",
            color=ALERT_COLOR_ADD,
        ))
        await send_success(
            interaction,
            "Ghost ping detection is now **Announced**. Deleting a message that mentions a user or role will "
            "post a public callout in that channel naming the sender, what was mentioned, and whether the "
            "sender also deleted it themselves.",
        )
    else:
        await send_moderation_alert(interaction.client, alert_embed(
            "👻 Ghost Ping Detection Disabled",
            f"{interaction.user.mention} set ghost ping detection to **Nothing** via `/ghostping toggle`.",
            color=ALERT_COLOR_CAUTION,
        ))
        await send_success(
            interaction,
            "Ghost ping detection is now **Nothing**. Deleted mentions will be ignored -- same as normal.",
        )


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ban", description="Bans a user from the server, delete their recent messages?, specify a temporary ban duration?")
    @app_commands.guilds(GUILD)
    @app_commands.describe(target="User to ban", reason="Ban reason", duration="Ban duration in minutes", preserve_messages="Keep the user's messages?")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def ban(self, interaction: discord.Interaction, target: discord.User, reason: str = "None", duration: int = None, preserve_messages: bool = True):
        await _ban_impl(interaction, target, reason, duration, preserve_messages)

    @app_commands.command(name="checkban", description="Returns if the user is banned from the server.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="User to check the ban status of")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def checkban(self, interaction: discord.Interaction, user: discord.User):
        try:
            await interaction.response.defer(ephemeral=True)

            async for ban_entry in interaction.guild.bans(limit=None):
                if ban_entry.user.id == user.id:
                    reason = ban_entry.reason or "No reason provided"
                    embed = error_embed(
                        title="User is Banned",
                        fields=[("User", f"{user} (`{user.id}`)", False), ("Reason", reason, False)],
                    )
                    return await interaction.followup.send(embed=embed, ephemeral=True)

            await send_success(interaction, f"{user.mention} is not currently banned from this server.")

        except discord.Forbidden:
            await send_error(interaction, "I don't have permission to view bans.")
        except Exception as e:
            await send_error(interaction, f"Error while checking ban: `{e}`")

    @app_commands.command(name="unban", description="Unbans a user from the server.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="The user to unban")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def unban(self, interaction: discord.Interaction, user: discord.User):
        # Deferred up front, same as /checkban's identical bans() call --
        # this iterates the guild's *entire* ban list with no limit, which
        # can easily blow past Discord's 3 second initial-response window
        # on a server with a large ban list. Without this, the very first
        # response (send_error/send_success below) can land after the
        # interaction token has already gone stale, surfacing to the user
        # as a silent "Unknown interaction" failure with no error shown.
        await interaction.response.defer(ephemeral=True)
        try:
            bans = [ban async for ban in interaction.guild.bans()]
            banned_entry = discord.utils.find(lambda b: b.user.id == user.id, bans)

            if not banned_entry:
                await send_error(interaction, "User is not banned.")
                return

            await interaction.guild.unban(banned_entry.user, reason=f"Unbanned by {interaction.user}")

            # If this was a temp ban, cancel its still-pending auto-unban
            # timer and clear the BotState entry -- otherwise it's harmless
            # (the scheduled unban() would just hit a NotFound and no-op)
            # but leaves a confusing stray entry sitting in BotState.json
            # until its original timer eventually fires.
            await _cancel_temp_ban_for(user.id, interaction.guild.id)

            await send_moderation_alert(interaction.client, alert_embed(
                "🔓 Member Unbanned",
                f"{interaction.user.mention} unbanned {user.mention} via `/unban`.",
                color=ALERT_COLOR_ADD,
            ))
            await send_success(interaction, f"Successfully unbanned {user.mention}.")

        except discord.Forbidden:
            await send_error(interaction, "Missing permissions to unban.")
        except Exception as e:
            await send_error(interaction, str(e))

    @app_commands.command(name="purge", description="Deletes the specified amount of messages in the current channel.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(amount="Number of messages to delete (1-100)")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
        # Range[int, 1, 100] pushes the 1-100 bound onto Discord's own slash
        # command validation, so an out-of-range value can't even be
        # submitted -- no wasted round trip rejecting it server-side like
        # the old manual `if amount < 1 or amount > 100` check used to.

        # Defer before purge()'s internal fetch-then-bulk-delete round trip,
        # since that occasionally runs past Discord's 3 second
        # initial-response window on a full 100-message purge. thinking=True
        # (the default) so there's visible feedback while it works, instead
        # of the old thinking=False-then-silently-delete trick that made it
        # look like the command hadn't done anything at all.
        await interaction.response.defer(ephemeral=True)

        try:
            deleted = await interaction.channel.purge(limit=amount, reason=f"Purged by {interaction.user}")
        except discord.Forbidden:
            await send_error(interaction, "Missing permissions to purge messages in this channel.")
            return
        except Exception as e:
            await send_error(interaction, str(e))
            return

        count = len(deleted)
        await send_moderation_alert(interaction.client, alert_embed(
            "🧹 Messages Purged",
            f"{interaction.user.mention} purged {count} {'message' if count == 1 else 'messages'} in {interaction.channel.mention} via `/purge`.",
            color=ALERT_COLOR_CAUTION,
        ))
        await send_success(interaction, f"Purged {count} {'message' if count == 1 else 'messages'}.")

    @app_commands.command(name="kick", description="Kicks a member from the server.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(target="Member to kick", reason="Reason for kick")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def kick(self, interaction: discord.Interaction, target: discord.Member, reason: str = "Unspecified"):
        await _kick_impl(interaction, target, reason)

    @app_commands.command(name="mute", description="Mutes a member from all channels.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(target="Member to mute", reason="Reason for the mute")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def mute(self, interaction: discord.Interaction, target: discord.Member, reason: str = "Unspecified"):
        await _mute_impl(interaction, target, reason)

    @app_commands.command(name="unmute", description="Unmutes a member from all channels.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(target="Member to unmute", reason="Reason for the unmute")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def unmute(self, interaction: discord.Interaction, target: discord.Member, reason: str = "No reason provided"):
        await _unmute_impl(interaction, target, reason)

    @app_commands.command(name="temprole", description="Gives a member a role for a set amount of time, then auto-removes it.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(
        target="Member to give the role to",
        role="Role to assign",
        duration="How long to keep the role, in minutes",
        reason="Reason for granting the role",
    )
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def temprole(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        role: discord.Role,
        duration: int,
        reason: str = "No reason provided",
    ):
        await _temprole_impl(interaction, target, role, duration, reason)

    @app_commands.command(name="dm", description="Sends a direct message to a user.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(target="User to direct message", message="Message to send")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def dm(self, interaction: discord.Interaction, target: discord.User, message: str):
        try:
            await target.send(message)
            trimmed_message = message if len(message) <= 500 else message[:497] + "..."
            await send_moderation_alert(interaction.client, alert_embed(
                "✉️ DM Sent",
                f"{interaction.user.mention} sent a DM to {target.mention} via `/dm`.",
                color=ALERT_COLOR_EDIT,
                fields=[("Message", trimmed_message, False)],
            ))
            await send_success(interaction, f"Sent message to {target.mention}.")
        except discord.Forbidden as e:
            if e.code == 50007:
                await send_error(interaction, f"Failed to dm {target.mention}. They may have dms disabled, or you're not connected through a shared server or friendship.")
            else:
                await send_error(interaction, f"Failed to dm: {e}")
        except discord.HTTPException as e:
            if e.status == 400 and e.code == 50007:
                await send_error(interaction, f"Cannot DM {target.mention}. The user may have DMs disabled or has blocked the bot.")
            else:
                await send_error(interaction, f"Failed to send DM: {e}")
        except Exception as e:
            await send_error(interaction, f"Unexpected error: {e}")

    # /ghostping user (manual ghost ping) and /ghostping toggle (passive
    # detection mode) share a single guild-scoped group -- same pattern as
    # afk.py's afk_group, so the guild restriction only needs to live once,
    # here on the top-level group, for both subcommands to inherit it.
    ghostping_group = app_commands.guilds(GUILD)(
        app_commands.Group(
            name="ghostping",
            description="Ghost ping a user, or configure detection of deleted mentions.",
        )
    )

    @ghostping_group.command(name="user", description="Sends a user's mention in this channel and deletes it immediately.")
    @app_commands.describe(user="User to ghost ping")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def ghostping_user(self, interaction: discord.Interaction, user: discord.User):
        await _ghostping_user_impl(interaction, user)

    @ghostping_group.command(name="toggle", description="Toggles whether a deleted mention gets publicly called out (Announced) or ignored (Nothing).")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def ghostping_toggle(self, interaction: discord.Interaction):
        await _ghostping_toggle_impl(interaction)

    # =====================================================================
    # Passive behavior: publicly calls out a deleted mention while
    # /ghostping toggle is set to Announced. No-ops entirely while the mode
    # is Nothing (the default), and only fires for messages discord.py had
    # already cached -- see the module-level note above the /ghostping
    # group for why that's the right population for this feature.
    # =====================================================================

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if _ghostping_mode != GHOSTPING_MODE_ANNOUNCED:
            return
        if message.author.bot or message.webhook_id is not None:
            return
        if not message.guild or message.guild.id != config.GUILD_ID:
            return

        mentioned = [user.mention for user in message.mentions] + [role.mention for role in message.role_mentions]
        if not mentioned:
            return

        deleter = await _find_message_deleter(message)
        embed = _ghostping_announcement_embed(
            sender=message.author,
            mentioned=mentioned,
            deleter=deleter,
            content=message.content,
        )

        try:
            await message.channel.send(embed=embed)
        except discord.Forbidden:
            print(f"Missing permissions to post ghost ping callout in {getattr(message.channel, 'name', message.channel)}")
        except Exception as e:
            print(f"Failed to post ghost ping callout: {e}")

    @app_commands.command(name="slowmode", description="Sets slowmode for a channel (text, voice, stage, or forum).")
    @app_commands.guilds(GUILD)
    @app_commands.describe(
        seconds="Slowmode delay in seconds (0 disables it, max 21600 / 6 hours)",
        channel="Channel to set slowmode for (defaults to the current channel)",
    )
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def slowmode(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600],
        channel: Optional[_LockableChannel] = None,
    ):
        await _slowmode_impl(interaction, seconds, channel)

    @app_commands.command(name="togglelock", description="Toggles the lock/unlock state of a channel (text, voice, stage, or forum).")
    @app_commands.guilds(GUILD)
    @app_commands.describe(
        channel="Channel to toggle (defaults to the current channel)",
        duration="Lock duration in minutes -- auto-unlocks after this time (only applies when locking)",
        message="Public message to announce in the channel alongside the lock/unlock",
    )
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def togglelock(
        self,
        interaction: discord.Interaction,
        channel: Optional[_LockableChannel] = None,
        duration: Optional[app_commands.Range[int, 1, 10080]] = None,
        message: Optional[str] = None,
    ):
        await _togglelock_impl(interaction, channel, duration, message)

    @app_commands.command(name="togglelockdown", description="Toggles the lock or unlock state on all text, voice, stage, and forum channels.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(
        duration="Lockdown duration in minutes -- auto-lifts after this time (only applies when locking down)",
        message="Public message to announce in this channel alongside the lockdown/lift",
    )
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def togglelockdown(
        self,
        interaction: discord.Interaction,
        duration: Optional[app_commands.Range[int, 1, 10080]] = None,
        message: Optional[str] = None,
    ):
        await _togglelockdown_impl(interaction, duration, message)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
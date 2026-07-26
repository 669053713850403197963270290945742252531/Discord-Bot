import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import (
    has_role, is_in_guild, can_moderate, notify_user,
    send_success, send_error, edit_or_send_error, error_embed, success_embed,
)

GUILD = discord.Object(id=config.GUILD_ID)


# =========================================================================
# Implementations (standalone functions so context_menus.py can call them
# directly, without needing a bound cog instance)
# =========================================================================

async def _ban_impl(interaction: discord.Interaction, target: discord.User, reason: str = "None", duration: int = None, preserve_messages: bool = True):
    try:
        await interaction.response.send_message(f"Processing ban for {target.mention}...", ephemeral=True)

        member = interaction.guild.get_member(target.id)

        # Only run moderation checks and message deletion for members
        if member:
            await can_moderate(interaction, member)

            try:
                embed = discord.Embed(title=f"You have been banned from {interaction.guild.name}", description=f"**Reason:** {reason}", color=discord.Color.red(), timestamp=datetime.now(timezone.utc))

                if duration:
                    unban_time = datetime.now(timezone.utc) + timedelta(minutes=duration)
                    timestamp = int(unban_time.timestamp())
                    minute_label = "minute" if duration == 1 else "minutes"

                    embed.add_field(name="Duration", value=f"{duration} {minute_label}", inline=True)
                    embed.add_field(name="Unban Time", value=f"<t:{timestamp}:F>\n<t:{timestamp}:T> (<t:{timestamp}:R>)", inline=True)

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

        if duration:
            async def unban_later():
                await asyncio.sleep(duration * 60)
                try:
                    await interaction.guild.unban(target, reason="Temporary ban expired")
                except Exception as e:
                    print(f"Failed to unban {target}: {e}")

            interaction.client.loop.create_task(unban_later())

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
        await send_success(interaction, f"{target.mention} has been unmuted.")
        await notify_user(target, "unmuted", interaction.user, reason, interaction.guild.name)
    except discord.Forbidden:
        await send_error(interaction, "Missing permissions to remove roles.")


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
# has already manually toggled it back.
_lock_duration_tasks: dict = {}


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
    # applies once its lock state is about to change again.
    pending_task = _lock_duration_tasks.pop(target.id, None)
    if pending_task:
        pending_task.cancel()

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

    if message:
        await _send_lock_announcement(
            target,
            title=_LOCK_ANNOUNCEMENT_TITLES[("togglelock", action)],
            message=message,
            duration=duration if action == "locked" else None,
        )

    if action == "locked" and duration:
        async def _auto_unlock():
            await asyncio.sleep(duration * 60)
            _lock_duration_tasks.pop(target.id, None)
            fresh_overwrite = target.overwrites_for(everyone_role)
            for perm in perm_names:
                setattr(fresh_overwrite, perm, None)
            try:
                await target.set_permissions(everyone_role, overwrite=fresh_overwrite, reason="Lock duration expired")
                await _send_lock_announcement(
                    target,
                    title=_LOCK_ANNOUNCEMENT_TITLES[("togglelock", "unlocked")],
                    message="Lock duration expired -- channel automatically unlocked.",
                )
            except Exception as e:
                print(f"Failed to auto-unlock {target.name}: {e}")

        _lock_duration_tasks[target.id] = interaction.client.loop.create_task(_auto_unlock())


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

    if _lockdown_snapshots:
        # Lockdown is currently active -> disable it by restoring each
        # channel to whatever state it actually had *before* lockdown was
        # enabled, rather than blanket-unlocking everything. This keeps
        # channels that were already manually locked beforehand locked.
        count = await _lockdown_restore(channels, everyone_role)
        action = "unlocked"
    else:
        # Not currently in lockdown -> enable it.
        count = await _lockdown_apply(channels, everyone_role)
        action = "locked"

    # Duration only means anything when this toggle just enabled lockdown;
    # flag it rather than silently ignoring it if it was passed on a lift.
    ignored_duration = duration is not None and action == "unlocked"
    confirmation_fields = [("Note", "Duration is ignored when unlocking.", False)] if ignored_duration else None
    await send_success(interaction, f"{action.capitalize()} {count} channel(s).", fields=confirmation_fields)

    # There's no single "the channel" a lockdown applies to, so the public
    # announcement (if any) is posted wherever the command was run rather
    # than fanned out across every affected channel.
    announce_channel = interaction.channel
    if message:
        await _send_lock_announcement(
            announce_channel,
            title=_LOCK_ANNOUNCEMENT_TITLES[("togglelockdown", action)],
            message=message,
            duration=duration if action == "locked" else None,
        )

    if action == "locked" and duration:
        async def _auto_unlockdown():
            global _lockdown_duration_task
            await asyncio.sleep(duration * 60)
            _lockdown_duration_task = None
            current_channels = [ch for ch in guild.channels if isinstance(ch, LOCKDOWN_CHANNEL_TYPES)]
            await _lockdown_restore(current_channels, everyone_role)
            await _send_lock_announcement(
                announce_channel,
                title=_LOCK_ANNOUNCEMENT_TITLES[("togglelockdown", "unlocked")],
                message="Lockdown duration expired -- server automatically unlocked.",
            )

        _lockdown_duration_task = interaction.client.loop.create_task(_auto_unlockdown())


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
        try:
            bans = [ban async for ban in interaction.guild.bans()]
            banned_entry = discord.utils.find(lambda b: b.user.id == user.id, bans)

            if not banned_entry:
                await send_error(interaction, "User is not banned.")
                return

            await interaction.guild.unban(banned_entry.user, reason=f"Unbanned by {interaction.user}")
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

    @app_commands.command(name="dm", description="Sends a direct message to a user.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(target="User to direct message", message="Message to send")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def dm(self, interaction: discord.Interaction, target: discord.User, message: str):
        try:
            await target.send(message)
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

    @app_commands.command(name="ghostping", description="Sends a user's mention in this channel and deletes it immediately.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="User to ghost ping")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def ghostping(self, interaction: discord.Interaction, user: discord.User):
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

        await send_success(interaction, f"Ghost pinged {user.mention}.")

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
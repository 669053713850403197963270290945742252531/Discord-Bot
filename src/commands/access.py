import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, send_success, send_error

GUILD = discord.Object(id=config.GUILD_ID)

# Discord IDs currently holding the Bot Access role via /tempaccess, so a
# second grant for the same user can be rejected instead of stacking timers.
_active_temp_access: set = set()

# Snapshot of each channel's @everyone overwrite permissions from right
# before /togglelockdown was last enabled. Non-empty while lockdown is
# active; used to restore channels to their exact prior state (instead of
# blanket unlocking) so channels that were already locked beforehand stay
# locked.
_lockdown_snapshots: dict = {}


async def _toggleaccess_impl(interaction: discord.Interaction, user: discord.Member):
    guild = interaction.guild
    role = guild.get_role(config.REQUIRED_ROLE_ID)
    if not role:
        return await send_error(interaction, "Bot Access role not found.")

    if role in user.roles:
        await user.remove_roles(role, reason=f"Toggled off Bot Access role by {interaction.user}")
        await send_success(interaction, f"Removed {role.name} role from {user.mention}.")
    else:
        await user.add_roles(role, reason=f"Toggled on Bot Access role by {interaction.user}")
        await send_success(interaction, f"Granted {role.name} role to {user.mention}.")


async def _remove_temp_access_after(interaction: discord.Interaction, user: discord.Member, role: discord.Role, minutes: int):
    try:
        await asyncio.sleep(minutes * 60)

        # Fetch a fresh member since roles aren't always reflected on the
        # cached object right away.
        guild = interaction.client.get_guild(user.guild.id)
        fresh_member = guild.get_member(user.id)
        if fresh_member and role in fresh_member.roles:
            await fresh_member.remove_roles(role, reason="Temporary Bot Access expired")
        _active_temp_access.discard(user.id)
    except Exception as e:
        _active_temp_access.discard(user.id)
        print(f"Error removing temporary Bot Access role: {e}")


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
        _active_temp_access.add(user.id)
        await send_success(interaction, f"Given Bot Access role to {user.mention} for {minutes} minutes.")

        interaction.client.loop.create_task(_remove_temp_access_after(interaction, user, role, minutes))
    except Exception as e:
        await send_error(interaction, f"Failed to give Bot Access role: {e}")


LOCKDOWN_CHANNEL_TYPES = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)


def _lockdown_perms(channel: discord.abc.GuildChannel) -> tuple:
    """Which @everyone overwrite permissions get locked/restored for a given channel type."""
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return ("connect", "send_messages")
    return ("send_messages",)


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

    @app_commands.command(name="togglelock", description="Toggles the lock or unlock state on the current channel.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def togglelock(self, interaction: discord.Interaction):
        channel = interaction.channel
        everyone_role = interaction.guild.default_role
        overwrite = channel.overwrites_for(everyone_role)

        is_locked = overwrite.send_messages is False

        if is_locked:
            overwrite.send_messages = None
            action = "unlocked"
        else:
            overwrite.send_messages = False
            action = "locked"

        await channel.set_permissions(everyone_role, overwrite=overwrite)
        await send_success(interaction, f"{channel.name} has been {action}.")

    @app_commands.command(name="togglelockdown", description="Toggles the lock or unlock state on all text, voice, and stage channels.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def togglelockdown(self, interaction: discord.Interaction):
        # Defer immediately -- looping + editing permissions on every
        # channel in the server can easily take longer than the 3 second
        # window Discord gives an interaction before it expires.
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        everyone_role = guild.default_role

        channels = [ch for ch in guild.channels if isinstance(ch, LOCKDOWN_CHANNEL_TYPES)]
        if not channels:
            return await send_error(interaction, "No text, voice, or stage channels found.")

        count = 0

        if _lockdown_snapshots:
            # Lockdown is currently active -> disable it by restoring each
            # channel to whatever state it actually had *before* lockdown
            # was enabled, rather than blanket-unlocking everything. This
            # keeps channels that were already manually locked beforehand
            # locked.
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
                        await channel.set_permissions(everyone_role, overwrite=overwrite)
                        count += 1
                    except discord.Forbidden:
                        print(f"Missing permissions to restore {channel.name}")
                    except Exception as e:
                        print(f"Failed to restore {channel.name}: {e}")

            _lockdown_snapshots.clear()
            action = "unlocked"
        else:
            # Not currently in lockdown -> enable it. Snapshot each
            # channel's current overwrite state first so it can be restored
            # exactly later.
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
                        await channel.set_permissions(everyone_role, overwrite=overwrite)
                        count += 1
                    except discord.Forbidden:
                        print(f"Missing permissions to lock {channel.name}")
                    except Exception as e:
                        print(f"Failed to lock {channel.name}: {e}")

            action = "locked"

        await send_success(interaction, f"{action.capitalize()} {count} channel(s).")


async def setup(bot: commands.Bot):
    await bot.add_cog(Access(bot))

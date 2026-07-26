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


async def setup(bot: commands.Bot):
    await bot.add_cog(Access(bot))

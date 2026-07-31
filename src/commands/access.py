import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, send_success, send_error
from api.alerts import (
    send_alert, alert_embed,
    ALERT_COLOR_ADD, ALERT_COLOR_REMOVE, ALERT_COLOR_TEMP, ALERT_COLOR_CAUTION,
    alerts_enabled, set_alerts_enabled,
)

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


async def _remove_temp_access_after(interaction: discord.Interaction, user: discord.Member, role: discord.Role, minutes: int):
    try:
        await asyncio.sleep(minutes * 60)

        # Fetch a fresh member since roles aren't always reflected on the
        # cached object right away.
        guild = interaction.client.get_guild(user.guild.id)
        fresh_member = guild.get_member(user.id)
        if fresh_member and role in fresh_member.roles:
            await fresh_member.remove_roles(role, reason="Temporary Bot Access expired")
            await send_alert(interaction.client, alert_embed(
                "⌛ Temp Bot Access Expired",
                f"{user.mention}'s temporary {role.mention} role expired and was auto-removed.",
                color=ALERT_COLOR_REMOVE,
            ))
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
        await send_alert(interaction.client, alert_embed(
            "🔓 Temp Bot Access Granted",
            f"{interaction.user.mention} granted {user.mention} the {role.mention} role for {minutes} minute(s) via `/tempaccess`.",
            color=ALERT_COLOR_TEMP,
        ))
        await send_success(interaction, f"Given Bot Access role to {user.mention} for {minutes} minutes.")

        interaction.client.loop.create_task(_remove_temp_access_after(interaction, user, role, minutes))
    except Exception as e:
        await send_error(interaction, f"Failed to give Bot Access role: {e}")


async def _togglealerts_impl(interaction: discord.Interaction):
    """Flips api.alerts's mute switch. This is process-local and resets to
    enabled on every restart -- see api.alerts._alerts_enabled -- so it's
    meant for a single testing session, not a lasting setting. The toggle
    itself always posts to the Alerts channel (bypass_mute=True) even when
    turning alerts *off*, so there's a visible record of exactly when/why
    the channel went quiet instead of it just stopping with no trace."""
    now_enabled = set_alerts_enabled(not alerts_enabled())

    if now_enabled:
        await send_alert(interaction.client, alert_embed(
            "🔔 Alerts Re-Enabled",
            f"{interaction.user.mention} re-enabled staff alerts via `/togglealerts`.",
            color=ALERT_COLOR_ADD,
        ), bypass_mute=True)
        await send_success(interaction, "Alerts have been **re-enabled** -- staff will see alert embeds again.")
    else:
        await send_alert(interaction.client, alert_embed(
            "🔕 Alerts Disabled",
            f"{interaction.user.mention} disabled staff alerts via `/togglealerts`. "
            "No further alert embeds will post here until this is toggled back on.",
            color=ALERT_COLOR_CAUTION,
        ), bypass_mute=True)
        await send_success(interaction, "Alerts have been **disabled** -- no alert embeds will post to the Alerts channel until this is toggled back on.")


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

    @app_commands.command(name="togglealerts", description="Toggles whether alert embeds post to the staff Alerts channel (e.g. during testing).")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def togglealerts(self, interaction: discord.Interaction):
        await _togglealerts_impl(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(Access(bot))

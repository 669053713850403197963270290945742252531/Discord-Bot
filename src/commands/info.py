import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, send_success, send_error
from api.github import GitHubAPIError, fetch_users_with_sha
from api.time_utils import format_discord_timestamp
from api.users import find_user_by_discord_id

GUILD = discord.Object(id=config.GUILD_ID)


class Info(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Returns the bot's latency.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def ping(self, interaction: discord.Interaction):
        await send_success(interaction, f"Pong! Latency: {round(self.bot.latency * 1000)}ms", title="🏓 Pong")

    @app_commands.command(name="myinfo", description="Fetches your whitelist information from the database.")
    @app_commands.guilds(GUILD)
    @is_in_guild(config.GUILD_ID)
    async def myinfo(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            users, _ = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        user_data = find_user_by_discord_id(users, interaction.user.id)
        if not user_data:
            return await send_error(interaction, "You were not found in the user database.")

        embed = discord.Embed(title=f"User Info: {interaction.user}", color=discord.Color.blue())
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="Identifier", value=user_data.get("Identifier", "N/A"), inline=True)
        embed.add_field(name="Rank", value=user_data.get("Rank", "N/A"), inline=True)
        embed.add_field(name="Join Date", value=format_discord_timestamp(user_data.get("JoinDate")), inline=True)
        embed.add_field(name="HWID", value=f"||{user_data.get('HWID', 'N/A')}||", inline=True)
        embed.add_field(name="Key", value=f"||{user_data.get('Key', 'N/A')}||", inline=True)
        embed.add_field(name="Last HWID Reset", value=format_discord_timestamp(user_data.get("LastHwidReset")), inline=True)
        embed.add_field(name="Total HWID Resets", value=str(user_data.get("totalHwidResets", 0)), inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Info(bot))

import asyncio
import platform
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, send_error, build_embed, safe_respond
from api.supabase_db import fetch_users, get_license_by_discord_id
from api.time_utils import format_discord_timestamp
from api.users import find_user_by_discord_id

GUILD = discord.Object(id=config.GUILD_ID)

# This module is imported once, during setup_hook's load_extension() call
# early in bot.run() -- close enough to "process start" for an uptime field
# without needing start.py (an entry point, not a library) to hand over a
# more precise timestamp the way it does for the refresh task below.
_PROCESS_STARTED_AT = datetime.now(timezone.utc)

# How long /botstatus keeps re-editing its own response, and how often.
# Bounded by Discord invalidating an interaction's webhook edit token ~15
# minutes after the command was invoked -- 14 minutes leaves a safety
# margin rather than running until edits start failing outright.
BOTSTATUS_TRACKER_DURATION = 14 * 60  # seconds
BOTSTATUS_TRACKER_TICK = 5  # seconds

def _asset_links(asset: discord.Asset) -> str:
    """
    Builds a row of markdown links to `asset` (an avatar/banner) at full
    4096px size in every format Discord's CDN will actually serve it in --
    static images get PNG/JPEG/WEBP, animated ones additionally get GIF so
    the animation isn't lost. `asset.url` alone only gives one (Discord's
    own auto-picked) format, which isn't enough when the point of the
    command is letting someone grab the exact file type they want.
    """
    full_size = asset.with_size(4096)
    formats = ["png", "jpg", "webp"]
    if asset.is_animated():
        formats.append("gif")
    return " • ".join(f"[{fmt.upper()}]({full_size.with_format(fmt).url})" for fmt in formats)


class Info(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="botstatus", description="Shows the bot's health and current license database status.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def botstatus(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            users = await fetch_users()
            db_status = "✅ Connected to Supabase."
            user_count = len(users)
            db_color = discord.Color.green()
        except Exception as e:
            db_status = f"❌ Couldn't reach Supabase ({e})."
            user_count = None
            db_color = discord.Color.red()

        embed = self._build_status_embed(db_status, user_count, db_color)
        message = await interaction.followup.send(embed=embed, ephemeral=True)
        asyncio.create_task(self._botstatus_tracker(message))

    def _build_status_embed(self, db_status: str, user_count: Optional[int], color: discord.Color, *, live: bool = True) -> discord.Embed:
        footer = (
            "Live status -- updates automatically for ~14 minutes" if live
            else "No longer live -- run /botstatus again for current status"
        )
        top_level = self.bot.tree.get_commands(guild=GUILD)
        group_count = sum(1 for c in top_level if isinstance(c, app_commands.Group))
        command_count = len(top_level) - group_count

        fields = [
            ("🏓 Latency", f"{round(self.bot.latency * 1000)}ms", True),
            ("⏱️ Uptime", f"<t:{int(_PROCESS_STARTED_AT.timestamp())}:R>", True),
            ("🌐 Guilds", str(len(self.bot.guilds)), True),
            ("🗄️ License Database", db_status, False),
            ("👥 Licensed Users", str(user_count) if user_count is not None else "Unavailable", True),
            ("🧩 Commands Registered", str(command_count), True),
            ("🗂️ Groups Registered", str(group_count), True),
            ("📚 discord.py", discord.__version__, True),
            ("🐍 Python", platform.python_version(), True),
        ]
        return build_embed(title="🤖 Bot Status", color=color, footer=footer, fields=fields)

    async def _botstatus_tracker(self, message: discord.WebhookMessage):
        loop_clock = asyncio.get_running_loop()
        next_tick = loop_clock.time()
        elapsed = 0
        try:
            while elapsed < BOTSTATUS_TRACKER_DURATION:
                next_tick += BOTSTATUS_TRACKER_TICK
                await asyncio.sleep(max(0, next_tick - loop_clock.time()))
                elapsed += BOTSTATUS_TRACKER_TICK
                try:
                    users = await fetch_users()
                    db_status = "✅ Connected to Supabase."
                    user_count = len(users)
                    color = discord.Color.green()
                except Exception as e:
                    db_status = f"❌ Couldn't reach Supabase ({e})."
                    user_count = None
                    color = discord.Color.red()
                try:
                    await message.edit(embed=self._build_status_embed(db_status, user_count, color))
                except discord.NotFound:
                    return
                except discord.HTTPException:
                    pass
            try:
                users = await fetch_users()
                await message.edit(embed=self._build_status_embed("✅ Connected to Supabase.", len(users), discord.Color.green(), live=False))
            except Exception as e:
                await message.edit(embed=self._build_status_embed(f"❌ Couldn't reach Supabase ({e}).", None, discord.Color.red(), live=False))
        except (asyncio.CancelledError, discord.NotFound, discord.HTTPException):
            pass

    @app_commands.command(name="myinfo", description="Fetches your whitelist information from the database.")
    @app_commands.guilds(GUILD)
    @is_in_guild(config.GUILD_ID)
    async def myinfo(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            user_data = await get_license_by_discord_id(str(interaction.user.id))
        except Exception as e:
            return await send_error(interaction, f"Database error: {e}")
        if not user_data:
            return await send_error(interaction, "You were not found in the license database.")
        embed = discord.Embed(title=f"User Info: {interaction.user}", color=discord.Color.blue())
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="Identifier", value=user_data.get("Identifier", "N/A"), inline=True)
        embed.add_field(name="Rank", value=user_data.get("Rank", "N/A"), inline=True)
        embed.add_field(name="Activated", value=format_discord_timestamp(user_data.get("Activated")), inline=True)
        embed.add_field(name="Key", value=f"||`{user_data.get('Key', 'N/A')}`||", inline=True)
        embed.add_field(name="Executions", value=str(user_data.get("Executions", 0)), inline=True)
        embed.add_field(name="Games", value="All games" if "*" in (user_data.get("Games") or []) else ", ".join(user_data.get("Games") or []) or "None", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="avatar", description="Fetches a user's full-size avatar, server avatar, and banner.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="The user to look up -- defaults to yourself")
    @is_in_guild(config.GUILD_ID)
    async def avatar(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        member = user or interaction.user
        await interaction.response.defer(ephemeral=True)

        # Member/cached-User objects never carry banner or accent_color --
        # those aren't sent over the gateway, only returned by a live fetch
        # -- so this is the one part of the command that has to hit
        # Discord's API instead of reading straight off the cache.
        try:
            full_user = await interaction.client.fetch_user(member.id)
        except discord.HTTPException as e:
            return await send_error(interaction, f"Couldn't fetch {member.mention}'s profile: {e}")

        accent_color = full_user.accent_color
        embed_color = member.color if member.color.value else (accent_color or discord.Color.blurple())

        avatar_embed = build_embed(title=f"🖼️ {member.display_name}'s Avatar", color=embed_color)

        global_avatar = full_user.avatar
        if global_avatar:
            avatar_embed.set_image(url=global_avatar.with_size(4096).url)
            avatar_embed.add_field(name="Global Avatar", value=_asset_links(global_avatar), inline=False)
        else:
            avatar_embed.description = "This user has no custom avatar set -- showing Discord's default."
            avatar_embed.set_image(url=full_user.default_avatar.url)

        # guild_avatar is only ever set on a Member (a per-server override),
        # never on the plain User fetch_user() returns -- so this has to
        # come from `member`, not `full_user`.
        guild_avatar = member.guild_avatar
        if guild_avatar:
            avatar_embed.set_thumbnail(url=guild_avatar.with_size(4096).url)
            avatar_embed.add_field(name="Server Avatar", value=_asset_links(guild_avatar), inline=False)

        embeds = [avatar_embed]

        if full_user.banner:
            banner_embed = build_embed(
                title=f"🎏 {member.display_name}'s Banner",
                color=accent_color or embed_color,
            )
            banner_embed.set_image(url=full_user.banner.with_size(4096).url)
            banner_embed.add_field(name="Banner", value=_asset_links(full_user.banner), inline=False)
            embeds.append(banner_embed)
        elif accent_color:
            avatar_embed.add_field(name="Banner", value=f"No banner image -- accent color `#{accent_color.value:06X}`", inline=False)
        else:
            avatar_embed.add_field(name="Banner", value="No banner or accent color set.", inline=False)

        await safe_respond(interaction, embeds=embeds, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Info(bot))

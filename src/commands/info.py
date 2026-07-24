import asyncio
import platform
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, send_error, build_embed
from api.github import (
    GitHubAPIError, fetch_users_with_sha,
    get_cached_users, cached_users_updated_at, next_cache_refresh,
)
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


class Info(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="botstatus", description="Shows the bot's health: latency, user-cache sync status, and more.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def botstatus(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # --- User cache vs. the live Users.json file ---
        # get_cached_users() alone only says what the cache held as of its
        # last successful refresh -- it can't say whether that's still true
        # *right now*. A live fetch is the only way to actually answer "is
        # the cache in sync", so (unlike the control panel's pre-checks)
        # this deliberately pays for a real Contents-API round trip.
        #
        # This is the ONLY GitHub call this command makes, including over
        # the tracker loop's ~14-minute lifetime below -- it isn't repeated
        # per tick. The tracker's periodic edits below only re-read local
        # process state (get_cached_users()'s timestamp, the refresh task's
        # schedule), which costs nothing, so leaving /botstatus open doesn't
        # add its own polling on top of the refresh loop's.
        sync_state = await self._check_cache_sync()

        embed = self._build_status_embed(sync_state)
        message = await interaction.followup.send(embed=embed, ephemeral=True)

        asyncio.create_task(self._botstatus_tracker(message, sync_state))

    async def _check_cache_sync(self) -> dict:
        """One-time live comparison of the in-memory cache against the real
        Users.json file, returned as {"status": <field text>, "color": <embed
        color>}. The tracker loop below reuses this dict rather than calling
        this again on every tick."""
        cached_users = get_cached_users()
        if cached_users is None:
            return {"status": "⚠️ Not yet populated (bot likely just restarted).", "color": discord.Color.orange()}

        try:
            live_users, _sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return {"status": f"❌ Couldn't reach GitHub to compare ({e}).", "color": discord.Color.red()}

        if live_users == cached_users:
            return {"status": "✅ Synced -- matches the live Users.json file.", "color": discord.Color.green()}
        return {"status": "⚠️ Out of sync with Users.json (will self-correct at the next update below).", "color": discord.Color.orange()}

    def _build_status_embed(self, sync_state: dict, *, live: bool = True) -> discord.Embed:
        updated_at = cached_users_updated_at()
        last_updated = f"<t:{int(updated_at.timestamp())}:R>" if updated_at else "Never"

        next_refresh = next_cache_refresh()
        next_update = (
            f"<t:{int(next_refresh.timestamp())}:R>" if next_refresh
            else "Not scheduled (background task not running yet)"
        )

        footer = (
            "Live status -- updates automatically for ~14 minutes" if live
            else "No longer live -- run /botstatus again for current status"
        )

        return build_embed(
            title="🤖 Bot Status",
            color=sync_state["color"],
            footer=footer,
            fields=[
                ("🏓 Latency", f"{round(self.bot.latency * 1000)}ms", True),
                ("⏱️ Uptime", f"<t:{int(_PROCESS_STARTED_AT.timestamp())}:R>", True),
                ("🌐 Guilds", str(len(self.bot.guilds)), True),
                ("📦 User Cache", sync_state["status"], False),
                ("🕒 Cache Last Updated", last_updated, True),
                ("⏭️ Next Cache Update", next_update, True),
                ("🧩 Commands Registered", str(len(self.bot.tree.get_commands(guild=GUILD))), True),
                ("📚 discord.py", discord.__version__, True),
                ("🐍 Python", platform.python_version(), True),
            ],
        )

    async def _botstatus_tracker(self, message: discord.WebhookMessage, sync_state: dict):
        """Keeps /botstatus's own response current.

        Without this, Cache Last Updated / Next Cache Update are frozen at
        whatever they were the instant the command ran: Discord renders
        <t:...:R> relative to the *viewer's current time*, so a frozen
        "in 45 seconds" naturally decays into "45 seconds ago" once that
        instant passes -- even though the real next_cache_refresh() has long
        since moved forward to a new future time. Re-editing the message
        with fresh values on a timer is the only way to keep it honest, the
        same way keys_hwid.py's temp-whitelist tracker keeps its "Time Left"
        field honest.

        Also watches cached_users_updated_at() for changes: when it moves,
        a background refresh just landed, so this flips a stale "Out of
        sync"/"Couldn't reach GitHub" verdict over to synced -- without
        spending a fresh GitHub call to re-verify it, since a refresh by
        definition just pulled the current file (see refresh_users_cache()).
        """
        last_seen_updated_at = cached_users_updated_at()

        loop_clock = asyncio.get_running_loop()
        next_tick = loop_clock.time()
        elapsed = 0

        try:
            while elapsed < BOTSTATUS_TRACKER_DURATION:
                next_tick += BOTSTATUS_TRACKER_TICK
                await asyncio.sleep(max(0, next_tick - loop_clock.time()))
                elapsed += BOTSTATUS_TRACKER_TICK

                updated_at = cached_users_updated_at()
                if updated_at is not None and updated_at != last_seen_updated_at:
                    last_seen_updated_at = updated_at
                    sync_state = {
                        "status": "✅ Synced -- matches Users.json as of the refresh above.",
                        "color": discord.Color.green(),
                    }

                try:
                    await message.edit(embed=self._build_status_embed(sync_state))
                except discord.NotFound:
                    return  # Message deleted -- nothing left to update.
                except discord.HTTPException:
                    pass  # Transient (rate limit, etc.) -- just retry next tick.

            try:
                await message.edit(embed=self._build_status_embed(sync_state, live=False))
            except (discord.NotFound, discord.HTTPException):
                pass
        except asyncio.CancelledError:
            pass

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

import asyncio
import platform
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, send_error, build_embed
from api.github import (
    GitHubAPIError, fetch_users_with_sha, fetch_rate_limit,
    get_cached_users, cached_users_updated_at,
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

# --- /ratelimits ----------------------------------------------------------
#
# Remaining-fraction thresholds (of a single resource's `limit`) at/below
# which /ratelimits flags that resource as "getting low" or "critical".
# Every resource gets a status emoji at these thresholds, but it's
# specifically `core` -- the quota every Users.json read/write and most
# other GitHub calls in this bot draw from -- whose status decides the
# embed's overall color and whether the standalone warning field appears.
RATE_LIMIT_WARNING_THRESHOLD = 0.20
RATE_LIMIT_CRITICAL_THRESHOLD = 0.05

# Cosmetic only -- anything not listed here still gets a field, just with
# the fallback emoji. Keys are exactly the resource names GitHub's
# /rate_limit response uses (see fetch_rate_limit()).
RATE_LIMIT_RESOURCE_EMOJI = {
    "core": "🧩",
    "search": "🔍",
    "code_search": "🔎",
    "graphql": "🕸️",
    "integration_manifest": "📄",
    "source_import": "📥",
    "code_scanning_autofix": "🛠️",
    "actions_runner_registration": "🏃",
    "scim": "🧑‍🤝‍🧑",
    "dependency_snapshots": "📦",
    "dependency_sbom": "📋",
    "audit_log": "📜",
    "audit_log_streaming": "📡",
    "copilot_usage_records": "🤖",
}
RATE_LIMIT_RESOURCE_EMOJI_DEFAULT = "🔧"

# How long /ratelimits keeps re-editing its own response, and how often.
# Unlike /botstatus's tracker, each tick here does re-hit GitHub -- but
# /rate_limit is explicitly free to call (doesn't itself count against any
# limit it reports), so polling it this often is safe. Duration is bounded
# by the same ~15-minute webhook-edit-token window as /botstatus.
RATELIMITS_TRACKER_DURATION = 14 * 60  # seconds
RATELIMITS_TRACKER_TICK = 15  # seconds

# Values accepted by /ratelimits' `preview` option -- lets someone see what
# the warning/critical embed states actually look like without needing to
# genuinely burn through ~4,750+ of the PAT's real Core requests first.
RATE_LIMIT_PREVIEW_CHOICES = [
    app_commands.Choice(name="Warning (yellow) -- Core near its limit", value="warning"),
    app_commands.Choice(name="Critical (red) -- Core almost/fully exhausted", value="critical"),
]


def _apply_rate_limit_preview(resources: Dict[str, Any], preview: Optional[str]) -> Dict[str, Any]:
    """
    Returns a copy of `resources` with `core`'s used/remaining overridden to
    land just inside the warning/critical threshold, for /ratelimits'
    `preview` option. `preview` is None (no-op, returns `resources`
    unchanged) unless that option was explicitly passed.

    Only `core` is touched -- every other resource in the returned dict is
    still whatever GitHub actually reported, so `preview` only ever fakes
    the one number the warning field and embed color key off of.
    """
    if preview not in ("warning", "critical"):
        return resources

    resources = dict(resources)
    core = dict(resources.get("core", {}))
    limit = core.get("limit") or 5000

    # Land one request inside the threshold's edge -- comfortably past it
    # rather than exactly on the boundary, so rounding can't accidentally
    # put it back on the wrong side.
    threshold = RATE_LIMIT_CRITICAL_THRESHOLD if preview == "critical" else RATE_LIMIT_WARNING_THRESHOLD
    remaining = max(0, int(limit * threshold) - 1)

    core["limit"] = limit
    core["used"] = limit - remaining
    core["remaining"] = remaining
    resources["core"] = core
    return resources


def _rate_limit_status(used: int, limit: int) -> Tuple[str, float]:
    """Returns (status_emoji, remaining_fraction) for one resource entry."""
    if limit <= 0:
        return "⚪", 1.0
    remaining_fraction = max(0.0, (limit - used) / limit)
    if remaining_fraction <= RATE_LIMIT_CRITICAL_THRESHOLD:
        return "🔴", remaining_fraction
    if remaining_fraction <= RATE_LIMIT_WARNING_THRESHOLD:
        return "🟡", remaining_fraction
    return "🟢", remaining_fraction


def _rate_limit_field(name: str, resource: Dict[str, Any]) -> Tuple[str, str, bool]:
    """Builds one (name, value, inline) embed-field tuple for a single
    resource entry out of the /rate_limit response (e.g. resources["core"])."""
    limit = resource.get("limit", 0)
    used = resource.get("used", 0)
    remaining = resource.get("remaining", max(0, limit - used))
    reset_ts = resource.get("reset", 0)

    status_emoji, _ = _rate_limit_status(used, limit)
    icon = RATE_LIMIT_RESOURCE_EMOJI.get(name, RATE_LIMIT_RESOURCE_EMOJI_DEFAULT)
    label = name.replace("_", " ").title()

    field_name = f"{status_emoji} {icon} {label}"
    field_value = f"**{remaining:,}** / {limit:,} left ({used:,} used)\nResets <t:{reset_ts}:R>"
    return field_name, field_value, True


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
        return {"status": "⚠️ Out of sync with Users.json (will self-correct once the cache next refreshes).", "color": discord.Color.orange()}

    def _build_status_embed(self, sync_state: dict, *, live: bool = True) -> discord.Embed:
        updated_at = cached_users_updated_at()
        last_updated = f"<t:{int(updated_at.timestamp())}:R>" if updated_at else "Never"

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
                ("🧩 Commands Registered", str(len(self.bot.tree.get_commands(guild=GUILD))), True),
                ("📚 discord.py", discord.__version__, True),
                ("🐍 Python", platform.python_version(), True),
            ],
        )

    async def _botstatus_tracker(self, message: discord.WebhookMessage, sync_state: dict):
        """Keeps /botstatus's own response current.

        Without this, Cache Last Updated is frozen at whatever it was the
        instant the command ran: Discord renders <t:...:R> relative to the
        *viewer's current time*, so a frozen "45 seconds ago" only gets
        further out of date as real time passes, and never picks up a
        refresh that lands after the command was run. Re-editing the
        message with fresh values on a timer is the only way to keep it
        honest, the same way keys_hwid.py's temp-whitelist tracker keeps its
        "Time Left" field honest.

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

    @app_commands.command(name="ratelimits", description="Shows this bot's GitHub PAT rate-limit usage across every API category.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(preview="Testing only -- force Core into the warning/critical state to preview it, without spending real quota")
    @app_commands.choices(preview=RATE_LIMIT_PREVIEW_CHOICES)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def ratelimits(self, interaction: discord.Interaction, preview: Optional[app_commands.Choice[str]] = None):
        await interaction.response.defer(ephemeral=True)
        preview_mode = preview.value if preview else None

        try:
            data = await fetch_rate_limit()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        embed = self._build_ratelimits_embed(data, preview_mode)
        message = await interaction.followup.send(embed=embed, ephemeral=True)

        asyncio.create_task(self._ratelimits_tracker(message, preview_mode))

    def _build_ratelimits_embed(self, data: Dict[str, Any], preview_mode: Optional[str], *, live: bool = True) -> discord.Embed:
        # `data["rate"]` is GitHub's older/deprecated top-level field -- it's
        # always identical to resources["core"], so it's only used here as a
        # fallback in the (shouldn't-happen) case resources is missing core.
        raw_resources: Dict[str, Any] = data.get("resources", {}) or {"core": data.get("rate", {})}
        resources = _apply_rate_limit_preview(raw_resources, preview_mode)

        core = resources.get("core", {})
        core_limit = core.get("limit", 0)
        core_used = core.get("used", 0)
        core_remaining = core.get("remaining", max(0, core_limit - core_used))
        _, core_remaining_fraction = _rate_limit_status(core_used, core_limit)

        if core_remaining_fraction <= RATE_LIMIT_CRITICAL_THRESHOLD:
            color = discord.Color.red()
        elif core_remaining_fraction <= RATE_LIMIT_WARNING_THRESHOLD:
            color = discord.Color.orange()
        else:
            color = discord.Color.green()

        fields: List[Tuple[str, str, bool]] = []

        # Standalone warning -- only shown once Core is actually close to
        # running out, since that's the one resource this bot's own calls
        # (Users.json reads/writes, key generation, commit history, etc.)
        # can realistically exhaust.
        if core_remaining_fraction <= RATE_LIMIT_WARNING_THRESHOLD:
            severity = "**already exhausted**" if core_remaining <= 0 else "**close to being exhausted**"
            fields.append((
                "⚠️ Core Rate Limit Warning",
                f"This PAT's Core quota is {severity}: **{core_remaining:,}/{core_limit:,}** requests "
                f"left this window. Once it hits 0, GitHub starts rejecting this bot's requests outright "
                f"(Users.json reads/writes included) until it resets <t:{core.get('reset', 0)}:R>.\n"
                f"This limit is set and enforced entirely on GitHub's end -- the bot can't raise it, "
                f"delay it, or work around it in any way.",
                False,
            ))

        # Core first (it's the one that actually matters to this bot), then
        # every other resource GitHub reports for this PAT, alphabetically.
        for name in sorted(resources.keys(), key=lambda k: (k != "core", k)):
            fields.append(_rate_limit_field(name, resources[name]))

        title = "📊 GitHub Rate Limit Status"
        footer_parts = []
        if preview_mode:
            title += " -- Core PREVIEW"
            footer_parts.append(f"🧪 Core is faked to preview the {preview_mode} state -- not real usage.")
        footer_parts.append(
            "Live -- updates automatically for ~14 minutes" if live
            else "No longer live -- run /ratelimits again for current numbers"
        )
        footer_parts.append("GitHub enforces these limits server-side per token, per hour -- this bot can only report them, not change them.")

        return build_embed(
            title=title,
            description="Usage for this bot's configured PAT, across every quota GitHub tracks for it.",
            color=color,
            fields=fields,
            footer=" • ".join(footer_parts),
        )

    async def _ratelimits_tracker(self, message: discord.WebhookMessage, preview_mode: Optional[str]):
        """Keeps /ratelimits' own response current by re-polling GitHub's
        /rate_limit endpoint on a timer and re-editing the message -- e.g.
        so a /unwhitelist run elsewhere while this is still open shows
        Core's `used` tick up live instead of freezing at whatever it was
        the instant the command ran.

        Unlike /botstatus's tracker (which only re-reads free local state
        on each tick), this one genuinely re-fetches from GitHub every tick
        -- safe to do this often since /rate_limit is explicitly free to
        call, and it's the only way to see `used` change at all (there's no
        local cache of it to fall back on)."""
        loop_clock = asyncio.get_running_loop()
        next_tick = loop_clock.time()
        elapsed = 0

        try:
            while elapsed < RATELIMITS_TRACKER_DURATION:
                next_tick += RATELIMITS_TRACKER_TICK
                await asyncio.sleep(max(0, next_tick - loop_clock.time()))
                elapsed += RATELIMITS_TRACKER_TICK

                try:
                    data = await fetch_rate_limit()
                except GitHubAPIError:
                    continue  # Transient -- leave the last-good embed up and retry next tick.

                try:
                    await message.edit(embed=self._build_ratelimits_embed(data, preview_mode))
                except discord.NotFound:
                    return  # Message deleted -- nothing left to update.
                except discord.HTTPException:
                    pass  # Transient (rate limit, etc.) -- just retry next tick.

            try:
                data = await fetch_rate_limit()
                await message.edit(embed=self._build_ratelimits_embed(data, preview_mode, live=False))
            except (GitHubAPIError, discord.NotFound, discord.HTTPException):
                pass
        except asyncio.CancelledError:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Info(bot))

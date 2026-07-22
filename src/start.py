"""
Entry point. Everything else in this package is a library (api/) or an
extension (commands/); this is the only file that actually constructs the
Client, wires the 10 extensions into it, and calls bot.run().

Run from the repo root with `python src/start.py` (after `pip install -r
requirements.txt` and filling in `.env`).
"""

import sys
import os
import re
import shutil
import traceback
from pathlib import Path

# So `import api` / `import commands` resolve as top-level packages no
# matter what directory this is launched from
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))



def _clear_pycache():
    """
    Deletes every __pycache__ folder under src/ before anything in this
    package gets imported.
    """
    src_dir = Path(__file__).resolve().parent
    for pycache_dir in src_dir.rglob("__pycache__"):
        shutil.rmtree(pycache_dir, ignore_errors=True)
 
 
_clear_pycache()

from keep_alive import keep_alive

# Spun up before the heavier discord.py import below so a host that's
# waiting on an open port (e.g. Replit/UptimeRobot-style keep-alive setups)
# sees one as early as possible, same as the original single-file bot.
keep_alive()

import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.app_commands import errors as app_errors

from api import config
from api.github import GitHubAPIError, refresh_users_cache
from api.discord_helpers import send_error, notify_permission_error
from commands.panel import ControlPanelView

# // Intents & Client //

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

EXTENSIONS = (
    "commands.info",
    "commands.utility",
    "commands.moderation",
    "commands.whitelist",
    "commands.keys_hwid",
    "commands.database",
    "commands.panel",
    "commands.access",
    "commands.reaction_roles",
    "commands.context_menus",
)


EXTENSION_MAX_COMMANDS = {
    "commands.info": 2,
    "commands.utility": 2,
    "commands.moderation": 9,
    "commands.whitelist": 12,
    "commands.keys_hwid": 8,
    "commands.database": 7,
    "commands.panel": 2,
    "commands.access": 4,
    "commands.reaction_roles": 1,
    "commands.context_menus": 15,
}
TOTAL_DEFINED_COMMANDS = sum(EXTENSION_MAX_COMMANDS.values())  # 62


class Client(commands.Bot):
    async def setup_hook(self):
        guild_obj = discord.Object(id=config.GUILD_ID)
        total_extensions = len(EXTENSIONS)
        loaded_extensions = 0

        for extension in EXTENSIONS:
            before = len(self.tree.get_commands(guild=guild_obj))
            print(f"Loading extension: {extension}")
            try:
                await self.load_extension(extension)
            except Exception:
                print(f"FAILED to load extension: {extension}")
                traceback.print_exc()
                continue
            loaded_extensions += 1
            after = len(self.tree.get_commands(guild=guild_obj))
            added = after - before
            max_for_ext = EXTENSION_MAX_COMMANDS.get(extension, added)
            print(f"Loaded extension:  {extension} ({added}/{max_for_ext} commands loaded)")

        registered_commands = len(self.tree.get_commands(guild=guild_obj))
        print(
            f"All {loaded_extensions}/{total_extensions} extensions loaded, "
            f"{registered_commands}/{TOTAL_DEFINED_COMMANDS} commands registered."
        )

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="database"))

        # Re-registers the /createpanel control panel's button handlers so
        # they keep responding after a bot restart. This does NOT resend the
        # message -- the panel embed posted by /createpanel stays put in
        # #panel; this just reconnects its (fixed custom_id) buttons to a
        # live view again, since ControlPanelView(timeout=None) instances
        # don't otherwise survive a process restart.
        self.add_view(ControlPanelView())

        # Guarded with is_running() since on_ready can fire again on
        # reconnect, and tasks.loop.start() raises if it's already going.
        if not refresh_users_cache_task.is_running():
            refresh_users_cache_task.start()

        try:
            guild_obj = discord.Object(id=config.GUILD_ID)
            synced = await self.tree.sync(guild=guild_obj)
            print(f"Synced {len(synced)} commands to guild.")
        except Exception as e:
            print(f"Error syncing commands: {e}")


bot = Client(command_prefix="!", intents=intents)

# --- Users.json cache refresh task ---
#
# Keeps api.github's in-memory Users.json cache warm so read-only whitelist/
# cooldown pre-checks (e.g. the control panel's Reset HWID button) never
# have to make a live network call on the interaction's critical path -- so
# they can't time out or silently fail open the way the old fetch_raw_users()
# -with-a-2s-timeout check did.
#
# commit_content() (used by every write path, including commit_users() for
# redeem/edituser/reset hwid/etc.) already updates the cache immediately on
# every write the bot makes itself, so this loop only has to catch external
# changes -- e.g. someone editing Users.json by hand on GitHub, or a
# /rollback -- within USERS_CACHE_REFRESH_INTERVAL seconds. It refreshes via
# the Contents API rather than the raw.githubusercontent.com CDN precisely
# so that "within USERS_CACHE_REFRESH_INTERVAL seconds" is actually true --
# the CDN endpoint can lag well past that on its own.
USERS_CACHE_REFRESH_INTERVAL = 60  # seconds


@tasks.loop(seconds=USERS_CACHE_REFRESH_INTERVAL)
async def refresh_users_cache_task():
    try:
        await refresh_users_cache()
    except GitHubAPIError as e:
        # Leave the existing cache in place and just try again next tick --
        # stale-but-known beats throwing away the last good copy.
        print(f"Failed to refresh Users.json cache: {e}")


@refresh_users_cache_task.before_loop
async def before_refresh_users_cache_task():
    await bot.wait_until_ready()


# --- Error Handlers ---

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Unwrap CommandInvokeError/TransformerError to get at the underlying exception
    original = getattr(error, "original", error)

    # Catch transformer errors caused by bad member conversion
    if isinstance(error, app_errors.TransformerError):
        if "to Member" in str(error):
            await send_error(interaction, "That user is not in this server.")
            return

    if isinstance(error, app_commands.CheckFailure):
        await send_error(interaction, str(error))
        return

    # Catch Discord's "Embed size exceeds maximum size of 6000" HTTPException
    # (error code 50035, Invalid Form Body) so it doesn't just get printed
    # and swallowed, leaving the user with no response at all.
    if isinstance(original, discord.HTTPException) and "Embed size exceeds maximum size" in str(original):
        await send_error(
            interaction,
            "The response was too large to display (Discord limits embeds to 6,000 characters total). "
            "Try narrowing your request so it returns less data.",
        )
        return

    # Catch Discord's per-field "Must be X or fewer in length" HTTPException
    # (also error code 50035, Invalid Form Body) -- distinct from the
    # whole-embed 6000-character check above: this one fires when a single
    # field (an embed's description/title/a field value, or message content)
    # individually exceeds its own limit, e.g. /getkeys or /genkey building
    # a keys list that's short enough to pass the "under 6000 total" check
    # but still blows past a single embed description's own 4096 cap. The
    # inline-vs-file fallbacks those commands use are meant to avoid this in
    # the first place -- this is just the safety net for whatever slips
    # past that (or any other command that hits the same shape of error).
    if isinstance(original, discord.HTTPException) and "or fewer in length" in str(original):
        match = re.search(r"In ([\w.]+): Must be (\d+) or fewer in length", str(original))
        if match:
            field, limit = match.group(1), match.group(2)
            await send_error(
                interaction,
                f"That response was too long for Discord ({field} is limited to {limit} characters). "
                "Try narrowing your request so it returns less text.",
            )
        else:
            await send_error(
                interaction,
                "That response exceeded one of Discord's character limits. Try narrowing your request "
                "so it returns less text.",
            )
        return

    # Catch-all for any other Discord API errors (rate limits, malformed
    # payloads, permission issues surfaced as HTTP errors, etc.) so the user
    # always gets *some* response instead of the command silently failing.
    if isinstance(original, discord.HTTPException):
        print(f"Unhandled HTTPException: {original.status} {original.code} - {original.text}")
        try:
            await send_error(
                interaction,
                f"Something went wrong talking to Discord (HTTP {original.status}, error code {original.code}). "
                "Please try again, and let a developer know if it keeps happening.",
            )
        except Exception as e:
            print(f"Failed to notify user of HTTPException: {e}")
        return

    print(f"Unhandled error: {error}")


# on_app_command_error above only covers slash commands (it's registered on
# bot.tree). Raw gateway events like on_raw_reaction_add/on_raw_reaction_remove
# aren't slash commands, so exceptions in them (e.g. the Forbidden/"Missing
# Permissions" error from add_roles/remove_roles when the bot's role sits
# below the target role) never reach it -- they instead hit discord.py's
# default on_error, which just prints "Ignoring exception in <event>" and
# swallows it with no feedback to anyone. This override is that missing
# counterpart for raw events.
@bot.event
async def on_error(event_method, *args, **kwargs):
    exc_type, exc, tb = sys.exc_info()

    if isinstance(exc, discord.Forbidden):
        print(f"Missing permissions in {event_method}: {exc.text} (error code: {exc.code})")

        # For reaction role events specifically, the payload (first arg) tells
        # us who was affected, so we can let them know it didn't work instead
        # of leaving them thinking the role was applied/removed.
        if event_method in ("on_raw_reaction_add", "on_raw_reaction_remove") and args:
            payload = args[0]
            guild = bot.get_guild(getattr(payload, "guild_id", None))
            if guild:
                member = guild.get_member(payload.user_id)
                if member and not member.bot:
                    action = "add that role to you" if event_method == "on_raw_reaction_add" else "remove that role from you"
                    await notify_permission_error(member, action, guild.name)
        return

    # Anything else: log it the same way discord.py's default handler would,
    # so unrelated bugs are still fully visible in the console.
    print(f"Unhandled exception in {event_method}:")
    traceback.print_exception(exc_type, exc, tb)


# --- Run Bot ---

if __name__ == "__main__":
    bot.run(config.DISCORD_TOKEN)

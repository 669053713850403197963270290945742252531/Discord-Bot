"""
Entry point. Everything else in this package is a library (api/) or an
extension (commands/); this is the only file that actually constructs the
Client, wires the 17 extensions into it, and calls bot.run().

Run from the repo root with `python src/start.py` (after `pip install -r
requirements.txt` and filling in `.env`).
"""

import asyncio
import sys
import os
import re
import shutil
import signal
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
from api.github import GitHubAPIError, fetch_botstate_with_sha
from api.discord_helpers import send_error, notify_permission_error, reconcile_dms_enabled
from api.alerts import reconcile_alerts_enabled
from commands.panel import ControlPanelView
from commands.moderation import (
    reconcile_temp_bans, reconcile_channel_locks, reconcile_lockdown,
    reconcile_temp_roles, reconcile_ghostping_mode, reconcile_banned_users_cache,
)
from commands.keys import reconcile_temp_whitelists
from commands.access import reconcile_temp_access
from commands.reaction_roles import reconcile_reaction_role_panel
from commands.autorole import reconcile_autorole
from commands.warnings import reconcile_warnings_cache, reconcile_warning_config

# // Intents & Client //

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

EXTENSIONS = (
    "commands.info",
    "commands.utility",
    "commands.genpass",
    "commands.ciphers",
    "commands.encryption",
    "commands.moderation",
    "commands.afk",
    "commands.whitelist",
    "commands.keys",
    "commands.database",
    "commands.panel",
    "commands.access",
    "commands.reaction_roles",
    "commands.autorole",
    "commands.context_menus",
    "commands.qrcode",
    "commands.warnings",
    "commands.url",
)

# Guards the BotState.json reconciliation block in on_ready() so it only
# ever runs once per process -- on_ready can fire again on reconnect, and
# re-running reconciliation would double-schedule every temp ban/lock/
# access timer it already rescheduled the first time.
_botstate_reconciled = False


class Client(commands.Bot):
    async def setup_hook(self):
        # discord.py's own run() already turns Ctrl+C (SIGINT) into a
        # graceful close() -- see the `async with self:` in its run(), whose
        # __aexit__ calls close() as KeyboardInterrupt unwinds the runner.
        # SIGTERM isn't covered by that at all, though, and it's what
        # process managers actually send on a stop/restart (Render, Docker,
        # systemd -- anywhere this is actually hosted). Without this, a
        # SIGTERM just kills the process outright: close() (and, with it,
        # the presence-clearing step below) never runs, Discord gets no
        # clean disconnect to react to, and falls back to its own
        # heartbeat timeout to notice the bot's gone -- which can leave it
        # showing online with its last activity for a while after the
        # process is actually dead. Not supported on Windows
        # (add_signal_handler raises NotImplementedError there) -- SIGTERM
        # isn't meaningfully delivered to Windows processes the way it is
        # on POSIX anyway, so this only matters, and only applies, on
        # whatever POSIX host this actually ends up running on.
        try:
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGTERM, lambda: asyncio.create_task(self.close())
            )
        except NotImplementedError:
            pass

        def _count_leaf_commands(group):
            """Recursively counts leaf (directly-invokable) commands nested
            under `group`, drilling into nested subcommand groups -- e.g.
            afk.py's /afk mod, a Group living inside the top-level /afk
            group -- so those get counted as their own subcommands instead
            of the nested group swallowing them into a single opaque
            child."""
            total = 0
            for child in group.commands:
                if isinstance(child, app_commands.Group):
                    total += _count_leaf_commands(child)
                else:
                    total += 1
            return total

        def _split_top_level(top_level):
            """top_level is whatever tree.get_commands(guild=...) or
            Cog.get_app_commands() returns: a flat list of top-level chat
            -input entries, standalone commands and groups alike, with each
            group counted once regardless of how many subcommands live
            under it. Splits that into (command_count, group_count,
            subcommand_count) so "commands" always means standalone,
            directly-invokable top-level commands -- never a mixed
            command+group total -- matching /botstatus's own Commands
            Registered/Groups Registered split in commands/info.py.
            subcommand_count is every leaf command nested under any of
            those groups (see _count_leaf_commands above), which the
            "commands" figure deliberately excludes."""
            groups = [c for c in top_level if isinstance(c, app_commands.Group)]
            subcommands = sum(_count_leaf_commands(g) for g in groups)
            return len(top_level) - len(groups), len(groups), subcommands

        guild_obj = discord.Object(id=config.GUILD_ID)
        total_extensions = len(EXTENSIONS)
        loaded_extensions = 0
        # Sums of the per-extension actual/expected figures below -- these
        # are dynamically observed, rather than hardcoded constants that
        # would need to be hand-updated (and could silently drift out of
        # sync) every time a command is added/removed from any extension.
        commands_added = 0
        groups_added = 0
        subcommands_added = 0
        commands_expected = 0
        groups_expected = 0
        subcommands_expected = 0

        for extension in EXTENSIONS:
            before_commands, before_groups, before_subcommands = _split_top_level(self.tree.get_commands(guild=guild_obj))
            # Every command/group in this bot is meant to be guild-scoped
            # (see the sync comment below) -- a command or group missing its
            # own @app_commands.guilds(GUILD), or a Group missing the
            # app_commands.guilds(GUILD)(...) wrapper described in
            # ciphers.py, lands in the *global* namespace instead. That's
            # invisible in the guild-scoped counts above/below, so it was
            # slipping through as a silent count mismatch (e.g. keys.py's
            # /key group) instead of naming the culprit. Snapshot global
            # top-level names before/after this extension loads so any new
            # one can be called out immediately, by name, right here.
            before_global_names = {c.name for c in self.tree.get_commands(guild=None)}
            before_cogs = set(self.cogs)
            print(f"Loading extension: {extension}")
            try:
                await self.load_extension(extension)
            except Exception:
                print(f"FAILED to load extension: {extension}")
                traceback.print_exc()
                continue
            loaded_extensions += 1
            after_global_names = {c.name for c in self.tree.get_commands(guild=None)}
            new_global_names = after_global_names - before_global_names
            if new_global_names:
                print(
                    f"ERROR: {extension} registered {sorted(new_global_names)} "
                    f"GLOBALLY instead of to guild {config.GUILD_ID} -- missing "
                    f"@app_commands.guilds(GUILD), or (for a Group) missing the "
                    f"app_commands.guilds(GUILD)(...) wrapper. This bot has no "
                    f"global commands by design; the purge below will remove "
                    f"these from Discord, but the source needs fixing too."
                )
            after_commands, after_groups, after_subcommands = _split_top_level(self.tree.get_commands(guild=guild_obj))
            added_commands = after_commands - before_commands
            added_groups = after_groups - before_groups
            added_subcommands = after_subcommands - before_subcommands
            commands_added += added_commands
            groups_added += added_groups
            subcommands_added += added_subcommands

            # How many commands/groups this extension's *source* actually
            # defines, independent of what landed in the tree above -- so a
            # mismatch (a name collision clobbering an earlier extension's
            # command, a Discord-side platform limit like context_menus.py's
            # 5-per-guild USER-command cap, etc.) is visible per-extension
            # instead of only surfacing in the final tally below.
            new_cog_name = next(iter(set(self.cogs) - before_cogs), None)
            if new_cog_name is not None:
                # Cog-based extension (every one of these except
                # context_menus.py) -- Cog.get_app_commands() returns
                # exactly what that cog's source defines, in the same
                # top-level-only shape as tree.get_commands() above.
                expected_commands, expected_groups, expected_subcommands = _split_top_level(self.get_cog(new_cog_name).get_app_commands())
            else:
                # No Cog was added -- e.g. commands.context_menus, which
                # (per its own module docstring) hands its ContextMenu
                # commands to the tree directly in setup() instead of via
                # a Cog. Fall back to counting top-level app-command
                # objects defined directly in the module's own namespace.
                module_vars = vars(sys.modules[extension]).values() if extension in sys.modules else ()
                module_commands = [
                    obj for obj in module_vars
                    if isinstance(obj, (app_commands.Command, app_commands.ContextMenu, app_commands.Group))
                ]
                expected_commands, expected_groups, expected_subcommands = _split_top_level(module_commands)

            commands_expected += expected_commands
            groups_expected += expected_groups
            subcommands_expected += expected_subcommands
            # Builds the "(...)" segment out of only the categories this
            # extension actually has -- by expectation or by what landed in
            # the tree -- rather than always listing commands/groups/
            # subcommands regardless. A group-only cog like qrcode.py no
            # longer prints a pointless "0/0 commands loaded" for the
            # standalone-command slot it was never going to fill, and
            # whichever groups it does have now report how many of their
            # subcommands loaded (e.g. qrcode.py's generate/decode/help,
            # or a nested group like afk.py's /afk mod) instead of the
            # group count alone leaving that invisible.
            segments = []
            if added_commands or expected_commands:
                segments.append(f"{added_commands}/{expected_commands} commands loaded")
            if added_groups or expected_groups:
                segments.append(f"{added_groups}/{expected_groups} groups loaded")
            if added_subcommands or expected_subcommands:
                segments.append(f"{added_subcommands}/{expected_subcommands} subcommands loaded")
            if not segments:
                # Nothing registered and nothing expected either -- flag it
                # plainly instead of printing an empty, confusing-looking
                # "()" (e.g. a listener-only extension with no app
                # commands at all).
                segments.append("no commands registered")
            print(f"Loaded extension:  {extension} ({', '.join(segments)})")

        # All application commands are guild-scoped for this bot.
        # Synchronize only the configured guild so the command signatures
        # used by Discord exactly match the definitions loaded above.
        try:
            synced_guild = await self.tree.sync(guild=guild_obj)
            print(f"Synced {len(synced_guild)} guild commands to {config.GUILD_ID}.")
        except Exception as e:
            print(f"Error syncing guild commands: {e}")

        # Enforce the "no global commands, ever" design invariant every boot,
        # rather than relying on every extension author remembering the
        # @app_commands.guilds(...) decorator. If the ERROR check above (or
        # any future slip like it) let a command register globally, this
        # clears the local tree's global command list and syncs that empty
        # list to Discord -- which deletes any global commands Discord has on
        # record for this application, including ones left behind by an
        # older version of the bot (e.g. from a bare tree.sync() call that no
        # longer exists in this file). Cheap and idempotent when there's
        # nothing to clear, so it's safe to run unconditionally on every
        # startup rather than as a one-off manual step.
        try:
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        except Exception as e:
            print(f"Error purging global commands: {e}")

        registered_commands, registered_groups, registered_subcommands = _split_top_level(self.tree.get_commands(guild=guild_obj))
        print(
            f"All {loaded_extensions}/{total_extensions} extensions loaded, "
            f"{registered_groups}/{groups_expected} groups, "
            f"{registered_subcommands}/{subcommands_expected} subcommands, and "
            f"{registered_commands}/{commands_expected} commands registered."
        )
        if (
            registered_commands != commands_added or registered_groups != groups_added
            or registered_subcommands != subcommands_added
            or registered_commands != commands_expected or registered_groups != groups_expected
            or registered_subcommands != subcommands_expected
        ):
            # The first pair can only differ if some extension's commands got
            # clobbered by a same-named command/group from a later extension
            # (before/after would show 0 added for the second one, but the
            # first one's slot in the tree was silently overwritten rather
            # than net-new). The second pair differs whenever what actually
            # registered doesn't match what an extension's source defines --
            # e.g. a Discord-side platform limit (like context_menus.py's
            # 5-per-guild USER-command cap) silently dropped some commands.
            # Either way it means something's worth a closer look above.
            print(
                "Warning: registered command/group counts don't match the "
                "per-extension additions/expectations -- check for duplicate "
                "command names across extensions, or a platform limit "
                "silently dropping commands."
            )

    async def close(self):
        """Every path that ends up shutting the bot down -- Ctrl+C (handled
        by discord.py's own run(), see the SIGTERM comment in setup_hook
        above), the SIGTERM handler registered there, or a direct
        bot.close() call -- funnels through here, since discord.py calls
        this itself either way. Clears the presence *before* the gateway
        connection actually drops, instead of leaving whatever
        rotate_presence_task last set (a stale "Watching X") displayed
        until Discord's own heartbeat timeout notices the client's gone.

        Stops both background loops first -- specifically so a stray
        rotate_presence_task tick can't land in the gap between clearing
        the presence and the connection actually closing and re-set an
        activity right as the bot's shutting down.

        Doesn't help with a hard kill (kill -9, Windows' TerminateProcess,
        an IDE's "Stop" button that doesn't deliver a real signal) --
        nothing running in-process can, since the OS ends the process
        before any of this code gets a chance to run. This only covers
        shutdowns the process actually gets a chance to react to.
        """
        if rotate_presence_task.is_running():
            rotate_presence_task.cancel()

        if not self.is_closed():
            try:
                await self.change_presence(status=discord.Status.invisible, activity=None)
            except Exception as e:
                # Best-effort -- e.g. the gateway connection is already
                # unhealthy. Shouldn't block the actual close() below.
                print(f"Failed to clear presence before shutdown: {e}")

        await super().close()

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")

        # Guarded with is_running() since on_ready can fire again on
        # reconnect, and tasks.loop.start() raises if it's already going.
        # start() runs the loop body immediately (not after the first
        # interval), so this also sets the very first status -- no separate
        # change_presence() call needed here.
        if not rotate_presence_task.is_running():
            rotate_presence_task.start()

        # Re-registers the /createpanel control panel's button handlers so
        # they keep responding after a bot restart. This does NOT resend the
        # message -- the panel embed posted by /createpanel stays put in
        # #panel; this just reconnects its (fixed custom_id) buttons to a
        # live view again, since ControlPanelView(timeout=None) instances
        # don't otherwise survive a process restart.
        self.add_view(ControlPanelView())


        # Reads storage/BotState.json back and reschedules every timer/
        # pointer that only ever lived in process memory before -- temp
        # ban auto-unbans, an in-progress lockdown/its auto-lift, per-
        # channel lock auto-unlocks, temp whitelist expiry notifications
        # (Users.json's own read-back), temp Bot Access auto-removals, the
        # reaction-role panel's message pointer, temp role auto-removals,
        # ghost ping detection mode, the autorole toggle+role, the
        # /togglealerts whitelist/moderation mute switches, and the
        # /warnings autocomplete cache. Without this, a
        # restart mid-timer either makes a "temp" action silently
        # permanent, or silently stops a mechanism that was already
        # correctly persisted elsewhere. Guarded the same way as
        # startup reconciliation above should only ever run once per process.
        global _botstate_reconciled
        if not _botstate_reconciled:
            _botstate_reconciled = True

            try:
                botstate, _sha = await fetch_botstate_with_sha()
            except GitHubAPIError as e:
                print(f"Failed to fetch BotState.json for startup reconciliation: {e}")
                botstate = None

            await reconcile_temp_bans(self, botstate)
            await reconcile_banned_users_cache(self, botstate)
            await reconcile_lockdown(self, botstate)
            await reconcile_channel_locks(self, botstate)
            await reconcile_temp_whitelists(self)
            await reconcile_temp_access(self, botstate)
            await reconcile_reaction_role_panel(self, botstate)
            await reconcile_temp_roles(self, botstate)
            await reconcile_ghostping_mode(self, botstate)
            await reconcile_autorole(self, botstate)
            await reconcile_alerts_enabled(self, botstate)
            await reconcile_dms_enabled(self, botstate)
            await reconcile_warnings_cache(self, botstate)
            await reconcile_warning_config(self, botstate)




bot = Client(command_prefix="!", intents=intents)

# --- Rotating status ---
_PRESENCE_ROTATION_INTERVAL = 30
_presence_index = 0


def _build_presence_activities(guild_obj: discord.Object) -> list:
    guild = bot.get_guild(config.GUILD_ID)
    member_count = guild.member_count if guild else None
    command_count = len(bot.tree.get_commands(guild=guild_obj))

    activities = []
    try:
        # The presence task is deliberately lightweight; the license count is
        # fetched from Supabase each rotation rather than maintained in an
        # in-memory user cache.
        activities.append(discord.Activity(type=discord.ActivityType.watching, name="the whitelist"))
    except Exception:
        pass
    if member_count is not None:
        label = "member" if member_count == 1 else "members"
        activities.append(discord.Activity(type=discord.ActivityType.watching, name=f"over {member_count} {label}"))
    activities.append(discord.Activity(type=discord.ActivityType.listening, name=f"{command_count} slash commands"))
    return activities


@tasks.loop(seconds=_PRESENCE_ROTATION_INTERVAL)
async def rotate_presence_task():
    global _presence_index
    guild_obj = discord.Object(id=config.GUILD_ID)
    activities = _build_presence_activities(guild_obj)
    if not activities:
        return
    await bot.change_presence(activity=activities[_presence_index % len(activities)])
    _presence_index += 1


@rotate_presence_task.before_loop
async def before_rotate_presence_task():
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
    # individually exceeds its own limit, e.g. /key fetch or /key generate building
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
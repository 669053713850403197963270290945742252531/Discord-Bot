from typing import Any, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, send_success, send_error
from api.github import GitHubAPIError, fetch_botstate_with_sha, update_botstate

GUILD = discord.Object(id=config.GUILD_ID)

# =========================================================================
# /autorole -- /autorole set (which role) and /autorole toggle (whether it's
# currently applied to new members).
#
# Both pieces of state are kept in-memory for fast access from
# on_member_join below, and mirrored to BotState.json's "autorole" key
# ({"enabled": ..., "role_id": ...}) on every /autorole set or /autorole
# toggle so a restart resumes whatever was configured instead of silently
# clearing it -- reconcile_autorole() below reads it back in on_ready.
# =========================================================================

_autorole_enabled: bool = False
_autorole_role_id: Optional[int] = None


async def _persist_autorole_state(message: str):
    """Mirrors the in-memory `_autorole_enabled`/`_autorole_role_id` to
    BotState.json. Best-effort -- logged rather than raised, since the
    setting itself has already taken effect in-process by the time this
    runs; a failure here only means it would fall back to disabled/unset on
    the next restart instead of resuming where it left off."""
    def _mutate(state):
        state["autorole"] = {
            "enabled": _autorole_enabled,
            "role_id": str(_autorole_role_id) if _autorole_role_id is not None else None,
        }
        return state
    try:
        await update_botstate(_mutate, message)
    except GitHubAPIError as e:
        print(f"Failed to persist autorole state to BotState.json: {e}")


async def reconcile_autorole(bot: commands.Bot, state: Optional[Dict[str, Any]] = None):
    """Called once from on_ready: restores `_autorole_enabled` and
    `_autorole_role_id` from BotState.json, so a restart resumes whatever
    was configured via /autorole set + /autorole toggle instead of silently
    reverting to disabled/unset.

    `state` lets a caller that's already fetched BotState.json hand it over
    directly instead of this making its own redundant fetch -- see
    commands.moderation.reconcile_temp_bans() for the full reasoning."""
    global _autorole_enabled, _autorole_role_id
    if state is None:
        try:
            state, _sha = await fetch_botstate_with_sha()
        except GitHubAPIError as e:
            print(f"Failed to fetch BotState.json for autorole reconciliation: {e}")
            return

    saved = state.get("autorole") or {}
    _autorole_enabled = bool(saved.get("enabled", False))
    raw_role_id = saved.get("role_id")
    try:
        _autorole_role_id = int(raw_role_id) if raw_role_id else None
    except (TypeError, ValueError):
        _autorole_role_id = None

    if _autorole_enabled or _autorole_role_id is not None:
        print(f"Reconciled autorole from BotState.json (enabled={_autorole_enabled}, role_id={_autorole_role_id}).")


def _autorole_assignability_warning(guild: discord.Guild, role: discord.Role) -> Optional[str]:
    """Returns a human-readable warning if the bot currently couldn't
    actually assign `role` to anyone, so /autorole set can flag that
    immediately instead of only failing silently at the next
    on_member_join. Doesn't block the set -- the bot's permissions or role
    position might still change before the next member joins -- it's just
    an early heads-up."""
    me = guild.me
    if not me.guild_permissions.manage_roles:
        return "I don't currently have the **Manage Roles** permission, so I won't be able to assign this role yet."
    if role >= me.top_role:
        return f"My top role currently sits at or below {role.mention}, so I won't be able to assign it yet -- move my role above it in Server Settings > Roles."
    return None


class Autorole(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # /autorole set and /autorole toggle live as subcommands under a single
    # /autorole group -- mirrors the /cipher, /afk, /reactionrole group
    # pattern. The guild restriction lives on the group itself so both
    # subcommands inherit it.
    autorole_group = app_commands.guilds(GUILD)(
        app_commands.Group(
            name="autorole",
            description="Configure the role automatically assigned to new members.",
        )
    )

    @autorole_group.command(name="set", description="Sets the role automatically assigned to new members.")
    @app_commands.describe(role="Role to auto-assign on join")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def autorole_set(self, interaction: discord.Interaction, role: discord.Role):
        global _autorole_role_id

        # Neither of these is ever assignable via the API regardless of
        # role position -- @everyone because every member already has it
        # (there's nothing to "assign"), managed roles because they belong
        # to a bot/integration/booster tier and Discord rejects manually
        # touching them.
        if role.is_default():
            return await send_error(interaction, "Can't set @everyone as the autorole -- every member already has it.")
        if role.managed:
            return await send_error(interaction, f"{role.mention} is managed by a bot/integration and can't be manually assigned.")

        _autorole_role_id = role.id
        await _persist_autorole_state(f"Autorole role set to {role} ({role.id}) by {interaction.user} ({interaction.user.id})")

        fields = [("Currently", "Enabled" if _autorole_enabled else "Disabled -- run `/autorole toggle` to turn it on", False)]
        warning = _autorole_assignability_warning(interaction.guild, role)
        if warning:
            fields.append(("Warning", warning, False))

        await send_success(interaction, f"New members will now be given {role.mention}.", fields=fields)

    @autorole_group.command(name="toggle", description="Toggles whether new members automatically receive the configured autorole.")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def autorole_toggle(self, interaction: discord.Interaction):
        global _autorole_enabled
        _autorole_enabled = not _autorole_enabled
        await _persist_autorole_state(f"Autorole toggled {'enabled' if _autorole_enabled else 'disabled'} by {interaction.user} ({interaction.user.id})")

        if not _autorole_enabled:
            await send_success(interaction, "Autorole is now **disabled** -- new members won't be given a role automatically.")
            return

        if _autorole_role_id is None:
            await send_success(
                interaction,
                "Autorole is now **enabled**, but no role has been set yet.",
                fields=[("Note", "Run `/autorole set` to pick a role -- until then, this won't do anything.", False)],
            )
            return

        role = interaction.guild.get_role(_autorole_role_id)
        if role is None:
            await send_success(
                interaction,
                "Autorole is now **enabled**, but the previously set role no longer exists.",
                fields=[("Note", "Run `/autorole set` again to pick a new one.", False)],
            )
            return

        fields = []
        warning = _autorole_assignability_warning(interaction.guild, role)
        if warning:
            fields.append(("Warning", warning, False))

        await send_success(interaction, f"Autorole is now **enabled** -- new members will be given {role.mention}.", fields=fields or None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not _autorole_enabled or _autorole_role_id is None:
            return
        if member.guild.id != config.GUILD_ID:
            return
        # Other bots joining (e.g. a music bot, a moderation bot) don't get
        # the member autorole -- same convention afk.py and
        # reaction_roles.py already use for skipping bot accounts.
        if member.bot:
            return

        role = member.guild.get_role(_autorole_role_id)
        if role is None:
            print(f"Autorole role {_autorole_role_id} no longer exists in {member.guild.name} -- skipping {member}.")
            return

        try:
            await member.add_roles(role, reason="Auto-assigned via /autorole")
        except discord.Forbidden:
            print(f"Missing permissions to auto-assign {role} to {member} in {member.guild.name}.")
        except discord.HTTPException as e:
            print(f"Failed to auto-assign {role} to {member}: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Autorole(bot))

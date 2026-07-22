"""
Context-menu ("right-click a user") commands: 15 total. None of these
duplicate any logic -- each one either forwards straight to an existing
slash command's _xxx_impl() (when the right-clicked user is the only input
needed), or opens a small Modal to collect the extra option(s) that slash
command takes (reason, duration, hwid, minutes, ...) and *then* forwards to
that same _xxx_impl(). That way validation, DMs, embeds, etc. all still
live in exactly one place (moderation.py / whitelist.py / keys_hwid.py /
access.py) and can't drift between the slash command and its context-menu
twin.

The one exception is "Whitelist User", which reuses WhitelistModal directly
from whitelist.py instead of routing through an _impl function, since
/whitelist's entire body already lives inside that modal's on_submit().

Unlike every other file in this package, this one isn't a Cog -- app_commands
.ContextMenu isn't a Command/Group subclass, so Cog scanning never picks up
@app_commands.context_menu()-decorated methods for auto-registration the way
it does for @app_commands.command(). Instead, each context menu is built as
a standalone module-level command and handed to the tree explicitly in
setup(), which is just as valid an extension entry point as returning a Cog.
"""

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Modal, TextInput, Label, Checkbox

from api import config
from api.discord_helpers import has_role, is_in_guild, send_error
from commands.moderation import _ban_impl, _kick_impl, _mute_impl, _unmute_impl
from commands.whitelist import (
    WhitelistModal, _edituser_impl, _unwhitelist_impl, _fetchuser_impl, _clearnotes_impl,
)
from commands.keys_hwid import (
    _checktemp_impl, _forceresethwid_impl, _resethwidcooldown_impl, _tempwhitelist_impl,
)
from commands.access import _toggleaccess_impl, _tempaccess_impl

GUILD = discord.Object(id=config.GUILD_ID)


# // Ban User //

class BanContextModal(Modal):
    """Reason + optional duration + a Preserve Messages checkbox for the Ban
    User context menu command, mirroring /ban's `reason`/`duration`/
    `preserve_messages` options. Uses a Components V2 Checkbox (rather than
    a text field) for preserve_messages since it's a plain on/off toggle --
    defaults checked (messages preserved), matching /ban's own default."""

    reason = Label(
        text="Reason",
        component=TextInput(required=False, max_length=200, placeholder="None"),
    )
    duration = Label(
        text="Duration in minutes",
        description="Leave blank for a permanent ban.",
        component=TextInput(required=False, max_length=10, placeholder="e.g. 60"),
    )
    preserve_messages = Label(
        text="Preserve Messages",
        description="Checked = keep the user's messages. Unchecked = delete their recent messages.",
        component=Checkbox(default=True),
    )

    def __init__(self, target: discord.Member):
        super().__init__(title=f"Ban {target.display_name}"[:45])
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):
        reason = (self.reason.component.value or "").strip() or "None"
        duration_raw = (self.duration.component.value or "").strip()
        duration = None
        if duration_raw:
            if not duration_raw.isdigit() or int(duration_raw) <= 0:
                return await send_error(interaction, "Duration must be a positive whole number of minutes.")
            duration = int(duration_raw)
        preserve_messages = self.preserve_messages.component.value
        await _ban_impl(interaction, self.target, reason=reason, duration=duration, preserve_messages=preserve_messages)


@app_commands.context_menu(name="Ban User")
@app_commands.guilds(GUILD)
@has_role(config.REQUIRED_ROLE_ID)
@is_in_guild(config.GUILD_ID)
async def ctx_ban_user(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.send_modal(BanContextModal(target))


# // Kick User //

class KickContextModal(Modal):
    def __init__(self, target: discord.Member):
        super().__init__(title=f"Kick {target.display_name}"[:45])
        self.target = target
        self.reason = TextInput(label="Reason", required=False, max_length=200, placeholder="Unspecified")
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        reason = (self.reason.value or "").strip() or "Unspecified"
        await _kick_impl(interaction, self.target, reason)


@app_commands.context_menu(name="Kick User")
@app_commands.guilds(GUILD)
@has_role(config.REQUIRED_ROLE_ID)
@is_in_guild(config.GUILD_ID)
async def ctx_kick_user(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.send_modal(KickContextModal(target))


# // Mute User //

class MuteContextModal(Modal):
    def __init__(self, target: discord.Member):
        super().__init__(title=f"Mute {target.display_name}"[:45])
        self.target = target
        self.reason = TextInput(label="Reason", required=False, max_length=200, placeholder="Unspecified")
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        reason = (self.reason.value or "").strip() or "Unspecified"
        await _mute_impl(interaction, self.target, reason)


@app_commands.context_menu(name="Mute User")
@app_commands.guilds(GUILD)
@has_role(config.REQUIRED_ROLE_ID)
@is_in_guild(config.GUILD_ID)
async def ctx_mute_user(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.send_modal(MuteContextModal(target))


# // Unmute User //

@app_commands.context_menu(name="Unmute User")
@app_commands.guilds(GUILD)
@has_role(config.REQUIRED_ROLE_ID)
@is_in_guild(config.GUILD_ID)
async def ctx_unmute_user(interaction: discord.Interaction, target: discord.Member):
    await _unmute_impl(interaction, target)


# // Whitelist User //

@app_commands.context_menu(name="Whitelist User")
@app_commands.guilds(GUILD)
@has_role(config.REQUIRED_ROLE_ID)
@is_in_guild(config.GUILD_ID)
async def ctx_whitelist_user(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.send_modal(WhitelistModal(target=target))


# // Edit User //

@app_commands.context_menu(name="Edit User")
@app_commands.guilds(GUILD)
@has_role(config.REQUIRED_ROLE_ID)
@is_in_guild(config.GUILD_ID)
async def ctx_edit_user(interaction: discord.Interaction, target: discord.Member):
    # _edituser_impl already just fetches the entry and opens a modal itself,
    # so there's no extra input to collect here first.
    await _edituser_impl(interaction, target)


# // Unwhitelist User //

@app_commands.context_menu(name="Unwhitelist User")
@app_commands.guilds(GUILD)
@has_role(config.REQUIRED_ROLE_ID)
@is_in_guild(config.GUILD_ID)
async def ctx_unwhitelist_user(interaction: discord.Interaction, target: discord.Member):
    await _unwhitelist_impl(interaction, target)


# // Fetch User Info //

@app_commands.context_menu(name="Fetch User Info")
@app_commands.guilds(GUILD)
@has_role(config.REQUIRED_ROLE_ID)
@is_in_guild(config.GUILD_ID)
async def ctx_fetch_user(interaction: discord.Interaction, target: discord.Member):
    await _fetchuser_impl(interaction, target)


# // Check Temp Whitelist //

@app_commands.context_menu(name="Check Temp Whitelist")
@app_commands.guilds(GUILD)
@has_role(config.REQUIRED_ROLE_ID)
@is_in_guild(config.GUILD_ID)
async def ctx_check_temp(interaction: discord.Interaction, target: discord.Member):
    await _checktemp_impl(interaction, target)


# // Clear User Notes //

@app_commands.context_menu(name="Clear User Notes")
@app_commands.guilds(GUILD)
@has_role(config.REQUIRED_ROLE_ID)
@is_in_guild(config.GUILD_ID)
async def ctx_clear_notes(interaction: discord.Interaction, target: discord.Member):
    await _clearnotes_impl(interaction, target)


# // Force Reset HWID //

class ForceResetHwidContextModal(Modal):
    def __init__(self, target: discord.Member):
        super().__init__(title=f"Reset HWID: {target.display_name}"[:45])
        self.target = target
        self.hwid = TextInput(label="New HWID (SHA-256, 64 hex chars)", max_length=100, placeholder="64-character hex string")
        self.add_item(self.hwid)

    async def on_submit(self, interaction: discord.Interaction):
        # _forceresethwid_impl already validates the HWID format and reports
        # a clear error itself, so it's passed straight through.
        await _forceresethwid_impl(interaction, self.target, self.hwid.value.strip())


@app_commands.context_menu(name="Force Reset HWID")
@app_commands.guilds(GUILD)
@has_role(config.REQUIRED_ROLE_ID)
@is_in_guild(config.GUILD_ID)
async def ctx_force_reset_hwid(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.send_modal(ForceResetHwidContextModal(target))


# // Reset HWID Cooldown //

@app_commands.context_menu(name="Reset HWID Cooldown")
@app_commands.guilds(GUILD)
@has_role(config.REQUIRED_ROLE_ID)
@is_in_guild(config.GUILD_ID)
async def ctx_reset_hwid_cooldown(interaction: discord.Interaction, target: discord.Member):
    await _resethwidcooldown_impl(interaction, target)


# // Toggle Bot Access //

@app_commands.context_menu(name="Toggle Bot Access")
@app_commands.guilds(GUILD)
@has_role(config.REQUIRED_ROLE_ID)
@is_in_guild(config.GUILD_ID)
async def ctx_toggle_access(interaction: discord.Interaction, target: discord.Member):
    await _toggleaccess_impl(interaction, target)


# // Grant Temp Bot Access //

class TempAccessContextModal(Modal):
    def __init__(self, target: discord.Member):
        super().__init__(title=f"Bot Access: {target.display_name}"[:45])
        self.target = target
        self.minutes = TextInput(label="Duration in minutes", max_length=10, placeholder="e.g. 30")
        self.add_item(self.minutes)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.minutes.value.strip()
        if not raw.isdigit() or int(raw) <= 0:
            return await send_error(interaction, "Duration must be a positive whole number of minutes.")
        await _tempaccess_impl(interaction, self.target, int(raw))


@app_commands.context_menu(name="Grant Temp Bot Access")
@app_commands.guilds(GUILD)
@has_role(config.REQUIRED_ROLE_ID)
@is_in_guild(config.GUILD_ID)
async def ctx_temp_access(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.send_modal(TempAccessContextModal(target))


# // Temp Whitelist User //

class TempWhitelistContextModal(Modal):
    def __init__(self, target: discord.Member):
        super().__init__(title=f"Temp Whitelist: {target.display_name}"[:45])
        self.target = target
        self.hwid = TextInput(label="HWID (SHA-256, 64 hex chars)", max_length=100, placeholder="64-character hex string")
        self.minutes = TextInput(label="Duration in minutes", max_length=10, placeholder="e.g. 60")
        self.add_item(self.hwid)
        self.add_item(self.minutes)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.minutes.value.strip()
        if not raw.isdigit() or int(raw) <= 0:
            return await send_error(interaction, "Duration must be a positive whole number of minutes.")
        await _tempwhitelist_impl(interaction, self.target, self.hwid.value.strip(), int(raw))


@app_commands.context_menu(name="Temp Whitelist User")
@app_commands.guilds(GUILD)
@has_role(config.REQUIRED_ROLE_ID)
@is_in_guild(config.GUILD_ID)
async def ctx_temp_whitelist(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.send_modal(TempWhitelistContextModal(target))


# All 15, in the same order as they're defined above.
_CONTEXT_MENUS = (
    ctx_ban_user, ctx_kick_user, ctx_mute_user, ctx_unmute_user,
    ctx_whitelist_user, ctx_edit_user, ctx_unwhitelist_user, ctx_fetch_user,
    ctx_check_temp, ctx_clear_notes, ctx_force_reset_hwid, ctx_reset_hwid_cooldown,
    ctx_toggle_access, ctx_temp_access, ctx_temp_whitelist,
)


async def setup(bot: commands.Bot):
    # No Cog here -- see module docstring for why these are added to the
    # tree directly instead of via bot.add_cog().
    #
    # IMPORTANT: Discord caps USER-type context menu commands at 5 per guild
    # (and separately, 5 global). All 15 of the menus above are guild-scoped
    # USER commands, so only the first 5 in _CONTEXT_MENUS will actually
    # register -- the rest raise CommandLimitReached. That's a hard Discord
    # platform limit, not a bug here, and it isn't something this file can
    # route around on its own (it needs a product decision: which 5 stay as
    # real context menus, whether some become global instead of guild-scoped
    # to get 5 more, or whether several get folded into one "hub" menu that
    # opens a follow-up select). Until that's decided, failures are caught
    # and logged individually so one platform limit doesn't take the whole
    # bot down on startup.
    for context_menu in _CONTEXT_MENUS:
        try:
            bot.tree.add_command(context_menu)
        except app_commands.CommandLimitReached as e:
            print(f"Skipped context menu {context_menu.name!r}: {e}")

import io
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Modal, TextInput, Label, LayoutView, Container, TextDisplay, ActionRow, Button, View, File

from api import config
from api.discord_helpers import has_role, is_in_guild, send_success, send_error, build_embed, notify_user
from api.github import (
    GitHubAPIError, fetch_users_with_sha, commit_users,
    fetch_permitted_keys_with_sha, commit_permitted_keys, remove_permitted_key,
    fetch_stored_script, inject_script_key,
    fetch_stored_script_with_sha, commit_stored_script, validate_stored_script,
    get_cached_users,
)
from api.users import find_user_by_discord_id, find_user_by_hwid, find_user_by_key, build_user_entry, remove_user_by_discord_id, revoke_buyer_role
from api.keys import is_valid_hwid
from api.time_utils import format_join_date, humanize_timeleft, hwid_reset_cooldown_remaining

GUILD = discord.Object(id=config.GUILD_ID)

CONTROL_PANEL_TITLE = "### Control Panel"
CONTROL_PANEL_DESCRIPTION = "Click the buttons below to redeem your key, get the script, or get your role."

# Fixed custom_ids so Discord routes button presses back to these handlers
# even after a bot restart (see ControlPanelView + start.py's on_ready).
PANEL_REDEEM_KEY_ID = "panel_redeem_key"
PANEL_GET_SCRIPT_ID = "panel_get_script"
PANEL_GET_ROLE_ID = "panel_get_role"
PANEL_RESET_HWID_ID = "panel_reset_hwid"
PANEL_GET_INFO_ID = "panel_get_info"


async def send_redeem_alert(bot: commands.Bot, embed: discord.Embed, view: Optional[View] = None):
    """Best-effort delivery to the Redeem Alerts channel for the control
    panel's Redeem Key flow (successful redemptions + HWID-breach attempts).
    A missing channel or delivery failure here is logged and swallowed
    rather than surfaced to the redeeming user -- their redemption already
    succeeded or failed on its own, independent of whether staff got
    notified about it."""
    channel = bot.get_channel(config.REDEEM_ALERTS_CHANNEL_ID)
    if not channel:
        print(f"Redeem Alerts channel not found (REDEEM_ALERTS_CHANNEL_ID={config.REDEEM_ALERTS_CHANNEL_ID}).")
        return

    try:
        if view is not None:
            await channel.send(embed=embed, view=view)
        else:
            await channel.send(embed=embed)
    except Exception as e:
        print(f"Failed to send alert to Redeem Alerts channel: {e}")


class HWIDBreachAlertView(View):
    """Attached to the "Potential Breach" alert posted when a Redeem Key
    attempt reuses an HWID that's already whitelisted under a different
    Discord account. The button bans BOTH accounts involved -- the
    attempting redeemer (for trying to use someone else's HWID) and the
    existing owner of that HWID (presumed to have shared/leaked their
    access) -- and unwhitelists the owner's Users.json entry. Only the
    owner's entry gets removed since the attempting redeemer never
    actually gets whitelisted in this scenario.

    Unlike ControlPanelView, this isn't re-registered on restart, so the
    button only stays clickable until the next bot restart -- past that,
    fall back to /unwhitelist + /ban manually using the IDs in the embed."""

    def __init__(self, owner_discord_id: str, owner_identifier: str, hwid: str, attempting_discord_id: str):
        super().__init__(timeout=None)
        self.owner_discord_id = owner_discord_id
        self.owner_identifier = owner_identifier
        self.hwid = hwid
        self.attempting_discord_id = attempting_discord_id

        button = Button(label="❌ Unwhitelist & Ban Both", style=discord.ButtonStyle.danger, custom_id="breach_unwhitelist_ban")
        button.callback = self.unwhitelist_and_ban
        self.add_item(button)

    async def _ban(self, interaction: discord.Interaction, discord_id: str, reason: str) -> str:
        """Bans a single Discord ID (member or not), DMs them a best-effort
        notice, and returns a one-line result string for the summary."""
        try:
            user = await interaction.client.fetch_user(int(discord_id))
        except (discord.NotFound, ValueError):
            user = None

        if user:
            try:
                await notify_user(user, "banned", interaction.user, reason, interaction.guild.name)
            except Exception as e:
                print(f"Failed to DM {user}: {e}")

        try:
            await interaction.guild.ban(discord.Object(id=int(discord_id)), reason=reason)
        except discord.Forbidden:
            return f"❌ Failed to ban `{discord_id}` (missing permissions)"
        except discord.HTTPException as e:
            return f"❌ Failed to ban `{discord_id}`: {e}"

        return f"✅ Banned {user.mention if user else f'`{discord_id}`'}"

    async def unwhitelist_and_ban(self, interaction: discord.Interaction):
        if config.REQUIRED_ROLE_ID not in [role.id for role in interaction.user.roles]:
            return await send_error(interaction, "You do not have the required permissions to do this.")

        await interaction.response.defer(ephemeral=True)

        results = []

        # Unwhitelist the owner's entry -- the attempting redeemer never had
        # one to begin with, since the duplicate-HWID check blocks the
        # redemption before it's committed.
        try:
            users, sha = await fetch_users_with_sha()
            filtered, removed = remove_user_by_discord_id(users, self.owner_discord_id)
            if removed:
                await commit_users(filtered, sha, f"Unwhitelisted (HWID breach): {self.owner_identifier} ({self.owner_discord_id})")
                await revoke_buyer_role(interaction.guild, self.owner_discord_id)
                results.append(f"✅ Unwhitelisted **{self.owner_identifier}**")
            else:
                results.append(f"⚠️ No whitelist entry found for **{self.owner_identifier}** (may already be removed)")
        except GitHubAPIError as e:
            results.append(f"❌ Failed to unwhitelist **{self.owner_identifier}**: {e}")

        owner_reason = f"HWID breach -- HWID shared/leaked, actioned by {interaction.user}"
        attempting_reason = f"HWID breach -- attempted redemption using another user's HWID, actioned by {interaction.user}"

        results.append(await self._ban(interaction, self.owner_discord_id, owner_reason))
        if self.attempting_discord_id != self.owner_discord_id:
            results.append(await self._ban(interaction, self.attempting_discord_id, attempting_reason))

        # Mark the alert as handled so it can't be actioned twice, and
        # record who resolved it directly on the original embed.
        for item in self.children:
            item.disabled = True
        self.children[0].label = "Resolved"

        try:
            resolved_embed = interaction.message.embeds[0]
            resolved_embed.color = discord.Color.dark_grey()
            resolved_embed.add_field(name="Resolved By", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
            await interaction.message.edit(embed=resolved_embed, view=self)
        except Exception as e:
            print(f"Failed to update breach alert message: {e}")

        await send_success(interaction, "\n".join(results), title="Breach Action Complete")


class RedeemKeyModal(Modal, title="Redeem Key"):
    """Self-service equivalent of /register + /whitelist: the user supplies
    their key and pre-hashed HWID, the key is checked against
    permittedKeys.txt, and -- if valid -- a new Users.json entry is
    committed for them directly, with no moderator step in between. On
    success, the redeemed key is also removed from permittedKeys.txt so it
    can't be used again."""

    key = Label(
        text="Key",
        description="The key you were given to redeem.",
        component=TextInput(placeholder="Enter your key", max_length=100),
    )
    hwid = Label(
        text="HWID",
        description="Pre-hashed HWID in SHA-256 (64 hex characters). Run /hwidhelp for help getting yours.",
        component=TextInput(placeholder="64-character hex string", min_length=64, max_length=64),
    )

    async def on_submit(self, interaction: discord.Interaction):
        key = self.key.component.value.strip()
        hwid = self.hwid.component.value.strip()

        if not is_valid_hwid(hwid):
            return await send_error(interaction, "Invalid HWID format. Must be 64 hex characters (SHA-256).")

        await interaction.response.defer(ephemeral=True)

        try:
            permitted_keys, keys_sha = await fetch_permitted_keys_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        if key not in permitted_keys:
            return await send_error(interaction, "That key is invalid. Please double-check it and try again.")

        try:
            users, sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        discord_id_str = str(interaction.user.id)

        existing = find_user_by_discord_id(users, discord_id_str)
        if existing:
            return await send_error(interaction, "You have already redeemed a key and are whitelisted.")

        existing_hwid = find_user_by_hwid(users, hwid)
        if existing_hwid:
            owner_identifier = existing_hwid.get("Identifier", "Unknown")
            owner_discord_id = str(existing_hwid.get("DiscordId", ""))

            breach_embed = build_embed(
                title="🚨 Potential Breach: Duplicate HWID",
                description=(
                    f"{interaction.user.mention} attempted to redeem a key using an HWID that's "
                    f"already whitelisted under a different account. No user should ever have "
                    f"someone else's HWID -- this likely means **{owner_identifier}**'s access "
                    "was shared or leaked."
                ),
                color=discord.Color.orange(),
                fields=[
                    ("Attempting User", f"{interaction.user.mention} (`{discord_id_str}`)", False),
                    ("HWID Owner", f"**{owner_identifier}** (`{owner_discord_id}`)", False),
                    ("HWID", f"||`{hwid}`||", False),
                    ("Key Attempted", f"||`{key}`||", False),
                ],
                timestamp=datetime.now(timezone.utc),
            )
            await send_redeem_alert(interaction.client, breach_embed, HWIDBreachAlertView(owner_discord_id, owner_identifier, hwid, discord_id_str))

            return await send_error(interaction, f"This HWID is already whitelisted under **{owner_identifier}**.")

        existing_key = find_user_by_key(users, key)
        if existing_key:
            return await send_error(interaction, "This key has already been redeemed by someone else.")

        identifier = interaction.user.name
        rank = "User"
        join_date = format_join_date()

        try:
            users.append(build_user_entry(hwid, identifier, rank, discord_id_str, key, notes=None, join_date=join_date))
            await commit_users(users, sha, f"Redeemed key for user: {identifier} ({discord_id_str})")
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        redeemed_embed = build_embed(
            title="✅ Key Redeemed",
            color=discord.Color.green(),
            fields=[
                ("User", f"{interaction.user.mention} (`{discord_id_str}`)", False),
                ("Identifier", identifier, True),
                ("Rank", rank, True),
                ("Key", f"||`{key}`||", False),
                ("HWID", f"||`{hwid}`||", False),
            ],
            timestamp=datetime.now(timezone.utc),
        )
        await send_redeem_alert(interaction.client, redeemed_embed)

        success_fields = [
            ("Identifier", identifier, True),
            ("Rank", rank, True),
            ("Join Date", format_join_date_display := format_join_date(), True),
            ("HWID", f"||`{hwid}`||", False),
        ]

        # The user is already whitelisted at this point regardless of what
        # happens next, so a failure here shouldn't be reported as a plain
        # error -- tell them it succeeded and flag the leftover key for a
        # moderator to clean up manually.
        try:
            updated_keys = remove_permitted_key(permitted_keys, key)
            await commit_permitted_keys(updated_keys, keys_sha, f"Removed redeemed key for user: {identifier} ({discord_id_str})")
        except GitHubAPIError as e:
            return await send_success(
                interaction,
                f"Your key has been redeemed, **{identifier}**! You've been added to the whitelist.\n\n"
                f"⚠️ The key could not be automatically removed from permittedKeys.txt ({e}). "
                "A moderator should remove it manually to prevent reuse.",
                fields=success_fields,
            )

        await send_success(
            interaction,
            f"Your key has been redeemed, **{identifier}**! You've been added to the whitelist.",
            fields=success_fields,
        )


class ResetHWIDModal(Modal, title="Reset HWID"):
    """Lets an already-whitelisted user swap in a new HWID on their own
    entry (e.g. after a hardware change) without needing a moderator to run
    /edituser. Gated by ControlPanelView.reset_hwid's whitelist + cooldown
    checks before this modal is ever shown, and re-checked again here since
    the fetch those checks ran on can be stale by the time the user submits."""

    hwid = Label(
        text="HWID",
        description="Pre-hashed HWID in SHA-256 (64 hex characters). Run /hwidhelp for help getting yours.",
        component=TextInput(placeholder="64-character hex string", min_length=64, max_length=64),
    )

    async def on_submit(self, interaction: discord.Interaction):
        hwid = self.hwid.component.value.strip()

        if not is_valid_hwid(hwid):
            return await send_error(interaction, "Invalid HWID format. Must be 64 hex characters (SHA-256).")

        await interaction.response.defer(ephemeral=True)

        try:
            users, sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        discord_id_str = str(interaction.user.id)
        entry = find_user_by_discord_id(users, discord_id_str)
        if not entry:
            return await send_error(interaction, "You need to redeem a key before you can reset your HWID.")

        remaining = hwid_reset_cooldown_remaining(entry)
        if remaining:
            return await send_error(interaction, f"You can reset your HWID again in {humanize_timeleft(remaining, suffix=False)}.")

        old_hwid = entry.get("HWID")
        if hwid.lower() == (old_hwid or "").lower():
            return await send_error(interaction, "That's already your current HWID.")

        existing_hwid = find_user_by_hwid(users, hwid)
        if existing_hwid and existing_hwid is not entry:
            owner_identifier = existing_hwid.get("Identifier", "Unknown")
            owner_discord_id = str(existing_hwid.get("DiscordId", ""))

            breach_embed = build_embed(
                title="🚨 Potential Breach: Duplicate HWID",
                description=(
                    f"{interaction.user.mention} attempted to reset their HWID to one that's "
                    f"already whitelisted under a different account. No user should ever have "
                    f"someone else's HWID -- this likely means **{owner_identifier}**'s access "
                    "was shared or leaked."
                ),
                color=discord.Color.orange(),
                fields=[
                    ("Attempting User", f"{interaction.user.mention} (`{discord_id_str}`)", False),
                    ("HWID Owner", f"**{owner_identifier}** (`{owner_discord_id}`)", False),
                    ("HWID", f"||`{hwid}`||", False),
                ],
                timestamp=datetime.now(timezone.utc),
            )
            await send_redeem_alert(interaction.client, breach_embed, HWIDBreachAlertView(owner_discord_id, owner_identifier, hwid, discord_id_str))

            return await send_error(interaction, f"This HWID is already whitelisted under **{owner_identifier}**.")

        entry["HWID"] = hwid
        entry["LastHwidReset"] = format_join_date()
        entry["totalHwidResets"] = entry.get("totalHwidResets", 0) + 1

        try:
            await commit_users(users, sha, f"Reset HWID for user: {entry.get('Identifier', discord_id_str)} ({discord_id_str})")
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        reset_embed = build_embed(
            title="🔄 HWID Reset",
            color=discord.Color.blue(),
            fields=[
                ("User", f"{interaction.user.mention} (`{discord_id_str}`)", False),
                ("Old HWID", f"||`{old_hwid}`||", False),
                ("New HWID", f"||`{hwid}`||", False),
                ("Total Resets", str(entry["totalHwidResets"]), False),
            ],
            timestamp=datetime.now(timezone.utc),
        )
        await send_redeem_alert(interaction.client, reset_embed)

        await send_success(
            interaction,
            "Your HWID has been reset successfully.",
            fields=[
                ("New HWID", f"||`{hwid}`||", False),
                ("Next Reset Available", humanize_timeleft(config.RESET_HWID_COOLDOWN), False),
                ("Total Resets", str(entry["totalHwidResets"]), False),
            ],
        )


class ControlPanelView(LayoutView):
    """Persistent Components V2 control panel posted by /createpanel into
    #panel. Every button uses a fixed custom_id and this view is constructed
    with timeout=None, so as long as it's re-registered via bot.add_view()
    on startup, the buttons keep working indefinitely -- including across
    bot restarts -- without the panel message itself ever needing to be
    resent or edited."""

    def __init__(self):
        super().__init__(timeout=None)

        container = Container(
            TextDisplay(CONTROL_PANEL_TITLE),
            TextDisplay(CONTROL_PANEL_DESCRIPTION),
            accent_color=discord.Color.blurple(),
        )

        row = ActionRow()

        redeem_button = Button(label="Redeem Key", style=discord.ButtonStyle.success, custom_id=PANEL_REDEEM_KEY_ID)
        redeem_button.callback = self.redeem_key
        row.add_item(redeem_button)

        script_button = Button(label="Get Script", style=discord.ButtonStyle.primary, custom_id=PANEL_GET_SCRIPT_ID)
        script_button.callback = self.get_script
        row.add_item(script_button)

        role_button = Button(label="Get Role", style=discord.ButtonStyle.primary, custom_id=PANEL_GET_ROLE_ID)
        role_button.callback = self.get_role
        row.add_item(role_button)

        hwid_button = Button(label="Reset HWID", style=discord.ButtonStyle.secondary, custom_id=PANEL_RESET_HWID_ID)
        hwid_button.callback = self.reset_hwid
        row.add_item(hwid_button)

        info_button = Button(label="Get Info", style=discord.ButtonStyle.secondary, custom_id=PANEL_GET_INFO_ID)
        info_button.callback = self.get_info
        row.add_item(info_button)

        container.add_item(row)
        self.add_item(container)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        # The default View.on_error only logs to the console and leaves the
        # interaction completely unanswered, which looks like a silent/dead
        # button instead of a visible error.
        print(f"Error in ControlPanelView for item {item!r}: {error}")
        try:
            await send_error(interaction, "Something went wrong. Please try again, and let a moderator know if it keeps happening.")
        except Exception as e:
            print(f"Failed to notify user of ControlPanelView error: {e}")

    async def redeem_key(self, interaction: discord.Interaction):
        # Sending a modal must be the interaction's very first response,
        # within Discord's ~3 second ack window, so this can't do a live
        # GitHub fetch first. get_cached_users() reads the in-memory
        # Users.json cache instead, which never touches the network. If the
        # cache says the user already has an entry, skip the modal entirely
        # instead of letting them fill it out for nothing.
        # RedeemKeyModal.on_submit() still re-checks against a fresh fetch
        # before committing anything -- this is just a UX improvement, not
        # a security boundary. If the cache hasn't been populated yet (e.g.
        # right after a bot restart), fall back to opening the modal
        # unconditionally.
        users = get_cached_users()
        if users is not None:
            existing = find_user_by_discord_id(users, str(interaction.user.id))
            if existing:
                return await send_error(interaction, "You have already redeemed a key and are whitelisted.")

        await interaction.response.send_modal(RedeemKeyModal())

    async def get_script(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            users, _sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        entry = find_user_by_discord_id(users, str(interaction.user.id))
        if not entry:
            return await send_error(interaction, "You need to redeem a key before you can get your script.")

        key = entry.get("Key")
        if not key:
            return await send_error(interaction, "Your whitelist entry doesn't have a key on file. Contact a moderator.")

        try:
            script_text = await fetch_stored_script()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        try:
            script_text = inject_script_key(script_text, key)
        except ValueError as e:
            return await send_error(interaction, str(e))

        filename = "script.lua"
        file = discord.File(io.BytesIO(script_text.encode("utf-8")), filename=filename)

        layout = LayoutView(timeout=None)
        layout.add_item(Container(
            TextDisplay("### 📜 Your Script"),
            TextDisplay("Here's your personalized script, keyed to your account. Keep it to yourself."),
            accent_color=discord.Color.blue(),
        ))
        layout.add_item(File(f"attachment://{filename}"))

        await interaction.followup.send(view=layout, file=file, ephemeral=True)

    async def get_role(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            users, _sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        entry = find_user_by_discord_id(users, str(interaction.user.id))
        if not entry:
            return await send_error(interaction, "You need to redeem a key before you can get your role.")

        role = interaction.guild.get_role(config.BUYER_ROLE_ID)
        if not role:
            return await send_error(interaction, "Buyer role not found.")

        if role in interaction.user.roles:
            return await send_error(interaction, f"You already have the {role.mention} role.")

        await interaction.user.add_roles(role, reason="Whitelisted user claimed Buyer role via control panel")
        await send_success(interaction, f"You've been given the {role.mention} role.")

    async def reset_hwid(self, interaction: discord.Interaction):
        # Best-effort pre-check: skip prompting for a new HWID if the user
        # isn't whitelisted or is still on cooldown. This has to stay fast --
        # send_modal (like send_message) must be the interaction's first
        # response, so it reads from the in-memory Users.json cache instead
        # of hitting GitHub live.
        #
        # get_cached_users() can still be None very briefly right after a
        # bot restart, before the first refresh has landed -- in that one
        # window this falls back to opening the modal unconditionally, same
        # as redeem_key. ResetHWIDModal.on_submit() re-checks both
        # whitelist and cooldown against a fresh fetch regardless.
        users = get_cached_users()
        if users is None:
            return await interaction.response.send_modal(ResetHWIDModal())

        discord_id_str = str(interaction.user.id)
        entry = find_user_by_discord_id(users, discord_id_str)
        if not entry:
            return await send_error(interaction, "You need to redeem a key before you can reset your HWID.")

        remaining = hwid_reset_cooldown_remaining(entry)
        if remaining:
            return await send_error(interaction, f"You can reset your HWID again in {humanize_timeleft(remaining, suffix=False)}.")

        await interaction.response.send_modal(ResetHWIDModal())

    async def get_info(self, interaction: discord.Interaction):
        # Same lookup + embed as /myinfo, just triggered from the panel
        # button instead. Always a fresh Contents-API fetch (same reasoning
        # as get_script/get_role above) rather than the in-memory cache,
        # since this is a user-facing info readout and should reflect the
        # latest data.
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
        embed.add_field(name="Join Date", value=user_data.get("JoinDate", "N/A"), inline=True)
        embed.add_field(name="HWID", value=f"||{user_data.get('HWID', 'N/A')}||", inline=True)
        embed.add_field(name="Key", value=f"||{user_data.get('Key', 'N/A')}||", inline=True)
        embed.add_field(name="Last HWID Reset", value=user_data.get("LastHwidReset") or "N/A", inline=True)
        embed.add_field(name="Total HWID Resets", value=str(user_data.get("totalHwidResets", 0)), inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)


class Panel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="createpanel", description="Posts the control panel in the panel channel.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def createpanel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        channel = self.bot.get_channel(config.PANEL_CHANNEL_ID)
        if not channel:
            return await send_error(interaction, "Panel channel not found.")

        await channel.send(view=ControlPanelView())
        await send_success(interaction, f"Control panel posted in {channel.mention}.")

    @app_commands.command(name="updatescript", description="Updates the script /createpanel's Get Script button hands out.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(script="New storedscript.lua contents. Must be exactly 2 lines: the script key line, then the loading line.")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def updatescript(self, interaction: discord.Interaction, script: discord.Attachment):
        await interaction.response.defer(ephemeral=True)

        if not script.filename.lower().endswith(".lua"):
            return await send_error(interaction, "Please upload a valid `.lua` file.")

        try:
            raw_bytes = await script.read()
        except discord.HTTPException as e:
            return await send_error(interaction, f"Failed to download the uploaded file: {e}")

        try:
            script_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return await send_error(interaction, "That file isn't valid UTF-8 text.")

        # Enforces the exact 2-line shape (script key line, then loading
        # line) that Get Script's inject_script_key() depends on. Catches a
        # bad upload here instead of it silently breaking every whitelisted
        # user's Get Script click afterward.
        error = validate_stored_script(script_text)
        if error:
            return await send_error(interaction, error)

        try:
            _current_text, sha = await fetch_stored_script_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        try:
            await commit_stored_script(script_text, sha, f"Update storedscript.lua by {interaction.user}")
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        await send_success(
            interaction,
            "`storedscript.lua` has been updated. **Get Script** will now hand out this version, keyed to each user.",
            fields=[("New Script", f"```lua\n{script_text}\n```", False)],
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Panel(bot))

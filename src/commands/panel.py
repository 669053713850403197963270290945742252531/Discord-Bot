"""Self-service control panel for the Discord-powered license system."""

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Modal, TextInput, Label, LayoutView, Container, TextDisplay, ActionRow, Button

from api import config
from api.discord_helpers import has_role, is_in_guild, send_success, send_error, build_embed, default_ui_error, dms_enabled
from api.github import fetch_stored_script, fetch_stored_script_with_sha, commit_stored_script, validate_stored_script, inject_script_key
from api.supabase_db import get_license_by_discord_id, is_redeemable_key, redeem_license, reset_license_hwid
from api.time_utils import format_discord_timestamp, hwid_reset_cooldown_remaining, humanize_timeleft

GUILD = discord.Object(id=config.GUILD_ID)
CONTROL_PANEL_TITLE = "### Control Panel"
CONTROL_PANEL_DESCRIPTION = "Redeem your license key, retrieve the loader, or view your license information."
PANEL_REDEEM_KEY_ID = "panel_redeem_key"
PANEL_GET_SCRIPT_ID = "panel_get_script"
PANEL_GET_ROLE_ID = "panel_get_role"
PANEL_GET_INFO_ID = "panel_get_info"
PANEL_RESET_HWID_ID = "panel_reset_hwid"


def _games_text(games):
    return "All games" if "*" in (games or []) else ", ".join(games or []) or "No games assigned"


def _games_links_text(games):
    games = games or []
    if "*" in games:
        return "* — All supported games"
    if not games:
        return "No games assigned"
    return "\n".join(
        f"[{game_id}](https://www.roblox.com/games/{game_id})"
        for game_id in games
    )


class RedeemKeyModal(Modal, title="Redeem Key"):
    key = Label(text="License Key", description="Enter the key you were given.", component=TextInput(placeholder="Paste your license key", max_length=256))

    async def on_error(self, interaction, error):
        await default_ui_error(interaction, error, label="RedeemKeyModal")

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        key = self.key.component.value.strip()
        if not key:
            return await send_error(interaction, "Enter a license key.")
        existing_user = await get_license_by_discord_id(str(interaction.user.id))
        if existing_user and existing_user.get("Key"):
            return await send_error(interaction, "You already have a license associated with your Discord account.")
        if not await is_redeemable_key(key):
            return await send_error(interaction, "That license key does not exist or has already been redeemed.")
        try:
            entry = await redeem_license(key, str(interaction.user.id), str(interaction.user.name), "User")
        except PermissionError:
            return await send_error(interaction, "That license key has already been redeemed by another Discord account.")
        except ValueError as e:
            return await send_error(interaction, f"Could not redeem the license: {e}")
        except Exception as e:
            return await send_error(interaction, f"License database error: {e}")
        if not entry:
            return await send_error(interaction, "That license key could not be redeemed.")
        await send_success(interaction, "License redeemed successfully.", fields=[
            ("Identifier", entry.get("Identifier"), True),
            ("Rank", entry.get("Rank"), True),
            ("Games", _games_text(entry.get("Games")), False),
            ("Activation", "Pending first successful game launch", False),
        ])


class ControlPanelView(LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        self.redeem = Button(label="🔑 Redeem Key", style=discord.ButtonStyle.success, custom_id=PANEL_REDEEM_KEY_ID)
        self.script = Button(label="📜 Get Script", style=discord.ButtonStyle.primary, custom_id=PANEL_GET_SCRIPT_ID)
        self.role = Button(label="🎖️ Get Role", style=discord.ButtonStyle.secondary, custom_id=PANEL_GET_ROLE_ID)
        self.reset_hwid = Button(label="🔄 Reset HWID", style=discord.ButtonStyle.secondary, custom_id=PANEL_RESET_HWID_ID)
        self.info = Button(label="📊 Get Stats", style=discord.ButtonStyle.secondary, custom_id=PANEL_GET_INFO_ID)
        self.redeem.callback = self.on_redeem
        self.script.callback = self.on_script
        self.role.callback = self.on_role
        self.reset_hwid.callback = self.on_reset_hwid
        self.info.callback = self.on_info
        self.add_item(Container(TextDisplay(CONTROL_PANEL_TITLE), TextDisplay(CONTROL_PANEL_DESCRIPTION), ActionRow(self.redeem, self.script, self.role, self.reset_hwid, self.info), accent_color=discord.Color.green()))

    async def on_redeem(self, interaction):
        existing_user = await get_license_by_discord_id(str(interaction.user.id))
        if existing_user and existing_user.get("Key"):
            return await send_error(interaction, "You already have a license associated with your Discord account.")
        await interaction.response.send_modal(RedeemKeyModal())

    async def on_script(self, interaction):
        entry = await get_license_by_discord_id(str(interaction.user.id))
        if not entry or not entry.get("Key"):
            return await send_error(interaction, "You do not have a redeemed license.")
        try:
            script = await fetch_stored_script()
            script = inject_script_key(script, entry["Key"])
        except Exception as e:
            return await send_error(interaction, f"Failed to prepare the loader: {e}")
        await interaction.response.send_message(f"```lua\n{script}\n```", ephemeral=True)

    async def on_role(self, interaction):
        entry = await get_license_by_discord_id(str(interaction.user.id))
        if not entry:
            return await send_error(interaction, "You do not have a redeemed license.")
        guild = interaction.guild
        role = guild.get_role(config.BUYER_ROLE_ID) if guild else None
        member = guild.get_member(interaction.user.id) if guild else None
        if not role or not member:
            return await send_error(interaction, "The Buyer role could not be found.")
        if role in member.roles:
            return await send_success(interaction, "You already have the Buyer role.")
        try:
            await member.add_roles(role, reason="Licensed user requested Buyer role")
        except (discord.Forbidden, discord.HTTPException) as e:
            return await send_error(interaction, f"Could not add the Buyer role: {e}")
        await send_success(interaction, "Buyer role granted.")

    async def on_reset_hwid(self, interaction):
        """Clear this user's bound HWID and start the persistent 7-day cooldown."""
        await interaction.response.defer(ephemeral=True)

        try:
            entry = await get_license_by_discord_id(str(interaction.user.id))
        except Exception as exc:
            return await send_error(interaction, f"License database error: {exc}")

        if not entry:
            return await send_error(interaction, "You do not have a redeemed license.")

        remaining = hwid_reset_cooldown_remaining(entry.get("LastHwidReset"))
        if remaining:
            return await send_error(
                interaction,
                f"You can reset your HWID again in {humanize_timeleft(remaining, suffix=False)}.",
            )

        old_hwid = entry.get("HWID")
        if not old_hwid:
            return await send_error(
                interaction,
                "Your HWID is already cleared. Run the script again to bind this device.",
            )

        try:
            updated = await reset_license_hwid(str(entry["Identifier"]))
        except Exception as exc:
            return await send_error(interaction, f"Failed to reset your HWID: {exc}")

        if not updated:
            return await send_error(interaction, "The license could not be updated.")

        new_count = int(updated.get("hwid_resets") or 0)
        await send_success(
            interaction,
            "Your HWID has been reset successfully.",
            fields=[
                ("Status", "HWID cleared; ready for a new device", False),
                ("Previous HWID", f"||`{old_hwid}`||", False),
                ("Current HWID", "Unset — awaiting next activation", False),
                ("Next Reset Available", humanize_timeleft(config.RESET_HWID_COOLDOWN), False),
                ("Total Resets", str(new_count), True),
            ],
        )

    async def on_info(self, interaction):
        entry = await get_license_by_discord_id(str(interaction.user.id))
        if not entry:
            return await send_error(interaction, "You do not have a redeemed license.")

        enabled = bool(entry.get("Enabled", True))
        hwid = entry.get("HWID")
        key = entry.get("Key") or "N/A"
        last_reset = format_discord_timestamp(entry.get("LastHwidReset"), "R") if entry.get("LastHwidReset") else "Never"
        expires_at = format_discord_timestamp(entry.get("ExpiresAt"), "R") if entry.get("ExpiresAt") else "Never"
        updated_at = format_discord_timestamp(entry.get("UpdatedAt"), "R") if entry.get("UpdatedAt") else "Never"

        embed = discord.Embed(
            title="License Stats",
            color=discord.Color.green(),
            description=f"**{entry.get('Identifier') or 'Unknown'}** — license information",
        )
        embed.add_field(name="Identifier", value=f"`{entry.get('Identifier') or 'N/A'}`", inline=False)
        embed.add_field(name="Discord ID", value=f"`{entry.get('DiscordId') or 'N/A'}`", inline=False)
        embed.add_field(name="Games", value=_games_links_text(entry.get("Games")), inline=False)
        embed.add_field(name="License Enabled", value="Yes ✅" if enabled else "No ❌", inline=True)
        embed.add_field(name="License Updated", value=updated_at, inline=True)
        embed.add_field(name="Total Executions", value=f"`{int(entry.get('Executions') or 0)}` 🧠", inline=True)
        embed.add_field(name="HWID Status", value="Assigned ✅" if hwid else "Unset ❌", inline=True)
        embed.add_field(name="Key", value=f"||`{key}`|| 🔒", inline=False)
        embed.add_field(name="Total HWID Resets", value=f"`{int(entry.get('totalHwidResets') or 0)}` ⚙️", inline=True)
        embed.add_field(name="Last Reset", value=f"{last_reset} 📅", inline=True)
        embed.add_field(name="Expires At", value=f"{expires_at} 📅", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)


class Panel(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @app_commands.command(name="createpanel", description="Posts the license control panel.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def createpanel(self, interaction):
        await interaction.response.send_message("Control panel posted.", ephemeral=True)
        await interaction.channel.send(view=ControlPanelView())

    @app_commands.command(name="updatescript", description="Updates the public loader used by the Get Script button.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def updatescript(self, interaction, file: discord.Attachment):
        raw = (await file.read()).decode("utf-8")
        if len(raw) > 100_000:
            return await send_error(interaction, "The script is too large.")
        try:
            validate_stored_script(raw)
            old, sha = await fetch_stored_script_with_sha()
            await commit_stored_script(raw, sha, f"Update stored loader by {interaction.user}")
        except Exception as e:
            return await send_error(interaction, f"Failed to update loader: {e}")
        await send_success(interaction, "Public loader updated.")


async def setup(bot):
    await bot.add_cog(Panel(bot))

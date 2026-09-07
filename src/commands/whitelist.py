"""Whitelist/license administration backed by Supabase."""

import csv
import io
import json
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Modal, TextInput, Label, LayoutView, Container, TextDisplay, ActionRow, Button

from api import config
from api.discord_helpers import has_role, is_in_guild, send_success, send_error, default_ui_error, resolve_user_option
from api.alerts import send_alert, alert_embed, ALERT_COLOR_ADD, ALERT_COLOR_REMOVE, ALERT_COLOR_EDIT
from api.supabase_db import (
    fetch_users, fetch_users_with_sha, fetch_api_text_and_sha, commit_content,
    commit_users, get_license_by_discord_id, get_license_by_key, get_license_by_identifier, update_license,
    delete_license, set_license_games, get_license_game_ids,
)
from api.users import find_user_by_discord_id, find_user_by_key, remove_user_by_discord_id, build_user_entry, revoke_buyer_role, find_removed_discord_ids
from api.keys import generate_unique_key, is_valid_discord_id, parse_game_ids
from api.time_utils import format_discord_timestamp

GUILD = discord.Object(id=config.GUILD_ID)
WHITELIST_RANKS = ["User", "Premium", "VIP", "Staff", "Admin", "Owner"]
_RANK_LOOKUP = {x.lower(): x for x in WHITELIST_RANKS}
MAX_BULK_WHITELIST_ROWS = 50


def _games_text(games):
    games = games or []
    return "All games" if "*" in games else ", ".join(games) or "No games assigned"


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


def _record_updates_from_legacy(entry):
    return {
        "discord_id": entry.get("DiscordId"),
        "license_key": entry.get("Key"),
        "activated": entry.get("Activated"),
        "executions": entry.get("Executions", 0),
        "rank": entry.get("Rank", "User"),
        "notes": entry.get("Notes"),
        "enabled": entry.get("Enabled", True),
        "expires_at": entry.get("ExpiresAt"),
        "games": entry.get("Games") or ["*"],
    }


class WhitelistModal(Modal, title="Whitelist a User"):
    identifier = Label(text="Identifier", component=TextInput(placeholder="e.g. JohnDoe", max_length=100))
    target_user = Label(text="Discord User ID", component=TextInput(placeholder="e.g. 123456789012345678", max_length=32))
    rank = Label(text="Rank", component=discord.ui.Select(
        placeholder="Select a rank...", min_values=1, max_values=1,
        options=[discord.SelectOption(label=r, value=r) for r in WHITELIST_RANKS],
    ))
    games = Label(text="Games", description="Comma-separated Roblox PlaceIds; use * for all games.", component=TextInput(placeholder="* or 123456789, 5936782561", max_length=500))
    notes = Label(text="Notes", component=TextInput(style=discord.TextStyle.paragraph, placeholder="Optional", required=False, max_length=500))

    def __init__(self, target: Optional[discord.Member] = None):
        if target:
            super().__init__(title=f"Whitelist {target.display_name}"[:45])
        else:
            super().__init__()
        if target:
            self.target_user.component.default = str(target.id)

    async def on_error(self, interaction, error):
        await default_ui_error(interaction, error, label="WhitelistModal")

    async def on_submit(self, interaction):
        identifier = self.identifier.component.value.strip()
        discord_id = self.target_user.component.value.strip()
        rank = self.rank.component.values[0]
        notes = self.notes.component.value.strip() or None
        try:
            games = parse_game_ids(self.games.component.value)
        except ValueError as e:
            return await send_error(interaction, str(e))
        if not identifier or not is_valid_discord_id(discord_id):
            return await send_error(interaction, "A valid Discord user ID is required.")
        users = await fetch_users()
        if find_user_by_discord_id(users, discord_id):
            return await send_error(interaction, "That Discord user is already licensed.")
        key = generate_unique_key(users)
        entry = build_user_entry(identifier, rank, discord_id, key, notes=notes, games=games)
        users.append(entry)
        try:
            await commit_users(users, None, f"Whitelist {identifier} ({discord_id})")
        except Exception as e:
            return await send_error(interaction, f"Failed to create license: {e}")
        await send_alert(interaction.client, alert_embed("✅ User Whitelisted", f"{interaction.user.mention} added {identifier}.", color=ALERT_COLOR_ADD))
        await send_success(interaction, f"Added **{identifier}** to the license database.", fields=[
            ("Discord ID", discord_id, True), ("License Key", f"||`{key}`||", False), ("Games", _games_text(games), False),
        ])


def _parse_bulk_row(row):
    def cell(name):
        value = row.get(name)
        return value.strip() if isinstance(value, str) else ""
    identifier = cell("identifier")
    discord_id = cell("discord_id")
    rank = _RANK_LOOKUP.get(cell("rank").lower(), "User") or "User"
    notes = cell("notes") or None
    key = cell("key")
    games_value = cell("games") or "*"
    if not identifier or not is_valid_discord_id(discord_id):
        return None, "identifier and a valid discord_id are required"
    try:
        games = parse_game_ids(games_value)
    except ValueError as e:
        return None, str(e)
    return {"Identifier": identifier, "DiscordId": discord_id, "Rank": rank, "Notes": notes, "Key": key, "Games": games}, None


async def _bulkwhitelist_impl(interaction, attachment):
    await interaction.response.defer(ephemeral=True)
    raw = await attachment.read()
    try:
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    except Exception as e:
        return await send_error(interaction, f"Invalid CSV: {e}")
    if len(rows) > MAX_BULK_WHITELIST_ROWS:
        return await send_error(interaction, f"CSV exceeds the {MAX_BULK_WHITELIST_ROWS}-row limit.")
    if not rows or not {str(k).strip().lower() for k in rows[0].keys()} >= {"identifier", "discord_id"}:
        return await send_error(interaction, "CSV must contain `identifier` and `discord_id` columns.")
    users = await fetch_users()
    used_identifiers = {str(u.get("Identifier")) for u in users}
    used_discord = {str(u.get("DiscordId")) for u in users if u.get("DiscordId")}
    used_keys = {str(u.get("Key")) for u in users if u.get("Key")}
    added = []
    errors = []
    for index, row in enumerate(rows, 2):
        normalized = {str(k).strip().lower(): v for k, v in row.items()}
        parsed, error = _parse_bulk_row(normalized)
        if error:
            errors.append(f"row {index}: {error}")
            continue
        if parsed["Identifier"] in used_identifiers:
            errors.append(f"row {index}: identifier already exists")
            continue
        if parsed["DiscordId"] in used_discord:
            errors.append(f"row {index}: Discord ID already exists")
            continue
        key = parsed["Key"] or generate_unique_key(users, 25, 40)
        while key in used_keys:
            key = generate_unique_key(users, 25, 40)
        parsed["Key"] = key
        used_identifiers.add(parsed["Identifier"]); used_discord.add(parsed["DiscordId"]); used_keys.add(key)
        added.append(parsed)
        users.append(parsed)
    if added:
        try:
            await commit_users(users, None, f"Bulk whitelist {len(added)} user(s) by {interaction.user}")
        except Exception as e:
            return await send_error(interaction, f"Database write failed: {e}")
    message = f"Added {len(added)} license(s)."
    if errors:
        message += "\n\nSkipped:\n" + "\n".join(errors[:10])
    await send_success(interaction, message)


async def _unwhitelist_user_impl(interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    entry = await get_license_by_discord_id(str(user.id))
    if not entry:
        return await send_error(interaction, f"{user.mention} is not licensed.")
    try:
        removed = await delete_license(str(entry["Identifier"]))
    except Exception as e:
        return await send_error(interaction, f"Failed to remove license: {e}")
    if not removed:
        return await send_error(interaction, "License was not found when the delete was attempted.")
    await revoke_buyer_role(interaction.guild, user.id)
    await send_alert(interaction.client, alert_embed("🗑️ License Removed", f"{interaction.user.mention} removed {user.mention}.", color=ALERT_COLOR_REMOVE))
    await send_success(interaction, f"Removed {user.mention} from the license database.")


class EditUserModal(Modal, title="Edit User"):
    identifier = Label(text="Identifier", component=TextInput(max_length=100))
    discord_id = Label(text="Discord User ID", component=TextInput(max_length=32))
    rank = Label(text="Rank", component=discord.ui.Select(
        placeholder="Select a rank...", min_values=1, max_values=1,
        options=[discord.SelectOption(label=r, value=r) for r in WHITELIST_RANKS],
    ))
    games = Label(text="Games", component=TextInput(placeholder="* or 123456789,5936782561", max_length=500))
    notes = Label(text="Notes", component=TextInput(style=discord.TextStyle.paragraph, required=False, max_length=500))

    def __init__(self, user_data, whitelist_view=None):
        super().__init__(title=f"Edit {user_data.get('Identifier','User')}"[:45])
        self.original_identifier = user_data.get("Identifier")
        self.whitelist_view = whitelist_view
        self.identifier.component.default = user_data.get("Identifier") or ""
        self.discord_id.component.default = str(user_data.get("DiscordId") or "")
        rank = str(user_data.get("Rank") or "User")
        for option in self.rank.component.options:
            option.default = option.value == rank
        self.games.component.default = ",".join(user_data.get("Games") or ["*"])
        self.notes.component.default = user_data.get("Notes") or ""

    async def on_error(self, interaction, error):
        await default_ui_error(interaction, error, label="EditUserModal")

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        identifier = self.identifier.component.value.strip()
        discord_id = self.discord_id.component.value.strip()
        if not identifier or not is_valid_discord_id(discord_id):
            return await send_error(interaction, "A valid identifier and Discord ID are required.")
        try:
            games = parse_game_ids(self.games.component.value)
        except ValueError as e:
            return await send_error(interaction, str(e))
        old = await get_license_by_identifier(self.original_identifier)
        if not old:
            return await send_error(interaction, "License no longer exists.")
        if identifier != self.original_identifier and await get_license_by_identifier(identifier):
            return await send_error(interaction, "That identifier is already in use.")
        updates = {"discord_id": discord_id, "rank": self.rank.component.values[0], "notes": self.notes.component.value.strip() or None, "games": games}
        if identifier != self.original_identifier:
            updates["license_key"] = old.get("Key")
            new_record = dict(old); new_record["Identifier"] = identifier; new_record["DiscordId"] = discord_id; new_record["Rank"] = updates["rank"]; new_record["Notes"] = updates["notes"]; new_record["Games"] = games
            users = await fetch_users(); users = [new_record if str(u.get("Identifier")) == self.original_identifier else u for u in users]
            await commit_users(users, None, f"Edit license {identifier} ({discord_id})")
        else:
            await update_license(self.original_identifier, **updates)
        await send_success(interaction, f"Updated **{identifier}**.")
        if self.whitelist_view is not None:
            try:
                self.whitelist_view.users = await fetch_users()
                for i, user in enumerate(self.whitelist_view.users):
                    if str(user.get("Identifier")) == str(identifier):
                        self.whitelist_view.index = i
                        break
                self.whitelist_view.pending_notice = f"✅ Updated **{identifier}**."
                if self.whitelist_view.message is not None:
                    self.whitelist_view._update_buttons()
                    await self.whitelist_view.message.edit(view=self.whitelist_view)
                self.whitelist_view.pending_notice = None
            except Exception:
                pass


class DeleteUserConfirmView(LayoutView):
    def __init__(self, whitelist_view):
        super().__init__(timeout=60)
        self.whitelist_view = whitelist_view
        self.confirm = Button(label="Confirm Delete", emoji="🗑️", style=discord.ButtonStyle.danger)
        self.cancel = Button(label="Cancel", emoji="↩️", style=discord.ButtonStyle.secondary)
        self.confirm.callback = self._confirm
        self.cancel.callback = self._cancel
        self.add_item(Container(
            TextDisplay("### ⚠️ Delete Whitelist Entry"),
            TextDisplay("Are you sure you want to delete this whitelist entry? This action cannot be undone."),
            ActionRow(self.confirm, self.cancel),
        ))

    async def _confirm(self, interaction: discord.Interaction):
        view = self.whitelist_view
        if not view.users:
            return await interaction.response.edit_message(view=view.render())

        entry = view.users[view.index]
        identifier = str(entry.get("Identifier") or "")
        discord_id = str(entry.get("DiscordId") or "")
        if not identifier:
            return await send_error(interaction, "The selected license entry is invalid.")

        try:
            removed = await delete_license(identifier)
        except Exception as e:
            return await send_error(interaction, f"Failed to delete license: {e}")
        if not removed:
            return await send_error(interaction, "The license no longer exists.")

        if discord_id:
            try:
                await revoke_buyer_role(interaction.guild, discord_id)
            except Exception:
                pass

        await send_alert(
            interaction.client,
            alert_embed(
                "🗑️ User Deleted",
                f"{interaction.user.mention} deleted **{identifier}** from the whitelist via `/viewwhitelist`.",
                color=ALERT_COLOR_REMOVE,
            ),
        )

        view.users = await fetch_users()
        if view.index >= len(view.users):
            view.index = max(0, len(view.users) - 1)
        view.pending_notice = f"🗑️ Deleted **{identifier}**."
        view._rebuild()
        await interaction.response.edit_message(view=view)
        view.pending_notice = None

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=self.whitelist_view)


class WhitelistView(LayoutView):
    """Paginated whitelist browser using Components V2 with full license information."""

    def __init__(self, bot, users, current_index=0):
        super().__init__(timeout=600)
        self.bot = bot
        self.users = users
        self.index = max(0, min(current_index, max(0, len(users) - 1)))
        self.message = None
        self.pending_notice = None
        self._rebuild()

    def _games_display(self, games):
        return _games_links_text(games)

    def _description(self):
        if not self.users:
            return "### Whitelist\nNo license entries found."

        user = self.users[self.index]
        total = len(self.users)
        discord_id = str(user.get("DiscordId") or "")
        hwid = user.get("HWID")
        key = user.get("Key")
        activated = user.get("Activated")
        updated = user.get("UpdatedAt")
        created = user.get("CreatedAt")
        expires = user.get("ExpiresAt")
        last_reset = user.get("LastHwidReset")
        reset_count = int(user.get("totalHwidResets") or 0)
        executions = int(user.get("Executions") or 0)
        notes = user.get("Notes")
        lines = [
            f"### Whitelist Entry — {self.index + 1}/{total}",
            self.pending_notice or "",
            f"**Identifier:** {user.get('Identifier') or 'N/A'}",
            f"**Discord:** <@{discord_id}> (`{discord_id}`)" if discord_id else "**Discord:** N/A",
            f"**Rank:** {user.get('Rank') or 'User'}",
            f"**License Key:** ||`{key or 'N/A'}`||",
            f"**License Enabled:** {'✅ Yes' if user.get('Enabled', True) else '❌ No'}",
            f"**HWID Status:** {'✅ Assigned' if hwid else '❌ Unset'}",
            f"**HWID:** ||`{hwid}`||" if hwid else "**HWID:** Unset",
            f"**Games:**\n{self._games_display(user.get('Games'))}",
            f"**Activated:** {format_discord_timestamp(activated) if activated else 'Pending first successful execution'}",
            f"**Total Executions:** `{executions}`",
            f"**Total HWID Resets:** `{reset_count}`",
            f"**Last HWID Reset:** {format_discord_timestamp(last_reset) if last_reset else 'Never'}",
            f"**Expires At:** {format_discord_timestamp(expires) if expires else 'Never'}",
            f"**Created:** {format_discord_timestamp(created) if created else 'Unknown'}",
            f"**License Updated:** {format_discord_timestamp(updated) if updated else 'Unknown'}",
            f"**Notes:** {notes if notes else 'N/A'}",
        ]
        return "\n".join(x for x in lines if x)

    def _rebuild(self):
        self.clear_items()
        self.previous_button = Button(label="Previous", emoji="⏮️", style=discord.ButtonStyle.secondary, disabled=self.index <= 0)
        self.next_button = Button(label="Next", emoji="⏭️", style=discord.ButtonStyle.secondary, disabled=self.index >= len(self.users) - 1)
        self.edit_button = Button(label="Edit User", emoji="✏️", style=discord.ButtonStyle.primary, disabled=not bool(self.users))
        self.delete_button = Button(label="Delete User", emoji="🗑️", style=discord.ButtonStyle.danger, disabled=not bool(self.users))
        self.refresh_button = Button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary)
        self.previous_button.callback = self._previous
        self.next_button.callback = self._next
        self.edit_button.callback = self._edit_user
        self.delete_button.callback = self._delete_user
        self.refresh_button.callback = self._refresh
        self.add_item(Container(
            TextDisplay(self._description()),
            ActionRow(self.previous_button, self.next_button, self.edit_button, self.delete_button, self.refresh_button),
        ))

    def _update_buttons(self):
        self._rebuild()

    async def _refresh_data(self):
        self.users = await fetch_users()
        if self.index >= len(self.users):
            self.index = max(0, len(self.users) - 1)
        self.pending_notice = None
        self._rebuild()

    async def _previous(self, interaction: discord.Interaction):
        self.index = max(0, self.index - 1)
        self._rebuild()
        await interaction.response.edit_message(view=self)
        self.pending_notice = None

    async def _next(self, interaction: discord.Interaction):
        self.index = min(len(self.users) - 1, self.index + 1)
        self._rebuild()
        await interaction.response.edit_message(view=self)
        self.pending_notice = None

    async def _edit_user(self, interaction: discord.Interaction):
        if not self.users:
            return await send_error(interaction, "No license entry is selected.")
        await interaction.response.send_modal(EditUserModal(self.users[self.index], self))

    async def _delete_user(self, interaction: discord.Interaction):
        if not self.users:
            return await send_error(interaction, "No license entry is selected.")
        await interaction.response.edit_message(view=DeleteUserConfirmView(self))

    async def _refresh(self, interaction: discord.Interaction):
        try:
            await self._refresh_data()
        except Exception as e:
            return await send_error(interaction, f"Failed to refresh whitelist: {e}")
        self.pending_notice = "🔄 Whitelist refreshed."
        self._rebuild()
        await interaction.response.edit_message(view=self)
        self.pending_notice = None

    async def on_error(self, interaction, error, item):
        await default_ui_error(interaction, error, item, label="WhitelistView")

    def render(self):
        self._rebuild()
        return self


class Whitelist(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @app_commands.command(name="whitelist", description="Adds a user to the license database.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def whitelist(self, interaction):
        await interaction.response.send_modal(WhitelistModal())

    @app_commands.command(name="bulkwhitelist", description="Bulk-add licenses from a CSV (identifier,discord_id,rank,notes,key,games).")
    @app_commands.guilds(GUILD)
    @app_commands.describe(file="CSV file to import")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def bulkwhitelist(self, interaction, file: discord.Attachment):
        await _bulkwhitelist_impl(interaction, file)

    @app_commands.command(name="unwhitelist", description="Removes a user's license.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="User to remove")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def unwhitelist(self, interaction, user: discord.Member):
        await _unwhitelist_user_impl(interaction, user)

    @app_commands.command(name="editwhitelist", description="Replace the license database from JSON export.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def editwhitelist(self, interaction):
        current, _ = await fetch_api_text_and_sha()
        modal = Modal(title="Edit License JSON")
        text = TextInput(label="License JSON", style=discord.TextStyle.paragraph, default=current[:4000], max_length=4000)
        modal.add_item(text)
        async def submit(i):
            try:
                payload = json.loads(text.value)
                if not isinstance(payload, list): raise ValueError("JSON must be an array of license objects")
                await commit_content(json.dumps(payload), None, f"Replace license database by {i.user}")
            except Exception as e:
                return await send_error(i, f"Invalid license JSON: {e}")
            await send_success(i, "License database updated.")
        modal.on_submit = submit
        await interaction.response.send_modal(modal)

    @app_commands.command(name="edituser", description="Edits a licensed user's information.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="Licensed user")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def edituser(self, interaction, user: discord.Member):
        entry = await get_license_by_discord_id(str(user.id))
        if not entry: return await send_error(interaction, "That user is not licensed.")
        await interaction.response.send_modal(EditUserModal(entry))

    @app_commands.command(name="fetchuser", description="Fetches stored license information.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="Licensed user")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def fetchuser(self, interaction, user: discord.Member):
        entry = await get_license_by_discord_id(str(user.id))
        if not entry: return await send_error(interaction, "That user is not licensed.")
        embed = discord.Embed(title="License Information", color=discord.Color.green())
        updated = format_discord_timestamp(entry.get("UpdatedAt"), "R") if entry.get("UpdatedAt") else "Never"
        last_hwid_reset = format_discord_timestamp(entry.get("LastHwidReset"), "R") if entry.get("LastHwidReset") else "Never"
        expires_at = format_discord_timestamp(entry.get("ExpiresAt"), "R") if entry.get("ExpiresAt") else "Never"
        for name, value in [
            ("Identifier", entry.get("Identifier")),
            ("Discord ID", entry.get("DiscordId")),
            ("Rank", entry.get("Rank")),
            ("Key", f"||`{entry.get('Key')}`||"),
            ("Activated", format_discord_timestamp(entry.get("Activated"))),
            ("Executions", str(entry.get("Executions", 0))),
            ("Games", _games_links_text(entry.get("Games"))),
            ("Notes", entry.get("Notes") or "N/A"),
            ("Total HWID Resets", str(int(entry.get("totalHwidResets") or 0))),
            ("Last HWID Reset", last_hwid_reset),
            ("HWID", f"||`{entry.get('HWID')}`||" if entry.get("HWID") else "Unset"),
            ("License Enabled", "Yes" if entry.get("Enabled", True) else "No"),
            ("License Updated", updated),
            ("HWID Status", "Assigned" if entry.get("HWID") else "Unset"),
            ("Expires At", expires_at),
        ]:
            embed.add_field(name=name, value=str(value), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="fetchdupes", description="Find duplicate identifiers, Discord IDs, or keys.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def fetchdupes(self, interaction):
        users = await fetch_users()
        buckets = {"Identifier": {}, "Discord ID": {}, "License Key": {}}
        for u in users:
            for label, value in [("Identifier",u.get("Identifier")),("Discord ID",u.get("DiscordId")),("License Key",u.get("Key"))]:
                if value: buckets[label].setdefault(str(value), []).append(u)
        dupes=[]
        for label, vals in buckets.items():
            for value, entries in vals.items():
                if len(entries)>1: dupes.append(f"**{label}:** `{value}` → {', '.join(e.get('Identifier','?') for e in entries)}")
        await interaction.response.send_message("\n".join(dupes) if dupes else "No duplicates found.", ephemeral=True)

    @app_commands.command(name="viewwhitelist", description="View all license entries.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def viewwhitelist(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            users = await fetch_users()
        except Exception as e:
            return await send_error(interaction, f"Failed to load whitelist: {e}")
        if not users:
            return await send_error(interaction, "No database entries found.")
        view = WhitelistView(interaction.client, users)
        view.message = await interaction.followup.send(view=view, ephemeral=True)



async def setup(bot):
    await bot.add_cog(Whitelist(bot))


# Context-menu helpers
async def _edituser_impl(interaction, target):
    entry = await get_license_by_discord_id(str(target.id))
    if not entry: return await send_error(interaction, "That user is not licensed.")
    await interaction.response.send_modal(EditUserModal(entry))

async def _unwhitelist_impl(interaction, target):
    return await _unwhitelist_user_impl(interaction, target)

async def _fetchuser_impl(interaction, target):
    entry = await get_license_by_discord_id(str(target.id))
    if not entry: return await send_error(interaction, "That user is not licensed.")
    last_hwid_reset = format_discord_timestamp(entry.get("LastHwidReset"), "R") if entry.get("LastHwidReset") else "Never"
    updated = format_discord_timestamp(entry.get("UpdatedAt"), "R") if entry.get("UpdatedAt") else "Never"
    expires_at = format_discord_timestamp(entry.get("ExpiresAt"), "R") if entry.get("ExpiresAt") else "Never"
    embed = discord.Embed(title="License Information", color=discord.Color.green())
    for name, value in [
        ("Identifier", entry.get("Identifier")), ("Discord ID", entry.get("DiscordId")),
        ("Key", f"||`{entry.get('Key')}`||"), ("Games", _games_links_text(entry.get("Games"))),
        ("Activated", format_discord_timestamp(entry.get("Activated"))), ("Executions", str(entry.get("Executions", 0))),
        ("Rank", entry.get("Rank")), ("Notes", entry.get("Notes") or "N/A"),
        ("Total HWID Resets", str(int(entry.get("totalHwidResets") or 0))),
        ("Last HWID Reset", last_hwid_reset),
        ("HWID", f"||`{entry.get('HWID')}`||" if entry.get("HWID") else "Unset"),
        ("License Enabled", "Yes" if entry.get("Enabled", True) else "No"),
        ("License Updated", updated), ("HWID Status", "Assigned" if entry.get("HWID") else "Unset"), ("Expires At", expires_at),
    ]:
        embed.add_field(name=name, value=str(value), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


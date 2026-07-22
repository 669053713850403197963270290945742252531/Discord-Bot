import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import (
    Modal, TextInput, Label, Select, LayoutView, Container, TextDisplay,
    ActionRow, Section, Thumbnail, Button,
)

from api import config
from api.discord_helpers import (
    has_role, is_in_guild, send_success, send_error, status_layout,
)
from api.github import GitHubAPIError, fetch_users_with_sha, fetch_api_text_and_sha, commit_content, commit_users
from api.users import (
    find_user_by_discord_id, find_user_by_hwid, remove_user_by_discord_id,
    build_user_entry, revoke_buyer_role, find_removed_discord_ids,
)
from api.keys import generate_unique_key, is_valid_hwid, is_valid_discord_id
from api.time_utils import format_join_date, format_discord_timestamp, is_notes_locked

GUILD = discord.Object(id=config.GUILD_ID)

# Discord's String Select requires a fixed, predefined set of choices (max 25)
# -- edit this list to match whatever rank tiers are actually in use.
WHITELIST_RANKS = ["User", "Premium", "VIP", "Staff", "Admin", "Owner"]

# Kept as a constant so the pre-flight check in editwhitelist() can never
# silently drift out of sync with the modal's actual max_length.
EDIT_WHITELIST_MAX_LENGTH = 1900  # Discord modal input limit is 4000; kept well under that with room to spare

REGISTRATION_EMBED_TITLE = "Registration Successful"

# TODO: replace with your actual executor/HWID-script instructions
HWID_INSTRUCTIONS = (
    "You need to provide your **HWID** to register.\n\n"
    "**How to get your HWID:**\n"
    "1. Open your executor, join any game, and attach.\n"
    "2. Run the [HWID script](https://raw.githubusercontent.com/corradedied/Public-Scripts/refs/heads/main/get%20hwid.lua) and click `Copy HWID` to copy your hashed HWID.\n"
    "3. Run `/register` again with both `identifier` (what you want to be named in the script) and `hwid` filled in."
)


# =========================================================================
# /whitelist
# =========================================================================

class WhitelistModal(Modal, title="Whitelist a User"):
    identifier = Label(
        text="Identifier",
        description="Username or alias for this entry.",
        component=TextInput(placeholder="e.g. JohnDoe", max_length=100),
    )
    hwid = Label(
        text="HWID",
        description="Pre-hashed HWID in SHA-256 (64 hex characters).",
        component=TextInput(placeholder="64-character hex string", min_length=64, max_length=64),
    )
    target_user = Label(
        text="Discord User",
        description="Discord ID or @mention. Works even if they aren't in this server.",
        component=TextInput(placeholder="e.g. 123456789012345678 or <@123456789012345678>", max_length=32),
    )
    rank = Label(
        text="Rank",
        description="The rank to assign this user.",
        component=Select(
            placeholder="Select a rank...",
            min_values=1,
            max_values=1,
            required=True,
            options=[discord.SelectOption(label=r) for r in WHITELIST_RANKS],
        ),
    )
    notes = Label(
        text="Notes",
        description="Optional notes to keep reminders about this user.",
        component=TextInput(style=discord.TextStyle.paragraph, placeholder="Leave blank for none", required=False, max_length=500),
    )

    def __init__(self, target: Optional[discord.Member] = None):
        if target is not None:
            super().__init__(title=f"Whitelist {target.display_name}"[:45])
        else:
            super().__init__()
        if target is not None:
            # Pre-fill from the "Whitelist User" context menu command so the
            # already-known target doesn't need to be re-typed; still
            # editable in case the wrong user was right-clicked.
            self.target_user.component.default = str(target.id)

    async def on_submit(self, interaction: discord.Interaction):
        identifier = self.identifier.component.value.strip()
        hwid = self.hwid.component.value.strip()
        rank = self.rank.component.values[0]
        notes = (self.notes.component.value or "").strip() or None

        raw_target = self.target_user.component.value.strip()
        mention_match = re.fullmatch(r"<@!?(\d{17,20})>", raw_target)
        discord_id = mention_match.group(1) if mention_match else raw_target

        if not is_valid_discord_id(discord_id):
            return await send_error(
                interaction,
                "Invalid Discord User. Enter a valid Discord ID or @mention "
                "(e.g. `123456789012345678` or `<@123456789012345678>`) -- this works "
                "even if the user isn't in this server.",
            )
        mention = f"<@{discord_id}>"

        if not is_valid_hwid(hwid):
            return await send_error(interaction, "Invalid HWID format. Must be 64 hex characters (SHA-256).")

        await interaction.response.defer(ephemeral=True)

        try:
            users, sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        existing = find_user_by_discord_id(users, discord_id)
        if existing:
            return await send_error(interaction, f"{mention} is already whitelisted as **{existing.get('Identifier', 'Unknown')}**.")

        existing = find_user_by_hwid(users, hwid)
        if existing:
            return await send_error(interaction, f"This HWID is already whitelisted under **{existing.get('Identifier', 'Unknown')}** (<@{existing.get('DiscordId')}>).")

        generated_key = generate_unique_key(users)

        try:
            users.append(build_user_entry(hwid, identifier, rank, discord_id, generated_key, notes))
            await commit_users(users, sha, f"Whitelist user: {identifier} ({discord_id})")
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        await send_success(
            interaction,
            f"**{identifier}** ({mention}) has been whitelisted.",
            fields=[("HWID", f"||`{hwid}`||", False)],
        )


# =========================================================================
# /unwhitelist
# =========================================================================

async def _unwhitelist_impl(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    discord_id = str(user.id)

    try:
        users, sha = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    filtered, removed = remove_user_by_discord_id(users, discord_id)
    if not removed:
        return await send_error(interaction, f"{user.mention} was not found in database.")

    try:
        await commit_users(filtered, sha, f"Unwhitelist user: {discord_id}")
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    await revoke_buyer_role(interaction.guild, discord_id)
    await send_success(interaction, f"{user.mention} has been removed from the whitelist.")


# =========================================================================
# /editwhitelist
# =========================================================================

class EditWhitelistModal(Modal):
    def __init__(self, initial_json: str):
        super().__init__(title="Edit Whitelist JSON")
        self.json_input = TextInput(
            label="Whitelist JSON",
            style=discord.TextStyle.paragraph,
            default=initial_json,
            max_length=EDIT_WHITELIST_MAX_LENGTH,
        )
        self.add_item(self.json_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_content = self.json_input.value.strip()

        try:
            new_users = json.loads(new_content)
        except json.JSONDecodeError as e:
            return await send_error(interaction, f"Invalid JSON: {e}")

        # Fetch the latest content (not just the sha) to avoid race
        # conditions on the commit *and* to check the Notes-lock guard below
        # against genuinely current data, not whatever this modal happened
        # to be pre-filled with when it was opened.
        try:
            current_users, sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        # Block this edit from silently overwriting/clearing the Notes field
        # of any entry that's currently temporarily whitelisted -- same
        # guard as /edituser, /clearnotes, and the Edit User button, just
        # applied here across every entry in the pasted JSON at once. An
        # entry being removed entirely (a legitimate unwhitelist-style edit)
        # is fine; only a *changed* Notes value on an entry that still
        # exists is blocked.
        if isinstance(new_users, list):
            locked_violations = []
            for old_entry in current_users:
                if not is_notes_locked(old_entry):
                    continue
                discord_id = old_entry.get("DiscordId")
                new_entry = find_user_by_discord_id(new_users, discord_id)
                if new_entry is not None and (new_entry.get("Notes") or None) != (old_entry.get("Notes") or None):
                    locked_violations.append(f"<@{discord_id}> ({old_entry.get('Identifier', 'Unknown')})")

            if locked_violations:
                return await send_error(
                    interaction,
                    "This edit changes the Notes field of a currently temporarily whitelisted user, which "
                    "isn't allowed -- Notes stores the auto-removal timestamp the temp-whitelist system "
                    "relies on. Remove those changes and resubmit.",
                    fields=[("Affected users", ", ".join(locked_violations), False)],
                )

        try:
            await commit_content(new_content, sha, f"Edit whitelist by {interaction.user}")
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        for discord_id in find_removed_discord_ids(current_users, new_users):
            await revoke_buyer_role(interaction.guild, discord_id)

        await send_success(interaction, "Whitelist updated successfully.")


# =========================================================================
# /edituser
# =========================================================================

class EditUserCommandModal(Modal):
    """Multi-field edit modal opened by /edituser. Unlike WhitelistModal (a
    brand new, always-empty entry), this needs to be pre-filled with the
    target's *current* values, which aren't known until the command runs --
    so the Label-wrapped fields are built per-instance in __init__ and added
    with add_item(), rather than declared as static class attributes.

    Discord caps modals at 5 top-level components, so JoinDate and Key are
    intentionally left out (same 5 fields /whitelist itself asks for);
    those can still be changed via /editwhitelist or the Edit User button
    on /viewwhitelist.
    """

    def __init__(self, user_entry: Dict[str, Any]):
        title = f"Edit {user_entry.get('Identifier', 'User')}"
        if len(title) > 45:
            title = title[:42] + "..."
        super().__init__(title=title)

        # Stored so on_submit can tell "untouched" from "deliberately
        # changed" -- see the HWID check below.
        self.original_discord_id = str(user_entry.get("DiscordId", ""))
        self.original_hwid = (user_entry.get("HWID") or "").strip()

        self.identifier = Label(
            text="Identifier",
            description="Username or alias for this entry.",
            component=TextInput(default=(user_entry.get("Identifier") or "")[:100], placeholder="e.g. JohnDoe", max_length=100),
        )
        self.discord_user = Label(
            text="Discord User",
            description="Discord ID or @mention. Works even if not in server.",
            component=TextInput(default=self.original_discord_id[:32], placeholder="e.g. 123456789012345678 or <@123...>", max_length=32),
        )
        self.rank = Label(
            text="Rank",
            description="The rank to assign this user.",
            component=TextInput(default=(user_entry.get("Rank") or "")[:50], placeholder="e.g. VIP", max_length=50),
        )
        self.hwid = Label(
            text="HWID",
            description="Pre-hashed HWID in SHA-256 (64 hex characters).",
            # No min_length here (unlike /whitelist's HWID field) -- some
            # existing entries may not hold a strict 64-char value, and a
            # `default` that violates the field's own min/max length makes
            # Discord reject opening the modal entirely. Correctness is
            # instead enforced in on_submit.
            component=TextInput(default=self.original_hwid[:100], placeholder="64-character hex string", max_length=100),
        )
        self.notes = Label(
            text="Notes",
            description="Optional notes to keep reminders about this user.",
            component=TextInput(style=discord.TextStyle.paragraph, default=(user_entry.get("Notes") or "")[:500], placeholder="Leave blank for none", required=False, max_length=500),
        )

        for field in (self.identifier, self.discord_user, self.rank, self.hwid, self.notes):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        identifier = self.identifier.component.value.strip()
        rank = self.rank.component.value.strip()
        hwid = self.hwid.component.value.strip()
        notes = (self.notes.component.value or "").strip() or None

        raw_target = self.discord_user.component.value.strip()
        mention_match = re.fullmatch(r"<@!?(\d{17,20})>", raw_target)
        discord_id = mention_match.group(1) if mention_match else raw_target

        if not is_valid_discord_id(discord_id):
            return await send_error(
                interaction,
                "Invalid Discord User. Enter a valid Discord ID or @mention "
                "(e.g. `123456789012345678` or `<@123456789012345678>`).",
            )

        # Only enforce the HWID format if it was actually changed, so a
        # legacy/malformed value left untouched doesn't block edits to the
        # other fields.
        if hwid != self.original_hwid and not is_valid_hwid(hwid):
            return await send_error(interaction, "Invalid HWID format. Must be 64 hex characters (SHA-256).")

        mention = f"<@{discord_id}>"
        await interaction.response.defer(ephemeral=True)

        try:
            users, sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        entry = find_user_by_discord_id(users, self.original_discord_id)
        if not entry:
            return await send_error(interaction, "This user's whitelist entry no longer exists (it may have been removed by someone else).")

        if discord_id != self.original_discord_id:
            collision = find_user_by_discord_id(users, discord_id)
            if collision and collision is not entry:
                return await send_error(interaction, f"{mention} is already whitelisted as **{collision.get('Identifier', 'Unknown')}**.")

        if hwid != self.original_hwid:
            collision = find_user_by_hwid(users, hwid)
            if collision and collision is not entry:
                return await send_error(interaction, f"This HWID is already whitelisted under **{collision.get('Identifier', 'Unknown')}** (<@{collision.get('DiscordId')}>).")

        if is_notes_locked(entry) and notes != (entry.get("Notes") or None):
            return await send_error(
                interaction,
                f"{mention}'s Notes field can't be changed right now -- they're currently temporarily "
                "whitelisted, and Notes stores the auto-removal timestamp the temp-whitelist system "
                "relies on. It'll unlock once the temporary whitelist expires or is removed.",
            )

        entry["Identifier"] = identifier
        entry["DiscordId"] = discord_id
        entry["Rank"] = rank
        entry["HWID"] = hwid
        entry["Notes"] = notes

        try:
            await commit_users(users, sha, f"Edited whitelist user: {identifier} ({discord_id})")
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        await send_success(
            interaction,
            f"**{identifier}** ({mention}) has been updated.",
            fields=[("HWID", f"||`{hwid}`||", False)],
        )


async def _edituser_impl(interaction: discord.Interaction, user: discord.User):
    try:
        users, _sha = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    user_entry = find_user_by_discord_id(users, user.id)
    if not user_entry:
        return await send_error(interaction, f"User {user.mention} not found in whitelist.")

    await interaction.response.send_modal(EditUserCommandModal(user_entry))


# =========================================================================
# /fetchuser
# =========================================================================

async def _fetchuser_impl(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)

    try:
        users, _ = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    user_data = find_user_by_discord_id(users, user.id)
    if not user_data:
        return await send_error(interaction, f"No data found for {user.mention}.")

    guild = interaction.client.get_guild(config.GUILD_ID)
    member = guild.get_member(user.id) if guild else None

    num_roles = len(member.roles) - 1 if member else "Unknown"

    if member and member.joined_at:
        join_ts = int(member.joined_at.replace(tzinfo=timezone.utc).timestamp())
        server_join_display = f"<t:{join_ts}:D>"
    else:
        server_join_display = "Unknown"

    embed = discord.Embed(title=f"User Info: {user.name}", color=discord.Color.teal(), timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=user.display_avatar.url)

    join_date_display = format_discord_timestamp(user_data.get("JoinDate", "Unknown"))

    fields = [
        ("Identifier", user_data.get("Identifier")),
        ("Rank", user_data.get("Rank")),
        ("Join Date", join_date_display),
        ("HWID", f"||{user_data.get('HWID')}||" if user_data.get("HWID") else "N/A"),
        ("Key", f"||{user_data.get('Key')}||" if user_data.get("Key") else "N/A"),
        ("Last HWID Reset", format_discord_timestamp(user_data.get("LastHwidReset"))),
        ("Total HWID Resets", str(user_data.get("totalHwidResets", 0))),
        ("Discord ID", f"{user_data.get('DiscordId')} ({user.mention})"),
        ("Server Join Date", server_join_display),
        ("Number of Roles", str(num_roles)),
    ]

    if user_data.get("Notes") and user_data["Notes"] != "false":
        fields.append(("Notes", user_data["Notes"]))

    for name, value in fields:
        embed.add_field(name=name, value=value or "N/A", inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)


# =========================================================================
# /viewwhitelist
# =========================================================================

class EditUserModal(Modal):
    def __init__(self, user_data, whitelist_view: "WhitelistView"):
        super().__init__(title=f"Edit {user_data.get('Identifier', 'User')}")

        self.user_data = user_data
        self.whitelist_view = whitelist_view

        self.identifier = TextInput(label="Identifier", default=user_data.get("Identifier", ""), required=True)
        self.rank = TextInput(label="Rank", default=user_data.get("Rank", ""), required=True)
        self.hwid = TextInput(label="HWID", default=user_data.get("HWID", ""), required=False)
        self.key = TextInput(label="Key", default=user_data.get("Key", ""), required=False)
        self.notes = TextInput(label="Notes", default=user_data.get("Notes") or "", style=discord.TextStyle.paragraph, required=False)

        for field in (self.identifier, self.rank, self.hwid, self.key, self.notes):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        new_notes = self.notes.value or None
        discord_id = self.user_data.get("DiscordId")

        try:
            existing, sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        # Re-fetched fresh (rather than trusting the possibly-stale
        # self.user_data this modal was opened with) so the Notes-lock check
        # below can't be bypassed by data that's gone stale since the
        # whitelist view was last built/refreshed.
        entry = find_user_by_discord_id(existing, discord_id)
        if not entry:
            return await send_error(interaction, "This user's whitelist entry no longer exists (it may have been removed by someone else).")

        if is_notes_locked(entry) and new_notes != (entry.get("Notes") or None):
            return await send_error(
                interaction,
                f"**{entry.get('Identifier', 'This user')}**'s Notes field can't be changed right now -- "
                "they're currently temporarily whitelisted, and Notes stores the auto-removal timestamp "
                "the temp-whitelist system relies on. It'll unlock once the temporary whitelist expires "
                "or is removed.",
            )

        self.user_data["Identifier"] = self.identifier.value
        self.user_data["Rank"] = self.rank.value
        self.user_data["HWID"] = self.hwid.value or "N/A"
        self.user_data["Key"] = self.key.value or "N/A"
        self.user_data["Notes"] = new_notes

        try:
            for i, u in enumerate(existing):
                if u.get("DiscordId") == discord_id:
                    existing[i] = self.user_data
                    break
            await commit_users(existing, sha, f"Edited whitelist user: {self.user_data.get('Identifier', 'N/A')} ({discord_id})")
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        # Same page they were editing on -- just refresh the entry's own data.
        self.whitelist_view.users = existing
        self.whitelist_view.pending_notice = f"✅ User **{self.user_data.get('Identifier')}** updated."
        await self.whitelist_view.build()
        await interaction.response.edit_message(view=self.whitelist_view)


class DeleteUserConfirmView(LayoutView):
    """Components V2 confirmation prompt shown in place of the whitelist
    entry when 'Delete User' is pressed. Confirm applies the delete and
    returns to WhitelistView on the same page (clamped if that was the
    last entry); Cancel returns to WhitelistView unchanged."""

    def __init__(self, whitelist_view: "WhitelistView"):
        super().__init__(timeout=60)
        self.whitelist_view = whitelist_view

        user_data = whitelist_view.users[whitelist_view.current_index]
        self.identifier = user_data.get("Identifier", "N/A")
        self.discord_id = user_data.get("DiscordId")

        container = Container(
            TextDisplay("### ⚠️ Delete Whitelist Entry"),
            TextDisplay(
                f"Are you sure you want to delete **{self.identifier}** "
                f"(`{self.discord_id}`) from the whitelist? This action cannot be undone."
            ),
            accent_color=discord.Color.red(),
        )

        row = ActionRow()
        confirm_button = Button(label="Confirm Delete", style=discord.ButtonStyle.danger)
        confirm_button.callback = self.confirm
        row.add_item(confirm_button)

        cancel_button = Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self.cancel
        row.add_item(cancel_button)

        container.add_item(row)
        self.add_item(container)

    async def confirm(self, interaction: discord.Interaction):
        view = self.whitelist_view

        try:
            existing, sha = await fetch_users_with_sha()
            existing, _ = remove_user_by_discord_id(existing, self.discord_id)
            await commit_users(existing, sha, f"Deleted whitelist user: {self.identifier} ({self.discord_id})")
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        await revoke_buyer_role(interaction.guild, self.discord_id)

        view.users = existing
        if view.current_index >= len(view.users):
            view.current_index = max(0, len(view.users) - 1)

        view.pending_notice = f"🗑️ Deleted user **{self.identifier}**."
        await view.build()
        await interaction.response.edit_message(view=view)

    async def cancel(self, interaction: discord.Interaction):
        await self.whitelist_view.build()
        await interaction.response.edit_message(view=self.whitelist_view)


class WhitelistView(LayoutView):
    """Components V2 paginated whitelist browser. The 'embed' (entry details
    + avatar) and the Previous/Next/Edit/Delete/Refresh buttons all live
    inside a single Container; Previous/Next only appear when there's more
    than one entry."""

    def __init__(self, bot, users, current_index=0):
        super().__init__(timeout=None)
        self.bot = bot
        self.users = users
        self.current_index = current_index
        self.pending_notice: Optional[str] = None

        self.prev_button = Button(label="⏮️ Previous", style=discord.ButtonStyle.secondary)
        self.next_button = Button(label="⏭️ Next", style=discord.ButtonStyle.secondary)
        self.edit_button = Button(label="✏️ Edit User", style=discord.ButtonStyle.primary)
        self.delete_button = Button(label="🗑️ Delete User", style=discord.ButtonStyle.danger)
        self.refresh_button = Button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)

        self.prev_button.callback = self.on_prev
        self.next_button.callback = self.on_next
        self.edit_button.callback = self.on_edit
        self.delete_button.callback = self.on_delete
        self.refresh_button.callback = self.on_refresh

    def update_button_states(self):
        self.prev_button.disabled = self.current_index == 0
        self.next_button.disabled = self.current_index >= len(self.users) - 1

    async def _thumbnail_url(self, user_data) -> str:
        discord_id = int(user_data.get("DiscordId", 0))
        try:
            member = await self.bot.fetch_user(discord_id)
            return member.display_avatar.url
        except Exception:
            return "https://cdn.discordapp.com/embed/avatars/0.png"

    def _fields_text(self, user_data) -> str:
        lines = [
            f"**Identifier:** {user_data.get('Identifier', 'N/A')}",
            f"**Rank:** {user_data.get('Rank', 'N/A')}",
            f"**Join Date:** {format_discord_timestamp(user_data.get('JoinDate', 'N/A'))}",
            f"**HWID:** ||`{user_data.get('HWID', '')}`||",
            f"**Key:** ||`{user_data.get('Key', '')}`||",
            f"**Last HWID Reset:** {format_discord_timestamp(user_data.get('LastHwidReset'))}",
            f"**Total HWID Resets:** {user_data.get('totalHwidResets', 0)}",
        ]
        notes = user_data.get("Notes")
        if notes is not None and notes != "false" and notes.strip() != "":
            lines.append(f"**Notes:** {notes}")
        return "\n".join(lines)

    async def build(self):
        """(Re)builds this view's components from current state. Call after
        any state change, then edit_message/followup.send(view=self)."""
        self.clear_items()

        if self.pending_notice:
            self.add_item(Container(TextDisplay(f"### {self.pending_notice}"), accent_color=discord.Color.green()))
            self.pending_notice = None

        if not self.users:
            empty_container = Container(TextDisplay("### Database is empty"), accent_color=discord.Color.red())
            empty_container.add_item(ActionRow(self.refresh_button))
            self.add_item(empty_container)
            return

        user_data = self.users[self.current_index]
        header = TextDisplay(f"### Whitelist Entry {self.current_index + 1}/{len(self.users)}")
        fields = TextDisplay(self._fields_text(user_data))
        thumbnail = Thumbnail(await self._thumbnail_url(user_data))
        section = Section(header, fields, accessory=thumbnail)

        self.update_button_states()
        row = ActionRow()
        if len(self.users) > 1:
            row.add_item(self.prev_button)
            row.add_item(self.next_button)
        row.add_item(self.edit_button)
        row.add_item(self.delete_button)
        row.add_item(self.refresh_button)

        container = Container(section, accent_color=discord.Color.blue())
        container.add_item(row)
        self.add_item(container)

    async def on_prev(self, interaction: discord.Interaction):
        self.current_index = max(0, self.current_index - 1)
        await self.build()
        await interaction.response.edit_message(view=self)

    async def on_next(self, interaction: discord.Interaction):
        self.current_index = min(len(self.users) - 1, self.current_index + 1)
        await self.build()
        await interaction.response.edit_message(view=self)

    async def on_edit(self, interaction: discord.Interaction):
        user_data = self.users[self.current_index]
        await interaction.response.send_modal(EditUserModal(user_data, self))

    async def on_delete(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=DeleteUserConfirmView(self))

    async def on_refresh(self, interaction: discord.Interaction):
        try:
            users, _sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        self.users = users
        if self.current_index >= len(self.users):
            self.current_index = max(0, len(self.users) - 1)

        self.pending_notice = "🔄 Whitelist refreshed."
        await self.build()
        await interaction.response.edit_message(view=self)


# =========================================================================
# /register, /hwidhelp, /checkregistration, /clearregistrations
# =========================================================================

class ConfirmClearLayout(LayoutView):
    """Components V2 confirmation prompt: the title/description ('embed')
    and the Confirm/Cancel buttons live in the *same* Container."""

    def __init__(self, author_id: int, channel: discord.abc.GuildChannel):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.confirmed: Optional[bool] = None

        self.container = Container(
            TextDisplay("### ⚠️ Clear Registrations"),
            TextDisplay(
                f"Are you sure you want to clear all registration entries in {channel.mention}? "
                "Other messages in the channel will be left untouched. This action cannot be undone."
            ),
            accent_color=discord.Color.blurple(),
        )

        action_row = ActionRow()
        confirm_button = Button(label="Confirm Clear", style=discord.ButtonStyle.danger)
        confirm_button.callback = self.confirm
        action_row.add_item(confirm_button)

        cancel_button = Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self.cancel
        action_row.add_item(cancel_button)

        self.container.add_item(action_row)
        self.add_item(self.container)

    async def on_timeout(self):
        self.confirmed = None

    async def confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await send_error(interaction, "You cannot confirm this action.")

        self.confirmed = True
        self.stop()
        await interaction.response.edit_message(
            view=status_layout("Clearing Registrations", "Clearing registrations...", discord.Color.blurple())
        )

    async def cancel(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await send_error(interaction, "You cannot cancel this action.")

        self.confirmed = False
        self.stop()
        await interaction.response.defer()
        await interaction.delete_original_response()


class Whitelist(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="whitelist", description="Adds a user to the database.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def whitelist(self, interaction: discord.Interaction):
        await interaction.response.send_modal(WhitelistModal())

    @app_commands.command(name="unwhitelist", description="Removes a user from the database.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="Discord user to remove from the database.")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def unwhitelist(self, interaction: discord.Interaction, user: discord.User):
        await _unwhitelist_impl(interaction, user)

    @app_commands.command(name="editwhitelist", description="Edits the database JSON directly.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def editwhitelist(self, interaction: discord.Interaction):
        try:
            decoded, _sha = await fetch_api_text_and_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        # A modal's TextInput can't pre-fill more characters than its own
        # max_length allows -- if `decoded` is longer than that, Discord
        # rejects send_modal() outright. Catch that up front with a clear
        # message instead of letting the raw HTTPException surface.
        if len(decoded) > EDIT_WHITELIST_MAX_LENGTH:
            return await send_error(
                interaction,
                f"The whitelist JSON is {len(decoded):,} characters, which is too long to "
                f"load into this modal (Discord caps modal text fields at "
                f"{EDIT_WHITELIST_MAX_LENGTH:,} here). Use `/edituser` to change a single "
                "field, or `/export` to pull the full file and edit it directly on GitHub.",
            )

        await interaction.response.send_modal(EditWhitelistModal(decoded))

    @app_commands.command(name="edituser", description="Edits a whitelisted user's info.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="User to edit")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def edituser(self, interaction: discord.Interaction, user: discord.User):
        await _edituser_impl(interaction, user)

    @app_commands.command(name="fetchuser", description="Fetches all stored info about a user.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="The user to look up")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def fetchuser(self, interaction: discord.Interaction, user: discord.User):
        await _fetchuser_impl(interaction, user)

    @app_commands.command(name="fetchdupes", description="Find duplicate values in the whitelist.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(field="The field to search for duplicates in")
    @app_commands.choices(field=[
        app_commands.Choice(name="HWID", value="HWID"),
        app_commands.Choice(name="Identifier", value="Identifier"),
        app_commands.Choice(name="Rank", value="Rank"),
        app_commands.Choice(name="Discord ID", value="DiscordId"),
        app_commands.Choice(name="Key", value="Key"),
        app_commands.Choice(name="All", value="All"),
    ])
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def fetchdupes(self, interaction: discord.Interaction, field: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)

        try:
            users, _ = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        if field.value == "All":
            fields_to_check = ["HWID", "Identifier", "Rank", "DiscordId", "Key"]
            dupes_all = {}

            for fname in fields_to_check:
                value_map = defaultdict(list)
                for entry in users:
                    value = entry.get(fname)
                    if not value or value == "false":
                        continue
                    value_map[value].append(entry)
                dupes = {k: v for k, v in value_map.items() if len(v) > 1}
                if dupes:
                    dupes_all[fname] = dupes

            if not dupes_all:
                return await send_error(interaction, "No duplicates found in any fields.")

            embed = discord.Embed(title="🔁 Duplicate Entries: All Fields", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
            for fname, dupes in dupes_all.items():
                embed.add_field(name=f"Field: {fname}", value="—", inline=False)
                for value, entries in dupes.items():
                    identifiers = ", ".join(entry.get("Identifier", "Unknown") for entry in entries)
                    value_display = f"`{value}`" if len(value) <= 50 else f"`{value[:47]}...`"
                    embed.add_field(name=value_display, value=f"Count: `{len(entries)}` — {identifiers}", inline=False)

        else:
            value_map = defaultdict(list)
            for entry in users:
                value = entry.get(field.value)
                if not value or value == "false":
                    continue
                value_map[value].append(entry)

            dupes = {k: v for k, v in value_map.items() if len(v) > 1}
            if not dupes:
                return await send_error(interaction, f"No duplicates found for **{field.value}**.")

            embed = discord.Embed(title=f"🔁 Duplicate Entries: `{field.value}`", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
            for value, entries in dupes.items():
                identifiers = ", ".join(entry.get("Identifier", "Unknown") for entry in entries)
                value_display = f"`{value}`" if len(value) <= 50 else f"`{value[:47]}...`"
                embed.add_field(name=value_display, value=f"Count: `{len(entries)}` — {identifiers}", inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="viewwhitelist", description="View all whitelist entries.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def viewwhitelist(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            users, _sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        if not users:
            return await send_error(interaction, "No database entries found.")

        view = WhitelistView(interaction.client, users)
        await view.build()
        await interaction.followup.send(view=view, ephemeral=True)

    @app_commands.command(name="register", description="Submit your info to be reviewed and whitelisted.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(identifier="Your identifier (username, alias, etc.)", hwid="Pre-hashed HWID in SHA-256, obtained from the executor")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def register(self, interaction: discord.Interaction, identifier: str, hwid: str):
        await interaction.response.defer(ephemeral=True)
        discord_id_str = str(interaction.user.id)

        if not is_valid_hwid(hwid):
            return await send_error(interaction, "Invalid HWID format. Must be 64 hex characters (SHA-256).")

        try:
            whitelist_users, _ = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        if find_user_by_discord_id(whitelist_users, discord_id_str):
            return await send_error(interaction, "You are already whitelisted.")

        reg_channel = interaction.client.get_channel(config.REGISTRATION_CHANNEL_ID)
        if not reg_channel:
            return await send_error(interaction, "Registration channel not found.")

        messages = [msg async for msg in reg_channel.history(limit=100)]
        for msg in messages:
            if msg.embeds:
                embed = msg.embeds[0]
                for field in embed.fields:
                    if discord_id_str in field.value:
                        return await send_error(interaction, "You have already registered before.")

        rank = "User"
        join_date = format_join_date()

        embed = discord.Embed(title=REGISTRATION_EMBED_TITLE, color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="Identifier", value=identifier, inline=True)
        embed.add_field(name="Rank", value=rank, inline=True)
        embed.add_field(name="Discord ID", value=discord_id_str, inline=True)
        embed.add_field(name="Join Date", value=format_discord_timestamp(join_date), inline=True)
        embed.add_field(name="HWID", value=f"||`{hwid}`||", inline=False)
        await reg_channel.send(embed=embed)

        await send_success(
            interaction,
            "Registration completed.",
            fields=[
                ("Identifier", identifier, True),
                ("Rank", rank, True),
                ("Discord ID", discord_id_str, True),
                ("Join Date", format_discord_timestamp(join_date), True),
                ("HWID", f"||`{hwid}`||", False),
            ],
        )

    @app_commands.command(name="hwidhelp", description="Shows instructions for getting your HWID.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def hwidhelp(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(title="🔑 HWID Required", description=HWID_INSTRUCTIONS, color=discord.Color.orange())
        await interaction.edit_original_response(embed=embed)

    @app_commands.command(name="checkregistration", description="Checks if a user is registered.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="The user to check registration for")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def checkregistration(self, interaction: discord.Interaction, user: discord.User):
        await interaction.response.defer(ephemeral=True)
        discord_id_str = str(user.id)

        try:
            users, _ = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        whitelist_registered = find_user_by_discord_id(users, discord_id_str) is not None

        reg_channel = interaction.client.get_channel(config.REGISTRATION_CHANNEL_ID)
        if not reg_channel:
            return await send_error(interaction, "Registration channel not found.")

        registered_in_channel = False
        registration_message_url = None
        try:
            async for msg in reg_channel.history(limit=100):
                if msg.embeds:
                    embed = msg.embeds[0]
                    for field in embed.fields:
                        if discord_id_str in field.value:
                            registered_in_channel = True
                            registration_message_url = msg.jump_url
                            break
                if registered_in_channel:
                    break
        except Exception as e:
            return await send_error(interaction, f"Error reading registration embeds: {e}")

        if whitelist_registered and registered_in_channel:
            status_msg = f"User **{user}** is **registered** in both whitelist and registration channel.\n[View Registration Message]({registration_message_url})"
        elif whitelist_registered:
            status_msg = f"User **{user}** is **registered** in the whitelist only."
        elif registered_in_channel:
            status_msg = f"User **{user}** is **registered** in the registration channel only.\n[View Registration Message]({registration_message_url})"
        else:
            status_msg = f"User **{user}** is **not** registered."

        if whitelist_registered or registered_in_channel:
            await send_success(interaction, status_msg)
        else:
            await send_error(interaction, status_msg)

    @app_commands.command(name="clearregistrations", description="Clear all messages in the registration channel.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def clearregistrations(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        reg_channel = interaction.client.get_channel(config.REGISTRATION_CHANNEL_ID)
        if not reg_channel:
            return await send_error(interaction, "Registration channel not found.")

        permissions = reg_channel.permissions_for(interaction.guild.me)
        if not permissions.manage_messages:
            return await send_error(interaction, "I need Manage Messages permission in the registration channel to clear messages.")

        view = ConfirmClearLayout(interaction.user.id, reg_channel)
        message = await interaction.followup.send(view=view, ephemeral=True)

        await view.wait()
        if not view.confirmed:
            return  # User cancelled (message deleted by the button) or timed out

        # Find registration messages only (leave any other channel messages untouched)
        to_delete = []
        last_message = None
        try:
            while True:
                batch = [msg async for msg in reg_channel.history(limit=100, before=last_message)]
                if not batch:
                    break
                last_message = batch[-1]
                for msg in batch:
                    if msg.embeds and msg.embeds[0].title == REGISTRATION_EMBED_TITLE:
                        to_delete.append(msg)
                if len(batch) < 100:
                    break
        except Exception as e:
            return await message.edit(view=status_layout("Scan Failed", f"Failed to scan registration messages: {e}", discord.Color.red()))

        deleted_count = 0
        try:
            for i in range(0, len(to_delete), 100):
                chunk = to_delete[i:i + 100]
                await reg_channel.delete_messages(chunk)
                deleted_count += len(chunk)
        except Exception as e:
            return await message.edit(view=status_layout("Clear Failed", f"Failed to clear messages: {e}", discord.Color.red()))

        await message.edit(view=status_layout("✅ Registrations Cleared", f"Cleared {deleted_count} registrations.", discord.Color.green()))

    @app_commands.command(name="clearnotes", description="Clears the notes field for a user in the GitHub whitelist JSON.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="The user whose notes to clear")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def clearnotes(self, interaction: discord.Interaction, user: discord.User):
        await _clearnotes_impl(interaction, user)


async def _clearnotes_impl(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)

    try:
        users, sha = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    discord_id_str = str(user.id)
    entry = find_user_by_discord_id(users, discord_id_str)
    if not entry:
        return await send_error(interaction, f"No user found with Discord ID {user.mention}.")

    if is_notes_locked(entry):
        return await send_error(
            interaction,
            f"{user.mention}'s Notes field can't be cleared right now -- they're currently temporarily "
            "whitelisted, and Notes stores the auto-removal timestamp the temp-whitelist system relies "
            "on. It'll unlock once the temporary whitelist expires or is removed.",
        )

    entry["Notes"] = None

    try:
        await commit_users(users, sha, f"Cleared notes for user: {user} ({discord_id_str})")
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    await send_success(interaction, f"Notes cleared for {user.mention}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Whitelist(bot))

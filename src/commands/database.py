import base64
import csv
import io
import json
from datetime import datetime, timezone
from typing import List

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import LayoutView, Container, TextDisplay, ActionRow, Button, File as UIFile

from api import config
from api.discord_helpers import (
    has_role, is_in_guild, send_error, file_success_layout, status_layout,
    build_unified_diff, default_ui_error,
)
from api.alerts import send_alert, alert_embed, ALERT_COLOR_CAUTION
from api.github import (
    GitHubAPIError, fetch_raw_text, fetch_api_text_and_sha, fetch_api_file,
    commit_content, fetch_users_with_sha, list_commits, get_commit,
)
from api.users import find_removed_discord_ids, revoke_buyer_role
from api.time_utils import format_discord_timestamp

GUILD = discord.Object(id=config.GUILD_ID)

# Maps Users.json's PascalCase keys to the snake_case column names
# /bulkwhitelist's CSV parser looks for (identifier, hwid, discord_id, rank,
# notes -- see BULK_WHITELIST_REQUIRED_COLUMNS in whitelist.py). Used by
# /export's CSV branch so the exported file can be edited and fed straight
# back into /bulkwhitelist. Every key here matches its required column
# case-insensitively except DiscordId, which /bulkwhitelist wouldn't
# recognize at all without this remap (it lowercases to "discordid", not
# "discord_id"). The remaining keys aren't columns /bulkwhitelist reads, but
# are still normalized to snake_case for consistency in the exported file.
CSV_EXPORT_COLUMN_MAP = {
    "Identifier": "identifier",
    "HWID": "hwid",
    "DiscordId": "discord_id",
    "Rank": "rank",
    "Activated": "activated",
    "Key": "key",
    "Notes": "notes",
    "LastHwidReset": "last_hwid_reset",
    "totalHwidResets": "total_hwid_resets",
    "Executions": "executions",
}


class DbSearchView(LayoutView):
    """Components V2 paginated view for /dbsearch. The 'embed' (title +
    fields) and the Previous/Next buttons live inside a single Container;
    the buttons are only added when there's more than one match."""

    def __init__(self, matches, current_index=0):
        super().__init__(timeout=300)
        self.matches = matches
        self.current_index = current_index

        self.header = TextDisplay("")
        self.fields = TextDisplay("")

        self.prev_button = Button(label="⏮️ Previous", style=discord.ButtonStyle.secondary)
        self.next_button = Button(label="⏭️ Next", style=discord.ButtonStyle.secondary)
        self.prev_button.callback = self.on_prev
        self.next_button.callback = self.on_next

        container = Container(self.header, self.fields, accent_color=discord.Color.green())
        if len(self.matches) > 1:
            container.add_item(ActionRow(self.prev_button, self.next_button))

        self.add_item(container)
        self.refresh_content()

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await default_ui_error(interaction, error, item, label="DbSearchView")

    def update_button_states(self):
        self.prev_button.disabled = self.current_index == 0
        self.next_button.disabled = self.current_index >= len(self.matches) - 1

    def refresh_content(self):
        user = self.matches[self.current_index]
        self.header.content = f"### Search Result {self.current_index + 1}/{len(self.matches)}"

        discord_id = user.get("DiscordId", "N/A")
        mention = f"<@{discord_id}>" if isinstance(discord_id, str) and discord_id.isdigit() else "N/A"
        lines = [
            f"**Identifier:** {user.get('Identifier', 'N/A')}",
            f"**Rank:** {user.get('Rank', 'N/A')}",
            f"**Discord ID:** {discord_id} ({mention})",
            f"**HWID:** ||`{user.get('HWID', '')}`||",
            f"**Key:** ||`{user.get('Key', '')}`||",
            f"**Last HWID Reset:** {format_discord_timestamp(user.get('LastHwidReset'))}",
            f"**Total HWID Resets:** {user.get('totalHwidResets', 0)}",
            f"**Executions:** {user.get('Executions', 0)}",
        ]
        notes = user.get("Notes")
        if notes and notes != "false" and notes.strip() != "":
            lines.append(f"**Notes:** {notes}")
        self.fields.content = "\n".join(lines)

        self.update_button_states()

    async def on_prev(self, interaction: discord.Interaction):
        self.current_index = max(0, self.current_index - 1)
        self.refresh_content()
        await interaction.response.edit_message(view=self)

    async def on_next(self, interaction: discord.Interaction):
        self.current_index = min(len(self.matches) - 1, self.current_index + 1)
        self.refresh_content()
        await interaction.response.edit_message(view=self)


class RollbackResultView(LayoutView):
    """
    Components V2 layout for a successful /rollback. The diff is the
    biggest (and often least-needed-at-a-glance) part of the response --
    and can contain sensitive fields like HWIDs/keys -- so it starts
    hidden behind a "Show Diff" button instead of being dumped straight
    into the response the way /diff's is. Clicking it edits this same
    message in place to reveal the diff, rather than posting it as a
    separate followup message.

    Only ever constructed when `diff_lines` is non-empty -- a no-op
    rollback (nothing actually changed) skips this entirely and just
    sends a plain status_layout(), so there's no "nothing to show" case
    to handle here.
    """

    def __init__(self, description: str, diff_lines: List[str], filename: str, *, inline_char_limit: int = 1800):
        super().__init__(timeout=300)
        self.diff_lines = diff_lines
        self.filename = filename
        self.inline_char_limit = inline_char_limit

        self.show_diff_button = Button(label="Show Diff", emoji="🔍", style=discord.ButtonStyle.secondary)
        self.show_diff_button.callback = self.on_show_diff

        self.container = Container(
            TextDisplay("### ✅ Success"),
            TextDisplay(description),
            accent_color=discord.Color.green(),
        )
        self.container.add_item(ActionRow(self.show_diff_button))
        self.add_item(self.container)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await default_ui_error(interaction, error, item, label="RollbackResultView")

    async def on_show_diff(self, interaction: discord.Interaction):
        diff_text = "\n".join(self.diff_lines)

        self.show_diff_button.disabled = True

        # Same inline-vs-attachment split /diff and the old /rollback used
        # (see send_diff_result) -- small diffs get added straight into
        # this message as a fenced codeblock, oversized ones get attached
        # as a .diff file instead so the edit doesn't blow past Discord's
        # per-component character limit.
        if len(diff_text) <= self.inline_char_limit:
            self.show_diff_button.label = "Diff Shown"
            self.container.add_item(TextDisplay(f"```diff\n{diff_text}\n```"))
            await interaction.response.edit_message(view=self)
        else:
            self.show_diff_button.label = "Diff Attached Below"
            self.container.add_item(UIFile(f"attachment://{self.filename}"))
            diff_file = discord.File(io.BytesIO(diff_text.encode()), filename=self.filename)
            await interaction.response.edit_message(view=self, attachments=[diff_file])


class Database(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="rollback", description="Rollback the user database to a specific commit.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(sha="The commit SHA to rollback to")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def rollback(self, interaction: discord.Interaction, sha: str):
        await interaction.response.defer(ephemeral=True)

        raw_url = f"https://raw.githubusercontent.com/{config.OWNER}/{config.REPO}/{sha}/{config.FILE_PATH}"

        try:
            restored_content = await fetch_raw_text(raw_url)
            json.loads(restored_content)
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))
        except json.JSONDecodeError as e:
            return await send_error(interaction, f"Error loading commit content: {e}")

        try:
            current_content, current_sha = await fetch_api_text_and_sha()
            await commit_content(restored_content, current_sha, f"Rollback Users.json to commit {sha}")
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        # A rollback can implicitly "unwhitelist" anyone added after the
        # commit being rolled back to -- they're not targeted individually
        # the way /unwhitelist is, so this diffs the before/after lists.
        try:
            removed_ids = find_removed_discord_ids(json.loads(current_content), json.loads(restored_content))
        except json.JSONDecodeError:
            removed_ids = []
        for discord_id in removed_ids:
            await revoke_buyer_role(interaction.guild, discord_id)

        await send_alert(interaction.client, alert_embed(
            "⏮️ Database Rolled Back",
            f"{interaction.user.mention} rolled back Users.json to commit `{sha[:7]}` via `/rollback`.",
            color=ALERT_COLOR_CAUTION,
            fields=[("Users Implicitly Removed", str(len(removed_ids)), True)] if removed_ids else None,
        ))

        # Diff what's being replaced against what was just restored, so
        # staff can see exactly what the rollback changed -- kept out of
        # the initial response and revealed on demand via RollbackResultView
        # (see class docstring) rather than shown inline right away.
        diff_lines = build_unified_diff(
            current_content,
            restored_content,
            fromfile="Users.json (before rollback)",
            tofile=f"Users.json (rolled back to {sha[:7]})",
        )

        description = f"Successfully rolled back the database to commit `{sha}`."

        if not diff_lines:
            await interaction.followup.send(
                view=status_layout(
                    "✅ Success",
                    f"{description}\n\nNo changes -- content is identical to the current version.",
                    discord.Color.green(),
                ),
                ephemeral=True,
            )
            return

        view = RollbackResultView(description, diff_lines, filename=f"rollback_{sha[:7]}.diff")
        await interaction.followup.send(view=view, ephemeral=True)

    @app_commands.command(name="commithistory", description="View the recent commit history.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(max_entries="Maximum number of commits to display (default 5, max 20)")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def commithistory(self, interaction: discord.Interaction, max_entries: int = 5):
        await interaction.response.defer(ephemeral=True)

        max_entries = min(max(1, max_entries), 20)

        try:
            commits = await list_commits(per_page=max_entries)
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        if not commits:
            return await send_error(interaction, "No commits found.")

        embed = discord.Embed(title=f"Commit History: `{config.FILE_PATH}`", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))

        for commit in commits:
            sha = commit["sha"]
            html_url = commit["html_url"]
            message = commit["commit"]["message"].split('\n')[0]
            author = commit["commit"]["author"]["name"]
            date = commit["commit"]["author"]["date"]
            date_obj = datetime.fromisoformat(date.replace("Z", "+00:00"))
            date_str = date_obj.strftime("%Y-%m-%d")

            max_name_len = 256
            sha_spoiler = f"||`{sha[:7]}`||"
            base_name = f"{date_str} — {sha_spoiler} — [View Commit]({html_url}) — "

            allowed_msg_len = max_name_len - len(base_name)
            if len(message) > allowed_msg_len:
                message = message[:allowed_msg_len - 3] + "..."

            try:
                stats_data = await get_commit(sha)
                additions = stats_data.get("stats", {}).get("additions", 0)
                deletions = stats_data.get("stats", {}).get("deletions", 0)
            except GitHubAPIError:
                additions = deletions = 0

            date_ts = int(date_obj.timestamp())
            name = f"{date_str} — ||{sha}||"
            value = (
                f"[View Commit]({html_url}) — {message}\n"
                f"🟢 `+{additions}` 🔴 `-{deletions}`\n"
                f"👤 **{author}** • <t:{date_ts}:R>\n"
                "\u200b\n"
            )

            embed.add_field(name=name, value=value, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="fetchcommit", description="Fetches the details for a specific commit.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(sha="Commit SHA to fetch")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def fetchcommit(self, interaction: discord.Interaction, sha: str):
        await interaction.response.defer(ephemeral=True)

        try:
            data = await get_commit(sha)
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        commit = data["commit"]
        author = commit["author"]["name"]
        date_str = commit["author"]["date"]
        date_obj = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        date_ts = int(date_obj.timestamp())

        message = commit["message"]
        additions = data.get("stats", {}).get("additions", 0)
        deletions = data.get("stats", {}).get("deletions", 0)
        html_url = data["html_url"]

        embed = discord.Embed(
            title=f"Commit Details — ||{sha}||",
            url=html_url,
            description=message,
            color=discord.Color.green(),
            timestamp=date_obj,
        )
        embed.set_author(name=author)
        embed.add_field(name="Additions", value=f"🟢 +{additions}", inline=True)
        embed.add_field(name="Deletions", value=f"❌ -{deletions}", inline=True)
        embed.add_field(name="Date", value=f"<t:{date_ts}:F>", inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="verifydata", description="Validates if the raw database file matches the real database file.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def verifydata(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            raw_content = await fetch_raw_text(config.RAW_URL)
            real_content, _sha = await fetch_api_text_and_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        if raw_content.strip() == real_content.strip():
            embed = discord.Embed(
                title="Database Integrity Verified",
                description="The raw database matches the real database exactly.",
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title="Database Integrity Mismatch",
                description="The raw database does **not** match the real database.\nPossible causes:\n- CDN caching\n- Unauthorized edits\n- Commit mismatch (API Limitations)",
                color=discord.Color.red(),
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="export", description="Exports the current database.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(format="Select export format")
    @app_commands.choices(format=[
        app_commands.Choice(name="JSON", value="json"),
        app_commands.Choice(name="CSV", value="csv"),
    ])
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def export(self, interaction: discord.Interaction, format: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)

        try:
            data = await fetch_api_file()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        content_b64 = data["content"]
        decoded = base64.b64decode(content_b64).decode('utf-8')
        users = json.loads(decoded)

        if format.value == "json":
            filename = "Users.json"
            file_bytes = base64.b64decode(content_b64)
            file = discord.File(io.BytesIO(file_bytes), filename=filename)
            view = file_success_layout("Here is the exported JSON database.", filename)

        else:  # csv
            output = io.StringIO()
            if users:
                # Column order/coverage is derived from every row (not just
                # users[0]) so older entries missing a newer field (e.g. a
                # pre-HWID-reset-tracking entry without LastHwidReset) don't
                # break the header -- restval="" fills those in as blank.
                fieldnames = list(dict.fromkeys(
                    CSV_EXPORT_COLUMN_MAP.get(key, key)
                    for user in users
                    for key in user.keys()
                ))
                writer = csv.DictWriter(output, fieldnames=fieldnames, restval="")
                writer.writeheader()
                for user in users:
                    writer.writerow({CSV_EXPORT_COLUMN_MAP.get(k, k): v for k, v in user.items()})
            else:
                output.write("No data available.")

            filename = "Users.csv"
            file = discord.File(io.BytesIO(output.getvalue().encode()), filename=filename)
            view = file_success_layout("Here is the exported CSV database.", filename)

        await interaction.followup.send(view=view, file=file, ephemeral=True)

    @app_commands.command(name="upload", description="Upload a Users.json file to replace the contents of the database. Can be used as a bulk-import.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(file="Upload a Users.json file")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def upload(self, interaction: discord.Interaction, file: discord.Attachment):
        await interaction.response.defer(ephemeral=True)

        if not file.filename.lower().endswith(".json"):
            return await send_error(interaction, "Please upload a valid JSON file.")

        try:
            file_bytes = await file.read()
            users_data = json.loads(file_bytes)
        except Exception as e:
            return await send_error(interaction, f"Failed to parse JSON: {e}")

        content_str = json.dumps(users_data, indent=4)

        try:
            current_users, sha = await fetch_users_with_sha()
            await commit_content(content_str, sha, f"Upload Users.json by {interaction.user}")
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        # A bulk upload can implicitly "unwhitelist" anyone missing from the
        # uploaded file -- diff the before/after lists to find them.
        removed_ids = find_removed_discord_ids(current_users, users_data)
        for discord_id in removed_ids:
            await revoke_buyer_role(interaction.guild, discord_id)

        added_ids = find_removed_discord_ids(users_data, current_users) if isinstance(users_data, list) else []
        await send_alert(interaction.client, alert_embed(
            "📤 Database Uploaded",
            f"{interaction.user.mention} replaced Users.json via `/upload` ({file.filename}).",
            color=ALERT_COLOR_CAUTION,
            fields=[
                ("Total Entries", str(len(users_data)) if isinstance(users_data, list) else "N/A", True),
                ("Added", str(len(added_ids)), True),
                ("Removed", str(len(removed_ids)), True),
            ],
        ))

        await interaction.followup.send(
            view=status_layout("✅ Success", "Users.json uploaded successfully.", discord.Color.green()),
            ephemeral=True,
        )

    @app_commands.command(name="dbsearch", description="Searches the entire database for a value.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(query="Value to search for in all user fields")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def dbsearch(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=True)

        try:
            users, _ = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        query_lower = query.lower()
        matches = []

        for user in users:
            for value in user.values():
                if isinstance(value, str) and query_lower in value.lower():
                    matches.append(user)
                    break
                elif isinstance(value, (int, float)) and query_lower in str(value).lower():
                    matches.append(user)
                    break

        if not matches:
            return await send_error(interaction, "No matching entries found.")

        view = DbSearchView(matches)
        await interaction.followup.send(view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Database(bot))

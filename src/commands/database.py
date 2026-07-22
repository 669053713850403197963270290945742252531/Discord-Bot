import base64
import csv
import difflib
import io
import json
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import LayoutView, Container, TextDisplay, ActionRow, Button

from api import config
from api.discord_helpers import has_role, is_in_guild, send_success, send_error, file_success_layout, status_layout
from api.github import (
    GitHubAPIError, fetch_raw_text, fetch_api_text_and_sha, fetch_api_file,
    commit_content, fetch_users_with_sha, list_commits, get_commit,
)
from api.users import find_removed_discord_ids, revoke_buyer_role
from api.time_utils import format_discord_timestamp

GUILD = discord.Object(id=config.GUILD_ID)


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

        # Diff what's being replaced against what was just restored, so
        # staff can see exactly what the rollback changed.
        diff_lines = list(difflib.unified_diff(
            current_content.splitlines(),
            restored_content.splitlines(),
            fromfile="Users.json (before rollback)",
            tofile=f"Users.json (rolled back to {sha[:7]})",
            lineterm="",
        ))

        description = f"Successfully rolled back the database to commit `{sha}`."
        diff_file = None
        diff_filename = None

        if not diff_lines:
            description += "\n\nNo changes -- content is identical to the current version."
        else:
            diff_text = "\n".join(diff_lines)
            if len(diff_text) <= 1800:
                description += f"\n\n```diff\n{diff_text}\n```"
            else:
                diff_filename = f"rollback_{sha[:7]}.diff"
                description += f"\n\nDiff too large to display inline ({len(diff_lines)} lines) — see attached file below."
                diff_file = discord.File(io.BytesIO(diff_text.encode()), filename=diff_filename)

        if diff_file:
            layout = file_success_layout(description, diff_filename)
            await interaction.followup.send(view=layout, file=diff_file, ephemeral=True)
        else:
            await send_success(interaction, description)

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
                fieldnames = users[0].keys()
                writer = csv.DictWriter(output, fieldnames=fieldnames)
                writer.writeheader()
                for user in users:
                    writer.writerow(user)
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
        for discord_id in find_removed_discord_ids(current_users, users_data):
            await revoke_buyer_role(interaction.guild, discord_id)

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

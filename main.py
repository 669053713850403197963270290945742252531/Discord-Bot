import discord
from discord.ext import commands
from discord import (
    app_commands,
    ui,
    Interaction,
    TextStyle,
    Embed,
    ButtonStyle,
    Attachment,
)
import aiohttp
import json
from github import Github
import os
from dotenv import load_dotenv
import re
from datetime import datetime, timezone, timedelta, UTC
from zoneinfo import ZoneInfo
from functools import wraps
from discord.ui import View, Button
import asyncio
import io
import random
import string
import base64
import time
import pytz

from flask import Flask
from threading import Thread
from keep_alive import keep_alive

app = Flask("")


@app.route("/")
def home():
    return "Bot is alive!"


def run():
    app.run(host="0.0.0.0", port=8080)


def keep_alive():
    t = Thread(target=run)
    t.start()


load_dotenv()  # Load environment variables into bot environment
stored_script_timestamp = datetime.now(ZoneInfo("America/New_York"))
formatted_time = stored_script_timestamp.strftime("%Y-%m-%d %I:%M:%S %p %Z")


class Client(commands.Bot):
    async def on_ready(self):
        print(f"Logged on as {self.user}!")
        try:
            guild = discord.Object(id=1263334150018961559)
            synced = await self.tree.sync(guild=guild)
            print(f"Synced {len(synced)} commands to guild {guild.id}")
        except Exception as e:
            print(f"Error syncing commands: {e}")


def require_role(required_role_id: int):
    def decorator(func):
        @wraps(func)
        async def wrapper(interaction: Interaction, *args, **kwargs):
            if required_role_id in [role.id for role in interaction.user.roles]:
                return await func(interaction, *args, **kwargs)
            else:
                await interaction.response.send_message(
                    "❌ You do not have permission to use this command.", ephemeral=True
                )

        return wrapper

    return decorator


intents = discord.Intents.default()
intents.message_content = True
client = Client(command_prefix="!", intents=intents)

GUILD_ID = discord.Object(id=1263334150018961559)
RESTRICTED_ROLE_ID = 1368809009456615434

REPO_OWNER = "669053713850403197963270290945742252531"
REPO_NAME = "Celestial"
USERFILE_PATH = "Users.json"

stored_script_content = None
stored_script_filename = None
stored_script_timestamp = None


async def fetch_user_data():
    url = "https://raw.githubusercontent.com/669053713850403197963270290945742252531/Celestial/refs/heads/main/Users.json"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    text = await response.text()
                    data = json.loads(text)

                    # field value checks
                    for entry in data:
                        entry["Banned"] = (
                            str(entry.get("Banned", "false")).lower() == "true"
                        )
                        entry["TempBan"] = (
                            str(entry.get("TempBan", "false")).lower() == "true"
                        )
                        entry["Notes"] = (
                            None
                            if str(entry.get("Notes", "")).lower() == "false"
                            else entry["Notes"]
                        )
                        entry["BanReason"] = (
                            None
                            if str(entry.get("BanReason", "")).lower() == "null"
                            else entry["BanReason"]
                        )
                        entry["TempBanEnd"] = (
                            None
                            if str(entry.get("TempBanEnd", "")).lower() == "null"
                            else entry["TempBanEnd"]
                        )
                        entry["TempBanDuration"] = (
                            None
                            if str(entry.get("TempBanDuration", "")).lower() == "null"
                            else entry["TempBanDuration"]
                        )

                    return data
                else:
                    print(f"HTTP Error: {response.status}")
                    return None
        except aiohttp.ClientError as e:
            print(f"Failed to fetch data: {e}")
            return None


def validate_whitelist_entry(entry):
    required_keys = ["HWID", "Identifier", "Rank", "JoinDate", "DiscordId"]
    for key in required_keys:
        if key not in entry:
            raise ValueError(f"Missing required key: {key}")


# HWID hashing


def hash_hwid(hwid):
    hwid = hwid.strip().lower()
    sha384_pattern = re.compile(r"^[0-9a-f]{96}$")

    if not sha384_pattern.match(hwid):
        raise ValueError(
            "HWID must be a valid SHA-384 string (96 alphanumeric characters)"
        )

    return hwid


# Command: myinfo


@client.tree.command(
    name="myinfo",
    description="Fetches the non-sensitive information about yourself.",
    guild=GUILD_ID,
)
async def myinfo(interaction: discord.Interaction):
    user_id = str(interaction.user.id)  # Discord ID > string
    data = await fetch_user_data()

    if data is None:
        await interaction.response.send_message(
            "Failed to fetch user data. Please try again later.", ephemeral=True
        )
        return

    user_info = next((user for user in data if user["DiscordId"] == user_id), None)

    if user_info:
        # Ban and no hwid set check
        hashed_hwid = user_info.get("HashedHWID", "N/A")

        if not user_info.get("HWID"):
            await interaction.response.send_message(
                "You are not authorized to use this command.", ephemeral=True
            )
            return

        # Create embed
        embed = discord.Embed(
            title="Your whitelist information", color=discord.Color.blue()
        )
        embed.add_field(
            name="Identifier", value=user_info.get("Identifier", "N/A"), inline=True
        )
        embed.add_field(name="Rank", value=user_info.get("Rank", "N/A"), inline=True)
        embed.add_field(
            name="JoinDate", value=user_info.get("JoinDate", "N/A"), inline=True
        )
        embed.add_field(
            name="DiscordId", value=user_info.get("DiscordId", "N/A"), inline=True
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(
            "No information found for your user ID.", ephemeral=True
        )


# Command: fetchinfo


@client.tree.command(
    name="fetchinfo",
    description="Fetches all information about a user from the whitelist.",
    guild=GUILD_ID,
)
@app_commands.describe(user="The Discord user to fetch information for")
@require_role(RESTRICTED_ROLE_ID)
async def fetchinfo(interaction: discord.Interaction, user: discord.User):
    if not any(role.id == RESTRICTED_ROLE_ID for role in interaction.user.roles):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    user_id = str(user.id)
    data = await fetch_user_data()

    if data is None:
        await interaction.response.send_message(
            "⚠️ Failed to fetch user data. Please try again later.", ephemeral=True
        )
        return

    user_info = next((entry for entry in data if entry["DiscordId"] == user_id), None)

    if user_info:
        embed = discord.Embed(
            title=f"Whitelist Info for {user}",
            description=f"Requested by {interaction.user.mention}",
            color=discord.Color.green(),
        )

        for key, value in user_info.items():
            display_value = str(value) if value is not None else "N/A"
            embed.add_field(name=key, value=display_value, inline=True)

        await interaction.response.send_message(
            content=f"✅ Whitelist details for {user.mention}:",
            embed=embed,
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"❌ No whitelist entry found for {user.mention}.", ephemeral=True
        )


# Command: registerhwid


class HWIDModal(discord.ui.Modal, title="Register HWID"):
    identifier = discord.ui.TextInput(
        label="Enter an Identifier (name)",
        placeholder="e.g. Corrade",
        min_length=2,
        max_length=50,
        required=True,
    )

    hwid = discord.ui.TextInput(
        label="Enter your HWID (Reference tutorial)",
        placeholder="96-character SHA-384 hash",
        min_length=8,
        max_length=100,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        identifier_input = self.identifier.value.strip()
        user_input = self.hwid.value.strip()

        if not user_input or len(user_input) < 8:
            await interaction.response.send_message(
                "❌ Invalid HWID format. HWID must be at least 8 characters long.",
                ephemeral=True,
            )
            return

        try:
            hashed_hwid = hash_hwid(user_input)
        except ValueError as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
            return

        registered_hwid_channel = interaction.client.get_channel(1325394667918987266)
        if not registered_hwid_channel:
            await interaction.response.send_message(
                "Invalid HWID registration channel. Contact an admin.", ephemeral=True
            )
            return

        async for message in registered_hwid_channel.history(limit=100):
            if message.embeds:
                for embed in message.embeds:
                    for field in embed.fields:
                        if hashed_hwid == field.value:
                            await interaction.response.send_message(
                                "⚠️ This HWID is already registered.", ephemeral=True
                            )
                            return

        embed = discord.Embed(title="HWID Registered", color=discord.Color.green())
        embed.add_field(
            name="User",
            value=f"{interaction.user.mention} ({interaction.user.id})",
            inline=False,
        )
        embed.add_field(name="Identifier", value=identifier_input, inline=True)
        embed.add_field(name="Hashed HWID", value=f"```{hashed_hwid}```", inline=True)
        await registered_hwid_channel.send(embed=embed)

        await interaction.response.send_message(
            "✅ Your HWID has been successfully registered. Please wait for a response from an admin while it's being reviewed.",
            ephemeral=True,
        )


class TutorialView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Continue", style=discord.ButtonStyle.green, custom_id="open_hwid_modal"
    )
    async def continue_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_modal(HWIDModal())


@client.tree.command(
    name="registerhwid",
    description="Register your HWID to the whitelist system.",
    guild=GUILD_ID,
)
async def registerhwid(interaction: discord.Interaction):
    await interaction.response.send_message(
        content=(
            "**Need help finding your HWID?** [Watch the tutorial](https://youtu.be/YOUR_VIDEO_ID)\n\n"
            "When you're ready, click the button below to continue."
        ),
        view=TutorialView(),
        ephemeral=True,
    )


# Command: whitelist


class WhitelistModal(discord.ui.Modal, title="Add Whitelisted User"):
    hwid = discord.ui.TextInput(
        label="SHA-384 HWID", placeholder="96-character SHA-384 hash", required=True
    )
    identifier = discord.ui.TextInput(
        label="Identifier", placeholder="e.g., user123", required=True
    )
    rank = discord.ui.TextInput(
        label="Rank", placeholder="e.g., Member, Admin", required=True
    )
    discord_id = discord.ui.TextInput(
        label="Discord ID", placeholder="e.g., 123456789012345678", required=True
    )
    key = discord.ui.TextInput(
        label="Key", placeholder="e.g., ABCD-1234-EFGH", required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        admin_role_id = 1273432694533001286
        if not any(role.id == admin_role_id for role in interaction.user.roles):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            return

        github_token = os.getenv("GITHUB_TOKEN")
        if not github_token:
            await interaction.response.send_message(
                "❌ GitHub token not found.", ephemeral=True
            )
            return

        try:
            hashed_hwid = hash_hwid(self.hwid.value)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        try:
            github = Github(github_token)
            repo = github.get_repo(REPO_NAME)
            file = repo.get_contents(USERFILE_PATH)

            try:
                data = json.loads(file.decoded_content.decode())
            except json.JSONDecodeError:
                await interaction.response.send_message(
                    "⚠️ The JSON file is invalid.", ephemeral=True
                )
                return

            for entry in data:
                if entry.get("HWID") == hashed_hwid:
                    await interaction.response.send_message(
                        "⚠️ This HWID is already whitelisted.", ephemeral=True
                    )
                    return
                if entry.get("Identifier") == self.identifier.value:
                    await interaction.response.send_message(
                        "⚠️ This Identifier is already used.", ephemeral=True
                    )
                    return
                if entry.get("DiscordId") == self.discord_id.value:
                    await interaction.response.send_message(
                        "⚠️ This Discord ID is already registered.", ephemeral=True
                    )
                    return
                if entry.get("Key") == self.key.value:
                    await interaction.response.send_message(
                        "⚠️ This Key is already in use.", ephemeral=True
                    )
                    return

            join_date = f"{datetime.now().year}-{datetime.now().month:02d}-{datetime.now().day:02d}"

            new_entry = {
                "HWID": hashed_hwid,
                "Identifier": self.identifier.value,
                "Rank": self.rank.value,
                "JoinDate": join_date,
                "DiscordId": self.discord_id.value,
                "Key": self.key.value,
                "Notes": "false",
                "Banned": "false",
                "TempBan": "false",
                "BanReason": "null",
                "TempBanDuration": "null",
                "TempBanEnd": "null",
            }

            data.append(new_entry)

            updated_content = json.dumps(data, indent=4)

            repo.update_file(
                path=file.path,
                message=f"Add whitelist entry for {self.identifier.value}",
                content=updated_content,
                sha=file.sha,
            )

            await interaction.response.send_message(
                "✅ Whitelisted user successfully added.", ephemeral=True
            )

        except Exception as e:
            await interaction.response.send_message(
                f"❌ Failed to update whitelist: {e}", ephemeral=True
            )


@client.tree.command(
    name="whitelist",
    description="Add a user to the database.",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
async def whitelist(interaction: discord.Interaction):
    await interaction.response.send_modal(WhitelistModal())


# Command: unwhitelist


@client.tree.command(
    name="unwhitelist",
    description="Remove a user from the database.",
    guild=GUILD_ID,
)
@app_commands.describe(user="Mention the user to remove from the whitelist")
@require_role(RESTRICTED_ROLE_ID)
async def unwhitelist(interaction: discord.Interaction, user: discord.User):
    admin_role_id = 1273432694533001286
    if not any(role.id == admin_role_id for role in interaction.user.roles):
        await interaction.response.send_message(
            "❌ You do not have permission to use this command.", ephemeral=True
        )
        return

    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        await interaction.response.send_message(
            "❌ GitHub token not found.", ephemeral=True
        )
        return

    try:
        github = Github(github_token)
        repo = github.get_repo(REPO_NAME)
        file = repo.get_contents(USERFILE_PATH)

        try:
            data = json.loads(file.decoded_content.decode())
        except json.JSONDecodeError:
            await interaction.response.send_message(
                "⚠️ The JSON file is invalid.", ephemeral=True
            )
            return

        user_id_str = str(user.id)
        updated_data = [
            entry for entry in data if entry.get("DiscordId") != user_id_str
        ]

        if len(updated_data) == len(data):
            await interaction.response.send_message(
                f"⚠️ No user found with Discord ID `{user_id_str}` in the whitelist.",
                ephemeral=True,
            )
            return

        updated_content = json.dumps(updated_data, indent=4)
        repo.update_file(
            file.path,
            f"Removed user with Discord ID '{user_id_str}' from whitelist",
            updated_content,
            file.sha,
        )

        await interaction.response.send_message(
            f"✅ Successfully removed {user.mention} from the whitelist.",
            ephemeral=True,
        )

    except Exception as e:
        await interaction.response.send_message(
            f"❌ Failed to update whitelist: {e}", ephemeral=True
        )


# Command: viewwhitelist


class WhitelistPaginator(View):
    def __init__(self, pages, user):
        super().__init__(timeout=60)
        self.pages = pages
        self.current = 0
        self.user = user

        self.prev_button.disabled = True
        if len(pages) == 1:
            self.next_button.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user.id

    @discord.ui.button(label="Previous", style=ButtonStyle.primary, row=0)
    async def prev_button(self, interaction: Interaction, button: Button):
        self.current -= 1
        self.prev_button.disabled = self.current == 0
        self.next_button.disabled = False
        await interaction.response.edit_message(
            embed=self.pages[self.current], view=self
        )

    @discord.ui.button(label="Next", style=ButtonStyle.primary, row=0)
    async def next_button(self, interaction: Interaction, button: Button):
        self.current += 1
        self.next_button.disabled = self.current == len(self.pages) - 1
        self.prev_button.disabled = False
        await interaction.response.edit_message(
            embed=self.pages[self.current], view=self
        )


@client.tree.command(
    name="viewwhitelist", description="View all users in the database.", guild=GUILD_ID
)
@require_role(RESTRICTED_ROLE_ID)
async def view_whitelist(interaction: discord.Interaction):
    admin_role_id = 1273432694533001286
    if not any(role.id == admin_role_id for role in interaction.user.roles):
        await interaction.response.send_message(
            "❌ You do not have permission to use this command.", ephemeral=True
        )
        return

    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        await interaction.response.send_message(
            "❌ GitHub token not found.", ephemeral=True
        )
        return

    try:
        github = Github(github_token)
        repo = github.get_repo(REPO_NAME)
        file = repo.get_contents(USERFILE_PATH)

        try:
            data = json.loads(file.decoded_content.decode())
        except json.JSONDecodeError:
            await interaction.response.send_message(
                "⚠️ The JSON file is invalid.", ephemeral=True
            )
            return

        if not data:
            await interaction.response.send_message(
                "⚠️ The whitelist is currently empty.", ephemeral=True
            )
            return

        entries_per_page = 3
        pages = [
            data[i : i + entries_per_page]
            for i in range(0, len(data), entries_per_page)
        ]

        embeds = []
        for i, page in enumerate(pages):
            embed = discord.Embed(
                title=f"Whitelist - Page {i+1}/{len(pages)}",
                color=discord.Color.blurple(),
            )
            for user in page:
                embed.add_field(
                    name=f"{user.get('Identifier', 'N/A')} ({user.get('Rank', 'Unknown')})",
                    value=(
                        f"**HWID:** `{user.get('HWID', 'None')}`\n"
                        f"**JoinDate:** {user.get('JoinDate', 'Unknown')}\n"
                        f"**DiscordId:** {user.get('DiscordId', 'Unknown')}\n"
                        f"**Key:** `{user.get('Key', 'N/A')}`\n"
                        f"**Notes:** {user.get('Notes', 'None')}\n"
                        f"**Banned:** {user.get('Banned', False)}\n"
                        f"**TempBan:** {user.get('TempBan', False)}\n"
                        f"**BanReason:** {user.get('BanReason', 'None')}\n"
                        f"**TempBanDuration:** {user.get('TempBanDuration', 'None')}\n"
                        f"**TempBanEnd:** {user.get('TempBanEnd', 'None')}"
                    ),
                    inline=False,
                )
            embeds.append(embed)

        if len(embeds) == 1:
            await interaction.response.send_message(embed=embeds[0], ephemeral=True)
        else:
            view = WhitelistPaginator(embeds, interaction.user)
            await interaction.response.send_message(
                embed=embeds[0], view=view, ephemeral=True
            )

    except Exception as e:
        await interaction.response.send_message(
            f"❌ Failed to retrieve whitelist: {e}", ephemeral=True
        )


# Command: editwhitelist


class EditWhitelistModal(ui.Modal, title="Edit Whitelist Data"):
    def __init__(self, original_data, github, repo, file_path, sha):
        super().__init__()
        self.github = github
        self.repo = repo
        self.file_path = file_path
        self.sha = sha
        self.add_item(
            ui.TextInput(
                label="Whitelist JSON (edit with caution)",
                style=TextStyle.paragraph,
                default=original_data[:4000],
                max_length=4000,
                placeholder="Paste JSON here...",
            )
        )

    async def on_submit(self, interaction: Interaction):
        try:
            json.loads(self.children[0].value)
            # GitHub commit
            self.repo.update_file(
                path=self.file_path,
                message="Update whitelist via modal",
                content=self.children[0].value,
                sha=self.sha,
            )
            await interaction.response.send_message(
                "✅ Whitelist updated successfully!", ephemeral=True
            )
        except json.JSONDecodeError:
            await interaction.response.send_message(
                "❌ Invalid JSON format.", ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Failed to update: {e}", ephemeral=True
            )


@client.tree.command(
    name="editwhitelist", description="Edit the raw database data.", guild=GUILD_ID
)
@require_role(RESTRICTED_ROLE_ID)
async def edit_whitelist(interaction: discord.Interaction):
    admin_role_id = 1273432694533001286
    if not any(role.id == admin_role_id for role in interaction.user.roles):
        await interaction.response.send_message(
            "❌ You do not have permission.", ephemeral=True
        )
        return

    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        await interaction.response.send_message(
            "❌ GitHub token not found.", ephemeral=True
        )
        return

    try:
        github = Github(github_token)
        file = REPO_NAME.get_contents(USERFILE_PATH)
        content = file.decoded_content.decode()

        await interaction.response.send_modal(
            EditWhitelistModal(content, github, REPO_NAME, "Users.json", file.sha)
        )

    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


# Command: createpanel


class RedeemKeyModal(discord.ui.Modal, title="Redeem Script Key"):
    key = discord.ui.TextInput(label="Script Key", required=True)
    hwid = discord.ui.TextInput(label="HWID (SHA-384 hash)", required=True)
    identifier = discord.ui.TextInput(label="Username / Identifier", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        github_token = os.getenv("GITHUB_TOKEN")
        if not github_token:
            await interaction.response.send_message(
                "❌ GitHub token missing.", ephemeral=True
            )
            return

        try:
            # Validate HWID format
            try:
                hashed_hwid = hash_hwid(self.hwid.value)
            except ValueError as e:
                await interaction.response.send_message(
                    f"❌ HWID error: {e}", ephemeral=True
                )
                return

            repo_name = "669053713850403197963270290945742252531/Celestial"
            file_path = "Users.json"
            github = Github(github_token)
            repo = github.get_repo(repo_name)
            file = repo.get_contents(file_path)

            try:
                data = json.loads(file.decoded_content.decode())
            except json.JSONDecodeError:
                await interaction.response.send_message(
                    "⚠️ JSON file is invalid.", ephemeral=True
                )
                return

            # Check if HWID or Key already used
            if any(u["HWID"] == hashed_hwid for u in data):
                await interaction.response.send_message(
                    "⚠️ This HWID is already registered.", ephemeral=True
                )
                return
            if any(u["Key"] == self.key.value for u in data):
                await interaction.response.send_message(
                    "⚠️ This key has already been used.", ephemeral=True
                )
                return

            # Prepare new entry
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            new_entry = {
                "HWID": hashed_hwid,
                "Identifier": self.identifier.value,
                "Rank": "User",
                "JoinDate": today,
                "DiscordId": str(interaction.user.id),
                "Key": self.key.value,
                "Notes": "false",
                "Banned": "false",
                "TempBan": "false",
                "BanReason": "null",
                "TempBanDuration": "null",
                "TempBanEnd": "null",
            }

            data.append(new_entry)
            updated_content = json.dumps(data, indent=4)
            repo.update_file(
                path=file.path,
                message=f"Redeemed key for {self.identifier.value}",
                content=updated_content,
                sha=file.sha,
            )

            await interaction.response.send_message(
                "✅ Key redeemed and access granted!", ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Failed to redeem: {e}", ephemeral=True
            )


class ContinueRedeemView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def continue_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_modal(RedeemKeyModal())


class RedeemKeyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔑 Redeem Key", style=discord.ButtonStyle.green)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "To redeem your key, you'll need your HWID. If you're unsure how to get it, follow this [tutorial](https://www.youtube.com/VIDEO).\n"
            "When you're ready, click **Continue**.",
            view=ContinueRedeemView(),
            ephemeral=True,
        )


class GetScriptButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="📜 Get Script",
            style=discord.ButtonStyle.blurple,
            custom_id="get_script_button",
        )

    async def callback(self, interaction: discord.Interaction):
        global stored_script_content, stored_script_filename

        # Load whitelist data
        whitelist_data = await fetch_user_data()
        if not whitelist_data:
            await interaction.response.send_message(
                "❌ Your not whitelisted to this script.", ephemeral=True
            )
            return

        user_entry = next(
            (
                entry
                for entry in whitelist_data
                if str(entry.get("DiscordId")) == str(interaction.user.id)
            ),
            None,
        )

        if not user_entry:
            await interaction.response.send_message(
                "❌ You're not in the whitelist.", ephemeral=True
            )
            return

        try:
            validate_whitelist_entry(user_entry)
        except ValueError as e:
            await interaction.response.send_message(
                f"❌ Invalid whitelist entry: {e}", ephemeral=True
            )
            return

        if user_entry.get("Banned") or user_entry.get("TempBan"):
            await interaction.response.send_message(
                "❌ You are banned from using this service.", ephemeral=True
            )
            return

        if not stored_script_content or not stored_script_filename:
            await interaction.response.send_message(
                "❌ No script has been uploaded by the admin.", ephemeral=True
            )
            return

        # Decode script
        try:
            script_text = stored_script_content.decode("utf-8")
        except UnicodeDecodeError:
            await interaction.response.send_message(
                "❌ Script encoding error.", ephemeral=True
            )
            return

        # Replace key inside quotes
        updated_script = re.sub(
            r'getgenv\(\)\.script_key\s*=\s*"(.*?)"',
            f'getgenv().script_key = "{user_entry["Key"]}";',
            script_text,
        )

        # Send modified script
        file = discord.File(
            fp=io.BytesIO(updated_script.encode("utf-8")),
            filename=stored_script_filename,
        )
        await interaction.response.send_message(
            "Here is your script:", file=file, ephemeral=True
        )


class CreatePanelModal(discord.ui.Modal, title="Create Script Panel"):
    embed_title = discord.ui.TextInput(label="Embed Title", required=True)
    embed_description = discord.ui.TextInput(
        label="Embed Description", style=discord.TextStyle.paragraph, required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title=self.embed_title.value,
            description=self.embed_description.value,
            color=discord.Color.blurple(),
        )

        view = discord.ui.View(timeout=None)
        view.add_item(RedeemKeyButton())
        view.add_item(GetScriptButton())

        target_channel = interaction.client.get_channel(1368816321139183647)
        if not target_channel:
            await interaction.response.send_message(
                "❌ Target channel not found.", ephemeral=True
            )
            return

        await target_channel.send(embed=embed, view=view)

        await interaction.response.send_message(
            "✅ Panel created successfully.", ephemeral=True
        )


@client.tree.command(
    name="createpanel",
    description="Creates an embed panel inside the panel channel.",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
async def createpanel(interaction: discord.Interaction):
    await interaction.response.send_modal(CreatePanelModal())


# Command: updatescript


@client.tree.command(
    name="updatescript",
    description="Upload a new script to update the stored script to then supply in the panel.",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
async def updatescript(interaction: discord.Interaction, file: Attachment):
    # Is file check
    if not file:
        await interaction.response.send_message(
            "❌ Please upload a script file to update.", ephemeral=True
        )
        return

    try:
        # Read content of uploaded file
        file_content = await file.read()

        # Update global variables with new script's data
        global stored_script_content, stored_script_filename, stored_script_timestamp
        stored_script_content = file_content
        stored_script_filename = file.filename
        stored_script_timestamp = datetime.now(timezone.utc)

        await interaction.response.send_message(
            f"✅ The script `{stored_script_filename}` has been successfully uploaded and updated.",
            ephemeral=True,
        )

    except Exception as e:
        await interaction.response.send_message(
            f"❌ Error reading the file: {e}", ephemeral=True
        )


# Command: purge


@client.tree.command(
    name="purge",
    description="Deletes a number of messages in a channel.",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
@app_commands.describe(amount="Number of messages to delete (max 100)")
async def purge(interaction: discord.Interaction, amount: int):
    if amount < 1 or amount > 100:
        await interaction.response.send_message(
            "❌ Amount must be between 1 and 100.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        deleted = await interaction.channel.purge(
            limit=amount, check=lambda msg: not msg.pinned, bulk=True
        )
        await interaction.followup.send(
            f"🗑️ Deleted {len(deleted)} messages.", ephemeral=True
        )

    except discord.Forbidden:
        await interaction.followup.send(
            "❌ I don't have permission to delete messages.", ephemeral=True
        )
    except discord.HTTPException as e:
        await interaction.followup.send(
            f"❌ Failed to delete messages: {e}", ephemeral=True
        )


# Command: lock


@client.tree.command(
    name="lock",
    description="Locks a specific channel (defaults to current channel).",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
@app_commands.describe(channel="Channel to lock (optional)")
async def lock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    channel = channel or interaction.channel
    overwrite = channel.overwrites_for(interaction.guild.default_role)

    if overwrite.send_messages is False:
        await interaction.response.send_message(
            "🔒 Channel is already locked.", ephemeral=True
        )
        return

    overwrite.send_messages = False
    await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
    await interaction.response.send_message(
        f"🔒 Locked {channel.mention}.", ephemeral=True
    )


# Command: unlock


@client.tree.command(
    name="unlock",
    description="Unlocks a specific channel (defaults to current channel).",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
@app_commands.describe(channel="Channel to unlock (optional)")
async def unlock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    channel = channel or interaction.channel
    overwrite = channel.overwrites_for(interaction.guild.default_role)

    if overwrite.send_messages is not False:
        await interaction.response.send_message(
            "🔓 Channel is already unlocked.", ephemeral=True
        )
        return

    overwrite.send_messages = None
    await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
    await interaction.response.send_message(
        f"🔓 Unlocked {channel.mention}.", ephemeral=True
    )


# Command: genkey


def generate_key(min_length=35, max_length=45):
    length = random.randint(min_length, max_length)
    charset = string.ascii_letters + string.digits
    return "".join(random.choices(charset, k=length))


@client.tree.command(
    name="genkey",
    description="Generates a new random and unique script key.",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
async def genkey(interaction: discord.Interaction):
    key = generate_key()
    await interaction.response.send_message(
        f"🔑 **Generated Key:** `{key}`", ephemeral=True
    )


# Command: scriptstatus


class ScriptDownloadView(View):
    def __init__(self, filename: str, content: bytes):
        super().__init__(timeout=60)
        self.filename = filename
        self.content = content

    @discord.ui.button(label="📥 Download Script", style=discord.ButtonStyle.blurple)
    async def download(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        # Create file with the content
        file = discord.File(fp=io.BytesIO(self.content), filename=self.filename)

        # Send message with the attachment
        await interaction.response.send_message(
            content=f"📦 Here's your script file `{self.filename}`.",
            file=file,
            ephemeral=True,
        )


@client.tree.command(
    name="scriptstatus",
    description="View the information about the currently uploaded script.",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
async def scriptstatus(interaction: discord.Interaction):
    global stored_script_content, stored_script_filename, stored_script_timestamp

    if stored_script_content and stored_script_filename:
        timestamp_str = (
            stored_script_timestamp.strftime("%Y-%m-%d %H:%M:%S %Z")
            if stored_script_timestamp
            else "Unknown"
        )

        embed = discord.Embed(
            title="📄 Script Status",
            description=(
                f"**Script Name:** {stored_script_filename}\n"
                f"**Last Updated:** `{formatted_time}`"
            ),
            color=discord.Color.blue(),
        )

        # Create download button, pass filename & content
        view = ScriptDownloadView(stored_script_filename, stored_script_content)

        # Send embed with download button
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(
            "❌ No script has been uploaded yet.", ephemeral=True
        )


# Command: scriptban


async def save_user_data_to_github(data, commit_message):
    update_url = "https://api.github.com/repos/669053713850403197963270290945742252531/Celestial/contents/Users.json"

    headers = {
        "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}",
        "Content-Type": "application/json",
    }
    content = json.dumps(data, indent=4)
    encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    async with aiohttp.ClientSession() as session:
        async with session.get(update_url, headers=headers) as response:
            if response.status == 200:
                file_data = await response.json()
                sha = file_data["sha"]
                update_data = {
                    "message": commit_message,
                    "sha": sha,
                    "content": encoded_content,
                }
                async with session.put(
                    update_url, json=update_data, headers=headers
                ) as response:
                    return response.status == 200
            else:
                error_message = await response.text()
                raise Exception(
                    f"Failed to fetch file SHA. Status: {response.status}, Details: {error_message}"
                )


@client.tree.command(
    name="scriptban",
    description="Bans a user from the script (temporarily or permanently).",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
@app_commands.describe(
    user="The Discord user to ban (mention).",
    duration="Ban duration in seconds (leave blank or 0 for permanent).",
    reason="Reason for the ban.",
)
async def scriptban(
    interaction: discord.Interaction,
    user: discord.User,
    duration: int = 0,
    reason: str = "No reason provided.",
):
    try:
        await interaction.response.defer(ephemeral=True)

        # Fetch GitHub user data
        data = await fetch_user_data()

        if not data:
            await interaction.followup.send(
                "❌ Failed to fetch user data from GitHub.", ephemeral=True
            )
            return

        # User mention > Discord ID
        user_id = str(user.id)

        # Find user in fetched data using DiscordId
        user_data = next(
            (entry for entry in data if str(entry.get("DiscordId")) == user_id), None
        )

        if not user_data:
            await interaction.followup.send(
                f"❌ User `{user}` is not found in the whitelist. Please ensure the user is added to the whitelist.",
                ephemeral=True,
            )
            return

        # Banning
        now = datetime.now(ZoneInfo("America/New_York"))

        user_data["BanReason"] = reason

        if duration <= 0:
            # Perm ban
            user_data["Banned"] = "true"
            user_data["TempBan"] = "false"
            user_data["TempBanDuration"] = "null"
            user_data["TempBanEnd"] = "null"
            ban_type = "permanently"
            await user.send(
                f"🔒 You have been permanently banned from the script. Reason: {reason}."
            )
        else:
            # Temp ban
            end_time = now + timedelta(seconds=duration)
            user_data["TempBan"] = "true"
            user_data["TempBanDuration"] = str(duration)
            user_data["TempBanEnd"] = end_time.strftime("%Y-%m-%d %H:%M:%S %p")
            user_data["Banned"] = "false"
            ban_type = f"temporarily for {duration} seconds"
            # Handling ban shit
            # Just mark that we're doing a temp ban
            asyncio.create_task(start_temp_ban_timer(user, user_data, end_time, data))

        # Prepare GitHub payload
        content = json.dumps(data, indent=4)
        update_url = f"https://api.github.com/repos/669053713850403197963270290945742252531/Celestial/contents/Users.json"
        headers = {
            "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            try:
                # Fetch file's current SHA
                async with session.get(update_url, headers=headers) as response:
                    if response.status == 200:
                        file_data = await response.json()
                        sha = file_data["sha"]

                        # Prepare update data for GitHub
                        encoded_content = base64.b64encode(
                            content.encode("utf-8")
                        ).decode("utf-8")

                        update_data = {
                            "message": f"Ban updated for user {user.id}",
                            "sha": sha,
                            "content": encoded_content,
                        }

                        # Commit file on GitHub
                        async with session.put(
                            update_url, json=update_data, headers=headers
                        ) as response:
                            if response.status == 200:
                                await interaction.followup.send(
                                    f"✅ `{user}` has been {ban_type} banned. Reason: {reason}",
                                    ephemeral=True,
                                )
                            else:
                                error_message = await response.text()
                                await interaction.followup.send(
                                    f"❌ Failed to update user data on GitHub.\n"
                                    f"Status: {response.status}\n"
                                    f"Details: {error_message}",
                                    ephemeral=True,
                                )
                    else:
                        await interaction.followup.send(
                            "❌ Failed to fetch current file from GitHub.",
                            ephemeral=True,
                        )

            except aiohttp.ClientError as e:
                await interaction.followup.send(
                    f"❌ Error saving data: {e}", ephemeral=True
                )

    except discord.errors.NotFound as e:
        # Interaction has expired or already responded
        print(f"Interaction error: {e} - Cannot respond again.")

    except Exception as e:
        try:
            await interaction.followup.send(
                f"❌ Failed to ban user: {e}", ephemeral=True
            )
        except discord.errors.InteractionResponded:
            print(f"❌ Could not follow up — interaction already responded or expired.")
        except discord.HTTPException as err:
            print(f"❌ HTTP error on followup: {err}")


# Handle temp ban timer
async def start_temp_ban_timer(user, user_data, end_time, data):
    await asyncio.sleep(
        (end_time - datetime.now(ZoneInfo("America/New_York"))).total_seconds()
    )
    await unban_temp_user(user, user_data, data)


async def unban_temp_user(user, user_data, data):
    # Reset banned information
    user_data["Banned"] = "false"
    user_data["TempBan"] = "false"
    user_data["BanReason"] = "null"
    user_data["TempBanDuration"] = "null"
    user_data["TempBanEnd"] = "null"

    # Send dm to the user about their unban
    try:
        await user.send(
            "🔓 Your temporary ban from the script has expired. You have been unbanned."
        )
    except discord.DiscordException:
        print(f"Failed to send unban DM to {user.id}.")

    # Prepare the payload for GitHub
    content = json.dumps(data, indent=4)
    update_url = f"https://api.github.com/repos/669053713850403197963270290945742252531/Celestial/contents/Users.json"
    headers = {
        "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}",
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        try:
            # Fetch file to get sha for update
            async with session.get(update_url, headers=headers) as response:
                if response.status == 200:
                    file_data = await response.json()
                    sha = file_data["sha"]

                    # Prepare update data for GitHub
                    encoded_content = base64.b64encode(content.encode("utf-8")).decode(
                        "utf-8"
                    )

                    update_data = {
                        "message": f"Temporary ban expired for user {user.id}",
                        "sha": sha,
                        "content": encoded_content,
                    }

                    # Send PUT request to GitHub API
                    async with session.put(
                        update_url, json=update_data, headers=headers
                    ) as response:
                        if not response.status == 200:
                            error_message = await response.text()
                            print(
                                f"Failed to update user data on GitHub. Details: {error_message}"
                            )
                else:
                    print("Failed to fetch current file from GitHub.")

        except aiohttp.ClientError as e:
            print(f"Error saving data: {e}")


# Command: scriptunban


@client.tree.command(
    name="scriptunban",
    description="Unbans a user from the script.",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
@app_commands.describe(
    user="The Discord user to unban (mention).",
)
async def scriptunban(
    interaction: discord.Interaction,
    user: discord.User,
):
    try:
        # Fetch user data from the GitHub
        data = await fetch_user_data()

        if not data:
            await interaction.response.send_message(
                "❌ Failed to fetch user data from GitHub.", ephemeral=True
            )
            return

        user_id = str(user.id)

        user_data = next(
            (entry for entry in data if str(entry.get("DiscordId")) == user_id), None
        )

        if not user_data:
            await interaction.response.send_message(
                f"❌ User `{user}` is not found in the whitelist.",
                ephemeral=True,
            )
            return

        if user_data.get("Banned") == "false" and user_data.get("TempBan") == "false":
            await interaction.response.send_message(
                f"❌ User `{user}` is not banned.", ephemeral=True
            )
            return

        # Unbanning
        user_data["Banned"] = "false"
        user_data["TempBan"] = "false"
        user_data["BanReason"] = "null"
        user_data["TempBanDuration"] = "null"
        user_data["TempBanEnd"] = "null"

        content = json.dumps(data, indent=4)
        update_url = "https://api.github.com/repos/669053713850403197963270290945742252531/Celestial/contents/Users.json"
        headers = {
            "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(update_url, headers=headers) as response:
                    if response.status == 200:
                        file_data = await response.json()
                        sha = file_data["sha"]

                        encoded_content = base64.b64encode(
                            content.encode("utf-8")
                        ).decode("utf-8")
                        update_data = {
                            "message": f"Unban updated for user {user.id}",
                            "sha": sha,
                            "content": encoded_content,
                        }

                        async with session.put(
                            update_url, json=update_data, headers=headers
                        ) as response:
                            if response.status == 200:
                                await interaction.response.send_message(
                                    f"✅ `{user}` has been unbanned successfully.",
                                    ephemeral=True,
                                )

                                # Dm user
                                try:
                                    await user.send(
                                        "✅ You have been unbanned from using the script. You may now access it again."
                                    )
                                except discord.Forbidden:
                                    await interaction.followup.send(
                                        f"⚠️ `{user}` has been unbanned, but I couldn't DM them (they may have DMs disabled).",
                                        ephemeral=True,
                                    )
                            else:
                                error_message = await response.text()
                                await interaction.response.send_message(
                                    f"❌ Failed to update user data on GitHub.\n"
                                    f"Status: {response.status}\nDetails: {error_message}",
                                    ephemeral=True,
                                )
                    else:
                        await interaction.response.send_message(
                            "❌ Failed to fetch current file from GitHub.",
                            ephemeral=True,
                        )
            except aiohttp.ClientError as e:
                await interaction.response.send_message(
                    f"❌ Error saving data: {e}", ephemeral=True
                )

    except Exception as e:
        await interaction.response.send_message(
            f"❌ Failed to unban user: {e}", ephemeral=True
        )


# Command: dm


@client.tree.command(
    name="dm",
    description="Send a private message to a user from the bot.",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
@app_commands.describe(user="The user to message", message="The message to send")
async def dm(interaction: discord.Interaction, user: discord.User, message: str):
    if user.bot:
        await interaction.response.send_message(
            "🤖 You can't DM other bots.", ephemeral=True
        )
        return

    try:
        await user.send(message)
        await interaction.response.send_message(
            f"📬 Successfully sent a DM to {user.mention}.", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            f"❌ Couldn't DM {user.mention}. They may have DMs off or blocked the bot.",
            ephemeral=True,
        )
    except discord.HTTPException as e:
        await interaction.response.send_message(f"❌ DM failed: {e}", ephemeral=True)


# Command: clearregistrations


@client.tree.command(
    name="clearregistrations",
    description="Clears the HWID registration log.",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
async def clearregistrations(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🧹 Clearing registration messages...", ephemeral=True
    )

    registered_hwid_channel = interaction.client.get_channel(1325394667918987266)
    if not registered_hwid_channel:
        await interaction.followup.send(
            "❌ Couldn't find the HWID registration channel.", ephemeral=True
        )
        return

    deleted_count = 0
    async for message in registered_hwid_channel.history(limit=100):
        if message.author == client.user and message.embeds:
            embed = message.embeds[0]
            if embed.title == "HWID Registered":
                try:
                    await message.delete()
                    deleted_count += 1
                except discord.Forbidden:
                    pass
                except discord.HTTPException:
                    pass

    await interaction.followup.send(
        f"✅ Cleared {deleted_count} registration(s).", ephemeral=True
    )


# Command: edituser


VALID_FIELDS = {
    "HWID",
    "Identifier",
    "Rank",
    "JoinDate",
    "DiscordId",
    "Key",
    "Notes",
    "Banned",
    "TempBan",
    "BanReason",
    "TempBanDuration",
    "TempBanEnd",
}


@client.tree.command(
    name="edituser",
    description="Edit's a user's specific whitelist field.",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
@app_commands.describe(
    user="The user to edit",
    field="HWID, Identifier, Rank, JoinDate, DiscordId, Key, Notes, Banned, TempBan, BanReason, TempBanDuration, TempBanEnd.",
    value="New value to assign to the field",
)
async def edituser(
    interaction: discord.Interaction, user: discord.Member, field: str, value: str
):
    # GitHub
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        await interaction.response.send_message(
            "❌ GitHub token not found.", ephemeral=True
        )
        return

    # Field check
    if field not in VALID_FIELDS:
        await interaction.response.send_message(
            f"❌ Invalid field name: `{field}`.\nValid fields: {', '.join(sorted(VALID_FIELDS))}",
            ephemeral=True,
        )
        return

    try:
        # GitHub fetch
        github = Github(github_token)
        repo = github.get_repo("669053713850403197963270290945742252531/Celestial")
        file_path = "Users.json"
        file = repo.get_contents(file_path)
        data = json.loads(file.decoded_content.decode())

        # User mention > Discord ID
        user_id = str(user.id)
        target_entry = next(
            (entry for entry in data if entry.get("DiscordId") == user_id), None
        )

        if not target_entry:
            await interaction.response.send_message(
                "❌ User not found in the whitelist.", ephemeral=True
            )
            return

        # Update field
        old_value = target_entry.get(field, "null")
        target_entry[field] = value

        # GitHub commit
        updated_content = json.dumps(data, indent=4)
        repo.update_file(
            path=file.path,
            message=f"Edit '{field}' for Discord ID {user_id}",
            content=updated_content,
            sha=file.sha,
        )

        await interaction.response.send_message(
            f"✅ Updated `{field}` for <@{user_id}> from `{old_value}` to `{value}`.",
            ephemeral=True,
        )

    except Exception as e:
        await interaction.response.send_message(
            f"❌ An error occurred: `{e}`", ephemeral=True
        )


# Command: verifydata


@client.tree.command(
    name="verifydata",
    description="Verifies the integrity of the users with the raw version.",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
async def verifydata(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    api_url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{USERFILE_PATH}"
    raw_url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{USERFILE_PATH}"

    headers = {
        "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with aiohttp.ClientSession() as session:
        try:
            # Get API version
            async with session.get(api_url, headers=headers) as api_response:
                if api_response.status != 200:
                    raise Exception(f"GitHub API returned {api_response.status}")
                api_data = await api_response.json()
                api_content = base64.b64decode(api_data["content"]).decode("utf-8")
                api_json = json.loads(api_content)

            # Get raw version
            async with session.get(raw_url) as raw_response:
                if raw_response.status != 200:
                    raise Exception(f"Raw GitHub file returned {raw_response.status}")
                raw_content = await raw_response.text()
                raw_json = json.loads(raw_content)

            # Compare json objects
            if api_json == raw_json:
                embed = discord.Embed(
                    title="✅ Sync Verified!",
                    description=(
                        "The `Users.json` file is exactly the same between GitHub API and the raw version.\n\n"
                        f"🔍 **Entries Compared:** `{len(api_json)}`\n"
                        f"🕓 **Last Checked:** <t:{int(time.time())}:R>\n"
                        f"📁 **Repo:** [Celestial/Users.json](https://github.com/{REPO_OWNER}/{REPO_NAME}/blob/main/{USERFILE_PATH})"
                    ),
                    color=discord.Color.green(),
                )

            else:
                embed = discord.Embed(
                    title="⚠️ Data Mismatch!",
                    description=(
                        "The `Users.json` file from the GitHub API does **not** match the raw version.\n\n"
                        "⚠️ This may be due to a **delay in GitHub's raw file caching**. Raw URLs usually take a few minutes to reflect recent commits."
                    ),
                    color=discord.Color.orange(),
                )

            await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(
                f"❌ Error verifying data: `{e}`", ephemeral=True
            )


# Command: commitdetails


@client.tree.command(
    name="commitdetails",
    description="Check the status of a specific GitHub commit by SHA.",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
@app_commands.describe(sha="The SHA hash of the commit to check.")
async def commitdetails(interaction: discord.Interaction, sha: str):
    await interaction.response.defer(ephemeral=True)

    commit_url = f"https://api.github.com/repos/669053713850403197963270290945742252531/Celestial/commits/{sha}"
    headers = {
        "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(commit_url, headers=headers) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    return await interaction.followup.send(
                        f"❌ Failed to fetch commit info.\nStatus: {resp.status}\nDetails: {error}",
                        ephemeral=True,
                    )

                commit_data = await resp.json()

                message = commit_data["commit"]["message"]
                author = commit_data["commit"]["author"]["name"]
                date = commit_data["commit"]["author"]["date"]  # Get github date

                # Date > to local time zone
                date_obj = datetime.fromisoformat(
                    date.replace("Z", "+00:00")
                )  # Convert from github's UTC
                local_time = date_obj.astimezone(ZoneInfo("America/New_York"))

                # 12-hour
                formatted_time = local_time.strftime("%Y-%m-%d %I:%M:%S %p")

                stats = commit_data.get("stats", {})
                files = commit_data.get("files", [])

                file_list = "\n".join(f"- {file['filename']}" for file in files)

                embed = discord.Embed(
                    title=f"Commit {sha[:7]} Status",
                    description=message,
                    color=discord.Color.blurple(),
                )
                embed.add_field(name="Author", value=author, inline=True)
                embed.add_field(name="Date", value=formatted_time, inline=True)
                embed.add_field(
                    name="Stats",
                    value=f"+{stats.get('additions', 0)} / -{stats.get('deletions', 0)}",
                    inline=False,
                )
                embed.add_field(
                    name="Files Changed", value=file_list or "None", inline=False
                )

                await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error occurred: {e}", ephemeral=True)


# Command: commithistory


@client.tree.command(
    name="commithistory",
    description="Fetch recent commit history and SHAs for the whitelist file.",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
@app_commands.describe(limit="How many recent commits to show (1-20)")
async def commithistory(interaction: discord.Interaction, limit: int = 5):
    if not (1 <= limit <= 20):
        return await interaction.response.send_message(
            "❌ Please provide a number between 1 and 20 for the commit limit.",
            ephemeral=True,
        )

    await interaction.response.defer(ephemeral=True)

    commits_url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/commits?path={USERFILE_PATH}&per_page={limit}"
    headers = {"Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(commits_url, headers=headers) as resp:
                if resp.status != 200:
                    return await interaction.followup.send(
                        f"❌ Failed to fetch commit history. Status: {resp.status}",
                        ephemeral=True,
                    )

                commits = await resp.json()

                if not commits:
                    return await interaction.followup.send(
                        "⚠️ No recent commits found for the whitelist file.",
                        ephemeral=True,
                    )

                embed = discord.Embed(
                    title=f"📜 Last {len(commits)} Commits – `Users.json`",
                    color=discord.Color.blue(),
                )

                local_tz = pytz.timezone("US/Eastern")

                for commit in commits:
                    sha = commit.get("sha", "")
                    message = commit["commit"]["message"]
                    author = commit["commit"]["author"]["name"]
                    iso_time = commit["commit"]["author"]["date"]

                    # UTC > local timezone
                    dt = datetime.strptime(iso_time, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    )
                    local_dt = dt.astimezone(local_tz)
                    formatted_time = local_dt.strftime("%b %d, %Y at %I:%M %p %Z")

                    commit_url = commit["html_url"]

                    embed.add_field(
                        name=f"{author} – {message}",
                        value=f"🔗 [View Commit]({commit_url})\n🧾 SHA: `{sha}`\n🕒 {formatted_time}",
                        inline=False,
                    )

                await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(
            f"❌ Error fetching commits: {e}", ephemeral=True
        )


# Command: giveaccess


BOT_ACCESS_ROLE_ID = 1368809009456615434


@client.tree.command(
    name="giveaccess",
    description="Grants or revokes bot access to a user.",
    guild=GUILD_ID,
)
@app_commands.describe(
    user="Mention the user to modify access for",
    state="Grant or revoke access",
    notify="Whether to notify the user about the change (default: No)",
)
@app_commands.choices(
    state=[
        app_commands.Choice(name="true", value="true"),
        app_commands.Choice(name="false", value="false"),
    ],
    notify=[
        app_commands.Choice(name="Yes", value="true"),
        app_commands.Choice(name="No", value="false"),
    ],
)
@require_role(RESTRICTED_ROLE_ID)
async def giveaccess(
    interaction: discord.Interaction,
    user: discord.Member,
    state: app_commands.Choice[str],
    notify: app_commands.Choice[str] = None,
):
    role = interaction.guild.get_role(1368809009456615434)
    if not role:
        await interaction.response.send_message(
            "❌ Bot Access role not found.", ephemeral=True
        )
        return

    notify_user = notify is not None and notify.value == "true"

    try:
        if state.value == "true":
            if role in user.roles:
                await interaction.response.send_message(
                    f"⚠️ {user.mention} already has access.", ephemeral=True
                )
                return

            await user.add_roles(role)
            await interaction.response.send_message(
                f"✅ Granted access to {user.mention}.", ephemeral=True
            )
            if notify_user:
                try:
                    await user.send("✅ You've been granted access to the bot.")
                except discord.Forbidden:
                    pass

        else:  # state == "false"
            if role not in user.roles:
                await interaction.response.send_message(
                    f"⚠️ {user.mention} does not currently have access.", ephemeral=True
                )
                return

            await user.remove_roles(role)
            await interaction.response.send_message(
                f"🚫 Removed access from {user.mention}.", ephemeral=True
            )
            if notify_user:
                try:
                    await user.send("🚫 Your access to the bot has been revoked.")
                except discord.Forbidden:
                    pass

    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I don't have permission to modify that user's roles. Check my permissions and role order.",
            ephemeral=True,
        )


# Establish bot connection


token = os.getenv("DISCORD_TOKEN")
if not token:
    raise ValueError("DISCORD_TOKEN is not set in environment or .env file.")
keep_alive()
client.run(token)

import discord
from discord.ext import commands
from discord import app_commands, ui, Interaction, TextStyle, Embed, ButtonStyle
import aiohttp
import json
import hashlib
from github import Github  # Requires `PyGithub` library
import os
from dotenv import load_dotenv
import re
from datetime import datetime
from functools import wraps
from discord.ui import View, Button
from flask import Flask
from threading import Thread
from keep_alive import keep_alive

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

load_dotenv()


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


# Fetch user info from the provided GitHub JSON
async def fetch_user_data():
    url = "https://raw.githubusercontent.com/669053713850403197963270290945742252531/Celestial/refs/heads/main/Users.json"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    text = await response.text()
                    data = json.loads(text)

                    # Sanitize fields
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


# Hash the HWID using SHA-384
def hash_hwid(hwid):
    hwid = hwid.strip().lower()
    sha384_pattern = re.compile(r"^[0-9a-f]{96}$")

    if not sha384_pattern.match(hwid):
        raise ValueError(
            "HWID must be a valid SHA-384 hex string (96 lowercase hex characters)"
        )

    return hwid


# Command: myinfo
@client.tree.command(
    name="myinfo",
    description="Fetches the non-sensitive information about yourself.",
    guild=GUILD_ID,
)
async def myinfo(interaction: discord.Interaction):
    user_id = str(interaction.user.id)  # User's Discord ID as a string
    data = await fetch_user_data()

    if data is None:
        await interaction.response.send_message(
            "Failed to fetch user data. Please try again later.", ephemeral=True
        )
        return

    user_info = next((user for user in data if user["DiscordId"] == user_id), None)
    print(f"Fetched user info: {user_info}")

    if user_info:
        # Check if the user is banned or HWID is not set
        print(f"User Roles: {[role.id for role in interaction.user.roles]}")
        hashed_hwid = user_info.get("HashedHWID", "N/A")  # Fetch the HashedHWID safely
        print(f"Hashed HWID: {user_info.get('HWID', 'N/A')}")

        if not user_info.get("HWID"):
            await interaction.response.send_message(
                "You are not authorized to use this command.", ephemeral=True
            )
            return

        # Construct embed with non-sensitive data
        embed = discord.Embed(
            title="Your whitelist information", color=discord.Color.blue()
        )
        embed.add_field(
            name="Identifier", value=user_info.get("Identifier", "N/A"), inline=False
        )
        embed.add_field(name="Rank", value=user_info.get("Rank", "N/A"), inline=False)
        embed.add_field(
            name="JoinDate", value=user_info.get("JoinDate", "N/A"), inline=False
        )
        embed.add_field(
            name="DiscordId", value=user_info.get("DiscordId", "N/A"), inline=False
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(
            "No information found for your user ID.", ephemeral=True
        )


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
        if user_info.get("Banned", False):
            await interaction.response.send_message(
                f"🚫 {user.mention} is banned. Their information cannot be viewed.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"Whitelist Info for {user}",
            description=f"Requested by {interaction.user.mention}",
            color=discord.Color.green(),
        )

        for key, value in user_info.items():
            display_value = str(value) if value is not None else "N/A"
            embed.add_field(name=key, value=display_value, inline=False)

        await interaction.response.send_message(
            content=f"✅ Whitelist details for {user.mention}:",
            embed=embed,
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"❌ No whitelist entry found for {user.mention}.", ephemeral=True
        )


class HWIDModal(discord.ui.Modal, title="Register HWID"):
    hwid = discord.ui.TextInput(
        label="Enter your HWID",
        placeholder="e.g. XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
        min_length=8,
        max_length=100,  # Prevent abuse
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
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

        # Check for duplicates
        async for message in registered_hwid_channel.history(limit=100):
            if message.embeds:
                for embed in message.embeds:
                    for field in embed.fields:
                        if hashed_hwid == field.value:
                            await interaction.response.send_message(
                                "⚠️ This HWID is already registered.", ephemeral=True
                            )
                            return

        # Register
        embed = discord.Embed(title="HWID Registered", color=discord.Color.green())
        embed.add_field(
            name="User",
            value=f"{interaction.user.mention} ({interaction.user.id})",
            inline=False,
        )
        embed.add_field(name="Hashed HWID", value=hashed_hwid, inline=False)
        await registered_hwid_channel.send(embed=embed)

        await interaction.response.send_message(
            "✅ Your HWID has been successfully registered.", ephemeral=True
        )


@client.tree.command(
    name="registerhwid",
    description="Register your HWID to the whitelist system.",
    guild=GUILD_ID,
)
async def registerhwid(interaction: discord.Interaction):
    await interaction.response.send_modal(HWIDModal())


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

        repo_name = "669053713850403197963270290945742252531/Celestial"
        file_path = "Users.json"

        try:
            github = Github(github_token)
            repo = github.get_repo(repo_name)
            file = repo.get_contents(file_path)

            try:
                data = json.loads(file.decoded_content.decode())
            except json.JSONDecodeError:
                await interaction.response.send_message(
                    "⚠️ The JSON file is invalid.", ephemeral=True
                )
                return

            if any(entry.get("HWID") == hashed_hwid for entry in data):
                await interaction.response.send_message(
                    "⚠️ This HWID is already whitelisted.", ephemeral=True
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
    description="Add a user to the whitelist JSON file.",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
async def whitelist(interaction: discord.Interaction):
    await interaction.response.send_modal(WhitelistModal())


@client.tree.command(
    name="unwhitelist",
    description="Remove a user from the whitelist using their Discord mention.",
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
        repo_name = "669053713850403197963270290945742252531/Celestial"
        file_path = "Users.json"

        github = Github(github_token)
        repo = github.get_repo(repo_name)
        file = repo.get_contents(file_path)

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
    name="viewwhitelist", description="View all users in the whitelist.", guild=GUILD_ID
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
        repo_name = "669053713850403197963270290945742252531/Celestial"
        file_path = "Users.json"

        github = Github(github_token)
        repo = github.get_repo(repo_name)
        file = repo.get_contents(file_path)

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
                default=original_data[:4000],  # Truncate if needed
                max_length=4000,
                placeholder="Paste JSON here...",
            )
        )

    async def on_submit(self, interaction: Interaction):
        try:
            # Validate JSON
            json.loads(self.children[0].value)
            # Update file on GitHub
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
    name="editwhitelist", description="Edit the raw whitelist data.", guild=GUILD_ID
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
        repo = github.get_repo("669053713850403197963270290945742252531/Celestial")
        file = repo.get_contents("Users.json")
        content = file.decoded_content.decode()

        await interaction.response.send_modal(
            EditWhitelistModal(content, github, repo, "Users.json", file.sha)
        )

    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)


class HWIDModal(discord.ui.Modal, title="Register HWID"):
    hwid = discord.ui.TextInput(
        label="Enter your HWID",
        placeholder="e.g. XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
        min_length=8,
        max_length=100,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
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
        embed.add_field(name="Hashed HWID", value=hashed_hwid, inline=False)
        await registered_hwid_channel.send(embed=embed)

        await interaction.response.send_message(
            "✅ Your HWID has been successfully registered.", ephemeral=True
        )


class CreatePanelModal(discord.ui.Modal, title="Create Embed Panel"):
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

        class RegisterButton(discord.ui.Button):
            def __init__(self):
                super().__init__(label="Register", style=discord.ButtonStyle.success)

            async def callback(self, interaction: discord.Interaction):
                await interaction.response.send_modal(HWIDModal())

        class GetScriptButton(discord.ui.Button):
            def __init__(self):
                super().__init__(label="Get Script", style=discord.ButtonStyle.primary)

            async def callback(self, interaction: discord.Interaction):
                user_id = str(interaction.user.id)
                data = await fetch_user_data()

                if not data:
                    await interaction.response.send_message(
                        "⚠️ Failed to fetch your key.", ephemeral=True
                    )
                    return

                user_info = next((u for u in data if u["DiscordId"] == user_id), None)

                if not user_info:
                    await interaction.response.send_message(
                        "❌ You're not whitelisted.", ephemeral=True
                    )
                    return

                if user_info.get("Banned", False):
                    await interaction.response.send_message(
                        "🚫 You're banned.", ephemeral=True
                    )
                    return

                key = user_info.get("Key")
                if not key:
                    await interaction.response.send_message(
                        "❌ No key associated with your account.", ephemeral=True
                    )
                    return

                script = (
                    f'getgenv().script_key = "{key}";\n'
                    'loadstring(readfile("Celestial/Supported Games/Linoria Rewrite/2025 Rewrite/Break In 2 - Game new.lua"))() -- test script for experimental reasons'
                )

                await interaction.response.send_message(
                    f"Here is your script:\n```lua\n{script}\n```", ephemeral=True
                )

        view.add_item(RegisterButton())
        view.add_item(GetScriptButton())

        # Send to specific channel
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
    description="Creates the embed panel inside the panel channel.",
    guild=GUILD_ID,
)
@require_role(RESTRICTED_ROLE_ID)
async def createpanel(interaction: discord.Interaction):
    await interaction.response.send_modal(CreatePanelModal())


token = os.getenv("DISCORD_TOKEN")
if not token:
    raise ValueError("DISCORD_TOKEN is not set in environment or .env file.")
keep_alive()
client.run(token)

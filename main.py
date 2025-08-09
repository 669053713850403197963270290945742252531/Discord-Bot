# // Imports //

# spoof flask to allow local usage
import sys, os
from keep_alive import keep_alive
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

keep_alive()

import discord
from discord.ext import commands, tasks
from discord.app_commands import errors as app_errors
from discord import app_commands, InteractionResponded, ui, Interaction
import asyncio
from datetime import datetime, timezone, timedelta
import aiohttp
import json
from typing import Optional
import base64
import random
import string
import re
from discord.ui import Modal, TextInput, View, Button
import io
import subprocess
import hashlib
from collections import defaultdict

# // Constants //

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN is not set.")

GUILD_ID = 1263334150018961559
REQUIRED_ROLE_ID = 1368809009456615434
REGISTRATION_CHANNEL_ID = 1325394667918987266
REACTION_ROLE_CHANNEL_ID = 1403125677925863484

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_FILE_URL = "https://raw.githubusercontent.com/669053713850403197963270290945742252531/Celestial/refs/heads/main/Users.json"
OWNER = "669053713850403197963270290945742252531"
REPO = "Celestial"
FILE_PATH = "Users.json"
BRANCH = "main"

RAW_URL = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/refs/heads/{BRANCH}/{FILE_PATH}"
API_URL = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{FILE_PATH}?ref={BRANCH}"

headers = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json"
}

# // Intents & Setup //

reaction_roles_message_id = None
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

class Client(commands.Bot):
    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")
        try:
            guild_obj = discord.Object(id=GUILD_ID)
            synced = await self.tree.sync(guild=guild_obj)
            print(f"Synced {len(synced)} commands to guild.")
        except Exception as e:
            print(f"Error syncing commands: {e}")

bot = Client(command_prefix="!", intents=intents)
active_temp_access = set()
active_temp_whitelists = {}

# --- Utility Functions ---

async def safe_respond(interaction: discord.Interaction, content=None, **kwargs):
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content=content, **kwargs)
        else:
            await interaction.followup.send(content=content, **kwargs)
    except discord.NotFound:
        print("Interaction expired before it could be responded to.")
    except Exception as e:
        print(f"Failed to respond: {e}")


async def notify_user(user, action, moderator, reason, guild_name):
    try:
        if action == "muted":
            embed = discord.Embed(
                title=f"You have been muted in {guild_name}",
                description=f"**Reason:** {reason}",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
        elif action == "banned":
            embed = discord.Embed(
                title=f"You have been banned from {guild_name}",
                description=f"**Reason:** {reason}",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
        elif action == "unmuted":
            embed = discord.Embed(
                title=f"You have been unmuted in {guild_name}",
                description=f"**Reason:** {reason}",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
        elif action == "kicked":
            embed = discord.Embed(
                title=f"You have been kicked from {guild_name}",
                description=f"**Reason:** {reason}",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
        else:
            embed = discord.Embed(
                title=f"Notification from {guild_name}",
                description=f"**Reason:** {reason}",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )

        embed.set_footer(text=f"Moderator: {moderator}")
        await user.send(embed=embed)

    except Exception as e:
        print(f"Failed to send DM to {user}: {e}")

def has_role(role_id: int):
    async def predicate(interaction: discord.Interaction):
        if role_id in [role.id for role in interaction.user.roles]:
            return True
        raise app_commands.CheckFailure("You do not have the required role.")
    return app_commands.check(predicate)

def is_in_guild(guild_id: int):
    async def predicate(interaction: discord.Interaction):
        if interaction.guild and interaction.guild.id == guild_id:
            return True
        raise app_commands.CheckFailure("This command cannot be used in this server.")
    return app_commands.check(predicate)

async def can_moderate(interaction: discord.Interaction, target: discord.Member):
    author = interaction.user
    bot_member = interaction.guild.me

    if target == author:
        raise app_commands.CheckFailure("You cannot moderate yourself.")
    if target == bot_member:
        raise app_commands.CheckFailure("You cannot moderate the bot.")
    if target.top_role >= author.top_role and author != interaction.guild.owner:
        raise app_commands.CheckFailure("Target has equal or higher role than you.")
    if target.top_role >= bot_member.top_role:
        raise app_commands.CheckFailure("Target has equal or higher role than the bot.")
    return True

def generate_key(min_length=25, max_length=40):
    chars = string.ascii_letters + string.digits
    length = random.randint(min_length, max_length)
    return ''.join(random.choices(chars, k=length))

def is_valid_hwid(hwid: str) -> bool:
    # sha256 hash = 64 hex characters
    return bool(re.fullmatch(r"[a-fA-F0-9]{64}", hwid))

def is_valid_discord_id(discord_id: str) -> bool:
    if not discord_id.isdigit():
        return False
    snowflake = int(discord_id)
    return 1 << 17 < snowflake < 2**64

def get_hwid():
    try:
        output = subprocess.check_output("wmic csproduct get uuid", shell=True)
        lines = output.decode().splitlines()
        uuid = next((line.strip() for line in lines if line.strip() and line.strip() != "UUID"), None)

        if uuid:
            return uuid
    except Exception as e:
        print(f"Failed to retrieve HWID: {e}")
    return None

def is_valid_date(d: str) -> bool:
    try:
        datetime.strptime(d, "%Y-%m-%d")
        return True
    except:
        return False

# --- Commands ---

# // ping //

@bot.tree.command(name="ping", description="Returns the bot's latency.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ping(interaction: discord.Interaction):
    await safe_respond(interaction, f"Pong! Latency: {round(bot.latency * 1000)}ms", ephemeral=True)

# // ban //

@bot.tree.command(name="ban", description="Bans a user from the server, delete their recent messages?, specify a temporary ban duration?", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target="User to ban", reason="Ban reason", duration="Ban duration in minutes", preserve_messages="Keep the user's messages?")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ban(interaction: discord.Interaction, target: discord.User, reason: str = "None", duration: int = None, preserve_messages: bool = True):
    try:
        await interaction.response.send_message(f"Processing ban for {target.mention}...", ephemeral=True)

        member = interaction.guild.get_member(target.id)

        # Only run moderation checks and message deletion for members
        if member:
            await can_moderate(interaction, member)

            # DM
            try:
                embed = discord.Embed(title=f"You have been banned from {interaction.guild.name}", description=f"**Reason:** {reason}", color=discord.Color.red(), timestamp=datetime.now(timezone.utc))

                if duration:
                    unban_time = datetime.now(timezone.utc) + timedelta(minutes=duration)
                    timestamp = int(unban_time.timestamp())
                    minute_label = "minute" if duration == 1 else "minutes"

                    embed.add_field(name="Duration", value=f"{duration} {minute_label}", inline=True)
                    embed.add_field(name="Unban Time", value=f"<t:{timestamp}:F>\n<t:{timestamp}:T> (<t:{timestamp}:R>)", inline=True)

                await target.send(embed=embed)
            except Exception as e:
                print(f"Could not DM {member}: {e}")

            # If not preserve_messages, delete messages up to 1k

            if not preserve_messages:
                print(f"Deleting messages for {member}...")

                for channel in interaction.guild.text_channels:
                    try:
                        async for msg in channel.history(limit=1000):
                            if msg.author == member:
                                await msg.delete()
                                # Stop early if enough has been deleted
                                # break
                    except discord.Forbidden:
                        print(f"Missing permissions to delete messages in {channel.name}")
                    except Exception as e:
                        print(f"Error deleting messages in {channel.name}: {e}")
        else:
            # Banning globally

            try:
                await notify_user(target, "banned", interaction.user, reason, interaction.guild.name)
            except Exception as e:
                print(f"Failed to dm {target}: {e}")
            print(f"{target} was not found in server. Moderation checks and message deletion have been skipped.")

        # Ban
        await interaction.guild.ban(target, reason=reason, delete_message_seconds=0 if preserve_messages else 86400) # preserve_messages default = 1 day (86400)

        # Return ban summary
        summary = (f"```\n"
            f"Ban Summary:\n"
            f"User      : {target} ({target.id})\n"
            f"Reason    : {reason}\n"
            f"Messages  : {'Preserved' if preserve_messages else 'Deleted'}\n"
        )
        if duration:
            minute_label = "minute" if duration == 1 else "minutes"
            summary += f"Duration  : {duration} {minute_label}\n"
        summary += "```"

        await interaction.edit_original_response(content=summary)

        # Temp ban handling
        if duration:
            async def unban_later():
                await asyncio.sleep(duration * 60)
                try:
                    await interaction.guild.unban(target, reason="Temporary ban expired")
                except Exception as e:
                    print(f"Failed to unban {target}: {e}")

            bot.loop.create_task(unban_later())

    except app_commands.CheckFailure as e:
        if not interaction.response.is_done():
            await interaction.response.send_message(str(e), ephemeral=True)
        else:
            await interaction.followup.send(str(e), ephemeral=True)
    except discord.Forbidden:
        if not interaction.response.is_done():
            await interaction.response.send_message("Missing permissions to ban.", ephemeral=True)
        else:
            await interaction.followup.send("Missing permissions to ban.", ephemeral=True)
    except Exception as e:
        if not interaction.response.is_done():
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        else:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

# // checkban //

@bot.tree.command(name="checkban", description="Returns if the user is banned from the server.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to check the ban status of")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def checkban(interaction: discord.Interaction, user: discord.User):
    try:
        await interaction.response.defer(ephemeral=True)

        # Fetch current bans
        async for ban_entry in interaction.guild.bans(limit=None):
            if ban_entry.user.id == user.id:
                reason = ban_entry.reason or "No reason provided"

                embed = discord.Embed(title="User is Banned", color=discord.Color.red())
                embed.add_field(name="User", value=f"{user} (`{user.id}`)", inline=False)
                embed.add_field(name="Reason", value=reason, inline=False)

                return await interaction.followup.send(embed=embed, ephemeral=True)

        # When user is NOT found/not banned
        await interaction.followup.send(f"{user.mention} is not currently banned from this server.", ephemeral=True)

    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to view bans.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error while checking ban: `{e}`", ephemeral=True)

# // unban //

@bot.tree.command(name="unban", description="Unbans a user from the server.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user_id="The User ID of the user to unban")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def unban(interaction: discord.Interaction, user_id: str):
    if not user_id.isdigit() or int(user_id) < 1:
        await safe_respond(interaction, "Invalid user ID.", ephemeral=True)
        return

    user_id_int = int(user_id)

    try:
        # Fetch bans
        bans = [ban async for ban in interaction.guild.bans()]
        banned_entry = discord.utils.find(lambda ban: ban.user.id == user_id_int, bans)

        if not banned_entry:
            await safe_respond(interaction, "User is not banned.", ephemeral=True)
            return

        await interaction.guild.unban(banned_entry.user, reason=f"Unbanned by {interaction.user}")
        await safe_respond(interaction, f"Successfully unbanned <@{user_id}>.", ephemeral=True)

    except discord.Forbidden:
        await safe_respond(interaction, "Missing permissions to unban.", ephemeral=True)
    except Exception as e:
        await safe_respond(interaction, f"Error: {e}", ephemeral=True)

# // purge //

@bot.tree.command(name="purge", description="Deletes the specified amount of messages in the current channel.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(amount="Number of messages to delete")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def purge(interaction: discord.Interaction, amount: int):
    if amount < 1 or amount > 100:
        await interaction.response.defer(ephemeral=True) # fuck discord and its shitty timeout system
        return

    try:
        await interaction.response.defer(thinking=False, ephemeral=True)
        await interaction.channel.purge(limit=amount)

        # Delete deferred response so look like nothing happened
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass
    except discord.Forbidden:
        pass

# // kick //

@bot.tree.command(name="kick", description="Kicks a member from the server.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target="Member to kick", reason="Reason for kick")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def kick(interaction: discord.Interaction, target: discord.Member, reason: str = "Unspecified"):
    try:
        await can_moderate(interaction, target)

        # dm user before kick
        await notify_user(target, "kicked", interaction.user, reason, interaction.guild.name)

        await target.kick(reason=reason)
        await safe_respond(interaction, f"{target.mention} has been kicked.\nReason: {reason}", ephemeral=True)
    except app_commands.CheckFailure as e:
        await safe_respond(interaction, str(e), ephemeral=True)
    except discord.Forbidden:
        await safe_respond(interaction, "Missing permissions to kick.", ephemeral=True)
    except Exception as e:
        await safe_respond(interaction, f"Failed to kick: {e}", ephemeral=True)

# // mute //

@bot.tree.command(name="mute", description="Mutes a member from all channels.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target="Member to mute", reason="Reason for the mute")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def mute(interaction: discord.Interaction, target: discord.Member, reason: str = "Unspecified"):
    try:
        await interaction.response.send_message(f"Muting {target.mention}...", ephemeral=True)

        guild = interaction.guild
        muted_role = discord.utils.get(guild.roles, name="Muted")

        if not muted_role:
            try:
                muted_role = await guild.create_role(name="Muted", reason="Mute role required")
            except discord.Forbidden:
                await interaction.edit_original_response("Missing permission to create the muted role.")
                return

        allowed_perms = {
            "view_channel",
            "manage_channels",
            "manage_permissions",
            "manage_webhooks",
            "create_instant_invite",
        }

        all_channel_perms = [
            "add_reactions",
            "attach_files",
            "connect",
            "create_instant_invite",
            "deafen_members",
            "embed_links",
            "external_emojis",
            "manage_channels",
            "manage_messages",
            "manage_permissions",
            "manage_webhooks",
            "mention_everyone",
            "move_members",
            "mute_members",
            "priority_speaker",
            "read_message_history",
            "send_messages",
            "send_tts_messages",
            "speak",
            "stream",
            "use_external_emojis",
            "view_channel",
            "create_public_threads",
            "create_private_threads",
            "send_messages_in_threads",
            "use_external_stickers",
            "send_voice_messages",
            "create_polls",
        ]

        # Overwrite permissions on all channels to accomdate for the muted role
        for channel in guild.channels:
            overwrite = channel.overwrites_for(muted_role)
            for perm_name in all_channel_perms:
                if perm_name not in allowed_perms:
                    setattr(overwrite, perm_name, False)
                else:
                    setattr(overwrite, perm_name, None)  # Keep allowed perms untouched

            try:
                await channel.set_permissions(muted_role, overwrite=overwrite)
            except Exception as e:
                print(f"Failed to update permissions for channel {channel.name}: {e}")

        if muted_role in target.roles:
            await interaction.edit_original_response(content=f"{target.mention} is already muted.")
            return

        await target.add_roles(muted_role, reason=f"Muted by {interaction.user} - Reason: {reason}")
        await interaction.edit_original_response(content=f"{target.mention} has been muted.\nReason: {reason}")

        await notify_user(target, "muted", interaction.user, reason, guild.name)

    except Exception as e:
        # Replace original message with error message
        if not interaction.response.is_done():
            await interaction.response.send_message(f"Failed to mute: {e}", ephemeral=True)
        else:
            await interaction.edit_original_response(f"Failed to mute: {e}")

# // unmute //

@bot.tree.command(name="unmute", description="Unmutes a member from all channels.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target="Member to unmute", reason="Reason for the unmute")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def unmute(interaction: discord.Interaction, target: discord.Member, reason: str = "No reason provided"):
    try:
        await can_moderate(interaction, target)
    except app_commands.CheckFailure as e:
        await safe_respond(interaction, str(e), ephemeral=True)
        return

    muted_role = discord.utils.get(interaction.guild.roles, name="Muted")
    if not muted_role:
        await safe_respond(interaction, "Muted role missing.", ephemeral=True)
        return

    if muted_role not in target.roles:
        await safe_respond(interaction, f"{target.mention} is not muted.", ephemeral=True)
        return

    try:
        await target.remove_roles(muted_role, reason=f"Unmuted by {interaction.user}")
        await safe_respond(interaction, f"{target.mention} has been unmuted.", ephemeral=True)
        await notify_user(target, "unmuted", interaction.user, reason, interaction.guild.name)
    except discord.Forbidden:
        await safe_respond(interaction, "Missing permissions to remove roles.", ephemeral=True)

# // dm //

@bot.tree.command(name="dm", description="Sends a direct message to a user.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target="User to direct message", message="Message to send")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def dm(interaction: discord.Interaction, target: discord.User, message: str):
    try:
        await target.send(message)
        await safe_respond(interaction, f"Sent message to {target.mention}.", ephemeral=True)
    except discord.Forbidden as e:
        # Handle 'Cannot send messages to this user' error

        if e.code == 50007:
            await safe_respond(interaction, f"Failed to dm {target.mention}. They may have dms disabled, or you're not connected through a shared server or friendship.", ephemeral=True)
        else:
            await safe_respond(interaction, f"Failed to dm: {e}", ephemeral=True)
    except discord.HTTPException as e:
        # Handle 'Cannot send messages to this user' and blocked bot error

        if e.status == 400 and e.code == 50007:
            await safe_respond(interaction, f"Cannot DM {target.mention}. The user may have DMs disabled or has blocked the bot.", ephemeral=True)
        else:
            await safe_respond(interaction, f"Failed to send DM: {e}", ephemeral=True)
    except Exception as e:
        await safe_respond(interaction, f"Unexpected error: {e}", ephemeral=True)

# // myinfo //

@bot.tree.command(name="myinfo", description="Fetches your whitelist information from the database.", guild=discord.Object(id=GUILD_ID))
@is_in_guild(GUILD_ID)
async def myinfo(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(GITHUB_FILE_URL, headers=headers) as resp:
                if resp.status != 200:
                    await interaction.followup.send(f"Failed to fetch user data. (HTTP {resp.status})", ephemeral=True)
                    return
                text = await resp.text()
                users = json.loads(text)
        except Exception as e:
            await interaction.followup.send(f"Error fetching whitelist data: {e}", ephemeral=True)
            return

    discord_id = str(interaction.user.id)
    user_data: Optional[dict] = next((entry for entry in users if entry.get("DiscordId") == discord_id), None)

    if not user_data:
        await interaction.followup.send("You were not found in the user database.", ephemeral=True)
        return

    embed = discord.Embed(title=f"User Info: {interaction.user}", color=discord.Color.blue())
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

    # Parse join date string into timestamp

    join_date_raw = user_data.get("JoinDate")
    try:
        join_date_obj = datetime.strptime(join_date_raw, "%Y-%m-%d")
        join_timestamp = int(join_date_obj.timestamp())
        join_date_value = f"<t:{join_timestamp}:D>"
    except Exception:
        join_date_value = join_date_raw or "N/A"

    # Add fields
    embed.add_field(name="Identifier", value=user_data.get("Identifier", "N/A"), inline=True)
    embed.add_field(name="Rank", value=user_data.get("Rank", "N/A"), inline=True)
    embed.add_field(name="Join Date", value=join_date_value, inline=True)
    embed.add_field(name="HWID", value=f"||{user_data.get('HWID', 'N/A')}||", inline=True)
    embed.add_field(name="Key", value=f"||{user_data.get('Key', 'N/A')}||", inline=True)

    # Only add Notes if it's not the string "false"
    notes = user_data.get("Notes")
    if notes and notes.lower() != "false":
        embed.add_field(name="Notes", value=notes, inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)

# // verifydata

@bot.tree.command(name="verifydata", description="Validates if the raw database file matches the real database file.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def verifydata(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        async with aiohttp.ClientSession() as session:
            # Fetch raw file

            async with session.get(RAW_URL, headers=headers) as raw_resp:
                if raw_resp.status != 200:
                    await interaction.followup.send(f"Failed to fetch raw file. HTTP {raw_resp.status}", ephemeral=True)
                    return
                raw_content = await raw_resp.text()

            # Fetch real repo file

            async with session.get(API_URL, headers=headers) as api_resp:
                if api_resp.status != 200:
                    await interaction.followup.send(f"Failed to fetch real database. HTTP {api_resp.status}", ephemeral=True)
                    return
                api_data = await api_resp.json()
                encoded_content = api_data.get("content", "")
                real_content = base64.b64decode(encoded_content).decode("utf-8")

        # Comparison

        if raw_content.strip() == real_content.strip():
            embed = discord.Embed(title="Database Integrity Verified", description="The raw database matches the real database exactly.", color=discord.Color.green())
        else:
            embed = discord.Embed(
                title="Database Integrity Mismatch",
                description="The raw database does **not** match the real database.\nPossible causes:\n- CDN caching\n- Unauthorized edits\n- Commit mismatch (API Limitations)",
                color=discord.Color.red()
            )

        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error: {e}", ephemeral=True)

# // whitelist //

@bot.tree.command(name="whitelist", description="Adds a user to the database.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(hwid="Pre-hashed HWID in SHA-256", identifier="Username or alias", rank="User rank", discord_id="Discord ID of the user", notes="Notes to keep reminders about this user")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def whitelist(interaction: discord.Interaction, hwid: str, identifier: str, rank: str, discord_id: str, notes: str = "false"):
    await interaction.response.defer(ephemeral=True)

    # Checks

    if not is_valid_hwid(hwid):
        return await interaction.followup.send("Invalid HWID format. Must be 64 hex characters (SHA-256).", ephemeral=True)

    if not is_valid_discord_id(discord_id):
        return await interaction.followup.send("Invalid Discord ID.", ephemeral=True)

    if notes != "false" and not notes.strip():
        return await interaction.followup.send("Notes must be 'false' or a non-empty string.", ephemeral=True)
    
    generated_key = generate_key()

    async with aiohttp.ClientSession() as session:
        try:
            # Fetch current users via github

            async with session.get(API_URL, headers=headers) as get_resp:
                if get_resp.status != 200:
                    return await interaction.followup.send(f"Failed to fetch current user data: HTTP {get_resp.status}", ephemeral=True)
                data = await get_resp.json()
                content_b64 = data["content"]
                sha = data["sha"]
                existing = json.loads(base64.b64decode(content_b64).decode("utf-8"))

            today = datetime.now(timezone.utc).date().isoformat()
            new_entry = {
                "HWID": hwid,
                "Identifier": identifier,
                "Rank": rank,
                "JoinDate": today,
                "DiscordId": discord_id,
                "Key": generated_key,
                "Notes": notes
            }

            existing.append(new_entry)

            updated_content = json.dumps(existing, indent=4)
            updated_b64 = base64.b64encode(updated_content.encode()).decode("utf-8")

            commit_payload = {
                "message": f"Whitelist user: {identifier} ({discord_id})",
                "content": updated_b64,
                "branch": BRANCH,
                "sha": sha
            }

            async with session.put(API_URL, headers=headers, json=commit_payload) as put_resp:
                if put_resp.status != 200:
                    err = await put_resp.text()
                    return await interaction.followup.send(f"Failed to commit changes: HTTP {put_resp.status}\n{err}", ephemeral=True)

        except Exception as e:
            return await interaction.followup.send(f"Error: {e}", ephemeral=True)

    await interaction.followup.send(
        f"✅ **{identifier}** has been whitelisted.\n"
        f"HWID: ||`{hwid}`||",
        ephemeral=True
    )

# // unwhitelist //

@bot.tree.command(name="unwhitelist", description="Removes a user from the database.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(discord_id="Discord ID of the user to remove from the database.")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def unwhitelist(interaction: discord.Interaction, discord_id: str):
    await interaction.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        try:
            # Fetch current users via github
            async with session.get(API_URL, headers=headers) as get_resp:
                if get_resp.status != 200:
                    return await interaction.followup.send(f"Failed to fetch current data: HTTP {get_resp.status}", ephemeral=True)
                data = await get_resp.json()
                content_b64 = data["content"]
                sha = data["sha"]
                existing = json.loads(base64.b64decode(content_b64).decode("utf-8"))

            # Filter out entries matching the discord_id
            filtered = [entry for entry in existing if entry.get("DiscordId") != discord_id]

            if len(filtered) == len(existing):
                # Attempt to fetch user and get their mention

                user = None
                try:
                    user = await bot.fetch_user(int(discord_id))
                except Exception:
                    pass

                mention = user.mention if user else f"<@{discord_id}>"
                return await interaction.followup.send(f"{mention} was not found in database.", ephemeral=True)

            # Convert back to json string & base64 encode
            updated_content = json.dumps(filtered, indent=4)
            updated_b64 = base64.b64encode(updated_content.encode()).decode("utf-8")

            # Commit updated content back to github
            commit_payload = {
                "message": f"Unwhitelist user: {discord_id}",
                "content": updated_b64,
                "branch": BRANCH,
                "sha": sha
            }

            async with session.put(API_URL, headers=headers, json=commit_payload) as put_resp:
                if put_resp.status != 200:
                    err = await put_resp.text()
                    return await interaction.followup.send(f"Failed to commit changes: HTTP {put_resp.status}\n{err}", ephemeral=True)

        except Exception as e:
            return await interaction.followup.send(f"Error: {e}", ephemeral=True)

    await interaction.followup.send(f"User with Discord ID `{discord_id}` has been removed from the whitelist.", ephemeral=True)

# // editwhitelist //

class EditWhitelistModal(Modal):
    def __init__(self, initial_json: str):
        super().__init__(title="Edit Whitelist JSON")

        self.json_input = TextInput(
            label="Whitelist JSON",
            style=discord.TextStyle.paragraph,
            default=initial_json,
            max_length=1900  # Discord limit = 2000 chars for modal inputs
        )
        self.add_item(self.json_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_content = self.json_input.value.strip()

        try:
            parsed = json.loads(new_content)
        except json.JSONDecodeError as e:
            await interaction.response.send_message(f"Invalid JSON: {e}", ephemeral=True)
            return

        # Prepare commit

        async with aiohttp.ClientSession() as session:
            try:
                # Fetch latest sha again to avoid race conditions
                async with session.get(API_URL, headers=headers) as get_resp:
                    if get_resp.status != 200:
                        await interaction.response.send_message(f"Failed to fetch latest data: HTTP {get_resp.status}", ephemeral=True)
                        return
                    data = await get_resp.json()
                    sha = data["sha"]

                updated_b64 = base64.b64encode(new_content.encode()).decode("utf-8")
                commit_payload = {
                    "message": f"Edit whitelist by {interaction.user}",
                    "content": updated_b64,
                    "branch": BRANCH,
                    "sha": sha
                }

                async with session.put(API_URL, headers=headers, json=commit_payload) as put_resp:
                    if put_resp.status != 200:
                        err = await put_resp.text()
                        await interaction.response.send_message(f"Failed to commit changes: HTTP {put_resp.status}\n{err}", ephemeral=True)
                        return

            except Exception as e:
                await interaction.response.send_message(f"Error: {e}", ephemeral=True)
                return

        await interaction.response.send_message("Whitelist updated successfully.", ephemeral=True)


@bot.tree.command(name="editwhitelist", description="Edits the database JSON directly.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def editwhitelist(interaction: discord.Interaction):
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(API_URL, headers=headers) as get_resp:
                if get_resp.status != 200:
                    await interaction.response.send_message(f"Failed to fetch whitelist: HTTP {get_resp.status}", ephemeral=True)
                    return
                data = await get_resp.json()
                content_b64 = data["content"]
                decoded = base64.b64decode(content_b64).decode("utf-8")

        except Exception as e:
            await interaction.response.send_message(f"Error fetching whitelist: {e}", ephemeral=True)
            return

    modal = EditWhitelistModal(decoded)
    await interaction.response.send_modal(modal)

# // edituser //

@bot.tree.command(name="edituser", description="Edits a specific field of a whitelisted user.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to edit", field="Field to edit", value="New value for the field")
@app_commands.choices(field=[
    app_commands.Choice(name="HWID", value="HWID"),
    app_commands.Choice(name="Identifier", value="Identifier"),
    app_commands.Choice(name="Rank", value="Rank"),
    app_commands.Choice(name="JoinDate", value="JoinDate"),
    app_commands.Choice(name="DiscordId", value="DiscordId"),
    app_commands.Choice(name="Key", value="Key"),
    app_commands.Choice(name="Notes", value="Notes")
])
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def edituser(interaction: discord.Interaction, user: discord.Member, field: app_commands.Choice[str], value: str):
    await interaction.response.defer(ephemeral=True)

    field_name = field.value

    # Input checks per field
    if field_name == "HWID" and not is_valid_hwid(value):
        await interaction.followup.send("Invalid HWID format. Must be 64 hex characters and in SHA-256.", ephemeral=True)
        return
    if field_name == "JoinDate" and not is_valid_date(value):
        await interaction.followup.send("Invalid JoinDate format. Use yyyy-mm-dd.", ephemeral=True)
        return
    if field_name == "DiscordId" and not is_valid_discord_id(value):
        await interaction.followup.send("Invalid Discord ID format.", ephemeral=True)
        return

    async with aiohttp.ClientSession() as session:
        try:
            # Fetch current data

            async with session.get(API_URL, headers=headers) as get_resp:
                if get_resp.status != 200:
                    await interaction.followup.send(f"Failed to fetch data: HTTP {get_resp.status}", ephemeral=True)
                    return
                data = await get_resp.json()
                content_b64 = data["content"]
                sha = data["sha"]
                users = json.loads(base64.b64decode(content_b64).decode("utf-8"))

            discord_id_str = str(user.id)
            # Find user entry
            user_entry = next((u for u in users if u.get("DiscordId") == discord_id_str), None)
            if not user_entry:
                await interaction.followup.send(f"User {user.mention} not found in whitelist.", ephemeral=True)
                return

            # Update the field
            user_entry[field_name] = value

            # Prepare new content

            updated_content = json.dumps(users, indent=4)
            updated_b64 = base64.b64encode(updated_content.encode()).decode("utf-8")

            commit_payload = {
                "message": f"Edit whitelist user {user} - set {field_name} to {value}",
                "content": updated_b64,
                "branch": BRANCH,
                "sha": sha
            }

            async with session.put(API_URL, headers=headers, json=commit_payload) as put_resp:
                if put_resp.status != 200:
                    err = await put_resp.text()
                    await interaction.followup.send(f"Failed to commit changes: HTTP {put_resp.status}\n{err}", ephemeral=True)
                    return

        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
            return

    await interaction.followup.send(f"Updated {field_name} for {user.mention} to:\n```{value}```", ephemeral=True)

# // genkey //

@bot.tree.command(name="genkey", description="Generates a unique and random key using a strict alogrithm.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def genkey(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    key = generate_key()
    embed = discord.Embed(title="🔐 Generated Key", description=f"||`{key}`||", color=discord.Color.purple())
    embed.set_footer(text="Keep this key safe and only share to one specific individual.")
    
    await interaction.followup.send(embed=embed, ephemeral=True)

# // export //

@bot.tree.command(name="export", description="Export the current database.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def export(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(API_URL, headers=headers) as get_resp:
                if get_resp.status != 200:
                    return await interaction.followup.send(f"Failed to fetch database. (HTTP {get_resp.status})", ephemeral=True)

                data = await get_resp.json()
                content_b64 = data["content"]
                file_bytes = base64.b64decode(content_b64)
        except Exception as e:
            return await interaction.followup.send(f"Error fetching: {e}", ephemeral=True)

    # Send attachment
    file = discord.File(io.BytesIO(file_bytes), filename="Users.json")
    await interaction.followup.send("Here is the exported database:", file=file, ephemeral=True)

# // validatekey //

@bot.tree.command(name="validatekey", description="Validates and returns the full information for a key including ownership.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(key="Key to validate")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def validatekey(interaction: discord.Interaction, key: str):
    await interaction.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(API_URL, headers=headers) as get_resp:
                if get_resp.status != 200:
                    return await interaction.followup.send(f"Failed to fetch user data: HTTP {get_resp.status}", ephemeral=True)

                data = await get_resp.json()
                content_b64 = data["content"]
                users = json.loads(base64.b64decode(content_b64).decode("utf-8"))
        except Exception as e:
            return await interaction.followup.send(f"Error retrieving data: {e}", ephemeral=True)

    # Key search
    entry = next((user for user in users if user.get("Key") == key), None)

    if not entry:
        return await interaction.followup.send("Invalid key. No match found.", ephemeral=True)

    join_date = entry.get("JoinDate", "Unknown")
    try:
        timestamp = int(datetime.strptime(join_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        join_date_formatted = f"<t:{timestamp}:D>"
    except Exception:
        join_date_formatted = join_date

    embed = discord.Embed(
        title="Valid Key",
        description=f"**The info for key:** ||`{key}`||",
        color=discord.Color.green()
    )

    embed.add_field(name="Identifier", value=entry.get("Identifier", "N/A"), inline=True)
    embed.add_field(name="Rank", value=entry.get("Rank", "N/A"), inline=True)
    embed.add_field(name="Join Date", value=join_date_formatted, inline=True)
    embed.add_field(name="Discord ID", value=f"<@{entry.get('DiscordId')}>" if entry.get("DiscordId") else "N/A", inline=True)
    embed.add_field(name="Key", value=f"||`{entry.get('Key')}`||", inline=False)
    embed.add_field(name="HWID", value=f"||`{entry.get('HWID')}`||", inline=False)

    notes = entry.get("Notes")
    if notes and notes != "false":
        embed.add_field(name="Notes", value=notes, inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)

# // rollback //

@bot.tree.command(name="rollback", description="Rollback the user database to a specific commit.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(sha="The commit SHA to rollback to")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def rollback(interaction: discord.Interaction, sha: str):
    await interaction.response.defer(ephemeral=True)

    raw_url = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{sha}/{FILE_PATH}"

    async with aiohttp.ClientSession() as session:
        try:
            # Fetch old file content from specific commit

            async with session.get(raw_url) as raw_resp:
                if raw_resp.status != 200:
                    return await interaction.followup.send(f"Failed to fetch content from SHA `{sha}` (HTTP {raw_resp.status})", ephemeral=True)
                old_content = await raw_resp.text()
                json.loads(old_content)
        except Exception as e:
            return await interaction.followup.send(f"Error loading commit content: {e}", ephemeral=True)

        try:
            # Get current sha of Users.json

            async with session.get(API_URL, headers=headers) as get_resp:
                if get_resp.status != 200:
                    return await interaction.followup.send(f"Failed to fetch database metadata. (HTTP {get_resp.status})", ephemeral=True)
                data = await get_resp.json()
                current_sha = data["sha"]
        except Exception as e:
            return await interaction.followup.send(f"Error retrieving current database metadata: {e}", ephemeral=True)

        # Encode rollback content
        rollback_b64 = base64.b64encode(old_content.encode()).decode("utf-8")

        payload = {
            "message": f"Rollback Users.json to commit {sha}",
            "content": rollback_b64,
            "branch": BRANCH,
            "sha": current_sha
        }

        # Commit

        try:
            async with session.put(API_URL, headers=headers, json=payload) as put_resp:
                if put_resp.status != 200:
                    err = await put_resp.text()
                    return await interaction.followup.send(f"Commit failed (HTTP {put_resp.status}):\n{err}", ephemeral=True)
        except Exception as e:
            return await interaction.followup.send(f"Commit error: {e}", ephemeral=True)

    await interaction.followup.send(f"Successfully rolled back the database to commit `{sha}`.", ephemeral=True)

# // commithistory //

@bot.tree.command(name="commithistory", description="View the recent commit history.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(max_entries="Maximum number of commits to display (default 5, max 20)")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def commithistory(interaction: discord.Interaction, max_entries: int = 5):
    await interaction.response.defer(ephemeral=True)

    max_entries = min(max(1, max_entries), 20) # Clamp 1-20
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/commits"
    params = {
        "path": FILE_PATH,
        "sha": BRANCH,
        "per_page": max_entries
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    return await interaction.followup.send(f"Failed to fetch commits (HTTP {resp.status})", ephemeral=True)

                commits = await resp.json()

            if not commits:
                return await interaction.followup.send("No commits found.", ephemeral=True)

            embed = discord.Embed(title=f"Commit History: `{FILE_PATH}`", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))

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

                # Fetch commit stats

                stats_url = f"https://api.github.com/repos/{OWNER}/{REPO}/commits/{sha}"
                async with session.get(stats_url, headers=headers) as stats_resp:
                    stats_data = await stats_resp.json()
                    additions = stats_data.get("stats", {}).get("additions", 0)
                    deletions = stats_data.get("stats", {}).get("deletions", 0)

                date_ts = int(date_obj.timestamp())
                name = f"{date_str} — ||{sha}||"
                value = (
                    f"[View Commit]({html_url}) — {message}\n"
                    f"🟢 `+{additions}` 🔴 `-{deletions}`\n"
                    f"👤 **{author}** • <t:{date_ts}:R>\n"
                    "\u200b\n" # zero width space + newline to gap | shoutout google 👍
                )

                embed.add_field(name=name, value=value, inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

# // fetchcommits //

@bot.tree.command(name="fetchcommit", description="Fetches the details for a specific commit.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(sha="Commit SHA to fetch")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def fetchcommit(interaction: discord.Interaction, sha: str):
    await interaction.response.defer(ephemeral=True)
    commit_url = f"https://api.github.com/repos/{OWNER}/{REPO}/commits/{sha}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(commit_url, headers=headers) as resp:
                if resp.status != 200:
                    return await interaction.followup.send(f"Commit not found or an unexpected error has occurred. (HTTP {resp.status})", ephemeral=True)
                data = await resp.json()

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
                timestamp=date_obj
            )
            embed.set_author(name=author)
            embed.add_field(name="Additions", value=f"🟢 +{additions}", inline=True)
            embed.add_field(name="Deletions", value=f"❌ -{deletions}", inline=True)
            embed.add_field(name="Date", value=f"<t:{date_ts}:F>", inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"Error fetching commit: {e}", ephemeral=True)

# // fetchuser //

@bot.tree.command(name="fetchuser", description="Fetches all stored info about a user.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="The user to look up")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def fetchuser(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        try:
            # Fetch and decode json file

            async with session.get(GITHUB_FILE_URL, headers=headers) as resp:
                if resp.status != 200:
                    return await interaction.followup.send(f"Failed to fetch data. (HTTP {resp.status})", ephemeral=True)

                raw_text = await resp.text()
                users = json.loads(raw_text)

        except Exception as e:
            return await interaction.followup.send(f"Error: {e}", ephemeral=True)

    # Look up by user id

    discord_id = str(user.id)
    user_data = next((entry for entry in users if entry.get("DiscordId") == discord_id), None)

    if not user_data:
        return await interaction.followup.send(f"No data found for {user.mention}.", ephemeral=True)

    # Fetch member from guild to fetch roles and join date
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(user.id) if guild else None

    # Number of roles
    num_roles = len(member.roles) - 1 if member else "Unknown"

    # Format server join date as a timestamp
    if member and member.joined_at:
        join_ts = int(member.joined_at.replace(tzinfo=timezone.utc).timestamp())
        server_join_display = f"<t:{join_ts}:D>"
    else:
        server_join_display = "Unknown"

    embed = discord.Embed(title=f"User Info: {user.name}", color=discord.Color.teal(), timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=user.display_avatar.url)

    # Format join date as timestamp
    join_date = user_data.get("JoinDate", "Unknown")
    try:
        timestamp = int(datetime.strptime(join_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        join_date_display = f"<t:{timestamp}:D>"
    except:
        join_date_display = join_date

    # Fields
    fields = [
        ("Identifier", user_data.get("Identifier")),
        ("Rank", user_data.get("Rank")),
        ("Join Date", join_date_display),
        ("HWID", f"||{user_data.get('HWID')}||" if user_data.get("HWID") else "N/A"),
        ("Key", f"||{user_data.get('Key')}||" if user_data.get("Key") else "N/A"),
        ("Discord ID", f"{user_data.get('DiscordId')} ({user.mention})"),
        ("Server Join Date", server_join_display),
        ("Number of Roles", str(num_roles))
    ]

    if user_data.get("Notes") and user_data["Notes"] != "false":
        fields.append(("Notes", user_data["Notes"]))

    for name, value in fields:
        embed.add_field(name=name, value=value or "N/A", inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)

# // fetchdupes //

@bot.tree.command(name="fetchdupes", description="Find duplicate values in the whitelist.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(field="The field to search for duplicates in")
@app_commands.choices(field=[
    app_commands.Choice(name="HWID", value="HWID"),
    app_commands.Choice(name="Identifier", value="Identifier"),
    app_commands.Choice(name="Discord ID", value="DiscordId"),
    app_commands.Choice(name="Key", value="Key")
])
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def fetchdupes(interaction: discord.Interaction, field: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(GITHUB_FILE_URL, headers=headers) as resp:
                if resp.status != 200:
                    return await interaction.followup.send(f"Failed to fetch user data. (HTTP {resp.status})", ephemeral=True)
                text = await resp.text()
                users = json.loads(text)
        except Exception as e:
            return await interaction.followup.send(f"Error fetching data: {e}", ephemeral=True)

    field_name = field.value
    value_map = defaultdict(list)

    for entry in users:
        value = entry.get(field_name)
        if not value or value == "false":
            continue
        value_map[value].append(entry)

    dupes = {k: v for k, v in value_map.items() if len(v) > 1}

    if not dupes:
        return await interaction.followup.send(f"No duplicates found for **{field_name}**.", ephemeral=True)

    embed = discord.Embed(title=f"🔁 Duplicate Entries: `{field_name}`", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))

    for value, entries in dupes.items():
        identifiers = ", ".join(entry.get("Identifier", "Unknown") for entry in entries)
        value_display = f"`{value}`" if len(value) <= 50 else f"`{value[:47]}...`"
        embed.add_field(name=value_display, value=f"Count: `{len(entries)}` — {identifiers}", inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)

# // viewwhitelist //

class WhitelistPaginator(ui.View):
    def __init__(self, embeds: list[discord.Embed], author_id: int):
        super().__init__(timeout=120)
        self.embeds = embeds
        self.author_id = author_id
        self.index = 0

        self.prev_button = ui.Button(label="⏮️ Previous", style=discord.ButtonStyle.secondary)
        self.next_button = ui.Button(label="⏭️ Next", style=discord.ButtonStyle.secondary)
        self.delete_button = ui.Button(label="🗑️ Delete", style=discord.ButtonStyle.danger)

        self.prev_button.callback = self.prev_page
        self.next_button.callback = self.next_page
        self.delete_button.callback = self.delete_message

        self.add_item(self.prev_button)
        self.add_item(self.next_button)
        self.add_item(self.delete_button)

        self.update_button_states()

    def update_button_states(self):
        self.prev_button.disabled = self.index == 0
        self.next_button.disabled = self.index >= len(self.embeds) - 1

    async def prev_page(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("You can't control this panel.", ephemeral=True)
        self.index -= 1
        self.update_button_states()
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

    async def next_page(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("You can't control this panel.", ephemeral=True)
        self.index += 1
        self.update_button_states()
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

    async def delete_message(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("You can't delete this message.", ephemeral=True)
        await interaction.message.delete

class EditUserModal(Modal):
    def __init__(self, user_data, whitelist_view):
        super().__init__(title=f"Edit {user_data.get('Identifier', 'User')}")

        self.user_data = user_data
        self.whitelist_view = whitelist_view

        self.identifier = TextInput(label="Identifier", default=user_data.get("Identifier", ""), required=True)
        self.rank = TextInput(label="Rank", default=user_data.get("Rank", ""), required=True)
        self.hwid = TextInput(label="HWID", default=user_data.get("HWID", ""), required=False)
        self.key = TextInput(label="Key", default=user_data.get("Key", ""), required=False)
        self.notes = TextInput(label="Notes", default=user_data.get("Notes", ""), style=discord.TextStyle.paragraph, required=False)

        self.add_item(self.identifier)
        self.add_item(self.rank)
        self.add_item(self.hwid)
        self.add_item(self.key)
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction):
        # Update user data dictionary with form values

        self.user_data["Identifier"] = self.identifier.value
        self.user_data["Rank"] = self.rank.value
        self.user_data["HWID"] = self.hwid.value or "N/A"
        self.user_data["Key"] = self.key.value or "N/A"
        self.user_data["Notes"] = self.notes.value or "false"

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(API_URL, headers=headers) as get_resp:
                    if get_resp.status != 200:
                        await interaction.response.send_message(f"Failed to fetch data: HTTP {get_resp.status}", ephemeral=True)
                        return
                    data = await get_resp.json()
                    content_b64 = data["content"]
                    sha = data["sha"]
                    existing = json.loads(base64.b64decode(content_b64).decode("utf-8"))

                discord_id = self.user_data.get("DiscordId")
                for i, u in enumerate(existing):
                    if u.get("DiscordId") == discord_id:
                        existing[i] = self.user_data
                        break

                updated_content = json.dumps(existing, indent=4)
                updated_b64 = base64.b64encode(updated_content.encode()).decode("utf-8")

                commit_payload = {
                    "message": f"Edited whitelist user: {self.user_data.get('Identifier', 'N/A')} ({discord_id})",
                    "content": updated_b64,
                    "branch": BRANCH,
                    "sha": sha
                }

                async with session.put(API_URL, headers=headers, json=commit_payload) as put_resp:
                    if put_resp.status != 200:
                        err = await put_resp.text()
                        await interaction.response.send_message(f"Failed to commit changes: HTTP {put_resp.status}\n{err}", ephemeral=True)
                        return

                self.whitelist_view.users = existing
                self.whitelist_view.update_buttons()
                embed = self.whitelist_view.create_embed()

                await interaction.response.edit_message(content=f"User **{self.user_data.get('Identifier')}** updated.", embed=embed, view=self.whitelist_view)
            except Exception as e:
                await interaction.response.send_message(f"Error: {e}", ephemeral=True)

class WhitelistView(View):
    def __init__(self, bot, users, current_index=0):
        super().__init__(timeout=None)
        self.bot = bot
        self.users = users
        self.current_index = current_index
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = self.current_index == 0
        self.next_button.disabled = self.current_index >= len(self.users) - 1
        self.delete_button.disabled = len(self.users) == 0

    @discord.ui.button(label="⏮️ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: Button):
        self.current_index = max(0, self.current_index - 1)
        self.update_buttons()
        embed = self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: Button):
        self.current_index = min(len(self.users) - 1, self.current_index + 1)
        self.update_buttons()
        embed = self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="✏️ Edit User", style=discord.ButtonStyle.primary)
    async def edit_button(self, interaction: discord.Interaction, button: Button):
        user_data = self.users[self.current_index]
        modal = EditUserModal(user_data, self)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🗑️ Delete User", style=discord.ButtonStyle.danger)
    async def delete_button(self, interaction: discord.Interaction, button: Button):
        user_to_delete = self.users[self.current_index]
        identifier = user_to_delete.get("Identifier", "N/A")

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(API_URL, headers=headers) as get_resp:
                    if get_resp.status != 200:
                        await interaction.response.send_message(f"Failed to fetch data for deletion: HTTP {get_resp.status}", ephemeral=True)
                        return
                    data = await get_resp.json()
                    content_b64 = data["content"]
                    sha = data["sha"]
                    existing = json.loads(base64.b64decode(content_b64).decode("utf-8"))

                existing = [u for u in existing if u.get("DiscordId") != user_to_delete.get("DiscordId")]

                updated_content = json.dumps(existing, indent=4)
                updated_b64 = base64.b64encode(updated_content.encode()).decode("utf-8")
                commit_payload = {
                    "message": f"Deleted whitelist user: {identifier} ({user_to_delete.get('DiscordId', 'N/A')})",
                    "content": updated_b64,
                    "branch": BRANCH,
                    "sha": sha
                }

                async with session.put(API_URL, headers=headers, json=commit_payload) as put_resp:
                    if put_resp.status != 200:
                        err = await put_resp.text()
                        await interaction.response.send_message(f"Failed to commit deletion: HTTP {put_resp.status}\n{err}", ephemeral=True)
                        return

                self.users = existing
                if self.current_index >= len(self.users):
                    self.current_index = max(0, len(self.users) - 1)

                self.update_buttons()
                if self.users:
                    embed = self.create_embed()
                    await interaction.response.edit_message(content=f"Deleted user **{identifier}**.", embed=embed, view=self)
                else:
                    for child in self.children:
                        child.disabled = True
                    await interaction.response.edit_message(content=f"Deleted user **{identifier}**. The database is now empty.", embed=None, view=self)

            except Exception as e:
                await interaction.response.send_message(f"Error deleting user: {e}", ephemeral=True)

    def create_embed(self):
        if not self.users:
            embed = discord.Embed(title="Database is empty", color=discord.Color.red())
            return embed

        user_data = self.users[self.current_index]

        embed = discord.Embed(title=f"Whitelist Entry {self.current_index + 1}/{len(self.users)}", color=discord.Color.blue())

        embed.add_field(name="Identifier", value=user_data.get("Identifier", "N/A"), inline=True)
        embed.add_field(name="Rank", value=user_data.get("Rank", "N/A"), inline=True)

        join_date = user_data.get("JoinDate", "N/A")
        try:
            dt = datetime.fromisoformat(join_date)
            join_date = f"<t:{int(dt.replace(tzinfo=timezone.utc).timestamp())}:D>"
        except Exception:
            pass

        # Fields

        embed.add_field(name="Join Date", value=join_date, inline=True)
        embed.add_field(name="HWID", value=f"||`{user_data.get('HWID', '')}`||", inline=False)
        embed.add_field(name="Key", value=f"||`{user_data.get('Key', '')}`||", inline=False)

        notes = user_data.get("Notes", "false")
        if notes != "false" and notes.strip() != "":
            embed.add_field(name="Notes", value=notes, inline=False)

        discord_id = int(user_data.get("DiscordId", 0))
        member = self.bot.get_user(discord_id)
        if member:
            embed.set_thumbnail(url=member.display_avatar.url)
        else:
            embed.set_thumbnail(url="https://cdn.discordapp.com/embed/avatars/0.png")

        return embed

@bot.tree.command(name="viewwhitelist", description="View all whitelist entries.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def viewwhitelist(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        async with session.get(API_URL, headers=headers) as resp:
            if resp.status != 200:
                return await interaction.followup.send(f"Failed to fetch whitelist: HTTP {resp.status}", ephemeral=True)
            data = await resp.json()
            content_b64 = data["content"]
            users = json.loads(base64.b64decode(content_b64).decode("utf-8"))

    if not users:
        return await interaction.followup.send("No database entries found.", ephemeral=True)

    view = WhitelistView(bot, users)
    embed = view.create_embed()
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

# // register

@bot.tree.command(name="register", description="Submit your info to be reviewed and whitelisted.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(identifier="Your identifier (username, alias, etc.)")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def register(interaction: discord.Interaction, identifier: str):
    await interaction.response.defer(ephemeral=True)

    discord_id_str = str(interaction.user.id)

    # Check whitelist json for existing discord id

    async with aiohttp.ClientSession() as session:
        async with session.get(API_URL, headers=headers) as resp:
            if resp.status != 200:
                return await interaction.followup.send(f"Failed to fetch whitelist data: HTTP {resp.status}", ephemeral=True)
            data = await resp.json()
            content_b64 = data["content"]
            whitelist_users = json.loads(base64.b64decode(content_b64).decode("utf-8"))

    for user in whitelist_users:
        if user.get("DiscordId") == discord_id_str:
            return await interaction.followup.send("You are already whitelisted.", ephemeral=True)

    # Check for existing registration

    reg_channel = bot.get_channel(REGISTRATION_CHANNEL_ID)
    if not reg_channel:
        return await interaction.followup.send("Registration channel not found.", ephemeral=True)

    messages = [msg async for msg in reg_channel.history(limit=100)]
    for msg in messages:
        if msg.embeds:
            embed = msg.embeds[0]
            for field in embed.fields:
                if discord_id_str in field.value:
                    return await interaction.followup.send("You have already registered before.", ephemeral=True)

    # Registration

    rank = "User"
    join_date = datetime.now(timezone.utc).date().isoformat()
    hwid_raw = get_hwid()
    if not hwid_raw:
        hwid_raw = "UNKNOWN_HWID"
    hwid_hash = hashlib.sha256(hwid_raw.encode()).hexdigest()

    embed = discord.Embed(title="Registration Successful", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.add_field(name="Identifier", value=identifier, inline=True)
    embed.add_field(name="Rank", value=rank, inline=True)
    embed.add_field(name="Discord ID", value=discord_id_str, inline=True)
    embed.add_field(name="Join Date", value=f"<t:{int(datetime.strptime(join_date, '%Y-%m-%d').timestamp())}:D>", inline=True)
    embed.add_field(name="HWID", value=f"||`{hwid_hash}`||", inline=False)

    await reg_channel.send(embed=embed)

    await interaction.followup.send(
        f"Registration completed:\n"
        f"Identifier: {identifier}\n"
        f"Rank: {rank}\n"
        f"Discord ID: {discord_id_str}\n"
        f"Join Date: {join_date}\n"
        f"HWID: ||`{hwid_hash}`||",
        ephemeral=True
    )

# // checkregistration

@bot.tree.command(name="checkregistration", description="Checks if a user is registered.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="The user to check registration for")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def checkregistration(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    discord_id_str = str(user.id)

    # Check whitelist file

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, headers=headers) as resp:
                if resp.status != 200:
                    return await interaction.followup.send(f"Failed to fetch whitelist: HTTP {resp.status}", ephemeral=True)
                data = await resp.json()
                content_b64 = data["content"]
                users = json.loads(base64.b64decode(content_b64).decode("utf-8"))
    except Exception as e:
        return await interaction.followup.send(f"Error fetching whitelist: {e}", ephemeral=True)

    whitelist_registered = any(u.get("DiscordId") == discord_id_str for u in users)

    # Check registration embeds and find message link if found
    reg_channel = bot.get_channel(REGISTRATION_CHANNEL_ID)
    if not reg_channel:
        return await interaction.followup.send("Registration channel not found.", ephemeral=True)

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
        return await interaction.followup.send(f"Error reading registration embeds: {e}", ephemeral=True)

    # Reply
    if whitelist_registered and registered_in_channel:
        status_msg = f"User **{user}** is **registered** in both whitelist and registration channel.\n[View Registration Message]({registration_message_url})"
    elif whitelist_registered:
        status_msg = f"User **{user}** is **registered** in the whitelist only."
    elif registered_in_channel:
        status_msg = f"User **{user}** is **registered** in the registration channel only.\n[View Registration Message]({registration_message_url})"
    else:
        status_msg = f"User **{user}** is **not** registered."

    await interaction.followup.send(status_msg, ephemeral=True)

# // clearregistrations

class ConfirmClearView(View):
    def __init__(self, author_id):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.confirmed = False

    @discord.ui.button(label="Confirm Clear", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("You cannot confirm this action.", ephemeral=True)

        self.confirmed = True
        self.stop()
        await interaction.response.edit_message(content="Clearing registrations...", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("You cannot cancel this action.", ephemeral=True)

        self.confirmed = False
        self.stop()
        await interaction.response.edit_message(content="Cancelled clearing registrations.", view=None)


@bot.tree.command(name="clearregistrations", description="Clear all messages in the registration channel.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def clearregistrations(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    # Check bot perms

    reg_channel = bot.get_channel(REGISTRATION_CHANNEL_ID)
    if not reg_channel:
        return await interaction.followup.send("Registration channel not found.", ephemeral=True)

    permissions = reg_channel.permissions_for(interaction.guild.me)
    if not permissions.manage_messages:
        return await interaction.followup.send("I need Manage Messages permission in the registration channel to clear messages.", ephemeral=True)

    # Confirmation
    view = ConfirmClearView(interaction.user.id)
    await interaction.followup.send("Are you sure you want to clear all registration messages? This action cannot be undone.", view=view, ephemeral=True)

    await view.wait() # Confirmation wait

    if not view.confirmed:
        return # User cancel or timed out

    # Bulk delete messages

    deleted_count = 0
    try:
        while True:
            msgs = [msg async for msg in reg_channel.history(limit=100)]
            if not msgs:
                break
            await reg_channel.delete_messages(msgs)
            deleted_count += len(msgs)
            if len(msgs) < 100:
                break
    except Exception as e:
        return await interaction.followup.send(f"Failed to clear messages: {e}", ephemeral=True)

    await interaction.followup.send(f"Cleared {deleted_count} registrations.", ephemeral=True)

# // reactionrole

@bot.tree.command(name="reactionrole", description="Creates a reaction role panel or applies a reaction role to an already existing panel.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(emoji="Emoji", role="Role to assign", note="What is the purpose of this role?")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def reactionrole(interaction: discord.Interaction, emoji: str, role: discord.Role, note: str = None):
    await interaction.response.defer(ephemeral=True)

    channel = bot.get_channel(REACTION_ROLE_CHANNEL_ID)
    global reaction_roles_message_id

    if reaction_roles_message_id is None:
        embed = discord.Embed(title="React to assign roles", description="", color=discord.Color.blurple())
        msg = await channel.send(embed=embed)
        reaction_roles_message_id = msg.id
    else:
        try:
            msg = await channel.fetch_message(reaction_roles_message_id)
        except discord.NotFound:
            # If panel deleted, recreate it and save the new id\

            embed = discord.Embed(title="React to assign roles", description="", color=discord.Color.blurple())
            msg = await channel.send(embed=embed)
            reaction_roles_message_id = msg.id

    embed = msg.embeds[0] if msg.embeds else discord.Embed(title="React to assign roles", color=discord.Color.blurple())
    lines = embed.description.split("\n") if embed.description else []

    if any(emoji in line for line in lines):
        return await interaction.followup.send("That emoji is already used.", ephemeral=True)
    if any(role.mention in line for line in lines):
        return await interaction.followup.send("That role is already assigned.", ephemeral=True)

    if note:
        lines.append(f"{emoji} — {role.mention} *( {note} )*")
    else:
        lines.append(f"{emoji} — {role.mention}")

    embed.description = "\n".join(lines)

    await msg.edit(embed=embed)
    await msg.add_reaction(emoji)

    await interaction.followup.send(f"Added reaction role: {emoji} for {role.mention}" + (f" — {note}" if note else ""), ephemeral=True)

# // toggleaccess

@bot.tree.command(name="toggleaccess", description="Toggle the Bot Access role for a user.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to toggle the role for")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def toggleaccess(interaction: discord.Interaction, user: discord.Member):
    guild = interaction.guild
    role = guild.get_role(REQUIRED_ROLE_ID)
    if not role:
        return await interaction.response.send_message("Bot Access role not found.", ephemeral=True)

    if role in user.roles:
        await user.remove_roles(role, reason=f"Toggled off Bot Access role by {interaction.user}")
        await interaction.response.send_message(f"Removed {role.name} role from {user.mention}.", ephemeral=True)
    else:
        await user.add_roles(role, reason=f"Toggled on Bot Access role by {interaction.user}")
        await interaction.response.send_message(f"Granted {role.name} role to {user.mention}.", ephemeral=True)
        
# // togglelock //

@bot.tree.command(name="togglelock", description="Toggles the lock or unlock state on the current channel.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def togglelock(interaction: discord.Interaction):
    channel = interaction.channel
    everyone_role = interaction.guild.default_role
    overwrite = channel.overwrites_for(everyone_role)

    is_locked = overwrite.send_messages is False

    if is_locked:
        # Unlock
        overwrite.send_messages = None
        action = "unlocked"
    else:
        # Lock
        overwrite.send_messages = False
        action = "locked"

    await channel.set_permissions(everyone_role, overwrite=overwrite)
    await interaction.response.send_message(f"{channel.Name} has been {action}.", ephemeral=True)

# // togglelockdown //

@bot.tree.command(name="togglelockdown", description="Toggles the lock or unlock state on all text channels.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def togglelockdown(interaction: discord.Interaction):
    guild = interaction.guild
    everyone_role = guild.default_role

    text_channels = [ch for ch in guild.channels if isinstance(ch, discord.TextChannel)]
    if not text_channels:
        await interaction.response.send_message("No text channels found.", ephemeral=True)
        return

    first_channel = text_channels[0]
    overwrite = first_channel.overwrites_for(everyone_role)
    is_locked = overwrite.send_messages is False

    new_state = None if is_locked else False  # None = unlock, False = lock

    count = 0
    for channel in text_channels:
        overwrite = channel.overwrites_for(everyone_role)
        # Only update if state is going to change

        if overwrite.send_messages != new_state:
            overwrite.send_messages = new_state
            await channel.set_permissions(everyone_role, overwrite=overwrite)
            count += 1

    action = "unlocked" if is_locked else "locked"
    await interaction.response.send_message(f"{action.capitalize()} {count} text channel(s).", ephemeral=True)

# // upload //

@bot.tree.command(name="upload", description="Upload a Users.json file to replace the contents of the database. Can be used as a bulk-import.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(file="Upload a Users.json file")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def upload(interaction: Interaction, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True)

    # File extension check
    if not file.filename.lower().endswith(".json"):
        await interaction.followup.send("Please upload a valid JSON file.", ephemeral=True)
        return

    try:
        file_bytes = await file.read()
        users_data = json.loads(file_bytes)
    except Exception as e:
        await interaction.followup.send(f"Failed to parse JSON: {e}", ephemeral=True)
        return

    # Prepare commit
    content_str = json.dumps(users_data, indent=4)
    content_b64 = base64.b64encode(content_str.encode()).decode()

    async with aiohttp.ClientSession() as session:
        # Get current file SHA

        async with session.get(API_URL, headers=headers) as get_resp:
            if get_resp.status != 200:
                await interaction.followup.send(f"Failed to fetch current file info: HTTP {get_resp.status}", ephemeral=True)
                return
            data = await get_resp.json()
            sha = data.get("sha")
            if not sha:
                await interaction.followup.send("Could not retrieve file SHA for update.", ephemeral=True)
                return

        # Commit new file content
        commit_payload = {
            "message": f"Upload Users.json by {interaction.user}",
            "content": content_b64,
            "branch": BRANCH,
            "sha": sha
        }

        async with session.put(API_URL, headers=headers, json=commit_payload) as put_resp:
            if put_resp.status not in (200, 201):
                error_text = await put_resp.text()
                await interaction.followup.send(f"Failed to commit file: HTTP {put_resp.status}\n{error_text}", ephemeral=True)
                return

    await interaction.followup.send("Users.json uploaded successfully.", ephemeral=True)

# // toggleaccess //

@bot.tree.command(name="tempaccess", description="Temporarily applies the Bot Access role to a user (in minutes).", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to give temporary access", minutes="Duration in minutes")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def tempaccess(interaction: discord.Interaction, user: discord.Member, minutes: int):
    await interaction.response.defer(ephemeral=True)

    if minutes <= 0:
        await interaction.followup.send("Duration must be a positive integer.", ephemeral=True)
        return

    guild = bot.get_guild(GUILD_ID)
    role = guild.get_role(REQUIRED_ROLE_ID)
    if not role:
        await interaction.followup.send("Bot Access role not found.", ephemeral=True)
        return

    if role in user.roles:
        await interaction.followup.send(f"{user.mention} already has the Bot Access role.", ephemeral=True)
        return

    if user.id in active_temp_access:
        await interaction.followup.send(f"{user.mention} already has a temporary access timer running.", ephemeral=True)
        return

    # Apply role

    try:
        await user.add_roles(role, reason=f"Temporary Bot Access for {minutes} minutes")
        active_temp_access.add(user.id)
        await interaction.followup.send(f"Given Bot Access role to {user.mention} for {minutes} minutes.", ephemeral=True)

        # Start background timer
        bot.loop.create_task(remove_temp_access_after(user, role, minutes))

    except Exception as e:
        await interaction.followup.send(f"Failed to give Bot Access role: {e}", ephemeral=True)

async def remove_temp_access_after(user: discord.Member, role: discord.Role, minutes: int):
    try:
        await asyncio.sleep(minutes * 60)

        # Fetch fresh member because fsr roles arent always "updated"

        guild = bot.get_guild(user.guild.id)
        fresh_member = guild.get_member(user.id)
        if fresh_member and role in fresh_member.roles:
            await fresh_member.remove_roles(role, reason="Temporary Bot Access expired")
        active_temp_access.discard(user.id)
        # try:
            # await fresh_member.send(f"Your temporary Bot Access role has been removed after {minutes} minutes.")
        # except:
            # pass  # Fail silently if DMs disabled
    except Exception as e:
        active_temp_access.discard(user.id)
        print(f"Error removing temporary Bot Access role: {e}")

# // dbsearch //

class DbSearchView(discord.ui.View):
    def __init__(self, bot, matches, current_index=0):
        super().__init__(timeout=300)
        self.bot = bot
        self.matches = matches
        self.current_index = current_index
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = self.current_index == 0
        self.next_button.disabled = self.current_index >= len(self.matches) - 1

    @discord.ui.button(label="⏮️ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_index = max(0, self.current_index - 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_index = min(len(self.matches) - 1, self.current_index + 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    def create_embed(self):
        user = self.matches[self.current_index]
        embed = discord.Embed(title=f"Search Result {self.current_index + 1}/{len(self.matches)}", color=discord.Color.green())
        embed.add_field(name="Identifier", value=user.get("Identifier", "N/A"), inline=True)
        embed.add_field(name="Rank", value=user.get("Rank", "N/A"), inline=True)

        discord_id = user.get("DiscordId", "N/A")
        mention = f"<@{discord_id}>" if isinstance(discord_id, str) and discord_id.isdigit() else "N/A"
        embed.add_field(name="Discord ID", value=f"{discord_id} ({mention})", inline=True)
        embed.add_field(name="HWID", value=f"||`{user.get('HWID', '')}`||", inline=False)
        embed.add_field(name="Key", value=f"||`{user.get('Key', '')}`||", inline=False)

        notes = user.get("Notes", "false")
        if notes != "false" and notes.strip() != "":
            embed.add_field(name="Notes", value=notes, inline=False)
        return embed

@bot.tree.command(name="dbsearch", description="Searches the entire database for a value.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(query="Value to search for in all user fields")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def dbsearch(interaction: discord.Interaction, query: str):
    await interaction.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        async with session.get(API_URL, headers=headers) as resp:
            if resp.status != 200:
                return await interaction.followup.send(f"Failed to fetch data: HTTP {resp.status}", ephemeral=True)
            data = await resp.json()
            content_b64 = data["content"]
            users = json.loads(base64.b64decode(content_b64).decode("utf-8"))

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
        return await interaction.followup.send("No matching entries found.", ephemeral=True)

    view = DbSearchView(bot, matches)
    embed = view.create_embed()
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

# // tempwhitelist //

@bot.tree.command(name="tempwhitelist", description="Temporarily whitelists a user for x minutes.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to whitelist temporarily", minutes="Duration in minutes", hwid="Hashed HWID in SHA-256")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def tempwhitelist(interaction: discord.Interaction, user: discord.User, minutes: int, hwid: str):
    await interaction.response.defer(ephemeral=True)
    discord_id = str(user.id)

    if discord_id in active_temp_whitelists:
        return await interaction.followup.send(
            f"{user.mention} is already temporarily whitelisted until "
            f"{active_temp_whitelists[discord_id].strftime('%Y-%m-%d %H:%M:%S UTC')}.",
            ephemeral=True
        )

    async with aiohttp.ClientSession() as session:
        async with session.get(API_URL, headers=headers) as get_resp:
            if get_resp.status != 200:
                return await interaction.followup.send(f"Failed to fetch whitelist data: HTTP {get_resp.status}", ephemeral=True)
            data = await get_resp.json()
            content_b64 = data["content"]
            sha = data["sha"]
            whitelist = json.loads(base64.b64decode(content_b64).decode())

        # Already whitelisted check via discord id
        if any(str(u.get("DiscordId", "")) == discord_id for u in whitelist):
            return await interaction.followup.send(f"{user.mention} is already in the whitelist.", ephemeral=True)

        join_date = datetime.now(timezone.utc).date().isoformat()
        new_entry = {
            "Identifier": user.name,
            "Rank": "Temp",
            "DiscordId": discord_id,
            "JoinDate": join_date,
            "HWID": hwid,
            "Key": generate_key(),
            "Notes": f"Temp whitelist until {minutes} minutes from addition."
        }
        whitelist.append(new_entry)

        updated_content = json.dumps(whitelist, indent=4)
        updated_b64 = base64.b64encode(updated_content.encode()).decode()
        commit_payload = {
            "message": f"Temp whitelist added: {user.name} ({discord_id}) for {minutes} minutes",
            "content": updated_b64,
            "branch": BRANCH,
            "sha": sha
        }
        async with session.put(API_URL, headers=headers, json=commit_payload) as put_resp:
            if put_resp.status != 200:
                err = await put_resp.text()
                return await interaction.followup.send(f"Failed to commit whitelist addition: HTTP {put_resp.status}\n{err}", ephemeral=True)

    expiration_time = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    active_temp_whitelists[discord_id] = expiration_time

    await interaction.followup.send(f"Temporarily whitelisted {user.mention} for {minutes} minutes.", ephemeral=True)

    async def notify_and_remove():
        try:
            notify_time = expiration_time - timedelta(minutes=5)
            now = datetime.now(timezone.utc)
            if notify_time > now:
                await asyncio.sleep((notify_time - now).total_seconds())
                try:
                    await user.send("Your temporary whitelist will expire in 5 minutes.")
                except Exception:
                    pass

            now = datetime.now(timezone.utc)
            if expiration_time > now:
                await asyncio.sleep((expiration_time - now).total_seconds())

            async with aiohttp.ClientSession() as session:
                async with session.get(API_URL, headers=headers) as get_resp:
                    if get_resp.status != 200:
                        return
                    data = await get_resp.json()
                    content_b64 = data["content"]
                    sha = data["sha"]
                    whitelist = json.loads(base64.b64decode(content_b64).decode())

                whitelist = [u for u in whitelist if str(u.get("DiscordId")) != discord_id]

                updated_content = json.dumps(whitelist, indent=4)
                updated_b64 = base64.b64encode(updated_content.encode()).decode()
                commit_payload = {
                    "message": f"Temp whitelist expired: {user.name} ({discord_id})",
                    "content": updated_b64,
                    "branch": BRANCH,
                    "sha": sha
                }
                async with session.put(API_URL, headers=headers, json=commit_payload) as put_resp:
                    if put_resp.status != 200:
                        return

            active_temp_whitelists.pop(discord_id, None)

            try:
                await user.send("Your temporary whitelist has expired and access has now been removed.")
            except Exception:
                pass
        except asyncio.CancelledError:
            pass

    asyncio.create_task(notify_and_remove())


# --- Events ---

@bot.event
async def on_raw_reaction_add(payload):
    if payload.message_id != reaction_roles_message_id:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return

    member = guild.get_member(payload.user_id)
    if not member or member.bot:
        return

    emoji = str(payload.emoji)
    message = await bot.get_channel(payload.channel_id).fetch_message(payload.message_id)

    embed = message.embeds[0] if message.embeds else None
    if not embed or not embed.description:
        return

    for line in embed.description.split("\n"):
        if emoji in line:
            match = re.search(r"<@&(\d+)>", line)
            if match:
                role_id = int(match.group(1))
                role = guild.get_role(role_id)
                if role:
                    await member.add_roles(role, reason="Reaction role assigned")
                    # Fancy embed DM
                    role_display = role.name

                    dm_embed = discord.Embed(
                        title="Role Added!",
                        description=f"You have been **granted** the role **{role_display}** in **{guild.name}**.",
                        color=discord.Color.green(),
                        timestamp=datetime.now()
                    )
                    dm_embed.set_thumbnail(url=role.icon.url if role.icon else guild.icon.url if guild.icon else None)
                    dm_embed.set_footer(text="Reaction Role System")
                    try:
                        await member.send(embed=dm_embed)
                    except discord.Forbidden:
                        pass
            break


# --- Error Handler ---

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Catch transformer errors caused by bad member conversion

    if isinstance(error, app_errors.TransformerError):
        # Check if the error is related to the member converter failing

        if "to Member" in str(error):
            await safe_respond(interaction, "That user is not in this server.", ephemeral=True)
            return

    if isinstance(error, app_commands.CheckFailure):
        await safe_respond(interaction, str(error), ephemeral=True)
    else:
        print(f"Unhandled error: {error}")

# --- Run Bot ---

bot.run(TOKEN)
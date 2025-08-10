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
import csv
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import sql

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
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="database"))
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
    
def get_db(dict_cursor=False):
    try:
        cursor_factory = psycopg2.extras.RealDictCursor if dict_cursor else None
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST"),        # db.xxxxxx.supabase.co
            dbname=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            port=5432,
            sslmode="require",
            cursor_factory=cursor_factory
        )
        return conn
    except Exception as e:
        print(f"Database connection failed: {e}")
        raise

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

    discord_id = interaction.user.id

    try:
        conn = get_db(dict_cursor=True)
        cur = conn.cursor()

        # Postgres uses %s placeholders, not ?
        cur.execute(
            'SELECT "Identifier", "Rank", "JoinDate", "HWID", "Key", "Notes" '
            'FROM "Users" WHERE "DiscordId" = %s;',
            (discord_id,)
        )
        row = cur.fetchone()
        conn.close()

    except Exception as e:
        await interaction.followup.send(f"Database error: `{e}`", ephemeral=True)
        return

    if not row:
        await interaction.followup.send("You were not found in the user database.", ephemeral=True)
        return

    # Extract values with .get() for safety
    identifier = row.get("Identifier")
    rank = row.get("Rank")
    join_date_raw = row.get("JoinDate")
    hwid = row.get("HWID")
    key = row.get("Key")
    notes = row.get("Notes")

    embed = discord.Embed(
        title=f"User Info: {interaction.user}",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

    # Convert join date to Discord timestamp if possible
    join_date_value = "N/A"
    if join_date_raw:
        try:
            if isinstance(join_date_raw, datetime):
                join_timestamp = int(join_date_raw.timestamp())
            else:
                join_timestamp = int(datetime.strptime(join_date_raw, "%Y-%m-%d").timestamp())
            join_date_value = f"<t:{join_timestamp}:D>"
        except Exception:
            join_date_value = str(join_date_raw)

    embed.add_field(name="Identifier", value=identifier or "N/A", inline=True)
    embed.add_field(name="Rank", value=rank or "N/A", inline=True)
    embed.add_field(name="Join Date", value=join_date_value, inline=True)
    embed.add_field(name="HWID", value=f"||{hwid or 'N/A'}||", inline=True)
    embed.add_field(name="Key", value=f"||{key or 'N/A'}||", inline=True)

    if notes and notes.lower() != "false":
        embed.add_field(name="Notes", value=notes, inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)

# // whitelist //

@bot.tree.command(name="whitelist", description="Adds a user to whitelist.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(identifier="User Identifier", rank="User Rank", hwid="User HWID", discord_id="Discord ID of the user", notes="Optional notes about the user")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def whitelist(interaction: discord.Interaction, identifier: str, rank: str, hwid: str, discord_id: str, notes: str = None):
    await interaction.response.defer(ephemeral=True)

    join_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = generate_key()

    try:
        conn = get_db(dict_cursor=True)
        cur = conn.cursor()

        # Check for duplicate DiscordId or Identifier

        cur.execute(
            'SELECT 1 FROM "Users" WHERE "DiscordId" = %s OR "Identifier" = %s;',
            (discord_id, identifier)
        )
        if cur.fetchone():
            await interaction.followup.send("User with that Discord ID or Identifier already exists in whitelist.", ephemeral=True)
            conn.close()
            return

        # Insert new entry

        cur.execute(
            'INSERT INTO "Users" ("Identifier", "Rank", "JoinDate", "HWID", "DiscordId", "Key", "Notes") '
            'VALUES (%s, %s, %s, %s, %s, %s, %s);',
            (identifier, rank, join_date, hwid, discord_id, key, notes or "")
        )
        conn.commit()
        conn.close()

        await interaction.followup.send(f"✅ **{identifier}** whitelisted as **{rank}** with key ||{key}||.", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"Database error: `{e}`", ephemeral=True)

# // unwhitelist //

@bot.tree.command(name="unwhitelist", description="Removes a user from the database.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to remove")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def unwhitelist(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)

    try:
        conn = get_db(dict_cursor=True)
        cur = conn.cursor()

        cur.execute('DELETE FROM "Users" WHERE "DiscordId" = %s;', (str(user.id),))
        conn.commit()
        conn.close()

        await interaction.followup.send(f"Removed whitelist entry for {user.mention}.", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"Database error: `{e}`", ephemeral=True)

# // editwhitelist //

class EditWhitelistModal(Modal):
    def __init__(self, initial_json: str):
        super().__init__(title="Edit Whitelist JSON")

        self.json_input = TextInput(
            label="Whitelist JSON",
            style=discord.TextStyle.paragraph,
            default=initial_json,
            max_length=1900
        )
        self.add_item(self.json_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_content = self.json_input.value.strip()

        try:
            parsed = json.loads(new_content)
        except json.JSONDecodeError as e:
            await interaction.response.send_message(f"Invalid JSON: {e}", ephemeral=True)
            return

        try:
            conn = get_db(dict_cursor=True)
            cur = conn.cursor()

            # Clear old data
            cur.execute('DELETE FROM "Users";')

            # Insert new data

            for entry in parsed:
                cur.execute(
                    '''
                    INSERT INTO "Users" ("HWID", "Identifier", "Rank", "JoinDate", "DiscordId", "Key", "Notes")
                    VALUES (%s, %s, %s, %s, %s, %s, %s);
                    ''',
                    (
                        entry.get("HWID"),
                        entry.get("Identifier"),
                        entry.get("Rank"),
                        entry.get("JoinDate"),
                        int(entry["DiscordId"]) if entry.get("DiscordId") else None,
                        entry.get("Key"),
                        entry.get("Notes")
                    )
                )

            conn.commit()
            conn.close()

            await interaction.response.send_message("✅ Whitelist updated successfully.", ephemeral=True)

        except Exception as e:
            await interaction.response.send_message(f"Database error: {e}", ephemeral=True)

@bot.tree.command(name="editwhitelist", description="Edits the database JSON directly.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def editwhitelist(interaction: discord.Interaction):
    try:
        conn = get_db(dict_cursor=True)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute('SELECT "HWID", "Identifier", "Rank", "JoinDate", "DiscordId", "Key", "Notes" FROM "Users";')
        rows = cur.fetchall()
        conn.close()

        # Convert to list of dicts as RealDictCursor already gives dicts
        
        users_list = []
        for r in rows:
            users_list.append({
                "HWID": r["HWID"],
                "Identifier": r["Identifier"],
                "Rank": r["Rank"],
                "JoinDate": r["JoinDate"],
                "DiscordId": str(r["DiscordId"]) if r["DiscordId"] is not None else None,
                "Key": r["Key"],
                "Notes": r["Notes"]
            })

        json_str = json.dumps(users_list, indent=4)

    except Exception as e:
        await interaction.response.send_message(f"Error fetching database: {e}", ephemeral=True)
        return

    modal = EditWhitelistModal(json_str)
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
async def edituser(interaction: discord.Interaction, user: discord.User, field: app_commands.Choice[str], value: str):
    await interaction.response.defer(ephemeral=True)

    field_name = field.value

    # Checks

    if field_name == "HWID" and not is_valid_hwid(value):
        await interaction.followup.send("Invalid HWID format. Must be 64 hex characters and in SHA-256.", ephemeral=True)
        return
    if field_name == "JoinDate" and not is_valid_date(value):
        await interaction.followup.send("Invalid JoinDate format. Use yyyy-mm-dd.", ephemeral=True)
        return
    if field_name == "DiscordId" and not is_valid_discord_id(value):
        await interaction.followup.send("Invalid Discord ID format.", ephemeral=True)
        return

    try:
        conn = get_db(dict_cursor=True)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # User existence check

        cur.execute('SELECT * FROM "Users" WHERE "DiscordId" = %s;', (user.id,))
        row = cur.fetchone()
        if not row:
            await interaction.followup.send(
                f"User {user.mention} not found in whitelist.",
                ephemeral=True
            )
            conn.close()
            return

        # DiscordId > integer if said field is updating
        if field_name == "DiscordId":
            value = int(value)

        cur.execute(
            f'UPDATE "Users" SET "{field_name}" = %s WHERE "DiscordId" = %s;',
            (value, user.id)
        )
        conn.commit()
        conn.close()

    except Exception as e:
        await interaction.followup.send(f"Database error: {e}", ephemeral=True)
        return

    await interaction.followup.send(f"✅ Updated `{field_name}` for {user.mention} to:\n```{value}```", ephemeral=True)

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
@app_commands.describe(format="Choose the export format")
@app_commands.choices(format=[
    app_commands.Choice(name="CSV", value="csv"),
    app_commands.Choice(name="JSON", value="json")
])
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def export(interaction: discord.Interaction, format: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)

    try:
        conn = get_db(dict_cursor=True)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT * FROM "Users";')
        rows = cur.fetchall()
        conn.close()

        if not rows:
            await interaction.followup.send("No data found in the database.", ephemeral=True)
            return

        # Get column names from first row's keys
        columns = list(rows[0].keys())

    except Exception as e:
        await interaction.followup.send(f"Failed to read database: {e}", ephemeral=True)
        return

    if format.value == "csv":
        try:
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
            output.seek(0)

            file = discord.File(io.BytesIO(output.getvalue().encode()), filename="users.csv")
            await interaction.followup.send("Here is the exported CSV file:", file=file, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed to export CSV: {e}", ephemeral=True)

    elif format.value == "json":
        try:
            json_str = json.dumps(rows, indent=4, default=str)
            file = discord.File(io.BytesIO(json_str.encode()), filename="users.json")
            await interaction.followup.send("Here is the exported JSON file:", file=file, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed to export JSON: {e}", ephemeral=True)

# // validatekey //

@bot.tree.command(name="validatekey", description="Validates and returns the full information for a key including ownership.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(key="Key to validate")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def validatekey(interaction: discord.Interaction, key: str):
    await interaction.response.defer(ephemeral=True)

    try:
        conn = get_db(dict_cursor=True)
        cur = conn.cursor()
        cur.execute('SELECT * FROM "Users" WHERE "Key" = %s', (key,))
        entry = cur.fetchone()
        conn.close()
    except Exception as e:
        return await interaction.followup.send(f"Error retrieving data: {e}", ephemeral=True)

    if not entry:
        return await interaction.followup.send("Invalid key. No match found.", ephemeral=True)

    join_date = entry.get("JoinDate", "Unknown") if isinstance(entry, dict) else entry[3]

    try:
        timestamp = int(datetime.strptime(join_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        join_date_formatted = f"<t:{timestamp}:D>"
    except Exception:
        join_date_formatted = join_date

    # Extract fields safely whether dict or tuple:
    def get_field(field_name, idx):
        if isinstance(entry, dict):
            return entry.get(field_name, "N/A")
        return entry[idx] if idx < len(entry) else "N/A"

    embed = discord.Embed(title="Valid Key", description=f"**The info for key:** ||`{key}`||", color=discord.Color.green())

    embed.add_field(name="Identifier", value=get_field("Identifier", 1), inline=True)
    embed.add_field(name="Rank", value=get_field("Rank", 2), inline=True)
    embed.add_field(name="Join Date", value=join_date_formatted, inline=True)

    discord_id = get_field("DiscordId", 5)
    embed.add_field(name="Discord ID", value=f"<@{discord_id}>" if discord_id and discord_id != "N/A" else "N/A", inline=True)

    embed.add_field(name="Key", value=f"||`{get_field('Key', 4)}`||", inline=False)
    embed.add_field(name="HWID", value=f"||`{get_field('HWID', 0)}`||", inline=False)

    notes = get_field("Notes", 6)
    if notes and notes != "false":
        embed.add_field(name="Notes", value=notes, inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)

# // fetchuser //

@bot.tree.command(name="fetchuser", description="Fetches all stored info about a user", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="The user to look up")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def fetchuser(interaction: discord.Interaction, user: discord.User):
    conn = get_db(dict_cursor=True)
    cur = conn.cursor()
    cur.execute('SELECT "HWID", "Identifier", "Rank", "JoinDate", "DiscordId", "Key", "Notes" FROM "Users" WHERE "DiscordId" = %s', (str(user.id),))
    row = cur.fetchone()
    conn.close()

    if not row:
        return await interaction.response.send_message(f"No data found for {user.mention}.", ephemeral=True)

    embed = discord.Embed(title=f"User Info: {user.name}", color=discord.Color.teal(), timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=user.display_avatar.url)

    fields = [
        ("Identifier", row.get("Identifier")),
        ("Rank", row.get("Rank")),
        ("Join Date", row.get("JoinDate")),
        ("HWID", f"||{row.get('HWID')}||" if row.get("HWID") else "N/A"),
        ("Key", f"||{row.get('Key')}||" if row.get("Key") else "N/A"),
        ("Discord ID", f"{row.get('DiscordId')} (<@{row.get('DiscordId')}>)"),
        ("Notes", row.get("Notes") if row.get("Notes") and row.get("Notes").lower() != "false" else "N/A")
    ]

    member = interaction.guild.get_member(user.id)
    if member:
        fields.append(("Roles", str(len(member.roles) - 1)))  # exclude @everyone
        fields.append(("Server Join Date", f"<t:{int(member.joined_at.timestamp())}:D>"))

    for name, value in fields:
        embed.add_field(name=name, value=value or "N/A", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# // fetchdupes //

@bot.tree.command(name="fetchdupes", description="Find duplicate values in the whitelist.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(field="The field to search for duplicates in")
@app_commands.choices(field=[
    app_commands.Choice(name="HWID", value="HWID"),
    app_commands.Choice(name="Identifier", value="Identifier"),
    app_commands.Choice(name="Rank", value="Rank"),
    app_commands.Choice(name="Discord ID", value="DiscordId"),
    app_commands.Choice(name="Key", value="Key"),
    app_commands.Choice(name="All", value="All")
])
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def fetchdupes(interaction: discord.Interaction, field: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)

    field_name = field.value

    try:
        conn = get_db(dict_cursor=True)
        cur = conn.cursor()

        fields = ["HWID", "Identifier", "Rank", "DiscordId", "Key"]

        if field_name == "All":
            # Fetch all relevant columns
            cur.execute('SELECT "HWID", "Identifier", "Rank", "DiscordId", "Key" FROM "Users"')
            rows = cur.fetchall()

            dupes_map = {f: defaultdict(list) for f in fields}

            for row in rows:
                for f in fields:
                    value = row.get(f)
                    if value and str(value).lower() != "false":
                        dupes_map[f][value].append(row)

            # Filter dupes only
            dupes = {f: {k: v for k, v in val.items() if len(v) > 1} for f, val in dupes_map.items() if any(len(v) > 1 for v in val.values())}

            if not dupes:
                await interaction.followup.send(f"No duplicates found for **{field_name}**.", ephemeral=True)
                return

        else:
            # Fetch rows that dont have a null value

            cur.execute(f'SELECT * FROM "Users" WHERE "{field_name}" IS NOT NULL')
            rows = cur.fetchall()

            value_map = defaultdict(list)
            for row in rows:
                value = row.get(field_name)
                if value and str(value).lower() != "false":
                    value_map[value].append(row)

            dupes = {field_name: {k: v for k, v in value_map.items() if len(v) > 1}}

            if not dupes[field_name]:
                await interaction.followup.send(f"No duplicates found for **{field_name}**.", ephemeral=True)
                return

        conn.close()

    except Exception as e:
        await interaction.followup.send(f"Error accessing database: {e}", ephemeral=True)
        return

    embed = discord.Embed(title=f"🔁 Duplicate Entries: `{field_name}`", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))

    if field_name == "All":
        for f, field_dupes in dupes.items():
            embed.add_field(name=f"**Duplicates in {f}:**", value="\u200b", inline=False)

            for value, entries in field_dupes.items():
                # Build list of identifiers for the dupes
                identifiers = ", ".join(e.get("Identifier", "N/A") for e in entries)
                val_display = f"`{value}`" if len(str(value)) <= 50 else f"`{str(value)[:47]}...`"

                embed.add_field(name=val_display, value=f"Count: `{len(entries)}` — {identifiers}", inline=False)
            embed.add_field(name="\n", value="\n\n", inline=False)

    else:
        for value, entries in dupes[field_name].items():
            identifiers = ", ".join(e.get("Identifier", "N/A") for e in entries)
            val_display = f"`{value}`" if len(str(value)) <= 50 else f"`{str(value)[:47]}...`"

            embed.add_field(name=val_display, value=f"Count: `{len(entries)}` — {identifiers}", inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)

# // viewwhitelist //

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
        self.user_data["Identifier"] = self.identifier.value
        self.user_data["Rank"] = self.rank.value
        self.user_data["HWID"] = self.hwid.value or "N/A"
        self.user_data["Key"] = self.key.value or "N/A"
        self.user_data["Notes"] = self.notes.value or "false"

        try:
            conn = get_db(dict_cursor=True)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE "Users" SET
                    "Identifier" = %s,
                    "Rank" = %s,
                    "HWID" = %s,
                    "Key" = %s,
                    "Notes" = %s
                WHERE "DiscordId" = %s
            """, (
                self.user_data["Identifier"],
                self.user_data["Rank"],
                self.user_data["HWID"],
                self.user_data["Key"],
                self.user_data["Notes"],
                int(self.user_data.get("DiscordId") or 0),
            ))

            conn.commit()
            cursor.close()
            conn.close()

            self.whitelist_view.users[self.whitelist_view.current_index] = self.user_data
            self.whitelist_view.update_buttons()
            embed = await self.whitelist_view.create_embed()

            await interaction.response.edit_message(content=f"User **{self.user_data.get('Identifier')}** updated.", embed=embed, view=self.whitelist_view)
        except Exception as e:
            await interaction.response.send_message(f"Error updating user in database: {e}", ephemeral=True)


class WhitelistView(ui.View):
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

    @ui.button(label="⏮️ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: Button):
        self.current_index = max(0, self.current_index - 1)
        self.update_buttons()
        embed = await self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: Button):
        self.current_index = min(len(self.users) - 1, self.current_index + 1)
        self.update_buttons()
        embed = await self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="✏️ Edit User", style=discord.ButtonStyle.primary)
    async def edit_button(self, interaction: discord.Interaction, button: Button):
        user_data = self.users[self.current_index]
        modal = EditUserModal(user_data, self)
        await interaction.response.send_modal(modal)

    @ui.button(label="🗑️ Delete User", style=discord.ButtonStyle.danger)
    async def delete_button(self, interaction: discord.Interaction, button: Button):
        user_to_delete = self.users[self.current_index]
        identifier = user_to_delete.get("Identifier", "N/A")
        discord_id = user_to_delete.get("DiscordId")

        try:
            conn = get_db(dict_cursor=True)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM "Users" WHERE "DiscordId" = %s', (int(discord_id),))
            conn.commit()
            cursor.close()
            conn.close()

            # Remove from local list
            self.users.pop(self.current_index)
            if self.current_index >= len(self.users):
                self.current_index = max(0, len(self.users) - 1)

            self.update_buttons()

            if self.users:
                embed = await self.create_embed()
                await interaction.response.edit_message(content=f"Deleted user **{identifier}**.", embed=embed, view=self)
            else:
                for child in self.children:
                    child.disabled = True
                await interaction.response.edit_message(content=f"Deleted user **{identifier}**. The database is now empty.", embed=None, view=self)
        except Exception as e:
            await interaction.response.send_message(f"Error deleting user: {e}", ephemeral=True)

    async def create_embed(self):
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

        embed.add_field(name="Join Date", value=join_date, inline=True)
        embed.add_field(name="HWID", value=f"||`{user_data.get('HWID', '')}`||", inline=False)
        embed.add_field(name="Key", value=f"||`{user_data.get('Key', '')}`||", inline=False)

        notes = user_data.get("Notes")
        if notes is not None and notes != "false" and notes.strip() != "":
            embed.add_field(name="Notes", value=notes, inline=False)

        discord_id = int(user_data.get("DiscordId", 0))
        try:
            member = await self.bot.fetch_user(discord_id)
            embed.set_thumbnail(url=member.display_avatar.url)
        except Exception:
            embed.set_thumbnail(url="https://cdn.discordapp.com/embed/avatars/0.png")

        return embed

@bot.tree.command(name="viewwhitelist", description="View all whitelist entries.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def viewwhitelist(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        conn = get_db(dict_cursor=True)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM "Users"')
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        if not rows:
            return await interaction.followup.send("No database entries found.", ephemeral=True)

        users = [row for row in rows]

    except Exception as e:
        return await interaction.followup.send(f"Error accessing database: {e}", ephemeral=True)

    view = WhitelistView(bot, users)
    embed = await view.create_embed()
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

# // register

@bot.tree.command(name="register", description="Submit your info to be reviewed and whitelisted.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(identifier="Your identifier (username, alias, etc.)")
@is_in_guild(GUILD_ID)
async def register(interaction: discord.Interaction, identifier: str):
    await interaction.response.defer(ephemeral=True)
    discord_id = interaction.user.id

    try:
        conn = get_db(dict_cursor=True)
        cur = conn.cursor()

        # Check if already registered in Registrations database

        cur.execute('SELECT * FROM "Registrations" WHERE "DiscordId" = %s', (discord_id,))
        existing_reg = cur.fetchone()
        if existing_reg:
            await interaction.followup.send("You have already registered.", ephemeral=True)
            cur.close()
            conn.close()
            return

        # Check if already whitelisted in Users database

        cur.execute('SELECT * FROM "Users" WHERE "DiscordId" = %s', (discord_id,))
        already_whitelisted = cur.fetchone()
        if already_whitelisted:
            await interaction.followup.send("You are already whitelisted.", ephemeral=True)
            cur.close()
            conn.close()
            return

        # Prepare registration data
        rank = "User"
        join_date = datetime.now(timezone.utc).date().isoformat()
        hwid_raw = get_hwid() or "UNKNOWN_HWID"
        hwid_hash = hashlib.sha256(hwid_raw.encode()).hexdigest()

        # Insert registration details into database
        cur.execute(
            'INSERT INTO "Registrations" ("HWID", "Identifier", "Rank", "JoinDate", "DiscordId") VALUES (%s, %s, %s, %s, %s)',
            (hwid_hash, identifier, rank, join_date, discord_id)
        )
        conn.commit()
        cur.close()
        conn.close()

        # Send embed

        reg_channel = bot.get_channel(REGISTRATION_CHANNEL_ID)
        if reg_channel:
            embed = discord.Embed(title="A registration has been logged.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
            embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)

            await reg_channel.send(embed=embed)

        await interaction.followup.send(
            f"Registration completed:\n"
            f"Identifier: {identifier}\n"
            f"Rank: {rank}\n"
            f"Discord ID: {discord_id}\n"
            f"Join Date: {join_date}\n"
            f"HWID: ||`{hwid_hash}`||",
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send(f"Failed to register: {e}", ephemeral=True)

# // checkregistration

@bot.tree.command(name="checkregistration", description="Checks if a user is registered in the Registration database.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="The user to check registration for")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def checkregistration(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    discord_id = user.id

    try:
        conn = get_db(dict_cursor=True)
        cur = conn.cursor()

        # Check whitelist db
        cur.execute('SELECT 1 FROM "Users" WHERE "DiscordId" = %s', (discord_id,))
        whitelist_registered = cur.fetchone() is not None

        # Check registrations db
        cur.execute('SELECT * FROM "Registrations" WHERE "DiscordId" = %s', (discord_id,))
        registration_row = cur.fetchone()

        cur.close()
        conn.close()

        if whitelist_registered and registration_row:
            status_msg = f"User **{user}** is **registered** in both the whitelist and registration database."
        elif whitelist_registered:
            status_msg = f"User **{user}** is **registered** in the whitelist only."
        elif registration_row:
            status_msg = f"User **{user}** is **registered** in the registration database only."
        else:
            status_msg = f"User **{user}** is **not** registered."

        await interaction.followup.send(status_msg, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"Error checking registration: {e}", ephemeral=True)

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


@bot.tree.command(name="clearregistrations", description="Clears all registrations from the database.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def clearregistrations(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    # Confirmation

    view = ConfirmClearView(interaction.user.id)
    await interaction.followup.send("Are you sure you want to clear all registrations? This action cannot be undone.", view=view, ephemeral=True)

    await view.wait()
    if not view.confirmed:
        return

    try:
        conn = get_db(dict_cursor=True)
        cur = conn.cursor()
        cur.execute('DELETE FROM "Registrations"')
        conn.commit()
        cur.close()
        conn.close()

        await interaction.followup.send("All registrations have been cleared.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed to clear registrations: {e}", ephemeral=True)

# // delregistration

@bot.tree.command(name="delregistration", description="Deletes a specific registration from the database.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="The user whose registration to delete")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def delregistration(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    discord_id = user.id

    try:
        conn = get_db(dict_cursor=True)
        cur = conn.cursor()

        # Check if registration exists

        cur.execute('SELECT * FROM "Registrations" WHERE "DiscordId" = %s', (discord_id,))
        reg = cur.fetchone()

        if not reg:
            await interaction.followup.send(f"No registration found for {user.mention}.", ephemeral=True)
            cur.close()
            conn.close()
            return

        # Delete registration

        cur.execute('DELETE FROM "Registrations" WHERE "DiscordId" = %s', (discord_id,))
        conn.commit()
        cur.close()
        conn.close()

        await interaction.followup.send(f"Registration for {user.mention} has been deleted.", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"Failed to delete registration: {e}", ephemeral=True)

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
            # If panel deleted, recreate it and save the new id

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

@bot.tree.command(name="upload", description="Upload a Users.json or Users.csv file to replace the database. Can be used as a bulk-import.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def upload(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True)

    filename = file.filename.lower()
    if not (filename.endswith(".json") or filename.endswith(".csv")):
        return await interaction.followup.send("Please upload a `.json` or `.csv` file.", ephemeral=True)

    data = await file.read()

    try:
        if filename.endswith(".json"):
            users = json.loads(data.decode())
            # Check if users database is a list of dicts
            
            if not isinstance(users, list):
                raise ValueError("JSON file does not contain a list of users.")

        elif filename.endswith(".csv"):
            decoded = data.decode()
            reader = csv.DictReader(io.StringIO(decoded))
            users = list(reader)
    except Exception as e:
        return await interaction.followup.send(f"Failed to parse file: {e}", ephemeral=True)

    try:
        conn = get_db(dict_cursor=True)
        cur = conn.cursor()

        # Clear current database
        cur.execute(sql.SQL('DELETE FROM "Users"'))

        # Insert users from uploaded file
        insert_query = sql.SQL("""
            INSERT INTO "Users" ("Identifier", "Rank", "JoinDate", "HWID", "Key", "DiscordId", "Notes")
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """)

        for user in users:
            cur.execute(
                insert_query,
                (
                    user.get("Identifier"),
                    user.get("Rank"),
                    user.get("JoinDate"),
                    user.get("HWID"),
                    user.get("Key"),
                    user.get("DiscordId"),
                    user.get("Notes"),
                )
            )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        return await interaction.followup.send(f"Database error: {e}", ephemeral=True)

    await interaction.followup.send("Database replaced with uploaded file.", ephemeral=True)

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

@bot.tree.command(name="dbsearch", description="Searches the entire database for a value.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(query="Search term")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def dbsearch(interaction: discord.Interaction, query: str):
    await interaction.response.defer(ephemeral=True)

    conn = get_db(dict_cursor=True)
    cur = conn.cursor()

    # Fetch all users
    cur.execute('SELECT * FROM "Users"')
    rows = cur.fetchall()
    conn.close()

    # Rows are RealDictCursor dicts, so look through all

    matches = []
    for row in rows:
        if any(query.lower() in (str(value).lower() if value else "") for value in row.values()):
            matches.append(row)

    if not matches:
        return await interaction.followup.send("No matches found.", ephemeral=True)

    # Build list of matches
    msg = "\n".join(f'ID: {m["DiscordId"]} (<@{m["DiscordId"]}>) | Identifier: {m.get("Identifier", "N/A")}' for m in matches)
    await interaction.followup.send(f"**Matches:**\n{msg}", ephemeral=True)

# // tempwhitelist //

@bot.tree.command(name="tempwhitelist", description="Temporarily whitelists a user for x minutes.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to whitelist temporarily", minutes="Duration in minutes", hwid="Hashed HWID in SHA-256")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def tempwhitelist(interaction: discord.Interaction, user: discord.User, minutes: int, hwid: str):
    await interaction.response.defer(ephemeral=True)
    discord_id = str(user.id)

    # Check if already temporarily whitelisted
    if discord_id in active_temp_whitelists:
        expires = active_temp_whitelists[discord_id]
        expires_str = f"<t:{int(expires.timestamp())}:F>"
        return await interaction.followup.send(
            f"{user.mention} is already temporarily whitelisted until {expires_str}.",
            ephemeral=True
        )

    join_date = datetime.now(timezone.utc).date().isoformat()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)

    # Insert into the database
    try:
        conn = get_db(dict_cursor=True)
        cur = conn.cursor()

        # Check if user already in DB whitelist (full check is recommended in your real logic)
        cur.execute('SELECT * FROM "Users" WHERE "DiscordId" = %s', (discord_id,))
        existing = cur.fetchone()
        if existing:
            await interaction.followup.send(f"{user.mention} is already whitelisted in the database.", ephemeral=True)
            cur.close()
            conn.close()
            return

        # Insert new temp whitelist user with expiration timestamp
        cur.execute("""
            INSERT INTO "Users" ("Identifier", "Rank", "DiscordId", "JoinDate", "HWID", "Key", "Notes", "TempExpires")
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            user.name,
            "Temp",
            discord_id,
            join_date,
            hwid,
            generate_key(),
            f"Temp whitelist until {minutes} minutes from addition.",
            expires_at  # datetime object stored as timestamptz
        ))
        conn.commit()
        cur.close()
        conn.close()

    except Exception as e:
        return await interaction.followup.send(f"Database error: {e}", ephemeral=True)

    active_temp_whitelists[discord_id] = expires_at

    expires_str = f"<t:{int(expires_at.timestamp())}:F>"
    await interaction.followup.send(f"Temporarily whitelisted {user.mention} for {minutes} minutes (until {expires_str}).", ephemeral=True)

    async def notify_and_remove():
        try:
            notify_time = expires_at - timedelta(minutes=5)
            now = datetime.now(timezone.utc)
            if notify_time > now:
                await asyncio.sleep((notify_time - now).total_seconds())
                try:
                    await user.send("Your temporary whitelist will expire in 5 minutes.")
                except Exception:
                    pass

            now = datetime.now(timezone.utc)
            if expires_at > now:
                await asyncio.sleep((expires_at - now).total_seconds())

            # Remove from DB
            try:
                conn = get_db(dict_cursor=True)
                cur = conn.cursor()
                cur.execute('DELETE FROM "Users" WHERE "DiscordId" = %s AND "Rank" = %s', (discord_id, "Temp"))
                conn.commit()
                cur.close()
                conn.close()
            except Exception:
                pass

            active_temp_whitelists.pop(discord_id, None)

            try:
                await user.send("Your temporary whitelist has expired and access has now been removed.")
            except Exception:
                pass

        except asyncio.CancelledError:
            pass

    asyncio.create_task(notify_and_remove())

# // clearchat //

@bot.tree.command(name="clearnotes", description="Clears the Notes field.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="The user to clear notes for")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def clearnotes(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    discord_id = str(user.id)

    try:
        conn = get_db(dict_cursor=True)
        cur = conn.cursor()
        cur.execute('UPDATE "Users" SET "Notes" = NULL WHERE "DiscordId" = %s', (discord_id,))
        conn.commit()
        updated_rows = cur.rowcount
        cur.close()
        conn.close()

        if updated_rows == 0:
            await interaction.followup.send(f"No user found with Discord ID `{discord_id}`.", ephemeral=True)
        else:
            await interaction.followup.send(f"Cleared notes for user {user.mention}.", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"Failed to clear notes: {e}", ephemeral=True)


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
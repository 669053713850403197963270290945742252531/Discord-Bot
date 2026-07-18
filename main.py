# // Imports //

# spoof flask to allow local usage
import sys, os
import traceback
from keep_alive import keep_alive
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

keep_alive()

import discord
from discord.ext import commands, tasks
from discord.app_commands import errors as app_errors
from discord import app_commands, InteractionResponded, ui, Interaction
import asyncio
from typing import Optional
from datetime import datetime, timezone, timedelta
import json
import base64
import re
import io
import csv
from discord.ui import Modal, TextInput, View, Button, LayoutView, Container, TextDisplay, ActionRow, Section, Thumbnail, File
from collections import defaultdict

import bot_api
from bot_api import (
    GUILD_ID, REQUIRED_ROLE_ID, REGISTRATION_CHANNEL_ID, REACTION_ROLE_CHANNEL_ID,
    GitHubAPIError,
    fetch_raw_users, fetch_users_with_sha, fetch_api_file, fetch_raw_text,
    fetch_api_text_and_sha, commit_content, commit_users, get_current_sha,
    list_commits, get_commit,
    find_user_by_discord_id, find_user_by_hwid, find_user_by_key, remove_user_by_discord_id, build_user_entry,
    generate_key, generate_unique_key, is_valid_hwid, is_valid_discord_id, is_valid_date,
    format_discord_timestamp, format_join_date,
    format_expiration_note, parse_expiration_note, humanize_timeleft,
    safe_respond, notify_user, notify_permission_error, has_role, is_in_guild, can_moderate,
    build_embed, success_embed, error_embed, send_success, send_error, edit_or_send_error,
)

# // Constants //

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN is not set.")

# Code-only feature toggle: set to False to stop the bot from DMing users
# when they gain/lose a reaction role. No slash command controls this;
# flip it here and restart the bot.
REACTION_ROLE_DMS_ENABLED = True

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
# Snapshot of each channel's @everyone overwrite permissions from right before
# /togglelockdown was last enabled. Non-empty while lockdown is active; used
# to restore channels to their exact prior state (instead of blanket
# unlocking) so channels that were already locked beforehand stay locked.
lockdown_snapshots = {}

# --- Commands ---

# // ping //

@bot.tree.command(name="ping", description="Returns the bot's latency.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ping(interaction: discord.Interaction):
    await send_success(interaction, f"Pong! Latency: {round(bot.latency * 1000)}ms", title="🏓 Pong")

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
        summary_fields = [
            ("User", f"{target} ({target.id})", False),
            ("Reason", reason, False),
            ("Messages", "Preserved" if preserve_messages else "Deleted", False),
        ]
        if duration:
            minute_label = "minute" if duration == 1 else "minutes"
            summary_fields.append(("Duration", f"{duration} {minute_label}", False))

        summary_embed = success_embed(title="Ban Summary", fields=summary_fields)
        await interaction.edit_original_response(content=None, embed=summary_embed)

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
        await edit_or_send_error(interaction, str(e))
    except discord.Forbidden:
        await edit_or_send_error(interaction, "Missing permissions to ban.")
    except Exception as e:
        await edit_or_send_error(interaction, str(e))

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

                embed = error_embed(
                    title="User is Banned",
                    fields=[("User", f"{user} (`{user.id}`)", False), ("Reason", reason, False)],
                )
                return await interaction.followup.send(embed=embed, ephemeral=True)

        # When user is NOT found/not banned
        await send_success(interaction, f"{user.mention} is not currently banned from this server.")

    except discord.Forbidden:
        await send_error(interaction, "I don't have permission to view bans.")
    except Exception as e:
        await send_error(interaction, f"Error while checking ban: `{e}`")

# // unban //

@bot.tree.command(name="unban", description="Unbans a user from the server.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="The user to unban")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def unban(interaction: discord.Interaction, user: discord.User):
    try:
        # Fetch bans
        bans = [ban async for ban in interaction.guild.bans()]
        banned_entry = discord.utils.find(lambda ban: ban.user.id == user.id, bans)

        if not banned_entry:
            await send_error(interaction, "User is not banned.")
            return

        await interaction.guild.unban(banned_entry.user, reason=f"Unbanned by {interaction.user}")
        await send_success(interaction, f"Successfully unbanned {user.mention}.")

    except discord.Forbidden:
        await send_error(interaction, "Missing permissions to unban.")
    except Exception as e:
        await send_error(interaction, str(e))

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
        await send_success(interaction, f"{target.mention} has been kicked.", fields=[("Reason", reason, False)])
    except app_commands.CheckFailure as e:
        await send_error(interaction, str(e))
    except discord.Forbidden:
        await send_error(interaction, "Missing permissions to kick.")
    except Exception as e:
        await send_error(interaction, f"Failed to kick: {e}")

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
                await interaction.edit_original_response(content=None, embed=error_embed("Missing permission to create the muted role."))
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
            await interaction.edit_original_response(content=None, embed=error_embed(f"{target.mention} is already muted."))
            return

        await target.add_roles(muted_role, reason=f"Muted by {interaction.user} - Reason: {reason}")
        await interaction.edit_original_response(
            content=None,
            embed=success_embed(f"{target.mention} has been muted.", fields=[("Reason", reason, False)]),
        )

        await notify_user(target, "muted", interaction.user, reason, guild.name)

    except Exception as e:
        await edit_or_send_error(interaction, f"Failed to mute: {e}")

# // unmute //

@bot.tree.command(name="unmute", description="Unmutes a member from all channels.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target="Member to unmute", reason="Reason for the unmute")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def unmute(interaction: discord.Interaction, target: discord.Member, reason: str = "No reason provided"):
    try:
        await can_moderate(interaction, target)
    except app_commands.CheckFailure as e:
        await send_error(interaction, str(e))
        return

    muted_role = discord.utils.get(interaction.guild.roles, name="Muted")
    if not muted_role:
        await send_error(interaction, "Muted role missing.")
        return

    if muted_role not in target.roles:
        await send_error(interaction, f"{target.mention} is not muted.")
        return

    try:
        await target.remove_roles(muted_role, reason=f"Unmuted by {interaction.user}")
        await send_success(interaction, f"{target.mention} has been unmuted.")
        await notify_user(target, "unmuted", interaction.user, reason, interaction.guild.name)
    except discord.Forbidden:
        await send_error(interaction, "Missing permissions to remove roles.")

# // dm //

@bot.tree.command(name="dm", description="Sends a direct message to a user.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(target="User to direct message", message="Message to send")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def dm(interaction: discord.Interaction, target: discord.User, message: str):
    try:
        await target.send(message)
        await send_success(interaction, f"Sent message to {target.mention}.")
    except discord.Forbidden as e:
        # Handle 'Cannot send messages to this user' error

        if e.code == 50007:
            await send_error(interaction, f"Failed to dm {target.mention}. They may have dms disabled, or you're not connected through a shared server or friendship.")
        else:
            await send_error(interaction, f"Failed to dm: {e}")
    except discord.HTTPException as e:
        # Handle 'Cannot send messages to this user' and blocked bot error

        if e.status == 400 and e.code == 50007:
            await send_error(interaction, f"Cannot DM {target.mention}. The user may have DMs disabled or has blocked the bot.")
        else:
            await send_error(interaction, f"Failed to send DM: {e}")
    except Exception as e:
        await send_error(interaction, f"Unexpected error: {e}")

# // myinfo //

@bot.tree.command(name="myinfo", description="Fetches your whitelist information from the database.", guild=discord.Object(id=GUILD_ID))
@is_in_guild(GUILD_ID)
async def myinfo(interaction: discord.Interaction):
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
    embed.add_field(name="Join Date", value=format_discord_timestamp(user_data.get("JoinDate")), inline=True)
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
        raw_content = await fetch_raw_text(bot_api.RAW_URL)
        real_content, _sha = await fetch_api_text_and_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    if raw_content.strip() == real_content.strip():
        embed = success_embed(
            "The raw database matches the real database exactly.",
            title="Database Integrity Verified",
        )
    else:
        embed = error_embed(
            "The raw database does **not** match the real database.\nPossible causes:\n- CDN caching\n- Unauthorized edits\n- Commit mismatch (API Limitations)",
            title="Database Integrity Mismatch",
        )

    await interaction.followup.send(embed=embed, ephemeral=True)

# // whitelist //

@bot.tree.command(name="whitelist", description="Adds a user to the database.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(identifier="Username or alias", hwid="Pre-hashed HWID in SHA-256", user="Discord user to whitelist", rank="User rank", notes="Notes to keep reminders about this user")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def whitelist(interaction: discord.Interaction, identifier: str, hwid: str, user: discord.User, rank: str, notes: str = None):
    await interaction.response.defer(ephemeral=True)

    discord_id = str(user.id)

    # Checks

    if not is_valid_hwid(hwid):
        return await send_error(interaction, "Invalid HWID format. Must be 64 hex characters (SHA-256).")

    if notes is not None and not notes.strip():
        return await send_error(interaction, "Notes must be left blank or a non-empty string.")

    try:
        users, sha = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    # Duplicate checks

    existing = find_user_by_discord_id(users, discord_id)
    if existing:
        return await send_error(interaction, f"{user.mention} is already whitelisted as **{existing.get('Identifier', 'Unknown')}**.")

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
        f"**{identifier}** has been whitelisted.",
        fields=[("HWID", f"||`{hwid}`||", False)],
    )

# // unwhitelist //

@bot.tree.command(name="unwhitelist", description="Removes a user from the database.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="Discord user to remove from the database.")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def unwhitelist(interaction: discord.Interaction, user: discord.User):
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

    await send_success(interaction, f"{user.mention} has been removed from the whitelist.")

# // editwhitelist //

# Kept as a constant (rather than a bare 1900 in the TextInput below) so the
# pre-flight check in editwhitelist() can never silently drift out of sync
# with the modal's actual max_length.
EDIT_WHITELIST_MAX_LENGTH = 1900  # Discord limit = 4000 chars for modal inputs; kept well under that with room to spare

class EditWhitelistModal(Modal):
    def __init__(self, initial_json: str):
        super().__init__(title="Edit Whitelist JSON")

        self.json_input = TextInput(
            label="Whitelist JSON",
            style=discord.TextStyle.paragraph,
            default=initial_json,
            max_length=EDIT_WHITELIST_MAX_LENGTH
        )
        self.add_item(self.json_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_content = self.json_input.value.strip()

        try:
            json.loads(new_content)
        except json.JSONDecodeError as e:
            await send_error(interaction, f"Invalid JSON: {e}")
            return

        # Fetch latest sha again to avoid race conditions, then commit
        try:
            sha = await get_current_sha()
            await commit_content(new_content, sha, f"Edit whitelist by {interaction.user}")
        except GitHubAPIError as e:
            await send_error(interaction, str(e))
            return

        await send_success(interaction, "Whitelist updated successfully.")


@bot.tree.command(name="editwhitelist", description="Edits the database JSON directly.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def editwhitelist(interaction: discord.Interaction):
    try:
        decoded, _sha = await fetch_api_text_and_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    # A modal's TextInput can't pre-fill more characters than its own
    # max_length allows -- if `decoded` (the whole Users.json) is longer
    # than that, Discord rejects send_modal() outright with an unhandled
    # "Invalid Form Body ... Must be {n} or fewer in length" 400, since the
    # `default` value itself violates the field's own limit before the user
    # ever gets to type anything. Catch that up front with a clear message
    # instead of letting the raw HTTPException surface. This also means the
    # whitelist has simply outgrown what a single modal field can hold --
    # /edituser (single field) or /export + a direct GitHub edit are the
    # ways to make changes once you're past this size.
    if len(decoded) > EDIT_WHITELIST_MAX_LENGTH:
        return await send_error(
            interaction,
            f"The whitelist JSON is {len(decoded):,} characters, which is too long to "
            f"load into this modal (Discord caps modal text fields at "
            f"{EDIT_WHITELIST_MAX_LENGTH:,} here). Use `/edituser` to change a single "
            "field, or `/export` to pull the full file and edit it directly on GitHub.",
        )

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
async def edituser(interaction: discord.Interaction, user: discord.User, field: app_commands.Choice[str], value: str):
    await interaction.response.defer(ephemeral=True)

    field_name = field.value

    # Input checks per field
    if field_name == "HWID" and not is_valid_hwid(value):
        return await send_error(interaction, "Invalid HWID format. Must be 64 hex characters and in SHA-256.")
    if field_name == "JoinDate" and not is_valid_date(value):
        return await send_error(interaction, "Invalid JoinDate format. Use mm/dd/yyyy, hh:mm:ss AM/PM (e.g. 6/19/2026, 3:24:53 AM).")
    if field_name == "DiscordId" and not is_valid_discord_id(value):
        return await send_error(interaction, "Invalid Discord ID format.")

    try:
        users, sha = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    user_entry = find_user_by_discord_id(users, user.id)
    if not user_entry:
        return await send_error(interaction, f"User {user.mention} not found in whitelist.")

    user_entry[field_name] = value

    try:
        await commit_users(users, sha, f"Edit whitelist user {user} - set {field_name} to {value}")
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    await send_success(interaction, f"Updated {field_name} for {user.mention} to:\n```{value}```")

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

def file_success_layout(description: str, filename: str) -> LayoutView:
    """Components V2 success confirmation with the attached file placed as an
    explicit component *after* the message text, so the confirmation always
    renders above the file rather than relying on Discord's default
    attachment/embed ordering. Used by /export."""
    layout = LayoutView(timeout=None)
    layout.add_item(Container(
        TextDisplay("### ✅ Success"),
        TextDisplay(description),
        accent_color=discord.Color.green(),
    ))
    layout.add_item(File(f"attachment://{filename}"))
    return layout

@bot.tree.command(name="export", description="Exports the current database.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
@app_commands.describe(format="Select export format")
@app_commands.choices(format=[
    app_commands.Choice(name="JSON", value="json"),
    app_commands.Choice(name="CSV", value="csv"),
])
async def export(interaction: discord.Interaction, format: app_commands.Choice[str]):
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

    elif format.value == "csv":
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

# // validatekey //

@bot.tree.command(name="validatekey", description="Validates and returns the full information for a key including ownership.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(key="Key to validate")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def validatekey(interaction: discord.Interaction, key: str):
    await interaction.response.defer(ephemeral=True)

    try:
        users, _sha = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    # Key search
    entry = next((user for user in users if user.get("Key") == key), None)

    if not entry:
        return await send_error(interaction, "Invalid key. No match found.")

    embed = discord.Embed(
        title="Valid Key",
        description=f"**The info for key:** ||`{key}`||",
        color=discord.Color.green()
    )

    embed.add_field(name="Identifier", value=entry.get("Identifier", "N/A"), inline=True)
    embed.add_field(name="Rank", value=entry.get("Rank", "N/A"), inline=True)
    embed.add_field(name="Join Date", value=format_discord_timestamp(entry.get("JoinDate", "Unknown")), inline=True)
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

    raw_url = f"https://raw.githubusercontent.com/{bot_api.OWNER}/{bot_api.REPO}/{sha}/{bot_api.FILE_PATH}"

    try:
        old_content = await fetch_raw_text(raw_url)
        json.loads(old_content)
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))
    except json.JSONDecodeError as e:
        return await send_error(interaction, f"Error loading commit content: {e}")

    try:
        current_sha = await get_current_sha()
        await commit_content(old_content, current_sha, f"Rollback Users.json to commit {sha}")
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    await send_success(interaction, f"Successfully rolled back the database to commit `{sha}`.")

# // commithistory //

@bot.tree.command(name="commithistory", description="View the recent commit history.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(max_entries="Maximum number of commits to display (default 5, max 20)")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def commithistory(interaction: discord.Interaction, max_entries: int = 5):
    await interaction.response.defer(ephemeral=True)

    max_entries = min(max(1, max_entries), 20) # Clamp 1-20

    try:
        commits = await list_commits(per_page=max_entries)
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    if not commits:
        return await send_error(interaction, "No commits found.")

    embed = discord.Embed(title=f"Commit History: `{bot_api.FILE_PATH}`", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))

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
            "\u200b\n" # zero width space + newline to gap | shoutout google 👍
        )

        embed.add_field(name=name, value=value, inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)

# // fetchcommits //

@bot.tree.command(name="fetchcommit", description="Fetches the details for a specific commit.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(sha="Commit SHA to fetch")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def fetchcommit(interaction: discord.Interaction, sha: str):
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
        timestamp=date_obj
    )
    embed.set_author(name=author)
    embed.add_field(name="Additions", value=f"🟢 +{additions}", inline=True)
    embed.add_field(name="Deletions", value=f"❌ -{deletions}", inline=True)
    embed.add_field(name="Date", value=f"<t:{date_ts}:F>", inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)

# // fetchuser //

@bot.tree.command(name="fetchuser", description="Fetches all stored info about a user.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="The user to look up")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def fetchuser(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)

    try:
        users, _ = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    user_data = find_user_by_discord_id(users, user.id)
    if not user_data:
        return await send_error(interaction, f"No data found for {user.mention}.")

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

    join_date_display = format_discord_timestamp(user_data.get("JoinDate", "Unknown"))

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
    app_commands.Choice(name="Rank", value="Rank"),
    app_commands.Choice(name="Discord ID", value="DiscordId"),
    app_commands.Choice(name="Key", value="Key"),
    app_commands.Choice(name="All", value="All")
])
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def fetchdupes(interaction: discord.Interaction, field: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)

    try:
        users, _ = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    if field.value == "All":
        # Check duplicates for all relevant fields, accumulate results
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
        # Single field duplicate check
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
        self.notes = TextInput(label="Notes", default=user_data.get("Notes") or "", style=discord.TextStyle.paragraph, required=False)

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
        self.user_data["Notes"] = self.notes.value or None

        try:
            existing, sha = await fetch_users_with_sha()

            discord_id = self.user_data.get("DiscordId")
            for i, u in enumerate(existing):
                if u.get("DiscordId") == discord_id:
                    existing[i] = self.user_data
                    break

            await commit_users(existing, sha, f"Edited whitelist user: {self.user_data.get('Identifier', 'N/A')} ({discord_id})")
        except GitHubAPIError as e:
            await send_error(interaction, str(e))
            return

        # Same page they were editing on - just refresh the entry's own data.
        self.whitelist_view.users = existing
        self.whitelist_view.pending_notice = f"✅ User **{self.user_data.get('Identifier')}** updated."
        await self.whitelist_view.build()
        await interaction.response.edit_message(view=self.whitelist_view)


class DeleteUserConfirmView(LayoutView):
    """Components V2 confirmation prompt shown in place of the whitelist entry
    when 'Delete User' is pressed. Confirm applies the delete and returns to
    WhitelistView on the same page (clamped if that was the last entry);
    Cancel returns to WhitelistView unchanged - either way the original
    whitelist view message is what the user ends up looking at."""

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

        view.users = existing
        # Keep the user on the same page index; only clamp if that entry no
        # longer exists (e.g. it was the last page).
        if view.current_index >= len(view.users):
            view.current_index = max(0, len(view.users) - 1)

        view.pending_notice = f"🗑️ Deleted user **{self.identifier}**."
        await view.build()
        await interaction.response.edit_message(view=view)

    async def cancel(self, interaction: discord.Interaction):
        await self.whitelist_view.build()
        await interaction.response.edit_message(view=self.whitelist_view)


class WhitelistView(LayoutView):
    """Components V2 paginated whitelist browser. The 'embed' (entry details +
    avatar) and the Previous/Next/Edit/Delete/Refresh buttons all live inside a
    single Container; Previous/Next only appear when there's more than one
    entry. Refresh is always present so stale data can be pulled without
    re-running the command."""

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
        modal = EditUserModal(user_data, self)
        await interaction.response.send_modal(modal)

    async def on_delete(self, interaction: discord.Interaction):
        confirm_view = DeleteUserConfirmView(self)
        await interaction.response.edit_message(view=confirm_view)

    async def on_refresh(self, interaction: discord.Interaction):
        try:
            users, _sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        self.users = users
        # Stay on the same page; clamp only if entries were removed out from
        # under this view (e.g. someone else deleted the last few via GitHub).
        if self.current_index >= len(self.users):
            self.current_index = max(0, len(self.users) - 1)

        self.pending_notice = "🔄 Whitelist refreshed."
        await self.build()
        await interaction.response.edit_message(view=self)


@bot.tree.command(name="viewwhitelist", description="View all whitelist entries.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def viewwhitelist(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        users, _sha = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    if not users:
        return await send_error(interaction, "No database entries found.")

    view = WhitelistView(bot, users)
    await view.build()
    await interaction.followup.send(view=view, ephemeral=True)

# // register

REGISTRATION_EMBED_TITLE = "Registration Successful"

# TODO: replace with your actual executor/HWID-script instructions
HWID_INSTRUCTIONS = (
    "You need to provide your **HWID** to register.\n\n"
    "**How to get your HWID:**\n"
    "1. Open your executor, join any game, and attach.\n"
    "2. Run the [HWID script](https://raw.githubusercontent.com/corradedied/Public-Scripts/refs/heads/main/get%20hwid.lua) and click `Copy HWID` to copy your hashed HWID.\n"
    "3. Run `/register` again with both `identifier` (what you want to be named in the script) and `hwid` filled in."
)

@bot.tree.command(name="register", description="Submit your info to be reviewed and whitelisted.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(identifier="Your identifier (username, alias, etc.)", hwid="Pre-hashed HWID in SHA-256, obtained from the executor")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def register(interaction: discord.Interaction, identifier: str, hwid: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)

    if not hwid:
        embed = discord.Embed(
            title="🔑 HWID Required",
            description=HWID_INSTRUCTIONS,
            color=discord.Color.orange(),
        )
        return await interaction.edit_original_response(embed=embed)

    discord_id_str = str(interaction.user.id)

    # Checks

    if not is_valid_hwid(hwid):
        return await send_error(interaction, "Invalid HWID format. Must be 64 hex characters (SHA-256).")

    # Check whitelist for existing discord id

    try:
        whitelist_users, _ = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    if find_user_by_discord_id(whitelist_users, discord_id_str):
        return await send_error(interaction, "You are already whitelisted.")

    # Check for existing registration

    reg_channel = bot.get_channel(REGISTRATION_CHANNEL_ID)
    if not reg_channel:
        return await send_error(interaction, "Registration channel not found.")

    messages = [msg async for msg in reg_channel.history(limit=100)]
    for msg in messages:
        if msg.embeds:
            embed = msg.embeds[0]
            for field in embed.fields:
                if discord_id_str in field.value:
                    return await send_error(interaction, "You have already registered before.")

    # Registration

    rank = "User"
    join_date = format_join_date()

    embed = success_embed(
        title=REGISTRATION_EMBED_TITLE,
        author=str(interaction.user),
        author_icon=interaction.user.display_avatar.url,
        timestamp=datetime.now(timezone.utc),
        fields=[
            ("Identifier", identifier, True),
            ("Rank", rank, True),
            ("Discord ID", discord_id_str, True),
            ("Join Date", format_discord_timestamp(join_date), True),
            ("HWID", f"||`{hwid}`||", False),
        ],
    )

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
        users, _ = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    whitelist_registered = find_user_by_discord_id(users, discord_id_str) is not None

    # Check registration embeds and find message link if found
    reg_channel = bot.get_channel(REGISTRATION_CHANNEL_ID)
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

    # Reply
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

# // clearregistrations

def status_layout(title: str, description: str, color: discord.Color) -> LayoutView:
    """A no-button Components V2 'embed' (Container) used for the resolved states
    (cleared / cancelled / timed out) once the confirmation buttons are gone."""
    layout = LayoutView(timeout=None)
    container = Container(
        TextDisplay(f"### {title}"),
        TextDisplay(description),
        accent_color=color,
    )
    layout.add_item(container)
    return layout


class ConfirmClearLayout(LayoutView):
    """Components V2 confirmation prompt: the title/description ('embed') and the
    Confirm/Cancel buttons live in the *same* Container, instead of an embed with
    a separate button row underneath it."""

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


@bot.tree.command(name="clearregistrations", description="Clear all messages in the registration channel.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def clearregistrations(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    # Check bot perms

    reg_channel = bot.get_channel(REGISTRATION_CHANNEL_ID)
    if not reg_channel:
        return await send_error(interaction, "Registration channel not found.")

    permissions = reg_channel.permissions_for(interaction.guild.me)
    if not permissions.manage_messages:
        return await send_error(interaction, "I need Manage Messages permission in the registration channel to clear messages.")

    # Confirmation
    view = ConfirmClearLayout(interaction.user.id, reg_channel)
    message = await interaction.followup.send(view=view, ephemeral=True)

    await view.wait() # Confirmation wait

    if not view.confirmed:
        return # User cancel (message deleted by the button) or timed out

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
        return await message.edit(
            view=status_layout("Scan Failed", f"Failed to scan registration messages: {e}", discord.Color.red())
        )

    # Bulk delete only the matched registration messages, in chunks of 100

    deleted_count = 0
    try:
        for i in range(0, len(to_delete), 100):
            chunk = to_delete[i:i + 100]
            await reg_channel.delete_messages(chunk)
            deleted_count += len(chunk)
    except Exception as e:
        return await message.edit(
            view=status_layout("Clear Failed", f"Failed to clear messages: {e}", discord.Color.red())
        )

    await message.edit(
        view=status_layout("✅ Registrations Cleared", f"Cleared {deleted_count} registrations.", discord.Color.green())
    )

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
        return await send_error(interaction, "That emoji is already used.")
    if any(role.mention in line for line in lines):
        return await send_error(interaction, "That role is already assigned.")

    if note:
        lines.append(f"{emoji} — {role.mention} *( {note} )*")
    else:
        lines.append(f"{emoji} — {role.mention}")

    embed.description = "\n".join(lines)

    await msg.edit(embed=embed)
    await msg.add_reaction(emoji)

    await send_success(interaction, f"Added reaction role: {emoji} for {role.mention}" + (f" — {note}" if note else ""))

# // toggleaccess

@bot.tree.command(name="toggleaccess", description="Toggle the Bot Access role for a user.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to toggle the role for")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def toggleaccess(interaction: discord.Interaction, user: discord.Member):
    guild = interaction.guild
    role = guild.get_role(REQUIRED_ROLE_ID)
    if not role:
        return await send_error(interaction, "Bot Access role not found.")

    if role in user.roles:
        await user.remove_roles(role, reason=f"Toggled off Bot Access role by {interaction.user}")
        await send_success(interaction, f"Removed {role.name} role from {user.mention}.")
    else:
        await user.add_roles(role, reason=f"Toggled on Bot Access role by {interaction.user}")
        await send_success(interaction, f"Granted {role.name} role to {user.mention}.")

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
    await send_success(interaction, f"{channel.name} has been {action}.")

# // togglelockdown //

LOCKDOWN_CHANNEL_TYPES = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)


def _lockdown_perms(channel: discord.abc.GuildChannel) -> tuple:
    """Which @everyone overwrite permissions get locked/restored for a given channel type."""
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return ("connect", "send_messages")
    return ("send_messages",)


@bot.tree.command(name="togglelockdown", description="Toggles the lock or unlock state on all text, voice, and stage channels.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def togglelockdown(interaction: discord.Interaction):
    # Defer immediately - looping + editing permissions on every channel in
    # the server can easily take longer than the 3 second window Discord
    # gives an interaction before it expires.
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    everyone_role = guild.default_role

    channels = [ch for ch in guild.channels if isinstance(ch, LOCKDOWN_CHANNEL_TYPES)]
    if not channels:
        return await send_error(interaction, "No text, voice, or stage channels found.")

    count = 0

    if lockdown_snapshots:
        # Lockdown is currently active -> disable it by restoring each
        # channel to whatever state it actually had *before* lockdown was
        # enabled, rather than blanket-unlocking everything. This keeps
        # channels that were already manually locked beforehand locked.
        for channel in channels:
            snapshot = lockdown_snapshots.get(channel.id)
            if snapshot is None:
                continue  # channel didn't exist yet / wasn't touched during lockdown

            overwrite = channel.overwrites_for(everyone_role)
            changed = False
            for perm_name, original_value in snapshot.items():
                if getattr(overwrite, perm_name) != original_value:
                    setattr(overwrite, perm_name, original_value)
                    changed = True

            if changed:
                try:
                    await channel.set_permissions(everyone_role, overwrite=overwrite)
                    count += 1
                except discord.Forbidden:
                    print(f"Missing permissions to restore {channel.name}")
                except Exception as e:
                    print(f"Failed to restore {channel.name}: {e}")

        lockdown_snapshots.clear()
        action = "unlocked"
    else:
        # Not currently in lockdown -> enable it. Snapshot each channel's
        # current overwrite state first so it can be restored exactly later.
        for channel in channels:
            overwrite = channel.overwrites_for(everyone_role)
            perm_names = _lockdown_perms(channel)

            lockdown_snapshots[channel.id] = {perm: getattr(overwrite, perm) for perm in perm_names}

            changed = False
            for perm_name in perm_names:
                if getattr(overwrite, perm_name) is not False:
                    setattr(overwrite, perm_name, False)
                    changed = True

            if changed:
                try:
                    await channel.set_permissions(everyone_role, overwrite=overwrite)
                    count += 1
                except discord.Forbidden:
                    print(f"Missing permissions to lock {channel.name}")
                except Exception as e:
                    print(f"Failed to lock {channel.name}: {e}")

        action = "locked"

    await send_success(interaction, f"{action.capitalize()} {count} channel(s).")

# // upload //

@bot.tree.command(name="upload", description="Upload a Users.json file to replace the contents of the database. Can be used as a bulk-import.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(file="Upload a Users.json file")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def upload(interaction: Interaction, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True)

    # File extension check
    if not file.filename.lower().endswith(".json"):
        return await send_error(interaction, "Please upload a valid JSON file.")

    try:
        file_bytes = await file.read()
        users_data = json.loads(file_bytes)
    except Exception as e:
        return await send_error(interaction, f"Failed to parse JSON: {e}")

    content_str = json.dumps(users_data, indent=4)

    try:
        sha = await get_current_sha()
        await commit_content(content_str, sha, f"Upload Users.json by {interaction.user}")
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    await interaction.followup.send(
        view=status_layout("✅ Success", "Users.json uploaded successfully.", discord.Color.green()),
        ephemeral=True,
    )

# // tempaccess //

@bot.tree.command(name="tempaccess", description="Temporarily applies the Bot Access role to a user (in minutes).", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to give temporary access", minutes="Duration in minutes")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def tempaccess(interaction: discord.Interaction, user: discord.Member, minutes: int):
    await interaction.response.defer(ephemeral=True)

    if minutes <= 0:
        await send_error(interaction, "Duration must be a positive integer.")
        return

    guild = bot.get_guild(GUILD_ID)
    role = guild.get_role(REQUIRED_ROLE_ID)
    if not role:
        await send_error(interaction, "Bot Access role not found.")
        return

    if role in user.roles:
        await send_error(interaction, f"{user.mention} already has the Bot Access role.")
        return

    if user.id in active_temp_access:
        await send_error(interaction, f"{user.mention} already has a temporary access timer running.")
        return

    # Apply role

    try:
        await user.add_roles(role, reason=f"Temporary Bot Access for {minutes} minutes")
        active_temp_access.add(user.id)
        await send_success(interaction, f"Given Bot Access role to {user.mention} for {minutes} minutes.")

        # Start background timer
        bot.loop.create_task(remove_temp_access_after(user, role, minutes))

    except Exception as e:
        await send_error(interaction, f"Failed to give Bot Access role: {e}")

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

class DbSearchView(LayoutView):
    """Components V2 paginated view for /dbsearch. The 'embed' (title + fields)
    and the Previous/Next buttons live inside a single Container; the buttons
    are only added when there's more than one match."""

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


@bot.tree.command(name="dbsearch", description="Searches the entire database for a value.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(query="Value to search for in all user fields")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def dbsearch(interaction: discord.Interaction, query: str):
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

# // tempwhitelist //

@bot.tree.command(name="tempwhitelist", description="Temporarily whitelists a user for x minutes.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to whitelist temporarily", hwid="Hashed HWID in SHA-256", minutes="Duration in minutes")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def tempwhitelist(interaction: discord.Interaction, user: discord.User, hwid: str, minutes: int):
    await interaction.response.defer(ephemeral=True)
    discord_id = str(user.id)

    if discord_id in active_temp_whitelists:
        return await send_error(
            interaction,
            f"{user.mention} is already temporarily whitelisted until "
            f"{active_temp_whitelists[discord_id].strftime('%Y-%m-%d %H:%M:%S UTC')}.",
        )

    try:
        whitelist_users, sha = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    # Already whitelisted check via discord id
    if find_user_by_discord_id(whitelist_users, discord_id):
        return await send_error(interaction, f"{user.mention} is already in the whitelist.")

    existing_hwid = find_user_by_hwid(whitelist_users, hwid)
    if existing_hwid:
        return await send_error(interaction, f"This HWID is already whitelisted under **{existing_hwid.get('Identifier', 'Unknown')}** (<@{existing_hwid.get('DiscordId')}>).")

    expiration_time = datetime.now(timezone.utc) + timedelta(minutes=minutes)

    new_entry = build_user_entry(
        hwid, user.name, "Temp", discord_id, generate_unique_key(whitelist_users),
        notes=format_expiration_note(expiration_time)
    )
    whitelist_users.append(new_entry)

    try:
        await commit_users(whitelist_users, sha, f"Temp whitelist added: {user.name} ({discord_id}) for {minutes} minutes")
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    active_temp_whitelists[discord_id] = expiration_time

    await send_success(
        interaction,
        f"Temporarily whitelisted {user.mention} for {minutes} minutes.",
        fields=[("HWID", f"||`{hwid}`||", False)],
    )

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

            try:
                current_whitelist, current_sha = await fetch_users_with_sha()
                current_whitelist, _ = remove_user_by_discord_id(current_whitelist, discord_id)
                await commit_users(current_whitelist, current_sha, f"Temp whitelist expired: {user.name} ({discord_id})")
            except GitHubAPIError:
                return

            active_temp_whitelists.pop(discord_id, None)

            try:
                await user.send("Your temporary whitelist has expired and access has now been removed.")
            except Exception:
                pass
        except asyncio.CancelledError:
            pass

    asyncio.create_task(notify_and_remove())

# // checktemp //

@bot.tree.command(name="checktemp", description="Checks a user's temporary whitelist status (via their Notes field) with a live countdown.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="Discord user to check")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def checktemp(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)

    try:
        whitelist_users, _ = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    entry = find_user_by_discord_id(whitelist_users, user.id)
    if not entry:
        return await send_error(interaction, f"{user.mention} is not in the whitelist.")

    notes = entry.get("Notes")
    if not notes:
        return await send_error(
            interaction,
            f"{user.mention} has no notes, which doesn't indicate anything about their whitelist status.",
        )

    expiration_time = parse_expiration_note(notes)
    if not expiration_time:
        return await send_error(
            interaction,
            f"{user.mention}'s notes don't mark them as a temporary whitelist entry.",
            fields=[("Notes", notes, False)],
        )

    now = datetime.now(timezone.utc)
    if (expiration_time - now).total_seconds() <= 0:
        return await send_error(
            interaction,
            f"{user.mention}'s temporary whitelist already expired on <t:{int(expiration_time.timestamp())}:F>. "
            "It should be removed automatically shortly, if it hasn't been already.",
        )

    def build_tracker_embed(now_: datetime) -> discord.Embed:
        remaining_ = expiration_time - now_
        expires_ts = int(expiration_time.timestamp())

        fields = [
            ("Identifier", entry.get("Identifier"), True),
            ("Rank", entry.get("Rank"), True),
            ("Discord ID", f"{entry.get('DiscordId')} ({user.mention})", True),
            ("HWID", f"||{entry.get('HWID')}||" if entry.get("HWID") else "N/A", True),
            ("Key", f"||{entry.get('Key')}||" if entry.get("Key") else "N/A", True),
            ("Join Date", format_discord_timestamp(entry.get("JoinDate", "Unknown")), True),
            ("Expires", f"<t:{expires_ts}:F>", True),
            ("Time Left", humanize_timeleft(remaining_), True),
        ]

        embed = discord.Embed(
            title=f"Temporary Whitelist: {entry.get('Identifier', user.name)}",
            color=discord.Color.gold(),
            timestamp=now_,
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        for name, value, inline in fields:
            embed.add_field(name=name, value=value or "N/A", inline=inline)
        embed.set_footer(text="Live countdown • updates automatically until expiry")
        return embed

    # Deliver the tracker as a DM to the invoker rather than posting it in
    # the channel, so it's private to them. A true Discord "ephemeral"
    # response isn't viable here: ephemeral/webhook messages can only be
    # edited for ~15 minutes after the command was invoked (the interaction
    # token expires), but this tracker may need to keep editing itself for
    # months. A DM, like a channel message, is a normal message editable via
    # the bot's REST client with no such time limit -- it's just only
    # visible to the recipient.
    try:
        tracker_message = await interaction.user.send(embed=build_tracker_embed(now))
    except discord.Forbidden:
        return await edit_or_send_error(
            interaction,
            "I couldn't DM you the tracker -- you likely have DMs from server "
            "members disabled for this server. Enable them and run the command again.",
        )

    # Turn the deferred ephemeral placeholder into a short confirmation
    # (rather than deleting it, since there's no longer a visible tracker
    # message in the channel to stand in as the confirmation).
    try:
        await interaction.edit_original_response(
            embed=success_embed(f"Sent you a DM with {user.mention}'s live temporary whitelist tracker.")
        )
    except discord.HTTPException:
        pass

    async def update_loop():
        # Anchored to the event loop's monotonic clock so each tick is
        # scheduled relative to the *previous scheduled tick*, not to
        # "whenever the last edit happened to finish". Without this, any
        # latency in tracker_message.edit() (Discord briefly queuing/rate-
        # limiting the edit, a slow response, etc.) gets added on top of the
        # normal sleep, so the loop runs slightly late -- and since the
        # displayed value is always recomputed from the real wall clock,
        # that lateness shows up as the countdown appearing to pause for a
        # beat and then jump by more than one step when it catches up.
        # Scheduling against a fixed cadence instead means an occasional
        # slow edit just eats into the *next* sleep rather than compounding.
        loop_clock = asyncio.get_running_loop()
        next_tick = loop_clock.time()
        try:
            while True:
                now_ = datetime.now(timezone.utc)
                remaining_seconds = (expiration_time - now_).total_seconds()

                if remaining_seconds <= 0:
                    expired_embed = discord.Embed(
                        title=f"Temporary Whitelist Expired: {entry.get('Identifier', user.name)}",
                        description=f"{user.mention}'s temporary whitelist expired on <t:{int(expiration_time.timestamp())}:F>.",
                        color=discord.Color.red(),
                        timestamp=now_,
                    )
                    expired_embed.set_footer(text="This tracker is no longer updating.")
                    try:
                        await tracker_message.edit(embed=expired_embed)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                    return

                try:
                    await tracker_message.edit(embed=build_tracker_embed(now_))
                except discord.NotFound:
                    # Tracker message was deleted -- nothing left to update.
                    return
                except discord.Forbidden:
                    return
                except discord.HTTPException:
                    pass  # Transient (rate limit, etc.) -- just retry next tick.

                # Update more often as the deadline nears, so the "X left" text
                # stays accurate without hammering the API on long (month/year)
                # whitelists that would otherwise need thousands of edits.
                #
                # The <=60s bucket ticks every 2s rather than every 1s on
                # purpose: editing a single message once per second sits
                # right at the edge of Discord's practical rate limit for
                # repeated edits on one message, so it was an easy bucket to
                # tip over (causing the pause-then-jump). 2s leaves headroom
                # while still reading as "live".
                if remaining_seconds <= 60:
                    interval = 2
                elif remaining_seconds <= 3600:
                    interval = 15
                elif remaining_seconds <= 86400:
                    interval = 60
                elif remaining_seconds <= 7 * 86400:
                    interval = 1800
                else:
                    interval = 3600

                next_tick += interval
                sleep_for = max(0, next_tick - loop_clock.time())
                await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            pass

    asyncio.create_task(update_loop())


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

                    if REACTION_ROLE_DMS_ENABLED:
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

@bot.event
async def on_raw_reaction_remove(payload):
    if payload.message_id != reaction_roles_message_id:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return

    # Raw reaction remove events don't include member data, so it has to be
    # resolved manually. Fall back to a fetch if the member isn't cached.
    member = guild.get_member(payload.user_id)
    if not member:
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.NotFound:
            return
        except discord.HTTPException:
            return

    if member.bot:
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
                if role and role in member.roles:
                    await member.remove_roles(role, reason="Reaction role unassigned")

                    if REACTION_ROLE_DMS_ENABLED:
                        # Fancy embed DM
                        role_display = role.name

                        dm_embed = discord.Embed(
                            title="Role Removed!",
                            description=f"You have **lost** the role **{role_display}** in **{guild.name}**.",
                            color=discord.Color.red(),
                            timestamp=datetime.now()
                        )
                        dm_embed.set_thumbnail(url=role.icon.url if role.icon else guild.icon.url if guild.icon else None)
                        dm_embed.set_footer(text="Reaction Role System")
                        try:
                            await member.send(embed=dm_embed)
                        except discord.Forbidden:
                            pass
            break

# // clearnotes //

@bot.tree.command(name="clearnotes", description="Clears the notes field for a user in the GitHub whitelist JSON.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="The user whose notes to clear")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def clearnotes(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)

    try:
        users, sha = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    discord_id_str = str(user.id)
    entry = find_user_by_discord_id(users, discord_id_str)
    if not entry:
        return await send_error(interaction, f"No user found with Discord ID {user.mention}.")

    entry["Notes"] = None

    try:
        await commit_users(users, sha, f"Cleared notes for user: {user} ({discord_id_str})")
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    await send_success(interaction, f"Notes cleared for {user.mention}.")


# --- Error Handler ---

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Unwrap CommandInvokeError/TransformerError to get at the underlying exception
    original = getattr(error, "original", error)

    # Catch transformer errors caused by bad member conversion

    if isinstance(error, app_errors.TransformerError):
        # Check if the error is related to the member converter failing

        if "to Member" in str(error):
            await send_error(interaction, "That user is not in this server.")
            return

    if isinstance(error, app_commands.CheckFailure):
        await send_error(interaction, str(error))
        return

    # Catch Discord's "Embed size exceeds maximum size of 6000" HTTPException
    # (error code 50035, Invalid Form Body) so it doesn't just get printed
    # and swallowed, leaving the user with no response at all.
    if isinstance(original, discord.HTTPException) and "Embed size exceeds maximum size" in str(original):
        await send_error(
            interaction,
            "The response was too large to display (Discord limits embeds to 6,000 characters total). "
            "Try narrowing your request so it returns less data.",
        )
        return

    # Catch-all for any other Discord API errors (rate limits, malformed
    # payloads, permission issues surfaced as HTTP errors, etc.) so the user
    # always gets *some* response instead of the command silently failing.
    if isinstance(original, discord.HTTPException):
        print(f"Unhandled HTTPException: {original.status} {original.code} - {original.text}")
        try:
            await send_error(
                interaction,
                f"Something went wrong talking to Discord (HTTP {original.status}, error code {original.code}). "
                "Please try again, and let a developer know if it keeps happening.",
            )
        except Exception as e:
            print(f"Failed to notify user of HTTPException: {e}")
        return

    print(f"Unhandled error: {error}")

# on_app_command_error above only covers slash commands (it's registered on
# bot.tree). Raw gateway events like on_raw_reaction_add/on_raw_reaction_remove
# aren't slash commands, so exceptions in them (e.g. the Forbidden/"Missing
# Permissions" error from add_roles/remove_roles when the bot's role sits
# below the target role) never reach it -- they instead hit discord.py's
# default on_error, which just prints "Ignoring exception in <event>" and
# swallows it with no feedback to anyone. This override is that missing
# counterpart for raw events.
@bot.event
async def on_error(event_method, *args, **kwargs):
    exc_type, exc, tb = sys.exc_info()

    if isinstance(exc, discord.Forbidden):
        print(f"Missing permissions in {event_method}: {exc.text} (error code: {exc.code})")

        # For reaction role events specifically, the payload (first arg) tells
        # us who was affected, so we can let them know it didn't work instead
        # of leaving them thinking the role was applied/removed.
        if event_method in ("on_raw_reaction_add", "on_raw_reaction_remove") and args:
            payload = args[0]
            guild = bot.get_guild(getattr(payload, "guild_id", None))
            if guild:
                member = guild.get_member(payload.user_id)
                if member and not member.bot:
                    action = "add that role to you" if event_method == "on_raw_reaction_add" else "remove that role from you"
                    await notify_permission_error(member, action, guild.name)
        return

    # Anything else: log it the same way discord.py's default handler would,
    # so unrelated bugs are still fully visible in the console.
    print(f"Unhandled exception in {event_method}:")
    traceback.print_exception(exc_type, exc, tb)

# --- Run Bot ---

bot.run(TOKEN)
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
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone, timedelta
import json
import base64
import re
import io
import csv
import difflib
import hashlib
from discord.ui import Modal, TextInput, View, Button, LayoutView, Container, TextDisplay, ActionRow, Section, Thumbnail, File, Label, Select, Checkbox
from collections import defaultdict

import bot_api
from bot_api import (
    GUILD_ID, REQUIRED_ROLE_ID, REGISTRATION_CHANNEL_ID, REACTION_ROLE_CHANNEL_ID, PANEL_CHANNEL_ID, BUYER_ROLE_ID,
    REDEEM_ALERTS_CHANNEL_ID,
    GitHubAPIError,
    fetch_raw_users, fetch_users_with_sha, fetch_api_file, fetch_raw_text,
    fetch_api_text_and_sha, commit_content, commit_users, get_current_sha,
    list_commits, get_commit,
    fetch_permitted_keys_with_sha, commit_permitted_keys, remove_permitted_key, remove_permitted_keys,
    remove_first_n_permitted_keys, is_key_permitted,
    fetch_stored_script, fetch_stored_script_with_sha, commit_stored_script, inject_script_key, validate_stored_script,
    find_user_by_discord_id, find_user_by_hwid, find_user_by_key, remove_user_by_discord_id, build_user_entry,
    revoke_buyer_role, find_removed_discord_ids,
    generate_key, generate_unique_key, generate_unique_keys, parse_key_length_range, is_valid_hwid, is_valid_discord_id,
    format_discord_timestamp, format_join_date,
    get_available_hash_algorithms, hash_text, SHAKE_OUTPUT_BYTES,
    TRANSFORM_FORMAT_CHOICES, transform_text,
    format_expiration_note, parse_expiration_note, humanize_timeleft, is_notes_locked,
    RESET_HWID_COOLDOWN, hwid_reset_cooldown_remaining,
    get_cached_users, refresh_users_cache,
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

# Caps /genkey's bulk `amount` option. Mainly guards against an oversized
# permittedKeys.txt commit and against blowing well past what the inline
# embed / fallback file can reasonably display, not a security control.
MAX_BULK_GENKEY_AMOUNT = 100

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

        # Re-registers the /createpanel control panel's button handlers so
        # they keep responding after a bot restart. This does NOT resend the
        # message -- the panel embed posted by /createpanel stays put in
        # #panel; this just reconnects its (fixed custom_id) buttons to a
        # live view again, since ControlPanelView(timeout=None) instances
        # don't otherwise survive a process restart.
        self.add_view(ControlPanelView())

        # Guarded with is_running() since on_ready can fire again on
        # reconnect, and tasks.loop.start() raises if it's already going.
        if not refresh_users_cache_task.is_running():
            refresh_users_cache_task.start()

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

# --- Users.json cache refresh task ---
#
# Keeps bot_api's in-memory Users.json cache warm so read-only whitelist/
# cooldown pre-checks (e.g. the control panel's Reset HWID button, in
# ControlPanelView.reset_hwid below) never have to make a live network call
# on the interaction's critical path -- so they can't time out or silently
# fail open the way the old fetch_raw_users()-with-a-2s-timeout check did.
#
# commit_content() (used by every write path, including commit_users() for
# redeem/edituser/reset hwid/etc.) already updates the cache immediately on
# every write the bot makes itself, so this loop only has to catch external
# changes -- e.g. someone editing Users.json by hand on GitHub, or a
# /rollback -- within USERS_CACHE_REFRESH_INTERVAL seconds. It refreshes via
# the Contents API rather than the raw.githubusercontent.com CDN precisely
# so that "within USERS_CACHE_REFRESH_INTERVAL seconds" is actually true --
# the CDN endpoint can lag well past that on its own.
USERS_CACHE_REFRESH_INTERVAL = 60  # seconds

@tasks.loop(seconds=USERS_CACHE_REFRESH_INTERVAL)
async def refresh_users_cache_task():
    try:
        await refresh_users_cache()
    except GitHubAPIError as e:
        # Leave the existing cache in place and just try again next tick --
        # stale-but-known beats throwing away the last good copy.
        print(f"Failed to refresh Users.json cache: {e}")

@refresh_users_cache_task.before_loop
async def before_refresh_users_cache_task():
    await bot.wait_until_ready()

# --- Commands ---

# // ping //

@bot.tree.command(name="ping", description="Returns the bot's latency.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ping(interaction: discord.Interaction):
    await send_success(interaction, f"Pong! Latency: {round(bot.latency * 1000)}ms", title="🏓 Pong")

# // hash //

async def hash_algorithm_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    """
    Populates /hash's `algorithm` option as the user types. hashlib can
    easily expose more algorithms than Discord's 25-result autocomplete cap
    (especially once OpenSSL's extras are counted), so this narrows to
    substring matches against whatever's typed so far instead of always
    showing the same first 25 alphabetically -- typing "sha" surfaces every
    sha1/sha2/sha3/shake variant instead of getting stuck on "blake2b".
    """
    algorithms = get_available_hash_algorithms()
    query = current.lower().strip()
    matches = [a for a in algorithms if query in a] if query else algorithms
    return [app_commands.Choice(name=a, value=a) for a in matches[:25]]


@bot.tree.command(name="hash", description="Hashes text using a chosen algorithm (MD5, SHA-2, SHA-3, BLAKE2, SHAKE, etc).", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(text="The text to hash", algorithm="Hash algorithm to use -- start typing to search the full list")
@app_commands.autocomplete(algorithm=hash_algorithm_autocomplete)
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def hash_cmd(interaction: discord.Interaction, text: str, algorithm: str):
    algo = algorithm.lower().strip()
    available = get_available_hash_algorithms()

    if algo not in available:
        # Autocomplete only *suggests* valid values -- Discord still lets a
        # user submit whatever raw text they typed instead of picking a
        # suggestion, so this re-validates rather than trusting the input.
        suggestion = difflib.get_close_matches(algo, available, n=1)
        hint = f" Did you mean `{suggestion[0]}`?" if suggestion else " Start typing to see the list of supported algorithms."
        return await send_error(interaction, f"`{algorithm}` isn't a supported hash algorithm.{hint}")

    try:
        digest = hash_text(algo, text)
    except (TypeError, ValueError) as e:
        return await send_error(interaction, f"Failed to hash text with `{algo}`: {e}")

    def _safe_codeblock(value: str, limit: int = 1000) -> str:
        # Truncate to stay under Discord's 1024-char embed field limit, and
        # break up any literal ``` in the input so it can't prematurely
        # close the surrounding code block.
        value = value.replace("```", "``\u200b`")
        if len(value) > limit:
            value = value[:limit] + "… (truncated)"
        return value

    algorithm_label = f"`{algo}`"
    if algo.startswith("shake_"):
        algorithm_label += f" (SHAKE / XOF -- shown at {SHAKE_OUTPUT_BYTES * 8}-bit output length)"

    embed = build_embed(
        title="🔐 Hash Result",
        color=discord.Color.blue(),
        fields=[
            ("Algorithm", algorithm_label, False),
            ("Before", f"```{_safe_codeblock(text)}```", False),
            ("After", f"```{_safe_codeblock(digest)}```", False),
        ],
    )
    await safe_respond(interaction, embed=embed, ephemeral=True)

# // transform //

@bot.tree.command(name="transform", description="Transforms text into a stylized Unicode format (superscript, cursive, zalgo, and more).", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(text="The text to transform", format="Style to transform the text into")
@app_commands.choices(format=[app_commands.Choice(name=name, value=value) for name, value in TRANSFORM_FORMAT_CHOICES])
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def transform_cmd(interaction: discord.Interaction, text: str, format: app_commands.Choice[str]):
    try:
        result = transform_text(format.value, text)
    except ValueError as e:
        return await send_error(interaction, str(e))

    def _safe_codeblock(value: str, limit: int = 1000) -> str:
        # Truncate to stay under Discord's 1024-char embed field limit, and
        # break up any literal ``` in the input/output so it can't
        # prematurely close the surrounding code block.
        value = value.replace("```", "``\u200b`")
        if len(value) > limit:
            value = value[:limit] + "… (truncated)"
        return value

    embed = build_embed(
        title="🎨 Transform Result",
        color=discord.Color.blue(),
        fields=[
            ("Format", f"`{format.name}`", False),
            ("Before", f"```{_safe_codeblock(text)}```", False),
            ("After", f"```{_safe_codeblock(result)}```", False),
        ],
    )
    await safe_respond(interaction, embed=embed, ephemeral=True)

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

# // ghostping //

@bot.tree.command(name="ghostping", description="Sends a user's mention in this channel and deletes it immediately.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to ghost ping")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ghostping(interaction: discord.Interaction, user: discord.User):
    # The only real latency here is two sequential Discord API calls: send,
    # then delete. Delete needs the message ID that only comes back in
    # send()'s own response, so the two can't be run concurrently with
    # asyncio.gather() or similar -- delete strictly depends on send's
    # result. What asyncio buys here is that nothing else competes for the
    # event loop between the two calls below: no interaction ack, no embed
    # building, no logging -- literally send immediately followed by
    # delete, so the mention is live for exactly as long as these two HTTP
    # round trips take and not a moment longer.
    #
    # channel.send() is used directly (rather than
    # interaction.response.send_message() + interaction.original_response())
    # since send() already hands back the created Message with its id
    # populated -- no extra fetch needed just to get something to delete.
    try:
        msg = await interaction.channel.send(
            user.mention,
            allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False),
        )
        await msg.delete()
    except discord.Forbidden:
        return await send_error(interaction, "Missing permissions to send or delete messages in this channel.")
    except discord.HTTPException as e:
        return await send_error(interaction, f"Failed to ghost ping: {e}")

    # Only reached after the ping is already gone, so this can't add any
    # delay to the window it was actually visible for.
    await send_success(interaction, f"Ghost pinged {user.mention}.")

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
    embed.add_field(name="Last HWID Reset", value=format_discord_timestamp(user_data.get("LastHwidReset")), inline=True)
    embed.add_field(name="Total HWID Resets", value=str(user_data.get("totalHwidResets", 0)), inline=True)

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

# Discord's String Select requires a fixed, predefined set of choices (max 25)
# -- unlike the old free-text `rank` argument, which accepted anything.
# Edit this list to match whatever rank tiers are actually in use.
WHITELIST_RANKS = ["User", "Premium", "VIP", "Staff", "Admin", "Owner"]


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

        # Checks that don't need any network calls run first, and respond
        # immediately (without deferring) so an obviously bad submission
        # errors right away in the modal instead of waiting on a fetch.

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

        # Duplicate checks

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


@bot.tree.command(name="whitelist", description="Adds a user to the database.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def whitelist(interaction: discord.Interaction):
    await interaction.response.send_modal(WhitelistModal())

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

    await revoke_buyer_role(interaction.guild, discord_id)

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
            new_users = json.loads(new_content)
        except json.JSONDecodeError as e:
            await send_error(interaction, f"Invalid JSON: {e}")
            return

        # Fetch the latest content (not just the sha) to avoid race
        # conditions on the commit *and* to check the Notes-lock guard below
        # against genuinely current data, not whatever this modal happened
        # to be pre-filled with when it was opened.
        try:
            current_users, sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            await send_error(interaction, str(e))
            return

        # Block this edit from silently overwriting/clearing the Notes
        # field of any entry that's currently temporarily whitelisted --
        # same guard as /edituser, /clearnotes, and the Edit User button,
        # just applied here across every entry in the pasted JSON at once.
        # An entry being removed entirely (a legitimate unwhitelist-style
        # edit) is fine; only a *changed* Notes value on an entry that
        # still exists is blocked.
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
                await send_error(
                    interaction,
                    "This edit changes the Notes field of a currently temporarily whitelisted user, which "
                    "isn't allowed -- Notes stores the auto-removal timestamp the temp-whitelist system "
                    "relies on. Remove those changes and resubmit.",
                    fields=[("Affected users", ", ".join(locked_violations), False)],
                )
                return

        try:
            await commit_content(new_content, sha, f"Edit whitelist by {interaction.user}")
        except GitHubAPIError as e:
            await send_error(interaction, str(e))
            return

        for discord_id in find_removed_discord_ids(current_users, new_users):
            await revoke_buyer_role(interaction.guild, discord_id)

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

class EditUserCommandModal(Modal):
    """Multi-field edit modal opened by /edituser. Unlike WhitelistModal
    (a brand new, always-empty entry), this needs to be pre-filled with the
    target's *current* values, which aren't known until the command runs --
    so the Label-wrapped fields are built per-instance in __init__ and
    added with add_item(), rather than declared as static class attributes.

    Which Discord user's entry this is editing is already fixed by the
    /edituser `user` argument, so the modal doesn't ask that again. It does
    still expose a "Discord User" field (styled the same free-text ID/mention
    way as /whitelist) so a wrong DiscordId can be corrected without
    reaching for /editwhitelist -- that's a distinct thing from "which entry
    did we load".

    Discord caps modals at 5 top-level components, so JoinDate and Key
    are intentionally left out (same 5 fields /whitelist itself asks for);
    those can still be changed via /editwhitelist or the Edit User button
    on /viewwhitelist.
    """

    def __init__(self, user_entry: Dict[str, Any]):
        title = f"Edit {user_entry.get('Identifier', 'User')}"
        if len(title) > 45:
            title = title[:42] + "..."
        super().__init__(title=title)

        # Stored so on_submit can tell "untouched" from "deliberately
        # changed" -- see the comment above the HWID check below.
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

        # Checks that don't need any network calls run first, and respond
        # immediately (without deferring) so a bad submission errors right
        # away in the modal instead of waiting on a fetch.

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


@bot.tree.command(name="edituser", description="Edits a whitelisted user's info.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="User to edit")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def edituser(interaction: discord.Interaction, user: discord.User):
    try:
        users, _sha = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    user_entry = find_user_by_discord_id(users, user.id)
    if not user_entry:
        return await send_error(interaction, f"User {user.mention} not found in whitelist.")

    await interaction.response.send_modal(EditUserCommandModal(user_entry))

# // genkey //

@bot.tree.command(name="genkey", description="Generates one or more unique, random keys.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    amount="How many keys to generate",
    allow_redemption="Commit the generated keys to permittedKeys.txt so they're redeemable via the control panel",
    length="Key length: a single number (e.g. 20) or a range (e.g. 5-10). Defaults to 25-40.",
)
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def genkey(interaction: discord.Interaction, amount: int, allow_redemption: bool = False, length: Optional[str] = None):
    if amount < 1:
        return await send_error(interaction, "`amount` must be at least 1.")
    if amount > MAX_BULK_GENKEY_AMOUNT:
        return await send_error(interaction, f"`amount` can't exceed {MAX_BULK_GENKEY_AMOUNT} at once.")

    if length is not None:
        try:
            min_length, max_length = parse_key_length_range(length)
        except ValueError as e:
            return await send_error(interaction, str(e))
    else:
        min_length, max_length = 25, 40  # generate_key()'s own defaults

    await interaction.response.defer(ephemeral=True)

    try:
        users, _users_sha = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    # Always cross-checked against both already-assigned Keys and whatever's
    # currently sitting in permittedKeys.txt (fetched via the sha-returning
    # variant regardless of allow_redemption, since it's needed for the
    # commit below anyway when allow_redemption is True, and costs nothing
    # extra to read from when it's False).
    try:
        permitted_keys, keys_sha = await fetch_permitted_keys_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    existing_keys = {u.get("Key") for u in users if u.get("Key")} | set(permitted_keys)
    new_keys = generate_unique_keys(amount, existing_keys, min_length, max_length)

    if allow_redemption:
        try:
            await commit_permitted_keys(
                permitted_keys + new_keys,
                keys_sha,
                f"Bulk generated {len(new_keys)} key(s) for redemption by {interaction.user}",
            )
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

    footer_text = (
        "Committed to permittedKeys.txt -- redeemable now via the control panel."
        if allow_redemption else
        "Not committed -- not yet redeemable via the control panel."
    )

    keys_block = "\n".join(f"||`{k}`||" for k in new_keys)
    title = f"🔐 Generated {len(new_keys)} Key{'s' if len(new_keys) != 1 else ''}"

    # Same inline-vs-file fallback /rollback's diff view uses: spoiler-tagged
    # inline text is nicer when it fits, but a large amount/length combo can
    # blow past Discord's message/embed limits, so fall back to an attached
    # file rather than truncating the list of keys.
    if len(keys_block) <= 1800:
        embed = discord.Embed(title=title, description=keys_block, color=discord.Color.purple())
        embed.set_footer(text=footer_text)
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        filename = "SPOILER_generated_keys.txt"
        file = discord.File(io.BytesIO(("\n".join(new_keys) + "\n").encode()), filename=filename)
        layout = file_success_layout(f"**{title}**\n{footer_text}", filename)
        await interaction.followup.send(view=layout, file=file, ephemeral=True)

# // getkeys //

@bot.tree.command(name="getkeys", description="Displays every key currently available for redemption.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def getkeys(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    # fetch_permitted_keys_with_sha() (Contents API) rather than
    # fetch_permitted_keys() (raw CDN) even though nothing here writes back
    # -- same reasoning as everywhere else this distinction shows up in this
    # file: this command's whole purpose is showing the live, current list,
    # so it shouldn't risk the CDN's staleness (e.g. right after a
    # /genkey ... allow_redemption:True or a /clearkeys).
    try:
        permitted_keys, _sha = await fetch_permitted_keys_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    if not permitted_keys:
        return await send_success(interaction, "No keys are currently available for redemption.")

    keys_block = "\n".join(f"||`{k}`||" for k in permitted_keys)
    title = f"🔑 {len(permitted_keys)} Available Key{'s' if len(permitted_keys) != 1 else ''}"

    # Same inline-vs-file fallback /genkey uses -- see the global error
    # handler's "or fewer in length" branch for the safety net covering
    # whatever slips past this (it isn't a substitute for this check, since
    # a file is a much better experience than an error for a long list).
    if len(keys_block) <= 1800:
        embed = discord.Embed(title=title, description=keys_block, color=discord.Color.purple())
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        filename = "SPOILER_available_keys.txt"
        file = discord.File(io.BytesIO(("\n".join(permitted_keys) + "\n").encode()), filename=filename)
        layout = file_success_layout(f"**{title}**", filename)
        await interaction.followup.send(view=layout, file=file, ephemeral=True)

# // clearkeys //

@bot.tree.command(name="clearkeys", description="Removes keys from permittedKeys.txt -- provide a list of keys, or a number to clear, not both.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    keys="Space/comma separated list of exact keys to remove",
    amount="Number of keys to remove (earliest entries first) -- use instead of `keys`, not with it",
)
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def clearkeys(interaction: discord.Interaction, keys: Optional[str] = None, amount: Optional[int] = None):
    if keys is not None and amount is not None:
        return await send_error(interaction, "Provide either `keys` or `amount`, not both.")
    if keys is None and amount is None:
        return await send_error(interaction, "Provide either `keys` or `amount`.")
    if amount is not None and amount < 1:
        return await send_error(interaction, "`amount` must be at least 1.")

    await interaction.response.defer(ephemeral=True)

    try:
        permitted_keys, sha = await fetch_permitted_keys_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    if not permitted_keys:
        return await send_error(interaction, "There's nothing to clear -- permittedKeys.txt is already empty.")

    if keys is not None:
        requested = [k.strip() for k in re.split(r"[,\s]+", keys.strip()) if k.strip()]
        if not requested:
            return await send_error(interaction, "No valid keys were provided.")
        remaining, removed = remove_permitted_keys(permitted_keys, requested)
        not_found = [k for k in requested if k not in removed]
    else:
        remaining, removed = remove_first_n_permitted_keys(permitted_keys, amount)
        not_found = []

    if not removed:
        return await send_error(interaction, "None of the provided keys were found in permittedKeys.txt.")

    try:
        await commit_permitted_keys(remaining, sha, f"Cleared {len(removed)} key(s) by {interaction.user}")
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    fields = [
        ("Removed", str(len(removed)), True),
        ("Remaining", str(len(remaining)), True),
    ]
    if not_found:
        not_found_display = ", ".join(f"||`{k}`||" for k in not_found)
        if len(not_found_display) > 1000:
            not_found_display = f"{len(not_found)} key(s) not found (too many to list)."
        fields.append(("Not Found", not_found_display, False))

    await send_success(
        interaction,
        f"Cleared {len(removed)} key{'s' if len(removed) != 1 else ''} from permittedKeys.txt.",
        fields=fields,
    )

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
    embed.add_field(name="Last HWID Reset", value=format_discord_timestamp(entry.get("LastHwidReset")), inline=True)
    embed.add_field(name="Total HWID Resets", value=str(entry.get("totalHwidResets", 0)), inline=True)
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

    # A rollback can implicitly "unwhitelist" anyone added after the commit
    # being rolled back to -- they're not targeted individually the way
    # /unwhitelist is, so this diffs the before/after lists to find them.
    try:
        removed_ids = find_removed_discord_ids(json.loads(current_content), json.loads(restored_content))
    except json.JSONDecodeError:
        removed_ids = []
    for discord_id in removed_ids:
        await revoke_buyer_role(interaction.guild, discord_id)

    # Diff what's being replaced against what was just restored, so staff
    # can see exactly what the rollback changed without cross-referencing
    # /commithistory or GitHub directly.
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
        # Keep well under Discord's message/embed limits; attach as a file
        # instead of truncating if the diff doesn't fit inline.
        if len(diff_text) <= 1800:
            description += f"\n\n```diff\n{diff_text}\n```"
        else:
            diff_filename = f"rollback_{sha[:7]}.diff"
            description += f"\n\nDiff too large to display inline ({len(diff_lines)} lines) — see attached file below."
            diff_file = discord.File(io.BytesIO(diff_text.encode()), filename=diff_filename)

    if diff_file:
        # Components V2 layout (same helper /export uses) so the text always
        # renders above the attached file, instead of relying on Discord's
        # default embed/attachment ordering.
        layout = file_success_layout(description, diff_filename)
        await interaction.followup.send(view=layout, file=diff_file, ephemeral=True)
    else:
        await send_success(interaction, description)

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
        ("Last HWID Reset", format_discord_timestamp(user_data.get("LastHwidReset"))),
        ("Total HWID Resets", str(user_data.get("totalHwidResets", 0))),
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
        new_notes = self.notes.value or None
        discord_id = self.user_data.get("DiscordId")

        try:
            existing, sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            await send_error(interaction, str(e))
            return

        # Re-fetched fresh (rather than trusting the possibly-stale
        # self.user_data this modal was opened with) so the Notes-lock check
        # below can't be bypassed by data that's gone stale since the
        # whitelist view was last built/refreshed.
        entry = find_user_by_discord_id(existing, discord_id)
        if not entry:
            await send_error(interaction, "This user's whitelist entry no longer exists (it may have been removed by someone else).")
            return

        if is_notes_locked(entry) and new_notes != (entry.get("Notes") or None):
            await send_error(
                interaction,
                f"**{entry.get('Identifier', 'This user')}**'s Notes field can't be changed right now -- "
                "they're currently temporarily whitelisted, and Notes stores the auto-removal timestamp "
                "the temp-whitelist system relies on. It'll unlock once the temporary whitelist expires "
                "or is removed.",
            )
            return

        # Update user data dictionary with form values

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

        await revoke_buyer_role(interaction.guild, self.discord_id)

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
async def register(interaction: discord.Interaction, identifier: str, hwid: str):
    await interaction.response.defer(ephemeral=True)

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

# // hwidhelp //

@bot.tree.command(name="hwidhelp", description="Shows instructions for getting your HWID.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def hwidhelp(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    embed = discord.Embed(
        title="🔑 HWID Required",
        description=HWID_INSTRUCTIONS,
        color=discord.Color.orange(),
    )
    await interaction.edit_original_response(embed=embed)

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
        current_users, sha = await fetch_users_with_sha()
        await commit_content(content_str, sha, f"Upload Users.json by {interaction.user}")
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    # A bulk upload can implicitly "unwhitelist" anyone missing from the
    # uploaded file -- they're not targeted individually the way
    # /unwhitelist is, so this diffs the before/after lists to find them.
    for discord_id in find_removed_discord_ids(current_users, users_data):
        await revoke_buyer_role(interaction.guild, discord_id)

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

    guild_name = interaction.guild.name
    expires_ts = int(expiration_time.timestamp())
    minute_label = "minute" if minutes == 1 else "minutes"

    # DM the user an embed confirming their temporary whitelist, matching
    # the embed style used everywhere else in the bot (ban/kick/mute
    # notify_user, etc.) instead of a plain-text message.
    try:
        granted_embed = discord.Embed(
            title=f"You've been temporarily whitelisted in {guild_name}",
            description=f"You now have whitelist access to **{guild_name}** for a limited time.",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        granted_embed.add_field(name="Duration", value=f"{minutes} {minute_label}", inline=True)
        granted_embed.add_field(name="Expires", value=f"<t:{expires_ts}:F>\n<t:{expires_ts}:R>", inline=True)
        granted_embed.set_footer(text=f"Granted by: {interaction.user}")
        await user.send(embed=granted_embed)
    except Exception as e:
        print(f"Could not DM temp whitelist grant to {user}: {e}")

    async def notify_and_remove():
        try:
            notify_time = expiration_time - timedelta(minutes=5)
            now = datetime.now(timezone.utc)
            if notify_time > now:
                await asyncio.sleep((notify_time - now).total_seconds())
                try:
                    expiring_embed = discord.Embed(
                        title="Temporary Whitelist Expiring Soon",
                        description=f"Your temporary whitelist access to **{guild_name}** will expire in 5 minutes.",
                        color=discord.Color.orange(),
                        timestamp=datetime.now(timezone.utc),
                    )
                    expiring_embed.add_field(name="Expires", value=f"<t:{expires_ts}:F>\n<t:{expires_ts}:R>", inline=False)
                    await user.send(embed=expiring_embed)
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

            await revoke_buyer_role(interaction.guild, discord_id)

            active_temp_whitelists.pop(discord_id, None)

            try:
                removed_embed = discord.Embed(
                    title="Temporary Whitelist Access Removed",
                    description=f"Your temporary whitelist has expired and your access to **{guild_name}** has now been removed.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                )
                await user.send(embed=removed_embed)
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
            ("Last HWID Reset", format_discord_timestamp(entry.get("LastHwidReset")), True),
            ("Total HWID Resets", str(entry.get("totalHwidResets", 0)), True),
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

# // forceresethwid //

@bot.tree.command(name="forceresethwid", description="Forcefully sets a whitelisted user's HWID, bypassing their reset cooldown.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="The whitelisted user whose HWID to force-reset.", hwid="The user's new HWID, pre-hashed in SHA-256 (64 hex characters).")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def forceresethwid(interaction: discord.Interaction, user: discord.User, hwid: str):
    # Unlike /edituser's modal (capped at 5 components, leaving no room for
    # LastHwidReset/totalHwidResets inputs), this is a plain slash command,
    # so it can go ahead and bump those two fields itself -- same as a
    # self-service reset via the panel's "Reset HWID" button/ResetHWIDModal,
    # just admin-triggered and with the cooldown ignored entirely rather
    # than checked.
    hwid = hwid.strip()

    if not is_valid_hwid(hwid):
        return await send_error(interaction, "Invalid HWID format. Must be 64 hex characters (SHA-256).")

    await interaction.response.defer(ephemeral=True)

    try:
        users, sha = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    discord_id_str = str(user.id)
    entry = find_user_by_discord_id(users, discord_id_str)
    if not entry:
        return await send_error(interaction, f"{user.mention} was not found in the user database.")

    old_hwid = entry.get("HWID")
    if hwid.lower() == (old_hwid or "").lower():
        return await send_error(interaction, f"{user.mention} already has this HWID.")

    # Same duplicate-HWID guard as edituser/ResetHWIDModal -- no user should
    # ever end up sharing another account's HWID, force-reset or not.
    collision = find_user_by_hwid(users, hwid)
    if collision and collision is not entry:
        return await send_error(
            interaction,
            f"This HWID is already whitelisted under **{collision.get('Identifier', 'Unknown')}** (<@{collision.get('DiscordId')}>).",
        )

    entry["HWID"] = hwid
    entry["LastHwidReset"] = format_join_date()
    entry["totalHwidResets"] = entry.get("totalHwidResets", 0) + 1

    try:
        await commit_users(users, sha, f"Force reset HWID for user: {entry.get('Identifier', discord_id_str)} ({discord_id_str})")
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    # No DM to the target -- this just confirms the change to the moderator
    # who ran the command.
    await send_success(
        interaction,
        f"{user.mention}'s HWID has been force reset.",
        fields=[
            ("Old HWID", f"||`{old_hwid}`||", False),
            ("New HWID", f"||`{hwid}`||", False),
            ("Last HWID Reset", format_discord_timestamp(entry["LastHwidReset"]), False),
            ("Total HWID Resets", str(entry["totalHwidResets"]), False),
        ],
    )

# // resethwidcooldown //

@bot.tree.command(name="resethwidcooldown", description="Clears a user's HWID reset cooldown so they can reset their own HWID again immediately.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="The whitelisted user whose HWID reset cooldown to clear.")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def resethwidcooldown(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)

    try:
        users, sha = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    discord_id_str = str(user.id)
    entry = find_user_by_discord_id(users, discord_id_str)
    if not entry:
        return await send_error(interaction, f"{user.mention} was not found in the user database.")

    # hwid_reset_cooldown_remaining() (not just checking LastHwidReset for
    # None) so this correctly reports "nothing to clear" if the cooldown
    # already lapsed on its own, not just if it was never set.
    if hwid_reset_cooldown_remaining(entry) is None:
        return await send_error(interaction, f"{user.mention} is not currently on an HWID reset cooldown.")

    entry["LastHwidReset"] = None

    try:
        await commit_users(users, sha, f"Reset HWID cooldown for user: {entry.get('Identifier', discord_id_str)} ({discord_id_str})")
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    await send_success(
        interaction,
        f"{user.mention}'s HWID reset cooldown has been cleared. They can now reset their own HWID via the control panel immediately.",
    )


# // createpanel //

CONTROL_PANEL_TITLE = "### Control Panel"
CONTROL_PANEL_DESCRIPTION = "Click the buttons below to redeem your key, get the script, or get your role."

# Fixed custom_ids so Discord routes button presses back to these handlers
# even after a bot restart (see ControlPanelView + Client.on_ready).
PANEL_REDEEM_KEY_ID = "panel_redeem_key"
PANEL_GET_SCRIPT_ID = "panel_get_script"
PANEL_GET_ROLE_ID = "panel_get_role"
PANEL_RESET_HWID_ID = "panel_reset_hwid"
PANEL_GET_INFO_ID = "panel_get_info"


# // createpanel - redeem alerts //

async def send_redeem_alert(embed: discord.Embed, view: Optional[View] = None):
    """Best-effort delivery to the Redeem Alerts channel for the control
    panel's Redeem Key flow (successful redemptions + HWID-breach attempts).
    A missing channel or delivery failure here is logged and swallowed
    rather than surfaced to the redeeming user -- their redemption already
    succeeded or failed on its own, independent of whether staff got
    notified about it."""
    channel = bot.get_channel(REDEEM_ALERTS_CHANNEL_ID)
    if not channel:
        print(f"Redeem Alerts channel not found (REDEEM_ALERTS_CHANNEL_ID={REDEEM_ALERTS_CHANNEL_ID}). Set it in bot_api.py.")
        return

    try:
        if view is not None:
            await channel.send(embed=embed, view=view)
        else:
            await channel.send(embed=embed)
    except Exception as e:
        print(f"Failed to send alert to Redeem Alerts channel: {e}")


class HWIDBreachAlertView(View):
    """Attached to the "Potential Breach" alert posted when a Redeem Key
    attempt reuses an HWID that's already whitelisted under a different
    Discord account. The button bans BOTH accounts involved -- the
    attempting redeemer (for trying to use someone else's HWID) and the
    existing owner of that HWID (presumed to have shared/leaked their
    access) -- and unwhitelists the owner's Users.json entry. Only the
    owner's entry gets removed since the attempting redeemer never
    actually gets whitelisted in this scenario (find_user_by_hwid blocks
    the redemption before anything is committed).

    Unlike ControlPanelView, this isn't re-registered in on_ready, so the
    button only stays clickable until the next bot restart -- past that,
    fall back to /unwhitelist + /ban manually using the IDs in the embed."""

    def __init__(self, owner_discord_id: str, owner_identifier: str, hwid: str, attempting_discord_id: str):
        super().__init__(timeout=None)
        self.owner_discord_id = owner_discord_id
        self.owner_identifier = owner_identifier
        self.hwid = hwid
        self.attempting_discord_id = attempting_discord_id

        button = Button(label="❌ Unwhitelist & Ban Both", style=discord.ButtonStyle.danger, custom_id="breach_unwhitelist_ban")
        button.callback = self.unwhitelist_and_ban
        self.add_item(button)

    async def _ban(self, interaction: discord.Interaction, discord_id: str, reason: str) -> str:
        """Bans a single Discord ID (member or not), DMs them a best-effort
        notice, and returns a one-line result string for the summary."""
        try:
            user = await bot.fetch_user(int(discord_id))
        except (discord.NotFound, ValueError):
            user = None

        if user:
            try:
                await notify_user(user, "banned", interaction.user, reason, interaction.guild.name)
            except Exception as e:
                print(f"Failed to DM {user}: {e}")

        try:
            await interaction.guild.ban(discord.Object(id=int(discord_id)), reason=reason)
        except discord.Forbidden:
            return f"❌ Failed to ban `{discord_id}` (missing permissions)"
        except discord.HTTPException as e:
            return f"❌ Failed to ban `{discord_id}`: {e}"

        return f"✅ Banned {user.mention if user else f'`{discord_id}`'}"

    async def unwhitelist_and_ban(self, interaction: discord.Interaction):
        if REQUIRED_ROLE_ID not in [role.id for role in interaction.user.roles]:
            return await send_error(interaction, "You do not have the required permissions to do this.")

        await interaction.response.defer(ephemeral=True)

        results = []

        # Unwhitelist the owner's entry -- the attempting redeemer never had
        # one to begin with, since the duplicate-HWID check blocks the
        # redemption before it's committed.
        try:
            users, sha = await fetch_users_with_sha()
            filtered, removed = remove_user_by_discord_id(users, self.owner_discord_id)
            if removed:
                await commit_users(filtered, sha, f"Unwhitelisted (HWID breach): {self.owner_identifier} ({self.owner_discord_id})")
                await revoke_buyer_role(interaction.guild, self.owner_discord_id)
                results.append(f"✅ Unwhitelisted **{self.owner_identifier}**")
            else:
                results.append(f"⚠️ No whitelist entry found for **{self.owner_identifier}** (may already be removed)")
        except GitHubAPIError as e:
            results.append(f"❌ Failed to unwhitelist **{self.owner_identifier}**: {e}")

        # Ban both accounts, each in its own try/except (via _ban) so a
        # failure on one doesn't block the other.
        owner_reason = f"HWID breach -- HWID shared/leaked, actioned by {interaction.user}"
        attempting_reason = f"HWID breach -- attempted redemption using another user's HWID, actioned by {interaction.user}"

        results.append(await self._ban(interaction, self.owner_discord_id, owner_reason))
        if self.attempting_discord_id != self.owner_discord_id:
            results.append(await self._ban(interaction, self.attempting_discord_id, attempting_reason))

        # Mark the alert as handled so it can't be actioned twice, and
        # record who resolved it directly on the original embed.
        for item in self.children:
            item.disabled = True
        self.children[0].label = "Resolved"

        try:
            resolved_embed = interaction.message.embeds[0]
            resolved_embed.color = discord.Color.dark_grey()
            resolved_embed.add_field(name="Resolved By", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
            await interaction.message.edit(embed=resolved_embed, view=self)
        except Exception as e:
            print(f"Failed to update breach alert message: {e}")

        await send_success(interaction, "\n".join(results), title="Breach Action Complete")


class RedeemKeyModal(Modal, title="Redeem Key"):
    """Self-service equivalent of /register + /whitelist: the user supplies
    their key and pre-hashed HWID (same SHA-256 format enforced everywhere
    else in this file, via is_valid_hwid), the key is checked against
    permittedKeys.txt, and -- if valid -- a new Users.json entry is
    committed for them directly, with no moderator step in between. On
    success, the redeemed key is also removed from permittedKeys.txt so it
    can't be used again."""

    key = Label(
        text="Key",
        description="The key you were given to redeem.",
        component=TextInput(placeholder="Enter your key", max_length=100),
    )
    hwid = Label(
        text="HWID",
        description="Pre-hashed HWID in SHA-256 (64 hex characters). Run /hwidhelp for help getting yours.",
        component=TextInput(placeholder="64-character hex string", min_length=64, max_length=64),
    )

    async def on_submit(self, interaction: discord.Interaction):
        key = self.key.component.value.strip()
        hwid = self.hwid.component.value.strip()

        # Format checks that don't need any network calls run first, so an
        # obviously malformed HWID errors immediately instead of waiting on
        # two GitHub fetches.
        if not is_valid_hwid(hwid):
            return await send_error(interaction, "Invalid HWID format. Must be 64 hex characters (SHA-256).")

        await interaction.response.defer(ephemeral=True)

        try:
            permitted_keys, keys_sha = await fetch_permitted_keys_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        if not is_key_permitted(key, permitted_keys):
            return await send_error(interaction, "That key is invalid. Please double-check it and try again.")

        try:
            users, sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        discord_id_str = str(interaction.user.id)

        existing = find_user_by_discord_id(users, discord_id_str)
        if existing:
            return await send_error(interaction, "You have already redeemed a key and are whitelisted.")

        existing_hwid = find_user_by_hwid(users, hwid)
        if existing_hwid:
            owner_identifier = existing_hwid.get("Identifier", "Unknown")
            owner_discord_id = str(existing_hwid.get("DiscordId", ""))

            breach_embed = build_embed(
                title="🚨 Potential Breach: Duplicate HWID",
                description=(
                    f"{interaction.user.mention} attempted to redeem a key using an HWID that's "
                    f"already whitelisted under a different account. No user should ever have "
                    f"someone else's HWID -- this likely means **{owner_identifier}**'s access "
                    "was shared or leaked."
                ),
                color=discord.Color.orange(),
                fields=[
                    ("Attempting User", f"{interaction.user.mention} (`{discord_id_str}`)", False),
                    ("HWID Owner", f"**{owner_identifier}** (`{owner_discord_id}`)", False),
                    ("HWID", f"||`{hwid}`||", False),
                    ("Key Attempted", f"||`{key}`||", False),
                ],
                timestamp=datetime.now(timezone.utc),
            )
            await send_redeem_alert(breach_embed, HWIDBreachAlertView(owner_discord_id, owner_identifier, hwid, discord_id_str))

            return await send_error(
                interaction,
                f"This HWID is already whitelisted under **{owner_identifier}**.",
            )

        existing_key = find_user_by_key(users, key)
        if existing_key:
            return await send_error(interaction, "This key has already been redeemed by someone else.")

        identifier = interaction.user.name
        rank = "User"
        join_date = format_join_date()

        try:
            users.append(build_user_entry(hwid, identifier, rank, discord_id_str, key, notes=None, join_date=join_date))
            await commit_users(users, sha, f"Redeemed key for user: {identifier} ({discord_id_str})")
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        redeemed_embed = build_embed(
            title="✅ Key Redeemed",
            color=discord.Color.green(),
            fields=[
                ("User", f"{interaction.user.mention} (`{discord_id_str}`)", False),
                ("Identifier", identifier, True),
                ("Rank", rank, True),
                ("Key", f"||`{key}`||", False),
                ("HWID", f"||`{hwid}`||", False),
            ],
            timestamp=datetime.now(timezone.utc),
        )
        await send_redeem_alert(redeemed_embed)

        success_fields = [
            ("Identifier", identifier, True),
            ("Rank", rank, True),
            ("Join Date", format_discord_timestamp(join_date), True),
            ("HWID", f"||`{hwid}`||", False),
        ]

        # The user is already whitelisted at this point regardless of what
        # happens next, so a failure here shouldn't be reported as a plain
        # error (that would look like the whole redemption failed). Instead
        # tell them it succeeded and flag the leftover key for a moderator
        # to clean up manually -- find_user_by_key above already guards
        # against it being redeemed twice in the meantime.
        try:
            updated_keys = remove_permitted_key(permitted_keys, key)
            await commit_permitted_keys(updated_keys, keys_sha, f"Removed redeemed key for user: {identifier} ({discord_id_str})")
        except GitHubAPIError as e:
            return await send_success(
                interaction,
                f"Your key has been redeemed, **{identifier}**! You've been added to the whitelist.\n\n"
                f"⚠️ The key could not be automatically removed from permittedKeys.txt ({e}). "
                "A moderator should remove it manually to prevent reuse.",
                fields=success_fields,
            )

        await send_success(
            interaction,
            f"Your key has been redeemed, **{identifier}**! You've been added to the whitelist.",
            fields=success_fields,
        )


class ResetHWIDModal(Modal, title="Reset HWID"):
    """Lets an already-whitelisted user swap in a new HWID on their own
    entry (e.g. after a hardware change) without needing a moderator to run
    /edituser. Gated by reset_hwid()'s whitelist + cooldown checks before
    this modal is ever shown, and re-checked again here since the fetch
    those checks ran on can be stale by the time the user submits."""

    hwid = Label(
        text="HWID",
        description="Pre-hashed HWID in SHA-256 (64 hex characters). Run /hwidhelp for help getting yours.",
        component=TextInput(placeholder="64-character hex string", min_length=64, max_length=64),
    )

    async def on_submit(self, interaction: discord.Interaction):
        hwid = self.hwid.component.value.strip()

        if not is_valid_hwid(hwid):
            return await send_error(interaction, "Invalid HWID format. Must be 64 hex characters (SHA-256).")

        await interaction.response.defer(ephemeral=True)

        try:
            users, sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        discord_id_str = str(interaction.user.id)
        entry = find_user_by_discord_id(users, discord_id_str)
        if not entry:
            return await send_error(interaction, "You need to redeem a key before you can reset your HWID.")

        remaining = hwid_reset_cooldown_remaining(entry)
        if remaining:
            return await send_error(interaction, f"You can reset your HWID again in {humanize_timeleft(remaining, suffix=False)}.")

        old_hwid = entry.get("HWID")
        if hwid.lower() == (old_hwid or "").lower():
            return await send_error(interaction, "That's already your current HWID.")

        existing_hwid = find_user_by_hwid(users, hwid)
        if existing_hwid and existing_hwid is not entry:
            owner_identifier = existing_hwid.get("Identifier", "Unknown")
            owner_discord_id = str(existing_hwid.get("DiscordId", ""))

            breach_embed = build_embed(
                title="🚨 Potential Breach: Duplicate HWID",
                description=(
                    f"{interaction.user.mention} attempted to reset their HWID to one that's "
                    f"already whitelisted under a different account. No user should ever have "
                    f"someone else's HWID -- this likely means **{owner_identifier}**'s access "
                    "was shared or leaked."
                ),
                color=discord.Color.orange(),
                fields=[
                    ("Attempting User", f"{interaction.user.mention} (`{discord_id_str}`)", False),
                    ("HWID Owner", f"**{owner_identifier}** (`{owner_discord_id}`)", False),
                    ("HWID", f"||`{hwid}`||", False),
                ],
                timestamp=datetime.now(timezone.utc),
            )
            await send_redeem_alert(breach_embed, HWIDBreachAlertView(owner_discord_id, owner_identifier, hwid, discord_id_str))

            return await send_error(
                interaction,
                f"This HWID is already whitelisted under **{owner_identifier}**.",
            )

        entry["HWID"] = hwid
        entry["LastHwidReset"] = format_join_date()
        entry["totalHwidResets"] = entry.get("totalHwidResets", 0) + 1

        try:
            await commit_users(users, sha, f"Reset HWID for user: {entry.get('Identifier', discord_id_str)} ({discord_id_str})")
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        reset_embed = build_embed(
            title="🔄 HWID Reset",
            color=discord.Color.blue(),
            fields=[
                ("User", f"{interaction.user.mention} (`{discord_id_str}`)", False),
                ("Old HWID", f"||`{old_hwid}`||", False),
                ("New HWID", f"||`{hwid}`||", False),
                ("Total Resets", str(entry["totalHwidResets"]), False),
            ],
            timestamp=datetime.now(timezone.utc),
        )
        await send_redeem_alert(reset_embed)

        await send_success(
            interaction,
            "Your HWID has been reset successfully.",
            fields=[
                ("New HWID", f"||`{hwid}`||", False),
                ("Next Reset Available", humanize_timeleft(RESET_HWID_COOLDOWN), False),
                ("Total Resets", str(entry["totalHwidResets"]), False),
            ],
        )


class ControlPanelView(LayoutView):
    """Persistent Components V2 control panel posted by /createpanel into
    #panel. Every button uses a fixed custom_id and this view is constructed
    with timeout=None, so as long as it's re-registered via bot.add_view()
    in on_ready, the buttons keep working indefinitely -- including across
    bot restarts -- without the panel message itself ever needing to be
    resent or edited."""

    def __init__(self):
        super().__init__(timeout=None)

        container = Container(
            TextDisplay(CONTROL_PANEL_TITLE),
            TextDisplay(CONTROL_PANEL_DESCRIPTION),
            accent_color=discord.Color.blurple(),
        )

        row = ActionRow()

        redeem_button = Button(label="Redeem Key", style=discord.ButtonStyle.success, custom_id=PANEL_REDEEM_KEY_ID)
        redeem_button.callback = self.redeem_key
        row.add_item(redeem_button)

        script_button = Button(label="Get Script", style=discord.ButtonStyle.primary, custom_id=PANEL_GET_SCRIPT_ID)
        script_button.callback = self.get_script
        row.add_item(script_button)

        role_button = Button(label="Get Role", style=discord.ButtonStyle.primary, custom_id=PANEL_GET_ROLE_ID)
        role_button.callback = self.get_role
        row.add_item(role_button)

        hwid_button = Button(label="Reset HWID", style=discord.ButtonStyle.secondary, custom_id=PANEL_RESET_HWID_ID)
        hwid_button.callback = self.reset_hwid
        row.add_item(hwid_button)

        info_button = Button(label="Get Info", style=discord.ButtonStyle.secondary, custom_id=PANEL_GET_INFO_ID)
        info_button.callback = self.get_info
        row.add_item(info_button)

        container.add_item(row)
        self.add_item(container)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        # The default View.on_error only logs to the console and leaves the
        # interaction completely unanswered, which is exactly what made
        # this bug look like a silent/dead button instead of a visible
        # error. send_error() -> safe_respond() already picks the right
        # response method (initial vs. followup) based on whether this
        # interaction was already acknowledged, so this works regardless of
        # which step in a callback the exception came from.
        print(f"Error in ControlPanelView for item {item!r}: {error}")
        try:
            await send_error(interaction, "Something went wrong. Please try again, and let a moderator know if it keeps happening.")
        except Exception as e:
            print(f"Failed to notify user of ControlPanelView error: {e}")

    async def redeem_key(self, interaction: discord.Interaction):
        # Sending a modal must be the interaction's very first response,
        # within Discord's ~3 second ack window, so this can't do a live
        # GitHub fetch first the way it used to risk doing -- that's what
        # made this check impractical here before. get_cached_users() reads
        # bot_api's in-memory Users.json cache (see the Reset HWID cache
        # note above and refresh_users_cache_task in main.py) instead, which
        # never touches the network and so can't blow the ack window.
        #
        # If the cache says the user already has an entry, skip the modal
        # entirely instead of letting them fill it out for nothing.
        # RedeemKeyModal.on_submit() still re-checks "already whitelisted"
        # against a fresh fetch before committing anything, so it remains
        # the single source of truth -- this is just a UX improvement, not
        # a security boundary. If the cache hasn't been populated yet (e.g.
        # right after a bot restart), fall back to opening the modal
        # unconditionally, same as before.
        users = get_cached_users()
        if users is not None:
            existing = find_user_by_discord_id(users, str(interaction.user.id))
            if existing:
                return await send_error(interaction, "You have already redeemed a key and are whitelisted.")

        await interaction.response.send_modal(RedeemKeyModal())

    async def get_script(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            users, _sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        entry = find_user_by_discord_id(users, str(interaction.user.id))
        if not entry:
            return await send_error(interaction, "You need to redeem a key before you can get your script.")

        key = entry.get("Key")
        if not key:
            return await send_error(interaction, "Your whitelist entry doesn't have a key on file. Contact a moderator.")

        try:
            script_text = await fetch_stored_script()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        try:
            script_text = inject_script_key(script_text, key)
        except ValueError as e:
            return await send_error(interaction, str(e))

        filename = "script.lua"
        file = discord.File(io.BytesIO(script_text.encode("utf-8")), filename=filename)

        # Components V2 File item, same pattern as /export's file_success_layout:
        # the message text goes in its own Container, and the attachment is
        # referenced separately via attachment://<filename> so it renders as
        # its own component beneath the text rather than a bare attachment.
        layout = LayoutView(timeout=None)
        layout.add_item(Container(
            TextDisplay("### 📜 Your Script"),
            TextDisplay("Here's your personalized script, keyed to your account. Keep it to yourself."),
            accent_color=discord.Color.blue(),
        ))
        layout.add_item(File(f"attachment://{filename}"))

        await interaction.followup.send(view=layout, file=file, ephemeral=True)

    async def get_role(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Same Contents-API whitelist check used by redeem_key/get_script --
        # avoids the raw CDN endpoint's caching lag (see fetch_users_with_sha
        # note above in redeem_key).
        try:
            users, _sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        entry = find_user_by_discord_id(users, str(interaction.user.id))
        if not entry:
            return await send_error(interaction, "You need to redeem a key before you can get your role.")

        role = interaction.guild.get_role(BUYER_ROLE_ID)
        if not role:
            return await send_error(interaction, "Buyer role not found. Set BUYER_ROLE_ID in bot_api.py to your Buyer role's ID.")

        if role in interaction.user.roles:
            return await send_error(interaction, f"You already have the {role.mention} role.")

        await interaction.user.add_roles(role, reason="Whitelisted user claimed Buyer role via control panel")
        await send_success(interaction, f"You've been given the {role.mention} role.")

    async def reset_hwid(self, interaction: discord.Interaction):
        # Best-effort pre-check: skip prompting for a new HWID if the user
        # isn't whitelisted or is still on cooldown. This has to stay fast --
        # send_modal (like send_message) must be the interaction's first
        # response, inside Discord's ~3 second ack window, so it reads from
        # bot_api's in-memory Users.json cache (kept warm by
        # refresh_users_cache_task) instead of hitting GitHub live. That
        # cache read can't time out or fail, so unlike the old
        # fetch_raw_users()-with-a-2s-timeout version of this check, there's
        # no path left that silently "fails open" into showing the modal to
        # someone who was never whitelisted.
        #
        # get_cached_users() can still be None very briefly right after a
        # bot restart, before the first refresh has landed -- in that one
        # window we fall back to opening the modal unconditionally, same as
        # redeem_key. ResetHWIDModal.on_submit() re-checks both whitelist
        # and cooldown against a fresh fetch regardless, so it remains the
        # single source of truth either way.
        users = get_cached_users()
        if users is None:
            return await interaction.response.send_modal(ResetHWIDModal())

        discord_id_str = str(interaction.user.id)
        entry = find_user_by_discord_id(users, discord_id_str)
        if not entry:
            return await send_error(interaction, "You need to redeem a key before you can reset your HWID.")

        remaining = hwid_reset_cooldown_remaining(entry)
        if remaining:
            return await send_error(interaction, f"You can reset your HWID again in {humanize_timeleft(remaining, suffix=False)}.")

        await interaction.response.send_modal(ResetHWIDModal())

    async def get_info(self, interaction: discord.Interaction):
        # Same lookup + embed as the /myinfo slash command, just triggered
        # from the panel button instead. Always ephemeral, and always a
        # fresh Contents-API fetch (same reasoning as get_script/get_role
        # above) rather than the in-memory cache, since this is a
        # user-facing info readout and should reflect the latest data.
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
        embed.add_field(name="Last HWID Reset", value=format_discord_timestamp(user_data.get("LastHwidReset")), inline=True)
        embed.add_field(name="Total HWID Resets", value=str(user_data.get("totalHwidResets", 0)), inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="createpanel", description="Posts the control panel in the panel channel.", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def createpanel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    channel = bot.get_channel(PANEL_CHANNEL_ID)
    if not channel:
        return await send_error(interaction, "Panel channel not found. Set PANEL_CHANNEL_ID in bot_api.py to your #panel channel's ID.")

    await channel.send(view=ControlPanelView())

    await send_success(interaction, f"Control panel posted in {channel.mention}.")

# // updatescript //

@bot.tree.command(name="updatescript", description="Updates the script /createpanel's Get Script button hands out.", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(script="New storedscript.lua contents. Must be exactly 2 lines: the script key line, then the loading line.")
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def updatescript(interaction: discord.Interaction, script: discord.Attachment):
    await interaction.response.defer(ephemeral=True)

    if not script.filename.lower().endswith(".lua"):
        return await send_error(interaction, "Please upload a valid `.lua` file.")

    try:
        raw_bytes = await script.read()
    except discord.HTTPException as e:
        return await send_error(interaction, f"Failed to download the uploaded file: {e}")

    try:
        script_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return await send_error(interaction, "That file isn't valid UTF-8 text.")

    # Enforces the exact 2-line shape (script key line, then loading line)
    # that Get Script's inject_script_key() depends on -- see
    # validate_stored_script() in bot_api.py. Catches a bad upload here
    # instead of it silently breaking every whitelisted user's Get Script
    # click afterward.
    error = validate_stored_script(script_text)
    if error:
        return await send_error(interaction, error)

    try:
        _current_text, sha = await fetch_stored_script_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    try:
        await commit_stored_script(script_text, sha, f"Update storedscript.lua by {interaction.user}")
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    await send_success(
        interaction,
        "`storedscript.lua` has been updated. **Get Script** will now hand out this version, keyed to each user.",
        fields=[("New Script", f"```lua\n{script_text}\n```", False)],
    )


# --- User Context Menu Commands ---
#
# 15 right-click (member list) commands mirroring the slash commands above,
# so the most common moderation/whitelist actions don't require typing out
# a slash command and looking up a user manually. Discord caps a single
# guild at 15 USER-type context menu commands, so this list is
# intentionally exactly that many.
#
# Each one below calls straight into the matching slash command's
# `.callback` (the plain async function discord.py stores on every
# app_commands.Command) so the actual logic -- validation, GitHub writes,
# DMs, etc. -- lives in exactly one place and can't drift between the two
# entry points. Slash commands that take extra arguments (reason, hwid,
# minutes, ...) can't be given those via a context menu click, so a small
# Modal collects them first and then forwards to the same `.callback`.

# // Ban User //

class BanContextModal(Modal):
    """Reason + optional duration + a Preserve Messages checkbox for the Ban
    User context menu command, mirroring /ban's `reason`/`duration`/
    `preserve_messages` options. Uses a Components V2 Checkbox (rather than
    a text field) for preserve_messages since it's a plain on/off toggle --
    defaults checked (messages preserved), matching /ban's own default."""

    reason = Label(
        text="Reason",
        component=TextInput(required=False, max_length=200, placeholder="None"),
    )
    duration = Label(
        text="Duration in minutes",
        description="Leave blank for a permanent ban.",
        component=TextInput(required=False, max_length=10, placeholder="e.g. 60"),
    )
    preserve_messages = Label(
        text="Preserve Messages",
        description="Checked = keep the user's messages. Unchecked = delete their recent messages.",
        component=Checkbox(default=True),
    )

    def __init__(self, target: discord.Member):
        super().__init__(title=f"Ban {target.display_name}"[:45])
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):
        reason = (self.reason.component.value or "").strip() or "None"
        duration_raw = (self.duration.component.value or "").strip()
        duration = None
        if duration_raw:
            if not duration_raw.isdigit() or int(duration_raw) <= 0:
                return await send_error(interaction, "Duration must be a positive whole number of minutes.")
            duration = int(duration_raw)
        preserve_messages = self.preserve_messages.component.value
        await ban.callback(interaction, self.target, reason=reason, duration=duration, preserve_messages=preserve_messages)


@bot.tree.context_menu(name="Ban User", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ctx_ban_user(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.send_modal(BanContextModal(target))

# // Kick User //

class KickContextModal(Modal):
    def __init__(self, target: discord.Member):
        super().__init__(title=f"Kick {target.display_name}"[:45])
        self.target = target
        self.reason = TextInput(label="Reason", required=False, max_length=200, placeholder="Unspecified")
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        reason = (self.reason.value or "").strip() or "Unspecified"
        await kick.callback(interaction, self.target, reason=reason)


@bot.tree.context_menu(name="Kick User", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ctx_kick_user(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.send_modal(KickContextModal(target))

# // Mute User //

class MuteContextModal(Modal):
    def __init__(self, target: discord.Member):
        super().__init__(title=f"Mute {target.display_name}"[:45])
        self.target = target
        self.reason = TextInput(label="Reason", required=False, max_length=200, placeholder="Unspecified")
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        reason = (self.reason.value or "").strip() or "Unspecified"
        await mute.callback(interaction, self.target, reason=reason)


@bot.tree.context_menu(name="Mute User", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ctx_mute_user(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.send_modal(MuteContextModal(target))

# // Unmute User //

@bot.tree.context_menu(name="Unmute User", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ctx_unmute_user(interaction: discord.Interaction, target: discord.Member):
    await unmute.callback(interaction, target)

# // Whitelist User //

@bot.tree.context_menu(name="Whitelist User", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ctx_whitelist_user(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.send_modal(WhitelistModal(target=target))

# // Edit User //

@bot.tree.context_menu(name="Edit User", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ctx_edit_user(interaction: discord.Interaction, target: discord.Member):
    # /edituser already just fetches the entry and opens a modal itself, so
    # there's no extra input to collect here first.
    await edituser.callback(interaction, target)

# // Unwhitelist User //

@bot.tree.context_menu(name="Unwhitelist User", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ctx_unwhitelist_user(interaction: discord.Interaction, target: discord.Member):
    await unwhitelist.callback(interaction, target)

# // Fetch User Info //

@bot.tree.context_menu(name="Fetch User Info", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ctx_fetch_user(interaction: discord.Interaction, target: discord.Member):
    await fetchuser.callback(interaction, target)

# // Check Temp Whitelist //

@bot.tree.context_menu(name="Check Temp Whitelist", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ctx_check_temp(interaction: discord.Interaction, target: discord.Member):
    await checktemp.callback(interaction, target)

# // Clear User Notes //

@bot.tree.context_menu(name="Clear User Notes", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ctx_clear_notes(interaction: discord.Interaction, target: discord.Member):
    await clearnotes.callback(interaction, target)

# // Force Reset HWID //

class ForceResetHwidContextModal(Modal):
    def __init__(self, target: discord.Member):
        super().__init__(title=f"Reset HWID: {target.display_name}"[:45])
        self.target = target
        self.hwid = TextInput(label="New HWID (SHA-256, 64 hex chars)", max_length=100, placeholder="64-character hex string")
        self.add_item(self.hwid)

    async def on_submit(self, interaction: discord.Interaction):
        # forceresethwid.callback already validates the HWID format and
        # reports a clear error itself, so it's passed straight through.
        await forceresethwid.callback(interaction, self.target, self.hwid.value.strip())


@bot.tree.context_menu(name="Force Reset HWID", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ctx_force_reset_hwid(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.send_modal(ForceResetHwidContextModal(target))

# // Reset HWID Cooldown //

@bot.tree.context_menu(name="Reset HWID Cooldown", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ctx_reset_hwid_cooldown(interaction: discord.Interaction, target: discord.Member):
    await resethwidcooldown.callback(interaction, target)

# // Toggle Bot Access //

@bot.tree.context_menu(name="Toggle Bot Access", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ctx_toggle_access(interaction: discord.Interaction, target: discord.Member):
    await toggleaccess.callback(interaction, target)

# // Grant Temp Bot Access //

class TempAccessContextModal(Modal):
    def __init__(self, target: discord.Member):
        super().__init__(title=f"Bot Access: {target.display_name}"[:45])
        self.target = target
        self.minutes = TextInput(label="Duration in minutes", max_length=10, placeholder="e.g. 30")
        self.add_item(self.minutes)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.minutes.value.strip()
        if not raw.isdigit() or int(raw) <= 0:
            return await send_error(interaction, "Duration must be a positive whole number of minutes.")
        await tempaccess.callback(interaction, self.target, int(raw))


@bot.tree.context_menu(name="Grant Temp Bot Access", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ctx_temp_access(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.send_modal(TempAccessContextModal(target))

# // Temp Whitelist User //

class TempWhitelistContextModal(Modal):
    def __init__(self, target: discord.Member):
        super().__init__(title=f"Temp Whitelist: {target.display_name}"[:45])
        self.target = target
        self.hwid = TextInput(label="HWID (SHA-256, 64 hex chars)", max_length=100, placeholder="64-character hex string")
        self.minutes = TextInput(label="Duration in minutes", max_length=10, placeholder="e.g. 60")
        self.add_item(self.hwid)
        self.add_item(self.minutes)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.minutes.value.strip()
        if not raw.isdigit() or int(raw) <= 0:
            return await send_error(interaction, "Duration must be a positive whole number of minutes.")
        await tempwhitelist.callback(interaction, self.target, self.hwid.value.strip(), int(raw))


@bot.tree.context_menu(name="Temp Whitelist User", guild=discord.Object(id=GUILD_ID))
@has_role(REQUIRED_ROLE_ID)
@is_in_guild(GUILD_ID)
async def ctx_temp_whitelist(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.send_modal(TempWhitelistContextModal(target))


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

    # Catch Discord's per-field "Must be X or fewer in length" HTTPException
    # (also error code 50035, Invalid Form Body) -- distinct from the
    # whole-embed 6000-character check above: this one fires when a single
    # field (an embed's description/title/a field value, or message content)
    # individually exceeds its own limit, e.g. /getkeys or /genkey building
    # a keys list that's short enough to pass the "under 6000 total" check
    # but still blows past a single embed description's own 4096 cap. The
    # inline-vs-file fallbacks those commands use are meant to avoid this in
    # the first place -- this is just the safety net for whatever slips
    # past that (or any other command that hits the same shape of error).
    if isinstance(original, discord.HTTPException) and "or fewer in length" in str(original):
        match = re.search(r"In ([\w.]+): Must be (\d+) or fewer in length", str(original))
        if match:
            field, limit = match.group(1), match.group(2)
            await send_error(
                interaction,
                f"That response was too long for Discord ({field} is limited to {limit} characters). "
                "Try narrowing your request so it returns less text.",
            )
        else:
            await send_error(
                interaction,
                "That response exceeded one of Discord's character limits. Try narrowing your request "
                "so it returns less text.",
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
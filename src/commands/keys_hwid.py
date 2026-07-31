import asyncio
import io
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import (
    has_role, is_in_guild, send_success, send_error, file_success_layout, resolve_user_option,
)
from commands.whitelist import whitelisted_user_autocomplete
from api.alerts import (
    send_alert, alert_embed,
    ALERT_COLOR_ADD, ALERT_COLOR_REMOVE, ALERT_COLOR_EDIT, ALERT_COLOR_TEMP, ALERT_COLOR_CAUTION,
)
from api.github import (
    GitHubAPIError, fetch_users_with_sha, commit_users,
    fetch_permitted_keys_with_sha, commit_permitted_keys,
    remove_permitted_key, remove_permitted_keys, remove_first_n_permitted_keys,
)
from api.users import find_user_by_discord_id, find_user_by_hwid, build_user_entry, remove_user_by_discord_id, revoke_buyer_role
from api.keys import generate_unique_key, generate_unique_keys, parse_key_length_range, is_valid_hwid
from api.time_utils import (
    format_join_date, format_discord_timestamp, format_expiration_note,
    parse_expiration_note, humanize_timeleft, hwid_reset_cooldown_remaining,
)

GUILD = discord.Object(id=config.GUILD_ID)

# Caps /genkey's bulk `amount` option. Mainly guards against an oversized
# permittedKeys.txt commit and against blowing well past what the inline
# embed / fallback file can reasonably display, not a security control.
MAX_BULK_GENKEY_AMOUNT = 100

# Tracks who currently has an active temp whitelist, so /tempwhitelist can
# reject a duplicate grant. Keyed by Discord ID string -> expiration datetime.
_active_temp_whitelists: dict = {}

# Tracks the running notify/removal task backing each active temp
# whitelist, keyed by Discord ID string. Letting /extend look this up is
# what makes it safe to push an expiration back: without cancelling the
# old task first, it would still fire (and remove the user) at the
# original, now-stale expiration time regardless of what the Notes field
# says.
_temp_whitelist_tasks: dict = {}


# =========================================================================
# /tempwhitelist, /checktemp, /extend, /forceresethwid, /resethwidcooldown
# implementations (standalone so context_menus.py can call them directly)
# =========================================================================

def _schedule_temp_whitelist_expiry(bot: commands.Bot, guild: Optional[discord.Guild], user: discord.User, discord_id: str, expiration_time: datetime):
    """
    (Re)schedules the background task that DMs a "5 minutes left" warning
    and then removes `discord_id` from the whitelist at `expiration_time`.

    Cancels whatever task was previously tracked for this user first, if
    any -- used both for a fresh /tempwhitelist grant and for /extend
    pushing an existing expiration back, so there's never more than one
    removal task racing against the current expiration, and an extension
    can't be silently undone by a task still counting down to the old time.
    """
    guild_name = guild.name if guild else "the server"
    expires_ts = int(expiration_time.timestamp())

    existing_task = _temp_whitelist_tasks.get(discord_id)
    if existing_task and not existing_task.done():
        existing_task.cancel()

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
                removed_entry = find_user_by_discord_id(current_whitelist, discord_id)
                current_whitelist, _ = remove_user_by_discord_id(current_whitelist, discord_id)
                await commit_users(current_whitelist, current_sha, f"Temp whitelist expired: {user.name} ({discord_id})")
            except GitHubAPIError:
                return

            await revoke_buyer_role(guild, discord_id)
            _active_temp_whitelists.pop(discord_id, None)
            _temp_whitelist_tasks.pop(discord_id, None)

            identifier = removed_entry.get("Identifier", user.name) if removed_entry else user.name
            await send_alert(bot, alert_embed(
                "⌛ Temp Whitelist Expired",
                f"{user.mention} (**{identifier}**)'s temporary whitelist expired and was auto-removed.",
                color=ALERT_COLOR_REMOVE,
            ))

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

    _active_temp_whitelists[discord_id] = expiration_time
    _temp_whitelist_tasks[discord_id] = asyncio.create_task(notify_and_remove())


async def _tempwhitelist_impl(interaction: discord.Interaction, user: discord.User, hwid: str, minutes: int):
    await interaction.response.defer(ephemeral=True)
    discord_id = str(user.id)

    if discord_id in _active_temp_whitelists:
        return await send_error(
            interaction,
            f"{user.mention} is already temporarily whitelisted until "
            f"{_active_temp_whitelists[discord_id].strftime('%Y-%m-%d %H:%M:%S UTC')}.",
        )

    try:
        whitelist_users, sha = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    if find_user_by_discord_id(whitelist_users, discord_id):
        return await send_error(interaction, f"{user.mention} is already in the whitelist.")

    existing_hwid = find_user_by_hwid(whitelist_users, hwid)
    if existing_hwid:
        return await send_error(interaction, f"This HWID is already whitelisted under **{existing_hwid.get('Identifier', 'Unknown')}** (<@{existing_hwid.get('DiscordId')}>).")

    expiration_time = datetime.now(timezone.utc) + timedelta(minutes=minutes)

    new_entry = build_user_entry(
        hwid, user.name, "Temp", discord_id, generate_unique_key(whitelist_users),
        notes=format_expiration_note(expiration_time),
    )
    whitelist_users.append(new_entry)

    try:
        await commit_users(whitelist_users, sha, f"Temp whitelist added: {user.name} ({discord_id}) for {minutes} minutes")
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    minute_label_alert = "minute" if minutes == 1 else "minutes"
    await send_alert(interaction.client, alert_embed(
        "⏳ Temp Whitelist Granted",
        f"{interaction.user.mention} temporarily whitelisted {user.mention} for {minutes} {minute_label_alert}.",
        color=ALERT_COLOR_TEMP,
        fields=[("HWID", f"||`{hwid}`||", False), ("Expires", f"<t:{int(expiration_time.timestamp())}:F>", False)],
    ))

    await send_success(
        interaction,
        f"Temporarily whitelisted {user.mention} for {minutes} minutes.",
        fields=[("HWID", f"||`{hwid}`||", False)],
    )

    guild_name = interaction.guild.name
    expires_ts = int(expiration_time.timestamp())
    minute_label = "minute" if minutes == 1 else "minutes"

    # DM the user an embed confirming their temporary whitelist, matching
    # the embed style used everywhere else in the bot.
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

    _schedule_temp_whitelist_expiry(interaction.client, interaction.guild, user, discord_id, expiration_time)


async def _extend_impl(interaction: discord.Interaction, user: discord.User, minutes: int):
    if minutes <= 0:
        return await send_error(interaction, "`minutes` must be a positive number.")

    await interaction.response.defer(ephemeral=True)
    discord_id = str(user.id)

    try:
        whitelist_users, sha = await fetch_users_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    entry = find_user_by_discord_id(whitelist_users, discord_id)
    if not entry:
        return await send_error(interaction, f"{user.mention} is not in the whitelist.")

    # Temp status is determined purely by whether Notes carries an
    # "Expires on ..." marker (see time_utils.format_expiration_note) --
    # same check /checktemp uses -- not by Rank or anything else, per spec.
    current_expiration = parse_expiration_note(entry.get("Notes"))
    if not current_expiration:
        return await send_error(
            interaction,
            f"{user.mention} isn't a temporary whitelist entry -- their Notes field doesn't carry an "
            "expiration marker, so there's nothing for `/extend` to push back. Use `/tempwhitelist` to "
            "grant a new temporary entry instead.",
            fields=[("Notes", entry.get("Notes") or "N/A", False)],
        )

    now = datetime.now(timezone.utc)
    if current_expiration <= now:
        return await send_error(
            interaction,
            f"{user.mention}'s temporary whitelist already expired on <t:{int(current_expiration.timestamp())}:F>. "
            "It should be removed automatically shortly, if it hasn't been already -- use `/tempwhitelist` to "
            "grant a new one instead.",
        )

    new_expiration = current_expiration + timedelta(minutes=minutes)
    entry["Notes"] = format_expiration_note(new_expiration)

    try:
        await commit_users(whitelist_users, sha, f"Extended temp whitelist: {entry.get('Identifier', discord_id)} ({discord_id}) by {minutes} minutes")
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    minute_label = "minute" if minutes == 1 else "minutes"

    await send_alert(interaction.client, alert_embed(
        "⏲️ Temp Whitelist Extended",
        f"{interaction.user.mention} extended {user.mention}'s (**{entry.get('Identifier', 'Unknown')}**) temporary whitelist by {minutes} {minute_label}.",
        color=ALERT_COLOR_TEMP,
        fields=[("New Expiry", f"<t:{int(new_expiration.timestamp())}:F>", False)],
    ))

    await send_success(
        interaction,
        f"Extended {user.mention}'s temporary whitelist by {minutes} {minute_label}.",
        fields=[
            ("Previous Expiry", f"<t:{int(current_expiration.timestamp())}:F>", True),
            ("New Expiry", f"<t:{int(new_expiration.timestamp())}:F>", True),
        ],
    )

    guild_name = interaction.guild.name if interaction.guild else "the server"
    expires_ts = int(new_expiration.timestamp())

    # DM the user, matching the style of the initial /tempwhitelist grant DM.
    try:
        extended_embed = discord.Embed(
            title=f"Your temporary whitelist in {guild_name} was extended",
            description=f"Your whitelist access to **{guild_name}** has been extended by {minutes} {minute_label}.",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        extended_embed.add_field(name="New Expiry", value=f"<t:{expires_ts}:F>\n<t:{expires_ts}:R>", inline=False)
        extended_embed.set_footer(text=f"Extended by: {interaction.user}")
        await user.send(embed=extended_embed)
    except Exception as e:
        print(f"Could not DM temp whitelist extension to {user}: {e}")

    # Cancels whatever task was still counting down to the *old* expiration
    # and replaces it with one for the new one -- without this, the old
    # task would still remove the user right on schedule, undoing the
    # extension just written to Notes/GitHub above.
    _schedule_temp_whitelist_expiry(interaction.client, interaction.guild, user, discord_id, new_expiration)


async def _checktemp_impl(interaction: discord.Interaction, user: discord.User):
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
        return await send_error(interaction, f"{user.mention} has no notes, which doesn't indicate anything about their whitelist status.")

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
    # the channel, so it's private to them. A true ephemeral response isn't
    # viable here: ephemeral/webhook messages can only be edited for ~15
    # minutes after the command was invoked, but this tracker may need to
    # keep editing itself for months. A DM has no such time limit.
    try:
        tracker_message = await interaction.user.send(embed=build_tracker_embed(now))
    except discord.Forbidden:
        return await send_error(
            interaction,
            "I couldn't DM you the tracker -- you likely have DMs from server "
            "members disabled for this server. Enable them and run the command again.",
        )

    try:
        await interaction.followup.send(embed=discord.Embed(
            description=f"Sent you a DM with {user.mention}'s live temporary whitelist tracker.",
            color=discord.Color.green(),
        ), ephemeral=True)
    except discord.HTTPException:
        pass

    async def update_loop():
        # Anchored to the event loop's monotonic clock so each tick is
        # scheduled relative to the *previous scheduled tick*, not to
        # "whenever the last edit happened to finish" -- keeps the
        # countdown from appearing to pause and then jump when an edit is
        # briefly rate-limited.
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
                    return  # Tracker message was deleted -- nothing left to update.
                except discord.Forbidden:
                    return
                except discord.HTTPException:
                    pass  # Transient (rate limit, etc.) -- just retry next tick.

                # Update more often as the deadline nears, so the "X left"
                # text stays accurate without hammering the API on long
                # (month/year) whitelists that would otherwise need
                # thousands of edits. The <=60s bucket ticks every 2s rather
                # than every 1s -- editing a message once a second sits
                # right at the edge of Discord's practical rate limit for
                # repeated edits, so 2s leaves headroom while still reading
                # as "live".
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


async def _forceresethwid_impl(interaction: discord.Interaction, user: discord.User, hwid: str):
    # Unlike /edituser's modal (capped at 5 components, leaving no room for
    # LastHwidReset/totalHwidResets inputs), this is a plain slash command,
    # so it can go ahead and bump those two fields itself -- same as a
    # self-service reset via the panel's "Reset HWID" button, just
    # admin-triggered and with the cooldown ignored entirely rather than
    # checked.
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

    await send_alert(interaction.client, alert_embed(
        "🛠️ HWID Force Reset",
        f"{interaction.user.mention} force-reset {user.mention}'s (**{entry.get('Identifier', 'Unknown')}**) HWID via `/forceresethwid`.",
        color=ALERT_COLOR_CAUTION,
        fields=[("Old HWID", f"||`{old_hwid}`||", True), ("New HWID", f"||`{hwid}`||", True)],
    ))

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


async def _resethwidcooldown_impl(interaction: discord.Interaction, user: discord.User):
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

    await send_alert(interaction.client, alert_embed(
        "⏱️ HWID Cooldown Cleared",
        f"{interaction.user.mention} cleared {user.mention}'s (**{entry.get('Identifier', 'Unknown')}**) HWID reset cooldown via `/resethwidcooldown`.",
        color=ALERT_COLOR_EDIT,
    ))

    await send_success(
        interaction,
        f"{user.mention}'s HWID reset cooldown has been cleared. They can now reset their own HWID via the control panel immediately.",
    )


class KeysHwid(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="genkey", description="Generates one or more unique, random keys.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(
        amount="How many keys to generate",
        allow_redemption="Commit the generated keys to permittedKeys.txt so they're redeemable via the control panel",
        length="Key length: a single number (e.g. 20) or a range (e.g. 5-10). Defaults to 25-40.",
    )
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def genkey(self, interaction: discord.Interaction, amount: int, allow_redemption: bool = False, length: Optional[str] = None):
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

        # Always cross-checked against both already-assigned Keys and
        # whatever's currently sitting in permittedKeys.txt.
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

        # Deliberately doesn't echo the actual key values here -- unlike the
        # ephemeral response below, this posts to a shared staff channel,
        # and there's no reason to widen a redeemable key's exposure beyond
        # whoever ran the command. Amount/length/commit-status live in
        # fields rather than the description line, matching this module's
        # "one description line + a couple of essential fields" pattern.
        await send_alert(interaction.client, alert_embed(
            "🔐 Keys Generated",
            f"{interaction.user.mention} generated key(s) via `/genkey`.",
            color=ALERT_COLOR_ADD,
            fields=[
                ("Amount", str(len(new_keys)), True),
                ("Length", f"{min_length}-{max_length}" if min_length != max_length else str(min_length), True),
                (
                    "Status",
                    "✅ Committed to permittedKeys.txt -- redeemable now"
                    if allow_redemption else
                    "❌ Not committed -- not yet redeemable",
                    False,
                ),
            ],
        ))

        footer_text = (
            "Committed to permittedKeys.txt -- redeemable now via the control panel."
            if allow_redemption else
            "Not committed -- not yet redeemable via the control panel."
        )

        keys_block = "\n".join(f"||`{k}`||" for k in new_keys)
        title = f"🔐 Generated {len(new_keys)} Key{'s' if len(new_keys) != 1 else ''}"

        # Spoiler-tagged inline text is nicer when it fits, but a large
        # amount/length combo can blow past Discord's message/embed limits,
        # so fall back to an attached file rather than truncating.
        if len(keys_block) <= 1800:
            embed = discord.Embed(title=title, description=keys_block, color=discord.Color.purple())
            embed.set_footer(text=footer_text)
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            filename = "SPOILER_generated_keys.txt"
            file = discord.File(io.BytesIO(("\n".join(new_keys) + "\n").encode()), filename=filename)
            layout = file_success_layout(f"**{title}**\n{footer_text}", filename)
            await interaction.followup.send(view=layout, file=file, ephemeral=True)

    @app_commands.command(name="getkeys", description="Displays every key currently available for redemption.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def getkeys(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            permitted_keys, _sha = await fetch_permitted_keys_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        if not permitted_keys:
            return await send_success(interaction, "No keys are currently available for redemption.")

        keys_block = "\n".join(f"||`{k}`||" for k in permitted_keys)
        title = f"🔑 {len(permitted_keys)} Available Key{'s' if len(permitted_keys) != 1 else ''}"

        if len(keys_block) <= 1800:
            embed = discord.Embed(title=title, description=keys_block, color=discord.Color.purple())
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            filename = "SPOILER_available_keys.txt"
            file = discord.File(io.BytesIO(("\n".join(permitted_keys) + "\n").encode()), filename=filename)
            layout = file_success_layout(f"**{title}**", filename)
            await interaction.followup.send(view=layout, file=file, ephemeral=True)

    @app_commands.command(name="clearkeys", description="Removes keys from permittedKeys.txt -- provide a list of keys, or a number to clear, not both.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(
        keys="Space/comma separated list of exact keys to remove",
        amount="Number of keys to remove (earliest entries first) -- use instead of `keys`, not with it",
    )
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def clearkeys(self, interaction: discord.Interaction, keys: Optional[str] = None, amount: Optional[int] = None):
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

        await send_alert(interaction.client, alert_embed(
            "🧹 Keys Cleared",
            f"{interaction.user.mention} cleared **{len(removed)}** key(s) from permittedKeys.txt via `/clearkeys`.",
            color=ALERT_COLOR_REMOVE,
            fields=[("Remaining", str(len(remaining)), True)],
        ))

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

    @app_commands.command(name="validatekey", description="Validates and returns the full information for a key including ownership.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(key="Key to validate")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def validatekey(self, interaction: discord.Interaction, key: str):
        await interaction.response.defer(ephemeral=True)

        try:
            users, _sha = await fetch_users_with_sha()
        except GitHubAPIError as e:
            return await send_error(interaction, str(e))

        entry = next((user for user in users if user.get("Key") == key), None)
        if not entry:
            return await send_error(interaction, "Invalid key. No match found.")

        embed = discord.Embed(title="Valid Key", description=f"**The info for key:** ||`{key}`||", color=discord.Color.green())
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

    @app_commands.command(name="tempwhitelist", description="Temporarily whitelists a user for x minutes.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="User to whitelist temporarily", hwid="Hashed HWID in SHA-256", minutes="Duration in minutes")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def tempwhitelist(self, interaction: discord.Interaction, user: discord.User, hwid: str, minutes: int):
        await _tempwhitelist_impl(interaction, user, hwid, minutes)

    @app_commands.command(name="checktemp", description="Checks a user's temporary whitelist status (via their Notes field) with a live countdown.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="Discord user to check -- start typing an ID or name to search.")
    @app_commands.autocomplete(user=whitelisted_user_autocomplete)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def checktemp(self, interaction: discord.Interaction, user: str):
        resolved = await resolve_user_option(interaction, user)
        if resolved is None:
            return
        await _checktemp_impl(interaction, resolved)

    @app_commands.command(name="extend", description="Extends an existing temporary whitelist by x minutes, instead of re-running /tempwhitelist.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="Temporarily whitelisted user to extend -- start typing an ID or name to search.", minutes="How many minutes to add to their current expiration")
    @app_commands.autocomplete(user=whitelisted_user_autocomplete)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def extend(self, interaction: discord.Interaction, user: str, minutes: int):
        resolved = await resolve_user_option(interaction, user)
        if resolved is None:
            return
        await _extend_impl(interaction, resolved, minutes)

    @app_commands.command(name="forceresethwid", description="Forcefully sets a whitelisted user's HWID, bypassing their reset cooldown.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="The whitelisted user whose HWID to force-reset -- start typing an ID or name to search.", hwid="The user's new HWID, pre-hashed in SHA-256 (64 hex characters).")
    @app_commands.autocomplete(user=whitelisted_user_autocomplete)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def forceresethwid(self, interaction: discord.Interaction, user: str, hwid: str):
        resolved = await resolve_user_option(interaction, user)
        if resolved is None:
            return
        await _forceresethwid_impl(interaction, resolved, hwid)

    @app_commands.command(name="resethwidcooldown", description="Clears a user's HWID reset cooldown so they can reset their own HWID again immediately.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="The whitelisted user whose HWID reset cooldown to clear -- start typing an ID or name to search.")
    @app_commands.autocomplete(user=whitelisted_user_autocomplete)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def resethwidcooldown(self, interaction: discord.Interaction, user: str):
        resolved = await resolve_user_option(interaction, user)
        if resolved is None:
            return
        await _resethwidcooldown_impl(interaction, resolved)


async def setup(bot: commands.Bot):
    await bot.add_cog(KeysHwid(bot))

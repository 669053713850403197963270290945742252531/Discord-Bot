"""License-key administration backed by Supabase."""

from datetime import datetime, timezone, timedelta
import asyncio
from typing import Optional

import discord
import secrets
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, send_success, send_error, resolve_user_option
from api.alerts import send_alert, alert_embed, ALERT_COLOR_ADD, ALERT_COLOR_EDIT
from api.keys import generate_key, parse_key_length_range, parse_game_ids
from api.supabase_db import (
    fetch_users, fetch_redeemable_keys, create_redeemable_key, delete_redeemable_key,
    is_redeemable_key, get_license_by_key, get_license_by_discord_id, get_license_by_identifier, update_license, clear_hwid_reset_cooldown,
    create_license, delete_license,
)
from api.time_utils import humanize_timeleft, format_discord_timestamp
from api.users import revoke_buyer_role

GUILD = discord.Object(id=config.GUILD_ID)


def _games_text(games):
    return "All games" if "*" in (games or []) else ", ".join(games or []) or "No games assigned"


def _pending_identifier(key: str) -> str:
    return f"__pending__:{key}"


def _generate_key(existing: set[str], min_length=25, max_length=40) -> str:
    key = generate_key(min_length, max_length)
    while key in existing:
        key = generate_key(min_length, max_length)
    return key


async def _redeem_pending_key(key: str, discord_id: str, identifier: str):
    from api.supabase_db import redeem_license
    return await redeem_license(key, discord_id, identifier, "User")


async def _tempwhitelist_impl(interaction, user: discord.User, minutes: int, games_value: str = "*"):
    if minutes <= 0:
        return await send_error(interaction, "`minutes` must be positive.")
    try:
        games = parse_game_ids(games_value)
    except ValueError as e:
        return await send_error(interaction, str(e))
    existing = await get_license_by_discord_id(str(user.id))
    if existing:
        return await send_error(interaction, f"{user.mention} is already licensed.")
    users = await fetch_users()
    used = {str(u.get("Key")) for u in users if u.get("Key")}
    key = _generate_key(used)
    identifier = str(user.name).strip()
    if not identifier:
        return await send_error(interaction, "Could not determine the user's Discord name.")
    existing_identifier = await get_license_by_identifier(identifier)
    if existing_identifier:
        return await send_error(interaction, f"The Discord name `{identifier}` is already used by another license.")
    expiry = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    record = {
        "Identifier": identifier, "DiscordId": str(user.id), "Rank": "Temp", "Key": key,
        "Activated": None, "Executions": 0, "Notes": None, "Games": games,
        "Enabled": True, "ExpiresAt": expiry.isoformat(),
    }
    try:
        await create_license(record)
    except Exception as e:
        return await send_error(interaction, f"Failed to create temporary license: {e}")
    asyncio.create_task(_expire_temp_license_after_delay(identifier, expiry))
    await send_success(interaction, f"Granted {user.mention} temporary access until <t:{int(expiry.timestamp())}:F>.", fields=[
        ("License Key", f"||`{key}`||", False), ("Games", _games_text(games), False),
    ])


async def _expire_temp_license_after_delay(identifier: str, expiry: datetime):
    """Delete a temporary license when its expiry time is reached."""
    delay = (expiry - datetime.now(timezone.utc)).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        entry = await get_license_by_identifier(identifier)
        if not entry or str(entry.get("Rank") or "").strip().lower() != "temp":
            return
        raw_expiry = entry.get("ExpiresAt")
        try:
            current_expiry = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00")) if raw_expiry else None
        except ValueError:
            current_expiry = None
        if current_expiry and current_expiry > datetime.now(timezone.utc):
            return
        await delete_license(identifier)
    except Exception as exc:
        print(f"Failed to expire temporary license {identifier!r}: {exc}")


async def _checktemp_impl(interaction, user):
    """Show a live temporary-whitelist tracker in the invoker's DMs."""
    await interaction.response.defer(ephemeral=True)

    entry = await get_license_by_discord_id(str(user.id))
    if not entry:
        return await send_error(interaction, f"{user.mention} is not in the whitelist.")

    if str(entry.get("Rank") or "").strip().lower() != "temp":
        return await send_error(interaction, f"{user.mention} does not have a temporary whitelist.")

    def get_current_expiration(current_entry):
        raw_expiration = current_entry.get("ExpiresAt")
        if raw_expiration:
            try:
                parsed = datetime.fromisoformat(str(raw_expiration).replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return None

    expiration_time = get_current_expiration(entry)
    if not expiration_time:
        return await send_error(
            interaction,
            f"{user.mention}'s temporary whitelist does not have a valid expiration time.",
        )

    now = datetime.now(timezone.utc)
    if (expiration_time - now).total_seconds() <= 0:
        return await send_error(
            interaction,
            f"{user.mention}'s temporary whitelist already expired on <t:{int(expiration_time.timestamp())}:F>.",
        )

    def build_tracker_embed(current_entry, current_expiration, now_):
        remaining_ = current_expiration - now_
        expires_ts = int(current_expiration.timestamp())

        fields = [
            ("Identifier", current_entry.get("Identifier"), True),
            ("Rank", current_entry.get("Rank"), True),
            ("Discord ID", f"{current_entry.get('DiscordId')} ({user.mention})", True),
            ("HWID", f"||`{current_entry.get('HWID')}`||" if current_entry.get("HWID") else "N/A", True),
            ("Key", f"||`{current_entry.get('Key')}`||" if current_entry.get("Key") else "N/A", True),
            ("Activated", format_discord_timestamp(current_entry.get("Activated"), "F"), True),
            ("Last HWID Reset", format_discord_timestamp(current_entry.get("LastHwidReset")), True),
            ("Total HWID Resets", f"`{current_entry.get('totalHwidResets', 0)}`", True),
            ("Expires", f"<t:{expires_ts}:F>", True),
            ("Time Left", humanize_timeleft(remaining_), True),
        ]

        embed = discord.Embed(
            title=f"Temporary Whitelist: {current_entry.get('Identifier', user.name)}",
            color=discord.Color.gold(),
            timestamp=now_,
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        for name, value, inline in fields:
            embed.add_field(name=name, value=value or "N/A", inline=inline)
        embed.set_footer(text="Live countdown • updates automatically until expiry or extension")
        return embed

    try:
        tracker_message = await interaction.user.send(embed=build_tracker_embed(entry, expiration_time, now))
    except discord.Forbidden:
        return await send_error(
            interaction,
            "I couldn't DM you the tracker -- you likely have DMs from server members disabled for this server. Enable them and run the command again.",
        )

    try:
        await interaction.followup.send(
            embed=discord.Embed(
                description=f"Sent you a DM with {user.mention}'s live temporary whitelist tracker.",
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )
    except discord.HTTPException:
        pass

    async def update_loop():
        loop_clock = asyncio.get_running_loop()
        next_tick = loop_clock.time()
        last_expiration_ts = int(expiration_time.timestamp())

        try:
            while True:
                now_ = datetime.now(timezone.utc)
                current_entry = await get_license_by_discord_id(str(user.id))

                # The temporary license may have expired or been deleted.
                if not current_entry or str(current_entry.get("Rank") or "").strip().lower() != "temp":
                    expired_embed = discord.Embed(
                        title=f"Temporary Whitelist Expired: {entry.get('Identifier', user.name)}",
                        description=f"{user.mention}'s temporary whitelist is no longer active.",
                        color=discord.Color.red(),
                        timestamp=now_,
                    )
                    expired_embed.set_footer(text="This tracker is no longer updating.")
                    try:
                        await tracker_message.edit(embed=expired_embed)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                    return

                current_expiration = get_current_expiration(current_entry)
                if current_expiration is None:
                    error_embed = discord.Embed(
                        title=f"Temporary Whitelist: {current_entry.get('Identifier', user.name)}",
                        description="The temporary whitelist no longer has a valid expiration time.",
                        color=discord.Color.red(),
                        timestamp=now_,
                    )
                    try:
                        await tracker_message.edit(embed=error_embed)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                    return

                remaining_seconds = (current_expiration - now_).total_seconds()

                if remaining_seconds <= 0:
                    expired_embed = discord.Embed(
                        title=f"Temporary Whitelist Expired: {current_entry.get('Identifier', user.name)}",
                        description=f"{user.mention}'s temporary whitelist expired on <t:{int(current_expiration.timestamp())}:F>.",
                        color=discord.Color.red(),
                        timestamp=now_,
                    )
                    expired_embed.set_footer(text="This tracker is no longer updating.")
                    try:
                        await tracker_message.edit(embed=expired_embed)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                    return

                # Recalculate the schedule whenever Extend/Edit User changes expiry.
                current_expiration_ts = int(current_expiration.timestamp())
                if current_expiration_ts != last_expiration_ts:
                    next_tick = loop_clock.time()
                    last_expiration_ts = current_expiration_ts

                try:
                    await tracker_message.edit(
                        embed=build_tracker_embed(current_entry, current_expiration, now_)
                    )
                except discord.NotFound:
                    return
                except discord.Forbidden:
                    return
                except discord.HTTPException:
                    pass

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
                await asyncio.sleep(max(0, next_tick - loop_clock.time()))
        except asyncio.CancelledError:
            pass

    asyncio.create_task(update_loop())


async def _extend_impl(interaction, user, minutes: int):
    if minutes == 0:
        return await send_error(interaction, "`minutes` cannot be 0. Use a positive value to extend or a negative value to decrease the expiration.")
    entry = await get_license_by_discord_id(str(user.id))
    if not entry:
        return await send_error(interaction, f"{user.mention} is not licensed.")
    if str(entry.get("Rank") or "").strip().lower() != "temp":
        return await send_error(interaction, "That license is not an active temporary license.")
    raw_expiry = entry.get("ExpiresAt")
    try:
        expiry = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00")) if raw_expiry else None
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
    except ValueError:
        expiry = None
    now = datetime.now(timezone.utc)
    if not expiry or expiry <= now:
        return await send_error(interaction, "That license is not an active temporary license.")

    new_expiry = expiry + timedelta(minutes=minutes)

    try:
        if new_expiry <= now:
            await delete_license(entry["Identifier"])
            return await send_success(
                interaction,
                f"{user.mention}'s temporary access was decreased past the current time and has expired.",
                fields=[("New Expiry", f"<t:{int(new_expiry.timestamp())}:F>", False)],
            )

        await update_license(
            entry["Identifier"],
            expires_at=new_expiry.isoformat(),
        )
    except Exception as e:
        return await send_error(interaction, f"Failed to update temporary license: {e}")

    # Start a new expiry watcher for a shortened expiration. The existing watcher
    # remains harmless because it re-reads the current expiration before deleting.
    asyncio.create_task(_expire_temp_license_after_delay(entry["Identifier"], new_expiry))

    action = "extended" if minutes > 0 else "decreased"
    await send_success(
        interaction,
        f"{user.mention}'s temporary access was {action} by {abs(minutes)} minute(s).",
        fields=[("New Expiry", f"<t:{int(new_expiry.timestamp())}:F>", False)],
    )


class Keys(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @app_commands.command(name="tempwhitelist", description="Temporarily licenses a user for a number of minutes.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="User", minutes="Duration in minutes", games="Comma-separated PlaceIds; use * for all")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def tempwhitelist(self, interaction, user: discord.User, minutes: int, games: str = "*"):
        await _tempwhitelist_impl(interaction, user, minutes, games)

    @app_commands.command(name="checktemp", description="Checks temporary license status.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="User to check")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def checktemp(self, interaction, user: discord.Member):
        await _checktemp_impl(interaction, user)

    @app_commands.command(name="extend", description="Extends a temporary license.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="User", minutes="Minutes to add")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def extend(self, interaction, user: discord.Member, minutes: int):
        await _extend_impl(interaction, user, minutes)

    # Guild restriction goes on the group itself -- per discord.py, a group's
    # subcommands can't carry their own @app_commands.guilds(...) -- so
    # decorating the group here makes every child (generate/games/validate/
    # fetch/clear) inherit it, matching the pattern used by every other
    # grouped cog (ciphers.py, qrcode.py, warnings.py, etc.).
    key_group = app_commands.guilds(GUILD)(
        app_commands.Group(name="key", description="License-key administration.")
    )

    user_group = app_commands.guilds(GUILD)(
        app_commands.Group(name="user", description="Manage a licensed user.")
    )

    async def _set_user_enabled(self, interaction: discord.Interaction, user: discord.Member, enabled: bool):
        if config.REQUIRED_ROLE_ID not in [r.id for r in getattr(interaction.user, "roles", [])]:
            return await send_error(interaction, "You do not have permission.")

        entry = await get_license_by_discord_id(str(user.id))
        if not entry:
            return await send_error(interaction, f"{user.mention} is not licensed.")

        currently_enabled = bool(entry.get("Enabled", True))
        if currently_enabled == enabled:
            state = "enabled" if enabled else "disabled"
            return await send_error(interaction, f"{user.mention}'s whitelist is already {state}.")

        try:
            updated = await update_license(entry["Identifier"], enabled=enabled)
        except Exception as exc:
            state = "enable" if enabled else "disable"
            return await send_error(interaction, f"Failed to {state} {user.mention}'s whitelist: {exc}")

        if not updated:
            return await send_error(interaction, "The license could not be updated.")

        state = "enabled" if enabled else "disabled"
        await send_success(
            interaction,
            f"{user.mention}'s whitelist has been **{state}**.",
            title=f"Whitelist {state.title()}",
            fields=[
                ("Identifier", f"`{updated.get('Identifier')}`", True),
                ("License Enabled", "Yes ✅" if enabled else "No ❌", True),
            ],
        )

    @user_group.command(name="enable", description="Enable a licensed user's whitelist entry.")
    @app_commands.describe(user="Licensed user to enable")
    async def user_enable(self, interaction: discord.Interaction, user: discord.Member):
        await self._set_user_enabled(interaction, user, True)

    @user_group.command(name="disable", description="Disable a licensed user's whitelist entry.")
    @app_commands.describe(user="Licensed user to disable")
    async def user_disable(self, interaction: discord.Interaction, user: discord.Member):
        await self._set_user_enabled(interaction, user, False)

    @key_group.command(name="generate", description="Generate unredeemed license keys.")
    @app_commands.describe(amount="Number of keys", length="Fixed length or range, e.g. 25 or 25-32")
    async def key_generate(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100], length: str = "25-40"):
        if config.REQUIRED_ROLE_ID not in [r.id for r in getattr(interaction.user, "roles", [])]:
            return await send_error(interaction, "You do not have permission to generate keys.")
        try:
            min_len, max_len = parse_key_length_range(length)
        except ValueError as e:
            return await send_error(interaction, str(e))

        redeemable = await fetch_redeemable_keys()
        users = await fetch_users()
        used = {str(u.get("Key")) for u in users if u.get("Key")} | set(redeemable)
        generated = []
        try:
            for _ in range(amount):
                key = _generate_key(used, min_len, max_len)
                used.add(key)
                await create_redeemable_key(key)
                generated.append(key)
        except Exception as e:
            return await send_error(interaction, f"Failed to generate keys: {e}")

        await send_success(
            interaction,
            f"Generated {len(generated)} unredeemed license key(s).",
            title="License Keys Generated",
            fields=[
                ("Amount", f"`{len(generated)}`", True),
                ("Keys", "```\n" + "\n".join(generated) + "\n```", False),
            ],
        )


    @key_group.command(name="validate", description="Validate a license key and show its status.")
    @app_commands.describe(key="License key to validate")
    async def key_validate(self, interaction, key: str):
        if config.REQUIRED_ROLE_ID not in [r.id for r in getattr(interaction.user, "roles", [])]: return await send_error(interaction, "You do not have permission.")
        normalized = key.strip()
        redeemable = await is_redeemable_key(normalized)
        entry = await get_license_by_key(normalized)
        if not redeemable and not entry:
            return await send_error(interaction, "License key not found.")
        if redeemable and entry and not entry.get("DiscordId"):
            status = "Unredeemed"
        elif entry and entry.get("DiscordId"):
            status = "Redeemed"
        else:
            status = "Unredeemed"
        await send_success(interaction, "License key found.", fields=[
            ("Status", status, True),
            ("Identifier", entry.get("Identifier") if entry else "Not assigned", True),
            ("Discord ID", entry.get("DiscordId") if entry else "Unredeemed", True),
            ("Games", _games_text(entry.get("Games")) if entry else "All games", False),
            ("Activated", format_discord_timestamp(entry.get("Activated")) if entry else "Never", True),
            ("Executions", str(entry.get("Executions", 0)) if entry else "0", True),
        ])

    @key_group.command(name="fetch", description="List unredeemed license keys.")
    @app_commands.describe(amount="Number of unredeemed license keys to fetch")
    async def key_fetch(self, interaction, amount: app_commands.Range[int, 1, 100] = 1):
        if config.REQUIRED_ROLE_ID not in [r.id for r in getattr(interaction.user, "roles", [])]: return await send_error(interaction, "You do not have permission.")
        available_keys = await fetch_redeemable_keys()
        if len(available_keys) <= amount:
            keys = available_keys
        else:
            keys = secrets.SystemRandom().sample(available_keys, amount)
        if not keys:
            return await send_error(interaction, "No unredeemed license keys are available.")

        await send_success(
            interaction,
            f"Fetched {len(keys)} unredeemed license key(s).",
            title="Unredeemed License Keys",
            fields=[
                ("Amount", f"`{len(keys)}`", True),
                ("Keys", "```\n" + "\n".join(keys) + "\n```", False),
            ],
        )

    @key_group.command(name="clear", description="Delete one or more unredeemed license keys.")
    @app_commands.describe(key="License key(s) to delete, separated by commas")
    async def key_clear(self, interaction, key: str):
        if config.REQUIRED_ROLE_ID not in [r.id for r in getattr(interaction.user, "roles", [])]:
            return await send_error(interaction, "You do not have permission.")

        keys = []
        seen = set()
        for raw_key in key.split(","):
            normalized = raw_key.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                keys.append(normalized)

        if not keys:
            return await send_error(interaction, "Please provide at least one license key.")

        removed = []
        not_found = []
        try:
            for normalized in keys:
                if not await is_redeemable_key(normalized):
                    not_found.append(normalized)
                    continue
                if await delete_redeemable_key(normalized):
                    removed.append(normalized)
                else:
                    not_found.append(normalized)
        except Exception as e:
            return await send_error(interaction, f"Failed to delete license keys: {e}")

        if not removed:
            return await send_error(interaction, "None of the provided license keys were found or they have already been redeemed.")

        fields = [("Deleted", f"`{len(removed)}`", True)]
        if not_found:
            fields.append(("Not Found / Already Redeemed", "\n".join(f"`{item}`" for item in not_found), False))

        await send_success(
            interaction,
            f"Deleted {len(removed)} unredeemed license key(s).",
            fields=fields,
        )

    @app_commands.command(name="resethwidcooldown", description="Clear a user's HWID reset cooldown so they can reset their HWID again immediately.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="The whitelisted user whose HWID reset cooldown should be cleared")
    async def resethwidcooldown(self, interaction: discord.Interaction, user: discord.Member):
        if config.REQUIRED_ROLE_ID not in [r.id for r in getattr(interaction.user, "roles", [])]:
            return await send_error(interaction, "You do not have permission.")
        resolved = user
        entry = await get_license_by_discord_id(str(resolved.id))
        if not entry:
            return await send_error(interaction, f"{resolved.mention} is not licensed.")
        if not entry.get("LastHwidReset"):
            return await send_error(interaction, f"{resolved.mention} does not currently have an HWID reset cooldown.")
        try:
            updated = await clear_hwid_reset_cooldown(str(entry["Identifier"]))
        except Exception as exc:
            return await send_error(interaction, f"Failed to clear HWID reset cooldown: {exc}")
        if not updated:
            return await send_error(interaction, "The license could not be updated.")
        await send_success(interaction, f"{resolved.mention}'s HWID reset cooldown has been cleared. They can reset their HWID again immediately.")


async def reconcile_temp_whitelists(bot):
    """Remove expired temporary license rows and reschedule live expirations."""
    now = datetime.now(timezone.utc)
    removed = 0
    scheduled = 0
    for entry in await fetch_users():
        if str(entry.get("Rank") or "").strip().lower() != "temp":
            continue
        identifier = str(entry.get("Identifier") or "").strip()
        if not identifier:
            continue

        raw_expiry = entry.get("ExpiresAt")
        try:
            expiry = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00")) if raw_expiry else None
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except ValueError:
            expiry = None

        if expiry is None or expiry <= now:
            try:
                if await delete_license(identifier):
                    removed += 1
            except Exception as exc:
                print(f"Failed to remove expired temporary license {identifier!r}: {exc}")
            continue

        asyncio.create_task(_expire_temp_license_after_delay(identifier, expiry))
        scheduled += 1

    if removed or scheduled:
        print(f"Temporary whitelist reconciliation: removed={removed}, scheduled={scheduled}")
    return removed


async def setup(bot):
    cog = Keys(bot)
    await bot.add_cog(cog)
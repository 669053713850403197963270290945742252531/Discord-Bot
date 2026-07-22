import asyncio
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import (
    has_role, is_in_guild, can_moderate, notify_user,
    send_success, send_error, edit_or_send_error, error_embed, success_embed,
)

GUILD = discord.Object(id=config.GUILD_ID)


# =========================================================================
# Implementations (standalone functions so context_menus.py can call them
# directly, without needing a bound cog instance)
# =========================================================================

async def _ban_impl(interaction: discord.Interaction, target: discord.User, reason: str = "None", duration: int = None, preserve_messages: bool = True):
    try:
        await interaction.response.send_message(f"Processing ban for {target.mention}...", ephemeral=True)

        member = interaction.guild.get_member(target.id)

        # Only run moderation checks and message deletion for members
        if member:
            await can_moderate(interaction, member)

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

            if not preserve_messages:
                print(f"Deleting messages for {member}...")
                for channel in interaction.guild.text_channels:
                    try:
                        async for msg in channel.history(limit=1000):
                            if msg.author == member:
                                await msg.delete()
                    except discord.Forbidden:
                        print(f"Missing permissions to delete messages in {channel.name}")
                    except Exception as e:
                        print(f"Error deleting messages in {channel.name}: {e}")
        else:
            # Banning globally (user isn't a current member)
            try:
                await notify_user(target, "banned", interaction.user, reason, interaction.guild.name)
            except Exception as e:
                print(f"Failed to dm {target}: {e}")
            print(f"{target} was not found in server. Moderation checks and message deletion have been skipped.")

        await interaction.guild.ban(target, reason=reason, delete_message_seconds=0 if preserve_messages else 86400)

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

        if duration:
            async def unban_later():
                await asyncio.sleep(duration * 60)
                try:
                    await interaction.guild.unban(target, reason="Temporary ban expired")
                except Exception as e:
                    print(f"Failed to unban {target}: {e}")

            interaction.client.loop.create_task(unban_later())

    except app_commands.CheckFailure as e:
        await edit_or_send_error(interaction, str(e))
    except discord.Forbidden:
        await edit_or_send_error(interaction, "Missing permissions to ban.")
    except Exception as e:
        await edit_or_send_error(interaction, str(e))


async def _kick_impl(interaction: discord.Interaction, target: discord.Member, reason: str = "Unspecified"):
    try:
        await can_moderate(interaction, target)
        await notify_user(target, "kicked", interaction.user, reason, interaction.guild.name)
        await target.kick(reason=reason)
        await send_success(interaction, f"{target.mention} has been kicked.", fields=[("Reason", reason, False)])
    except app_commands.CheckFailure as e:
        await send_error(interaction, str(e))
    except discord.Forbidden:
        await send_error(interaction, "Missing permissions to kick.")
    except Exception as e:
        await send_error(interaction, f"Failed to kick: {e}")


_MUTE_ALLOWED_PERMS = {
    "view_channel",
    "manage_channels",
    "manage_permissions",
    "manage_webhooks",
    "create_instant_invite",
}

_MUTE_ALL_CHANNEL_PERMS = [
    "add_reactions", "attach_files", "connect", "create_instant_invite", "deafen_members",
    "embed_links", "external_emojis", "manage_channels", "manage_messages", "manage_permissions",
    "manage_webhooks", "mention_everyone", "move_members", "mute_members", "priority_speaker",
    "read_message_history", "send_messages", "send_tts_messages", "speak", "stream",
    "use_external_emojis", "view_channel", "create_public_threads", "create_private_threads",
    "send_messages_in_threads", "use_external_stickers", "send_voice_messages", "create_polls",
]


async def _mute_impl(interaction: discord.Interaction, target: discord.Member, reason: str = "Unspecified"):
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

        # Overwrite permissions on every channel to accommodate the muted role
        for channel in guild.channels:
            overwrite = channel.overwrites_for(muted_role)
            for perm_name in _MUTE_ALL_CHANNEL_PERMS:
                if perm_name not in _MUTE_ALLOWED_PERMS:
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


async def _unmute_impl(interaction: discord.Interaction, target: discord.Member, reason: str = "No reason provided"):
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


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ban", description="Bans a user from the server, delete their recent messages?, specify a temporary ban duration?")
    @app_commands.guilds(GUILD)
    @app_commands.describe(target="User to ban", reason="Ban reason", duration="Ban duration in minutes", preserve_messages="Keep the user's messages?")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def ban(self, interaction: discord.Interaction, target: discord.User, reason: str = "None", duration: int = None, preserve_messages: bool = True):
        await _ban_impl(interaction, target, reason, duration, preserve_messages)

    @app_commands.command(name="checkban", description="Returns if the user is banned from the server.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="User to check the ban status of")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def checkban(self, interaction: discord.Interaction, user: discord.User):
        try:
            await interaction.response.defer(ephemeral=True)

            async for ban_entry in interaction.guild.bans(limit=None):
                if ban_entry.user.id == user.id:
                    reason = ban_entry.reason or "No reason provided"
                    embed = error_embed(
                        title="User is Banned",
                        fields=[("User", f"{user} (`{user.id}`)", False), ("Reason", reason, False)],
                    )
                    return await interaction.followup.send(embed=embed, ephemeral=True)

            await send_success(interaction, f"{user.mention} is not currently banned from this server.")

        except discord.Forbidden:
            await send_error(interaction, "I don't have permission to view bans.")
        except Exception as e:
            await send_error(interaction, f"Error while checking ban: `{e}`")

    @app_commands.command(name="unban", description="Unbans a user from the server.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="The user to unban")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def unban(self, interaction: discord.Interaction, user: discord.User):
        try:
            bans = [ban async for ban in interaction.guild.bans()]
            banned_entry = discord.utils.find(lambda b: b.user.id == user.id, bans)

            if not banned_entry:
                await send_error(interaction, "User is not banned.")
                return

            await interaction.guild.unban(banned_entry.user, reason=f"Unbanned by {interaction.user}")
            await send_success(interaction, f"Successfully unbanned {user.mention}.")

        except discord.Forbidden:
            await send_error(interaction, "Missing permissions to unban.")
        except Exception as e:
            await send_error(interaction, str(e))

    @app_commands.command(name="purge", description="Deletes the specified amount of messages in the current channel.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(amount="Number of messages to delete")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def purge(self, interaction: discord.Interaction, amount: int):
        if amount < 1 or amount > 100:
            await interaction.response.defer(ephemeral=True)
            return

        try:
            await interaction.response.defer(thinking=False, ephemeral=True)
            await interaction.channel.purge(limit=amount)

            # Delete the deferred response so it looks like nothing happened
            try:
                await interaction.delete_original_response()
            except discord.NotFound:
                pass
        except discord.Forbidden:
            pass

    @app_commands.command(name="kick", description="Kicks a member from the server.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(target="Member to kick", reason="Reason for kick")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def kick(self, interaction: discord.Interaction, target: discord.Member, reason: str = "Unspecified"):
        await _kick_impl(interaction, target, reason)

    @app_commands.command(name="mute", description="Mutes a member from all channels.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(target="Member to mute", reason="Reason for the mute")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def mute(self, interaction: discord.Interaction, target: discord.Member, reason: str = "Unspecified"):
        await _mute_impl(interaction, target, reason)

    @app_commands.command(name="unmute", description="Unmutes a member from all channels.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(target="Member to unmute", reason="Reason for the unmute")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def unmute(self, interaction: discord.Interaction, target: discord.Member, reason: str = "No reason provided"):
        await _unmute_impl(interaction, target, reason)

    @app_commands.command(name="dm", description="Sends a direct message to a user.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(target="User to direct message", message="Message to send")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def dm(self, interaction: discord.Interaction, target: discord.User, message: str):
        try:
            await target.send(message)
            await send_success(interaction, f"Sent message to {target.mention}.")
        except discord.Forbidden as e:
            if e.code == 50007:
                await send_error(interaction, f"Failed to dm {target.mention}. They may have dms disabled, or you're not connected through a shared server or friendship.")
            else:
                await send_error(interaction, f"Failed to dm: {e}")
        except discord.HTTPException as e:
            if e.status == 400 and e.code == 50007:
                await send_error(interaction, f"Cannot DM {target.mention}. The user may have DMs disabled or has blocked the bot.")
            else:
                await send_error(interaction, f"Failed to send DM: {e}")
        except Exception as e:
            await send_error(interaction, f"Unexpected error: {e}")

    @app_commands.command(name="ghostping", description="Sends a user's mention in this channel and deletes it immediately.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(user="User to ghost ping")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def ghostping(self, interaction: discord.Interaction, user: discord.User):
        # channel.send() is used directly (rather than
        # interaction.response.send_message() + interaction.original_response())
        # since send() already hands back the created Message with its id
        # populated -- no extra fetch needed just to get something to delete.
        # Sending immediately followed by deleting keeps the mention live for
        # exactly as long as these two HTTP round trips take.
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

        await send_success(interaction, f"Ghost pinged {user.mention}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))

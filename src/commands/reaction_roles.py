import re
from datetime import datetime
from typing import Any, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, send_success, send_error
from api.github import GitHubAPIError, fetch_botstate_with_sha, update_botstate

GUILD = discord.Object(id=config.GUILD_ID)

# Code-only feature toggle: set to False to stop the bot from DMing users
# when they gain/lose a reaction role. No slash command controls this;
# flip it here and restart the bot.
REACTION_ROLE_DMS_ENABLED = True

# =========================================================================
# Reaction-role panel message pointer -- persisted to BotState.json's
# "reaction_role_panel" key.
#
# The emoji->role mappings themselves already live durably: they're written
# straight into the live Discord message's embed description by
# /reactionrole add/remove. What's NOT durable without this is the pointer
# to *which* message that is -- self.reaction_roles_message_id resets to
# None on every restart, which makes on_raw_reaction_add/remove silently
# no-op forever (they both bail immediately if payload.message_id doesn't
# match), and makes /reactionrole apply think there's no existing panel and
# create a duplicate one on top of the still-live one.
# =========================================================================


async def _persist_reaction_role_panel(channel_id: int, message_id: int):
    """Records the panel's current channel/message id in BotState.json.
    Best-effort -- logged rather than raised, since the panel message
    itself has already been created/edited by the time this is called."""
    def _mutate(state):
        state["reaction_role_panel"] = {"channel_id": str(channel_id), "message_id": str(message_id)}
        return state
    try:
        await update_botstate(_mutate, f"Reaction role panel recorded: {message_id}")
    except GitHubAPIError as e:
        print(f"Failed to persist reaction role panel pointer to BotState.json: {e}")


async def _clear_reaction_role_panel_state():
    """Clears BotState.json's "reaction_role_panel" key back to null --
    called once the panel message itself is confirmed gone, so a stale
    pointer to a deleted message doesn't stick around."""
    def _mutate(state):
        state["reaction_role_panel"] = None
        return state
    try:
        await update_botstate(_mutate, "Reaction role panel cleared")
    except GitHubAPIError as e:
        print(f"Failed to clear reaction role panel pointer from BotState.json: {e}")


async def reconcile_reaction_role_panel(bot: commands.Bot, state: Optional[Dict[str, Any]] = None):
    """Called once from on_ready: restores the ReactionRoles cog's
    reaction_roles_message_id from BotState.json, so a restart no longer
    permanently silences on_raw_reaction_add/remove or risks
    /reactionrole add creating a duplicate panel next to the still-live
    one. Confirms the message still actually exists first -- if it was
    deleted while the bot was down, blindly trusting a stale pointer would
    just reproduce the same silent-no-op failure this exists to fix.

    `state` lets a caller that's already fetched BotState.json hand it
    over directly instead of this making its own redundant fetch -- see
    commands.moderation.reconcile_temp_bans() for the full reasoning."""
    cog = bot.get_cog("ReactionRoles")
    if cog is None:
        return

    if state is None:
        try:
            state, _sha = await fetch_botstate_with_sha()
        except GitHubAPIError as e:
            print(f"Failed to fetch BotState.json for reaction role panel reconciliation: {e}")
            return

    panel = state.get("reaction_role_panel")
    if not panel or not panel.get("message_id"):
        return

    try:
        channel_id = int(panel["channel_id"])
        message_id = int(panel["message_id"])
    except (KeyError, TypeError, ValueError):
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        print(f"Could not find channel {channel_id} to reconcile the reaction role panel.")
        return

    try:
        await channel.fetch_message(message_id)
    except discord.NotFound:
        print(f"Reaction role panel message {message_id} no longer exists -- clearing BotState.json entry.")
        await _clear_reaction_role_panel_state()
        return
    except discord.HTTPException as e:
        print(f"Failed to verify reaction role panel message {message_id}: {e}")
        return

    cog.reaction_roles_message_id = message_id
    print(f"Reconciled the reaction role panel (message {message_id}) from BotState.json.")


class ReactionRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.reaction_roles_message_id = None

    # /reactionrole add and /reactionrole remove live as subcommands under a
    # single /reactionrole group (mirrors the /cipher group pattern in
    # ciphers.py). The guild restriction moves to the group itself -- per
    # discord.py, a group's subcommands can't carry their own
    # @app_commands.guilds(...) default, so decorating the group here makes
    # every child inherit it.
    reactionrole_group = app_commands.guilds(GUILD)(
        app_commands.Group(
            name="reactionrole",
            description="Manage reaction roles on the panel.",
        )
    )

    @reactionrole_group.command(name="add", description="Adds a new reaction role to the panel.")
    @app_commands.describe(
        emoji="Emoji members will react with",
        role="Role to assign when a member reacts with this emoji",
        note="What is the purpose of this role?",
    )
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def reactionrole_add(
        self,
        interaction: discord.Interaction,
        emoji: str,
        role: discord.Role,
        note: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)

        channel = self.bot.get_channel(config.REACTION_ROLE_CHANNEL_ID)

        if self.reaction_roles_message_id is None:
            embed = discord.Embed(title="React to assign roles", description="", color=discord.Color.blurple())
            msg = await channel.send(embed=embed)
            self.reaction_roles_message_id = msg.id
            await _persist_reaction_role_panel(channel.id, msg.id)
        else:
            try:
                msg = await channel.fetch_message(self.reaction_roles_message_id)
            except discord.NotFound:
                # Panel was deleted -- recreate it and save the new id.
                embed = discord.Embed(title="React to assign roles", description="", color=discord.Color.blurple())
                msg = await channel.send(embed=embed)
                self.reaction_roles_message_id = msg.id
                await _persist_reaction_role_panel(channel.id, msg.id)

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

    @reactionrole_group.command(name="remove", description="Removes an existing reaction role from the panel.")
    @app_commands.describe(
        emoji="Emoji of the reaction role to remove",
        role="Role of the reaction role to remove (optional if the emoji already identifies the entry)",
    )
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def reactionrole_remove(
        self,
        interaction: discord.Interaction,
        emoji: Optional[str] = None,
        role: Optional[discord.Role] = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if emoji is None and role is None:
            return await send_error(interaction, "Provide an emoji and/or a role to identify which reaction role to remove.")

        channel = self.bot.get_channel(config.REACTION_ROLE_CHANNEL_ID)

        if self.reaction_roles_message_id is None:
            return await send_error(interaction, "There's no reaction role panel to remove from yet.")

        try:
            msg = await channel.fetch_message(self.reaction_roles_message_id)
        except discord.NotFound:
            self.reaction_roles_message_id = None
            await _clear_reaction_role_panel_state()
            return await send_error(interaction, "The reaction role panel no longer exists.")

        embed = msg.embeds[0] if msg.embeds else None
        if not embed or not embed.description:
            return await send_error(interaction, "There are no reaction roles configured to remove yet.")

        lines = embed.description.split("\n")

        # Match on whichever identifier was given -- the emoji, the role,
        # or both. The first line to satisfy either wins.
        target_index = None
        for i, line in enumerate(lines):
            if emoji and emoji in line:
                target_index = i
                break
            if role and role.mention in line:
                target_index = i
                break

        if target_index is None:
            return await send_error(interaction, "Couldn't find a reaction role matching that emoji or role.")

        removed_line = lines.pop(target_index)
        removed_emoji = removed_line.split(" — ", 1)[0].strip()

        embed.description = "\n".join(lines)
        await msg.edit(embed=embed)

        try:
            await msg.clear_reaction(removed_emoji)
        except discord.HTTPException:
            # Bot may lack Manage Messages, or the reaction is already
            # gone -- either way, the panel/embed removal above already
            # took effect, so this is non-fatal.
            pass

        return await send_success(interaction, f"Removed reaction role: {removed_line}")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.message_id != self.reaction_roles_message_id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        member = guild.get_member(payload.user_id)
        if not member or member.bot:
            return

        emoji = str(payload.emoji)
        message = await self.bot.get_channel(payload.channel_id).fetch_message(payload.message_id)

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
                            dm_embed = discord.Embed(
                                title="Role Added!",
                                description=f"You have been **granted** the role **{role.name}** in **{guild.name}**.",
                                color=discord.Color.green(),
                                timestamp=datetime.now(),
                            )
                            dm_embed.set_thumbnail(url=role.icon.url if role.icon else guild.icon.url if guild.icon else None)
                            dm_embed.set_footer(text="Reaction Role System")
                            try:
                                await member.send(embed=dm_embed)
                            except discord.Forbidden:
                                pass
                break

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.message_id != self.reaction_roles_message_id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        # Raw reaction remove events don't include member data, so it has to
        # be resolved manually. Fall back to a fetch if the member isn't cached.
        member = guild.get_member(payload.user_id)
        if not member:
            try:
                member = await guild.fetch_member(payload.user_id)
            except (discord.NotFound, discord.HTTPException):
                return

        if member.bot:
            return

        emoji = str(payload.emoji)
        message = await self.bot.get_channel(payload.channel_id).fetch_message(payload.message_id)

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
                            dm_embed = discord.Embed(
                                title="Role Removed!",
                                description=f"You have **lost** the role **{role.name}** in **{guild.name}**.",
                                color=discord.Color.red(),
                                timestamp=datetime.now(),
                            )
                            dm_embed.set_thumbnail(url=role.icon.url if role.icon else guild.icon.url if guild.icon else None)
                            dm_embed.set_footer(text="Reaction Role System")
                            try:
                                await member.send(embed=dm_embed)
                            except discord.Forbidden:
                                pass
                break


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRoles(bot))
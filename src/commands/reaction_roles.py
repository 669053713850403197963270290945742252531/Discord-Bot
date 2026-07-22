import re
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, send_success, send_error

GUILD = discord.Object(id=config.GUILD_ID)

# Code-only feature toggle: set to False to stop the bot from DMing users
# when they gain/lose a reaction role. No slash command controls this;
# flip it here and restart the bot.
REACTION_ROLE_DMS_ENABLED = True


class ReactionRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.reaction_roles_message_id = None

    @app_commands.command(name="reactionrole", description="Creates a reaction role panel or applies a reaction role to an already existing panel.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(emoji="Emoji", role="Role to assign", note="What is the purpose of this role?")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def reactionrole(self, interaction: discord.Interaction, emoji: str, role: discord.Role, note: str = None):
        await interaction.response.defer(ephemeral=True)

        channel = self.bot.get_channel(config.REACTION_ROLE_CHANNEL_ID)

        if self.reaction_roles_message_id is None:
            embed = discord.Embed(title="React to assign roles", description="", color=discord.Color.blurple())
            msg = await channel.send(embed=embed)
            self.reaction_roles_message_id = msg.id
        else:
            try:
                msg = await channel.fetch_message(self.reaction_roles_message_id)
            except discord.NotFound:
                # Panel was deleted -- recreate it and save the new id.
                embed = discord.Embed(title="React to assign roles", description="", color=discord.Color.blurple())
                msg = await channel.send(embed=embed)
                self.reaction_roles_message_id = msg.id

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

from datetime import datetime, timezone
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, safe_respond, send_success, send_error, build_embed

GUILD = discord.Object(id=config.GUILD_ID)

# In-memory AFK registry -- process-local and intentionally not persisted
# (no file, no GitHub commit): AFK is a lightweight, session-scoped courtesy
# feature rather than whitelist data, so it resets on restart instead of
# needing a durable store. Keyed by Discord ID string -> {"message": str,
# "since": datetime}.
_afk_status: dict = {}


def _since_field(since: datetime) -> str:
    ts = int(since.timestamp())
    return f"<t:{ts}:F> (<t:{ts}:R>)"


class Afk(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # /afk set and /afk clear (self-service), plus the staff-only /afk mod
    # group, all live under a single /afk group -- mirrors the /cipher group
    # pattern in ciphers.py. The guild restriction goes on this top-level
    # group only: per discord.py, only the root command/group actually
    # passed to CommandTree.add_command() needs _guild_ids set for the
    # whole registered command -- including any nested subcommand group --
    # to be guild-scoped, so afk_mod_group below doesn't need (and can't
    # cleanly carry) its own @app_commands.guilds(...).
    afk_group = app_commands.guilds(GUILD)(
        app_commands.Group(
            name="afk",
            description="Set, clear, or check AFK status.",
        )
    )

    # Nested subcommand group: /afk mod clear, /afk mod check. discord.py
    # only registers class-level Group/Command attributes whose `.parent`
    # is None as a cog's top-level app commands, so this one -- created
    # with parent=afk_group -- is picked up as a child of afk_group instead
    # of a second, separate command.
    afk_mod_group = app_commands.Group(
        name="mod",
        description="Staff tools for managing another member's AFK status.",
        parent=afk_group,
    )

    @afk_group.command(name="set", description="Sets your AFK status -- anyone who pings or replies to you will be notified.")
    @app_commands.describe(message="Message shown to anyone who pings or replies to you (optional, defaults to \"AFK\")")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def afk_set(self, interaction: discord.Interaction, message: Optional[str] = None):
        afk_message = (message or "").strip() or "AFK"
        was_afk = str(interaction.user.id) in _afk_status

        _afk_status[str(interaction.user.id)] = {
            "message": afk_message,
            "since": datetime.now(timezone.utc),
        }

        verb = "Updated" if was_afk else "Set"
        await send_success(
            interaction,
            f"{verb} your AFK status. Anyone who pings or replies to you will be notified, and it'll clear "
            "automatically the next time you send a message.",
            fields=[("Message", afk_message, False)],
        )

    @afk_group.command(name="clear", description="Clears your own AFK status.")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def afk_clear(self, interaction: discord.Interaction):
        if _afk_status.pop(str(interaction.user.id), None) is None:
            return await send_error(interaction, "You're not currently marked as AFK.")

        await send_success(interaction, "Your AFK status has been cleared.")

    @afk_mod_group.command(name="clear", description="Removes the AFK status of a member.")
    @app_commands.describe(member="Member whose AFK status should be cleared")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def afk_mod_clear(self, interaction: discord.Interaction, member: discord.Member):
        if _afk_status.pop(str(member.id), None) is None:
            return await send_error(interaction, f"{member.mention} isn't currently marked as AFK.")

        await send_success(interaction, f"Cleared {member.mention}'s AFK status.")

    @afk_mod_group.command(name="check", description="Returns the AFK status and message of the specified member.")
    @app_commands.describe(member="Member to check")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def afk_mod_check(self, interaction: discord.Interaction, member: discord.Member):
        entry = _afk_status.get(str(member.id))
        if entry is None:
            return await send_success(interaction, f"{member.mention} is not currently AFK.", title="Not AFK")

        embed = build_embed(
            title="💤 AFK Status",
            color=discord.Color.blurple(),
            fields=[
                ("Member", member.mention, False),
                ("Message", entry["message"], False),
                ("AFK Since", _since_field(entry["since"]), False),
            ],
        )
        await safe_respond(interaction, embed=embed, ephemeral=True)

    # =====================================================================
    # Passive behavior: auto-clear on speaking again, and notify whoever
    # pings/replies to someone who's currently AFK.
    # =====================================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.webhook_id is not None:
            return
        if not message.guild or message.guild.id != config.GUILD_ID:
            return

        # Clear the sender's own AFK status the moment they speak again --
        # no need to wait for /afk clear.
        if _afk_status.pop(str(message.author.id), None) is not None:
            try:
                await message.reply(
                    embed=build_embed(
                        description=f"Welcome back, {message.author.mention} -- I've cleared your AFK status.",
                        color=discord.Color.green(),
                    ),
                    mention_author=False,
                    delete_after=10,
                )
            except discord.HTTPException:
                pass  # Missing permissions, or the interaction/message context vanished -- non-fatal.

        # Slash-command responses can be ephemeral, but a plain gateway
        # message like this one has no interaction to attach an ephemeral
        # response to -- Discord simply doesn't support that. This reply
        # self-deletes after a short delay instead, to approximate the same
        # "only matters for a moment" experience without permanently
        # cluttering the channel with AFK notices.
        seen_ids: set = set()
        afk_pinged: List[discord.abc.User] = []

        for user in message.mentions:
            if user.id == message.author.id or user.id in seen_ids:
                continue
            if str(user.id) in _afk_status:
                seen_ids.add(user.id)
                afk_pinged.append(user)

        if message.reference and isinstance(message.reference.resolved, discord.Message):
            replied_to = message.reference.resolved.author
            if replied_to.id != message.author.id and replied_to.id not in seen_ids and str(replied_to.id) in _afk_status:
                seen_ids.add(replied_to.id)
                afk_pinged.append(replied_to)

        if not afk_pinged:
            return

        fields = []
        for user in afk_pinged:
            entry = _afk_status[str(user.id)]
            since_ts = int(entry["since"].timestamp())
            fields.append((user.display_name, f"{entry['message']}\n-# AFK since <t:{since_ts}:R>", False))

        try:
            await message.reply(
                embed=build_embed(title="💤 AFK", color=discord.Color.blurple(), fields=fields),
                mention_author=False,
                delete_after=15,
            )
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Afk(bot))

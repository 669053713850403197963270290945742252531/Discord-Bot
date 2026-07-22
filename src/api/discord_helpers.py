"""
Discord-facing helpers shared across every cog: embed builders, interaction
responders (respecting whether an interaction was already acknowledged),
permission checks, DM notifications, and a couple of small Components V2
layouts reused by multiple commands (a "here's a file" success layout and a
plain no-button status layout).
"""

from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ui import Container, File, LayoutView, TextDisplay

# =========================================================================
# Embed helpers
# =========================================================================

DEFAULT_SUCCESS_COLOR = discord.Color.green()
DEFAULT_ERROR_COLOR = discord.Color.red()


def build_embed(
    title: Optional[str] = None,
    description: Optional[str] = None,
    *,
    color: discord.Color = discord.Color.blue(),
    fields: Optional[List[Tuple[str, Any, bool]]] = None,
    footer: Optional[str] = None,
    thumbnail: Optional[str] = None,
    author: Optional[str] = None,
    author_icon: Optional[str] = None,
    url: Optional[str] = None,
    timestamp: Optional[datetime] = None,
) -> discord.Embed:
    """
    General-purpose embed builder used under the hood by success_embed()/
    error_embed(), but also handy on its own for anything that doesn't
    neatly fit the success/error mold (e.g. informational lookups).

    `fields` accepts (name, value) or (name, value, inline) tuples so
    callers don't have to chain .add_field() themselves.
    """
    embed = discord.Embed(title=title, description=description, color=color, url=url)
    if timestamp is not None:
        embed.timestamp = timestamp

    for field in fields or []:
        if len(field) == 3:
            name, value, inline = field
        else:
            name, value = field
            inline = False
        embed.add_field(name=name, value=value if value not in (None, "") else "N/A", inline=inline)

    if footer:
        embed.set_footer(text=footer)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if author:
        embed.set_author(name=author, icon_url=author_icon)

    return embed


def success_embed(
    description: Optional[str] = None,
    *,
    title: str = "Success",
    color: discord.Color = DEFAULT_SUCCESS_COLOR,
    **kwargs,
) -> discord.Embed:
    """Green-flagged embed for confirming a command completed as expected."""
    return build_embed(title, description, color=color, **kwargs)


def error_embed(
    description: Optional[str] = None,
    *,
    title: str = "Error",
    color: discord.Color = DEFAULT_ERROR_COLOR,
    **kwargs,
) -> discord.Embed:
    """Red-flagged embed for validation failures, exceptions, or 'not found' results."""
    return build_embed(title, description, color=color, **kwargs)


# =========================================================================
# Discord interaction helpers
# =========================================================================

async def safe_respond(interaction: discord.Interaction, content: Optional[str] = None, **kwargs):
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content=content, **kwargs)
        else:
            await interaction.followup.send(content=content, **kwargs)
    except discord.NotFound:
        print("Interaction expired before it could be responded to.")
    except discord.HTTPException as e:
        # interaction.response.is_done() only reflects *this* Interaction
        # object's local state, which can be wrong if some other response
        # already reached Discord for the same underlying interaction --
        # Discord then rejects the "initial response" slot as already used
        # (error code 40060), even though this object never saw that
        # happen. The followup webhook still works regardless of who used
        # the initial response, so retry through that instead of just
        # dropping the message.
        if getattr(e, "code", None) == 40060:
            try:
                await interaction.followup.send(content=content, **kwargs)
            except Exception as e2:
                print(f"Failed to respond via followup after an already-acknowledged error: {e2}")
        else:
            print(f"Failed to respond: {e}")
    except Exception as e:
        print(f"Failed to respond: {e}")


async def send_success(
    interaction: discord.Interaction,
    description: Optional[str] = None,
    *,
    title: str = "Success",
    ephemeral: bool = True,
    fields: Optional[List[Tuple[str, Any, bool]]] = None,
    footer: Optional[str] = None,
    thumbnail: Optional[str] = None,
    embeds: Optional[List[discord.Embed]] = None,
    **kwargs,
):
    """
    Builds a success_embed() and sends it via safe_respond() in one call.
    Pass `embeds=[...]` to ship the success embed alongside another (e.g. a
    data embed) in the same message.
    """
    embed = success_embed(description, title=title, fields=fields, footer=footer, thumbnail=thumbnail)
    if embeds is not None:
        await safe_respond(interaction, embeds=[embed, *embeds], ephemeral=ephemeral, **kwargs)
    else:
        await safe_respond(interaction, embed=embed, ephemeral=ephemeral, **kwargs)


async def send_error(
    interaction: discord.Interaction,
    description: Optional[str] = None,
    *,
    title: str = "Error",
    ephemeral: bool = True,
    fields: Optional[List[Tuple[str, Any, bool]]] = None,
    footer: Optional[str] = None,
    thumbnail: Optional[str] = None,
    **kwargs,
):
    """Builds an error_embed() and sends it via safe_respond() in one call."""
    embed = error_embed(description, title=title, fields=fields, footer=footer, thumbnail=thumbnail)
    await safe_respond(interaction, embed=embed, ephemeral=ephemeral, **kwargs)


async def edit_or_send_error(
    interaction: discord.Interaction,
    description: Optional[str] = None,
    *,
    title: str = "Error",
    fields: Optional[List[Tuple[str, Any, bool]]] = None,
    footer: Optional[str] = None,
    thumbnail: Optional[str] = None,
):
    """
    Reports a failure without leaving a stray placeholder message behind.

    Commands like /ban or /mute send a visible "Processing..." message via
    interaction.response.send_message() before doing the real work. If that
    work then fails, calling send_error() would just post a brand new
    followup message underneath the still-visible "Processing..." message,
    since the interaction has already been responded to. This instead edits
    that original response in place to show the error, since the operation
    failed anyway and there's nothing left to preserve in it.

    Falls back to send_error() if there's no original response yet (or it's
    since been deleted), so this is always safe to call from an except block.
    """
    embed = error_embed(description, title=title, fields=fields, footer=footer, thumbnail=thumbnail)
    if not interaction.response.is_done():
        await send_error(interaction, description, title=title, fields=fields, footer=footer, thumbnail=thumbnail)
        return
    try:
        await interaction.edit_original_response(content=None, embed=embed)
    except discord.NotFound:
        await send_error(interaction, description, title=title, fields=fields, footer=footer, thumbnail=thumbnail)


async def notify_user(user, action: str, moderator, reason: str, guild_name: str):
    titles = {
        "muted": (f"You have been muted in {guild_name}", discord.Color.red()),
        "banned": (f"You have been banned from {guild_name}", discord.Color.red()),
        "unmuted": (f"You have been unmuted in {guild_name}", discord.Color.green()),
        "kicked": (f"You have been kicked from {guild_name}", discord.Color.red()),
    }
    title, color = titles.get(action, (f"Notification from {guild_name}", discord.Color.blue()))

    try:
        embed = discord.Embed(
            title=title,
            description=f"**Reason:** {reason}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Moderator: {moderator}")
        await user.send(embed=embed)
    except Exception as e:
        print(f"Failed to send DM to {user}: {e}")


async def notify_permission_error(user, action: str, guild_name: str):
    """
    DMs a user to let them know something the bot tried to do on their
    behalf failed because the bot itself is missing permissions (e.g. its
    role sits below the target role, or it lacks Manage Roles entirely).

    Meant for raw gateway event handlers (reaction roles, etc.) where
    there's no interaction to reply to, so a discord.Forbidden would
    otherwise vanish into the console with no feedback to anyone.
    """
    embed = error_embed(
        title="Action Failed",
        description=(
            f"I couldn't {action} in **{guild_name}** because I'm missing permissions there. "
            "Please let a staff member know so they can fix my role/permissions."
        ),
    )
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        pass
    except Exception as e:
        print(f"Failed to DM {user} about a permission error: {e}")


# =========================================================================
# Permission checks
# =========================================================================

def has_role(role_id: int):
    async def predicate(interaction: discord.Interaction):
        if role_id in [role.id for role in interaction.user.roles]:
            return True
        raise app_commands.CheckFailure("You do not have the required permissions to run this command.")
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


# =========================================================================
# Shared Components V2 layouts
# =========================================================================

def file_success_layout(description: str, filename: str) -> LayoutView:
    """Components V2 success confirmation with the attached file placed as an
    explicit component *after* the message text, so the confirmation always
    renders above the file rather than relying on Discord's default
    attachment/embed ordering. Used by /export, /genkey, /getkeys, /rollback."""
    layout = LayoutView(timeout=None)
    layout.add_item(Container(
        TextDisplay("### ✅ Success"),
        TextDisplay(description),
        accent_color=discord.Color.green(),
    ))
    layout.add_item(File(f"attachment://{filename}"))
    return layout


def status_layout(title: str, description: str, color: discord.Color) -> LayoutView:
    """A no-button Components V2 'embed' (Container), used for resolved
    states (cleared / cancelled / uploaded / timed out) once any
    confirmation buttons are gone."""
    layout = LayoutView(timeout=None)
    layout.add_item(Container(
        TextDisplay(f"### {title}"),
        TextDisplay(description),
        accent_color=color,
    ))
    return layout

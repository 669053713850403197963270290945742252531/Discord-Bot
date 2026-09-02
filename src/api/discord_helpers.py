"""
Discord-facing helpers shared across every cog: embed builders, interaction
responders (respecting whether an interaction was already acknowledged),
permission checks, DM notifications, and a couple of small Components V2
layouts reused by multiple commands (a "here's a file" success layout and a
plain no-button status layout).
"""

import difflib
import io
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import ActionRow, Button, Container, File, LayoutView, TextDisplay

from api.github import GitHubAPIError, fetch_botstate_with_sha, update_botstate
from api.keys import is_valid_discord_id

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
# Embed size limits
# =========================================================================
#
# Discord rejects an embed with a 50035 "Invalid Form Body" HTTPException
# if a single description/title/field/footer exceeds its own cap, or if
# the embed's *combined* text exceeds 6000 characters -- start.py's
# on_app_command_error() already catches both shapes reactively so a
# command never just silently fails, but commands building an embed from
# variable-length data (e.g. /key fetch's keys list) want to check this
# proactively so they can fall back to a file attachment instead of ever
# hitting that error in the first place.
EMBED_DESCRIPTION_CHAR_LIMIT = 4096
EMBED_TOTAL_CHAR_LIMIT = 6000


def embed_character_total(embed: discord.Embed) -> int:
    """Sums embed text the same way Discord's whole-embed 6000-character
    cap does: title + description + footer text + author name + every
    field's name and value combined."""
    total = 0
    if embed.title:
        total += len(embed.title)
    if embed.description:
        total += len(embed.description)
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    if embed.author and embed.author.name:
        total += len(embed.author.name)
    for field in embed.fields:
        if field.name:
            total += len(field.name)
        if field.value:
            total += len(field.value)
    return total


def embed_within_limits(embed: discord.Embed) -> bool:
    """True if `embed` fits under both of Discord's real limits: 4096
    characters for a single description, and 6000 characters combined
    across the whole embed. Meant to be checked right before sending a
    "finalized" embed built from data whose size isn't known ahead of
    time, so the caller can fall back to something else (a file
    attachment, trimming the content, etc.) instead of finding out via a
    failed API call."""
    if embed.description and len(embed.description) > EMBED_DESCRIPTION_CHAR_LIMIT:
        return False
    return embed_character_total(embed) <= EMBED_TOTAL_CHAR_LIMIT


# =========================================================================
# /toggledms -- global switch for non-essential member-facing DMs
# =========================================================================
#
# Runtime mute switch for /toggledms, gating notify_user()/
# notify_permission_error() below plus every "manual" DM site that doesn't
# go through either of those (temp whitelist grant/extend/expiry, the ban
# member-path DM, temprole grant/expiry, reaction role add/remove -- see
# each of those modules for their own `if dms_enabled():` guard). Kept
# in-memory for fast access from every DM call site, and mirrored to
# BotState.json's "dms_enabled" key on every toggle -- see
# persist_dms_enabled_state()/reconcile_dms_enabled() below -- so a restart
# resumes whichever state staff last left it in instead of silently
# reopening DMs. Defaults to enabled so a restart before reconciliation
# runs (or if reconciliation itself fails) fails open (DMs resume) instead
# of silently staying muted with no indication why. Deliberately doesn't
# cover /dm or /checktemp's tracker -- see access.py's _toggledms_impl()
# docstring for why those two are excluded.
_dms_enabled = True


def dms_enabled() -> bool:
    """Current state of the /toggledms switch, for anything that wants to
    reflect it (e.g. a status command) without importing the private flag
    directly."""
    return _dms_enabled


def set_dms_enabled(value: bool) -> bool:
    """Sets the /toggledms switch. Returns the new state for convenience at
    call sites that want to immediately report it back. In-memory only --
    callers that want the change to survive a restart should follow up with
    persist_dms_enabled_state()."""
    global _dms_enabled
    _dms_enabled = value
    return _dms_enabled


async def persist_dms_enabled_state(message: str):
    """Mirrors the in-memory switch to BotState.json. Called after
    set_dms_enabled() so the new state survives a restart. Best-effort --
    logged rather than raised, since the mute switch itself has already
    taken effect in-process by the time this runs; a failure here only
    means it would fall back to enabled on the next restart instead of
    resuming where it left off."""
    def _mutate(state):
        state["dms_enabled"] = _dms_enabled
        return state
    try:
        await update_botstate(_mutate, message)
    except GitHubAPIError as e:
        print(f"Failed to persist DM mute state to BotState.json: {e}")


async def reconcile_dms_enabled(bot: commands.Bot, state: Optional[Dict[str, Any]] = None):
    """Called once from on_ready: restores the switch from BotState.json,
    so a restart resumes whatever /toggledms state staff last left it in
    instead of silently reopening DMs.

    `state` lets a caller that's already fetched BotState.json hand it over
    directly instead of this making its own redundant fetch -- see
    commands.moderation.reconcile_temp_bans() for the full reasoning."""
    global _dms_enabled
    if state is None:
        try:
            state, _sha = await fetch_botstate_with_sha()
        except GitHubAPIError as e:
            print(f"Failed to fetch BotState.json for DM mute state reconciliation: {e}")
            return

    _dms_enabled = bool(state.get("dms_enabled", True))

    if not _dms_enabled:
        print("Reconciled DM mute state from BotState.json (dms_enabled=False).")


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


async def default_ui_error(
    interaction: discord.Interaction,
    error: Exception,
    item=None,
    *,
    label: str = "UI component",
):
    """
    Shared View/Modal on_error implementation.

    discord.py's default View.on_error/Modal.on_error only print to
    stderr and never touch the interaction -- so any exception that
    escapes a button/select/modal callback (and isn't already caught
    internally) leaves the interaction completely unanswered. Discord
    then shows the user a bare "This interaction failed" with no
    explanation, and the only trace is this print() on the server.

    Wire this in per-class as:
        async def on_error(self, interaction, error, item):
            await default_ui_error(interaction, error, item, label="MyView")
    (Modal.on_error has the same shape minus `item`.)
    """
    print(f"Error in {label} for item {item!r}: {error}")
    try:
        await send_error(interaction, "Something went wrong. Please try again, and let a moderator know if it keeps happening.")
    except Exception as e:
        print(f"Failed to notify user of {label} error: {e}")


async def resolve_user_option(interaction: discord.Interaction, raw_user: str) -> Optional[discord.User]:
    """
    Resolves a raw `user` slash-command option -- typed or picked from a
    whitelist-autocomplete field (see whitelisted_user_autocomplete in
    commands/whitelist.py) -- into an actual discord.User object.

    Re-validates the format first, since autocomplete only *suggests*
    values and Discord still lets someone submit arbitrary text. Then tries
    the client's cache before falling back to a fetch_user() API call (for
    users who share no mutual guild/cache entry with the bot). Sends a
    standard error and returns None on any failure, so callers can just
    `user = await resolve_user_option(...); if user is None: return`.
    """
    raw_user = raw_user.strip()
    if not is_valid_discord_id(raw_user):
        await send_error(
            interaction,
            f"`{raw_user}` doesn't look like a valid Discord ID -- start typing to pick a whitelisted user from the list.",
        )
        return None

    discord_id = int(raw_user)
    user = interaction.client.get_user(discord_id)
    if user is not None:
        return user

    # Cache miss (e.g. the target shares no mutual guild/cache entry with
    # the bot) -- fetch_user() is a live Discord API call, which can
    # occasionally take long enough to blow Discord's ~3 second ack window.
    # Defer first (if a caller hasn't already) so that risk lands here
    # instead of on whatever response the caller makes afterward. Callers
    # that then call interaction.response.defer() themselves guard it with
    # an is_done() check for exactly this reason.
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    try:
        return await interaction.client.fetch_user(discord_id)
    except discord.NotFound:
        await send_error(interaction, f"No Discord user exists with ID `{raw_user}`.")
        return None
    except discord.HTTPException as e:
        await send_error(interaction, f"Couldn't look up that Discord user: {e}")
        return None


async def notify_user(user, action: str, moderator, reason: str, guild_name: str):
    if not dms_enabled():
        return

    titles = {
        "muted": (f"You have been muted in {guild_name}", discord.Color.red()),
        "banned": (f"You have been banned from {guild_name}", discord.Color.red()),
        "unmuted": (f"You have been unmuted in {guild_name}", discord.Color.green()),
        "kicked": (f"You have been kicked from {guild_name}", discord.Color.red()),
        "warned": (f"You have been warned in {guild_name}", discord.Color.orange()),
        "timed_out": (f"You have been timed out in {guild_name}", discord.Color.red()),
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

    Gated behind /toggledms like every other non-essential member-facing
    DM -- see the _dms_enabled block above.
    """
    if not dms_enabled():
        return

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
    attachment/embed ordering. Used by /export, /key generate, /key fetch, /rollback."""
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


# =========================================================================
# Paginated list view (any line-based listing that can outgrow one message)
# =========================================================================
#
# A single Components V2 TextDisplay -- like a plain embed description --
# stops being usable well before Discord's hard character ceiling actually
# bites; a long enough listing (e.g. /url clear reporting hundreds of
# removed entries) needs to be split across multiple pages rather than
# truncated or dumped as a file. PAGINATED_LIST_MAX_CHARS is the per-page
# budget this splits at -- comfortably under that ceiling with headroom
# left for the header/page-count line above it, matching the conservative
# margin /key fetch's own inline-vs-file threshold (1800) and
# send_diff_result()'s inline_char_limit already use elsewhere in this
# module, rather than trying to hug the exact limit.

PAGINATED_LIST_MAX_CHARS = 3500


def paginate_lines(lines: List[str], *, max_chars: int = PAGINATED_LIST_MAX_CHARS) -> List[str]:
    """Greedily packs `lines` into page-sized chunks (each joined with
    "\\n") kept under `max_chars`, for use with PaginatedListView below.

    Packs as many lines as fit per page rather than one line/item per
    page -- a listing like /url clear's removed-entries report can run to
    hundreds of short, similar-looking lines, so this reads far better
    than forcing a page turn per entry.

    A single line longer than `max_chars` on its own still gets a page to
    itself rather than being split mid-line -- this is a packing
    algorithm, not a line-wrapping one, and none of this codebase's own
    callers (one short summary line per entry) are expected to hit that
    case; it exists purely as a safety valve against a caller that does.

    Returns `[""]` for an empty `lines` list, so PaginatedListView always
    has at least one (empty) page to render rather than needing a special
    case for \"nothing to show\"."""
    if not lines:
        return [""]

    pages: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in lines:
        added_len = len(line) + (1 if current else 0)  # +1 for the joining "\n"
        if current and current_len + added_len > max_chars:
            pages.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += added_len
    if current:
        pages.append("\n".join(current))
    return pages


class PaginatedListView(LayoutView):
    """Generic Components V2 paginated 'embed' for any pre-formatted,
    line-based listing that might not fit in a single message -- e.g.
    /url clear's list of removed entries. Splits `lines` into page-sized
    chunks up front via paginate_lines() and shows one page at a time
    with Previous/Next buttons, continuing until the data ends -- unlike
    DbSearchView/WhitelistView elsewhere in this codebase, which page one
    *record* at a time, this pages one *screenful of text* at a time,
    since the content here is a flat list rather than one detailed record
    per page.

    Previous/Next buttons are only added once there's more than one page
    -- a listing that fits on one page renders as a plain, buttonless
    Container, same convention as DbSearchView's own single-match case."""

    def __init__(
        self,
        title: str,
        lines: List[str],
        *,
        color: discord.Color = discord.Color.blurple(),
        timeout: Optional[float] = 300,
    ):
        super().__init__(timeout=timeout)
        self.title = title
        self.pages = paginate_lines(lines)
        self.current_page = 0

        self.header = TextDisplay("")
        self.body = TextDisplay("")

        self.prev_button = Button(label="⏮️ Previous", style=discord.ButtonStyle.secondary)
        self.next_button = Button(label="⏭️ Next", style=discord.ButtonStyle.secondary)
        self.prev_button.callback = self.on_prev
        self.next_button.callback = self.on_next

        self.container = Container(self.header, self.body, accent_color=color)
        if len(self.pages) > 1:
            self.container.add_item(ActionRow(self.prev_button, self.next_button))

        self.add_item(self.container)
        self.refresh_content()

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await default_ui_error(interaction, error, item, label="PaginatedListView")

    def update_button_states(self):
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= len(self.pages) - 1

    def refresh_content(self):
        page_suffix = f" (page {self.current_page + 1}/{len(self.pages)})" if len(self.pages) > 1 else ""
        self.header.content = f"### {self.title}{page_suffix}"
        self.body.content = self.pages[self.current_page] or "*Nothing to show.*"
        self.update_button_states()

    async def on_prev(self, interaction: discord.Interaction):
        self.current_page = max(0, self.current_page - 1)
        self.refresh_content()
        await interaction.response.edit_message(view=self)

    async def on_next(self, interaction: discord.Interaction):
        self.current_page = min(len(self.pages) - 1, self.current_page + 1)
        self.refresh_content()
        await interaction.response.edit_message(view=self)


# =========================================================================
# Diff rendering (shared by /rollback and /diff)
# =========================================================================

def build_unified_diff(before_text: str, after_text: str, *, fromfile: str, tofile: str) -> List[str]:
    """Thin wrapper around difflib.unified_diff with the line-splitting/
    lineterm convention used everywhere in this codebase, so every command
    that diffs two blobs of text produces diffs the exact same way."""
    return list(difflib.unified_diff(
        before_text.splitlines(),
        after_text.splitlines(),
        fromfile=fromfile,
        tofile=tofile,
        lineterm="",
    ))


async def send_diff_result(
    interaction: discord.Interaction,
    diff_lines: List[str],
    *,
    description: str,
    no_changes_message: str = "No changes -- the two are identical.",
    filename: str = "diff.diff",
    inline_char_limit: int = 1800,
    ephemeral: bool = True,
):
    """
    Renders a unified diff the same way /rollback originally did: inline in
    a ```diff``` block when it's small enough to fit, or as an attached
    .diff file when it isn't. Shared by /rollback and /diff so both stay in
    sync if this rendering ever changes.
    """
    if not diff_lines:
        await send_success(interaction, f"{description}\n\n{no_changes_message}", ephemeral=ephemeral)
        return

    diff_text = "\n".join(diff_lines)

    if len(diff_text) <= inline_char_limit:
        await send_success(interaction, f"{description}\n\n```diff\n{diff_text}\n```", ephemeral=ephemeral)
        return

    full_description = (
        f"{description}\n\nDiff too large to display inline ({len(diff_lines)} lines) "
        "-- see attached file below."
    )
    diff_file = discord.File(io.BytesIO(diff_text.encode()), filename=filename)
    layout = file_success_layout(full_description, filename)
    await interaction.followup.send(view=layout, file=diff_file, ephemeral=ephemeral)
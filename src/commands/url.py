"""
/url shorten, /url unshorten, /paste, and /file -- link shortening and
lookup, text pastes, and image/audio/application file hosting, all via
e-z.host (unshorten's live fallback aside, which can reach any http(s)
host -- see api.redirect_resolver / api.ssrf_guard).

Persists every successful create to storage/shortened-urls.json the
moment e-z.host's response comes back (see api.github.save_shortened_url)
-- e-z.host only ever hands back an entry's deletion_url once, at
creation, so it has to be captured immediately or it's gone for good.
"""

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import ActionRow, Button, Container, LayoutView, TextDisplay

from api import config
from api.discord_helpers import (
    has_role, is_in_guild, send_success, send_error,
    status_layout, default_ui_error, PaginatedListView,
)
from api.alerts import send_alert, alert_embed, ALERT_COLOR_REMOVE
from api.keys import is_valid_url
from api.github import (
    GitHubAPIError, save_shortened_url, find_shortened_url_entry,
    find_matching_shortened_urls, clear_shortened_urls,
)
from api.providers.ez_host import EZHostAPIError, shorten_url, create_paste, upload_file, extract_short_code
from api.redirect_resolver import RedirectResolutionError, follow_redirects
from api.ssrf_guard import SSRFBlockedError
from api.time_utils import parse_iso, parse_time_filter

GUILD = discord.Object(id=config.GUILD_ID)

# This provider's namespace key in storage/shortened-urls.json -- see
# api/github.py's "Shortened URLs" section for the full schema, including
# why each kind below ("shorten"/"paste"/"file") gets its own
# sub-namespace instead of sharing one flat dict.
PROVIDER = "ez_host"

# /file accepts image, audio, and application attachments -- checked
# before the attachment is even downloaded, so anything outside these
# three top-level MIME categories never reaches e-z.host at all.
#
# Note storage/test_ez_host_api.py only ever confirmed the two extremes
# live against e-z.host: a real PNG succeeds cleanly, and a plain .txt
# (an "image/" vs. everything-else test, not this three-way split) comes
# back as a broken, non-JSON 422 (e-z.host's own error handler crashes
# trying to report the validation failure). Audio/application content
# hasn't been separately confirmed working server-side -- e-z.host's own
# response is still the final word on those, and a rejection will surface
# to the user as an EZHostAPIError the same way the .txt case does.
_ALLOWED_FILE_CONTENT_TYPE_PREFIXES = ("image/", "audio/", "application/")

# Sanity cap on the /file attachment before it's sent to e-z.host at all --
# same convention as commands.qrcode.MAX_IMAGE_ATTACHMENT_SIZE /
# commands.utility.MAX_DIFF_ATTACHMENT_SIZE. Not a confirmed e-z.host
# limit (their docs don't publish one) -- e-z.host's own error response is
# still the final word on anything that slips past this.
#
# e-z.host's default upload limit is 50 MB; this account is on e-z.host
# Premium, which raises that ceiling to 100 MB, so the cap here matches
# the Premium limit rather than the default.
MAX_FILE_ATTACHMENT_SIZE = 100 * 1024 * 1024  # 100 MiB (e-z.host Premium)

# Discord's own hard ceiling for a STRING option's max_length.
MAX_PASTE_TEXT_LENGTH = 6000


async def _persist_or_degrade(
    interaction: discord.Interaction,
    *,
    kind: str,
    short_code: str,
    entry: Dict[str, Any],
    commit_message: str,
    success_description: str,
    success_fields: List[Tuple[str, Any, bool]],
    deletion_url: str,
):
    """Shared tail end of /url shorten, /paste, and /file: persist the
    entry e-z.host just created, then reply. If the GitHub write fails,
    the provider call already succeeded by this point -- that shouldn't
    read as the command having failed outright, just that this record
    won't survive a restart, so this degrades to showing the
    deletion_url inline instead (it won't be shown again, and this is the
    one path where it wouldn't otherwise be saved anywhere)."""
    try:
        await save_shortened_url(PROVIDER, kind, short_code, entry, commit_message)
    except GitHubAPIError as e:
        print(f"Failed to persist {kind} {short_code} to shortened-urls.json: {e}")
        return await send_success(
            interaction,
            success_description,
            fields=[
                *success_fields,
                (
                    "⚠️ Not saved to persistent storage",
                    f"Couldn't record this in shortened-urls.json ({e}). "
                    f"Save this now if you'll need to delete it later -- it won't be shown again:\n`{deletion_url}`",
                    False,
                ),
            ],
            ephemeral=True,
        )

    await send_success(interaction, success_description, fields=success_fields, ephemeral=True)


async def _url_shorten_impl(interaction: discord.Interaction, url: str):
    url = url.strip()
    if not is_valid_url(url):
        return await send_error(interaction, f"`{url}` doesn't look like a valid http(s) URL.")

    # Both the e-z.host call and the GitHub commit that follows are real
    # network round trips that can easily blow Discord's ~3s ack window,
    # so defer before either one. Ephemeral, matching the ephemeral
    # success reply below -- a deferral's ephemeral flag can't be
    # loosened by a later followup, so it has to be decided here.
    await interaction.response.defer(ephemeral=True)

    try:
        result = await shorten_url(url)
    except EZHostAPIError as e:
        return await send_error(interaction, f"Failed to shorten that link: {e}")

    short_code = extract_short_code(result["short_url"])
    entry = {
        "original_url": url,
        "shortened_url": result["short_url"],
        "deletion_url": result["deletion_url"],
        "creator_id": str(interaction.user.id),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    await _persist_or_degrade(
        interaction,
        kind="shorten",
        short_code=short_code,
        entry=entry,
        commit_message=f"Shortened URL created by {interaction.user} ({interaction.user.id}): {short_code}",
        success_description=f"Shortened: {result['short_url']}",
        success_fields=[("Original", url, False)],
        deletion_url=result["deletion_url"],
    )


async def _url_paste_impl(
    interaction: discord.Interaction,
    text: str,
    language: Optional[str],
    title: Optional[str],
    description: Optional[str],
):
    language = (language or "plaintext").strip() or "plaintext"
    title = title.strip() if title else None
    description = description.strip() if description else None

    # Same defer-first, ephemeral-from-the-start reasoning as /url shorten.
    await interaction.response.defer(ephemeral=True)

    try:
        result = await create_paste(text, language=language, title=title, description=description)
    except EZHostAPIError as e:
        return await send_error(interaction, f"Failed to create that paste: {e}")

    short_code = extract_short_code(result["paste_url"])
    entry = {
        "title": title,
        "language": language,
        "paste_url": result["paste_url"],
        "raw_url": result["raw_url"],
        "deletion_url": result["deletion_url"],
        "creator_id": str(interaction.user.id),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    fields = [("Paste", result["paste_url"], False), ("Raw", result["raw_url"], False)]
    if title:
        fields.insert(0, ("Title", title, False))

    await _persist_or_degrade(
        interaction,
        kind="paste",
        short_code=short_code,
        entry=entry,
        commit_message=f"Paste created by {interaction.user} ({interaction.user.id}): {short_code}",
        success_description="Paste created.",
        success_fields=fields,
        deletion_url=result["deletion_url"],
    )


async def _url_file_impl(interaction: discord.Interaction, file: discord.Attachment):
    if file.content_type and not file.content_type.startswith(_ALLOWED_FILE_CONTENT_TYPE_PREFIXES):
        return await send_error(
            interaction,
            f"`{file.filename}` isn't an image, audio, or application file ({file.content_type}). "
            "e-z.host's file upload only reliably accepts real image content -- other types may "
            "still be rejected by e-z.host itself.",
        )
    if file.size > MAX_FILE_ATTACHMENT_SIZE:
        return await send_error(
            interaction,
            f"`{file.filename}` is too large to upload ({file.size:,} bytes -- the limit is "
            f"{MAX_FILE_ATTACHMENT_SIZE:,} bytes).",
        )

    # Downloading the attachment from Discord's CDN is itself a network
    # round trip, on top of the e-z.host upload and GitHub commit that
    # follow -- defer before any of them, same ephemeral-from-the-start
    # reasoning as /url shorten and /paste.
    await interaction.response.defer(ephemeral=True)

    try:
        content = await file.read()
    except discord.HTTPException as e:
        return await send_error(interaction, f"Failed to download that attachment: {e}")

    try:
        result = await upload_file(file.filename, content, file.content_type)
    except EZHostAPIError as e:
        return await send_error(interaction, f"Failed to upload that file: {e}")

    short_code = extract_short_code(result["file_url"])
    entry = {
        "original_filename": file.filename,
        "content_type": file.content_type,
        "size": file.size,
        "file_url": result["file_url"],
        "deletion_url": result["deletion_url"],
        "creator_id": str(interaction.user.id),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    await _persist_or_degrade(
        interaction,
        kind="file",
        short_code=short_code,
        entry=entry,
        commit_message=f"File uploaded by {interaction.user} ({interaction.user.id}): {short_code}",
        success_description=f"Uploaded: {result['file_url']}",
        success_fields=[("Original filename", file.filename, False)],
        deletion_url=result["deletion_url"],
    )


def _format_created_at(created_at: Optional[str]) -> str:
    """Renders a shortened-urls.json entry's created_at (always
    "%Y-%m-%dT%H:%M:%SZ" UTC -- see _url_shorten_impl/_url_paste_impl/
    _url_file_impl above, which all stamp it the same way) as a Discord
    <t:...> timestamp, so it displays in whoever's looking at it's own
    local time. Not api.time_utils.format_discord_timestamp() -- that
    one's built around JoinDate's "m/d/yyyy, h:mm:ss AM/PM" format,
    which this isn't."""
    if not created_at:
        return "N/A"
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return created_at
    return f"<t:{int(dt.timestamp())}:f>"


# Human-readable label per kind, for /url unshorten's "found locally"
# description -- e.g. "Found in the local store (paste)."
_KIND_LABELS = {"shorten": "shortened link", "paste": "paste", "file": "file upload"}


def _fields_for_store_entry(kind: str, entry: Dict[str, Any]) -> List[Tuple[str, Any, bool]]:
    """Builds /url unshorten's success-embed fields for a
    storage/shortened-urls.json entry found locally. Shape differs per
    kind -- a "shorten" entry, a "paste", and a "file" upload each carry
    different fields (see api/github.py's schema comment) -- but
    creator/created_at are common to all three, so those are appended
    once at the end rather than repeated in each branch."""
    creator_id = entry.get("creator_id")
    mention = f"<@{creator_id}>" if creator_id else "Unknown"

    if kind == "shorten":
        fields: List[Tuple[str, Any, bool]] = [
            ("Original URL", entry.get("original_url", "N/A"), False),
            ("Shortened URL", entry.get("shortened_url", "N/A"), False),
        ]
    elif kind == "paste":
        fields = [
            ("Paste", entry.get("paste_url", "N/A"), False),
            ("Raw", entry.get("raw_url", "N/A"), False),
        ]
        if entry.get("title"):
            fields.append(("Title", entry["title"], True))
    else:  # "file"
        fields = [
            ("File URL", entry.get("file_url", "N/A"), False),
            ("Original filename", entry.get("original_filename", "N/A"), True),
        ]

    fields.append(("Created by", mention, True))
    fields.append(("Created at", _format_created_at(entry.get("created_at")), True))
    return fields


async def _url_unshorten_impl(interaction: discord.Interaction, url: str):
    url = url.strip()
    if not is_valid_url(url):
        return await send_error(interaction, f"`{url}` doesn't look like a valid http(s) URL.")

    # The store lookup below is one fast GitHub read; the live-redirect
    # fallback further down can be several sequential HTTP round trips
    # (one per hop) -- defer before either path, same ephemeral-from-
    # the-start reasoning as /url shorten, /paste, and /file.
    await interaction.response.defer(ephemeral=True)

    # Fast path: this bot's own record of what it created, if it created
    # it. extract_short_code() is generic across shorten/paste/file URLs
    # (see api/providers/ez_host.py), and find_shortened_url_entry()
    # checks all three kinds in one fetch.
    short_code = extract_short_code(url)
    store_error: Optional[GitHubAPIError] = None
    try:
        found = await find_shortened_url_entry(short_code, PROVIDER)
    except GitHubAPIError as e:
        found = None
        store_error = e

    if found is not None:
        kind, entry = found
        return await send_success(
            interaction,
            f"Found in the local store ({_KIND_LABELS.get(kind, kind)}).",
            fields=_fields_for_store_entry(kind, entry),
        )

    # Not (or not confirmably, if store_error is set) in this bot's own
    # store -- fall back to actually following the redirect chain live.
    # SSRF-guarded per hop; see api.ssrf_guard / api.redirect_resolver
    # for why and how.
    try:
        result = await follow_redirects(url)
    except SSRFBlockedError as e:
        return await send_error(interaction, f"Refused to follow that link: {e}")
    except RedirectResolutionError as e:
        return await send_error(interaction, f"Couldn't resolve that link: {e}")

    fields: List[Tuple[str, Any, bool]] = [
        ("Hops followed", str(result.hop_count), True),
        ("Final destination", result.final_url, False),
    ]
    if store_error is not None:
        fields.append((
            "⚠️ Couldn't check the local store first",
            f"{store_error} -- the result above is from following the link live, not from shortened-urls.json.",
            False,
        ))

    description = (
        f"Resolved: {result.final_url}"
        if result.hop_count > 0
        else "That URL didn't redirect anywhere -- it's already the final destination."
    )
    await send_success(interaction, description, fields=fields)


# =========================================================================
# /url clear
# =========================================================================

# Kind filter choices for /url clear's `type` option -- mirrors
# _KIND_LABELS' keys, but as app_commands.Choice objects since that's the
# shape @app_commands.choices() needs. Kept separate from _KIND_LABELS
# (rather than building this from it) so the *option's* wording ("Shortened
# links") can read naturally as a menu label without also having to double
# as _fields_for_store_entry()'s inline "(shortened link)" phrasing.
_CLEAR_KIND_CHOICES = [
    app_commands.Choice(name="Shortened links", value="shorten"),
    app_commands.Choice(name="Pastes", value="paste"),
    app_commands.Choice(name="File uploads", value="file"),
]


def _clear_predicate(
    mode: str,
    *,
    discord_id: Optional[str] = None,
    cutoff: Optional[datetime] = None,
    kind_filter: Optional[str] = None,
) -> Callable[[Dict[str, Any], str, str], bool]:
    """Builds the predicate find_matching_shortened_urls()/
    clear_shortened_urls() call per entry. `mode` selects which of /url
    clear's three mutually-exclusive filters (`all`/`user`/`before`) is
    active; `kind_filter` (from the `type` option) is layered on top of
    any of the three, rather than being a fourth mode of its own."""
    def _predicate(entry: Dict[str, Any], kind: str, provider: str) -> bool:
        if kind_filter is not None and kind != kind_filter:
            return False
        if mode == "all":
            return True
        if mode == "user":
            return str(entry.get("creator_id")) == discord_id
        if mode == "before":
            created = parse_iso(entry.get("created_at"))
            return created is not None and created <= cutoff
        return False
    return _predicate


def _format_removed_line(provider: str, kind: str, short_code: str, entry: Dict[str, Any]) -> str:
    """One line of /url clear's post-clear PaginatedListView report --
    kind label, the short code, whichever URL field that kind actually
    has (shorten/paste/file each name it differently -- see
    _fields_for_store_entry() above for the full per-kind shape), who
    created it, and when."""
    label = _KIND_LABELS.get(kind, kind)
    url_value = entry.get("shortened_url") or entry.get("paste_url") or entry.get("file_url") or "N/A"
    creator_id = entry.get("creator_id")
    mention = f"<@{creator_id}>" if creator_id else "Unknown"
    return f"`{short_code}` **({label})** {url_value} — {mention} — {_format_created_at(entry.get('created_at'))}"


class ConfirmUrlClearLayout(LayoutView):
    """Components V2 confirmation prompt for /url clear -- mirrors
    whitelist.py's ConfirmClearLayout (title/description and the
    Confirm/Cancel buttons live in the same Container), since this is
    just as capable of wiping a large amount of data in one shot and
    deserves the same "are you sure" gate before it commits anything."""

    def __init__(self, author_id: int, mode_desc: str, match_count: int):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.confirmed: Optional[bool] = None

        self.container = Container(
            TextDisplay("### ⚠️ Clear Shortened URLs"),
            TextDisplay(
                f"This will permanently remove **{match_count}** entr{'y' if match_count == 1 else 'ies'} "
                f"from `shortened-urls.json` -- {mode_desc}. This only clears this bot's local record of "
                "them; it does not delete the underlying links/pastes/files from e-z.host itself. "
                "This action cannot be undone."
            ),
            accent_color=discord.Color.orange(),
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

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await default_ui_error(interaction, error, item, label="ConfirmUrlClearLayout")

    async def confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await send_error(interaction, "You cannot confirm this action.")
        self.confirmed = True
        self.stop()
        await interaction.response.edit_message(
            view=status_layout("Clearing Entries", "Clearing `shortened-urls.json` entries...", discord.Color.blurple())
        )

    async def cancel(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await send_error(interaction, "You cannot cancel this action.")
        self.confirmed = False
        self.stop()
        await interaction.response.defer()
        await interaction.delete_original_response()


async def _url_clear_impl(
    interaction: discord.Interaction,
    clear_all: Optional[bool],
    user: Optional[discord.User],
    before: Optional[str],
    kind_filter: Optional[str],
):
    provided = [bool(clear_all), user is not None, before is not None]
    if sum(provided) == 0:
        return await send_error(interaction, "Provide one of `all`, `user`, or `before`.")
    if sum(provided) > 1:
        return await send_error(interaction, "Provide only one of `all`, `user`, or `before` -- not more than one.")

    cutoff: Optional[datetime] = None
    if before is not None:
        cutoff = parse_time_filter(before)
        if cutoff is None:
            return await send_error(
                interaction,
                f"Couldn't parse `{before}` as a date/time. Try an exact date/time (e.g. `8/13/2026, 2:50 AM`) "
                "or a relative duration (e.g. `20 minutes`, `2 hours ago`, `3 days`).",
            )

    if clear_all:
        mode = "all"
        mode_desc = "**every** entry"
    elif user is not None:
        mode = "user"
        mode_desc = f"every entry created by {user.mention}"
    else:
        mode = "before"
        mode_desc = f"every entry created at or before <t:{int(cutoff.timestamp())}:f>"

    if kind_filter is not None:
        mode_desc += f" ({_KIND_LABELS.get(kind_filter, kind_filter)}s only)"

    predicate = _clear_predicate(mode, discord_id=str(user.id) if user else None, cutoff=cutoff, kind_filter=kind_filter)

    # Deferred first: the preview fetch just below, and the confirmation
    # round trip that follows it, both easily blow Discord's ~3s ack
    # window -- same reasoning as every other /url subcommand.
    await interaction.response.defer(ephemeral=True)

    try:
        matches = await find_matching_shortened_urls(predicate)
    except GitHubAPIError as e:
        return await send_error(interaction, str(e))

    if not matches:
        return await send_success(interaction, f"Nothing to clear -- no entries match {mode_desc}.")

    view = ConfirmUrlClearLayout(interaction.user.id, mode_desc, len(matches))
    message = await interaction.followup.send(view=view, ephemeral=True)

    await view.wait()
    if not view.confirmed:
        return  # Cancelled (message already replaced/deleted by the button) or timed out.

    commit_message = (
        f"Cleared {len(matches)} shortened-urls.json entr{'y' if len(matches) == 1 else 'ies'} "
        f"by {interaction.user} ({interaction.user.id}) via /url clear ({mode})"
    )
    try:
        removed = await clear_shortened_urls(predicate, commit_message)
    except GitHubAPIError as e:
        return await message.edit(view=status_layout("Clear Failed", str(e), discord.Color.red()))

    await send_alert(interaction.client, alert_embed(
        "🧹 Shortened URLs Cleared",
        f"{interaction.user.mention} cleared **{len(removed)}** entr{'y' if len(removed) == 1 else 'ies'} "
        f"from `shortened-urls.json` via `/url clear` -- {mode_desc}.",
        color=ALERT_COLOR_REMOVE,
    ))

    await message.edit(view=status_layout(
        "✅ Cleared",
        f"Cleared **{len(removed)}** entr{'y' if len(removed) == 1 else 'ies'} matching {mode_desc}.",
        discord.Color.green(),
    ))

    # Full listing of what got removed, as a follow-up -- paginated (via
    # PaginatedListView) rather than crammed into one embed/Container,
    # since a large /url clear (e.g. `all`, or a wide `before` cutoff) can
    # easily produce more removed entries than a single message's text
    # components can hold.
    lines = [_format_removed_line(provider, kind, code, entry) for provider, kind, code, entry in removed]
    result_view = PaginatedListView(f"🗑️ Cleared Entries ({len(removed)})", lines, color=discord.Color.red())
    await interaction.followup.send(view=result_view, ephemeral=True)


class Url(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    url_group = app_commands.guilds(GUILD)(
        app_commands.Group(
            name="url",
            description="Shorten and manage links.",
        )
    )

    @url_group.command(name="shorten", description="Shortens a link via e-z.host.")
    @app_commands.describe(url="The URL to shorten")
    # 3 per 60s per user -- generous enough for normal use, tight enough
    # that a slip of the enter key (or someone poking at it) can't spray
    # requests at e-z.host or rack up GitHub commits.
    @app_commands.checks.cooldown(3, 60.0)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def url_shorten(self, interaction: discord.Interaction, url: str):
        await _url_shorten_impl(interaction, url)

    @url_group.command(name="unshorten", description="Resolves a short link back to its original destination.")
    @app_commands.describe(url="The short/redirecting URL to resolve.")
    # Same 3-per-60s convention as the other /url subcommands -- the
    # live-redirect fallback in particular is the one path here that
    # makes outbound requests to a URL a user supplied, so this also
    # doubles as a light brake on that.
    @app_commands.checks.cooldown(3, 60.0)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def url_unshorten(self, interaction: discord.Interaction, url: str):
        await _url_unshorten_impl(interaction, url)

    # Discord caps command/option descriptions at 100 characters -- the
    # fuller explanation of `before`'s accepted formats and each option's
    # mutual-exclusivity with the other two lives in /url clear's error
    # messages and ConfirmUrlClearLayout instead, where there's no such
    # limit.
    @url_group.command(name="clear", description="Clears entries from shortened-urls.json -- everything, one user's, or before a given time.")
    @app_commands.describe(
        all="Clear every entry. Use this, `user`, or `before` -- not more than one.",
        user="Clear only entries created by this user. Use this, `all`, or `before` -- not one.",
        before="Clear entries at/before this time -- a date/time or '20 minutes ago'-style duration.",
        type="Optional -- restrict to one kind of entry (default: all).",
    )
    @app_commands.choices(type=_CLEAR_KIND_CHOICES)
    # 1 per 30s -- this is a destructive, potentially-bulk operation (unlike
    # the 3-per-60s create/lookup commands above), so the cooldown leans
    # tighter to make an accidental double-run less likely, on top of the
    # confirmation prompt itself.
    @app_commands.checks.cooldown(1, 30.0)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def url_clear(
        self,
        interaction: discord.Interaction,
        all: Optional[bool] = None,
        user: Optional[discord.User] = None,
        before: Optional[str] = None,
        type: Optional[app_commands.Choice[str]] = None,
    ):
        await _url_clear_impl(interaction, all, user, before, type.value if type else None)

    @app_commands.command(name="paste", description="Creates a text paste via e-z.host.")
    @app_commands.describe(
        text=f"The text content to paste (up to {MAX_PASTE_TEXT_LENGTH} characters).",
        language="Syntax highlighting language, e.g. lua, python, plaintext. Defaults to plaintext.",
        title="Optional title for the paste.",
        description="Optional description for the paste.",
    )
    @app_commands.checks.cooldown(3, 60.0)
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def paste(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, MAX_PASTE_TEXT_LENGTH],
        language: Optional[str] = None,
        title: Optional[app_commands.Range[str, 1, 100]] = None,
        description: Optional[app_commands.Range[str, 1, 200]] = None,
    ):
        await _url_paste_impl(interaction, text, language, title, description)

    @app_commands.command(name="file", description="Uploads an image, audio, or application file via e-z.host.")
    @app_commands.describe(file="The file to upload -- image, audio, or application (PNG, JPG, MP3, WAV, ZIP, PDF, etc.).")
    @app_commands.checks.cooldown(3, 60.0)
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def file(self, interaction: discord.Interaction, file: discord.Attachment):
        await _url_file_impl(interaction, file)


async def setup(bot: commands.Bot):
    await bot.add_cog(Url(bot))
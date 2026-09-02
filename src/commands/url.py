"""
/url shorten, /url unshorten, /paste, and /file -- link shortening/lookup,
text pastes, and image/audio/application file hosting, each via a choice
of provider (see api/providers/registry.py for
the full list, capability flags, and each provider module's own confirmed-
vs-best-effort status) rather than being hardcoded to one. e-z.host
remains the default for /url shorten, /paste, and /file, matching this
feature's pre-multi-provider-expansion behavior (unshorten's live fallback
aside, which can reach any http(s) host regardless of provider -- see
api.redirect_resolver / api.ssrf_guard).

Persists every successful create to storage/shortened-urls.json the
moment the provider's response comes back (see api.github.save_shortened_url)
-- most providers only ever hand back an entry's deletion_url once, at
creation (some don't hand one back at all -- see registry.py), so it has
to be captured immediately or it's gone for good.
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
from api.providers.errors import ProviderAPIError
from api.providers.registry import PROVIDERS, DEFAULT_PROVIDER, choices_for
from api.providers.util import extract_short_code
from api.providers.litterbox import DEFAULT_EXPIRY as _LITTERBOX_DEFAULT_EXPIRY, VALID_EXPIRIES as _LITTERBOX_VALID_EXPIRIES
from api.providers import is_gd
from api.providers.is_gd import IsGdAPIError
from api.providers import pastebin
from api.providers.pastebin import PastebinAPIError
from api.providers import pastee_dev
from api.providers.pastee_dev import PasteeDevAPIError
from api.providers import pastey_gg
from api.providers import rubis
from api.providers.languages import search_languages, language_for_filename
from api.redirect_resolver import RedirectResolutionError, follow_redirects
from api.ssrf_guard import SSRFBlockedError
from api.time_utils import format_iso, parse_iso, parse_time_filter

GUILD = discord.Object(id=config.GUILD_ID)

# /file accepts image, audio, and application attachments -- checked
# before the attachment is even downloaded, so anything outside these
# three top-level MIME categories never reaches any provider at all.
#
# Note storage/test_ez_host_api.py only ever confirmed the two extremes
# live against e-z.host specifically: a real PNG succeeds cleanly, and a
# plain .txt (an "image/" vs. everything-else test, not this three-way
# split) comes back as a broken, non-JSON 422 (e-z.host's own error
# handler crashes trying to report the validation failure). Audio/
# application content hasn't been separately confirmed working
# server-side for e-z.host, and none of this has been confirmed at all
# for the other file providers the multi-provider expansion added
# (Catbox, Litterbox) -- each provider's own response is still the final
# word on anything that slips past this, and a rejection will surface to
# the user as a ProviderAPIError the same way the .txt case does for
# e-z.host.
_ALLOWED_FILE_CONTENT_TYPE_PREFIXES = ("image/", "audio/", "application/")

# Sanity cap on the /file attachment before it's sent to any provider at
# all -- same convention as commands.qrcode.MAX_IMAGE_ATTACHMENT_SIZE /
# commands.utility.MAX_DIFF_ATTACHMENT_SIZE. Not a confirmed limit for
# every provider this now covers (Catbox and Litterbox both advertise
# higher ceilings of their own, and Uguu's own homepage confirms a 128 MiB
# limit -- see api.providers.uguu's module docstring) -- kept at e-z.host
# Premium's 100 MiB uniformly across every file provider rather than varied
# per provider, since a single conservative cap that's always safe is
# simpler than tracking each provider's own (typically higher) limit
# separately, and nothing about this feature needs to push right up
# against any one provider's max.
MAX_FILE_ATTACHMENT_SIZE = 100 * 1024 * 1024  # 100 MiB

# Discord's own hard ceiling for a STRING option's max_length -- still
# the cap for /paste's typed `text` option specifically. Doesn't bound
# `file`/`file1`-`file4` below, since those read their content from an
# attachment rather than a STRING option -- see MAX_PASTE_FILE_ATTACHMENT_SIZE
# for the (byte-based, not character-based) cap that applies to those instead.
MAX_PASTE_TEXT_LENGTH = 6000

# Cap on any single text-bearing attachment /paste reads directly -- the
# primary content's `file` option, and each of the `file1`-`file4` extra
# file slots below. Same friendly-pre-provider-cap convention as
# commands.utility.MAX_DIFF_ATTACHMENT_SIZE / commands.qrcode.MAX_IMAGE_ATTACHMENT_SIZE,
# just sized for pasted text rather than a diff'd file or an image: 1 MiB
# is roomy for "a whole document/markdown file" (this option's whole
# reason for existing -- see the `file` option's own description) while
# still being nowhere near what any of this package's paste providers
# themselves accept, so a rejection here is about a sane upload size, not
# a provider's own (unconfirmed, and likely much higher) limit.
MAX_PASTE_FILE_ATTACHMENT_SIZE = 1 * 1024 * 1024  # 1 MiB

# pastey_gg/pastee_dev-exclusive (registry.py's supports_extra_files=True
# check in _url_paste_impl already rejects this for any other provider):
# how many extra `file1`-`file4` attachment slots /paste exposes on top of
# the primary text/file. Discord's option model has no variable-length
# list, so a fixed, numbered set of optional slots is the standard
# workaround for "attach up to N more" -- each slot is independently
# optional and simply omitted from the request's files/sections when left
# blank.
#
# Each slot's `content`, `name`, and `language` all come from the one
# attachment now (content from its bytes, name from its filename,
# language guessed from its filename via
# api.providers.languages.language_for_filename) -- there's no separate
# fileN_title/fileN_language option anymore for a slot to also need.
#
# 4 (5 files total, primary + 4) purely as a UI choice on this bot's
# side, picked to match Echo's self-hosted config.example.yaml MaxFiles
# default cited in pastey_gg.py's module docstring (point 4) -- that
# default is explicitly *not* confirmed to be api.pastey.gg's actual
# production value (admin-configured, never stated in any response), so
# this is never enforced as a local pre-request rejection the way
# `remaining_views`/`expires_at` are elsewhere in this function. A
# production MaxFiles lower than this still surfaces correctly, just one
# request-round-trip later, as pastey.gg's own validatePaste() error
# message via the existing ProviderAPIError handling below (pastee.dev
# has no such limit documented at all -- see pastee_dev.py).
MAX_PASTE_EXTRA_FILES = 4

# /file's `expiry` choices -- Litterbox-only (registry.py's
# requires_expiry=True for that provider alone), but the option itself
# lives on the shared /file command since every provider's own params sit
# on one command rather than one command per provider (see this module's
# docstring). Picking `expiry` while any other provider is selected is
# rejected in _url_file_impl below, per the planning doc's "reject/ignore
# instead of dynamically hiding" for provider-only extras.
_LITTERBOX_EXPIRY_CHOICES = [app_commands.Choice(name=e, value=e) for e in _LITTERBOX_VALID_EXPIRIES]

# /paste's `visibility` choices -- built from every supports_visibility=True
# provider's own valid-value tuple together (Rubiš's "public"/"private" plus
# Pastebin's "public"/"unlisted"/"private" -- see pastebin.py's own module
# docstring), deduped via dict.fromkeys() (preserves first-seen order) so
# "unlisted" shows up exactly once despite not being one of Rubiš's two.
# Picking a value a given provider doesn't itself accept (e.g. Rubiš with
# `unlisted`) still surfaces as that provider's own rejection -- same
# reject-if-mismatched-provider handling as `expiry` above.
_PASTE_VISIBILITY_CHOICES = [
    app_commands.Choice(name=v.capitalize(), value=v)
    for v in dict.fromkeys(rubis._VALID_VISIBILITIES + pastebin._VALID_VISIBILITIES)
]


async def _language_autocomplete(
    interaction: discord.Interaction, current: str
) -> List[app_commands.Choice[str]]:
    """Autocomplete callback for /paste's `language` option (wired up via
    @app_commands.autocomplete(language=...) on the `paste` command
    below) -- suggests from api.providers.languages.LANGUAGES as the
    person types, via search_languages(). Suggestions only: Discord's
    autocomplete, unlike @app_commands.choices(), never restricts the
    submitted value to what's suggested, so someone can still type a
    language spelling that isn't in the list at all and have it pass
    through unchanged -- see api.providers.languages' own docstring for
    why that matters here (this list isn't a validated contract, since
    each provider's own accepted spellings can differ)."""
    return [app_commands.Choice(name=name, value=value) for name, value in search_languages(current)]

# Hostnames /url unshorten treats as "this is an is.gd/v.gd link" for its
# provider-specific lookup fallback (see _url_unshorten_impl below) --
# is.gd and v.gd are the same service on two domains (see
# api.providers.is_gd's module docstring), so both count. www.-prefixed
# variants included defensively even though is.gd's own shortened links
# never carry one.
_ISGD_HOSTNAMES = {"is.gd", "www.is.gd", "v.gd", "www.v.gd"}


def _is_isgd_url(url: str) -> bool:
    """True if `url`'s host is is.gd or v.gd -- see _ISGD_HOSTNAMES above.
    Used by _url_unshorten_impl to route a short link this bot has no
    local record of to is.gd's own dedicated Lookup API (see
    api.providers.is_gd.lookup_url()) instead of this bot's generic
    live-redirect-following fallback -- is.gd's own API docs explicitly
    ask consumers to prefer their lookup endpoint over repeatedly visiting
    links live ("cache responses... avoid looking up the same ones
    multiple times"), and the lookup endpoint is both more authoritative
    (no SSRF-guard hop limits or redirect-chain edge cases to worry about)
    and cheaper (one request instead of however many hops the chain has)."""
    from urllib.parse import urlparse

    host = urlparse(url).hostname
    return host is not None and host.lower() in _ISGD_HOSTNAMES


def _provider_label(kind: str, provider: str) -> str:
    """Looks up `provider`'s display label for `kind` from
    api.providers.registry.PROVIDERS, falling back to the bare provider
    key itself (rather than raising) if it's since been removed from the
    registry -- e.g. a stored entry from a provider this bot no longer
    wires up shouldn't make /url unshorten or /url clear's report crash."""
    return PROVIDERS.get(kind, {}).get(provider, {}).get("label", provider)


async def _persist_or_degrade(
    interaction: discord.Interaction,
    *,
    provider: str,
    kind: str,
    short_code: str,
    entry: Dict[str, Any],
    commit_message: str,
    success_description: str,
    success_fields: List[Tuple[str, Any, bool]],
    deletion_url: Optional[str],
):
    """Shared tail end of /url shorten, /paste, and /file: persist the
    entry the provider just created, then reply. If the GitHub write
    fails, the provider call already succeeded by this point -- that
    shouldn't read as the command having failed outright, just that this
    record won't survive a restart, so this degrades to showing the
    deletion_url inline instead (it won't be shown again, and this is the
    one path where it wouldn't otherwise be saved anywhere).

    `deletion_url` is Optional -- several providers in this package never
    hand one back at all (Litterbox, is.gd, free-tier TinyURL; see
    api/providers/registry.py's module docstring for the full list), in
    which case there's nothing to fall back to showing, so that half of
    the warning field is skipped rather than showing a `None`."""
    try:
        await save_shortened_url(provider, kind, short_code, entry, commit_message)
    except GitHubAPIError as e:
        print(f"Failed to persist {provider}/{kind} {short_code} to shortened-urls.json: {e}")
        fields = list(success_fields)
        if deletion_url:
            fields.append((
                "⚠️ Not saved to persistent storage",
                f"Couldn't record this in shortened-urls.json ({e}). "
                f"Save this now if you'll need to delete it later -- it won't be shown again:\n`{deletion_url}`",
                False,
            ))
        else:
            fields.append((
                "⚠️ Not saved to persistent storage",
                f"Couldn't record this in shortened-urls.json ({e}).",
                False,
            ))
        return await send_success(interaction, success_description, fields=fields, ephemeral=True)

    await send_success(interaction, success_description, fields=success_fields, ephemeral=True)


async def _url_shorten_impl(
    interaction: discord.Interaction,
    url: str,
    provider: str,
    alias: Optional[str],
    logstats: Optional[bool],
):
    url = url.strip()
    if not is_valid_url(url):
        return await send_error(interaction, f"`{url}` doesn't look like a valid http(s) URL.")

    provider_info = PROVIDERS["shorten"][provider]
    alias = alias.strip() if alias else None

    # Each of these is exclusive to (at most) one provider -- see
    # registry.py's capability flags -- so a mismatched pick is rejected
    # rather than silently ignored, same "reject/ignore instead of
    # dynamically hiding" reasoning applies to every provider-only extra
    # in this module.
    if alias and not provider_info.get("supports_alias"):
        return await send_error(
            interaction,
            f"{provider_info['label']} doesn't support a custom alias -- leave that option blank, "
            "or pick a provider that does (is.gd or TinyURL).",
        )
    if logstats and not provider_info.get("supports_logstats"):
        return await send_error(
            interaction,
            f"{provider_info['label']} doesn't support `logstats` -- leave that option blank, or pick "
            "is.gd, the only provider with click-statistics logging.",
        )

    # Both the provider call and the GitHub commit that follows are real
    # network round trips that can easily blow Discord's ~3s ack window,
    # so defer before either one. Ephemeral, matching the ephemeral
    # success reply below -- a deferral's ephemeral flag can't be
    # loosened by a later followup, so it has to be decided here.
    await interaction.response.defer(ephemeral=True)

    kwargs: Dict[str, Any] = {}
    if alias:
        kwargs["alias"] = alias
    if logstats:
        kwargs["logstats"] = True

    try:
        result = await provider_info["module"].shorten_url(url, **kwargs)
    except ProviderAPIError as e:
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
        provider=provider,
        kind="shorten",
        short_code=short_code,
        entry=entry,
        commit_message=f"Shortened URL created by {interaction.user} ({interaction.user.id}) via {provider}: {short_code}",
        success_description=f"Shortened: {result['short_url']}",
        success_fields=[("Original", url, False), ("Provider", provider_info["label"], True)],
        deletion_url=result["deletion_url"],
    )


async def _url_paste_impl(
    interaction: discord.Interaction,
    text: Optional[str],
    provider: str,
    language: Optional[str],
    title: Optional[str],
    description: Optional[str],
    access_key: Optional[str],
    visibility: Optional[str],
    expires: Optional[str],
    encrypt: Optional[bool] = None,
    views: Optional[int] = None,
    # Pastebin-exclusive (registry.py's supports_folder=True check below
    # rejects this for any other provider) -- see pastebin.py's own module
    # docstring on api_folder_key.
    folder_key: Optional[str] = None,
    # Alternative to `text` -- its content becomes the paste text instead
    # of a typed STRING option, for content too long or too multi-line
    # for Discord's STRING option model (see MAX_PASTE_TEXT_LENGTH).
    # Exactly one of `text`/`file` must be given; enforced just below.
    file: Optional[discord.Attachment] = None,
    # pastey_gg/pastee_dev-exclusive extra file slots -- see
    # MAX_PASTE_EXTRA_FILES's own comment for why this is a fixed numbered
    # set rather than a real list, and for why each slot is now a single
    # attachment (content/name/language all read from it) rather than a
    # fileN_text/fileN_title/fileN_language trio.
    extra_file_attachments: Optional[List[Optional[discord.Attachment]]] = None,
):
    # Exactly one of `text`/`file` -- there's otherwise nothing to paste
    # (neither given) or an ambiguous choice of source (both given).
    # Checked first, before any provider/capability validation below,
    # same "cheapest, most fundamental check first" ordering as every
    # other exclusivity gate in this module (e.g. /url clear's
    # all/user/before).
    provided = [text is not None, file is not None]
    if sum(provided) == 0:
        return await send_error(
            interaction, "Provide either `text` or `file` -- there's no content to paste otherwise."
        )
    if sum(provided) > 1:
        return await send_error(interaction, "Provide only one of `text` or `file` -- not both.")

    if file is not None and file.size > MAX_PASTE_FILE_ATTACHMENT_SIZE:
        return await send_error(
            interaction,
            f"`{file.filename}` is too large to paste ({file.size:,} bytes -- the limit is "
            f"{MAX_PASTE_FILE_ATTACHMENT_SIZE:,} bytes).",
        )

    language = (language or "plaintext").strip() or "plaintext"
    title = title.strip() if title else None
    description = description.strip() if description else None
    access_key = access_key.strip() if access_key else None
    expires = expires.strip() if expires else None
    folder_key = folder_key.strip() if folder_key else None

    provider_info = PROVIDERS["paste"][provider]
    if access_key and not provider_info.get("supports_access_key"):
        return await send_error(
            interaction,
            f"{provider_info['label']} doesn't accept a per-call `access_key` -- leave that option blank, "
            "or pick a provider that does (pastee.dev, pastey.gg, or Pastebin).",
        )
    if visibility and not provider_info.get("supports_visibility"):
        return await send_error(
            interaction,
            f"{provider_info['label']} doesn't support `visibility` -- leave that option blank, or pick "
            "Rubiš or Pastebin, the providers that do.",
        )
    if expires and not provider_info.get("supports_expires"):
        return await send_error(
            interaction,
            f"{provider_info['label']} doesn't support `expires` -- leave that option blank, or pick "
            "pastee.dev, pastey.gg, or Pastebin, the only providers that do.",
        )
    if encrypt and not provider_info.get("supports_encrypt"):
        return await send_error(
            interaction,
            f"{provider_info['label']} doesn't support `encrypt` -- leave that option blank, or pick "
            "pastee.dev, the only provider that does.",
        )
    if views and not provider_info.get("supports_views"):
        return await send_error(
            interaction,
            f"{provider_info['label']} doesn't support `views` -- leave that option blank, or pick "
            "pastey.gg, the only provider that does.",
        )
    if folder_key and not provider_info.get("supports_folder"):
        return await send_error(
            interaction,
            f"{provider_info['label']} doesn't support `folder_key` -- leave that option blank, or pick "
            "Pastebin, the only provider that does.",
        )

    # Any file1-file4 slot given at all counts as "extra files requested"
    # -- checked before the capability gate immediately after, same
    # reasoning as `views` above: every other provider in this package
    # still only ever sends the one primary file (registry.py's
    # supports_extra_files=False for those -- see pastey_gg.py's module
    # docstring, point 4, and pastee_dev.py's create_paste() `extra_files`
    # docstring for the two providers where it's True).
    extra_file_attachments = [a for a in (extra_file_attachments or []) if a is not None]
    if extra_file_attachments and not provider_info.get("supports_extra_files"):
        return await send_error(
            interaction,
            f"{provider_info['label']} doesn't support attaching extra files -- leave `file1` "
            "(and file2/3/4) blank, or pick pastee.dev or pastey.gg, the only providers that do.",
        )

    # e-z.host-exclusive within /paste (registry.py's requires_title=True
    # -- confirmed 2026-08-17, live; see ez_host.py's module docstring's
    # "paste endpoint" note): unlike every other provider here, a blank
    # `title`/`description` isn't just ignored, it's rejected outright --
    # and, before this was confirmed, rejected via e-z.host's own broken,
    # non-JSON error-handler crash rather than a normal validation
    # message. Caught here, before the interaction is even deferred,
    # same as every other provider-capability mismatch above -- no need
    # to burn a deferred round trip on a request that was always going
    # to fail. `title` alone gets an exception for `file`: when `file` is
    # given instead of `text`, its filename becomes `title` further down
    # (after the defer below) even if this option itself was left blank,
    # so only reject here when neither `title` nor `file` could supply
    # one.
    if provider_info.get("requires_title") and not title and file is None:
        return await send_error(
            interaction,
            f"{provider_info['label']} requires a `title` for every paste -- provide one, attach `file` "
            "instead (its filename becomes the title), or pick a different provider.",
        )
    if provider_info.get("requires_description") and not description:
        return await send_error(
            interaction,
            f"{provider_info['label']} requires a `description` for every paste -- provide one, or pick "
            "a different provider.",
        )

    for attachment in extra_file_attachments:
        if attachment.size > MAX_PASTE_FILE_ATTACHMENT_SIZE:
            return await send_error(
                interaction,
                f"`{attachment.filename}` is too large to paste ({attachment.size:,} bytes -- the limit is "
                f"{MAX_PASTE_FILE_ATTACHMENT_SIZE:,} bytes).",
            )

    # pastey.gg's `expires_at` needs an absolute RFC3339 timestamp (see
    # pastey_gg.py's create_paste() docstring), not pastee.dev's own
    # relative-duration mini-language `expires` otherwise passes through
    # as-is -- reuses /url clear's own `before` parser for the same
    # flexible relative/absolute input, so a value that can't be
    # understood at all is caught here, before the interaction is even
    # deferred, same as every check above. Pastebin's own `expire_date` is
    # a third, different shape still (a small fixed 9-code enum, not a
    # timestamp or a free-form duration) -- validated inside
    # pastebin.create_paste() itself instead, via the kwargs routing a bit
    # further down, rather than here.
    expires_at: Optional[str] = None
    if expires and provider == "pastey_gg":
        parsed_expiry = parse_time_filter(expires, future=True)
        if parsed_expiry is None:
            return await send_error(
                interaction,
                f"`{expires}` isn't a time pastey.gg's `expires_at` can use -- try a relative "
                "duration (e.g. `3 days`), an absolute date/time, or a UTC ISO-8601 timestamp.",
            )
        expires_at = format_iso(parsed_expiry)

    # Same defer-first, ephemeral-from-the-start reasoning as /url shorten.
    await interaction.response.defer(ephemeral=True)

    # Downloading from Discord's CDN is itself a network round trip --
    # same reasoning as /url file's own `content = await file.read()` --
    # so this only happens now, after the defer above, not any earlier.
    if file is not None:
        try:
            file_bytes = await file.read()
        except discord.HTTPException as e:
            return await send_error(interaction, f"Failed to download `{file.filename}`: {e}")
        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            return await send_error(
                interaction,
                f"Couldn't read `{file.filename}` as UTF-8 text ({e}). `/paste`'s `file` option only "
                "supports plain-text files (source code, markdown, JSON, config files, etc.), not binaries.",
            )
        if not text:
            return await send_error(interaction, f"`{file.filename}` is empty -- there's nothing to paste.")
        # pastey.gg's primary file `name` comes from `title` alone
        # (create_paste()'s own docstring: "`title` becomes the primary
        # file's `name`") -- with nothing else to draw a name from here,
        # an explicit `title` still wins, but a `file` upload with no
        # `title` typed would otherwise reach pastey.gg as an unnamed
        # primary file even though the attachment's own filename was
        # right there. Same fallback the file1-file4 extra files already
        # get from their own attachments a few lines below.
        if title is None:
            title = file.filename

    # Built regardless of provider (empty when no slot was given, or when
    # the provider doesn't support this at all) -- pastey_gg.create_paste()
    # and pastee_dev.create_paste() both only ever see this via
    # kwargs["extra_files"] below, and only when it's actually non-empty.
    # Each attachment's own filename doubles as both `name` (pastey.gg's
    # per-file title, pastee.dev's per-section name) and the input to
    # language_for_filename() (see that function's own docstring) since
    # file1-file4 no longer have separate fileN_title/fileN_language
    # options to source those from.
    extra_files: List[Dict[str, Optional[str]]] = []
    for attachment in extra_file_attachments:
        try:
            extra_bytes = await attachment.read()
        except discord.HTTPException as e:
            return await send_error(interaction, f"Failed to download `{attachment.filename}`: {e}")
        try:
            extra_text = extra_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            return await send_error(
                interaction,
                f"Couldn't read `{attachment.filename}` as UTF-8 text ({e}). Extra paste files must be "
                "plain text too, not binaries.",
            )
        if not extra_text:
            return await send_error(
                interaction, f"`{attachment.filename}` is empty -- there's nothing to attach as an extra file."
            )
        extra_files.append({
            "content": extra_text,
            "name": attachment.filename,
            "language": language_for_filename(attachment.filename),
        })

    kwargs: Dict[str, Any] = {}
    if access_key:
        kwargs["access_key"] = access_key
    if visibility:
        kwargs["visibility"] = visibility
    if expires_at:
        kwargs["expires_at"] = expires_at
    elif expires and provider == "pastebin":
        # Pastebin-exclusive: a small fixed 9-code enum (see
        # pastebin.EXPIRE_DATE_HELP), a different shape from pastee_dev's
        # own free-form `expiration` below -- routes to
        # pastebin.create_paste's own `expire_date` kwarg, which validates
        # it against that fixed set itself (see this module's own comment
        # a bit further up, and pastebin.py's create_paste() docstring).
        kwargs["expire_date"] = expires
    elif expires:
        kwargs["expiration"] = expires
    if folder_key:
        # Pastebin-exclusive (registry.py's supports_folder=True check
        # above already rejected this for any other provider) -- routes
        # straight to pastebin.create_paste's own `folder_key` kwarg,
        # which itself re-checks that an api_user_key is actually
        # available (see that function's docstring).
        kwargs["folder_key"] = folder_key
    if views:
        # pastey_gg-exclusive (registry.py's supports_views=True check
        # above already rejected this for any other provider) -- maps to
        # pastey_gg.create_paste's own `remaining_views` kwarg, matching
        # Echo's own field name (see pastey_gg.py's module docstring,
        # point 2).
        kwargs["remaining_views"] = views
    if encrypt:
        # pastee.dev-exclusive (registry.py's supports_encrypt=True check
        # above already rejected this for any other provider), so there's
        # no cross-provider name to reconcile the way `expires` above
        # has to -- routes straight to pastee_dev.create_paste's own
        # `encrypted` kwarg. See that function's docstring for exactly
        # what this flag does and doesn't cover.
        kwargs["encrypted"] = True
    if extra_files:
        kwargs["extra_files"] = extra_files

    try:
        result = await provider_info["module"].create_paste(
            text, language=language, title=title, description=description, **kwargs
        )
    except PasteeDevAPIError as e:
        # pastee.dev's `expiration` isn't a fixed dropdown of choices --
        # it's a small set of accepted *shapes* (see pastee_dev.py's own
        # docstring) -- but "give the user the list of available
        # expiration times" is still doable: show that shape guide as an
        # embed instead of just relaying pastee.dev's bare validation
        # message. e.fields comes from pastee.dev's own {"errors": [...]}
        # response (see PasteeDevAPIError's docstring), so this only fires
        # for the specific field that actually failed, not e.g. a bad
        # `syntax` value tripping the same branch.
        if "expiration" in e.fields:
            formats = "\n".join(f"**{label}** -- {desc}" for label, desc in pastee_dev.EXPIRATION_HELP)
            return await send_error(
                interaction,
                f"`{expires}` isn't an expiration format pastee.dev accepts.",
                title="Invalid Expiration",
                fields=[
                    ("Accepted formats", formats, False),
                    ("Note", pastee_dev.EXPIRATION_HELP_NOTE, False),
                ],
            )
        return await send_error(interaction, f"Failed to create that paste: {e}")
    except PastebinAPIError as e:
        # Same "show the accepted-value guide instead of just relaying the
        # bare rejection" treatment as PasteeDevAPIError above, for
        # Pastebin's own `expire_date` -- unlike pastee_dev's `expiration`
        # though, Pastebin's set is a small, fixed, fully-known enum (see
        # pastebin.EXPIRE_DATE_HELP), so this is really just formatting
        # that same table for display, not deriving anything new.
        if "api_paste_expire_date" in e.fields:
            formats = "\n".join(f"**{code}** -- {label}" for code, label in pastebin.EXPIRE_DATE_HELP)
            return await send_error(
                interaction,
                f"`{expires}` isn't an expiration code Pastebin accepts.",
                title="Invalid Expiration",
                fields=[("Accepted codes", formats, False)],
            )
        return await send_error(interaction, f"Failed to create that paste: {e}")
    except ProviderAPIError as e:
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
    if encrypt:
        entry["encrypted"] = True
    if extra_files:
        entry["file_count"] = 1 + len(extra_files)
    if folder_key:
        entry["folder_key"] = folder_key

    fields = [
        ("Paste", result["paste_url"], False),
        ("Raw", result["raw_url"], False),
        ("Provider", provider_info["label"], True),
    ]
    if encrypt:
        fields.append(("Encrypted", "Yes", True))
    if extra_files:
        fields.append(("Files", str(1 + len(extra_files)), True))
    if folder_key:
        # Pastebin-exclusive (registry.py's supports_folder=True check
        # earlier already rejected this for any other provider) -- surfaces
        # the folder key the caller passed in, not a folder *name* (Pastebin's
        # own API never hands back a human-readable name for it anywhere in
        # this response).
        fields.append(("Folder", folder_key, True))
    if title:
        fields.insert(0, ("Title", title, False))
    if access_key and provider == "pastey_gg":
        # pastey.gg's own backend (Echo, routes/pastes.go's
        # fetchPasteOptions()) authenticates a password-protected paste's
        # GET/raw/file routes *exclusively* via the `Authorization`
        # request header (the raw password value, no "Bearer" prefix) --
        # confirmed straight from Echo's own source, same standard as
        # pastey_gg.py's own module docstring. There is no query-string
        # equivalent anywhere in that function (no `?password=`, nothing
        # comparable), so a bare `raw_url` link -- the only thing Discord
        # or a browser can actually click -- can never carry it: neither
        # can add a custom header to a plain link click. That 401 isn't
        # something this bot's request shape can fix; it's how pastey.gg
        # itself works.
        #
        # Two commands, not one: whoever reads this field's shell is
        # unknown, and `curl` isn't a safe universal answer -- Windows
        # PowerShell aliases `curl` to `Invoke-WebRequest`, which has an
        # entirely different parameter set (`-Headers <hashtable>`, not
        # `-H "k: v"`) and fails with a binding error on the curl-style
        # invocation rather than falling back to real curl.exe. Giving
        # only the curl form here would silently break for exactly the
        # audience most likely to be running this from a terminal at all.
        fields.append((
            "Fetching Raw Content (password-protected)",
            "The `Raw` link above needs the password sent as an `Authorization` header -- "
            "pastey.gg has no way to accept it in the URL itself, so clicking the link "
            "directly will 401.\n\n"
            "**curl / cmd.exe / macOS / Linux:**\n"
            f"```\ncurl -H \"Authorization: {access_key}\" {result['raw_url']}\n```\n"
            "**PowerShell** (its own `curl` is aliased to `Invoke-WebRequest`, which doesn't take `-H`):\n"
            f"```\nInvoke-RestMethod -Uri \"{result['raw_url']}\" -Headers @{{ Authorization = \"{access_key}\" }}\n```",
            False,
        ))

    await _persist_or_degrade(
        interaction,
        provider=provider,
        kind="paste",
        short_code=short_code,
        entry=entry,
        commit_message=f"Paste created by {interaction.user} ({interaction.user.id}) via {provider}: {short_code}",
        success_description="Paste created.",
        success_fields=fields,
        deletion_url=result["deletion_url"],
    )


async def _url_file_impl(
    interaction: discord.Interaction,
    file: discord.Attachment,
    provider: str,
    expiry: Optional[str],
):
    """/file's shared implementation. Every provider gets exactly one real
    upload attempt here, gated first by this function's own image/audio/
    application content-type check -- except a provider with registry.py's
    skips_type_gate=True (today, just Uguu), which skips that check
    entirely and lets the provider's own API be the sole judge of what it
    accepts. See api.providers.registry's own skips_type_gate docstring
    for why Uguu specifically needed that: its real accepted/rejected set
    doesn't line up cleanly with this gate's image/audio/application
    assumption in either direction, and there's no reliable list to mirror
    it with locally -- so its own API's rejection message (already
    surfaced cleanly via uguu.py's _describe_error()) is what the user
    sees instead of a guess made here."""
    provider_info = PROVIDERS["file"][provider]

    if not provider_info.get("skips_type_gate") and file.content_type and not file.content_type.startswith(
        _ALLOWED_FILE_CONTENT_TYPE_PREFIXES
    ):
        return await send_error(
            interaction,
            f"`{file.filename}` isn't an image, audio, or application file ({file.content_type}). "
            f"{provider_info['label']} may still reject other types of content that slip past this check.",
        )
    if file.size > MAX_FILE_ATTACHMENT_SIZE:
        return await send_error(
            interaction,
            f"`{file.filename}` is too large to upload ({file.size:,} bytes -- the limit is "
            f"{MAX_FILE_ATTACHMENT_SIZE:,} bytes).",
        )

    expiry = expiry.strip() if expiry else None
    if expiry and not provider_info.get("requires_expiry"):
        return await send_error(
            interaction,
            f"{provider_info['label']} doesn't use an `expiry` -- leave that option blank. Litterbox is the "
            "only provider here with a choice of duration; Uguu always expires on its own fixed 3-hour "
            "window, and Catbox/E-Z don't expire at all.",
        )
    if provider_info.get("requires_expiry") and not expiry:
        expiry = _LITTERBOX_DEFAULT_EXPIRY

    # Downloading the attachment from Discord's CDN is itself a network
    # round trip, on top of the provider upload and GitHub commit that
    # follow -- defer before any of them, same ephemeral-from-the-start
    # reasoning as /url shorten and /paste.
    await interaction.response.defer(ephemeral=True)

    try:
        content = await file.read()
    except discord.HTTPException as e:
        return await send_error(interaction, f"Failed to download that attachment: {e}")

    kwargs = {"expiry": expiry} if expiry else {}
    try:
        result = await provider_info["module"].upload_file(file.filename, content, file.content_type, **kwargs)
    except ProviderAPIError as e:
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
        provider=provider,
        kind="file",
        short_code=short_code,
        entry=entry,
        commit_message=f"File uploaded by {interaction.user} ({interaction.user.id}) via {provider}: {short_code}",
        success_description=f"Uploaded: {result['file_url']}",
        success_fields=[
            ("Original filename", file.filename, False),
            ("Provider", provider_info["label"], True),
        ],
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
# description -- e.g. "Found in the local store (paste, via TinyURL)."
_KIND_LABELS = {"shorten": "shortened link", "paste": "paste", "file": "file upload"}


def _fields_for_store_entry(kind: str, provider: str, entry: Dict[str, Any]) -> List[Tuple[str, Any, bool]]:
    """Builds /url unshorten's success-embed fields for a
    storage/shortened-urls.json entry found locally. Shape differs per
    kind -- a "shorten" entry, a "paste", and a "file" upload each carry
    different fields (see api/github.py's schema comment) -- but
    provider/creator/created_at are common to all three, so those are
    appended once at the end rather than repeated in each branch."""
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

    fields.append(("Provider", _provider_label(kind, provider), True))
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
    # it -- across every provider, not just one. extract_short_code() is
    # generic across shorten/paste/file URLs regardless of provider (see
    # api/providers/util.py), and find_shortened_url_entry() checks every
    # provider and kind in one fetch.
    short_code = extract_short_code(url)
    store_error: Optional[GitHubAPIError] = None
    try:
        found = await find_shortened_url_entry(short_code)
    except GitHubAPIError as e:
        found = None
        store_error = e

    if found is not None:
        provider, kind, entry = found
        return await send_success(
            interaction,
            f"Found in the local store ({_KIND_LABELS.get(kind, kind)}, via {_provider_label(kind, provider)}).",
            fields=_fields_for_store_entry(kind, provider, entry),
        )

    # Not (or not confirmably, if store_error is set) in this bot's own
    # store. For an is.gd/v.gd link specifically, prefer is.gd's own
    # dedicated Lookup API over the generic live-redirect-follow fallback
    # below -- see _is_isgd_url()'s docstring for why (it's what is.gd's
    # own API docs ask consumers to use, and it's both more authoritative
    # and cheaper than this bot manually following the redirect chain
    # itself). This applies regardless of whether this bot created the
    # link -- is.gd's lookup endpoint works for any is.gd/v.gd link, not
    # just this bot's own.
    if _is_isgd_url(url):
        try:
            destination = await is_gd.lookup_url(url)
        except IsGdAPIError as e:
            return await send_error(interaction, f"Couldn't look up that is.gd link: {e}")

        fields: List[Tuple[str, Any, bool]] = [("Final destination", destination, False)]
        if store_error is not None:
            fields.append((
                "⚠️ Couldn't check the local store first",
                f"{store_error} -- the result above is from is.gd's own Lookup API, not from "
                "shortened-urls.json.",
                False,
            ))
        return await send_success(interaction, f"Resolved via is.gd's Lookup API: {destination}", fields=fields)

    # Not an is.gd/v.gd link either -- fall back to actually following the
    # redirect chain live. SSRF-guarded per hop; see api.ssrf_guard /
    # api.redirect_resolver for why and how.
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
    any of the three, rather than being a fourth mode of its own. Not
    provider-filterable -- /url clear always sweeps every provider's
    namespace, matching find_matching_shortened_urls()/
    clear_shortened_urls()'s own already-multi-provider-generic design in
    api/github.py."""
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
    kind label, the provider that created it, the short code, whichever
    URL field that kind actually has (shorten/paste/file each name it
    differently -- see _fields_for_store_entry() above for the full
    per-kind shape), who created it, and when."""
    label = _KIND_LABELS.get(kind, kind)
    url_value = entry.get("shortened_url") or entry.get("paste_url") or entry.get("file_url") or "N/A"
    creator_id = entry.get("creator_id")
    mention = f"<@{creator_id}>" if creator_id else "Unknown"
    return (
        f"`{short_code}` **({label} · {_provider_label(kind, provider)})** {url_value} — {mention} — "
        f"{_format_created_at(entry.get('created_at'))}"
    )


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
                "them, across every provider; it does not delete the underlying links/pastes/files from "
                "whichever provider actually created them. This action cannot be undone."
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

    @url_group.command(name="shorten", description="Shortens a link.")
    @app_commands.describe(
        url="The URL to shorten",
        provider="Which service to shorten it with. Default: E-Z.",
        alias="Optional custom alias/slug -- only for providers that support one (is.gd, TinyURL).",
        logstats="Optional -- log click statistics for this link (is.gd only, viewable on is.gd's site).",
    )
    @app_commands.choices(provider=choices_for("shorten"))
    # 3 per 60s per user -- generous enough for normal use, tight enough
    # that a slip of the enter key (or someone poking at it) can't spray
    # requests at a provider or rack up GitHub commits.
    #@app_commands.checks.cooldown(3, 60.0)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def url_shorten(
        self,
        interaction: discord.Interaction,
        url: str,
        provider: Optional[app_commands.Choice[str]] = None,
        alias: Optional[app_commands.Range[str, 1, 30]] = None,
        logstats: Optional[bool] = None,
    ):
        await _url_shorten_impl(
            interaction, url, provider.value if provider else DEFAULT_PROVIDER, alias, logstats,
        )

    @url_group.command(name="unshorten", description="Resolves a short link back to its original destination.")
    @app_commands.describe(url="The short/redirecting URL to resolve.")
    # Same 3-per-60s convention as the other /url subcommands -- the
    # live-redirect fallback in particular is the one path here that
    # makes outbound requests to a URL a user supplied, so this also
    # doubles as a light brake on that.
    #@app_commands.checks.cooldown(3, 60.0)
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
    #@app_commands.checks.cooldown(1, 30.0)
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

    @app_commands.command(name="paste", description="Creates a text paste.")
    @app_commands.describe(
        text=f"The text content to paste (up to {MAX_PASTE_TEXT_LENGTH} chars, all providers). Provide this or `file`, not both.",
        file="Text file for long/multi-line content -- use this or `text`, not both (all providers).",
        provider="Which service to paste it to. Default: E-Z.",
        language="Syntax highlighting -- start typing for suggestions. Rubiš only: maps to Plain/Code/Markdown.",
        title="Title, all providers. E-Z requires this or `file` (its filename becomes the title). Pastebin: optional.",
        description="Description, all providers except pastey.gg and Pastebin (silently ignored, neither has a description field). Required by E-Z.",
        access_key="Optional -- pastee.dev or Pastebin: use your own account instead of this bot's default (Pastebin: otherwise an anonymous guest paste). pastey.gg: sets a password to view this paste.",
        visibility="Optional -- who can find this paste. Rubiš: `public` (default) or `private`. Pastebin: `public` (default), `unlisted`, or `private` (private needs access_key/a configured user key).",
        expires="Optional -- when this paste expires. pastee.dev, pastey.gg, or Pastebin only -- see its own error for accepted formats/codes.",
        encrypt="Optional -- pastee.dev only. Marks the paste as encrypted (see pastee.dev's own docs).",
        views=f"Optional -- delete this paste after this many views ({pastey_gg.REMAINING_VIEWS_MIN}-{pastey_gg.REMAINING_VIEWS_MAX}). pastey.gg only.",
        folder_key="Optional -- Pastebin only. Needs the folder's actual key (not its display name!) from Pastebin's site -- see /paste's own docs. Needs access_key/a configured user key.",
        file1="Optional -- a 2nd file for this paste. Its filename sets the title and language. pastee.dev or pastey.gg only.",
        file2="Optional -- a 3rd file for this paste. Its filename sets the title and language. pastee.dev or pastey.gg only.",
        file3="Optional -- a 4th file for this paste. Its filename sets the title and language. pastee.dev or pastey.gg only.",
        file4="Optional -- a 5th file for this paste. Its filename sets the title and language. pastee.dev or pastey.gg only.",
    )
    @app_commands.choices(provider=choices_for("paste"), visibility=_PASTE_VISIBILITY_CHOICES)
    @app_commands.autocomplete(language=_language_autocomplete)
    #@app_commands.checks.cooldown(3, 60.0)
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def paste(
        self,
        interaction: discord.Interaction,
        text: Optional[app_commands.Range[str, 1, MAX_PASTE_TEXT_LENGTH]] = None,
        file: Optional[discord.Attachment] = None,
        provider: Optional[app_commands.Choice[str]] = None,
        language: Optional[app_commands.Range[str, 1, 50]] = None,
        title: Optional[app_commands.Range[str, 1, 100]] = None,
        description: Optional[app_commands.Range[str, 1, 200]] = None,
        access_key: Optional[str] = None,
        visibility: Optional[app_commands.Choice[str]] = None,
        expires: Optional[app_commands.Range[str, 1, 40]] = None,
        encrypt: Optional[bool] = None,
        views: Optional[app_commands.Range[int, pastey_gg.REMAINING_VIEWS_MIN, pastey_gg.REMAINING_VIEWS_MAX]] = None,
        # Pastebin-exclusive (api_folder_key) -- see _url_paste_impl's own
        # folder_key parameter comment and pastebin.py's module docstring.
        # No Range: same "opaque key string, no fixed length documented"
        # reasoning as access_key above.
        folder_key: Optional[str] = None,
        # pastey_gg/pastee_dev-exclusive extra file slots
        # (MAX_PASTE_EXTRA_FILES == 4 of these) -- each is one attachment
        # covering that slot's content, title (its filename), and
        # language (guessed from its filename -- see
        # api.providers.languages.language_for_filename). Gated/validated
        # in _url_paste_impl, not here; this command only packages the raw
        # attachments into extra_file_attachments below.
        file1: Optional[discord.Attachment] = None,
        file2: Optional[discord.Attachment] = None,
        file3: Optional[discord.Attachment] = None,
        file4: Optional[discord.Attachment] = None,
    ):
        await _url_paste_impl(
            interaction, text, provider.value if provider else DEFAULT_PROVIDER, language, title, description,
            access_key, visibility.value if visibility else None, expires, encrypt, views,
            folder_key=folder_key,
            file=file,
            extra_file_attachments=[file1, file2, file3, file4],
        )

    @app_commands.command(name="file", description="Uploads an image, audio, or application file.")
    @app_commands.describe(
        file="The file to upload -- image, audio, or application (PNG, JPG, MP3, WAV, ZIP, PDF, etc.).",
        provider="Which service to upload it to. Default: E-Z. Uguu = temp hosting, fixed 3h expiry.",
        expiry="How long the file lasts -- Litterbox only. Defaults to 1h. (Uguu auto-expires in 3h instead.)",
    )
    @app_commands.choices(provider=choices_for("file"), expiry=_LITTERBOX_EXPIRY_CHOICES)
    #@app_commands.checks.cooldown(3, 60.0)
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def file(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        provider: Optional[app_commands.Choice[str]] = None,
        expiry: Optional[app_commands.Choice[str]] = None,
    ):
        await _url_file_impl(
            interaction, file, provider.value if provider else DEFAULT_PROVIDER, expiry.value if expiry else None
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Url(bot))
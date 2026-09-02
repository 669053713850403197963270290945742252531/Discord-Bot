"""
api.providers.pastey_gg -- async wrapper around pastey.gg
(https://pastey.gg), for /paste's `pastey_gg` provider choice.

Confirmed 2026-08-16 against Pastey.gg's own published source, same
standard as this module's original rewrite (a live account test can't
show what a service rejects and why; the server's own code can) --
re-pulled fresh rather than trusting the previous pull's notes, to add
four things that weren't implemented yet: password (reusing
`access_key`, not a new option, as asked), `remaining_views`,
`expires_at`, and multi-file pastes. Also pulled labstack/echo v5.1.1
itself (Echo's web framework, per go.mod) to settle exactly what a
framework-level error looks like on the wire, as opposed to one of
Echo-the-app's own `c.JSON(...)` calls -- see "On error handling" below.

1. **Password reuses `access_key` -- no new option, exactly as asked.**
   models.CreatePaste.Password (models/pastes.go) is `*string`, and
   database/postgres.go + database/memory.go both treat a nil OR
   empty-string Password identically to "no password" (bcrypt-hashing is
   skipped entirely: `if cp.Password != nil && *cp.Password != ""`). So
   create_paste() below only ever sets the `password` key in the request
   body when `access_key` is a non-empty string -- sending an explicit
   empty string would've been harmless anyway given that check, but
   omitting the key entirely is clearer. Nothing about this is
   pastey.gg-specific plumbing on this bot's side; it's `access_key`'s
   existing meaning (registry.py's supports_access_key) pointed at a
   different request field than pastee.dev's own `access_key` (an actual
   API auth token) points at.

2. **`remaining_views` is real and range-checked server-side.**
   validatePaste() (routes/pastes.go) rejects it outright -- HTTP 400,
   `{"error": "remaining_views must be between 1 and 1000."}` -- for
   anything outside 1-1000 inclusive (REMAINING_VIEWS_MIN/MAX below).
   create_paste() re-checks that same range locally before ever making
   the request, purely to fail instantly with the same message instead
   of waiting on a round trip for something already known to fail --
   unlike pastee_dev.py's local `expiration` check, this isn't working
   around unreliable server-side validation (Echo's really does reject
   it here), just skipping the network round trip.

3. **`expires_at` is a real, stored `*time.Time` -- but its format
   matters more than it looks.** models.CreatePaste.ExpiresAt
   (models/pastes.go) unmarshals via Go's stock `encoding/json`, which
   for `*time.Time` means strict RFC3339 (a timezone offset -- `Z` or
   `+hh:mm` -- is mandatory, not optional). The previous version of this
   docstring flagged this shape as genuinely unknown; it isn't anymore,
   but the failure mode is worse than a plain validation error:
   routes/pastes.go's createPaste() calls `c.Bind(&data)` *before*
   validatePaste() ever runs, so a value Go's JSON decoder can't parse as
   RFC3339 (a bare date, or a datetime with no offset) never reaches
   validatePaste() at all -- it fails at Bind() and comes back as the
   generic `{"error": "Invalid JSON."}`, which doesn't name `expires_at`
   as the problem. So this is validated locally too, via
   `datetime.fromisoformat` (Python 3.11+, needed since that's the
   version that accepts a trailing `Z`) -- a value with no offset at all
   is treated as UTC and has `Z` appended rather than rejected.
   commands/url.py hands this a `parse_time_filter()` result already run
   through `format_iso()` (always UTC-`Z`-suffixed), so the no-offset
   case mainly matters for a caller using create_paste() directly.

4. **Multi-file pastes are a real, already-working part of the exact
   schema this module already used for its single file.**
   CreatePaste.Files (models/pastes.go) is `[]CreateFile` with no
   hardcoded length-1 assumption anywhere in Echo; routes/pastes.go's own
   validatePaste() only caps it at `ctx.Config.Pastes.MaxFiles` (5 in the
   self-hosted config.example.yaml this repo ships -- *not* confirmed to
   be api.pastey.gg's actual production value, since that's
   admin-configured and nothing in the response ever states it).
   `extra_files` below appends further entries to the same `files` array
   `text`/`title`/`language` already build the first entry from. Wired
   into /paste's own Discord options (commands/url.py) via the
   `supports_extra_files` registry flag: a fixed set of numbered
   `file1`-`file4` attachment slots (Discord's option model has no
   variable-length list, so a bounded, numbered-slot UI is the standard
   workaround), each optional and independently skippable -- each
   attachment's own filename supplies that slot's `name` and (guessed
   via api.providers.languages.language_for_filename) `language`, since
   there's no separate per-slot title/language option anymore. Capped at
   4 extra slots (5 files total) purely as a Discord-side UI choice,
   chosen to match the self-hosted `MaxFiles` default two sentences up --
   see commands/url.py's own MAX_PASTE_EXTRA_FILES for why that slot
   count is never enforced locally as a hard rejection the way
   `remaining_views`/`expires_at` are: it isn't a confirmed limit for
   api.pastey.gg's actual production instance, so a production
   `MaxFiles` lower than this bot's own slot count still surfaces
   correctly, as validatePaste()'s own rejection message on the request
   that finally goes out. pastee_dev.py's own API accepts multiple
   `sections` per paste too, but its create_paste() still only ever
   builds one (see that module) -- pastey_gg is the one /paste provider
   in this package whose Discord command actually exposes its API's own
   multi-file support.

Everything the original rewrite already confirmed is unchanged and
re-confirmed against the same fresh source pull: BASE_URL having no
spurious `/v1`, no account/API-key concept for *creating* a paste, the
response shape including `safety_token` as the actual `deletion_url`
value (sent back as `X-Safety-Token` to delete/modify later), and
raw_url's `/pastes/<id>/raw` shape.

**On error handling** ("catch every documented status"): POST /pastes
itself (routes/pastes.go's createPaste() -- its own godoc only documents
201/400/500) can only ever produce three shapes from this bot's side:
201 success; 400 -- either validatePaste()'s own `{"error": "..."}` or
the Bind()-stage `{"error": "Invalid JSON."}` from point 3 above; and
500's `{"error": "Internal server error."}`. 401/404/409 are real Echo
statuses too, but only ever come from the *other* paste routes (fetch,
delete) -- createPaste() has no path to any of them, so PasteyGGAPIError
below doesn't invent handling for statuses this endpoint can't produce.

The one status not in createPaste()'s own godoc that can still happen
here is 429: MiddlewareView's rate limiter (routes/middleware.go) wraps
every route globally via `v.ctx.Server.Use`, *if* the deployment's
config.yaml defines a limit for `POST /pastes` specifically
(config.example.yaml's own `ratelimits` list ships with no concrete
example filled in, so an active limit on api.pastey.gg specifically
can't be confirmed either way -- just confirmed possible). That 429 is
`echo.NewHTTPError(429, "...")`, which is Echo-the-*framework's* own
error, not one of Echo-the-*app's* `c.JSON(...)` calls -- pulling
labstack/echo v5.1.1 itself confirms `e.HTTPErrorHandler` defaults to
`DefaultHTTPErrorHandler(false)` (echo.go's own `New()`, never overridden
anywhere in this app's source), which puts that message under a
**`message`** key, not `error`: `{"message": "You are requesting too
fast. Check headers for ratelimit information."}`. A naive
`data.get("error")` would silently return None for this one specific
status and fall through to a blank message, so this module checks both
keys, `error` first (the more common shape here).
"""

from datetime import datetime
from typing import Dict, List, Optional

import aiohttp

from api.providers.errors import ProviderAPIError
from api.providers.util import describe_network_error as _describe_network_error
from api.providers.util import get_session as _get_session_shared

BASE_URL = "https://api.pastey.gg"

_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Confirmed 2026-08-16 (routes/pastes.go's validatePaste()) -- the exact
# inclusive bounds Echo itself enforces for `remaining_views`. Re-checked
# locally in create_paste() below before the request goes out; see this
# module's docstring, point 2.
REMAINING_VIEWS_MIN = 1
REMAINING_VIEWS_MAX = 1000


class PasteyGGAPIError(ProviderAPIError):
    """Raised whenever a pastey.gg call fails outright (a non-2xx status
    this bot can actually receive -- see this module's docstring's "On
    error handling" section for exactly which those are), an unreachable
    host, a non-JSON body, or a 2xx response missing the one field this
    module actually depends on (`id`).

    `fields` -- set only for the two failures this module catches and
    raises locally *before* a request ever goes out (a `remaining_views`
    out of REMAINING_VIEWS_MIN..MAX, or an `expires_at` that doesn't
    parse as RFC3339) -- see this module's docstring, points 2 and 3.
    Empty for every failure that instead came back from pastey.gg itself,
    since its own {"error"/"message": "..."} shapes never name which
    field was the problem (unlike pastee_dev.py's structured
    {"errors": [{"field", ...}]})."""

    def __init__(self, message: str, *, fields: Optional[List[str]] = None):
        super().__init__(message)
        self.fields = fields or []


async def _get_session(session: Optional[aiohttp.ClientSession]):
    return await _get_session_shared(session, _TIMEOUT)


def _normalize_language(language: Optional[str]) -> Optional[str]:
    """Shared by the primary file and every `extra_files` entry: leaves
    `language` as-is (Pastey.gg has no fixed language enum found in
    Echo's source -- see this module's docstring), except for this bot's
    own "text"/"plaintext" default sentinel (or nothing at all), which is
    left unset rather than sent as a fake language name."""
    if not language or language.strip().lower() in ("text", "plaintext"):
        return None
    return language


def _normalize_expires_at(value: str) -> str:
    """Validates `value` locally as RFC3339 before it's ever sent -- see
    this module's docstring, point 3, for why pastey.gg's own failure
    mode for a bad value (a content-free `{"error": "Invalid JSON."}`) is
    worse than catching this here. A value with no timezone offset at all
    is treated as UTC (`Z` appended) rather than rejected, since that's
    almost always what's meant by e.g. "2026-12-31T23:59:59" on its own.
    Raises PasteyGGAPIError(fields=["expires_at"]) for anything that
    still doesn't parse as a datetime at all."""
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise PasteyGGAPIError(
            f"'{value}' isn't an RFC3339 timestamp pastey.gg accepts for `expires_at` -- "
            "try e.g. `2026-12-31T23:59:59Z`.",
            fields=["expires_at"],
        )
    if parsed.tzinfo is None:
        candidate = f"{candidate}Z"
    return candidate


async def create_paste(
    text: str,
    *,
    language: str = "text",
    title: Optional[str] = None,
    description: Optional[str] = None,
    access_key: Optional[str] = None,
    remaining_views: Optional[int] = None,
    expires_at: Optional[str] = None,
    extra_files: Optional[List[Dict[str, Optional[str]]]] = None,
    session: Optional[aiohttp.ClientSession] = None,
) -> Dict[str, Optional[str]]:
    """
    Creates a paste on pastey.gg -- see this module's docstring for the
    confirmed request/response shape, and its four numbered points for
    exactly what `access_key`, `remaining_views`, `expires_at`, and
    `extra_files` below each do and why.

    No config-backed default key: pastey.gg still has no API-key/account
    concept at all for creating a paste (this module's docstring, point
    1), so `access_key` -- when given -- is sent as-is as this paste's
    `password`, never compared against a config var the way
    pastee_dev.py's own `access_key` is.

    `description` is accepted only so commands/url.py can call every
    provider's create_paste() the same way -- pastey.gg has nowhere to
    put it, so it's silently dropped rather than sent (unchanged from
    this module's original rewrite).

    `title` becomes the primary file's `name`; `language` becomes that
    file's `language` -- see _normalize_language()'s own docstring.

    `remaining_views`, if given, must be within REMAINING_VIEWS_MIN..MAX
    (1-1000) -- checked locally before the request goes out; see this
    module's docstring, point 2. Raises PasteyGGAPIError
    (fields=["remaining_views"]) itself if it's out of range.

    `expires_at`, if given, is normalized/validated by
    _normalize_expires_at() -- see this module's docstring, point 3.

    `extra_files`, if given, is a list of {"content" (required), "name",
    "language"} dicts, each appended as a further entry in the same
    `files` array `text`/`title`/`language` build the first entry from --
    see this module's docstring, point 4, including why this isn't wired
    into /paste's own Discord options yet. Raises PasteyGGAPIError
    (fields=["files"]) itself if any entry's `content` is empty, matching
    validatePaste()'s own "File content cannot be empty." rule for the
    primary file.

    Returns {"paste_url": ..., "raw_url": ..., "deletion_url": ...} --
    `deletion_url` is actually the paste's `safety_token`, needed as the
    `X-Safety-Token` header to delete or modify it later (unchanged from
    this module's original rewrite), kept under this dict's usual key
    name for consistency with every other provider in this package.

    Raises PasteyGGAPIError on a non-2xx response (see this module's
    docstring's "On error handling" section for exactly which statuses
    are reachable here and how each one's message is parsed), a
    non-JSON response, or a 2xx response with no `id` in it.
    """
    files: List[Dict] = [
        {"content": text, "name": title, "language": _normalize_language(language)}
    ]
    if extra_files:
        for extra in extra_files:
            content = extra.get("content")
            if not content:
                raise PasteyGGAPIError(
                    "Every entry in `extra_files` needs non-empty `content` -- pastey.gg "
                    "rejects an empty file the same way it rejects an empty primary paste.",
                    fields=["files"],
                )
            files.append(
                {
                    "content": content,
                    "name": extra.get("name"),
                    "language": _normalize_language(extra.get("language")),
                }
            )

    body: Dict = {"files": files}

    if access_key:
        # Reused, unchanged, as pastey.gg's own `password` field -- see
        # this module's docstring, point 1.
        body["password"] = access_key

    if remaining_views is not None:
        if not (REMAINING_VIEWS_MIN <= remaining_views <= REMAINING_VIEWS_MAX):
            raise PasteyGGAPIError(
                f"`remaining_views` must be between {REMAINING_VIEWS_MIN} and "
                f"{REMAINING_VIEWS_MAX} -- pastey.gg rejects anything outside that range.",
                fields=["remaining_views"],
            )
        body["remaining_views"] = remaining_views

    if expires_at:
        body["expires_at"] = _normalize_expires_at(expires_at)

    sess, should_close = await _get_session(session)
    try:
        try:
            async with sess.post(f"{BASE_URL}/pastes", json=body) as resp:
                try:
                    data = await resp.json()
                except aiohttp.ContentTypeError:
                    raise PasteyGGAPIError(f"pastey.gg returned HTTP {resp.status} with a non-JSON body.")
                if resp.status not in (200, 201):
                    # See this module's docstring's "On error handling"
                    # section: createPaste()'s own failures are always
                    # {"error": ...}, but a 429 from Echo-the-framework's
                    # rate-limit middleware (not createPaste() itself) is
                    # {"message": ...} instead -- `error` checked first
                    # since it's the more common shape at this endpoint.
                    detail = data.get("error") or data.get("message")
                    if resp.status == 429:
                        retry_after = resp.headers.get("X-RateLimit-Retry-After")
                        if retry_after:
                            detail = f"{detail} (retry in {retry_after}s)" if detail else f"Retry in {retry_after}s."
                    raise PasteyGGAPIError(detail or f"pastey.gg returned HTTP {resp.status}.")
        except (aiohttp.ClientError, TimeoutError) as e:
            raise PasteyGGAPIError(f"Couldn't reach pastey.gg: {_describe_network_error(e, _TIMEOUT)}")
    finally:
        if should_close:
            await sess.close()

    paste_id = data.get("id")
    if not paste_id:
        raise PasteyGGAPIError(
            f"pastey.gg's response had no `id` field -- response was: {str(data)[:500]!r}"
        )

    return {
        "paste_url": f"https://pastey.gg/{paste_id}",
        "raw_url": f"{BASE_URL}/pastes/{paste_id}/raw",
        "deletion_url": data.get("safety_token"),
    }
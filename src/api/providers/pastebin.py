"""
api.providers.pastebin -- async wrapper around Pastebin.com's classic
Developer API (https://pastebin.com/doc_api), for /paste's `pastebin`
provider choice. Free-plan only -- see this docstring's "Free-plan scope"
section for exactly what that excludes.

Confirmed 2026-08-17 straight off pastebin.com/doc_api itself (fetched
fresh, not from training-data memory -- that page is also where every
quoted error string and parameter name below comes from).

## Wire format -- the one provider in this package that's neither JSON

POST https://pastebin.com/api/api_post.php as
`application/x-www-form-urlencoded` POST fields (aiohttp's `data=`, not
`json=`), with `api_option=paste` plus the fields documented below.
There's no request envelope of any kind -- just flat form fields, closer
to e-z.host's own multipart convention than pastey_gg/pastee_dev/rubis's
JSON bodies.

The response is plain text too, not JSON, and (per the docs -- no status
code is ever mentioned for this endpoint) always comes back as a normal
HTTP 200 either way:
  - success: the body IS the created paste's URL, e.g.
    `https://pastebin.com/UIFdu235s` -- nothing else, no wrapper.
  - failure: the body is `Bad API request, <reason>` -- a fixed,
    documented set of `<reason>` tails (see _BAD_REQUEST_MESSAGES below),
    every one of which is mapped to a friendlier message here rather than
    relayed verbatim.
This module treats "starts with `Bad API request`" as the failure
signal (not the HTTP status, which the docs never tie to success/failure
here) and "looks like an http(s) URL" as the success signal -- anything
else (an empty body, a body that's neither) is treated as an unexpected
response rather than guessed at either way.

## api_dev_key vs api_user_key -- two different keys, two different roles

- `api_dev_key` (config.PASTEBIN_API_DEV_KEY): mandatory, bot-wide
  infrastructure. Per the docs, "Everybody using our API is required to
  use a valid Developer API Key" -- there's no keyless path at all, not
  even for an anonymous guest paste. Always required via require_key()
  below; never overridden per-call (there's no /paste option for it --
  unlike api_user_key below, it identifies this bot's own registered
  application, not whoever's posting, so there's nothing meaningful for
  a caller to override it *with*).
- `api_user_key` (config.PASTEBIN_API_USER_KEY): optional, account-level.
  Left unset entirely, Pastebin creates an anonymous "guest" paste (the
  docs' own PHP example: "if no api_user_key is used, a guest paste will
  be created") -- guest pastes can still be `public` or `unlisted`, just
  never `private` (see below). Overridable per-call via /paste's
  `access_key` option -- same *meaning* as pastee_dev.py's own
  `access_key` ("use your own account instead of this bot's default"),
  NOT pastey_gg.py's unrelated meaning (a per-paste password) -- see
  api/providers/registry.py's own comment on this provider's
  supports_access_key flag.

## api_paste_private / `visibility` -- public=0, unlisted=1, private=2

Confirmed via doc_api's "api_paste_private parameter in detail" section.
_VALID_VISIBILITIES below is the full three-way set -- Pastebin is the
only /paste provider in this package with all three (api.providers.rubis
only has "public"/"private" -- see that module's own
_VALID_VISIBILITIES); commands/url.py's shared `visibility` choice list
is built from both tuples together so "unlisted" actually shows up as a
pickable option at all.

`private` is documented as "only allowed in combination with
api_user_key, as you have to be logged into your account to access the
paste" -- checked locally in create_paste() below (raising
PastebinAPIError(fields=["api_paste_private"]) before ever making the
request) rather than left for Pastebin's own validation, since which key
is missing is knowable in advance and the failure mode otherwise would
be an opaque round trip for something already certain to fail.

## api_paste_expire_date / `expire_date` -- a small, fixed, 9-value enum

Confirmed via doc_api's own "api_paste_expire_date parameter in detail"
section -- EXPIRE_DATE_HELP below is the complete list, nothing else is
valid. This is NOT the same shape as pastee_dev.py's own free-form
`expiration` mini-language (plain seconds, "3d", "4w", ...) or
pastey_gg.py's absolute RFC3339 `expires_at` -- it's a closed set of
literal codes ("10M", "1D", "1Y", ...), so commands/url.py routes to
this module's own `expire_date` kwarg specifically rather than reusing
either of those existing code paths. Validated locally
(_normalize_expire_date below) before the request goes out, exactly the
same "known in advance, don't burn a round trip" reasoning as
`visibility`'s private/api_user_key check above -- unlike pastee_dev's
own `expiration`, there's no ambiguity here about whether Pastebin's own
server-side validation is reliable; the accepted set is simply small and
fixed enough that checking it locally is strictly better.

## api_paste_format / `language` -- ~200 syntax-highlighting codes

doc_api lists roughly 200 of these (php, python, lua, ...), every one of
them lowercase -- and Pastebin's own validation is case-sensitive against
that exact spelling, so `_normalize_format()` below lowercases whatever
`language` it's given before sending it (a plain `.lower()`, not a lookup
against the ~200-code list itself). This module still doesn't hardcode
that list -- same "the provider's own response is the final word on an
invalid *spelling*, /paste's shared language list
(api/providers/languages.py) is a suggestion, not a validated contract"
convention as every other provider in this package (see that module's
own docstring) -- lowercasing only fixes a mismatched *case*, not a
genuinely unrecognized code, which still comes back as Pastebin's own
`invalid api_paste_format` (mapped via _BAD_REQUEST_MESSAGES below) same
as ever. This bot's own "plaintext"/"autodetect" sentinels are normalized
to omitting `api_paste_format` entirely below (Pastebin's own default
when the field is left out at all -- confirmed by the docs' "Welcome To
Pastebin V3" example listing output, whose `paste_format_short` reads
"text" for a paste created with no format specified), rather than
guessed at as an explicit "text" value.

## api_folder_key / `folder_key` -- Pastebin-exclusive within /paste

"Use the api_user_key parameter first before using api_folder_key" is
the only documented rule -- and the documented failure for skipping that
("Bad API request, you can't add paste to folder as guest") is checked
locally here too (PastebinAPIError(fields=["api_folder_key"])), same
early-exit reasoning as `visibility`'s private check above. No other
/paste provider in this package has an equivalent folder concept, so
this isn't a generic registry.py capability flag -- see registry.py's
own `supports_folder` comment on this provider's entry.

**Gotcha, confirmed by observed behavior 2026-08-18 (not stated on
doc_api itself):** this parameter takes the folder's own opaque key
(e.g. "8gt61jk8"), NOT the folder's display name shown in Pastebin's
website UI -- there is no documented `Bad API request, ...` string for
an unrecognized/nonexistent folder key the way there is for the
guest-user case above, so passing a display name (or any other wrong
key) doesn't raise anything here or in Pastebin's own response: the
paste is created successfully, just not placed in any folder. Nothing
in this module can catch that locally (unlike `visibility`/
`expire_date` above, there's no fixed set to validate against, and
Pastebin's API has no documented `list_folders`-style call this module
could use to resolve a name to its real key), so it's on the caller to
supply the actual key -- obtained out of band from Pastebin's own site
(logged in, the "New Paste" page's Folder dropdown; the real key is
each `<option>`'s `value`, not its visible label), same
obtained-out-of-band spirit as PASTEBIN_API_USER_KEY itself (this
docstring's "Free-plan scope" section).

## Free-plan scope -- what this module deliberately doesn't cover

Only paste *creation* (api_option=paste) is implemented -- not listing
(api_option=list), deleting (api_option=delete), user details
(api_option=userdetails), or the api_login.php member-login flow that
mints an api_user_key in the first place. All of those are real, documented
parts of Pastebin's API, just outside /paste's own scope (creating a
paste) the same way pastee_dev.py and pastey_gg.py each only implement
their own service's create endpoint. A deployment that wants
PASTEBIN_API_USER_KEY has to obtain it once, out of band (POST to
api_login.php per the docs, or copy it from Pastebin's own site) and set
it in .env -- this module never performs that login itself.

Consequently, and for the same reason pastee_dev.py's own deletion_url
is always None (its docstring), create_paste() below always returns
deletion_url=None too: actually deleting a paste needs api_option=delete
plus the exact api_user_key that created it plus the paste's own key,
none of which this module wires up.

## Known free-plan caps -- pre-checked locally where that's possible

- **Paste size**: confirmed 500 KB for free (non-PRO) accounts *and*
  guests via pastebin.com/pro ("Free members can create pastes up to 500
  kilobytes in size, PRO members can create pastes up to 10 megabytes"),
  NOT stated anywhere on doc_api itself (its own "maximum paste file size
  exceeded" error names no number). MAX_PASTE_SIZE below is checked
  locally as a courtesy -- unlike the `visibility`/`expire_date` checks
  above, this number didn't come from the same page as the rest of this
  module's confirmed behavior, so Pastebin's own server-side rejection
  (still handled below, mapped through _BAD_REQUEST_MESSAGES) remains the
  actual final word, not this pre-check.
- **25 unlisted / 10 private pastes per free account, 20 new pastes per
  24h** (doc_api's own error text for the first two; pastebin.com/faq for
  the daily cap, which has no dedicated documented error string at all
  and most likely just surfaces as a generic captcha/soft-block rather
  than a "Bad API request" line): NOT pre-checked locally at all -- this
  bot has no way to know a given account's current paste count or
  today's post count, so these three are only ever caught after the fact,
  via the server's own response.
"""

from typing import Dict, List, Optional, Tuple

import aiohttp

from api import config
from api.providers.errors import ProviderAPIError
from api.providers.util import describe_network_error as _describe_network_error
from api.providers.util import get_session as _get_session_shared
from api.providers.util import require_key

BASE_URL = "https://pastebin.com/api/api_post.php"
RAW_BASE_URL = "https://pastebin.com/raw"

_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Confirmed 2026-08-17 (pastebin.com/doc_api, "api_paste_expire_date
# parameter in detail") -- exactly these 9 literal codes are valid, and
# nothing else is. (code, human label) pairs, kept here (not
# commands/url.py) so they stay next to the field they document --
# commands/url.py imports this to build an "invalid expiration" help
# embed when Pastebin rejects one, same convention as
# pastee_dev.EXPIRATION_HELP.
EXPIRE_DATE_HELP: List[Tuple[str, str]] = [
    ("N", "Never"),
    ("10M", "10 Minutes"),
    ("1H", "1 Hour"),
    ("1D", "1 Day"),
    ("1W", "1 Week"),
    ("2W", "2 Weeks"),
    ("1M", "1 Month"),
    ("6M", "6 Months"),
    ("1Y", "1 Year"),
]
_VALID_EXPIRE_DATES = {code for code, _label in EXPIRE_DATE_HELP}

# Confirmed 2026-08-17 (pastebin.com/doc_api, "api_paste_private
# parameter in detail"): public=0, unlisted=1, private=2. See this
# module's own docstring for why "unlisted" makes this the one /paste
# provider in this package with all three, unlike
# api.providers.rubis._VALID_VISIBILITIES ("public"/"private" only).
_VALID_VISIBILITIES = ("public", "unlisted", "private")
_VISIBILITY_CODES = {"public": "0", "unlisted": "1", "private": "2"}

# pastebin.com/pro, confirmed 2026-08-17 -- free (non-PRO) accounts and
# guests alike are capped at 500 KB per paste. Decimal (500 * 1000), to
# match Pastebin's own "500 kilobytes" wording rather than assuming a
# binary 512000-byte KiB. See this module's docstring's "Known free-plan
# caps" section for why this is a courtesy pre-check, not a guarantee.
MAX_PASTE_SIZE = 500_000

# Every "Bad API request, <reason>" tail this endpoint's own docs (doc_api,
# "Creating A New Paste" section) list as a possible response, mapped to a
# friendlier, non-jargon message. Matched via the exact tail text (not a
# substring/regex) since that literal wording is Pastebin's own stated
# contract -- if it's ever reworded, _friendly_error()'s own unmatched
# fallback still surfaces the raw text instead of silently mismatching.
# Excludes api_login.php's own bad-response set ("invalid login",
# "account not active", ...) -- this module never calls that endpoint, see
# the module docstring's "Free-plan scope" section.
_BAD_REQUEST_MESSAGES: Dict[str, str] = {
    "invalid api_option": (
        "Pastebin rejected this request's `api_option` value -- that's a bug in the bot "
        "itself, not something a different paste will fix."
    ),
    "invalid api_dev_key": (
        "This bot's Pastebin Developer API Key looks invalid -- check PASTEBIN_API_DEV_KEY "
        "in the bot's .env file."
    ),
    "maximum number of 25 unlisted pastes for your free account": (
        "This Pastebin account already has the free-plan maximum of 25 unlisted pastes. "
        "Delete an old one, use `public`/`private` instead, or upgrade to Pastebin PRO."
    ),
    "maximum number of 10 private pastes for your free account": (
        "This Pastebin account already has the free-plan maximum of 10 private pastes. "
        "Delete an old one, use `public`/`unlisted` instead, or upgrade to Pastebin PRO."
    ),
    "api_paste_code was empty": "There's no content to paste.",
    "maximum paste file size exceeded": (
        f"That paste is too large for Pastebin's free plan (limit: {MAX_PASTE_SIZE:,} bytes / 500 KB)."
    ),
    "invalid api_paste_expire_date": (
        "That isn't one of Pastebin's accepted expiration codes -- see this bot's own "
        "`expires` error for the full list."
    ),
    "invalid api_paste_private": "That isn't a visibility Pastebin accepts (`public`/`unlisted`/`private`).",
    "invalid api_paste_format": (
        "That isn't a syntax-highlighting value Pastebin recognizes -- see "
        "https://pastebin.com/doc_api#5 for the full list, or leave `language` blank."
    ),
    "invalid api_user_key": (
        "The Pastebin account key configured for this bot (or the one passed via "
        "`access_key`) is malformed."
    ),
    "invalid or expired api_user_key": (
        "The Pastebin account key configured for this bot (or the one passed via "
        "`access_key`) is invalid or has expired -- generate a fresh api_user_key and "
        "update PASTEBIN_API_USER_KEY, or pass a new `access_key`."
    ),
    "you can't add paste to folder as guest": (
        "Pastebin rejects adding a paste to a folder without being logged in -- set "
        "PASTEBIN_API_USER_KEY on the bot, or pass a per-call `access_key`."
    ),
}


class PastebinAPIError(ProviderAPIError):
    """Raised whenever a Pastebin paste-creation call doesn't succeed --
    one of the documented `Bad API request, ...` responses (mapped via
    _BAD_REQUEST_MESSAGES), an unreachable host, or a 2xx body that's
    neither a recognized failure nor something that looks like a paste
    URL.

    `fields` -- set for the two failures this module catches and raises
    locally *before* a request ever goes out (an `expire_date` outside
    EXPIRE_DATE_HELP's 9 codes, or `visibility="private"`/`folder_key`
    with no api_user_key available -- see this module's own docstring),
    same convention as PasteyGGAPIError.fields/PasteeDevAPIError.fields.
    Empty for every failure that instead came back from Pastebin itself,
    since its own plain-text `Bad API request, ...` responses never name
    a field the structured way pastee_dev's own {"errors": [...]} does.
    """

    def __init__(self, message: str, *, fields: Optional[List[str]] = None):
        super().__init__(message)
        self.fields = fields or []


async def _get_session(session: Optional[aiohttp.ClientSession]):
    return await _get_session_shared(session, _TIMEOUT)


def _normalize_format(language: Optional[str]) -> Optional[str]:
    """Lowercases `language` for api_paste_format -- doc_api#5's ~200-code
    list is entirely lowercase (php, python, lua, ...; confirmed 2026-08-17
    off doc_api itself, no mixed-case code documented anywhere in it), and
    Pastebin's own validation is case-sensitive against that exact spelling
    -- `api_paste_format=Lua` comes back `Bad API request, invalid
    api_paste_format` the same as any other unrecognized code would,
    even though `lua` is valid. api/providers/languages.py's own shared
    `language` list already stores lowercase values (its own docstring),
    but that list is a suggestion, not a validated contract (same file) --
    Discord's autocomplete never stops someone from typing a differently-
    cased spelling by hand (commands/url.py's `_language_autocomplete`
    docstring) or passing one through /paste's `file` attachment-name
    guess (api.providers.languages.language_for_filename), so this is
    normalized here rather than assumed already-lowercase. Still not a
    validated list beyond casing -- Pastebin's own ~200-code list is the
    final word on whether the *spelling* itself is one it recognizes at
    all (this module's docstring), just no longer tripped up by case
    alone. Case-normalization is Pastebin-exclusive within this package:
    pastey_gg.py's own `_normalize_language` and pastee_dev.py's own
    `_normalize_syntax` both pass `language` through unchanged (aside from
    the "plaintext" sentinel each maps to its own equivalent), since
    neither of those providers' docs document a lowercase-only convention
    the way doc_api#5 does here.

    Sentinel handling is otherwise unchanged: this bot's own
    "plaintext"/"autodetect"/"text" sentinels (or nothing at all) are
    still left unset entirely rather than sent as a guessed-at literal
    "text" value -- checked case-insensitively already, so lowercasing
    doesn't change that branch's behavior."""
    if not language or language.strip().lower() in ("plaintext", "autodetect", "text"):
        return None
    return language.strip().lower()


def _normalize_expire_date(value: str) -> str:
    """Validates/normalizes `value` against EXPIRE_DATE_HELP's fixed
    9-code set before it's ever sent -- see this module's docstring for
    why this is checked locally rather than left to Pastebin's own
    validation. Case-insensitive (Pastebin's own codes are uppercase, but
    there's no reason to make someone typing `/paste expires:1d` retype
    it as `1D`). Raises PastebinAPIError(fields=["api_paste_expire_date"])
    for anything outside that set."""
    candidate = value.strip().upper()
    if candidate not in _VALID_EXPIRE_DATES:
        raise PastebinAPIError(
            f"'{value}' isn't one of Pastebin's accepted expiration codes.",
            fields=["api_paste_expire_date"],
        )
    return candidate


def _friendly_error(body: str) -> str:
    """Turns a raw `Bad API request, <reason>` body into a friendly
    message via _BAD_REQUEST_MESSAGES -- an unrecognized `<reason>` (a
    Pastebin-side wording change, or a genuinely new error this module
    doesn't know about yet) still surfaces that raw tail rather than a
    silent generic failure, same "forward-compatible fallback" reasoning
    as pastey_gg.py's own `data.get("error") or data.get("message")`."""
    prefix = "Bad API request, "
    if not body.startswith(prefix):
        return f"Pastebin returned an unexpected response: {body[:300]!r}"
    tail = body[len(prefix):].strip()
    mapped = _BAD_REQUEST_MESSAGES.get(tail)
    if mapped:
        return mapped
    return f"Pastebin rejected that paste: {tail}."


async def create_paste(
    text: str,
    *,
    language: str = "plaintext",
    title: Optional[str] = None,
    description: Optional[str] = None,
    access_key: Optional[str] = None,
    visibility: Optional[str] = None,
    expire_date: Optional[str] = None,
    folder_key: Optional[str] = None,
    session: Optional[aiohttp.ClientSession] = None,
) -> Dict[str, Optional[str]]:
    """
    Creates a paste on Pastebin.com via its classic Developer API -- see
    this module's docstring for the confirmed request/response shape and
    every parameter's own detailed writeup.

    `access_key` overrides config.PASTEBIN_API_USER_KEY for this call
    (registry.py's supports_access_key=True for this provider) -- same
    *meaning* as pastee_dev.py's own `access_key` ("post as your own
    account instead of this bot's default"), unlike pastey_gg.py's
    unrelated per-paste-password meaning. Unlike pastee_dev's own
    access_key though, this one is never required -- Pastebin allows a
    fully anonymous "guest" paste with no api_user_key at all (see this
    module's docstring); it's only needed for `visibility="private"` or
    `folder_key` below.

    config.PASTEBIN_API_DEV_KEY, by contrast, IS always required
    regardless of `access_key` -- raises PastebinAPIError naming that env
    var if unset (via require_key()), since Pastebin has no keyless path
    at all for this endpoint (this module's docstring).

    `description` is accepted only so commands/url.py can call every
    /paste provider's create_paste() the same way -- Pastebin's own
    create-paste endpoint has no description field anywhere in its docs,
    so it's silently dropped rather than sent, same convention as
    pastey_gg.py's own `description` parameter.

    `language` maps to `api_paste_format` -- see _normalize_format()'s
    own docstring for exactly which sentinels get left unset instead of
    forwarded.

    `visibility`, if given, must be one of _VALID_VISIBILITIES
    ("public"/"unlisted"/"private") -- raises
    PastebinAPIError(fields=["api_paste_private"]) itself for anything
    else, and for `"private"` specifically when neither `access_key` nor
    config.PASTEBIN_API_USER_KEY is available (Pastebin's own
    "only allowed in combination with api_user_key" rule -- see this
    module's docstring).

    `expire_date`, if given, is validated/normalized by
    _normalize_expire_date() against EXPIRE_DATE_HELP's fixed 9-code set
    -- see this module's docstring for why that's checked locally.

    `folder_key`, if given, is sent as-is as `api_folder_key` -- raises
    PastebinAPIError(fields=["api_folder_key"]) itself first if neither
    `access_key` nor config.PASTEBIN_API_USER_KEY is available, matching
    Pastebin's own documented "can't add paste to folder as guest"
    rejection (this module's docstring) rather than waiting on the round
    trip to hear it from Pastebin itself.

    Returns {"paste_url": ..., "raw_url": ..., "deletion_url": None} --
    see this module's docstring's "Free-plan scope" section for why
    deletion_url is always None here.

    Raises PastebinAPIError on any `Bad API request, ...` response
    (mapped via _BAD_REQUEST_MESSAGES/_friendly_error() -- see this
    module's docstring's "Wire format" section for why there's no JSON
    to speak of here at all), an unreachable host, or a 2xx body that
    doesn't look like a paste URL either.
    """
    dev_key = require_key(
        config.PASTEBIN_API_DEV_KEY, "PASTEBIN_API_DEV_KEY", "Pastebin", PastebinAPIError
    )
    user_key = access_key or config.PASTEBIN_API_USER_KEY

    if not text:
        raise PastebinAPIError("There's no content to paste.", fields=["api_paste_code"])

    size = len(text.encode("utf-8"))
    if size > MAX_PASTE_SIZE:
        raise PastebinAPIError(
            f"That paste is {size:,} bytes -- Pastebin's free-plan limit is "
            f"{MAX_PASTE_SIZE:,} bytes (500 KB).",
            fields=["api_paste_code"],
        )

    body: Dict[str, str] = {
        "api_dev_key": dev_key,
        "api_option": "paste",
        "api_paste_code": text,
    }
    if user_key:
        body["api_user_key"] = user_key
    if title:
        body["api_paste_name"] = title

    fmt = _normalize_format(language)
    if fmt:
        body["api_paste_format"] = fmt

    if visibility is not None:
        if visibility not in _VALID_VISIBILITIES:
            raise PastebinAPIError(
                f"'{visibility}' isn't a visibility Pastebin accepts "
                f"({', '.join(_VALID_VISIBILITIES)}).",
                fields=["api_paste_private"],
            )
        if visibility == "private" and not user_key:
            raise PastebinAPIError(
                "Pastebin only allows a `private` paste when posting as a logged-in "
                "account -- set PASTEBIN_API_USER_KEY on the bot, or pass a per-call "
                "`access_key`.",
                fields=["api_paste_private"],
            )
        body["api_paste_private"] = _VISIBILITY_CODES[visibility]

    if expire_date:
        body["api_paste_expire_date"] = _normalize_expire_date(expire_date)

    if folder_key:
        if not user_key:
            raise PastebinAPIError(
                "Pastebin rejects adding a paste to a folder as a guest -- set "
                "PASTEBIN_API_USER_KEY on the bot, or pass a per-call `access_key`.",
                fields=["api_folder_key"],
            )
        body["api_folder_key"] = folder_key

    sess, should_close = await _get_session(session)
    try:
        try:
            async with sess.post(BASE_URL, data=body) as resp:
                # Plain text, not JSON, on both success and failure -- see
                # this module's docstring's "Wire format" section.
                raw = (await resp.text()).strip()
        except (aiohttp.ClientError, TimeoutError) as e:
            raise PastebinAPIError(f"Couldn't reach Pastebin: {_describe_network_error(e, _TIMEOUT)}")
    finally:
        if should_close:
            await sess.close()

    if raw.startswith("Bad API request"):
        raise PastebinAPIError(_friendly_error(raw))
    if not (raw.startswith("http://") or raw.startswith("https://")):
        raise PastebinAPIError(f"Pastebin returned an unexpected response: {raw[:300]!r}")

    paste_url = raw
    paste_key = paste_url.rstrip("/").rsplit("/", 1)[-1]

    return {
        "paste_url": paste_url,
        "raw_url": f"{RAW_BASE_URL}/{paste_key}",
        "deletion_url": None,
    }
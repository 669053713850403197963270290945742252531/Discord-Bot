"""
api.providers.uguu -- thin async wrapper around Uguu's temporary-file-hosting
API (https://uguu.se/api.html), for /file's `uguu` provider choice. Uguu is
part of the same Pomf-derived family as api.providers.litterbox, but with a
single fixed retention window instead of a choice of one.

Confirmed shape (https://uguu.se/api.html, live 2026-08-18): POST
multipart/form-data to https://uguu.se/upload with the file as a `files[]`
array field (per that page's own curl example -- note the brackets even for
a single file, unlike catbox/litterbox's unbracketed `fileToUpload`). An
optional `output` query param controls the response format -- json (the
undocumented default), csv, text, html, or gyazo. This module requests
`output=text` rather than taking the default JSON: Uguu's own docs don't
document the JSON envelope's field names anywhere, while ShareX's own
uguu.se.sxcu custom-uploader config (github.com/ShareX/CustomUploaders) --
a real-world, independently-maintained integration -- talks to this same
endpoint family with `output=text` and no response-parsing rule configured
at all, meaning ShareX's own parser falls back to treating the entire
response body as the URL. That's the same plain-text-body-is-the-URL
convention api.providers.catbox / api.providers.litterbox already use here,
so this module follows it rather than guessing at undocumented JSON keys.

Confirmed live off uguu.se's own homepage (https://uguu.se/, 2026-08-18):
"Max upload size is 128 MiB & files expire after 3 hours." Both figures are
fixed platform-wide constants, not per-request options -- unlike Litterbox,
Uguu's API takes no duration parameter at all, so there's no `expiry` kwarg
here for commands/url.py to pass (registry.py's requires_expiry=False for
this provider reflects that; the fixed 3h window is surfaced to the user
instead via this provider's own registry.py `label`).

Every upload is anonymous (uguu.se/faq.html: no accounts, no registration),
and Uguu's API has no documented deletion endpoint at all -- so, like
catbox.py/litterbox.py, upload_file() below always returns
deletion_url=None.

Not confirmed: the full set of status/wording Uguu returns for every one of
its own documented rejection reasons (uploads over 128 MiB, its rate
limiter, and malware detection specifically -- all per uguu.se/faq.html and
the self-hosted project's own config.json options). Confirmed live
(2026-08-24) for its extension/MIME filter, though inconsistently: the same
HTTP 415 rejection has come back in two different shapes across separate
requests --
  - JSON: {"success": false, "errorcode": 415, "description": "Filetype not allowed"}
  - Plain text: "ERROR: (415) Filetype not allowed"
-- even though this module requests output=text either way (see above), so
that request parameter apparently doesn't reliably govern the error
response's shape the way it does the success one's.
_extract_description()/_describe_error() below parse both shapes
generically (not just for 415) and surface whichever `description`/message
they find; a body matching neither (a reverse-proxy's own error page for a
request rejected before ever reaching Uguu's app -- plausible for 413/429
specifically, which are exactly the sort of thing often enforced at the
Nginx/CDN layer in front of an app like this one, per the self-hosted
project's own Nginx config -- or any other failure mode this hasn't been
confirmed for) falls back to a friendlier status-specific message for
413/429, then finally to the raw status + body rather than hiding either.
"""

import json
import re
from typing import Dict, Optional

import aiohttp

from api.providers.errors import ProviderAPIError
from api.providers.util import describe_network_error as _describe_network_error
from api.providers.util import get_session as _get_session_shared

BASE_URL = "https://uguu.se/upload"

# Same 60s ceiling as catbox.py/litterbox.py -- this provider's own upload
# latency isn't separately confirmed, but there's no reason to assume it's
# faster than its Pomf-family siblings, and staying conservative here just
# means a slow upload surfaces as a clean timeout message instead of hanging.
_TIMEOUT = aiohttp.ClientTimeout(total=60)

# Matches Uguu's plain-text error shape, e.g. "ERROR: (415) Filetype not
# allowed" -- see module docstring for why this and the JSON shape both
# need handling rather than just one.
_ERROR_TEXT_RE = re.compile(r"^ERROR:\s*\(\d+\)\s*(.+)$", re.IGNORECASE)

# Confirmed live off uguu.se's own homepage (see module docstring). Checked
# locally so an oversized file gets one clear message naming the actual
# limit up front, instead of relying entirely on Uguu's own (unconfirmed)
# rejection wording for it. This is redundant in practice when called via
# /file -- commands.url.MAX_FILE_ATTACHMENT_SIZE's blanket 100 MiB cap is
# already tighter than Uguu's own 128 MiB, so that shared cap rejects an
# oversized attachment before this module ever sees one -- but it's kept
# here anyway so this module is correct on its own for any direct caller,
# not just correct as a side effect of that shared cap's current value.
MAX_UPLOAD_SIZE = 128 * 1024 * 1024  # 128 MiB


class UguuAPIError(ProviderAPIError):
    """Raised whenever a Uguu upload doesn't succeed -- an oversized file
    (checked locally against MAX_UPLOAD_SIZE before any request goes out),
    a non-200 HTTP status, or a 200 response whose body doesn't look like a
    URL (see this module's docstring for why that's the success/failure
    signal here, same convention as CatboxAPIError/LitterboxAPIError)."""


async def _get_session(session: Optional[aiohttp.ClientSession]):
    return await _get_session_shared(session, _TIMEOUT)


def _extract_description(body: str) -> Optional[str]:
    """Pulls the human-readable reason out of a Uguu error response,
    whichever of the two confirmed shapes it comes back as this time (see
    module docstring -- Uguu's own error formatting isn't consistent).
    Returns None if neither shape matches, so callers can fall back to
    their own wording instead of surfacing an empty/nonsense string."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict) and parsed.get("description"):
        return parsed["description"]

    match = _ERROR_TEXT_RE.match(body)
    if match:
        return match.group(1).strip()

    return None


def _describe_error(status: int, body: str) -> str:
    """Turns a non-200 Uguu response into one clean, human-readable
    sentence instead of surfacing its raw error body verbatim -- see
    _extract_description() above for the two response shapes this tries
    first. Falls back to friendlier wording for 413/429 specifically (the
    two most likely to instead be a reverse-proxy error page that never
    reached either of those shapes at all), then to the raw status + body
    so nothing is ever silently swallowed."""
    description = _extract_description(body)
    if description:
        return f"Uguu rejected that file: {description}"
    if status == 413:
        return "Uguu rejected that file for being too large -- its limit is 128 MiB."
    if status == 429:
        return "Uguu's upload rate limit was hit -- wait a bit and try again."
    return f"Uguu returned HTTP {status}: {body}"


async def upload_file(
    filename: str,
    content: bytes,
    content_type: Optional[str] = None,
    *,
    session: Optional[aiohttp.ClientSession] = None,
) -> Dict[str, Optional[str]]:
    """
    Uploads `content` (as `filename`) to Uguu. Every upload is anonymous and
    auto-expires after Uguu's fixed 3-hour window (see module docstring) --
    there's no expiry to choose, and, like catbox.py/litterbox.py, no
    deletion_url Uguu hands back either (its API has no documented deletion
    mechanism at all).

    Returns {"file_url": ..., "deletion_url": None}.

    Raises UguuAPIError if `content` is over MAX_UPLOAD_SIZE, on a non-200
    response (see _describe_error() above for how that's turned into a
    clean message), or on a 200 body that doesn't look like a successful
    URL.
    """
    if len(content) > MAX_UPLOAD_SIZE:
        raise UguuAPIError(
            f"That file is {len(content):,} bytes -- Uguu's own limit is {MAX_UPLOAD_SIZE:,} bytes (128 MiB)."
        )

    form = aiohttp.FormData()
    form.add_field("files[]", content, filename=filename, content_type=content_type)

    sess, should_close = await _get_session(session)
    try:
        try:
            async with sess.post(BASE_URL, params={"output": "text"}, data=form) as resp:
                body = (await resp.text()).strip()
                if resp.status != 200:
                    raise UguuAPIError(_describe_error(resp.status, body))
        except (aiohttp.ClientError, TimeoutError) as e:
            raise UguuAPIError(f"Couldn't reach Uguu: {_describe_network_error(e, _TIMEOUT)}")
    finally:
        if should_close:
            await sess.close()

    if not (body.startswith("http://") or body.startswith("https://")):
        # A 200 with a body that isn't a URL hasn't been observed (every
        # confirmed rejection so far came back as a non-200 -- see module
        # docstring), but this still tries both known error shapes first
        # on the off chance one shows up here too, rather than assuming a
        # 200 status always means success.
        description = _extract_description(body)
        if description:
            raise UguuAPIError(f"Uguu rejected that file: {description}")
        raise UguuAPIError(
            body or "Uguu returned an empty response -- this can happen if the file's extension/type is "
            "blacklisted or it was flagged as malware (see uguu.se/faq.html)."
        )

    return {"file_url": body, "deletion_url": None}
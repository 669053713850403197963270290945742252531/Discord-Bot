"""
api.providers.is_gd -- thin async wrapper around is.gd's URL-shortening API
(https://is.gd/apishorteningreference.php) and its separate URL Lookup API
(https://is.gd/apilookupreference.php), for /url shorten's `is_gd`
provider choice and /url unshorten's is.gd/v.gd-specific fallback (see
commands/url.py's `_ISGD_HOSTNAMES` / `_url_unshorten_impl`).

is.gd and v.gd are, per is.gd's own developer docs, the exact same API on
two domains ("you can support either just by changing the domain") -- this
module hardcodes is.gd rather than exposing both as separate providers,
matching how the planning doc's Services list groups them as one bullet
("is.gd (+v.gd)") rather than two. commands/url.py's unshorten fallback
does treat both domains as this provider's, though, since a v.gd link is
exactly as look-up-able via this module's functions as an is.gd one --
only the request URL's host would differ, and this module's endpoints
don't care which one you visited.

No API key: both of is.gd's endpoints below are fully public,
unauthenticated GETs. No deletion either -- is.gd doesn't hand back
anything to delete a link with (its shortened URLs are meant to be
permanent), so shorten_url() below always returns deletion_url=None, same
as Litterbox and free-tier TinyURL.

**logstats vs. the Lookup API -- these are two different, unrelated
features, easy to conflate:**
  - `logstats` (shorten_url()'s param below) is purely a creation-time
    toggle for *is.gd's own click-statistics logging*, viewable later on
    is.gd's website (via the link's "further options" -- see is.gd's FAQ).
    It has nothing to do with resolving what a link points to.
  - `lookup_url()` below is is.gd's separate, dedicated, always-available
    API for resolving what a short is.gd/v.gd URL points to -- it works
    for *any* is.gd/v.gd link regardless of whether logstats was ever
    turned on for it, and requires no configuration at all.
For /url unshorten's "not in our own store" fallback, what's actually
needed is lookup_url() -- that's what commands/url.py calls. logstats
being on or off for a given link is irrelevant to that lookup succeeding.
"""

from typing import Any, Dict, Optional

import aiohttp

from api.providers.errors import ProviderAPIError
from api.providers.util import describe_network_error as _describe_network_error
from api.providers.util import get_session as _get_session_shared

SHORTEN_URL = "https://is.gd/create.php"
LOOKUP_URL = "https://is.gd/forward.php"

# is.gd publishes no SLA; 15s matches the timeout this package's other
# provider modules use for the same reason (a known-good round number
# rather than a measured value).
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class IsGdAPIError(ProviderAPIError):
    """Raised whenever an is.gd API call (either endpoint in this module)
    doesn't succeed -- a non-200 HTTP status, a non-JSON response, or a
    200 response containing is.gd's own `errorcode`/`errormessage` pair.
    Shortening and lookup share the same error-response shape, but not
    the same code meanings -- see shorten_url()'s and lookup_url()'s own
    docstrings for each endpoint's specific error codes."""

    def __init__(self, message: str, errorcode: Optional[int] = None):
        super().__init__(message)
        self.errorcode = errorcode


async def _get_session(session: Optional[aiohttp.ClientSession]):
    return await _get_session_shared(session, _TIMEOUT)


async def _get_json(url: str, params: Dict[str, str], session: Optional[aiohttp.ClientSession]) -> Dict[str, Any]:
    """Shared GET-and-parse-JSON plumbing for both of is.gd's endpoints
    below -- identical transport-level error handling (non-200, non-JSON,
    unreachable host) either way; only what counts as a successful body
    differs per endpoint, which is left to each caller to check."""
    sess, should_close = await _get_session(session)
    try:
        try:
            async with sess.get(url, params=params) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise IsGdAPIError(f"is.gd returned HTTP {resp.status}: {err}")
                try:
                    return await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    # is.gd's own anti-abuse layer (flood protection, or
                    # scrutiny on shorturl/logstats specifically -- both
                    # are common spam/phishing vectors) can intercept a
                    # request before it ever reaches their JSON formatter
                    # and hand back their normal HTML page instead, still
                    # with a 200 status. Surface a snippet of what was
                    # actually returned rather than a bare "not JSON" --
                    # otherwise there's no way to tell that case apart
                    # from a genuine API-shape change without re-running
                    # the request outside this module.
                    body = (await resp.text())[:200].strip()
                    ct = resp.headers.get("Content-Type", "unknown")
                    raise IsGdAPIError(
                        f"is.gd returned a non-JSON response (Content-Type: {ct}). "
                        f"Body started with: {body!r}"
                    )
        except (aiohttp.ClientError, TimeoutError) as e:
            raise IsGdAPIError(f"Couldn't reach is.gd: {_describe_network_error(e, _TIMEOUT)}")
    finally:
        if should_close:
            await sess.close()


async def shorten_url(
    url: str,
    *,
    alias: Optional[str] = None,
    logstats: bool = False,
    session: Optional[aiohttp.ClientSession] = None,
) -> Dict[str, Optional[str]]:
    """
    Shortens `url` via is.gd's GET /create.php?format=json endpoint.

    `alias` maps to is.gd's `shorturl` param (registry.py's
    supports_alias=True for this provider) -- is.gd requires it be 5-30
    characters, alphanumeric/underscore only, and is.gd's own error
    response (errorcode 2) is the final word on anything that slips past
    that or is already taken, same as EZHostAPIError-style providers
    leaving validation to the provider itself.

    `logstats` maps to is.gd's `logstats` param -- turns on click-
    statistics logging for this link, viewable later on is.gd's own site.
    See this module's docstring for why this is unrelated to lookup_url()
    below; defaults to False (is.gd's own default) rather than True,
    since turning on logging is a choice the caller should opt into, not
    something this module should default on quietly.

    Returns {"short_url": ..., "deletion_url": None} -- is.gd links are
    permanent and is.gd's API never hands back anything to delete one with,
    so deletion_url is always None here (see this module's docstring).

    Raises IsGdAPIError on a non-200 response, a non-JSON response, or a
    200 response carrying is.gd's own error fields (errorcode 1 for a bad
    `url`, 2 for a taken/invalid custom `shorturl`, 3 for is.gd's own rate
    limit, 4 for anything else).
    """
    params = {"format": "json", "url": url}
    if alias:
        params["shorturl"] = alias
    if logstats:
        params["logstats"] = "1"

    data = await _get_json(SHORTEN_URL, params, session)

    if "errorcode" in data:
        raise IsGdAPIError(
            data.get("errormessage") or "is.gd rejected the request.",
            errorcode=data.get("errorcode"),
        )

    short_url = data.get("shorturl")
    if not short_url:
        raise IsGdAPIError("is.gd's response was missing the expected shorturl field.")

    return {"short_url": short_url, "deletion_url": None}


async def lookup_url(short_url: str, *, session: Optional[aiohttp.ClientSession] = None) -> str:
    """
    Resolves `short_url` back to its original destination, via is.gd's
    dedicated URL Lookup API (GET https://is.gd/forward.php?format=json)
    -- see this module's docstring for why this, not `logstats`, is what
    actually answers "what does this is.gd/v.gd link point to".

    `short_url` can be a full is.gd/v.gd address or just its bare code --
    is.gd's `shorturl` param accepts either form, so commands/url.py can
    pass the original URL straight through without extracting a code
    first, unlike this package's local-store lookups (which always need
    the bare code -- see api.providers.util.extract_short_code).

    Works for *any* valid is.gd/v.gd link, including ones this bot never
    created itself -- there's no ownership/auth concept on this endpoint
    at all, which is exactly what makes it useful as /url unshorten's
    fallback for a short link this bot has no local record of.

    Returns the destination URL as plain text.

    Raises IsGdAPIError on a non-200 response, a non-JSON response, or a
    200 response carrying is.gd's own error fields (errorcode 1 for a
    short URL that's invalid/doesn't exist, 2 for one that's been
    disabled, 3 for is.gd's own rate limit, 4 for anything else -- note
    these meanings differ from shorten_url()'s error codes despite
    sharing the same field names).
    """
    data = await _get_json(LOOKUP_URL, {"format": "json", "shorturl": short_url}, session)

    if "errorcode" in data:
        raise IsGdAPIError(
            data.get("errormessage") or "is.gd couldn't resolve that link.",
            errorcode=data.get("errorcode"),
        )

    destination = data.get("url")
    if not destination:
        raise IsGdAPIError("is.gd's lookup response was missing the expected url field.")

    return destination
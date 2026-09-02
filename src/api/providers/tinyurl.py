"""
api.providers.tinyurl -- thin async wrapper around TinyURL's modern
OpenAPI-based API (https://api.tinyurl.com, documented interactively at
https://tinyurl.com/app/dev), for /url shorten's `tinyurl` provider choice.

Requires config.TINYURL_API_KEY (an account API token from TinyURL's
Settings > API page) -- unlike is.gd/Litterbox, TinyURL's API has no
anonymous/unauthenticated path at all.

Free-plan scope only: TinyURL's `tags`, `expires_at`, and `description`
create-time fields, and its "Change URL" (destination-editing) action, are
paid-plan-only per TinyURL's own docs/help center, so none of them are
exposed here -- see shorten_url()'s docstring for exactly what a Free Plan
token can still do.

Confirmed against TinyURL's published docs/examples (POST /create's request
and response shapes, and that alias/domain are the create-time custom-slug
knobs): the shortened link never comes with a deletion_url -- TinyURL links
don't expire and TinyURL's Free plan has no delete capability at all (that's
a paid-plan-only permission), so shorten_url() below always returns
deletion_url=None, same as Litterbox and is.gd.
"""

from typing import Dict, Optional

import aiohttp

from api import config
from api.providers.errors import ProviderAPIError
from api.providers.util import describe_network_error as _describe_network_error
from api.providers.util import get_session as _get_session_shared
from api.providers.util import require_key

BASE_URL = "https://api.tinyurl.com"

# TinyURL matches api.e-z.host's un-published-SLA case; 15s is this
# package's usual known-good default rather than anything TinyURL-specific.
_TIMEOUT = aiohttp.ClientTimeout(total=15)

# TinyURL's Free plan only ever shortens onto this domain -- a branded/
# custom domain is a paid-plan feature this module doesn't attempt to
# support. Hardcoded rather than exposed as a param since there's no
# free-plan-reachable alternative to offer.
DEFAULT_DOMAIN = "tinyurl.com"


class TinyURLAPIError(ProviderAPIError):
    """Raised whenever a TinyURL API call doesn't succeed -- either a
    non-2xx HTTP status, a non-JSON response, or a 200 response body whose
    `code` field is non-zero (TinyURL's own convention for an
    application-level failure inside an otherwise-200 response)."""


async def _get_session(session: Optional[aiohttp.ClientSession]):
    return await _get_session_shared(session, _TIMEOUT)


def _headers() -> Dict[str, str]:
    key = require_key(config.TINYURL_API_KEY, "TINYURL_API_KEY", "TinyURL", TinyURLAPIError)
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


async def _request(method: str, path: str, *, json: Dict, session: Optional[aiohttp.ClientSession]) -> Dict:
    sess, should_close = await _get_session(session)
    try:
        try:
            async with sess.request(method, f"{BASE_URL}{path}", headers=_headers(), json=json) as resp:
                if resp.status not in (200, 201):
                    err = await resp.text()
                    raise TinyURLAPIError(f"TinyURL returned HTTP {resp.status}: {err}")
                try:
                    data = await resp.json()
                except aiohttp.ContentTypeError:
                    raise TinyURLAPIError("TinyURL returned a non-JSON response.")
        except (aiohttp.ClientError, TimeoutError) as e:
            raise TinyURLAPIError(f"Couldn't reach TinyURL: {_describe_network_error(e, _TIMEOUT)}")
    finally:
        if should_close:
            await sess.close()

    if data.get("code"):
        errors = data.get("errors") or []
        raise TinyURLAPIError(f"TinyURL rejected the request: {'; '.join(errors) or data.get('code')}")

    return data


async def shorten_url(
    url: str,
    *,
    alias: Optional[str] = None,
    session: Optional[aiohttp.ClientSession] = None,
) -> Dict[str, Optional[str]]:
    """
    Shortens `url` via TinyURL's POST /create endpoint.

    `alias` maps to TinyURL's `alias` field (registry.py's
    supports_alias=True for this provider) -- an already-taken or
    otherwise-invalid alias surfaces as a TinyURLAPIError carrying
    TinyURL's own message, same as every other provider's approach to
    provider-side validation.

    TinyURL's `tags`, `expires_at`, and `description` create-time fields
    are deliberately not exposed here -- per TinyURL's own "Try it out"
    walkthrough, those "will only work with paid subscriptions," so this
    module only ever sends what a Free Plan token can actually use.

    Returns {"short_url": ..., "deletion_url": None} -- see this module's
    docstring for why deletion_url is always None.

    Raises TinyURLAPIError on a non-2xx response, a non-JSON response, or
    a response TinyURL itself flagged as failed.
    """
    body: Dict[str, str] = {"url": url, "domain": DEFAULT_DOMAIN}
    if alias:
        body["alias"] = alias

    data = await _request("POST", "/create", json=body, session=session)

    result = data.get("data") or {}
    short_url = result.get("tiny_url")
    if not short_url:
        raise TinyURLAPIError("TinyURL's response was missing the expected tiny_url field.")

    return {"short_url": short_url, "deletion_url": None}

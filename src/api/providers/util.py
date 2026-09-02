"""
api.providers.util -- the handful of things every provider module in this
package would otherwise reimplement identically. Kept separate from
api.providers.errors (the shared exception base) since not every importer
of one needs the other -- registry.py, for instance, only needs
extract_short_code, never get_session or require_key.
"""

from typing import Optional, Type

import aiohttp

from api.tls import get_ssl_context
from .errors import ProviderAPIError


async def get_session(session: Optional[aiohttp.ClientSession], timeout: aiohttp.ClientTimeout):
    """Reuses a passed-in session, or opens (and flags for closing) a new
    one -- same convention as api.github._get_session, so a caller that
    already holds an open session (e.g. a command that talks to more than
    one provider, or a provider and GitHub, in one interaction) can share
    it instead of opening a second. `timeout` is only applied to a
    freshly-opened session -- a passed-in one keeps whatever timeout it was
    already opened with.

    A freshly-opened session is wired to api.tls.get_ssl_context()'s
    certifi-backed CA bundle rather than aiohttp's OS-trust-store-
    dependent default -- see that module's docstring for why a provider
    whose certificate chain the host's own (possibly incomplete) CA store
    doesn't recognize shouldn't surface as a certificate error."""
    if session is not None:
        return session, False
    connector = aiohttp.TCPConnector(ssl=get_ssl_context())
    return aiohttp.ClientSession(timeout=timeout, connector=connector), True


def describe_network_error(e: Exception, timeout: aiohttp.ClientTimeout) -> str:
    """Turns a caught aiohttp.ClientError/TimeoutError into the detail
    every provider module's own "Couldn't reach <X>: {detail}" message
    interpolates -- centralized for the same reason require_key() below
    is, so the wording doesn't drift module to module.

    A plain aiohttp.ClientError already stringifies to something useful
    (a connection-refused/DNS/etc. message), so str(e) covers that case
    as-is. A request that instead exceeded its aiohttp.ClientTimeout
    raises TimeoutError (asyncio.TimeoutError -- the same object as the
    builtin TimeoutError since Python 3.11) with an EMPTY str(e), which
    is why every provider module's own `except aiohttp.ClientError`
    never caught it at all: TimeoutError isn't a subclass of
    aiohttp.ClientError, so it was escaping as a raw, unhandled
    "TimeoutError:" instead of this package's normal ProviderAPIError
    handling. That case is spelled out explicitly here instead, using
    `timeout`'s own configured total so the message states a real number
    rather than a vague "it was slow"."""
    if isinstance(e, TimeoutError):
        total = timeout.total
        return f"timed out after {total:.0f}s" if total else "timed out"
    return str(e)


def require_key(value: Optional[str], env_var: str, provider_label: str, error_cls: Type[ProviderAPIError]) -> str:
    """Every optional provider (everything except e-z.host -- see
    api/config.py) fails the same way when its key was never configured: a
    clear, friendly error naming exactly which .env variable to set, raised
    the moment someone actually picks that provider rather than at boot.
    Centralized here so that message stays worded identically across every
    provider module instead of drifting module to module."""
    if not value:
        raise error_cls(
            f"{provider_label} isn't configured on this bot -- {env_var} is missing from its .env file."
        )
    return value


def extract_short_code(short_url: str) -> str:
    """Pulls the short/paste/file code (e.g. "abc123") out of a URL a
    provider handed back (a shortened link, a paste link, or a file link)
    -- used as that entry's key under storage/shortened-urls.json's
    per-provider namespace, in whichever of "shorten"/"paste"/"file" is the
    right sub-namespace for the call site.

    Generic across every provider in api/providers/registry.py (originally
    written for e-z.host alone, back when it was the only one -- the logic
    itself, grab the last path segment, was never e-z.host-specific), not
    just shortener-specific despite the name. Uses the URL's path rather
    than a raw string split so a stray query string or trailing slash can't
    end up baked into the key.
    """
    from urllib.parse import urlparse

    path = urlparse(short_url).path
    return path.rstrip("/").rsplit("/", 1)[-1]
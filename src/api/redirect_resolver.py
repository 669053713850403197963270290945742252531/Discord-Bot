"""
api.redirect_resolver -- follows an HTTP redirect chain hop by hop,
reading only response headers (a hop's body is never downloaded), for
/url unshorten's fallback path when a URL isn't one this bot's own
GitHub-backed store (storage/shortened-urls.json, see
api.github.find_shortened_url_entry) already has a record of.

Every hop is validated by api.ssrf_guard.validate_url() *before* the
request for that hop is made -- see that module's docstring for why
this has to happen per-hop rather than just once on the URL the user
typed in: a redirect chain can point anywhere on a later hop regardless
of how safe the first URL looked.

Only the `Location` response header is a documented, standards-based
way a server names a redirect target (RFC 7231 SS7.1.2) -- everything
else this module checks (_ALT_LOCATION_HEADERS, and the `Refresh`
header) is an unconfirmed, best-effort fallback for shorteners that
might not use Location the normal way. Nothing in this codebase has
tested that assumption against a live shortener the way
storage/test_ez_host_api.py did for e-z.host -- there was no equivalent
test target for this -- so treat the fallback list as a reasonable
guess, not a confirmed behavior, and worth re-checking against whatever
shorteners this bot actually gets pointed at in practice.
"""

import re
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import aiohttp

from api.ssrf_guard import SSRFBlockedError, build_pinned_connector, validate_url

# Standard header (RFC 7231 SS7.1.2). Checked first, and on its own
# covers every shortener this bot has actually been tested against
# (e-z.host included -- see api/providers/ez_host.py).
_LOCATION_HEADER = "Location"

# Non-standard fallbacks -- header names some real products are known to
# use instead of (or alongside) Location for a redirect target.
# Deliberately short and specific rather than a broad guess, so an
# unrelated header can't get mistaken for a redirect target:
#   - X-Redirect-To / X-Redirect-Location: used by some link-shortener
#     and CDN products as a custom redirect-target header.
#   - X-Amz-Website-Redirect-Location: S3 static-website hosting's own
#     per-object redirect header, distinct from S3's unrelated
#     x-amz-* metadata headers.
_ALT_LOCATION_HEADERS = (
    "X-Redirect-To",
    "X-Redirect-Location",
    "X-Amz-Website-Redirect-Location",
)

# HTTP's "Refresh" response header -- distinct from the two above: its
# value isn't a bare URL, it's "<seconds>;url=<target>", the same
# syntax as an HTML <meta http-equiv="refresh"> tag, just delivered as
# a header instead of in the body. A handful of older redirect/hosting
# products have been known to emit this instead of Location. Parsed
# separately below since its value needs splitting before the target
# URL is usable.
_REFRESH_HEADER = "Refresh"
_REFRESH_URL_RE = re.compile(r"url\s*=\s*(.+)", re.IGNORECASE)

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)

# Sanity caps, not confirmed limits from any spec -- same "generous
# enough for real use, tight enough to stop a runaway chain" convention
# as commands/url.py's MAX_FILE_ATTACHMENT_SIZE. max_hops caps how many
# redirects get followed; overall_timeout caps the whole chain's wall-
# clock time regardless of hop count, since a chain of many
# individually-fast hops could otherwise still run long.
DEFAULT_MAX_HOPS = 10
DEFAULT_OVERALL_TIMEOUT = 30.0
_PER_HOP_TIMEOUT = aiohttp.ClientTimeout(total=10)


class RedirectResolutionError(Exception):
    """Raised for anything that stops the chain from resolving that
    isn't an SSRF rejection (see api.ssrf_guard.SSRFBlockedError, raised
    separately and left to propagate as-is so callers can tell "blocked
    on purpose" apart from "failed to resolve"): too many hops, a
    redirect status with no usable target header, a network-level
    failure, or the overall time budget running out. `str(error)` is
    user-presentable, same convention as this codebase's other
    *Error classes."""


@dataclass
class RedirectResult:
    start_url: str
    final_url: str
    hop_count: int  # number of redirects actually followed; 0 if start_url was already final
    chain: List[str] = field(default_factory=list)  # every URL visited, start_url first, final_url last


def _extract_redirect_target(headers, current_url: str) -> Optional[str]:
    """Pulls the next hop's target out of a response's `headers`, or
    None if nothing recognizable is there. Relative targets (a bare
    path, or a scheme-relative //host/path) are resolved against
    `current_url` via urljoin -- Location is technically supposed to be
    absolute per RFC 7231, but plenty of real servers send relative
    ones anyway and browsers resolve them, so this matches that
    real-world behavior instead of the strict spec."""
    location = headers.get(_LOCATION_HEADER)
    if location:
        return urljoin(current_url, location.strip())

    for name in _ALT_LOCATION_HEADERS:
        value = headers.get(name)
        if value:
            return urljoin(current_url, value.strip())

    refresh = headers.get(_REFRESH_HEADER)
    if refresh:
        match = _REFRESH_URL_RE.search(refresh)
        if match:
            target = match.group(1).strip().strip("\"'")
            if target:
                return urljoin(current_url, target)

    return None


async def follow_redirects(
    start_url: str,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    overall_timeout: float = DEFAULT_OVERALL_TIMEOUT,
) -> RedirectResult:
    """Follows `start_url` through up to `max_hops` redirect responses
    (301/302/303/307/308), reading only each hop's headers -- never a
    hop's body, since /url unshorten only ever needs to report the
    chain's final destination, not fetch its content.

    Every hop -- including start_url itself -- is validated by
    api.ssrf_guard.validate_url() before it's requested, and the exact
    addresses that validation resolved are pinned for that hop's own
    request via api.ssrf_guard.build_pinned_connector() (see that
    module's docstring for why). A hop that fails validation stops the
    chain immediately by letting SSRFBlockedError propagate -- an
    unsafe hop is a reason to stop, not a reason to keep guessing at
    the destination past it.

    Raises:
      - SSRFBlockedError (from api.ssrf_guard), left to propagate as-is,
        if any hop fails the scheme/destination-IP checks.
      - RedirectResolutionError for anything else that stops the chain
        from resolving: too many hops, a redirect status with no usable
        target header, a network-level failure, or overall_timeout
        being exceeded.
    """
    started = time.monotonic()
    current = start_url
    chain = [start_url]

    # hop_number 0 is the initial request against start_url -- not yet
    # a "hop" in the result's hop_count sense. hop_number 1..max_hops
    # are requests made *because* the previous response redirected, so
    # up to max_hops+1 requests total get made before giving up.
    for hop_number in range(max_hops + 1):
        if time.monotonic() - started > overall_timeout:
            raise RedirectResolutionError(
                f"this redirect chain took longer than {overall_timeout:.0f}s to resolve -- gave up."
            )

        ips = await validate_url(current)  # SSRFBlockedError propagates as-is
        parsed = urlparse(current)
        connector = build_pinned_connector(parsed.hostname, ips)

        async with aiohttp.ClientSession(connector=connector, timeout=_PER_HOP_TIMEOUT) as session:
            try:
                async with session.get(current, allow_redirects=False) as resp:
                    status = resp.status
                    if status not in _REDIRECT_STATUSES:
                        return RedirectResult(start_url=start_url, final_url=current, hop_count=hop_number, chain=chain)
                    target = _extract_redirect_target(resp.headers, current)
            except aiohttp.ClientError as e:
                raise RedirectResolutionError(f"couldn't reach {current}: {e}") from e

        if not target:
            raise RedirectResolutionError(
                f"got a {status} redirect from {current} with no usable Location (or recognized fallback) header."
            )

        current = target
        chain.append(current)

    raise RedirectResolutionError(f"gave up after following {max_hops} redirects without reaching a final destination.")

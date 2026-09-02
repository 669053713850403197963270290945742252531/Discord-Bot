"""
api.redirect_resolver -- follows an HTTP redirect chain hop by hop, for
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

A hop's body is only ever read as a last resort, and only a capped
prefix of it (_META_REFRESH_SCAN_LIMIT), never the whole thing: a
genuine HTTP 3xx (the common case, headers-only, no body read at all)
is always preferred, and a non-redirect status other than 200 is still
treated as final immediately, same as before. What changed is 200
specifically -- a real-world shortener (e.g. shorturl.at) was found
(2026-08-24) serving an interstitial page at 200 whose actual redirect
is an HTML `<meta http-equiv="refresh" content="0;url=...">` tag rather
than any HTTP-level mechanism, which every check above this one is
blind to since it lives in the body, not the headers. Reading a small
capped prefix of an HTML-labeled 200 response and looking for that one
specific tag closes that gap without turning this into a general HTML
parser or a JS-redirect follower (out of scope -- see
_extract_meta_refresh_target()'s own docstring).
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

# Case-insensitive, tolerant of attribute order (http-equiv before or
# after content, either quote style or none) since real-world markup
# isn't guaranteed to match any one canonical form. Two-step rather than
# one giant regex: find each <meta ...> tag first, then check that
# specific tag for http-equiv="refresh" and pull its content attribute
# -- keeps a stray http-equiv/content pair in a *different* meta tag (or
# anywhere else in the scanned prefix) from being mismatched together.
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_HTTP_EQUIV_REFRESH_RE = re.compile(r"""http-equiv\s*=\s*["']?refresh["']?""", re.IGNORECASE)
_CONTENT_ATTR_RE = re.compile(r"""content\s*=\s*["']([^"']*)["']""", re.IGNORECASE)

# Only a 200 (not a 3xx -- those are handled above via headers alone,
# and never even reach this path -- and not a 4xx/5xx error page, which
# stays "final destination" same as before this existed) gets its body
# scanned at all, and only up to this many bytes of it: real-world
# meta-refresh interstitials put the tag early in <head>, so a prefix
# this size is already generous headroom without risking a slow read of
# a large/streaming response that was never going anywhere near a
# redirect tag in the first place.
_META_REFRESH_SCAN_LIMIT = 65536


def _extract_meta_refresh_target(html: str, current_url: str) -> Optional[str]:
    """Best-effort scan of an already-capped HTML prefix for a
    `<meta http-equiv="refresh" content="<seconds>;url=<target>">` tag
    (same content-attribute syntax as the `Refresh` response header --
    reuses _REFRESH_URL_RE below to parse it), returning the resolved
    target or None if no such tag is present in what was scanned.

    Deliberately narrow: this is the one client-side redirect mechanism
    common enough among real shorteners/interstitials (confirmed against
    shorturl.at, 2026-08-24) to be worth this module reaching into a
    body at all. A JavaScript-only redirect (`location.href = ...`, a
    `<script>`-driven "click to continue" page, etc.) is NOT handled --
    doing that safely would mean either running untrusted JS or writing
    a much broader, much less reliable heuristic, neither of which is
    worth it for what's meant to stay a lightweight fallback."""
    for tag in _META_TAG_RE.findall(html):
        if not _HTTP_EQUIV_REFRESH_RE.search(tag):
            continue
        content_match = _CONTENT_ATTR_RE.search(tag)
        if not content_match:
            continue
        url_match = _REFRESH_URL_RE.search(content_match.group(1))
        if not url_match:
            continue
        target = url_match.group(1).strip().strip("\"'")
        if target:
            return urljoin(current_url, target)
    return None


def _looks_like_html(resp: aiohttp.ClientResponse) -> bool:
    """True if `resp`'s Content-Type gives a real signal it's HTML (or
    is missing/generic enough that it plausibly could be) -- False for
    anything with a Content-Type clearly naming something else (an
    image, a PDF, JSON, etc.), so the meta-refresh scan below never
    bothers reading into a response that was never going to contain an
    HTML tag in the first place. aiohttp normalizes a missing
    Content-Type to "application/octet-stream" -- treated here the same
    as truly absent, since plenty of misconfigured servers send that
    for an HTML body too."""
    content_type = (resp.content_type or "").lower()
    if content_type in ("", "application/octet-stream"):
        return True
    return "html" in content_type or content_type.startswith("text/")


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
    """Follows `start_url` through up to `max_hops` redirect hops --
    either a real HTTP 301/302/303/307/308 (read from headers alone, no
    body ever touched) or, for a 200 OK specifically, an HTML
    `<meta http-equiv="refresh">` tag found in a small capped prefix of
    the body (see _extract_meta_refresh_target()'s docstring for why
    just this one client-side mechanism, and _META_REFRESH_SCAN_LIMIT
    for the cap) -- since /url unshorten only ever needs to report the
    chain's final destination, not fetch its content, every other status
    (a 4xx/5xx error page, or a 200 with no such tag found) is still
    treated as that hop being the final destination, same as before this
    existed.

    Every hop -- including start_url itself -- is validated by
    api.ssrf_guard.validate_url() before it's requested, and the exact
    addresses that validation resolved are pinned for that hop's own
    request via api.ssrf_guard.build_pinned_connector() (see that
    module's docstring for why). A hop that fails validation stops the
    chain immediately by letting SSRFBlockedError propagate -- an
    unsafe hop is a reason to stop, not a reason to keep guessing at
    the destination past it. A meta-refresh target discovered in a
    body gets exactly the same per-hop validation as any other target
    before it's ever requested -- it becomes `current` for the next
    loop iteration same as a Location-header target would.

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
                    if status in _REDIRECT_STATUSES:
                        target = _extract_redirect_target(resp.headers, current)
                    elif status == 200 and _looks_like_html(resp):
                        prefix = await resp.content.read(_META_REFRESH_SCAN_LIMIT)
                        html = prefix.decode("utf-8", errors="replace")
                        target = _extract_meta_refresh_target(html, current)
                        if not target:
                            return RedirectResult(start_url=start_url, final_url=current, hop_count=hop_number, chain=chain)
                    else:
                        return RedirectResult(start_url=start_url, final_url=current, hop_count=hop_number, chain=chain)
            except aiohttp.ClientError as e:
                raise RedirectResolutionError(f"couldn't reach {current}: {e}") from e

        if not target:
            raise RedirectResolutionError(
                f"got a {status} redirect from {current} with no usable Location (or recognized fallback) header."
            )

        current = target
        chain.append(current)

    raise RedirectResolutionError(f"gave up after following {max_hops} redirects without reaching a final destination.")
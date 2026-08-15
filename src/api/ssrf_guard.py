"""
api.ssrf_guard -- validates a URL is safe to fetch server-side before
this bot ever opens a socket to it.

Built for /url unshorten's live-redirect-following fallback (see
api.redirect_resolver): every other command that talks to an external
host either fetches a URL *this bot* generated (e-z.host, GitHub) or
never fetches attacker-influenced URLs at all. /url unshorten is the
first command where a user hands the bot an arbitrary URL and the bot
goes and fetches it -- classic SSRF shape, so this exists to stand
between the two.

Two things are enforced by validate_url() below, on *every* hop of a
redirect chain (not just the URL the user typed in -- see
api.redirect_resolver, which calls this before each individual request,
since a server can return a perfectly public-looking first URL and then
302 somewhere internal on a later hop):

  1. Scheme allowlist: http/https only. Blocks file://, ftp://,
     gopher://, data:, javascript:, and anything else -- there's no
     legitimate reason a redirect chain that started as an http(s) link
     should ever need to leave that scheme family.

  2. Destination-IP blocklist: the hostname is resolved via DNS, and
     *every* address that resolution returns is checked against
     ipaddress's is_loopback / is_link_local / is_private / is_reserved
     / is_multicast / is_unspecified. Rejected if any of them match --
     not just the first -- since a multi-answer DNS response only needs
     one bad address in it to make this exploitable. is_private alone
     already covers loopback, link-local, and RFC1918 space under
     Python's own iana-special-registry-based definition, but the
     others are checked explicitly too: cheap, and this is exactly the
     kind of check where redundancy beats relying on one property's
     documented scope never changing across Python versions.

     This deliberately also blocks 169.254.169.254 (the AWS/GCP/Azure
     cloud-metadata address) as a side effect of the link-local check --
     worth calling out explicitly, since it's the single highest-value
     address this guard exists to block, even though the request that
     prompted this only named loopback/link-local/RFC1918 in the
     abstract.

Resolution happens once per hop, via a pinned aiohttp resolver
(_PinnedResolver below) rather than letting aiohttp's connector
re-resolve the hostname itself right before connecting. Otherwise the
addresses this module validates and the address aiohttp actually opens
a socket to could come from two different DNS answers -- a classic
rebinding gap: nothing stops a malicious DNS server from answering a
validation-time query and a connect-time query differently, especially
with a low/zero TTL. Pinning means whatever passed validation is
exactly what gets connected to; build_pinned_connector() is how a
caller wires that up for the request that follows a successful
validate_url() call.

None of this has been exercised against a real malicious redirect chain
or a real DNS-rebinding attempt -- it's built to the stated
requirements and reasoned through, not confirmed live the way
e-z.host's endpoints were in storage/test_ez_host_api.py. Worth a
deliberate test (e.g. a throwaway redirect pointing at
169.254.169.254 or 127.0.0.1) before leaning on it in production.
"""

import asyncio
import socket
from typing import List, Optional, Union
from urllib.parse import urlparse

import aiohttp
from aiohttp.abc import AbstractResolver
import ipaddress

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

# http/https only -- see module docstring point 1.
ALLOWED_SCHEMES = ("http", "https")


class SSRFBlockedError(Exception):
    """Raised when a URL (or one hop of a redirect chain) fails the
    scheme or destination-IP checks. `str(error)` is written to be
    shown to the user as-is -- it only ever names the scheme/address
    class that was rejected, never anything else about the target, so
    it's safe to surface directly (same convention as
    EZHostAPIError/GitHubAPIError)."""


def _is_blocked_ip(ip: IPAddress) -> bool:
    """True if `ip` falls into any address class this guard exists to
    keep the bot from ever connecting to -- see module docstring point
    2 for why each of these is checked explicitly rather than relying
    on is_private alone."""
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def _resolve_all(hostname: str) -> List[IPAddress]:
    """Resolves `hostname` to every address DNS hands back (A and AAAA
    alike), deduplicated. Raises SSRFBlockedError on a resolution
    failure -- an unresolvable host can't be fetched anyway, so this
    folds cleanly into the same "can't proceed" path as an address that
    resolved but got blocked."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SSRFBlockedError(f"couldn't resolve {hostname!r}: {e}")

    ips: List[IPAddress] = []
    seen = set()
    for _family, _type, _proto, _canonname, sockaddr in infos:
        ip_str = sockaddr[0]
        if ip_str in seen:
            continue
        seen.add(ip_str)
        ips.append(ipaddress.ip_address(ip_str))
    return ips


async def validate_url(url: str) -> List[IPAddress]:
    """Checks `url`'s scheme against ALLOWED_SCHEMES and resolves+checks
    its hostname's addresses against the destination-IP blocklist (see
    module docstring). Returns the resolved, validated addresses on
    success -- a caller that goes on to fetch `url` should hand these to
    build_pinned_connector() rather than letting the request resolve
    the hostname a second time (see the module docstring's rebinding
    note).

    Raises SSRFBlockedError on any failure -- bad scheme, no host,
    resolution failure, or a resolved address that's blocked.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise SSRFBlockedError(f"scheme {parsed.scheme!r} isn't allowed here -- only http/https.")
    if not parsed.hostname:
        raise SSRFBlockedError("that URL has no host to resolve.")

    # An IP literal in the URL (e.g. http://127.0.0.1/...) skips DNS
    # entirely -- ip_address() parses it directly rather than going
    # through getaddrinfo, but still gets exactly the same blocklist
    # check as a resolved hostname would.
    try:
        literal = ipaddress.ip_address(parsed.hostname.strip("[]"))
    except ValueError:
        literal = None

    ips = [literal] if literal is not None else await _resolve_all(parsed.hostname)
    if not ips:
        raise SSRFBlockedError(f"couldn't resolve {parsed.hostname!r} to any address.")

    blocked = [ip for ip in ips if _is_blocked_ip(ip)]
    if blocked:
        raise SSRFBlockedError(
            f"{parsed.hostname!r} resolves to a non-public address ({blocked[0]}) -- refusing to fetch it."
        )

    return ips


class _PinnedResolver(AbstractResolver):
    """An aiohttp resolver that hands back exactly the addresses
    validate_url() already checked, instead of letting aiohttp's own
    resolver look the hostname up again right before connecting. See
    the module docstring's rebinding note for why that second lookup is
    the actual gap this closes.

    Scoped to a single hostname+address-list, built fresh per hop by
    build_pinned_connector() below -- not a general-purpose cache, and
    not meant to be reused across different hostnames.

    `resolve()`'s `host`/`family` parameters are part of aiohttp's
    AbstractResolver interface (aiohttp calls this internally) but are
    intentionally ignored here: this resolver only ever has one
    hostname's worth of pre-validated addresses to hand back regardless
    of what's asked for, since a fresh instance is built per hop.
    """

    def __init__(self, hostname: str, ips: List[IPAddress]):
        self._hostname = hostname
        self._ips = ips

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
        return [
            {
                "hostname": self._hostname,
                "host": str(ip),
                "port": port,
                "family": socket.AF_INET6 if ip.version == 6 else socket.AF_INET,
                "proto": 0,
                "flags": 0,
            }
            for ip in self._ips
        ]

    async def close(self) -> None:
        pass


def build_pinned_connector(hostname: str, ips: List[IPAddress]) -> aiohttp.TCPConnector:
    """A TCPConnector wired to _PinnedResolver -- pass this to the
    ClientSession that actually fetches `hostname`, immediately after
    validate_url() has already resolved+checked it, so the connect-time
    lookup and the validation-time lookup can never disagree. TLS
    verification (SNI + certificate hostname check) is unaffected --
    aiohttp still negotiates those against `hostname` itself, only the
    socket-level address comes from the pin.
    """
    return aiohttp.TCPConnector(resolver=_PinnedResolver(hostname, ips))

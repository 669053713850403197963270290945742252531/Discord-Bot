"""
api.tls -- a shared ssl.SSLContext for this bot's outbound HTTPS
connections (aiohttp), built from certifi's bundled CA certificates
instead of whatever CA trust store (if any) is installed on the host OS.

Why this exists: leaving aiohttp's `ssl` kwarg unset (or `True`) makes it
ask Python's own ssl module for its "default" verify context, which in
turn reads whatever CA bundle the host OS has installed -- fine on a
normal desktop/dev machine, but several minimal container images
(observed on this bot's own Render deployment) don't ship a complete,
up-to-date system CA bundle. The practical symptom isn't every HTTPS
request failing -- it's some sites verifying fine (whichever root/
intermediate chain happens to already be present) while others fail with
`[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to
get local issuer certificate`, even though the target's certificate
itself is perfectly valid -- e.g. this bot's own /url unshorten hitting
that error against bit.ly while everything else (Discord's API/gateway,
GitHub's API) kept working.

certifi ships Mozilla's own curated root CA bundle as an installable
Python package specifically to route around this class of problem --
pointing every ssl context this bot creates at certifi.where() makes
certificate verification behave the same way regardless of what (if
anything) the host OS has installed, rather than depending on it.

get_ssl_context() is built once and cached: an ssl.SSLContext has no
per-request state, so every caller in this process sharing the same
instance is safe and avoids re-parsing certifi's bundle on every
connection.
"""

import ssl
from functools import lru_cache

import certifi


@lru_cache(maxsize=1)
def get_ssl_context() -> ssl.SSLContext:
    """Returns this process's shared, verifying ssl.SSLContext, trusting
    certifi's CA bundle rather than the host OS's own trust store. Pass
    this as aiohttp's `ssl=` kwarg (on a TCPConnector, or directly on a
    request) anywhere this bot opens an HTTPS connection."""
    return ssl.create_default_context(cafile=certifi.where())

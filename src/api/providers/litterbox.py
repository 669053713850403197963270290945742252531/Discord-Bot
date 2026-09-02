"""
api.providers.litterbox -- thin async wrapper around Litterbox's
temporary-file-hosting API (https://litterbox.catbox.moe/tools.php), for
/file's `litterbox` provider choice. Litterbox is catbox.moe's sister
service for time-limited uploads -- same request shape and response
conventions as api.providers.catbox, different endpoint and one extra
mandatory field.

Confirmed shape: POST multipart/form-data to
https://litterbox.catbox.moe/resources/internals/api.php with
`reqtype=fileupload`, a mandatory `time` (one of "1h", "12h", "24h", "72h"
-- Litterbox's own four supported durations, per its tools.php page),
and the file as `fileToUpload`. Response conventions match catbox.moe
exactly: plain-text URL on success, plain-text error message otherwise,
no JSON envelope or distinguishing HTTP status.

Litterbox has no account system at all -- every upload is anonymous and
auto-deletes when `time` elapses, so there's no userhash-style config
here (unlike catbox.py's optional CATBOX_USERHASH) and upload_file()
below always returns deletion_url=None, exactly as the planning doc calls
out ("Litterbox and free-tier TinyURL don't hand one back").
"""

from typing import Dict, Optional

import aiohttp

from api.providers.errors import ProviderAPIError
from api.providers.util import describe_network_error as _describe_network_error
from api.providers.util import get_session as _get_session_shared

BASE_URL = "https://litterbox.catbox.moe/resources/internals/api.php"

# Litterbox's own tools.php documents the same four durations this
# module validates against -- kept here (rather than only in
# commands/url.py's app_commands.choices) so a direct call into this
# module still fails fast with a clear message instead of forwarding a
# bogus `time` value to Litterbox and getting back a less useful error.
VALID_EXPIRIES = ("1h", "12h", "24h", "72h")
DEFAULT_EXPIRY = "1h"

# Matches api.providers.catbox's 60s -- same upload-latency reasoning
# applies to Litterbox's sibling endpoint.
_TIMEOUT = aiohttp.ClientTimeout(total=60)


class LitterboxAPIError(ProviderAPIError):
    """Raised whenever a Litterbox upload doesn't succeed -- a non-200
    HTTP status, an invalid `time` value, or a 200 response whose
    plain-text body doesn't look like a Litterbox file URL (see
    api.providers.catbox.CatboxAPIError, whose sibling failure signal
    this mirrors exactly)."""


async def _get_session(session: Optional[aiohttp.ClientSession]):
    return await _get_session_shared(session, _TIMEOUT)


async def upload_file(
    filename: str,
    content: bytes,
    content_type: Optional[str] = None,
    *,
    expiry: str = DEFAULT_EXPIRY,
    session: Optional[aiohttp.ClientSession] = None,
) -> Dict[str, Optional[str]]:
    """
    Uploads `content` (as `filename`) to Litterbox, expiring after
    `expiry` (one of VALID_EXPIRIES; registry.py's requires_expiry=True
    for this provider means commands/url.py always passes one -- defaults
    to DEFAULT_EXPIRY here purely as a defensive fallback for direct
    callers of this module).

    Returns {"file_url": ..., "deletion_url": None} -- see this module's
    docstring for why deletion_url is always None here.

    Raises LitterboxAPIError on an invalid `expiry`, a non-200 response,
    or a body that doesn't look like a successful Litterbox URL.
    """
    if expiry not in VALID_EXPIRIES:
        raise LitterboxAPIError(
            f"'{expiry}' isn't a valid Litterbox expiry -- choose one of {', '.join(VALID_EXPIRIES)}."
        )

    form = aiohttp.FormData()
    form.add_field("reqtype", "fileupload")
    form.add_field("time", expiry)
    form.add_field("fileToUpload", content, filename=filename, content_type=content_type)

    sess, should_close = await _get_session(session)
    try:
        try:
            async with sess.post(BASE_URL, data=form) as resp:
                body = (await resp.text()).strip()
                if resp.status != 200:
                    raise LitterboxAPIError(f"Litterbox returned HTTP {resp.status}: {body}")
        except (aiohttp.ClientError, TimeoutError) as e:
            raise LitterboxAPIError(f"Couldn't reach Litterbox: {_describe_network_error(e, _TIMEOUT)}")
    finally:
        if should_close:
            await sess.close()

    if not body.startswith("https://litter.catbox.moe/"):
        raise LitterboxAPIError(body or "Litterbox returned an empty response.")

    return {"file_url": body, "deletion_url": None}
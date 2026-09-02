"""
api.providers.catbox -- thin async wrapper around catbox.moe's file-hosting
API (https://catbox.moe/tools.php), for /file's `catbox` provider choice.

Confirmed shape (multiple independent working implementations agree):
POST multipart/form-data to https://catbox.moe/user/api.php with fields
`reqtype=fileupload`, optional `userhash`, and the file itself as
`fileToUpload`. On success the response body is `text/plain` containing
just the file's URL (e.g. "https://files.catbox.moe/abc123.png"); on
failure it's a plain-text error message instead -- there's no JSON
envelope or HTTP-status signal to distinguish the two, so this module
distinguishes them by checking whether the body actually looks like a
catbox.moe URL.

config.CATBOX_USERHASH is optional and purely a courtesy: catbox uploads
are anonymous by default, and an anonymous upload can never be deleted
through the API (deletion needs a userhash tied to an account, not a
per-upload secret) -- setting CATBOX_USERHASH just makes this bot's own
uploads land in one catbox.moe account instead of scattering across
anonymous, permanently-undeletable ones. Either way, upload_file() below
always returns deletion_url=None: catbox deletion is an authenticated API
action (userhash + filename), never a URL, so there's nothing URL-shaped
to hand back regardless of whether a userhash was used.
"""

from typing import Dict, Optional

import aiohttp

from api import config
from api.providers.errors import ProviderAPIError
from api.providers.util import describe_network_error as _describe_network_error
from api.providers.util import get_session as _get_session_shared

BASE_URL = "https://catbox.moe/user/api.php"

# catbox.moe has no documented SLA; large files can also legitimately take
# a while (see this module's docstring's linked CatboxUploader project,
# which notes catbox can drop long-held connections on slow links), so
# this is longer than the other providers' 15s default rather than a
# straight copy of it.
_TIMEOUT = aiohttp.ClientTimeout(total=60)


class CatboxAPIError(ProviderAPIError):
    """Raised whenever a catbox.moe upload doesn't succeed -- a non-200
    HTTP status, or a 200 response whose plain-text body doesn't look like
    a catbox.moe file URL (catbox's own failure signal, per this module's
    docstring)."""


async def _get_session(session: Optional[aiohttp.ClientSession]):
    return await _get_session_shared(session, _TIMEOUT)


async def upload_file(
    filename: str,
    content: bytes,
    content_type: Optional[str] = None,
    *,
    session: Optional[aiohttp.ClientSession] = None,
) -> Dict[str, Optional[str]]:
    """
    Uploads `content` (as `filename`) to catbox.moe.

    Returns {"file_url": ..., "deletion_url": None} -- see this module's
    docstring for why deletion_url is always None here.

    Raises CatboxAPIError on a non-200 response or a body that doesn't
    look like a successful catbox.moe URL -- catbox's own error text
    (e.g. a rejected file extension or a size-limit message) is preserved
    in the exception rather than papered over.
    """
    form = aiohttp.FormData()
    form.add_field("reqtype", "fileupload")
    if config.CATBOX_USERHASH:
        form.add_field("userhash", config.CATBOX_USERHASH)
    form.add_field("fileToUpload", content, filename=filename, content_type=content_type)

    sess, should_close = await _get_session(session)
    try:
        try:
            async with sess.post(BASE_URL, data=form) as resp:
                body = (await resp.text()).strip()
                if resp.status != 200:
                    raise CatboxAPIError(f"catbox.moe returned HTTP {resp.status}: {body}")
        except (aiohttp.ClientError, TimeoutError) as e:
            raise CatboxAPIError(f"Couldn't reach catbox.moe: {_describe_network_error(e, _TIMEOUT)}")
    finally:
        if should_close:
            await sess.close()

    if not body.startswith("https://files.catbox.moe/"):
        raise CatboxAPIError(body or "catbox.moe returned an empty response.")

    return {"file_url": body, "deletion_url": None}
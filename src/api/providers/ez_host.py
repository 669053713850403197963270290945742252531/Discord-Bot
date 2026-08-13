"""
e-z.host API client -- HTTP layer for whatever /url and /upload commands end
up calling this provider (URL shortening today; e-z.host also offers file
upload and paste under the same account/API key, for later).

This is foundation only for now: the session-reuse helper and error type
every request will share. The actual request functions (shorten_url(),
etc.) come with the commands that call them.

Endpoint/schema notes, confirmed against a live, paid e-z.host account (see
storage/test_ez_host_api.py, the throwaway script that pinned these down --
the published `ez-api` PyPI package installs but ships zero source files,
so there was nothing importable to wrap):

    POST {BASE_URL}/shortener   headers: {"key": <api key>}   json: {"url": ...}
        -> 200 {"success": true, "shortendUrl": ..., "deletionUrl": ...}
           (their field really is "shortendUrl" -- a typo, missing the
           middle "e" -- not "shortenedUrl" like the old docs claimed)
        -> 400 malformed input, 401 bad/expired key, 429 rate limited

    POST {BASE_URL}/files       headers: {"key": <api key>}   multipart, field "file"
        -> 200 {"imageUrl": ..., "deletionUrl": ...}
        -> despite the dashboard calling this "Image uploader, File
           uploader," only real image bytes are accepted -- a plain .txt
           payload reliably 422s with a broken, non-JSON error body

Every request authenticates with a bare `key` header -- not
`Authorization: Bearer ...` like GitHub -- set from config.EZ_HOST_API_KEY.
"""

from typing import Optional

import aiohttp

BASE_URL = "https://api.e-z.host"


class EZHostAPIError(Exception):
    """Raised whenever an e-z.host API call doesn't return a success status.

    Mirrors api.github.GitHubAPIError's shape so callers can handle either
    the same way -- `str(error)` already contains a user-presentable
    message (including the HTTP status), so most commands can just do:

        except EZHostAPIError as e:
            return await send_error(interaction, str(e))
    """

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


async def _get_session(session: Optional[aiohttp.ClientSession]):
    """Reuses a passed-in session, or opens (and flags for closing) a new
    one. Same pattern as api.github._get_session -- kept as its own copy
    rather than imported, so this provider module doesn't need to reach
    into github.py's internals for something this small."""
    if session is not None:
        return session, False
    return aiohttp.ClientSession(), True

"""
api.providers.ez_host -- thin async wrapper around e-z.host's HTTP API
(https://api.e-z.host): the URL shortener (/url shorten), pastes (/paste),
and file/image uploads (/file).

There's no usable official SDK to wrap here: the `ez-api` package on PyPI
installs cleanly but ships zero source files -- both its wheel and sdist
contain only packaging metadata, so `import ez_api` always raises
ModuleNotFoundError (confirmed by installing it in a clean venv and
inspecting the distributed files directly). This module instead talks to
the same endpoints directly over HTTP with aiohttp, the same shapes
storage/test_ez_host_api.py confirmed against a live, paid e-z.host
account before this module was written.

Confirmed quirk (shortener endpoint): the success response's field is
"shortendUrl" -- e-z.host's own typo, missing the middle "e" -- not
"shortenedUrl" like older docs claimed. Both are checked below in case
they ever fix it server-side.

Confirmed quirk (files endpoint): despite the e-z.host dashboard labeling
this destination "Image uploader, File uploader," it only reliably
accepts real image content -- a plain .txt payload gets back a broken,
non-JSON 422 (e-z.host's own error handler crashes trying to report the
validation failure), while a real PNG succeeds cleanly. upload_file()
below doesn't second-guess what it's handed -- see commands/url.py's
/file for the pre-flight content-type check that keeps non-image
attachments from ever reaching this function in the first place.
"""

from typing import Dict, Optional
from urllib.parse import urlparse

import aiohttp

from api import config

BASE_URL = "https://api.e-z.host"

# e-z.host has no documented SLA on response time; 15s matches the
# timeout storage/test_ez_host_api.py used for its (successful) live
# testing, so it's a known-good value rather than a guess.
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class EZHostAPIError(Exception):
    """Raised whenever an e-z.host API call doesn't succeed -- either a
    non-200 HTTP status, or a 200 response body with `"success": false`
    (e-z.host uses that instead of a 4xx for some validation failures).

    `str(error)` already contains a user-presentable message (including
    the HTTP status where one exists), so most callers can just do:

        except EZHostAPIError as e:
            return await send_error(interaction, str(e))
    """

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


async def _get_session(session: Optional[aiohttp.ClientSession]):
    """Reuses a passed-in session, or opens (and flags for closing) a new
    one -- same convention as api.github._get_session, so a caller that
    already holds an open session (e.g. a command that talks to both
    GitHub and e-z.host in one interaction) can share it instead of
    opening a second."""
    if session is not None:
        return session, False
    return aiohttp.ClientSession(timeout=_TIMEOUT), True


async def shorten_url(url: str, session: Optional[aiohttp.ClientSession] = None) -> Dict[str, str]:
    """
    Shortens `url` via e-z.host's POST /shortener endpoint.

    Returns {"short_url": ..., "deletion_url": ...}. Callers should
    persist the deletion_url immediately -- e-z.host hands it back exactly
    once, on creation, and never surfaces it again afterward.

    Raises EZHostAPIError on a non-200 response, a "success": false body,
    a non-JSON response, or a 200 response missing either expected field.
    """
    sess, should_close = await _get_session(session)
    try:
        headers = {"Content-Type": "application/json", "key": config.EZ_HOST_API_KEY}
        try:
            async with sess.post(f"{BASE_URL}/shortener", headers=headers, json={"url": url}) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise EZHostAPIError(f"e-z.host returned HTTP {resp.status}: {err}", resp.status)
                try:
                    data = await resp.json()
                except aiohttp.ContentTypeError:
                    raise EZHostAPIError("e-z.host returned a non-JSON response.", resp.status)
        except aiohttp.ClientError as e:
            raise EZHostAPIError(f"Couldn't reach e-z.host: {e}")
    finally:
        if should_close:
            await sess.close()

    if not data.get("success", True):
        raise EZHostAPIError(f"e-z.host rejected the request: {data.get('message') or data.get('error') or 'unknown error'}")

    short_url = data.get("shortendUrl") or data.get("shortenedUrl")
    deletion_url = data.get("deletionUrl")
    if not short_url or not deletion_url:
        raise EZHostAPIError("e-z.host's response was missing the expected shortened-url/deletion-url fields.")

    return {"short_url": short_url, "deletion_url": deletion_url}


async def create_paste(
    text: str,
    *,
    language: str = "plaintext",
    title: Optional[str] = None,
    description: Optional[str] = None,
    session: Optional[aiohttp.ClientSession] = None,
) -> Dict[str, str]:
    """
    Creates a paste via e-z.host's POST /paste endpoint.

    Returns {"paste_url": ..., "raw_url": ..., "deletion_url": ...}.
    Same capture-immediately requirement as shorten_url()'s deletion_url
    -- e-z.host hands it back exactly once, on creation, and never
    surfaces it again afterward.

    `language` isn't validated against a known list here -- e-z.host's
    own docs don't publish one, and its 400/422 response is still the
    final word on an invalid value, same as is_valid_url()'s approach to
    the shortener's `url`.

    Raises EZHostAPIError on a non-200 response, a "success": false body,
    a non-JSON response, or a 200 response missing any of the three
    expected fields.
    """
    payload: Dict[str, str] = {"text": text, "language": language}
    if title:
        payload["title"] = title
    if description:
        payload["description"] = description

    sess, should_close = await _get_session(session)
    try:
        headers = {"Content-Type": "application/json", "key": config.EZ_HOST_API_KEY}
        try:
            async with sess.post(f"{BASE_URL}/paste", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise EZHostAPIError(f"e-z.host returned HTTP {resp.status}: {err}", resp.status)
                try:
                    data = await resp.json()
                except aiohttp.ContentTypeError:
                    raise EZHostAPIError("e-z.host returned a non-JSON response.", resp.status)
        except aiohttp.ClientError as e:
            raise EZHostAPIError(f"Couldn't reach e-z.host: {e}")
    finally:
        if should_close:
            await sess.close()

    if not data.get("success", True):
        raise EZHostAPIError(f"e-z.host rejected the request: {data.get('message') or data.get('error') or 'unknown error'}")

    paste_url = data.get("pasteUrl")
    raw_url = data.get("rawUrl")
    deletion_url = data.get("deletionUrl")
    if not paste_url or not raw_url or not deletion_url:
        raise EZHostAPIError("e-z.host's response was missing the expected paste-url/raw-url/deletion-url fields.")

    return {"paste_url": paste_url, "raw_url": raw_url, "deletion_url": deletion_url}


async def upload_file(
    filename: str,
    content: bytes,
    content_type: Optional[str] = None,
    session: Optional[aiohttp.ClientSession] = None,
) -> Dict[str, str]:
    """
    Uploads `content` via e-z.host's POST /files endpoint (multipart,
    form field name "file" -- matches the account's own ShareX export;
    no Content-Type header set explicitly since aiohttp derives the
    multipart boundary itself from the FormData body).

    This function doesn't validate `content` is actually an image --
    e-z.host's own response is the final word, and by the time a call
    reaches here that check already happened at the command layer (see
    commands/url.py's /file). Passing non-image bytes anyway will surface
    as an EZHostAPIError wrapping e-z.host's broken 422 body (see the
    module docstring's "files endpoint" note) rather than anything
    friendlier -- that's e-z.host's failure mode, not something this
    function can improve on.

    Returns {"file_url": ..., "deletion_url": ...}. Same capture-
    immediately requirement as shorten_url()'s deletion_url.

    Raises EZHostAPIError on a non-200 response, a "success": false body,
    a non-JSON response, or a 200 response missing either expected field.
    """
    form = aiohttp.FormData()
    form.add_field("file", content, filename=filename, content_type=content_type or "application/octet-stream")

    sess, should_close = await _get_session(session)
    try:
        headers = {"key": config.EZ_HOST_API_KEY}
        try:
            async with sess.post(f"{BASE_URL}/files", headers=headers, data=form) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise EZHostAPIError(f"e-z.host returned HTTP {resp.status}: {err}", resp.status)
                try:
                    data = await resp.json()
                except aiohttp.ContentTypeError:
                    raise EZHostAPIError("e-z.host returned a non-JSON response.", resp.status)
        except aiohttp.ClientError as e:
            raise EZHostAPIError(f"Couldn't reach e-z.host: {e}")
    finally:
        if should_close:
            await sess.close()

    if not data.get("success", True):
        raise EZHostAPIError(f"e-z.host rejected the upload: {data.get('message') or data.get('error') or 'unknown error'}")

    file_url = data.get("imageUrl")
    deletion_url = data.get("deletionUrl")
    if not file_url or not deletion_url:
        raise EZHostAPIError("e-z.host's response was missing the expected image-url/deletion-url fields.")

    return {"file_url": file_url, "deletion_url": deletion_url}


def extract_short_code(short_url: str) -> str:
    """Pulls the short/paste/file code (e.g. "abc123") out of a URL e-z.host
    handed back (e.g. "https://i.e-z.host/abc123", a pasteUrl, or an
    imageUrl) -- used as that entry's key under storage/shortened-urls.json's
    "ez_host" namespace, in whichever of "shorten"/"paste"/"file" is the
    right sub-namespace for the call site. Despite the name, the logic
    (grab the last path segment) isn't shortener-specific -- it's reused
    as-is by /paste and /file. Uses the URL's path rather than a raw
    string split so a stray query string or trailing slash can't end up
    baked into the key."""
    path = urlparse(short_url).path
    return path.rstrip("/").rsplit("/", 1)[-1]
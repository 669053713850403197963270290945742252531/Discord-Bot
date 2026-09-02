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

Confirmed quirk (paste endpoint, 2026-08-17): the same broken-error-
handler crash above isn't files-only -- POST /paste can hit it too, and
its actual trigger is now confirmed: **`title` and `description` are
both required**, unlike every other /paste provider in this package,
where both are optional. Live evidence: a paste with neither set came
back HTTP 422 with e-z.host's own error handler crashed (see the raw
JavaScriptCore/WebKit TypeError body below), while storage/
test_ez_host_api.py's confirmed-working /paste call has always included
both. create_paste() below now enforces this locally -- raising
EZHostAPIError before the request is even sent when either is missing --
rather than letting a doomed request reach e-z.host and come back as
that same cryptic crash. See _describe_http_error() below for how a
request that still somehow reaches that crash (some other, still-
unconfirmed trigger) is now made legible instead of relayed as raw JS.
"""

from typing import Dict, Optional

import aiohttp

from api import config
from api.providers.errors import ProviderAPIError
from api.providers.util import describe_network_error as _describe_network_error
from api.providers.util import extract_short_code as _extract_short_code
from api.providers.util import get_session as _get_session_shared

BASE_URL = "https://api.e-z.host"

# e-z.host has no documented SLA on response time; 15s matches the
# timeout storage/test_ez_host_api.py used for its (successful) live
# testing, so it's a known-good value rather than a guess.
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class EZHostAPIError(ProviderAPIError):
    """Raised whenever an e-z.host API call doesn't succeed -- either a
    non-200 HTTP status, or a 200 response body with `"success": false`
    (e-z.host uses that instead of a 4xx for some validation failures).

    `str(error)` already contains a user-presentable message (including
    the HTTP status where one exists), so most callers can just do:

        except EZHostAPIError as e:
            return await send_error(interaction, str(e))

    Subclasses api.providers.errors.ProviderAPIError so commands/url.py's
    impl functions -- which now dispatch across every provider in
    api/providers/registry.py, not just this one -- can also catch that
    shared base instead of needing an except per provider.
    """

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


# The telltale wording of e-z.host's own broken-error-handler crash (see
# this module's docstring's "files endpoint" and "paste endpoint" notes)
# -- a JavaScriptCore/WebKit TypeError's own message shape ("X is not an
# object (evaluating 'Y')"), not anything e-z.host's API is documented to
# return on purpose. Matched on this specific substring rather than
# "isn't JSON" broadly, since a non-JSON body could in principle be
# something else (a proxy's plain-text error page, for instance) that
# genuinely does need the raw text surfaced as-is.
_JS_CRASH_SIGNATURE = "is not an object (evaluating"


def _describe_http_error(status: int, body: str) -> str:
    """Builds the message every non-200 response across this module's
    three endpoints (shorten_url/create_paste/upload_file) raises --
    centralized so a body matching e-z.host's own known crash (see
    _JS_CRASH_SIGNATURE above) gets the same honest, non-cryptic wording
    everywhere, instead of dumping a raw JavaScript TypeError at whoever
    hit it as if it were e-z.host's real validation message. The raw body
    is still included (repr'd, since it's not real prose) so it's still
    there to compare against for whoever debugs this next."""
    if _JS_CRASH_SIGNATURE in body:
        return (
            f"e-z.host returned HTTP {status}, but its own error handler crashed instead of saying why "
            f"(raw: {body!r}). This is a known e-z.host-side bug, not something wrong with what was sent -- "
            "try again, try a different title/text, or pick a different provider if it keeps happening."
        )
    return f"e-z.host returned HTTP {status}: {body}"


async def _get_session(session: Optional[aiohttp.ClientSession]):
    """Thin wrapper around api.providers.util.get_session with this
    module's own timeout -- kept as a module-local name since every
    function below already calls it as `_get_session(session)`."""
    return await _get_session_shared(session, _TIMEOUT)


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
                    raise EZHostAPIError(_describe_http_error(resp.status, err), resp.status)
                try:
                    data = await resp.json()
                except aiohttp.ContentTypeError:
                    raise EZHostAPIError("e-z.host returned a non-JSON response.", resp.status)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise EZHostAPIError(f"Couldn't reach e-z.host: {_describe_network_error(e, _TIMEOUT)}")
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

    `title` and `description` are both required by e-z.host itself (see
    this module's docstring's "paste endpoint" note, confirmed
    2026-08-17) -- unlike every other /paste provider in this package,
    where both are optional. Checked locally, before the request is even
    sent: a missing one raises EZHostAPIError immediately with a plain
    explanation, rather than letting e-z.host's own broken error handler
    crash on it (see _JS_CRASH_SIGNATURE above) the way it did before
    this was confirmed.

    Raises EZHostAPIError on a missing `title`/`description`, a non-200
    response, a "success": false body, a non-JSON response, or a 200
    response missing any of the three expected fields.
    """
    if not title or not description:
        raise EZHostAPIError(
            "e-z.host requires both `title` and `description` for a paste -- unlike this bot's other "
            "/paste providers, neither can be left blank here. Provide both, or pick a different provider."
        )

    payload: Dict[str, str] = {"text": text, "language": language, "title": title, "description": description}

    sess, should_close = await _get_session(session)
    try:
        headers = {"Content-Type": "application/json", "key": config.EZ_HOST_API_KEY}
        try:
            async with sess.post(f"{BASE_URL}/paste", headers=headers, json=payload) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise EZHostAPIError(_describe_http_error(resp.status, err), resp.status)
                try:
                    data = await resp.json()
                except aiohttp.ContentTypeError:
                    raise EZHostAPIError("e-z.host returned a non-JSON response.", resp.status)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise EZHostAPIError(f"Couldn't reach e-z.host: {_describe_network_error(e, _TIMEOUT)}")
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
                    raise EZHostAPIError(_describe_http_error(resp.status, err), resp.status)
                try:
                    data = await resp.json()
                except aiohttp.ContentTypeError:
                    raise EZHostAPIError("e-z.host returned a non-JSON response.", resp.status)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise EZHostAPIError(f"Couldn't reach e-z.host: {_describe_network_error(e, _TIMEOUT)}")
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


# Re-exported for backward compatibility -- this used to be defined here
# (back when e-z.host was the only provider) before the multi-provider
# expansion moved the actual implementation to api.providers.util, where
# every other provider module can reach it too. commands/url.py now imports
# it from there directly; this alias just means anything still doing
# `from api.providers.ez_host import extract_short_code` keeps working.
extract_short_code = _extract_short_code
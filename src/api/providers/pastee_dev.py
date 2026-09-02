"""
api.providers.pastee_dev -- thin async wrapper around pastee.dev's API
(https://api.pastee.dev/v1, documented at https://pastee.dev/wiki/API),
for /paste's `pastee_dev` provider choice.

As of the 2025-12 domain registry dispute noted on pastee.dev's own
site, this service's original paste.ee domain is disabled and
pastee.dev is the primary domain (see https://pastee.dev/about) -- this
module (and its filename, provider key, error class, and
PASTEE_DEV_API_KEY config var) was renamed from paste_ee/PASTE_EE to
match, to avoid the two domains being confused with one another
elsewhere in this codebase. Not independently confirmed against a live
account whether api.pastee.dev mirrors the old api.paste.ee's schema
exactly (no changelog was found either way), but the two are the same
service, just reached via a different domain, so this is treated as a
domain swap rather than a schema unknown the way pastey_gg.py's is. If
the original domain is ever restored and pastee.dev stops working,
BASE_URL below is the only thing that should need to change back.

Confirmed shape: POST /v1/pastes with header `X-Auth-Token: <key>` and a
JSON body of {description?, sections: [{name?, syntax?, contents}]}.
Response: {"id": ..., "link": "https://paste.ee/p/<id>"} (or a
pastee.dev link -- this is whatever the API itself returns, so it's
passed through as-is rather than rewritten here). Confirmed raw URL
scheme (pastee.dev's legacy "Simple API" docs, which still apply):
https://pastee.dev/r/<id>.

Confirmed (2026-08) failure shape: {"success": false, "errors": [{"field",
"code", "message"}, ...]} -- a plural `errors` array, matching the old
paste.ee v1 schema, NOT a top-level `error`/`message` string. Parse
accordingly below; a live 400 surfaced as a content-free generic message
here previously because of that exact mismatch.
"""

from typing import Dict, List, Optional, Tuple

import base64
import hashlib
import os
import re
import secrets
import string

import aiohttp
from cryptography.hazmat.primitives import padding as _sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher as _Cipher
from cryptography.hazmat.primitives.ciphers import algorithms as _algorithms
from cryptography.hazmat.primitives.ciphers import modes as _modes

from api import config
from api.providers.errors import ProviderAPIError
from api.providers.util import describe_network_error as _describe_network_error
from api.providers.util import get_session as _get_session_shared
from api.providers.util import require_key

BASE_URL = "https://api.pastee.dev/v1"

_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Confirmed (2026-08, live): pastee.dev's `encrypted` request field (see
# create_paste's own docstring on `encrypted` below) is *purely* a display
# flag -- it does not make api.pastee.dev encrypt `contents` itself.
# Submitting encrypted=True with plaintext `contents` comes back as an
# ordinary success and stores that plaintext verbatim; the paste is not
# encrypted at all. Confirmed by fetching a real encrypted pastee.dev
# paste's raw content directly (bypassing its page JS): the *stored*
# section content is already ciphertext in the exact
# base64("Salted__" + 8-byte-salt + AES-CBC-ciphertext) shape OpenSSL's
# legacy `-md md5` key derivation (equivalently, CryptoJS.AES.encrypt(text,
# passphrase).toString() with no explicit key/IV -- CryptoJS defaults to
# the same MD5-based EvpKDF) produces. So encryption has to happen
# *before* the request goes out, client-side, exactly like pastee.dev's
# own web UI does in the browser -- this module can't rely on the server
# to do it. The passphrase itself is never sent to pastee.dev at all; it's
# appended as a URL fragment (the part after '#') on the returned link,
# which pastee.dev's own page JS reads from window.location.hash on load
# and feeds into CryptoJS.AES.decrypt to render the plaintext. Browsers
# never transmit URL fragments in HTTP requests, so this is the same
# "server only ever holds ciphertext" pattern used by every other
# zero-knowledge pastebin, not something specific to this module.
#
# This was reverse-engineered (pastee.dev's own API docs don't cover it),
# but is corroborated by two independent sources: (1) the original
# paste.ee Node client's own README, which describes its `encrypt` option
# as "encrypts the paste and returns the randomly generated key" -- i.e.
# encryption happening client-side in the library, not server-side; and
# (2) round-tripping this module's own _cryptojs_aes_encrypt output
# through `openssl enc -d -aes-256-cbc -md md5` (OpenSSL's own legacy KDF,
# the one CryptoJS's default EvpKDF replicates) and getting the original
# plaintext back. If pastee.dev's frontend ever changes to a different
# scheme (e.g. a modern AES-GCM/PBKDF2 zero-knowledge redesign), the
# tell will be that fragment-bearing links stop decrypting on the site
# even though this still produces the legacy "Salted__" shape -- nothing
# in the API response itself would flag that, the same blind spot noted
# on `expiration` above.
_ENCRYPTION_KEY_CHARSET = string.ascii_letters + string.digits
_ENCRYPTION_KEY_LENGTH = 32  # matches the length of the fragment key pastee.dev's own web UI generates


def _openssl_evp_bytes_to_key(password: bytes, salt: bytes, key_len: int, iv_len: int) -> Tuple[bytes, bytes]:
    """OpenSSL's legacy (MD5-based, single-iteration) EVP_BytesToKey key
    derivation -- what CryptoJS.AES.encrypt(text, passphrase) uses by
    default when given a plain string passphrase instead of a WordArray
    key/IV. See the module-level comment above `_ENCRYPTION_KEY_CHARSET`
    for how this was confirmed to be what pastee.dev's own frontend
    expects."""
    derived = b""
    block = b""
    while len(derived) < key_len + iv_len:
        block = hashlib.md5(block + password + salt).digest()
        derived += block
    return derived[:key_len], derived[key_len : key_len + iv_len]


def _cryptojs_aes_encrypt(plaintext: str, passphrase: str) -> str:
    """Encrypts `plaintext` the same way a browser running
    CryptoJS.AES.encrypt(plaintext, passphrase).toString() would: a fresh
    random 8-byte salt, a 256-bit key + 128-bit IV derived from
    `passphrase` via _openssl_evp_bytes_to_key above, AES-256-CBC with
    PKCS7 padding over the UTF-8 plaintext, and the result packed as
    base64("Salted__" + salt + ciphertext) -- pastee.dev's own page JS
    (and any other CryptoJS-based reader) expects exactly this shape."""
    salt = os.urandom(8)
    key, iv = _openssl_evp_bytes_to_key(passphrase.encode("utf-8"), salt, 32, 16)
    padder = _sym_padding.PKCS7(_algorithms.AES.block_size).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    encryptor = _Cipher(_algorithms.AES(key), _modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(b"Salted__" + salt + ciphertext).decode("ascii")


def _generate_encryption_key(length: int = _ENCRYPTION_KEY_LENGTH) -> str:
    """A random URL-fragment-safe passphrase for `_cryptojs_aes_encrypt`.
    Only ever appended to the returned link's `#fragment` -- see the
    module-level comment above `_ENCRYPTION_KEY_CHARSET` for why that
    never reaches pastee.dev itself. Uses `secrets` (not `random`) since
    this is the only thing standing between an "encrypted" paste and
    anyone who can guess it."""
    return "".join(secrets.choice(_ENCRYPTION_KEY_CHARSET) for _ in range(length))

# Confirmed accepted `expiration` shapes, per paste.ee's own API docs
# (pastee.dev inherited the same schema -- see this module's docstring).
# Not a fixed enum pastee.dev picks from, just a small set of shapes, so
# this is the practical equivalent of a "valid options" list for it.
# (label, example) pairs -- kept here rather than in commands/url.py so
# it stays next to the field it documents; commands/url.py imports it to
# build the "invalid expiration" embed when pastee.dev rejects a value
# (see PasteeDevAPIError.fields below, which is how that call site knows
# *which* field failed).
EXPIRATION_HELP: List[Tuple[str, str]] = [
    ("never", "Never expires"),
    ("A plain number", "Seconds until expiry, e.g. `3600`"),
    ("A number + `d`", "Days, e.g. `3d`"),
    ("A number + `w`", "Weeks, e.g. `4w`"),
    ("A number + `m`", "Months, e.g. `2m`"),
    ("A number + `y`", "Years, e.g. `1y`"),
]
EXPIRATION_HELP_NOTE = (
    "Only one unit at a time -- no combining units. If `expires` is left blank, "
    "pastee.dev defaults to `1m` (one month)."
)

# Confirmed (2026-08, live): pastee.dev does NOT reliably reject a
# malformed `expiration` value with a validation error the way this
# module previously assumed -- a garbage string like
# "fegregrgregregreghrgr" comes back as an ordinary 200/201 success
# with a real id/link, but the resulting paste isn't actually reachable
# on pastee.dev's own site (its duration parser most likely falls back
# to something degenerate, e.g. treating unparseable input as an
# immediate/zero expiration, rather than rejecting the request). Since
# that failure is invisible in the API response itself -- pastee.dev
# reports success either way -- it can't be caught by inspecting the
# response after the fact. So `expiration` is validated against the
# accepted shapes *before* the request goes out at all; anything that
# doesn't match never reaches pastee.dev, and this module raises with
# fields=["expiration"] itself instead of relying on pastee.dev's own
# (apparently unreliable) validation. Keep this pattern's alternatives in
# sync with EXPIRATION_HELP above if pastee.dev's accepted shapes change.
_EXPIRATION_PATTERN = re.compile(r"^(?:never|\d+|\d+[dwmy])$", re.IGNORECASE)


class PasteeDevAPIError(ProviderAPIError):
    """Raised whenever a pastee.dev API call doesn't succeed -- a non-2xx
    HTTP status, a non-JSON response, or a JSON response with
    `"success": false` (pastee.dev's own convention for a handled
    application-level failure, per its API docs).

    `fields` -- the `field` value(s) from pastee.dev's own
    {"errors": [{"field", "code", "message"}, ...]} shape (see this
    module's docstring), when this came from a parsed validation
    response. Lets a call site react to *which* input was rejected (e.g.
    "expiration" vs "sections.0.syntax") without string-matching the
    message text. Empty for the non-validation failure paths below
    (network errors, non-JSON bodies, a missing id/link on success)."""

    def __init__(self, message: str, *, fields: Optional[List[str]] = None):
        super().__init__(message)
        self.fields = fields or []


async def _get_session(session: Optional[aiohttp.ClientSession]):
    return await _get_session_shared(session, _TIMEOUT)


def _normalize_syntax(language: Optional[str]) -> Optional[str]:
    """Maps this bot's own "plaintext" sentinel to pastee.dev's own
    "text" syntax identifier (see create_paste()'s `language` docstring
    for why pastee.dev itself doesn't recognize "plaintext"); anything
    else -- including "autodetect" -- passes through unchanged, for
    pastee.dev's own /v1/pastes validation to accept or reject. Shared by
    the primary section and every `extra_files` entry below (pastee.dev's
    own multiple-`sections` support, merged in here with /paste's
    file1-file4 extra-file feature) so a multi-file paste normalizes the
    same sentinel consistently across every file, not just the first."""
    return "text" if language == "plaintext" else language


async def create_paste(
    text: str,
    *,
    language: str = "autodetect",
    title: Optional[str] = None,
    description: Optional[str] = None,
    access_key: Optional[str] = None,
    expiration: Optional[str] = None,
    encrypted: bool = False,
    extra_files: Optional[List[Dict[str, Optional[str]]]] = None,
    session: Optional[aiohttp.ClientSession] = None,
) -> Dict[str, Optional[str]]:
    """
    Creates a paste on pastee.dev.

    `access_key` overrides config.PASTEE_DEV_API_KEY for this call
    (registry.py's supports_access_key=True for this provider) -- lets
    someone attribute the paste to their own pastee.dev account instead
    of this bot's default. Raises PasteeDevAPIError naming
    PASTEE_DEV_API_KEY if neither is available, since pastee.dev has no
    anonymous path at all (see this module's docstring).

    `language` maps to pastee.dev's `syntax` field -- expects one of
    pastee.dev's own short syntax identifiers (its GET /v1/syntaxes
    endpoint lists them; "autodetect" and "text" are always valid). An
    unrecognized value surfaces as pastee.dev's own validation error
    rather than this module trying to validate against that list itself.

    One exception: /paste's own bot-wide default sentinel is "plaintext"
    (see commands/url.py's _url_paste_impl), but pastee.dev doesn't
    recognize "plaintext" itself -- only "text" (confirmed 2026-08: a
    live call with syntax="plaintext" fails pastee.dev's own validation,
    "The selected sections.0.syntax is invalid."). That one sentinel is
    normalized to "text" below, the same way rubis.py normalizes
    "plaintext" for its own API. Anything else, including "autodetect",
    is passed through unchanged and still surfaces pastee.dev's own
    validation error if it's wrong.

    Unlike `language` above, this IS validated locally against
    _EXPIRATION_PATTERN before the request goes out, rather than passed
    through for pastee.dev to reject -- confirmed (2026-08, live) that an
    unparseable value doesn't reliably surface as pastee.dev's own
    validation error the way this docstring used to claim; instead
    pastee.dev returns an ordinary success with a real id/link for a
    paste that then isn't actually reachable on its site. That failure
    is invisible in the response, so it has to be prevented up front
    instead of caught afterward. A value that fails this local check
    raises PasteeDevAPIError(fields=["expiration"]) itself, same shape as
    a pastee.dev-side validation error, so callers don't need to
    distinguish the two.

    `encrypted` maps to pastee.dev's own documented `encrypted` request
    field (registry.py's supports_encrypt=True for this provider only --
    no other provider in this package documents an equivalent, so
    /paste's `encrypt` option is rejected up front for anything else; see
    commands/url.py's _url_paste_impl).

    IMPORTANT, and updated from an earlier version of this docstring:
    that server-side `encrypted` flag by itself does NOT encrypt
    `contents` -- confirmed (2026-08, live) that submitting it with
    plaintext content stores that plaintext verbatim; pastee.dev only
    uses the flag to decide whether its own page should try to decrypt
    on load. See the module-level comment above `_ENCRYPTION_KEY_CHARSET`
    for the full writeup of how that was confirmed and what this module
    does about it: when `encrypted=True`, `text` is encrypted locally
    first (`_cryptojs_aes_encrypt`, matching pastee.dev's own
    CryptoJS-based scheme) with a freshly generated key
    (`_generate_encryption_key`), *that* ciphertext is what's sent as
    `contents`, and the key is appended as a URL fragment on the
    returned `paste_url` so pastee.dev's page JS can decrypt it back for
    a viewer -- never sent to pastee.dev itself. `language`/`title` are
    still honored, but `syntax` is forced to "text" regardless of what
    was passed, since syntax-highlighting base64 ciphertext is
    meaningless and pastee.dev's own UI skips it for encrypted pastes too.
    This only ever applies to the primary section built from `text` --
    any `extra_files` sections are still sent as given, unencrypted, since
    there's no per-file encrypt flag for this function to key off of.

    `extra_files`, if given, is a list of {"content" (required), "name",
    "language"} dicts -- pastee.dev's own API already accepts multiple
    `sections` per paste (this function used to only ever build one); this
    is that same multi-`sections` support merged in here with /paste's
    file1-file4 extra-file feature (registry.py's supports_extra_files=True
    for this provider), mirroring the exact {"content", "name", "language"}
    shape pastey_gg.create_paste's own `extra_files` already expects so
    commands/url.py can build one list and hand it to whichever provider
    was picked. Each entry becomes a further `sections` entry alongside
    the primary one built from `text`/`title`/`language` above, with its
    own `language` passed through _normalize_syntax() the same way the
    primary section's is -- so the "plaintext" sentinel normalizes to
    "text" for every file, not just the first. Raises
    PasteeDevAPIError(fields=["sections"]) itself if any entry's `content`
    is empty, matching pastee.dev's own rejection of an empty primary
    section. Not affected by `encrypted` -- see that parameter's own
    docstring for why encryption here only ever covers the primary `text`.

    Returns {"paste_url": ..., "raw_url": ..., "deletion_url": None} --
    see this module's docstring for why deletion_url is always None.
    `raw_url` is unaffected by `encrypted` and always points at
    pastee.dev's raw-content endpoint -- since decryption only happens in
    the browser via the URL-fragment key above, `raw_url` for an
    encrypted paste serves ciphertext only, same as a plain `curl` of the
    `paste_url` itself would.

    Raises PasteeDevAPIError on a non-2xx response, a non-JSON response,
    or a response pastee.dev itself flagged as failed.
    """
    key = require_key(
        access_key or config.PASTEE_DEV_API_KEY, "PASTEE_DEV_API_KEY", "pastee.dev", PasteeDevAPIError
    )

    if expiration and not _EXPIRATION_PATTERN.fullmatch(expiration.strip()):
        # See create_paste's own docstring on `expiration`: caught here,
        # before the request goes out, because pastee.dev doesn't
        # reliably reject this itself.
        raise PasteeDevAPIError(
            f"'{expiration}' isn't an expiration format pastee.dev accepts.", fields=["expiration"]
        )

    encryption_key: Optional[str] = None
    if encrypted:
        # See this function's `encrypted` docstring: the server-side flag
        # alone doesn't encrypt anything, so `text` has to be encrypted
        # here before it ever goes into the request body.
        encryption_key = _generate_encryption_key()
        text = _cryptojs_aes_encrypt(text, encryption_key)
        syntax = "text"
    else:
        syntax = _normalize_syntax(language)
    section: Dict[str, str] = {"contents": text, "syntax": syntax}
    if title:
        section["name"] = title

    # See this function's `extra_files` docstring: pastee.dev's own
    # multiple-`sections` support, merged with /paste's file1-file4
    # feature -- each entry becomes a further section alongside the
    # primary one built above, never touched by `encrypted` above.
    sections: List[Dict[str, str]] = [section]
    if extra_files:
        for extra in extra_files:
            content = extra.get("content")
            if not content:
                raise PasteeDevAPIError(
                    "Every extra file needs non-empty content -- pastee.dev's own sections "
                    "can't be empty either.",
                    fields=["sections"],
                )
            extra_section: Dict[str, str] = {
                "contents": content,
                "syntax": _normalize_syntax(extra.get("language")),
            }
            name = extra.get("name")
            if name:
                extra_section["name"] = name
            sections.append(extra_section)

    body: Dict = {"sections": sections}
    if description:
        body["description"] = description
    if expiration:
        body["expiration"] = expiration
    if encrypted:
        body["encrypted"] = True

    headers = {"X-Auth-Token": key, "Content-Type": "application/json"}

    sess, should_close = await _get_session(session)
    try:
        try:
            async with sess.post(f"{BASE_URL}/pastes", headers=headers, json=body) as resp:
                try:
                    data = await resp.json()
                except aiohttp.ContentTypeError:
                    raise PasteeDevAPIError(f"pastee.dev returned HTTP {resp.status} with a non-JSON body.")
                if resp.status not in (200, 201) or data.get("success") is False:
                    # Confirmed (2026-08): pastee.dev's actual failure shape is
                    # {"success": false, "errors": [{"field", "code", "message"}, ...]}
                    # -- a plural `errors` *array*, not a top-level `error`/`message`
                    # string the way this previously assumed. That mismatch is why
                    # this used to silently fall through to the generic HTTP-status
                    # message below instead of surfacing pastee.dev's real reason.
                    errors = data.get("errors")
                    if isinstance(errors, list) and errors:
                        detail = "; ".join(
                            e.get("message") or e.get("field") or str(e) if isinstance(e, dict) else str(e)
                            for e in errors
                        )
                        err_fields = [e.get("field") for e in errors if isinstance(e, dict) and e.get("field")]
                    else:
                        detail = data.get("error") or data.get("message")
                        err_fields = []
                    raise PasteeDevAPIError(detail or f"pastee.dev returned HTTP {resp.status}.", fields=err_fields)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise PasteeDevAPIError(f"Couldn't reach pastee.dev: {_describe_network_error(e, _TIMEOUT)}")
    finally:
        if should_close:
            await sess.close()

    paste_id = data.get("id")
    paste_url = data.get("link")
    if not paste_id or not paste_url:
        raise PasteeDevAPIError("pastee.dev's response was missing the expected id/link fields.")

    if encryption_key:
        # Never sent to pastee.dev -- see this function's `encrypted`
        # docstring for why the fragment is what makes decryption work.
        paste_url = f"{paste_url}#{encryption_key}"

    return {
        "paste_url": paste_url,
        "raw_url": f"https://pastee.dev/r/{paste_id}",
        "deletion_url": None,
    }
"""Server-held, per-build encryption for universal downloadable artifacts.

Artifacts are encrypted before they are persisted to a private Supabase Storage
bucket. The attached file is a small universal loader containing a random build
ID and a per-build bearer token; it contains neither the protected program nor
the server master key. The endpoint decrypts only after checking the token.

This is a barrier against offline/static recovery, not a guarantee against a
client that can observe the fetched source at runtime. A public executable must
receive the program it executes, so a determined user can capture that response.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import config

ARTIFACT_VERSION = 1
ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_ARTIFACT_SOURCE_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_ENVELOPE_BYTES = 48 * 1024 * 1024
ARTIFACT_HTTP_TIMEOUT_SECONDS = 20
_RATE_LOCK = threading.Lock()
_RATE_EVENTS: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_RATE_WINDOW_SECONDS = 60.0
_MAX_REQUESTS_PER_BUILD_PER_WINDOW = 120
_MAX_REQUESTS_PER_REMOTE_PER_WINDOW = 240


class ProtectedArtifactError(RuntimeError):
    """An error with a safe HTTP status/reason suitable for API responses."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = int(status)
        self.reason = str(reason)


def _master_key() -> bytes:
    value = str(getattr(config, "CELESTIAL_ARTIFACT_MASTER_KEY", "") or "").strip()
    if len(value) < 32:
        raise ProtectedArtifactError(503, "artifact_protection_not_configured")
    # Hash the configured high-entropy secret to a fixed-size HKDF input. Never
    # log, store in Storage, or return this value to clients.
    return hashlib.sha256(value.encode("utf-8")).digest()


def _bucket() -> str:
    value = str(getattr(config, "SUPABASE_PROTECTED_ARTIFACTS_BUCKET", "protected-artifacts") or "").strip()
    if not value or "/" in value:
        raise ProtectedArtifactError(503, "artifact_storage_not_configured")
    return value


def _storage_url(object_path: str) -> str:
    bucket = quote(_bucket(), safe="")
    path = "/".join(quote(part, safe="") for part in object_path.split("/"))
    return f"{config.SUPABASE_URL.rstrip('/')}/storage/v1/object/{bucket}/{path}"


def _storage_headers(*, content_type: str | None = None) -> dict[str, str]:
    secret = str(getattr(config, "SUPABASE_SECRET_KEY", "") or "").strip()
    if not secret or not str(getattr(config, "SUPABASE_URL", "") or "").strip():
        raise ProtectedArtifactError(503, "artifact_storage_not_configured")
    headers = {
        "Authorization": f"Bearer {secret}",
        "apikey": secret,
        "Accept": "application/json, application/octet-stream",
        "User-Agent": "CelestialProtectedArtifacts/1.0",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _upload_envelope(build_id: str, envelope: bytes) -> None:
    if len(envelope) > MAX_ARTIFACT_ENVELOPE_BYTES:
        raise ProtectedArtifactError(413, "artifact_too_large")
    headers = _storage_headers(content_type="application/json")
    headers.update({"Content-Length": str(len(envelope)), "x-upsert": "false"})
    req = Request(_storage_url(f"builds/{build_id}.json"), data=envelope, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=ARTIFACT_HTTP_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 200))
            if not 200 <= status < 300:
                raise ProtectedArtifactError(503, "artifact_storage_unavailable")
    except HTTPError as exc:
        if exc.code == 409:
            raise ProtectedArtifactError(503, "artifact_id_collision") from exc
        if exc.code in (408, 425, 429, 500, 502, 503, 504):
            raise ProtectedArtifactError(503, "artifact_storage_unavailable") from exc
        raise ProtectedArtifactError(503, "artifact_storage_rejected_upload") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ProtectedArtifactError(503, "artifact_storage_unavailable") from exc


def _download_envelope(build_id: str) -> bytes:
    headers = _storage_headers()
    req = Request(_storage_url(f"builds/{build_id}.json"), headers=headers, method="GET")
    try:
        with urlopen(req, timeout=ARTIFACT_HTTP_TIMEOUT_SECONDS) as response:
            data = response.read(MAX_ARTIFACT_ENVELOPE_BYTES + 1)
    except HTTPError as exc:
        if exc.code == 404:
            raise ProtectedArtifactError(404, "artifact_not_found") from exc
        if exc.code in (408, 425, 429, 500, 502, 503, 504):
            raise ProtectedArtifactError(503, "artifact_storage_unavailable") from exc
        raise ProtectedArtifactError(503, "artifact_storage_unavailable") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ProtectedArtifactError(503, "artifact_storage_unavailable") from exc

    if not data or len(data) > MAX_ARTIFACT_ENVELOPE_BYTES:
        raise ProtectedArtifactError(503, "artifact_storage_invalid_response")
    return data


def _derive_build_key(master_key: bytes, build_id: str, salt: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"Celestial/universal-protected-artifact/v1/" + build_id.encode("ascii"),
    ).derive(master_key)


def _encode_b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode_b64(value: Any, *, max_chars: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > max_chars:
        raise ProtectedArtifactError(503, "artifact_storage_invalid_response")
    try:
        return base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ProtectedArtifactError(503, "artifact_storage_invalid_response") from exc


def create_protected_artifact(source: bytes) -> dict[str, str]:
    """Encrypt and persist one obfuscated output. No game/license data is used."""
    if not isinstance(source, (bytes, bytearray)) or not source:
        raise ProtectedArtifactError(400, "empty_artifact")
    payload = bytes(source)
    if len(payload) > MAX_ARTIFACT_SOURCE_BYTES:
        raise ProtectedArtifactError(413, "artifact_too_large")

    master_key = _master_key()
    # Validate bucket and credentials before generating a loader token.
    _bucket()
    _storage_headers()
    build_id = secrets.token_hex(20)
    token = secrets.token_urlsafe(32)
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    build_key = _derive_build_key(master_key, build_id, salt)
    aad = f"celestial-artifact:v{ARTIFACT_VERSION}:{build_id}".encode("ascii")
    ciphertext = AESGCM(build_key).encrypt(nonce, payload, aad)
    envelope = {
        "version": ARTIFACT_VERSION,
        "build_id": build_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
        "salt": _encode_b64(salt),
        "nonce": _encode_b64(nonce),
        "ciphertext": _encode_b64(ciphertext),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
    }
    envelope_bytes = json.dumps(envelope, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    _upload_envelope(build_id, envelope_bytes)
    return {"build_id": build_id, "token": token, "payload_sha256": envelope["payload_sha256"]}


def _consume_rate_limit(build_id: str, remote: str) -> bool:
    now = time.monotonic()
    accepted = True
    keys = (("build", build_id), ("remote", remote or "unknown"))
    limits = (_MAX_REQUESTS_PER_BUILD_PER_WINDOW, _MAX_REQUESTS_PER_REMOTE_PER_WINDOW)
    with _RATE_LOCK:
        for key, limit in zip(keys, limits):
            events = _RATE_EVENTS[key]
            while events and now - events[0] > _RATE_WINDOW_SECONDS:
                events.popleft()
            if len(events) >= limit:
                accepted = False
        # Only count if all scopes allow it.
        if accepted:
            for key in keys:
                _RATE_EVENTS[key].append(now)
        # Bound stale entries so a long-running host does not accumulate keys.
        if len(_RATE_EVENTS) > 10_000:
            stale_before = now - _RATE_WINDOW_SECONDS
            for key in list(_RATE_EVENTS):
                events = _RATE_EVENTS[key]
                while events and events[0] < stale_before:
                    events.popleft()
                if not events:
                    _RATE_EVENTS.pop(key, None)
    return accepted


def fetch_protected_artifact(payload: Any, remote: str) -> bytes:
    """Validate a loader token, decrypt a stored build, and return its code."""
    if not isinstance(payload, dict):
        raise ProtectedArtifactError(400, "malformed_request")
    build_id = str(payload.get("artifact_id") or "").strip().lower()
    token = str(payload.get("token") or "").strip()
    if not ARTIFACT_ID_RE.fullmatch(build_id) or not 40 <= len(token) <= 128:
        raise ProtectedArtifactError(400, "malformed_request")
    if not _consume_rate_limit(build_id, str(remote or "unknown")):
        raise ProtectedArtifactError(429, "rate_limited")

    raw = _download_envelope(build_id)
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtectedArtifactError(503, "artifact_storage_invalid_response") from exc
    if not isinstance(envelope, dict) or envelope.get("version") != ARTIFACT_VERSION or envelope.get("build_id") != build_id:
        raise ProtectedArtifactError(503, "artifact_storage_invalid_response")

    expected_hash = envelope.get("token_sha256")
    supplied_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
    if not isinstance(expected_hash, str) or not hmac.compare_digest(expected_hash, supplied_hash):
        # Keep token failures indistinguishable from missing artifacts.
        raise ProtectedArtifactError(404, "artifact_not_found")

    salt = _decode_b64(envelope.get("salt"), max_chars=128)
    nonce = _decode_b64(envelope.get("nonce"), max_chars=64)
    ciphertext = _decode_b64(envelope.get("ciphertext"), max_chars=MAX_ARTIFACT_ENVELOPE_BYTES * 2)
    if len(salt) != 16 or len(nonce) != 12 or len(ciphertext) < 16 or len(ciphertext) > MAX_ARTIFACT_SOURCE_BYTES + 16:
        raise ProtectedArtifactError(503, "artifact_storage_invalid_response")

    master_key = _master_key()
    key = _derive_build_key(master_key, build_id, salt)
    aad = f"celestial-artifact:v{ARTIFACT_VERSION}:{build_id}".encode("ascii")
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
    except Exception as exc:
        # Do not leak crypto details to the public endpoint.
        raise ProtectedArtifactError(503, "artifact_decryption_failed") from exc
    expected_payload_hash = envelope.get("payload_sha256")
    actual_payload_hash = hashlib.sha256(plaintext).hexdigest()
    if not isinstance(expected_payload_hash, str) or not hmac.compare_digest(expected_payload_hash, actual_payload_hash):
        raise ProtectedArtifactError(503, "artifact_integrity_failed")
    try:
        plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtectedArtifactError(503, "artifact_encoding_invalid") from exc
    return plaintext


def validate_public_endpoint(endpoint: str) -> str:
    """Normalize a public HTTPS base URL before any artifact is stored."""
    value = str(endpoint or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ProtectedArtifactError(503, "public_https_artifact_endpoint_required")
    if parsed.query or parsed.fragment:
        raise ProtectedArtifactError(503, "public_https_artifact_endpoint_required")
    return value


def render_loader(endpoint: str, build_id: str, token: str) -> bytes:
    """Create the small, universal Lua/Luau bootstrap attached by /obfuscate."""
    endpoint = validate_public_endpoint(endpoint)
    parsed = urlsplit(endpoint)
    if not ARTIFACT_ID_RE.fullmatch(str(build_id or "")) or not token:
        raise ProtectedArtifactError(500, "artifact_metadata_invalid")
    url = parsed.geturl().rstrip("/") + "/obfuscator/artifact"
    # Only opaque per-build identifiers are embedded. Neither the master key nor
    # the derived AES key is put in the loader. The token is a bearer credential
    # and should be treated as sensitive despite being recoverable by the user.
    loader = f'''-- Celestial server-protected artifact loader
-- This build requires the Celestial artifact endpoint to be reachable.
local artifactId = "{build_id}"
local artifactToken = "{token}"
local endpoint = "{url}"
local HttpService = game:GetService("HttpService")
local requestBody = HttpService:JSONEncode({{ artifact_id = artifactId, token = artifactToken }})
local requestFunction
if type(syn) == "table" and type(syn.request) == "function" then
    requestFunction = syn.request
elseif type(http_request) == "function" then
    requestFunction = http_request
elseif type(request) == "function" then
    requestFunction = request
elseif type(fluxus) == "table" and type(fluxus.request) == "function" then
    requestFunction = fluxus.request
elseif type(http) == "table" and type(http.request) == "function" then
    requestFunction = http.request
end

local protectedSource
if type(requestFunction) == "function" then
    local ok, response = pcall(requestFunction, {{
        Url = endpoint,
        Method = "POST",
        Headers = {{ ["Content-Type"] = "application/json" }},
        Body = requestBody,
    }})
    if not ok or type(response) ~= "table" then
        error("[Celestial] Unable to reach the protected artifact server.", 0)
    end
    local status = tonumber(response.StatusCode or response.Status or response.status_code or 0) or 0
    if status ~= 200 then
        error("[Celestial] Protected artifact request failed (HTTP " .. tostring(status) .. ").", 0)
    end
    protectedSource = response.Body or response.body
else
    local ok, response = pcall(function()
        return HttpService:PostAsync(endpoint, requestBody, Enum.HttpContentType.ApplicationJson, false)
    end)
    if not ok then
        error("[Celestial] HTTP requests are unavailable; this protected build requires network access.", 0)
    end
    protectedSource = response
end

if type(protectedSource) ~= "string" or #protectedSource == 0 then
    error("[Celestial] The protected artifact server returned an empty response.", 0)
end
if type(loadstring) ~= "function" then
    error("[Celestial] This environment does not support loadstring.", 0)
end
local chunk, compileError = loadstring(protectedSource, "@CelestialProtectedArtifact")
if type(chunk) ~= "function" then
    error("[Celestial] The protected artifact could not be compiled: " .. tostring(compileError), 0)
end
return chunk()
'''
    return loader.encode("utf-8")

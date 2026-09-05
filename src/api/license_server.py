"""Public Roblox license API.

The Roblox client is intentionally treated as public/untrusted code: it contains
no shared secret and performs no direct GitHub access. HTTPS carries a short-lived
challenge and the client submits {key, hwid, game_id}. The server owns all
license decisions and, on first activation, writes the HWID to Users.json.

Because a public client cannot safely authenticate a first claim with a secret,
the security boundary is the license key itself plus first-claim-wins HWID
binding. After binding, the same key is locked to that HWID. Protected game
payloads are fetched from the authenticated GitHub Contents API and returned
only after the server authorizes the request.
"""

import asyncio
import base64
import json
import re
import threading
import time
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from . import config
from .github import GitHubAPIError, fetch_users_with_sha, commit_users, fetch_storage_file
from .users import find_user_by_key
from .keys import is_valid_hwid
from .time_utils import parse_expiration_note

MAX_CLOCK_SKEW = 30
CHALLENGE_TTL = 45
MIN_REQUEST_GAP = 2
MAX_BODY_BYTES = 16_384
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

_state_lock = threading.Lock()
_bind_lock = threading.Lock()
_recent: Dict[Tuple[str, str], float] = {}
_challenges: Dict[str, float] = {}
_challenge_recent: Dict[str, float] = {}


def _purge(now: float) -> None:
    for key, created in list(_challenges.items()):
        if now - created > CHALLENGE_TTL:
            _challenges.pop(key, None)
    for key, last in list(_recent.items()):
        if now - last > 120:
            _recent.pop(key, None)
    for remote, last in list(_challenge_recent.items()):
        if now - last > 120:
            _challenge_recent.pop(remote, None)


def issue_challenge(remote: str) -> Dict[str, Any]:
    now = time.time()
    with _state_lock:
        _purge(now)
        last = _challenge_recent.get(remote, 0.0)
        if now - last < 0.5:
            raise PermissionError("rate_limited")
        _challenge_recent[remote] = now
        nonce = secrets.token_urlsafe(32)
        _challenges[nonce] = now
    return {"nonce": nonce, "expires_in": CHALLENGE_TTL}


def _consume_challenge(nonce: str) -> bool:
    now = time.time()
    with _state_lock:
        _purge(now)
        created = _challenges.pop(nonce, None)
    return created is not None and now - created <= CHALLENGE_TTL


def _rate_limited(key: str, remote: str) -> bool:
    now = time.time()
    bucket = (key, remote)
    with _state_lock:
        last = _recent.get(bucket, 0.0)
        if now - last < MIN_REQUEST_GAP:
            return True
        _recent[bucket] = now
    return False


def _game_allowed(entry: Dict[str, Any], game_id: str) -> bool:
    games = entry.get("Games")
    if not games or "*" in games:
        return True
    normalized = {str(x) for x in games}
    return game_id in normalized


def _run(coro):
    return asyncio.run(coro)


def evaluate_and_load(key: str, hwid: str, game_id: str) -> Dict[str, Any]:
    if not is_valid_hwid(hwid):
        return {"allowed": False, "reason": "invalid_hwid_format"}
    if game_id not in config.LICENSE_GAME_SCRIPTS:
        return {"allowed": False, "reason": "game_not_configured"}

    # Serialize binding writes. Every binding fetches the current SHA from
    # GitHub, so two simultaneous first-run claims cannot both commit stale data.
    with _bind_lock:
        try:
            users, sha = _run(fetch_users_with_sha())
        except GitHubAPIError:
            return {"allowed": False, "reason": "backend_unavailable"}

        entry = find_user_by_key(users, key)
        if entry is None:
            return {"allowed": False, "reason": "not_whitelisted"}

        if not _game_allowed(entry, game_id):
            return {"allowed": False, "reason": "game_not_authorized"}

        expires_at = parse_expiration_note(entry.get("Notes"))
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            return {"allowed": False, "reason": "expired"}

        entry_hwid = str(entry.get("HWID") or "").strip()
        if not entry_hwid:
            entry["HWID"] = hwid
            # Persist through the bot's existing GitHub Contents API. This also
            # refreshes its in-memory cache after the write succeeds.
            try:
                _run(commit_users(users, sha, f"License activated: {key[:8]}..."))
            except GitHubAPIError:
                return {"allowed": False, "reason": "backend_unavailable"}
        elif entry_hwid.lower() != hwid.lower():
            return {"allowed": False, "reason": "hwid_mismatch"}

        path = config.LICENSE_GAME_SCRIPTS[game_id]
        if not isinstance(path, str) or not path:
            return {"allowed": False, "reason": "game_script_not_configured"}

        try:
            script = _run(fetch_storage_file(path))
        except GitHubAPIError:
            return {"allowed": False, "reason": "game_script_unavailable"}

        return {
            "allowed": True,
            "reason": "hwid_bound" if not entry_hwid else "ok",
            # Base64 is only transport encoding/obfuscation, not encryption.
            "payload": base64.b64encode(script.encode("utf-8")).decode("ascii"),
        }


def handle_challenge_request(remote: str = "unknown") -> Tuple[int, bytes, Dict[str, str]]:
    try:
        result = issue_challenge(remote)
    except PermissionError:
        return 429, b'{"allowed":false,"reason":"rate_limited"}', {"Content-Type": "application/json", "Cache-Control": "no-store"}
    body = json.dumps(result, separators=(",", ":")).encode()
    return 200, body, {"Content-Type": "application/json", "Cache-Control": "no-store"}


def handle_check_request(raw: bytes, remote: str) -> Tuple[int, bytes, Dict[str, str]]:
    if len(raw) > MAX_BODY_BYTES:
        return 413, b'{"allowed":false,"reason":"request_too_large"}', {"Content-Type": "application/json", "Cache-Control": "no-store"}

    try:
        payload = json.loads(raw)
        key = str(payload["key"]).strip()
        hwid = str(payload["hwid"]).strip()
        game_id = str(int(payload["game_id"]))
        timestamp = int(payload["timestamp"])
        nonce = str(payload["nonce"]).strip()
    except (KeyError, ValueError, TypeError, json.JSONDecodeError, OverflowError):
        return 400, b'{"allowed":false,"reason":"malformed_request"}', {"Content-Type": "application/json", "Cache-Control": "no-store"}

    if not key or len(key) > 256:
        return 400, b'{"allowed":false,"reason":"invalid_key"}', {"Content-Type": "application/json", "Cache-Control": "no-store"}
    if not NONCE_RE.fullmatch(nonce) or not _consume_challenge(nonce):
        return 400, b'{"allowed":false,"reason":"invalid_nonce"}', {"Content-Type": "application/json", "Cache-Control": "no-store"}
    if abs(time.time() - timestamp) > MAX_CLOCK_SKEW:
        return 400, b'{"allowed":false,"reason":"stale_timestamp"}', {"Content-Type": "application/json", "Cache-Control": "no-store"}
    if _rate_limited(key, remote):
        return 429, b'{"allowed":false,"reason":"rate_limited"}', {"Content-Type": "application/json", "Cache-Control": "no-store"}

    result = evaluate_and_load(key, hwid, game_id)
    status = 200 if result.get("allowed") else 403
    body = json.dumps(result, separators=(",", ":")).encode()
    return status, body, {"Content-Type": "application/json", "Cache-Control": "no-store", "Pragma": "no-cache"}

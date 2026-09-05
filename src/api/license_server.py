"""Public Roblox license API.

The Roblox client is public/untrusted code: it contains no server secret and
never writes GitHub directly. The server owns whitelist decisions, HWID
binding, activation timestamps, execution counts, game authorization, and
protected payload retrieval.

Protocol:
    POST /whitelist/challenge -> one-use nonce
    POST /whitelist/check     -> authorize key/HWID/game and return payload +
                                  one-use execution token
    POST /whitelist/complete   -> client reports that the protected payload
                                  executed successfully; server records
                                  Activated (first success) and increments
                                  Executions.
"""

import asyncio
import base64
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from . import config
from .github import GitHubAPIError, fetch_users_with_sha, commit_users
from .supabase_storage import SupabaseStorageError, fetch_game_script
from .users import find_user_by_key
from .keys import is_valid_hwid
from .time_utils import parse_expiration_note, format_join_date

MAX_CLOCK_SKEW = 30
CHALLENGE_TTL = 45
EXECUTION_TOKEN_TTL = 90
MIN_REQUEST_GAP = 2
MAX_BODY_BYTES = 16_384
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
TOKEN_RE = NONCE_RE

_state_lock = threading.Lock()
_bind_lock = threading.Lock()
_execution_lock = threading.Lock()
_recent: Dict[Tuple[str, str], float] = {}
_challenges: Dict[str, float] = {}
_challenge_recent: Dict[str, float] = {}
_execution_tokens: Dict[str, Dict[str, Any]] = {}


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
    for token, record in list(_execution_tokens.items()):
        if now - record["issued_at"] > EXECUTION_TOKEN_TTL:
            _execution_tokens.pop(token, None)


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


def _new_execution_token(key: str, hwid: str, game_id: str) -> str:
    token = secrets.token_urlsafe(32)
    with _execution_lock:
        _purge(time.time())
        _execution_tokens[token] = {
            "key": key,
            "hwid": hwid.lower(),
            "game_id": game_id,
            "issued_at": time.time(),
        }
    return token


def evaluate_and_load(key: str, hwid: str, game_id: str) -> Dict[str, Any]:
    if not is_valid_hwid(hwid):
        return {"allowed": False, "reason": "invalid_hwid_format"}
    if game_id not in config.LICENSE_GAME_SCRIPTS:
        return {"allowed": False, "reason": "game_not_configured"}

    # Serialize all binding writes so two simultaneous first activations cannot
    # commit stale Users.json revisions over one another.
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
        first_binding = not entry_hwid
        if first_binding:
            entry["HWID"] = hwid
            try:
                _run(commit_users(users, sha, f"Bind HWID: {key[:8]}..."))
            except GitHubAPIError:
                return {"allowed": False, "reason": "backend_unavailable"}
        elif entry_hwid.lower() != hwid.lower():
            return {"allowed": False, "reason": "hwid_mismatch"}

        path = config.LICENSE_GAME_SCRIPTS[game_id]
        if not isinstance(path, str) or not path:
            return {"allowed": False, "reason": "game_script_not_configured"}

        try:
            script = _run(fetch_game_script(path))
        except SupabaseStorageError:
            return {"allowed": False, "reason": "game_script_unavailable"}

        execution_token = _new_execution_token(key, hwid, game_id)
        return {
            "allowed": True,
            "reason": "hwid_bound" if first_binding else "ok",
            "payload": base64.b64encode(script.encode("utf-8")).decode("ascii"),
            "execution_token": execution_token,
        }


def complete_execution(key: str, hwid: str, game_id: str, token: str) -> Dict[str, Any]:
    now = time.time()
    with _execution_lock:
        _purge(now)
        record = _execution_tokens.get(token)
        if record is None:
            return {"completed": False, "reason": "invalid_execution_token"}
        if now - record["issued_at"] > EXECUTION_TOKEN_TTL:
            _execution_tokens.pop(token, None)
            return {"completed": False, "reason": "execution_token_expired"}
        if record["key"] != key or record["hwid"] != hwid.lower() or record["game_id"] != game_id:
            return {"completed": False, "reason": "execution_token_mismatch"}

        try:
            users, sha = _run(fetch_users_with_sha())
        except GitHubAPIError:
            return {"completed": False, "reason": "backend_unavailable"}

        entry = find_user_by_key(users, key)
        if entry is None:
            return {"completed": False, "reason": "not_whitelisted"}

        current_hwid = str(entry.get("HWID") or "").strip()
        if current_hwid.lower() != hwid.lower():
            return {"completed": False, "reason": "hwid_mismatch"}
        if not _game_allowed(entry, game_id):
            return {"completed": False, "reason": "game_not_authorized"}

        if entry.get("Activated") is None:
            entry["Activated"] = format_join_date()
        entry["Executions"] = int(entry.get("Executions") or 0) + 1

        try:
            _run(commit_users(users, sha, f"License execution: {key[:8]}..."))
        except GitHubAPIError:
            return {"completed": False, "reason": "backend_unavailable"}

        _execution_tokens.pop(token, None)
        return {
            "completed": True,
            "executions": entry["Executions"],
            "activated": entry["Activated"],
        }


def _headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    }


def handle_challenge_request(remote: str = "unknown") -> Tuple[int, bytes, Dict[str, str]]:
    try:
        result = issue_challenge(remote)
    except PermissionError:
        return 429, b'{"allowed":false,"reason":"rate_limited"}', _headers()
    return 200, json.dumps(result, separators=(",", ":")).encode(), _headers()


def _parse_request(raw: bytes) -> Tuple[Dict[str, Any] | None, Tuple[int, bytes, Dict[str, str]] | None]:
    if len(raw) > MAX_BODY_BYTES:
        return None, (413, b'{"allowed":false,"reason":"request_too_large"}', _headers())
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, (400, b'{"allowed":false,"reason":"malformed_request"}', _headers())
    if not isinstance(payload, dict):
        return None, (400, b'{"allowed":false,"reason":"malformed_request"}', _headers())
    return payload, None


def handle_check_request(raw: bytes, remote: str) -> Tuple[int, bytes, Dict[str, str]]:
    payload, error = _parse_request(raw)
    if error:
        return error
    try:
        key = str(payload["key"]).strip()
        hwid = str(payload["hwid"]).strip()
        game_id = str(int(payload["game_id"]))
        timestamp = int(payload["timestamp"])
        nonce = str(payload["nonce"]).strip()
    except (KeyError, ValueError, TypeError, OverflowError):
        return 400, b'{"allowed":false,"reason":"malformed_request"}', _headers()

    if not key or len(key) > 256:
        return 400, b'{"allowed":false,"reason":"invalid_key"}', _headers()
    if not NONCE_RE.fullmatch(nonce) or not _consume_challenge(nonce):
        return 400, b'{"allowed":false,"reason":"invalid_nonce"}', _headers()
    if abs(time.time() - timestamp) > MAX_CLOCK_SKEW:
        return 400, b'{"allowed":false,"reason":"stale_timestamp"}', _headers()
    if _rate_limited(key, remote):
        return 429, b'{"allowed":false,"reason":"rate_limited"}', _headers()

    result = evaluate_and_load(key, hwid, game_id)
    status = 200 if result.get("allowed") else 403
    return status, json.dumps(result, separators=(",", ":")).encode(), _headers()


def handle_complete_request(raw: bytes, remote: str) -> Tuple[int, bytes, Dict[str, str]]:
    payload, error = _parse_request(raw)
    if error:
        return error
    try:
        key = str(payload["key"]).strip()
        hwid = str(payload["hwid"]).strip()
        game_id = str(int(payload["game_id"]))
        timestamp = int(payload["timestamp"])
        token = str(payload["execution_token"]).strip()
    except (KeyError, ValueError, TypeError, OverflowError):
        return 400, b'{"completed":false,"reason":"malformed_request"}', _headers()

    if not key or len(key) > 256:
        return 400, b'{"completed":false,"reason":"invalid_key"}', _headers()
    if not TOKEN_RE.fullmatch(token):
        return 400, b'{"completed":false,"reason":"invalid_execution_token"}', _headers()
    if abs(time.time() - timestamp) > MAX_CLOCK_SKEW:
        return 400, b'{"completed":false,"reason":"stale_timestamp"}', _headers()
    if _rate_limited(f"complete:{key}", remote):
        return 429, b'{"completed":false,"reason":"rate_limited"}', _headers()

    result = complete_execution(key, hwid, game_id, token)
    status = 200 if result.get("completed") else 403
    return status, json.dumps(result, separators=(",", ":")).encode(), _headers()

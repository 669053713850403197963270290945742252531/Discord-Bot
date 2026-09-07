"""Public Roblox license API backed by Supabase.

The public client contains no backend credentials. The server owns all license
checks, game authorization, and protected script retrieval.
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
from .supabase_db import (
    get_license_by_key,
    game_allowed,
    get_game,
    complete_successful_execution,
    bind_license_hwid,
)
from .supabase_storage import fetch_game_script, SupabaseStorageError
from .keys import is_valid_hwid

MAX_CLOCK_SKEW = 30
CHALLENGE_TTL = 45
MIN_REQUEST_GAP = 2
MAX_BODY_BYTES = 16_384
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

_state_lock = threading.Lock()
_recent: Dict[Tuple[str, str], float] = {}
_challenges: Dict[str, float] = {}
_challenge_recent: Dict[str, float] = {}
_execution_tokens: Dict[str, Dict[str, Any]] = {}


def _purge(now: float) -> None:
    for nonce, created in list(_challenges.items()):
        if now - created > CHALLENGE_TTL:
            _challenges.pop(nonce, None)
    for key, last in list(_recent.items()):
        if now - last > 120:
            _recent.pop(key, None)
    for remote, last in list(_challenge_recent.items()):
        if now - last > 120:
            _challenge_recent.pop(remote, None)
    for token, record in list(_execution_tokens.items()):
        if now - record["issued_at"] > 90:
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


def _run(coro):
    return asyncio.run(coro)


def _valid_key(key: Any) -> bool:
    return isinstance(key, str) and 8 <= len(key.strip()) <= 256


def _issue_execution_token(identifier: str, hwid: str, game_id: str) -> str:
    token = secrets.token_urlsafe(32)
    with _state_lock:
        _purge(time.time())
        _execution_tokens[token] = {
            "identifier": identifier,
            "hwid": hwid.lower(),
            "game_id": game_id,
            "issued_at": time.time(),
        }
    return token


def _consume_execution_token(token: str, identifier: str, hwid: str, game_id: str) -> bool:
    with _state_lock:
        _purge(time.time())
        record = _execution_tokens.pop(token, None)
    if not record:
        return False
    return record["identifier"] == identifier and record["game_id"] == game_id


def evaluate_and_load(key: str, hwid: str, game_id: str, remote: str) -> Dict[str, Any]:
    key = str(key or "").strip()
    hwid = str(hwid or "").strip().lower()
    game_id = str(game_id or "").strip()

    if not _valid_key(key):
        return {"allowed": False, "reason": "invalid_key"}
    if not is_valid_hwid(hwid):
        return {"allowed": False, "reason": "invalid_hwid_format"}
    if not game_id.isdigit():
        return {"allowed": False, "reason": "invalid_game"}
    if _rate_limited(key, remote):
        return {"allowed": False, "reason": "rate_limited"}

    try:
        entry = _run(get_license_by_key(key))
    except Exception as exc:
        print(f"[License] Supabase license lookup failed: {exc}")
        return {"allowed": False, "reason": "backend_unavailable"}

    if entry is None:
        return {"allowed": False, "reason": "not_whitelisted"}
    if not entry.get("Enabled", True):
        return {"allowed": False, "reason": "license_disabled"}

    explicit_expiry = entry.get("ExpiresAt")
    if explicit_expiry:
        try:
            dt = datetime.fromisoformat(str(explicit_expiry).replace("Z", "+00:00"))
            if dt <= datetime.now(timezone.utc):
                return {"allowed": False, "reason": "expired"}
        except ValueError:
            pass

    entry_hwid = str(entry.get("HWID") or "").strip().lower()
    if entry_hwid and entry_hwid != hwid:
        return {"allowed": False, "reason": "hwid_mismatch"}
    if not entry_hwid:
        try:
            if not _run(bind_license_hwid(str(entry["Identifier"]), hwid)):
                return {"allowed": False, "reason": "backend_unavailable"}
        except Exception as exc:
            print(f"[License] HWID bind failed: {exc}")
            return {"allowed": False, "reason": "backend_unavailable"}

    try:
        game = _run(get_game(game_id))
    except Exception as exc:
        print(f"[License] Supported game lookup failed: {exc}")
        return {"allowed": False, "reason": "backend_unavailable"}

    if not game:
        return {"allowed": False, "reason": "game_not_supported"}

    if not game.get("enabled", True):
        return {"allowed": False, "reason": "game_disabled"}

    try:
        if not _run(game_allowed(str(entry["Identifier"]), game_id)):
            return {"allowed": False, "reason": "game_not_authorized"}
    except Exception as exc:
        print(f"[License] Game authorization lookup failed: {exc}")
        return {"allowed": False, "reason": "backend_unavailable"}

    try:
        payload = _run(fetch_game_script(str(game["script_path"])))
    except SupabaseStorageError as exc:
        print(f"[License] Game script fetch failed for {game_id}: {exc}")
        return {"allowed": False, "reason": "game_script_unavailable"}
    except Exception as exc:
        print(f"[License] Unexpected game script fetch failure for {game_id}: {exc}")
        return {"allowed": False, "reason": "backend_unavailable"}

    return {
        "allowed": True,
        "reason": "ok",
        "identifier": entry["Identifier"],
        "game_id": game_id,
        "payload": base64.b64encode(payload.encode("utf-8") if isinstance(payload, str) else payload).decode("ascii"),
        "execution_token": _issue_execution_token(str(entry["Identifier"]), hwid, game_id),
    }


def handle_challenge_request(remote: str) -> Tuple[int, str, Dict[str, str]]:
    try:
        body = issue_challenge(remote)
    except PermissionError as exc:
        return 429, json.dumps({"allowed": False, "reason": str(exc)}), {"Content-Type": "application/json"}
    return 200, json.dumps(body), {"Content-Type": "application/json", "Cache-Control": "no-store"}


def handle_check_request(raw: bytes, remote: str) -> Tuple[int, str, Dict[str, str]]:
    if len(raw) > MAX_BODY_BYTES:
        return 413, json.dumps({"allowed": False, "reason": "request_too_large"}), {"Content-Type": "application/json"}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, json.dumps({"allowed": False, "reason": "malformed_request"}), {"Content-Type": "application/json"}

    if not isinstance(payload, dict):
        return 400, json.dumps({"allowed": False, "reason": "malformed_request"}), {"Content-Type": "application/json"}

    key = str(payload.get("key") or "").strip()
    hwid = str(payload.get("hwid") or "").strip()
    game_id = str(payload.get("game_id") or "").strip()
    nonce = str(payload.get("nonce") or "").strip()
    timestamp = payload.get("timestamp")

    if not _valid_key(key) or not hwid or not game_id or not NONCE_RE.fullmatch(nonce):
        return 400, json.dumps({"allowed": False, "reason": "malformed_request"}), {"Content-Type": "application/json"}
    try:
        timestamp = int(timestamp)
    except (TypeError, ValueError):
        return 400, json.dumps({"allowed": False, "reason": "malformed_request"}), {"Content-Type": "application/json"}
    if abs(time.time() - timestamp) > MAX_CLOCK_SKEW:
        return 400, json.dumps({"allowed": False, "reason": "stale_timestamp"}), {"Content-Type": "application/json"}
    if not _consume_challenge(nonce):
        return 400, json.dumps({"allowed": False, "reason": "bad_challenge"}), {"Content-Type": "application/json"}

    result = evaluate_and_load(key, hwid, game_id, remote)
    status = 200 if result.get("allowed") else 403
    return status, json.dumps(result), {"Content-Type": "application/json", "Cache-Control": "no-store"}


def handle_complete_request(raw: bytes, remote: str) -> Tuple[int, str, Dict[str, str]]:
    if len(raw) > MAX_BODY_BYTES:
        return 413, json.dumps({"ok": False, "reason": "request_too_large"}), {"Content-Type": "application/json"}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, json.dumps({"ok": False, "reason": "malformed_request"}), {"Content-Type": "application/json"}
    if not isinstance(payload, dict):
        return 400, json.dumps({"ok": False, "reason": "malformed_request"}), {"Content-Type": "application/json"}

    key = str(payload.get("key") or "").strip()
    hwid = str(payload.get("hwid") or "").strip().lower()
    game_id = str(payload.get("game_id") or "").strip()
    execution_token = str(payload.get("execution_token") or "").strip()
    if not _valid_key(key) or not is_valid_hwid(hwid) or not game_id.isdigit() or not NONCE_RE.fullmatch(execution_token):
        return 400, json.dumps({"ok": False, "reason": "malformed_request"}), {"Content-Type": "application/json"}

    try:
        entry = _run(get_license_by_key(key))
        if not entry or not entry.get("Enabled", True):
            return 403, json.dumps({"ok": False, "reason": "not_authorized"}), {"Content-Type": "application/json"}
        current_hwid = str(entry.get("HWID") or "").strip().lower()
        if not current_hwid or current_hwid != hwid:
            return 403, json.dumps({"ok": False, "reason": "hwid_mismatch"}), {"Content-Type": "application/json"}
        if not _run(game_allowed(str(entry["Identifier"]), game_id)):
            return 403, json.dumps({"ok": False, "reason": "game_not_authorized"}), {"Content-Type": "application/json"}
        if not _consume_execution_token(execution_token, str(entry["Identifier"]), hwid, game_id):
            return 403, json.dumps({"ok": False, "reason": "invalid_execution_token"}), {"Content-Type": "application/json"}
        if not _run(complete_successful_execution(str(entry["Identifier"]))):
            return 503, json.dumps({"ok": False, "reason": "backend_unavailable"}), {"Content-Type": "application/json"}
    except Exception as exc:
        print(f"[License] Execution completion failed: {exc}")
        return 503, json.dumps({"ok": False, "reason": "backend_unavailable"}), {"Content-Type": "application/json"}

    return 200, json.dumps({"ok": True, "completed": True, "reason": "ok"}), {"Content-Type": "application/json", "Cache-Control": "no-store"}


# Backwards-compatible names for keep_alive callers.
handle_challenge = handle_challenge_request
handle_check_payload = handle_check_request
handle_complete_payload = handle_complete_request

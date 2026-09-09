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
import urllib.error
import urllib.request
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
    return (
        record["identifier"] == identifier
        and record["hwid"] == hwid.lower()
        and record["game_id"] == game_id
    )


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

    user_data = {
        "Identifier": entry.get("Identifier"),
        "DiscordId": entry.get("DiscordId"),
        "Key": entry.get("Key"),
        "Activated": entry.get("Activated"),
        "Executions": int(entry.get("Executions") or 0),
        "Rank": entry.get("Rank") or "User",
        "Notes": entry.get("Notes"),
        "Games": [str(x) for x in (entry.get("Games") or [])],
        "ScriptName": str(game.get("name") or game.get("script_path") or game_id),
        "GameId": game_id,
        "Enabled": bool(entry.get("Enabled", True)),
        "ExpiresAt": entry.get("ExpiresAt"),
        "CreatedAt": entry.get("CreatedAt"),
        "UpdatedAt": entry.get("UpdatedAt"),
        "HWID": entry.get("HWID"),
        "LastHwidReset": entry.get("LastHwidReset"),
        "totalHwidResets": int(entry.get("totalHwidResets") or 0),
    }

    return {
        "allowed": True,
        "reason": "ok",
        "identifier": entry["Identifier"],
        "game_id": game_id,
        "user": user_data,
        "payload": base64.b64encode(payload.encode("utf-8") if isinstance(payload, str) else payload).decode("ascii"),
        "execution_token": _issue_execution_token(str(entry["Identifier"]), hwid, game_id),
    }


def _safe_log_value(value: Any, fallback: str = "Unknown", max_length: int = 1024) -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    return text[:max_length]


def _send_execution_webhook(
    entry: Dict[str, Any],
    game: Dict[str, Any],
    executions: int,
    executor: Any,
    job_id: Any,
    device: Any,
) -> None:
    webhook_url = getattr(config, "LICENSE_EXECUTION_WEBHOOK_URL", "").strip()
    enabled = bool(getattr(config, "LICENSE_EXECUTION_LOGGING_ENABLED", False))
    if not enabled or not webhook_url:
        return

    identifier = _safe_log_value(entry.get("Identifier"))
    discord_id = _safe_log_value(entry.get("DiscordId"), fallback="Unknown")
    key = _safe_log_value(entry.get("Key"), fallback="Unknown")
    database_hwid = _safe_log_value(entry.get("HWID"), fallback="Unset")
    game_id = _safe_log_value(game.get("id") or game.get("game_id"), fallback="Unknown")
    script_name = _safe_log_value(game.get("name") or game.get("script_path") or game_id)

    executor_text = _safe_log_value(executor)
    job_id_text = _safe_log_value(job_id)
    device_text = _safe_log_value(device)

    description = (
        f"{identifier} has successfully executed the script `{executions}` "
        f"time{'' if executions == 1 else 's'}"
    )

    embed = {
        "title": "Script Execution",
        "description": description,
        "color": 0x00FF00,
        "fields": [
            {"name": "HWID:", "value": f"||``{database_hwid}``||", "inline": True},
            {"name": "Executor:", "value": executor_text, "inline": True},
            {"name": "Discord ID:", "value": f"<@{discord_id}>" if discord_id != "Unknown" else "Unknown", "inline": True},
            {"name": "Key:", "value": f"||`{key}`||", "inline": True},
            {"name": "Job ID:", "value": f"``{job_id_text}``", "inline": True},
            {"name": "Device:", "value": device_text, "inline": True},
            {"name": "Script:", "value": f"**{game_id} ({script_name})**", "inline": False},
        ],
        "footer": {"text": "Celestial License"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    payload = json.dumps({"embeds": [embed]}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Celestial-License-Server/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = getattr(response, "status", response.getcode())
            if status < 200 or status >= 300:
                print(f"[License] Execution webhook returned HTTP {status}.")
    except urllib.error.HTTPError as exc:
        print(f"[License] Execution webhook failed with HTTP {exc.code}.")
    except urllib.error.URLError as exc:
        print(f"[License] Execution webhook connection failed: {exc.reason}")
    except Exception as exc:
        print(f"[License] Execution webhook failed: {exc}")


def _queue_execution_webhook(
    entry: Dict[str, Any],
    game: Dict[str, Any],
    executions: int,
    executor: Any,
    job_id: Any,
    device: Any,
) -> None:
    if not getattr(config, "LICENSE_EXECUTION_LOGGING_ENABLED", False):
        return
    if not getattr(config, "LICENSE_EXECUTION_WEBHOOK_URL", "").strip():
        return

    threading.Thread(
        target=_send_execution_webhook,
        args=(entry, game, executions, executor, job_id, device),
        daemon=True,
        name="celestial-execution-webhook",
    ).start()


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
    executor = _safe_log_value(payload.get("executor"), fallback="Unknown", max_length=256)
    job_id = _safe_log_value(payload.get("job_id"), fallback="Unknown", max_length=128)
    device = _safe_log_value(payload.get("device"), fallback="Unknown", max_length=64)
    if not _valid_key(key) or not is_valid_hwid(hwid) or not game_id.isdigit() or not NONCE_RE.fullmatch(execution_token):
        return 400, json.dumps({"ok": False, "reason": "malformed_request"}), {"Content-Type": "application/json"}

    try:
        entry = _run(get_license_by_key(key))
        if not entry or not entry.get("Enabled", True):
            return 403, json.dumps({"ok": False, "reason": "not_authorized"}), {"Content-Type": "application/json"}
        current_hwid = str(entry.get("HWID") or "").strip().lower()
        if not current_hwid or current_hwid != hwid:
            return 403, json.dumps({"ok": False, "reason": "hwid_mismatch"}), {"Content-Type": "application/json"}
        game = _run(get_game(game_id))
        if not game:
            return 403, json.dumps({"ok": False, "reason": "game_not_supported"}), {"Content-Type": "application/json"}
        if not game.get("enabled", True):
            return 403, json.dumps({"ok": False, "reason": "game_disabled"}), {"Content-Type": "application/json"}
        if not _run(game_allowed(str(entry["Identifier"]), game_id)):
            return 403, json.dumps({"ok": False, "reason": "game_not_authorized"}), {"Content-Type": "application/json"}
        if not _consume_execution_token(execution_token, str(entry["Identifier"]), hwid, game_id):
            return 403, json.dumps({"ok": False, "reason": "invalid_execution_token"}), {"Content-Type": "application/json"}
        completed_entry = _run(complete_successful_execution(str(entry["Identifier"])))
        if not completed_entry:
            return 503, json.dumps({"ok": False, "reason": "backend_unavailable"}), {"Content-Type": "application/json"}
        completed_executions = int(completed_entry.get("executions") or 0)
        completed_activated = completed_entry.get("activated")
        _queue_execution_webhook(
            entry,
            game,
            completed_executions,
            executor,
            job_id,
            device,
        )
    except Exception as exc:
        print(f"[License] Execution completion failed: {exc}")
        return 503, json.dumps({"ok": False, "reason": "backend_unavailable"}), {"Content-Type": "application/json"}

    return 200, json.dumps({
        "ok": True,
        "completed": True,
        "reason": "ok",
        "executions": completed_executions,
        "activated": completed_activated,
    }), {"Content-Type": "application/json", "Cache-Control": "no-store"}


# Backwards-compatible names for keep_alive callers.
handle_challenge = handle_challenge_request
handle_check_payload = handle_check_request
handle_complete_payload = handle_complete_request

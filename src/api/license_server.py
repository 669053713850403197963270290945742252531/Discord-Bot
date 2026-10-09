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
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

from . import config
from .supabase_db import (
    get_license_by_key,
    get_license_by_identifier,
    game_allowed,
    get_game,
    complete_successful_execution,
    bind_license_hwid,
    disable_license,
    record_license_session,
    get_license_session,
    heartbeat_license_session,
    get_recent_license_sessions,
    get_active_license_sessions,
    get_license_identities,
    get_recent_license_security_events,
    record_license_security_event,
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
_breach_recent: Dict[Tuple[str, str], float] = {}
_breach_notification_recent: Dict[Tuple[str, str], float] = {}
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
    for key, last in list(_breach_recent.items()):
        if now - last > 60:
            _breach_recent.pop(key, None)
    for key, last in list(_breach_notification_recent.items()):
        if now - last > 3600:
            _breach_notification_recent.pop(key, None)
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


def _normalize_country_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if len(text) == 2 and text.isalpha():
        return text
    return ""


def _parse_roblox_user_id(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _roblox_profile_link(value: Any) -> str:
    user_id = _parse_roblox_user_id(value)
    if user_id is None:
        return "Unknown"
    return f"[{user_id}](https://www.roblox.com/users/{user_id}/profile)"


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _as_utc(value: Any) -> Optional[datetime]:
    parsed = _parse_iso_timestamp(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _identity_divergence(first: Dict[str, Any], second: Dict[str, Any]) -> Dict[str, Any]:
    comparisons = {
        "hwid": (str(first.get("hwid") or "").strip().lower(), str(second.get("hwid") or "").strip().lower()),
        "executor": (str(first.get("executor") or "").strip().lower(), str(second.get("executor") or "").strip().lower()),
        "device": (str(first.get("device") or "").strip().lower(), str(second.get("device") or "").strip().lower()),
        "country_code": (str(first.get("country_code") or "").strip().upper(), str(second.get("country_code") or "").strip().upper()),
        "ip": (str(first.get("last_ip") or "").strip(), str(second.get("last_ip") or "").strip()),
    }
    differing = [name for name, (left, right) in comparisons.items() if left and right and left != right]
    strong = [name for name in ("hwid",) if name in differing]
    return {"differing_fields": differing, "strong_differences": strong, "score": len(strong) * 3 + max(0, len(differing) - len(strong))}


def _collapse_identity_sequence(sessions: list[Dict[str, Any]]) -> list[int]:
    sequence: list[int] = []
    for session in reversed(sessions):
        try:
            identity_id = int(session.get("identity_id"))
        except (TypeError, ValueError):
            continue
        if not sequence or sequence[-1] != identity_id:
            sequence.append(identity_id)
    return sequence


def _sharing_pattern(sequence: list[int]) -> Dict[str, Any]:
    seen: set[int] = set()
    revisits = 0
    alternations = 0
    for index, identity_id in enumerate(sequence):
        if identity_id in seen and index > 0:
            revisits += 1
        seen.add(identity_id)
        if index >= 2 and sequence[index - 2] == identity_id and sequence[index - 1] != identity_id:
            alternations += 1
    return {
        "sequence": sequence,
        "unique_identities": len(seen),
        "switches": max(0, len(sequence) - 1),
        "revisit_count": revisits,
        "alternation_points": alternations,
    }


def _breach_notification_allowed(identifier: str, event_type: str) -> bool:
    """Rate-limit staff-only breach webhook alerts per license and event type."""
    now = time.time()
    bucket = (identifier, event_type)
    with _state_lock:
        _purge(now)
        last = _breach_notification_recent.get(bucket, 0.0)
        if now - last < 3600:
            return False
        _breach_notification_recent[bucket] = now
    return True


def _sharing_detection_details(pattern: Dict[str, Any], profiles: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    transitions = []
    sequence = pattern["sequence"]
    for left_id, right_id in zip(sequence, sequence[1:]):
        if left_id == right_id:
            continue
        transitions.append({
            "from": left_id,
            "to": right_id,
            "divergence": _identity_divergence(profiles.get(left_id, {}), profiles.get(right_id, {})),
        })
    max_divergence = max((x["divergence"]["score"] for x in transitions), default=0)
    return {"transitions": transitions[-8:], "max_identity_divergence": max_divergence}


# Repeated blocked HWID attempts are evaluated over the preceding 24 hours.
BLOCKED_ATTEMPT_DETECTION_WINDOW_SECONDS = 24 * 60 * 60


def _handle_hwid_mismatch_attempt(
    *,
    entry: Dict[str, Any],
    key: str,
    current_hwid: str,
    game_id: str,
    remote: str,
    executor: Any,
    job_id: Any,
    device: Any,
    country_code: Any = "",
) -> None:
    """Block a device mismatch, record a suspicion, and alert staff only.

    A single mismatch is not proof of sharing, so this function never disables
    the license. The client kick message is the only user-facing explanation;
    no direct notification or message is sent to the license owner.
    """
    identifier = str(entry.get("Identifier") or "").strip()
    owner_hwid = str(entry.get("HWID") or "").strip().lower()
    attempted_hwid = str(current_hwid or "").strip().lower()
    details = {
        "reason": "hwid_mismatch",
        "blocked": True,
        "attempted_hwid": attempted_hwid,
        "bound_hwid": owner_hwid,
        "remote_ip": str(remote or "").strip(),
        "executor": _safe_log_value(executor, fallback="Unknown", max_length=256),
        "device": _safe_log_value(device, fallback="Unknown", max_length=128),
        "country_code": _normalize_country_code(country_code),
        "game_id": str(game_id or "").strip(),
        "job_id": _safe_log_value(job_id, fallback="Unknown", max_length=128),
        "assessment": "A single HWID mismatch is suspicious but not proof of key sharing.",
    }

    event_recorded = False
    if identifier:
        try:
            _run(record_license_security_event(
                identifier,
                session_id=None,
                identity_id=None,
                event_type="key_sharing_suspected",
                confidence="low",
                details=details,
            ))
            event_recorded = True
        except Exception as exc:
            print(f"[License] Failed to record HWID-mismatch suspicion event: {exc}")

    # Keep mismatch requests blocked, but count repeated blocked attempts as their
    # own historical evidence stream. The successful-session detector cannot see
    # these requests because they exit before a session is created.
    if event_recorded and identifier and getattr(config, "LICENSE_KEY_SHARING_DETECTION_ENABLED", True):
        try:
            threshold = max(2, int(getattr(config, "LICENSE_SHARING_DETECTED_MIN_RUNS", 8)))
            window_seconds = BLOCKED_ATTEMPT_DETECTION_WINDOW_SECONDS
            since = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
            recent_attempts = _run(get_recent_license_security_events(
                identifier,
                event_type="key_sharing_suspected",
                since=since,
                limit=100,
            ))
            matching_attempts = []
            for row in recent_attempts:
                row_details = row.get("details") if isinstance(row.get("details"), dict) else {}
                attempted = str(row_details.get("attempted_hwid") or "").strip().lower()
                bound = str(row_details.get("bound_hwid") or "").strip().lower()
                if (
                    row_details.get("reason") == "hwid_mismatch"
                    and row_details.get("blocked") is True
                    and attempted
                    and attempted != bound
                ):
                    matching_attempts.append(row)

            # A previous detection must not permanently suppress future detections.
            # Treat its timestamp as the start of a new counting episode, so each
            # new batch of threshold-sized attempts can be escalated independently.
            existing_detections = _run(get_recent_license_security_events(
                identifier,
                event_type="key_sharing_detected",
                since=since,
                limit=100,
            ))
            prior_blocked_detections = [
                row for row in existing_detections
                if isinstance(row.get("details"), dict)
                and row["details"].get("detection_source") == "blocked_hwid_attempts"
            ]
            detection_times = [
                parsed for row in prior_blocked_detections
                if (parsed := _as_utc(row.get("created_at"))) is not None
            ]
            latest_detection_at = max(detection_times, default=None)
            if latest_detection_at is not None:
                matching_attempts = [
                    row for row in matching_attempts
                    if (attempt_at := _as_utc(row.get("created_at"))) is not None
                    and attempt_at > latest_detection_at
                ]

            count_window = (
                f"since the previous blocked-attempt detection at {latest_detection_at.isoformat()}"
                if latest_detection_at is not None
                else f"in the last {window_seconds} seconds"
            )
            print(
                f"[License] Blocked HWID attempt counter for {identifier!r}: "
                f"{len(matching_attempts)}/{threshold} qualifying attempts {count_window} "
                f"(queried {len(recent_attempts or [])} suspicion events)."
            )

            if len(matching_attempts) >= threshold:
                detection_details = {
                    "reason": "repeated_hwid_mismatch_attempts",
                    "detection_source": "blocked_hwid_attempts",
                    "blocked": True,
                    "blocked_attempt_count": len(matching_attempts),
                    "required_attempts": threshold,
                    "window_seconds": window_seconds,
                    "bound_hwid": owner_hwid,
                    "attempted_hwids": sorted({
                        str((row.get("details") or {}).get("attempted_hwid") or "").strip().lower()
                        for row in matching_attempts
                        if isinstance(row.get("details"), dict)
                    }),
                    "recent_attempts": [
                        {
                            "created_at": row.get("created_at"),
                            "remote_ip": (row.get("details") or {}).get("remote_ip"),
                            "game_id": (row.get("details") or {}).get("game_id"),
                            "job_id": (row.get("details") or {}).get("job_id"),
                        }
                        for row in matching_attempts[:10]
                    ],
                    "assessment": "Repeated blocked HWID mismatches met the configured detection threshold.",
                }
                disabled = False
                try:
                    disabled = bool(_run(disable_license(identifier)))
                except Exception as exc:
                    detection_details["disable_error"] = str(exc)[:500]
                    print(f"[License] Failed to disable license after repeated HWID mismatches: {exc}")
                detection_details["disabled"] = disabled
                try:
                    _run(record_license_security_event(
                        identifier,
                        session_id=None,
                        identity_id=None,
                        event_type="key_sharing_detected",
                        confidence="high",
                        details=detection_details,
                    ))
                except Exception as exc:
                    print(f"[License] Failed to record repeated-HWID key-sharing detection: {exc}")
                if _breach_notification_allowed(identifier, "key_sharing_detected"):
                    _queue_breach_webhook(
                        entry=entry,
                        key=key,
                        current_hwid=attempted_hwid,
                        game_id=game_id,
                        remote=remote,
                        executor=executor,
                        job_id=job_id,
                        device=device,
                        reason="key_sharing_detected",
                        detection_details=(
                            f"Repeated blocked HWID mismatch attempts: {len(matching_attempts)} "
                            f"within {window_seconds} seconds (threshold: {threshold}). "
                            f"License disabled: {disabled}."
                        ),
                        license_disabled=disabled,
                    )
                print(
                    f"[License] Repeated blocked HWID attempts detected for {identifier!r}: "
                    f"{len(matching_attempts)}/{threshold}; license_disabled={disabled}"
                )
        except Exception as exc:
            print(f"[License] Failed to evaluate repeated blocked HWID attempts: {exc}")

    _queue_breach_webhook(
        entry=entry,
        key=key,
        current_hwid=attempted_hwid,
        game_id=game_id,
        remote=remote,
        executor=executor,
        job_id=job_id,
        device=device,
        reason="hwid_mismatch",
        detection_details=(
            "The request was blocked because its HWID does not match the license's bound HWID. "
            "A single mismatch is only suspicious; repeated attempts are evaluated against the configured threshold."
        ),
        license_disabled=False,
    )


def _format_sharing_details(pattern: Dict[str, Any], details: Dict[str, Any], confidence: str, concurrent: list[Dict[str, Any]]) -> str:
    sequence = " → ".join(f"#{value}" for value in pattern["sequence"]) or "None"
    transitions = []
    for item in details.get("transitions", []):
        fields = ", ".join(item["divergence"].get("differing_fields", [])) or "no comparable differences"
        transitions.append(f"#{item['from']} → #{item['to']} ({fields})")
    return (
        f"Identity sequence: `{sequence}`; unique identities: {pattern['unique_identities']}; "
        f"switches: {pattern['switches']}; revisits: {pattern['revisit_count']}; "
        f"alternation points: {pattern['alternation_points']}; "
        f"max identity divergence: {details['max_identity_divergence']}; "
        f"confidence: {confidence}; concurrent sessions: {len(concurrent)}; "
        f"recent transitions: {'; '.join(transitions) or 'None'}"
    )


def assess_license_session_sharing(identifier: str, session_id: str, identity_id: Optional[int]) -> Dict[str, Any]:
    recent = _run(get_recent_license_sessions(identifier, limit=max(8, int(getattr(config, "LICENSE_SHARING_LOOKBACK_SESSIONS", 12)))))
    identity_ids = []
    for row in recent:
        if row.get("identity_id") is not None:
            try:
                identity_ids.append(int(row["identity_id"]))
            except (TypeError, ValueError):
                pass
    profiles_list = _run(get_license_identities(identifier, identity_ids=identity_ids))
    profiles = {int(row["id"]): row for row in profiles_list}
    pattern = _sharing_pattern(_collapse_identity_sequence(recent))
    details = _sharing_detection_details(pattern, profiles)

    concurrent: list[Dict[str, Any]] = []
    if identity_id is not None and getattr(config, "LICENSE_CONCURRENT_SESSION_ENFORCEMENT_ENABLED", True):
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(10, int(getattr(config, "LICENSE_SESSION_TIMEOUT_SECONDS", 75))))
        active_sessions = _run(get_active_license_sessions(
            identifier, cutoff, exclude_session_id=session_id
        ))
        for row in active_sessions:
            try:
                other_identity = int(row.get("identity_id"))
            except (TypeError, ValueError):
                continue
            if other_identity != int(identity_id):
                concurrent.append({
                    "session_id": row.get("session_id"),
                    "identity_id": other_identity,
                    "last_seen": row.get("last_seen"),
                    "game_id": row.get("game_id"),
                    "job_id": row.get("job_id"),
                })

    repeated_enabled = getattr(config, "LICENSE_REPEATED_ALTERNATION_DETECTION_ENABLED", True)
    suspected = bool(
        repeated_enabled
        and pattern["unique_identities"] >= 2
        and pattern["revisit_count"] >= int(getattr(config, "LICENSE_SHARING_SUSPECTED_REVISITS", 1))
        and len(pattern["sequence"]) >= 3
    )
    detected = bool(
        repeated_enabled
        and pattern["unique_identities"] >= 2
        and len(pattern["sequence"]) >= int(getattr(config, "LICENSE_SHARING_DETECTED_MIN_RUNS", 8))
        and pattern["revisit_count"] >= int(getattr(config, "LICENSE_SHARING_DETECTED_REVISITS", 6))
        and details["max_identity_divergence"] >= int(getattr(config, "LICENSE_SHARING_DETECTED_MIN_DIVERGENCE", 6))
    ) or bool(concurrent)
    confidence = "high" if detected else ("medium" if suspected else "low")
    return {
        "session_id": session_id,
        "identity_id": identity_id,
        "pattern": pattern,
        "details": details,
        "confidence": confidence,
        "suspected": suspected or bool(concurrent),
        "detected": detected,
        "concurrent_sessions": concurrent,
    }


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



def _send_breach_webhook(
    entry: Dict[str, Any],
    key: str,
    current_hwid: str,
    game_id: str,
    remote: str,
    executor: Any,
    job_id: Any,
    device: Any,
    reason: str,
    detection_details: Any = None,
    license_disabled: bool = False,
) -> None:
    """Send a staff-only security alert for a suspicious license-server event.

    Executor/device values are client-reported telemetry, so the alert labels
    them accordingly rather than treating them as independently verified.
    The supplied HWID is the live value from the current authentication request;
    the owner's HWID is read from the license record in Supabase.
    """
    webhook_url = getattr(config, "LICENSE_BREACH_WEBHOOK_URL", "").strip()
    enabled = bool(getattr(config, "LICENSE_BREACH_LOGGING_ENABLED", True))
    if not enabled or not webhook_url:
        return

    identifier = _safe_log_value(entry.get("Identifier"))
    discord_id = _safe_log_value(entry.get("DiscordId"), fallback="Unknown")
    owner_rank = _safe_log_value(entry.get("Rank"), fallback="Unknown")
    owner_hwid = _safe_log_value(entry.get("HWID"), fallback="Unset")
    detection_details_text = _safe_log_value(
        detection_details, fallback="No additional details", max_length=1024
    )
    used_key = _safe_log_value(key, fallback="Unknown", max_length=256)
    current_hwid = _safe_log_value(current_hwid, fallback="Unknown", max_length=128)
    game_id = _safe_log_value(game_id)
    remote = _safe_log_value(remote)

    executor_text = _safe_log_value(executor, max_length=256)
    job_id_text = _safe_log_value(job_id, max_length=128)
    device_text = _safe_log_value(device, max_length=128)

    owner_discord = f"<@{discord_id}>" if discord_id != "Unknown" else "Unknown"

    embed = {
        "title": "🚨 License Breach Detected",
        "description": (
            f"Security event `{reason}` detected while authenticating "
            f"license `{identifier}`."
        ),
        "color": 0xED4245,
        "fields": [
            {
                "name": "\n========------- Perpetrator / Current Attempt -------========",
                "value": (
                    f"**Device:** {device_text}\n"
                    f"**Executor:** {executor_text}\n"
                    f"**Current HWID:** ||`{current_hwid}`||\n"
                    f"**Used Key:** ||`{used_key}`||\n"
                    f"**Current Roblox User:** {_roblox_profile_link(entry.get('CurrentRobloxUserId'))}"
                ),
                "inline": False,
            },
            {
                "name": "\n========------- Key Owner -------========",
                "value": (
                    f"**Discord:** {owner_discord}\n"
                    f"**Identifier:** `{identifier}`\n"
                    f"**Rank:** `{owner_rank}`\n"
                    f"**Owner HWID:** ||`{owner_hwid}`||\n"
                    f"**Activation Country:** `{_safe_log_value(entry.get('ActivationCountry'), fallback='Unknown')}`\n"
                    f"**Activated Roblox User:** {_roblox_profile_link(entry.get('ActivationRobloxUserId'))}"
                ),
                "inline": False,
            },
            {
                "name": "Detection",
                "value": detection_details_text,
                "inline": False,
            },
            {
                "name": "Enforcement",
                "value": f"**License Status:** {"Disabled" if license_disabled else "Not changed"}",
                "inline": False,
            },
            {
                "name": "Request Context",
                "value": (
                    f"**Game ID:** `{game_id}`\n"
                    f"**Job ID:** `{job_id_text}`\n"
                    f"**Remote IP:** `{remote}`"
                ),
                "inline": False,
            },
        ],
        "footer": {"text": "Celestial License Security"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    payload = json.dumps({
        "embeds": [embed],
        # Breach embeds are staff-only; do not ping or message license owners.
        "allowed_mentions": {"parse": []},
    }).encode("utf-8")

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
                print(f"[License] Breach webhook returned HTTP {status}.")
    except urllib.error.HTTPError as exc:
        print(f"[License] Breach webhook failed with HTTP {exc.code}.")
    except urllib.error.URLError as exc:
        print(f"[License] Breach webhook connection failed: {exc.reason}")
    except Exception as exc:
        print(f"[License] Breach webhook failed: {exc}")


def _queue_breach_webhook(
    entry: Dict[str, Any],
    key: str,
    current_hwid: str,
    game_id: str,
    remote: str,
    executor: Any,
    job_id: Any,
    device: Any,
    reason: str,
    detection_details: Any = None,
    license_disabled: bool = False,
) -> None:
    if not getattr(config, "LICENSE_BREACH_LOGGING_ENABLED", True):
        return
    if not getattr(config, "LICENSE_BREACH_WEBHOOK_URL", "").strip():
        return

    threading.Thread(
        target=_send_breach_webhook,
        args=(entry, key, current_hwid, game_id, remote, executor, job_id, device, reason, detection_details, license_disabled),
        daemon=True,
        name="celestial-breach-webhook",
    ).start()



def _breach_rate_limited(key: str, remote: str) -> bool:
    now = time.time()
    bucket = (key, remote)
    with _state_lock:
        _purge(now)
        last = _breach_recent.get(bucket, 0.0)
        if now - last < 5.0:
            return True
        _breach_recent[bucket] = now
    return False


def handle_breach_request(raw: bytes, remote: str) -> Tuple[int, str, Dict[str, str]]:
    """Process a client-reported potential-circumvention event.

    The key must resolve to an existing license. The license row is disabled
    server-side; the reported telemetry is treated as untrusted context and
    the owner details in the alert come from Supabase.
    """
    if not getattr(config, "LICENSE_TAMPER_DETECTION_ENABLED", True):
        return 403, json.dumps({"ok": False, "reason": "tamper_detection_disabled"}), {"Content-Type": "application/json"}
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
    reason = str(payload.get("reason") or "").strip()
    details = _safe_log_value(payload.get("detection"), fallback="Unknown tamper detection", max_length=512)
    executor = _safe_log_value(payload.get("executor"), fallback="Unknown", max_length=256)
    job_id = _safe_log_value(payload.get("job_id"), fallback="Unknown", max_length=128)
    device = _safe_log_value(payload.get("device"), fallback="Unknown", max_length=128)

    if reason != "potential_circumvention":
        return 400, json.dumps({"ok": False, "reason": "invalid_breach_reason"}), {"Content-Type": "application/json"}
    if not _valid_key(key) or not is_valid_hwid(hwid) or not game_id.isdigit():
        return 400, json.dumps({"ok": False, "reason": "malformed_request"}), {"Content-Type": "application/json"}
    if _breach_rate_limited(key, remote):
        return 429, json.dumps({"ok": False, "reason": "rate_limited"}), {"Content-Type": "application/json"}

    try:
        entry = _run(get_license_by_key(key))
    except Exception as exc:
        print(f"[License] Potential-circumvention license lookup failed: {exc}")
        return 503, json.dumps({"ok": False, "reason": "backend_unavailable"}), {"Content-Type": "application/json"}

    if not entry:
        return 404, json.dumps({"ok": False, "reason": "not_whitelisted"}), {"Content-Type": "application/json"}

    identifier = str(entry.get("Identifier") or "").strip()
    if not identifier:
        return 503, json.dumps({"ok": False, "reason": "backend_unavailable"}), {"Content-Type": "application/json"}

    try:
        disabled_entry = _run(disable_license(identifier))
    except Exception as exc:
        print(f"[License] Failed to disable license after potential circumvention: {exc}")
        return 503, json.dumps({"ok": False, "reason": "backend_unavailable"}), {"Content-Type": "application/json"}

    if not disabled_entry:
        return 503, json.dumps({"ok": False, "reason": "backend_unavailable"}), {"Content-Type": "application/json"}

    _queue_breach_webhook(
        entry=entry,
        key=key,
        current_hwid=hwid,
        game_id=game_id,
        remote=remote,
        executor=executor,
        job_id=job_id,
        device=device,
        reason=reason,
        detection_details=details,
        license_disabled=True,
    )

    return 200, json.dumps({
        "ok": True,
        "reason": reason,
        "disabled": True,
        "identifier": identifier,
    }), {"Content-Type": "application/json", "Cache-Control": "no-store"}

def evaluate_and_load(
    key: str,
    hwid: str,
    game_id: str,
    remote: str,
    executor: Any = "Unknown",
    job_id: Any = "Unknown",
    device: Any = "Unknown",
    country_code: Any = "",
    roblox_user_id: Any = None,
) -> Dict[str, Any]:
    key = str(key or "").strip()
    hwid = str(hwid or "").strip().lower()
    game_id = str(game_id or "").strip()
    country_code = _normalize_country_code(country_code)
    roblox_user_id = _parse_roblox_user_id(roblox_user_id)

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

    # Historical key-sharing enforcement happens after a session is recorded,
    # so Roblox UserId/HWID/country changes alone can never disable a license.

    entry_hwid = str(entry.get("HWID") or "").strip().lower()
    if entry_hwid and entry_hwid != hwid:
        _handle_hwid_mismatch_attempt(
            entry=entry,
            key=key,
            current_hwid=hwid,
            game_id=game_id,
            remote=remote,
            executor=executor,
            job_id=job_id,
            device=device,
            country_code=country_code,
        )
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

    session_id = None
    if getattr(config, "LICENSE_IDENTITY_CLUSTERING_ENABLED", True):
        try:
            session_id = _run(
                record_license_session(
                    str(entry["Identifier"]),
                    hwid=hwid,
                    roblox_user_id=roblox_user_id,
                    country_code=country_code,
                    executor=executor,
                    device=device,
                    remote_ip=remote,
                    game_id=game_id,
                    job_id=job_id,
                )
            )
            if session_id and getattr(config, "LICENSE_KEY_SHARING_DETECTION_ENABLED", True):
                session_row = _run(get_license_session(session_id))
                sharing = assess_license_session_sharing(
                    str(entry["Identifier"]),
                    session_id,
                    int(session_row["identity_id"]) if session_row and session_row.get("identity_id") is not None else None,
                )
                if sharing.get("suspected") or sharing.get("detected"):
                    details = _format_sharing_details(
                        sharing["pattern"],
                        sharing["details"],
                        sharing["confidence"],
                        sharing.get("concurrent_sessions", []),
                    )
                    breach_entry = dict(entry)
                    breach_entry["CurrentRobloxUserId"] = roblox_user_id
                    event_type = "key_sharing_detected" if sharing.get("detected") else "key_sharing_suspected"
                    if sharing.get("detected"):
                        disabled = False
                        disable_error = None
                        try:
                            disabled = bool(_run(disable_license(str(entry["Identifier"]))))
                        except Exception as exc:
                            disable_error = exc
                            print(f"[License] Failed to disable shared license: {exc}")
                        details_for_event = details + (f"; Enforcement error: {disable_error}" if disable_error else "")
                        if _breach_notification_allowed(str(entry["Identifier"]), event_type):
                            _queue_breach_webhook(
                                entry=breach_entry, key=key, current_hwid=hwid, game_id=game_id, remote=remote,
                                executor=executor, job_id=job_id, device=device, reason=event_type,
                                detection_details=details_for_event, license_disabled=disabled,
                            )
                        try:
                            _run(record_license_security_event(
                                str(entry["Identifier"]), session_id=session_id, identity_id=sharing.get("identity_id"),
                                event_type=event_type, confidence="high",
                                details={**sharing, "disabled": disabled},
                            ))
                        except Exception as exc:
                            print(f"[License] Failed to record key-sharing detection event: {exc}")
                        if disabled:
                            return {"allowed": False, "reason": "key_sharing_detected"}
                        return {"allowed": False, "reason": "backend_unavailable"}
                    else:
                        if _breach_notification_allowed(str(entry["Identifier"]), event_type):
                            _queue_breach_webhook(
                                entry=breach_entry, key=key, current_hwid=hwid, game_id=game_id, remote=remote,
                                executor=executor, job_id=job_id, device=device, reason=event_type,
                                detection_details=details, license_disabled=False,
                            )
                        try:
                            _run(record_license_security_event(
                                str(entry["Identifier"]), session_id=session_id, identity_id=sharing.get("identity_id"),
                                event_type=event_type, confidence="medium",
                                details=sharing,
                            ))
                        except Exception as exc:
                            print(f"[License] Failed to record key-sharing suspicion event: {exc}")
        except Exception as exc:
            print(f"[License] Session history/security analysis failed: {exc}")

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
        "session_id": session_id,
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
    executor = _safe_log_value(payload.get("executor"), fallback="Unknown", max_length=256)
    job_id = _safe_log_value(payload.get("job_id"), fallback="Unknown", max_length=128)
    device = _safe_log_value(payload.get("device"), fallback="Unknown", max_length=128)
    country_code = _normalize_country_code(payload.get("country_code"))
    roblox_user_id = _parse_roblox_user_id(payload.get("roblox_user_id"))

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

    result = evaluate_and_load(
        key,
        hwid,
        game_id,
        remote,
        executor=executor,
        job_id=job_id,
        device=device,
        country_code=country_code,
        roblox_user_id=roblox_user_id,
    )
    status = 200 if result.get("allowed") else 403
    return status, json.dumps(result), {"Content-Type": "application/json", "Cache-Control": "no-store"}


def handle_heartbeat_request(raw: bytes, remote: str) -> Tuple[int, str, Dict[str, str]]:
    """Refresh a server-issued session while the client is still running."""
    if len(raw) > 4096:
        return 413, json.dumps({"ok": False, "reason": "request_too_large"}), {"Content-Type": "application/json"}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, json.dumps({"ok": False, "reason": "malformed_request"}), {"Content-Type": "application/json"}
    if not isinstance(payload, dict):
        return 400, json.dumps({"ok": False, "reason": "malformed_request"}), {"Content-Type": "application/json"}

    session_id = str(payload.get("session_id") or "").strip()
    hwid = str(payload.get("hwid") or "").strip().lower()
    timestamp = payload.get("timestamp")
    try:
        int(timestamp)
    except (TypeError, ValueError):
        return 400, json.dumps({"ok": False, "reason": "malformed_request"}), {"Content-Type": "application/json"}
    if abs(time.time() - int(timestamp)) > MAX_CLOCK_SKEW:
        return 400, json.dumps({"ok": False, "reason": "stale_timestamp"}), {"Content-Type": "application/json"}
    if not NONCE_RE.fullmatch(session_id) and len(session_id) < 16:
        # Session IDs are UUIDs and therefore high-entropy bearer credentials.
        return 400, json.dumps({"ok": False, "reason": "invalid_session"}), {"Content-Type": "application/json"}
    if not is_valid_hwid(hwid):
        return 400, json.dumps({"ok": False, "reason": "invalid_hwid_format"}), {"Content-Type": "application/json"}

    try:
        session = _run(heartbeat_license_session(session_id, hwid))
    except Exception as exc:
        print(f"[License] Session heartbeat failed: {exc}")
        return 503, json.dumps({"ok": False, "reason": "backend_unavailable"}), {"Content-Type": "application/json"}
    if not session:
        return 404, json.dumps({"ok": False, "reason": "session_not_found"}), {"Content-Type": "application/json"}
    if session.get("ok") is not True:
        return 403, json.dumps({"ok": False, "reason": session.get("reason", "session_identity_mismatch")}), {"Content-Type": "application/json"}

    identifier = str((session.get("session") or {}).get("license_identifier") or "").strip()
    if not identifier:
        return 503, json.dumps({"ok": False, "reason": "backend_unavailable"}), {"Content-Type": "application/json"}
    try:
        entry = _run(get_license_by_key(str(payload.get("key") or "").strip())) if payload.get("key") else _run(get_license_by_identifier(identifier))
    except Exception:
        entry = None
    if not entry or not entry.get("Enabled", True):
        return 403, json.dumps({"ok": False, "reason": "license_disabled"}), {"Content-Type": "application/json"}

    return 200, json.dumps({"ok": True, "reason": "ok", "last_seen": (session.get("session") or {}).get("last_seen")}), {"Content-Type": "application/json", "Cache-Control": "no-store"}


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
    country_code = _normalize_country_code(payload.get("country_code"))
    roblox_user_id = _parse_roblox_user_id(payload.get("roblox_user_id"))
    if not _valid_key(key) or not is_valid_hwid(hwid) or not game_id.isdigit() or not NONCE_RE.fullmatch(execution_token):
        return 400, json.dumps({"ok": False, "reason": "malformed_request"}), {"Content-Type": "application/json"}

    try:
        entry = _run(get_license_by_key(key))
        if not entry or not entry.get("Enabled", True):
            return 403, json.dumps({"ok": False, "reason": "not_authorized"}), {"Content-Type": "application/json"}
        current_hwid = str(entry.get("HWID") or "").strip().lower()
        if not current_hwid or current_hwid != hwid:
            if current_hwid and current_hwid != hwid:
                _handle_hwid_mismatch_attempt(
                    entry=entry,
                    key=key,
                    current_hwid=hwid,
                    game_id=game_id,
                    remote=remote,
                    executor=executor,
                    job_id=job_id,
                    device=device,
                    country_code=country_code,
                )
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
        completed_entry = _run(
            complete_successful_execution(
                str(entry["Identifier"]),
                activation_country=country_code,
                activation_roblox_user_id=roblox_user_id,
            )
        )
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

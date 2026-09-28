"""Supabase-backed persistence for durable bot state.

Bot state used to live in storage/BotState.json in the bot's GitHub storage
repository. This module is now the single persistence abstraction for that
state so command/reconciliation code does not care where the state is stored.

The table shape is intentionally simple: one row per top-level state key in
``public.bot_state`` and the actual value in ``state_value`` (jsonb).  Reads
return the same dictionary shape the previous file-backed implementation exposed,
which keeps the existing command logic intact while moving persistence to
Supabase.
"""

import asyncio
import copy
import secrets
from typing import Any, Callable, Dict, Optional

from .supabase_db import _client_sync, _is_transient_supabase_error


class BotStateError(Exception):
    """Raised when the bot state cannot be read or persisted."""


# These are the same durable top-level keys the previous file-backed implementation supported.
# There is intentionally no schema_version key: Supabase's table itself is
# the current schema, and migrations are tracked by SQL migration files.
DEFAULT_BOT_STATE: Dict[str, Any] = {
    "temp_bans": [],
    "banned_users": [],
    "lockdown": None,
    "channel_locks": [],
    "temp_bot_access": [],
    "pending_breach_alerts": [],
    "reaction_role_panel": None,
    "temp_roles": [],
    "ghostping_mode": "nothing",
    "autorole": {"enabled": False, "role_id": None},
    "alerts_enabled": {"whitelist": True, "moderation": True},
    "dms_enabled": True,
    "warnings": [],
    "warning_config": {
        "enabled": False,
        "threshold": 3,
        "action": "timeout",
        "timeout_minutes": 60,
        "reset_after_action": True,
        "notify_target": True,
    },
    "hwid_reset_cooldowns": {},
}

_STATE_WRITE_LOCK = asyncio.Lock()
_TRANSIENT_WRITE_RETRIES = 3


def new_state_id(prefix: str) -> str:
    """Return a short random ID for a durable state entry."""
    return f"{prefix}_{secrets.token_hex(3)}"


def _clone_defaults() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT_BOT_STATE)


def _fetch_state_sync() -> Dict[str, Any]:
    client = _client_sync()
    try:
        result = (
            client.table("bot_state")
            .select("state_key,state_value")
            .execute()
        )
    except Exception as exc:
        raise BotStateError(f"Failed to fetch bot state from Supabase: {exc}") from exc

    state = _clone_defaults()
    for row in result.data or []:
        key = row.get("state_key")
        if not key or key == "schema_version":
            continue
        state[key] = row.get("state_value")
    return state


async def fetch_botstate() -> Dict[str, Any]:
    """Fetch the complete bot state from Supabase."""
    last_error: Optional[BaseException] = None
    for attempt in range(_TRANSIENT_WRITE_RETRIES):
        try:
            return await asyncio.to_thread(_fetch_state_sync)
        except Exception as exc:
            last_error = exc
            if not _is_transient_supabase_error(exc) or attempt + 1 >= _TRANSIENT_WRITE_RETRIES:
                if isinstance(exc, BotStateError):
                    raise
                raise BotStateError(f"Failed to fetch bot state from Supabase: {exc}") from exc
            await asyncio.sleep(0.75 * (2 ** attempt))
    raise BotStateError(f"Failed to fetch bot state from Supabase: {last_error}") from last_error


def _normalize_state(state: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(state, dict):
        raise BotStateError("Bot state must be a JSON object")

    normalized = copy.deepcopy(state)
    # A legacy caller/import can still hand this function the old field; it is
    # deliberately discarded so schema_version can never return to Supabase.
    normalized.pop("schema_version", None)
    return normalized


def _write_state_sync(old_state: Dict[str, Any], new_state: Dict[str, Any]) -> None:
    old_state = _normalize_state(old_state)
    new_state = _normalize_state(new_state)

    # Ensure all changed/new keys are represented by one idempotent upsert.
    changed_rows = [
        {"state_key": key, "state_value": value}
        for key, value in new_state.items()
        if key not in old_state or old_state[key] != value
    ]

    removed_keys = [key for key in old_state if key not in new_state]

    client = _client_sync()
    try:
        if changed_rows:
            client.table("bot_state").upsert(changed_rows, on_conflict="state_key").execute()
        if removed_keys:
            client.table("bot_state").delete().in_("state_key", removed_keys).execute()
    except Exception as exc:
        raise BotStateError(f"Failed to persist bot state to Supabase: {exc}") from exc


async def update_botstate(
    mutate: Callable[[Dict[str, Any]], Dict[str, Any]],
    message: str,
    max_retries: int = 3,
) -> Dict[str, Any]:
    """Read, mutate, and persist bot state through Supabase.

    ``message`` is accepted for source compatibility with existing callers.
    Supabase does not need a commit message.

    A process-local lock prevents two bot tasks from read-modify-writing the
    same state snapshot concurrently. Upserts are idempotent, so transient
    Supabase failures can safely be retried.
    """
    retries = max(1, int(max_retries or 1))

    async with _STATE_WRITE_LOCK:
        last_error: Optional[BaseException] = None
        for attempt in range(retries):
            try:
                state = await fetch_botstate()
                original_state = copy.deepcopy(state)
                new_state = mutate(state)
                if new_state is None:
                    new_state = state
                new_state = _normalize_state(new_state)
                await asyncio.to_thread(_write_state_sync, original_state, new_state)
                return new_state
            except Exception as exc:
                last_error = exc
                if not _is_transient_supabase_error(exc) or attempt + 1 >= retries:
                    if isinstance(exc, BotStateError):
                        raise
                    raise BotStateError(f"Failed to update bot state in Supabase: {exc}") from exc
                await asyncio.sleep(0.75 * (2 ** attempt))

    raise BotStateError(f"Failed to update bot state in Supabase: {last_error}") from last_error

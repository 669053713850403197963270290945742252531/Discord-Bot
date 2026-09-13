"""Supabase-backed license/user database helpers.

This module is the single persistence layer for the whitelist/license system.
The bot talks to Supabase directly; GitHub is not used for license records.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from supabase import create_client

from . import config

_client = None
_client_lock = asyncio.Lock()


def _client_sync():
    global _client
    if _client is None:
        if not config.SUPABASE_URL or not config.SUPABASE_SECRET_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY must be configured")
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_SECRET_KEY)
    return _client


def _iso(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return str(value)


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _record_from_db(row: Dict[str, Any], game_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    games = [str(x) for x in (game_ids or [])]
    return {
        "Identifier": row.get("identifier"),
        "DiscordId": row.get("discord_id"),
        "Key": row.get("license_key"),
        "Activated": _iso(row.get("activated")),
        "Executions": int(row.get("executions") or 0),
        "Rank": row.get("rank") or "User",
        "Notes": row.get("notes"),
        "Games": games or [],
        "Enabled": bool(row.get("enabled", True)),
        "ExpiresAt": _iso(row.get("expires_at")),
        "CreatedAt": _iso(row.get("created_at")),
        "UpdatedAt": _iso(row.get("updated_at")),
        "HWID": row.get("hwid"),
        "LastHwidReset": _iso(row.get("last_hwid_reset")),
        "totalHwidResets": int(row.get("hwid_resets") or 0),
    }


def _db_row_from_record(record: Dict[str, Any], identifier_fallback: Optional[str] = None) -> Dict[str, Any]:
    identifier = str(record.get("Identifier") or identifier_fallback or "").strip()
    if not identifier:
        raise ValueError("License record is missing Identifier")
    out = {
        "identifier": identifier,
        "discord_id": (str(record.get("DiscordId")).strip() if record.get("DiscordId") not in (None, "") else None),
        "license_key": (str(record.get("Key")).strip() if record.get("Key") not in (None, "") else None),
        "activated": _iso(record.get("Activated")),
        "executions": int(record.get("Executions") or 0),
        "rank": str(record.get("Rank") or "User"),
        "notes": record.get("Notes"),
        "enabled": bool(record.get("Enabled", True)),
        "expires_at": _iso(record.get("ExpiresAt")),
    }
    return out


def _parse_stored_game_ids(value: Any) -> List[str]:
    """Parse the single license_games.game_id field into normalized game IDs.

    The database stores one row per identifier, with multiple game IDs kept in
    the same text field as a comma-separated value. The wildcard is stored as
    a literal ``*``. This parser also accepts legacy rows that still contain
    one game ID per row so existing data remains readable.
    """
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    parts = text.split(",")
    result: List[str] = []
    seen = set()
    for part in parts:
        game_id = part.strip()
        if game_id and game_id not in seen:
            seen.add(game_id)
            result.append(game_id)
    return result


def _fetch_games_for_identifiers_sync(identifiers: List[str]) -> Dict[str, List[str]]:
    if not identifiers:
        return {}
    rows = (
        _client_sync().table("license_games")
        .select("identifier,game_id")
        .in_("identifier", identifiers)
        .execute()
    ).data or []
    result = {identifier: [] for identifier in identifiers}
    seen = {identifier: set() for identifier in identifiers}
    for row in rows:
        identifier = str(row["identifier"])
        for game_id in _parse_stored_game_ids(row.get("game_id")):
            if game_id not in seen.setdefault(identifier, set()):
                seen[identifier].add(game_id)
                result.setdefault(identifier, []).append(game_id)
    return result


def _fetch_users_sync() -> List[Dict[str, Any]]:
    rows = (_client_sync().table("licenses").select("*").order("created_at").execute()).data or []
    game_map = _fetch_games_for_identifiers_sync([str(row["identifier"]) for row in rows])
    return [_record_from_db(row, game_map.get(str(row["identifier"]), [])) for row in rows]


async def fetch_users() -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_fetch_users_sync)


def _fetch_temp_whitelists_sync() -> List[Dict[str, Any]]:
    """Fetch only the fields startup temp-whitelist reconciliation needs."""
    rows = (
        _client_sync()
        .table("licenses")
        .select("identifier,rank,expires_at")
        .eq("rank", "Temp")
        .execute()
    ).data or []
    return [
        {
            "Identifier": row.get("identifier"),
            "Rank": row.get("rank"),
            "ExpiresAt": _iso(row.get("expires_at")),
        }
        for row in rows
    ]


async def fetch_temp_whitelists() -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_fetch_temp_whitelists_sync)


async def fetch_users_with_sha() -> Tuple[List[Dict[str, Any]], None]:
    return await fetch_users(), None


async def fetch_api_text_and_sha() -> Tuple[str, None]:
    users = await fetch_users()
    return serialize_users_json(users), None


def serialize_users_json(users: List[Dict[str, Any]]) -> str:
    lines = json.dumps(users, indent=4, ensure_ascii=False).splitlines()
    output = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith('"Games": [') and not stripped.rstrip().endswith(("],", "]")):
            indent = line[:len(line) - len(stripped)]
            parts = [stripped[len('"Games": '):]]
            i += 1
            while i < len(lines):
                part = lines[i].strip()
                parts.append(part)
                if part.endswith(("],", "]")):
                    break
                i += 1
            compact = " ".join(parts).replace("[ ", "[").replace(" ]", "]")
            output.append(f'{indent}"Games": {compact}')
        else:
            output.append(line)
        i += 1
    return "\n".join(output) + "\n"


def _set_games_sync(identifier: str, games: List[str]) -> None:
    """Store exactly one license_games row for an identifier.

    Multiple game IDs are serialized into the single text ``game_id`` field,
    e.g. ``286090429, 123974602339071``. ``*`` means all supported games and
    is stored literally; the ``games`` table itself is never modified here.
    """
    client = _client_sync()
    identifier = str(identifier).strip()
    if not identifier:
        raise ValueError("License record is missing Identifier")

    normalized: List[str] = []
    seen = set()
    for raw_game_id in games or ["*"]:
        for game_id in _parse_stored_game_ids(raw_game_id):
            if game_id == "*":
                normalized = ["*"]
                seen = {"*"}
                break
            if game_id not in seen:
                seen.add(game_id)
                normalized.append(game_id)
        if normalized == ["*"]:
            break

    if not normalized:
        normalized = ["*"]

    client.table("license_games").delete().eq("identifier", identifier).execute()
    client.table("license_games").insert({
        "identifier": identifier,
        "game_id": ", ".join(normalized),
    }).execute()


async def set_license_games(identifier: str, games: List[str]) -> None:
    await asyncio.to_thread(_set_games_sync, identifier, games)


async def get_license_game_ids(identifier: str) -> List[str]:
    rows = (_client_sync().table("license_games").select("game_id").eq("identifier", identifier).execute()).data or []
    result: List[str] = []
    seen = set()
    for row in rows:
        for game_id in _parse_stored_game_ids(row.get("game_id")):
            if game_id not in seen:
                seen.add(game_id)
                result.append(game_id)
    return result


async def fetch_redeemable_keys() -> List[str]:
    def _fetch():
        rows = (_client_sync().table("license_keys").select("key").order("key").execute()).data or []
        return [str(row["key"]).strip() for row in rows if row.get("key") not in (None, "")]
    return await asyncio.to_thread(_fetch)


async def is_redeemable_key(key: str) -> bool:
    def _exists():
        rows = (_client_sync().table("license_keys").select("key").eq("key", str(key).strip()).limit(1).execute()).data or []
        return bool(rows)
    return await asyncio.to_thread(_exists)


async def create_redeemable_key(key: str) -> bool:
    normalized = str(key).strip()
    if not normalized:
        raise ValueError("License key cannot be empty")

    def _create():
        result = _client_sync().table("license_keys").insert({"key": normalized}).execute()
        return bool(result.data)
    return await asyncio.to_thread(_create)


async def delete_redeemable_key(key: str) -> bool:
    normalized = str(key).strip()

    def _delete():
        result = (
            _client_sync().table("license_keys")
            .delete()
            .eq("key", normalized)
            .select("key")
            .execute()
        )
        return bool(result.data)
    return await asyncio.to_thread(_delete)


async def delete_redeemable_keys(keys: List[str], *, chunk_size: int = 20, retries: int = 3) -> List[str]:
    """Delete multiple unredeemed keys in bounded batches with retry handling.

    Returns the keys Supabase confirmed as deleted. Batching avoids issuing one
    network request per key, which can cause slow commands and transient 504
    gateway timeouts for larger amounts such as /key clear amount:50.
    """
    normalized_keys = []
    seen = set()
    for key in keys:
        normalized = str(key).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            normalized_keys.append(normalized)

    if not normalized_keys:
        return []

    async def delete_chunk(chunk: List[str]) -> List[str]:
        def _delete():
            result = (
                _client_sync().table("license_keys")
                .delete()
                .in_("key", chunk)
                .select("key")
                .execute()
            )
            return [str(row["key"]).strip() for row in (result.data or []) if row.get("key")]

        last_error = None
        for attempt in range(retries):
            try:
                return await asyncio.to_thread(_delete)
            except Exception as exc:
                last_error = exc
                if attempt + 1 < retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
        raise last_error

    removed = []
    for start in range(0, len(normalized_keys), max(1, chunk_size)):
        removed.extend(await delete_chunk(normalized_keys[start:start + max(1, chunk_size)]))
    return removed


def _get_by_key_sync(key: str) -> Optional[Dict[str, Any]]:
    rows = (_client_sync().table("licenses").select("*").eq("license_key", key).limit(1).execute()).data or []
    if not rows:
        return None
    row = rows[0]
    games = _fetch_games_for_identifiers_sync([str(row["identifier"])])
    return _record_from_db(row, games.get(str(row["identifier"]), []))


async def get_license_by_key(key: str) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(_get_by_key_sync, str(key).strip())


def _get_by_discord_sync(discord_id: str) -> Optional[Dict[str, Any]]:
    rows = (_client_sync().table("licenses").select("*").eq("discord_id", str(discord_id)).limit(1).execute()).data or []
    if not rows:
        return None
    row = rows[0]
    games = _fetch_games_for_identifiers_sync([str(row["identifier"])])
    return _record_from_db(row, games.get(str(row["identifier"]), []))


async def get_license_by_discord_id(discord_id: str) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(_get_by_discord_sync, str(discord_id))


def _has_license_by_discord_id_sync(discord_id: str) -> bool:
    rows = (
        _client_sync()
        .table("licenses")
        .select("license_key")
        .eq("discord_id", str(discord_id))
        .limit(1)
        .execute()
    ).data or []
    return bool(rows and rows[0].get("license_key"))


async def has_license_by_discord_id(discord_id: str) -> bool:
    """Fast preflight used by the control-panel Redeem Key button."""
    return await asyncio.to_thread(_has_license_by_discord_id_sync, str(discord_id))



def _get_by_identifier_sync(identifier: str) -> Optional[Dict[str, Any]]:
    rows = (_client_sync().table("licenses").select("*").eq("identifier", str(identifier)).limit(1).execute()).data or []
    if not rows:
        return None
    row = rows[0]
    games = _fetch_games_for_identifiers_sync([str(row["identifier"])])
    return _record_from_db(row, games.get(str(row["identifier"]), []))


async def get_license_by_identifier(identifier: str) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(_get_by_identifier_sync, str(identifier))


def _upsert_sync(record: Dict[str, Any]) -> Dict[str, Any]:
    client = _client_sync()
    identifier = str(record["Identifier"])
    row = _db_row_from_record(record)
    result = client.table("licenses").upsert(row, on_conflict="identifier").execute()
    if not result.data:
        raise RuntimeError("Supabase did not return the updated license")
    _set_games_sync(identifier, record.get("Games") or ["*"])
    return _record_from_db(result.data[0], record.get("Games") or ["*"])


async def upsert_license(record: Dict[str, Any]) -> Dict[str, Any]:
    return await asyncio.to_thread(_upsert_sync, record)


async def create_license(record: Dict[str, Any]) -> Dict[str, Any]:
    return await upsert_license(record)


async def update_license(identifier: str, **updates: Any) -> Optional[Dict[str, Any]]:
    def _update():
        client = _client_sync()
        row_updates = {}
        mapping = {
            "discord_id": "discord_id", "license_key": "license_key", "activated": "activated",
            "executions": "executions", "rank": "rank", "notes": "notes", "enabled": "enabled",
            "expires_at": "expires_at",
        }
        for key, value in updates.items():
            if key in mapping:
                row_updates[mapping[key]] = _iso(value) if key in {"activated", "expires_at"} else value
        result = client.table("licenses").update(row_updates).eq("identifier", identifier).execute()
        if not result.data:
            return None
        if "games" in updates:
            _set_games_sync(identifier, updates["games"])
        games = _fetch_games_for_identifiers_sync([identifier]).get(identifier, [])
        return _record_from_db(result.data[0], games)
    return await asyncio.to_thread(_update)


async def delete_license(identifier: str) -> bool:
    def _delete():
        result = _client_sync().table("licenses").delete().eq("identifier", identifier).execute()
        return bool(result.data)
    return await asyncio.to_thread(_delete)


async def redeem_license(key: str, discord_id: str, identifier: str, rank: str = "User") -> Optional[Dict[str, Any]]:
    def _redeem():
        client = _client_sync()
        normalized_key = str(key).strip()
        key_rows = (client.table("license_keys").select("key").eq("key", normalized_key).limit(1).execute()).data or []
        if not key_rows:
            return None

        rows = (client.table("licenses").select("*").eq("license_key", normalized_key).limit(1).execute()).data or []
        row = rows[0] if rows else None
        new_identifier = str(identifier).strip() or str(discord_id)

        if row:
            old_identifier = str(row["identifier"])
            current_discord = str(row.get("discord_id") or "")
            if current_discord and current_discord != str(discord_id):
                raise PermissionError("license_already_redeemed")

            if old_identifier != new_identifier:
                duplicate = (client.table("licenses").select("identifier").eq("identifier", new_identifier).limit(1).execute()).data or []
                if duplicate:
                    raise ValueError("identifier_already_exists")
                game_rows = (client.table("license_games").select("game_id").eq("identifier", old_identifier).execute()).data or []
                old_games: List[str] = []
                seen_games = set()
                for game_row in game_rows:
                    for game_id in _parse_stored_game_ids(game_row.get("game_id")):
                        if game_id not in seen_games:
                            seen_games.add(game_id)
                            old_games.append(game_id)
                client.table("license_games").delete().eq("identifier", old_identifier).execute()
                updated = client.table("licenses").update({
                    "identifier": new_identifier,
                    "discord_id": str(discord_id),
                    "rank": rank,
                }).eq("identifier", old_identifier).execute()
                if not updated.data:
                    raise RuntimeError("Failed to redeem the pending license")
                if old_games:
                    client.table("license_games").insert({
                        "identifier": new_identifier,
                        "game_id": ", ".join(old_games),
                    }).execute()
            else:
                updated = client.table("licenses").update({"discord_id": str(discord_id), "rank": rank}).eq("identifier", old_identifier).execute()
                if not updated.data:
                    raise RuntimeError("Failed to redeem the license")
        else:
            duplicate = (client.table("licenses").select("identifier").eq("identifier", new_identifier).limit(1).execute()).data or []
            if duplicate:
                raise ValueError("identifier_already_exists")
            created = client.table("licenses").insert({
                "identifier": new_identifier,
                "discord_id": str(discord_id),
                "license_key": normalized_key,
                "activated": None,
                "executions": 0,
                "rank": rank,
                "notes": None,
                "enabled": True,
                "expires_at": None,
            }).execute()
            if not created.data:
                raise RuntimeError("Failed to create the redeemed license")
            _set_games_sync(new_identifier, ["*"])

        # Consume the redeemable key only after the license update succeeded.
        consumed = (
            client.table("license_keys")
            .delete()
            .eq("key", normalized_key)
            .select("key")
            .execute()
        ).data or []
        if not consumed:
            raise RuntimeError("License key was no longer available for redemption")

        fresh = (client.table("licenses").select("*").eq("identifier", new_identifier).limit(1).execute()).data or []
        if not fresh:
            raise RuntimeError("Redeemed license could not be reloaded")
        games = _fetch_games_for_identifiers_sync([new_identifier]).get(new_identifier, [])
        return _record_from_db(fresh[0], games)
    return await asyncio.to_thread(_redeem)


def _replace_users_sync(users: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    client = _client_sync()
    normalized = []
    desired_ids = set()
    for record in users:
        identifier = str(record.get("Identifier") or "").strip()
        if not identifier:
            continue
        desired_ids.add(identifier)
        normalized.append(record)
        client.table("licenses").upsert(_db_row_from_record(record), on_conflict="identifier").execute()
        _set_games_sync(identifier, record.get("Games") or ["*"])

    existing = (client.table("licenses").select("identifier").execute()).data or []
    for row in existing:
        identifier = str(row["identifier"])
        if identifier not in desired_ids:
            client.table("licenses").delete().eq("identifier", identifier).execute()
    return _fetch_users_sync()


async def replace_users(users: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_replace_users_sync, users)


async def commit_users(users: List[Dict[str, Any]], sha=None, message: str = "Update licenses", session=None):
    return await replace_users(users)


async def commit_content(content_str: str, sha=None, message: str = "Update licenses", session=None):
    parsed = json.loads(content_str)
    return await replace_users(parsed)


async def fetch_api_file(session=None):
    content, sha = await fetch_api_text_and_sha()
    return {"content": content, "sha": sha}


async def fetch_raw_text(url: str, session=None) -> str:
    import aiohttp
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.text()


async def fetch_users_for_export() -> List[Dict[str, Any]]:
    return await fetch_users()


async def list_games() -> List[Dict[str, Any]]:
    return await asyncio.to_thread(lambda: (_client_sync().table("games").select("*").order("id").execute()).data or [])


async def get_game(game_id: str) -> Optional[Dict[str, Any]]:
    def _get():
        rows = (_client_sync().table("games").select("*").eq("id", str(game_id)).limit(1).execute()).data or []
        return rows[0] if rows else None
    return await asyncio.to_thread(_get)


def _create_game_sync(game_id: str, name: str, script_path: str) -> Dict[str, Any]:
    game_id = str(game_id or "").strip()
    name = str(name or "").strip()
    script_path = str(script_path or "").strip().lstrip("/")
    if not game_id:
        raise ValueError("Game ID cannot be empty")
    if not name:
        raise ValueError("Game name cannot be empty")
    if not script_path:
        raise ValueError("Script path cannot be empty")
    if ".." in script_path.split("/"):
        raise ValueError("Script path cannot contain '..' path segments")

    client = _client_sync()
    existing = (client.table("games").select("id").eq("id", game_id).limit(1).execute()).data or []
    if existing:
        raise ValueError(f"Game with ID {game_id!r} already exists")

    result = client.table("games").insert({
        "id": game_id,
        "name": name,
        "script_path": script_path,
        "enabled": True,
    }).execute()
    rows = result.data or []
    if not rows:
        raise RuntimeError("Supabase did not return the created game record")
    return rows[0]


async def create_game(game_id: str, name: str, script_path: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_create_game_sync, game_id, name, script_path)


async def game_allowed(identifier: str, game_id: str) -> bool:
    def _check():
        client = _client_sync()
        game = (client.table("games").select("id,enabled").eq("id", str(game_id)).limit(1).execute()).data or []
        if not game or not bool(game[0].get("enabled", True)):
            return False
        rows = (client.table("license_games").select("game_id").eq("identifier", str(identifier)).execute()).data or []
        allowed_games = set()
        for row in rows:
            allowed_games.update(_parse_stored_game_ids(row.get("game_id")))
        return str(game_id) in allowed_games or "*" in allowed_games
    return await asyncio.to_thread(_check)


async def get_license_for_user(discord_id: str) -> Optional[Dict[str, Any]]:
    return await get_license_by_discord_id(discord_id)


async def bind_license_hwid(identifier: str, hwid: str) -> bool:
    """Bind an unbound license to a HWID without changing an existing binding."""
    def _bind():
        client = _client_sync()
        rows = (client.table("licenses").select("hwid").eq("identifier", str(identifier)).limit(1).execute()).data or []
        if not rows:
            return False
        current = rows[0].get("hwid")
        if current:
            return str(current).strip().lower() == str(hwid).strip().lower()
        result = client.table("licenses").update({"hwid": str(hwid).strip().lower()}).eq("identifier", str(identifier)).is_("hwid", "null").execute()
        return bool(result.data)
    return await asyncio.to_thread(_bind)


async def reset_license_hwid(identifier: str) -> Optional[Dict[str, Any]]:
    """Clear a license's bound HWID and start its persistent reset cooldown."""
    def _reset():
        client = _client_sync()
        rows = (client.table("licenses").select("*").eq("identifier", str(identifier)).limit(1).execute()).data or []
        if not rows:
            return None
        row = rows[0]
        reset_count = int(row.get("hwid_resets") or 0) + 1
        now = datetime.now(timezone.utc)
        result = client.table("licenses").update({
            "hwid": None,
            "hwid_resets": reset_count,
            "last_hwid_reset": now.isoformat(),
        }).eq("identifier", str(identifier)).execute()
        return result.data[0] if result.data else None
    return await asyncio.to_thread(_reset)


async def clear_hwid_reset_cooldown(identifier: str) -> Optional[Dict[str, Any]]:
    """Clear only the persistent HWID reset cooldown timestamp."""
    def _clear():
        client = _client_sync()
        result = (
            client.table("licenses")
            .update({"last_hwid_reset": None})
            .eq("identifier", str(identifier))
            .execute()
        )
        return result.data[0] if result.data else None
    return await asyncio.to_thread(_clear)


async def complete_successful_execution(identifier: str) -> Optional[Dict[str, Any]]:
    def _complete():
        client = _client_sync()
        rows = (client.table("licenses").select("*").eq("identifier", identifier).limit(1).execute()).data or []
        if not rows:
            return None
        row = rows[0]
        updates = {"executions": int(row.get("executions") or 0) + 1}
        if row.get("activated") is None:
            updates["activated"] = datetime.now(timezone.utc).isoformat()
        result = client.table("licenses").update(updates).eq("identifier", identifier).execute()
        return result.data[0] if result.data else None
    return await asyncio.to_thread(_complete)




async def get_botstate_placeholder(*args, **kwargs):
    return None

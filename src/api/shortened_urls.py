"""Supabase-backed persistence for shortened links, pastes, and file uploads."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from supabase import create_client

from . import config


class ShortenedURLStoreError(Exception):
    """Raised when the persistent shortened-URL database cannot be used."""

    def __init__(self, message: str):
        super().__init__(message)


_TRANSIENT_CODES = {"408", "425", "429", "500", "502", "503", "504"}
_MAX_RETRIES = 3
_STATE_KINDS = {"shorten", "paste", "file"}
_COMMON_FIELDS = {"deletion_url", "creator_id", "created_at"}
_KIND_FIELDS = {
    "shorten": {"original_url", "shortened_url"},
    "paste": {"title", "language", "paste_url", "raw_url"},
    "file": {"original_filename", "content_type", "size", "size_bytes", "file_url"},
}

_client = None
_client_lock = asyncio.Lock()


def _is_transient(exc: BaseException) -> bool:
    code = str(getattr(exc, "code", "") or "")
    text = str(exc).lower()
    return (
        code in _TRANSIENT_CODES
        or "gateway timeout" in text
        or "json could not be generated" in text
        or "timed out" in text
        or "timeout" in text
    )


def _client_sync():
    global _client
    if _client is None:
        if not config.SUPABASE_URL or not config.SUPABASE_SECRET_KEY:
            raise ShortenedURLStoreError(
                "SUPABASE_URL and SUPABASE_SECRET_KEY must be configured."
            )
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_SECRET_KEY)
    return _client


def _format_error(operation: str, exc: BaseException) -> ShortenedURLStoreError:
    message = str(exc).strip() or exc.__class__.__name__
    return ShortenedURLStoreError(f"Supabase could not {operation}: {message}")


async def _run(operation: Callable[[], Any], operation_name: str) -> Any:
    last_error: Optional[BaseException] = None
    for attempt in range(_MAX_RETRIES):
        try:
            return await asyncio.to_thread(operation)
        except Exception as exc:
            last_error = exc
            if not _is_transient(exc) or attempt + 1 >= _MAX_RETRIES:
                raise _format_error(operation_name, exc) from exc
            await asyncio.sleep(0.75 * (2 ** attempt))
    raise _format_error(operation_name, last_error or RuntimeError("unknown error"))


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_created_at(value: Any) -> str:
    dt = _as_datetime(value)
    if dt is None:
        return datetime.now(timezone.utc).isoformat()
    return dt.astimezone(timezone.utc).isoformat()


def _entry_from_row(row: Dict[str, Any]) -> Tuple[str, str, str, Dict[str, Any]]:
    provider = str(row.get("provider") or "").strip()
    kind = str(row.get("kind") or "").strip()
    short_code = str(row.get("short_code") or "").strip()

    entry: Dict[str, Any] = {}
    if kind == "shorten":
        entry.update({
            "original_url": row.get("original_url"),
            "shortened_url": row.get("shortened_url"),
        })
    elif kind == "paste":
        entry.update({
            "title": row.get("title"),
            "language": row.get("language"),
            "paste_url": row.get("paste_url"),
            "raw_url": row.get("raw_url"),
        })
    elif kind == "file":
        entry.update({
            "original_filename": row.get("original_filename"),
            "content_type": row.get("content_type"),
            "size": row.get("size_bytes"),
            "file_url": row.get("file_url"),
        })
    else:
        raise ShortenedURLStoreError(f"Database returned unsupported record kind `{kind}`.")

    entry["id"] = row.get("id")
    entry["short_code"] = short_code
    entry["deletion_url"] = row.get("deletion_url")
    entry["creator_id"] = row.get("creator_id")
    entry["created_at"] = row.get("created_at")
    entry["updated_at"] = row.get("updated_at")

    metadata = row.get("metadata")
    entry["metadata"] = metadata if isinstance(metadata, dict) else {}
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if key not in entry:
                entry[key] = value

    return provider, kind, short_code, entry


def _row_from_entry(provider: str, kind: str, short_code: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    if kind not in _STATE_KINDS:
        raise ValueError(f"Unsupported shortened URL record kind: {kind!r}")
    if not provider or not short_code:
        raise ValueError("Provider and short_code are required")
    if not isinstance(entry, dict):
        raise ValueError("Shortened URL entry must be an object")

    row: Dict[str, Any] = {
        "provider": provider,
        "kind": kind,
        "short_code": short_code,
        "creator_id": str(entry.get("creator_id")).strip() if entry.get("creator_id") not in (None, "") else None,
        "created_at": _normalize_created_at(entry.get("created_at")),
        "deletion_url": entry.get("deletion_url"),
        "original_url": None,
        "shortened_url": None,
        "title": None,
        "language": None,
        "paste_url": None,
        "raw_url": None,
        "original_filename": None,
        "content_type": None,
        "size_bytes": None,
        "file_url": None,
        "metadata": {},
    }

    if kind == "shorten":
        row["original_url"] = entry.get("original_url")
        row["shortened_url"] = entry.get("shortened_url")
    elif kind == "paste":
        row["title"] = entry.get("title")
        row["language"] = entry.get("language")
        row["paste_url"] = entry.get("paste_url")
        row["raw_url"] = entry.get("raw_url")
    else:
        row["original_filename"] = entry.get("original_filename")
        row["content_type"] = entry.get("content_type")
        size = entry.get("size_bytes", entry.get("size"))
        if size not in (None, ""):
            try:
                row["size_bytes"] = int(size)
            except (TypeError, ValueError) as exc:
                raise ValueError("File size must be an integer") from exc
        row["file_url"] = entry.get("file_url")

    recognized = _COMMON_FIELDS | _KIND_FIELDS[kind]
    for key, value in entry.items():
        if key not in recognized:
            row["metadata"][key] = value

    return row


def _select_all_sync() -> List[Dict[str, Any]]:
    response = (
        _client_sync()
        .table("shortened_urls")
        .select("*")
        .order("id")
        .execute()
    )
    return response.data or []


async def fetch_all_shortened_urls() -> List[Tuple[str, str, str, Dict[str, Any]]]:
    rows = await _run(_select_all_sync, "read shortened URL records")
    return [_entry_from_row(row) for row in rows]


def _get_by_kind_sync(provider: str, kind: str) -> List[Dict[str, Any]]:
    return (
        _client_sync()
        .table("shortened_urls")
        .select("*")
        .eq("provider", provider)
        .eq("kind", kind)
        .order("id")
        .execute()
    ).data or []


async def get_shortened_urls(provider: str, kind: str) -> Dict[str, Any]:
    rows = await _run(lambda: _get_by_kind_sync(provider, kind), f"read {provider}/{kind} records")
    return {str(row["short_code"]): _entry_from_row(row)[3] for row in rows}


def _find_by_code_sync(short_code: str) -> List[Dict[str, Any]]:
    return (
        _client_sync()
        .table("shortened_urls")
        .select("*")
        .eq("short_code", short_code)
        .order("id")
        .execute()
    ).data or []


def _url_matches_entry(url: str, kind: str, row: Dict[str, Any]) -> bool:
    if not url:
        return False
    candidates = []
    if kind == "shorten":
        candidates.append(row.get("shortened_url"))
    elif kind == "paste":
        candidates.extend((row.get("paste_url"), row.get("raw_url")))
    else:
        candidates.append(row.get("file_url"))
    return any(candidate and str(candidate) == url for candidate in candidates)


async def find_shortened_url_entry(
    short_code: str,
    url: Optional[str] = None,
) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    rows = await _run(lambda: _find_by_code_sync(short_code), "find the shortened URL record")
    if not rows:
        return None

    if url:
        for row in rows:
            if _url_matches_entry(url, str(row.get("kind") or ""), row):
                provider, kind, _code, entry = _entry_from_row(row)
                return provider, kind, entry

    provider, kind, _code, entry = _entry_from_row(rows[0])
    return provider, kind, entry


def _upsert_sync(row: Dict[str, Any]) -> Dict[str, Any]:
    response = (
        _client_sync()
        .table("shortened_urls")
        .upsert(row, on_conflict="provider,kind,short_code")
        .execute()
    )
    data = response.data or []
    return data[0] if data else row


async def save_shortened_url(
    provider: str,
    kind: str,
    short_code: str,
    entry: Dict[str, Any],
) -> Dict[str, Any]:
    """Create or replace one persistent record in Supabase."""
    row = _row_from_entry(provider, kind, short_code, entry)
    stored = await _run(lambda: _upsert_sync(row), "save the shortened URL record")
    _provider, _kind, _code, result = _entry_from_row(stored)
    return result


def _delete_ids_sync(ids: List[int]) -> None:
    if not ids:
        return
    (
        _client_sync()
        .table("shortened_urls")
        .delete()
        .in_("id", ids)
        .execute()
    )


async def find_matching_shortened_urls(
    predicate: Callable[[Dict[str, Any], str, str], bool],
) -> List[Tuple[str, str, str, Dict[str, Any]]]:
    records = await fetch_all_shortened_urls()
    return [record for record in records if predicate(record[3], record[1], record[0])]


async def clear_shortened_urls(
    predicate: Callable[[Dict[str, Any], str, str], bool],
    message: Optional[str] = None,
) -> List[Tuple[str, str, str, Dict[str, Any]]]:
    """Delete every matching persistent record and return what was removed."""
    rows = await _run(_select_all_sync, "read shortened URL records before deletion")
    removed = []
    ids = []
    for row in rows:
        record = _entry_from_row(row)
        if predicate(record[3], record[1], record[0]):
            ids.append(int(row["id"]))
            removed.append(record)

    if ids:
        await _run(lambda: _delete_ids_sync(ids), "delete shortened URL records")
    return removed

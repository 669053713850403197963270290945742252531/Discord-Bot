"""One-time migration of storage/shortened-urls.json into Supabase."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv

# python scripts/migrate_shortened_urls_to_supabase.py storage\shortened-urls.json
KINDS = {"shorten", "paste", "file"}
COMMON_FIELDS = {"deletion_url", "creator_id", "created_at"}
KIND_FIELDS = {
    "shorten": {"original_url", "shortened_url"},
    "paste": {"title", "language", "paste_url", "raw_url"},
    "file": {"original_filename", "content_type", "size", "size_bytes", "file_url"},
}


def _normalize_created_at(value: Any) -> str:
    if value in (None, ""):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()
    text = str(value).strip()
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return text


def row_from_entry(provider: str, kind: str, short_code: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"Unsupported record kind: {kind!r}")
    if not provider or not short_code:
        raise ValueError("Provider and short_code are required")
    if not isinstance(entry, dict):
        raise ValueError("Record entry must be an object")

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
            row["size_bytes"] = int(size)
        row["file_url"] = entry.get("file_url")

    recognized = COMMON_FIELDS | KIND_FIELDS[kind]
    row["metadata"] = {
        key: value
        for key, value in entry.items()
        if key not in recognized
    }
    return row


def canonical(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "provider", "kind", "short_code", "creator_id", "created_at", "deletion_url",
            "original_url", "shortened_url", "title", "language", "paste_url", "raw_url",
            "original_filename", "content_type", "size_bytes", "file_url", "metadata",
        )
    }


def main() -> int:
    source_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("storage/shortened-urls.json")
    if not source_path.is_file():
        print(f"Migration source not found: {source_path}")
        return 1

    load_dotenv()
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    supabase_key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    if not supabase_url or not supabase_key:
        print("SUPABASE_URL and SUPABASE_SECRET_KEY must be configured.")
        return 1

    try:
        state = json.loads(source_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Failed to read {source_path}: {exc}")
        return 1

    if not isinstance(state, dict):
        print("Migration source must contain a top-level JSON object.")
        return 1

    try:
        from supabase import create_client
    except ImportError:
        print("The `supabase` Python package is not installed. Run `pip install -r requirements.txt` first.")
        return 1

    client = create_client(supabase_url, supabase_key)
    existing_rows = (
        client.table("shortened_urls")
        .select("*")
        .execute()
    ).data or []

    existing: Dict[Tuple[str, str, str], Dict[str, Any]] = {
        (str(row.get("provider")), str(row.get("kind")), str(row.get("short_code"))): row
        for row in existing_rows
    }

    source_seen: set[Tuple[str, str, str]] = set()
    insert_rows: List[Dict[str, Any]] = []
    duplicates_source: List[Tuple[str, str, str]] = []
    already_present: List[Tuple[str, str, str]] = []
    conflicts: List[Tuple[str, str, str]] = []
    invalid: List[str] = []
    counts = Counter()

    for provider, provider_state in state.items():
        if provider in {"schema_version", "last_updated"}:
            continue
        if not isinstance(provider_state, dict):
            invalid.append(f"{provider}: provider namespace is not an object")
            continue

        for kind, entries in provider_state.items():
            if kind not in KINDS:
                invalid.append(f"{provider}/{kind}: unsupported record kind")
                continue
            if not isinstance(entries, dict):
                invalid.append(f"{provider}/{kind}: entries are not an object")
                continue

            for short_code, entry in entries.items():
                key = (str(provider), str(kind), str(short_code))
                if key in source_seen:
                    duplicates_source.append(key)
                    continue
                source_seen.add(key)

                try:
                    row = row_from_entry(str(provider), str(kind), str(short_code), entry)
                except Exception as exc:
                    invalid.append(f"{provider}/{kind}/{short_code}: {exc}")
                    continue

                existing_row = existing.get(key)
                if existing_row is not None:
                    if canonical(existing_row) == canonical(row):
                        already_present.append(key)
                        continue
                    conflicts.append(key)
                    continue

                insert_rows.append(row)
                counts[kind] += 1

    print(f"Source: {source_path}")
    print(f"Source records discovered: {len(source_seen)}")
    print(f"Already present (identical): {len(already_present)}")
    print(f"Source duplicates: {len(duplicates_source)}")
    print(f"Database conflicts (same key, different data): {len(conflicts)}")
    print(f"Invalid records: {len(invalid)}")
    print(f"Ready to insert: {len(insert_rows)}")

    if invalid:
        print("\nInvalid records:")
        for item in invalid:
            print(f"  - {item}")

    if conflicts:
        print("\nConflicting database keys (skipped):")
        for provider, kind, code in conflicts:
            print(f"  - {provider}/{kind}/{code}")

    if duplicates_source:
        print("\nDuplicate source keys (skipped):")
        for provider, kind, code in duplicates_source:
            print(f"  - {provider}/{kind}/{code}")

    for start in range(0, len(insert_rows), 100):
        batch = insert_rows[start:start + 100]
        client.table("shortened_urls").insert(batch).execute()

    final_rows = client.table("shortened_urls").select("provider,kind,short_code").execute().data or []
    final_keys = {
        (str(row.get("provider")), str(row.get("kind")), str(row.get("short_code")))
        for row in final_rows
    }
    missing = sorted(key for key in source_seen if key not in final_keys)

    print("\nInserted by kind:")
    for kind in ("shorten", "paste", "file"):
        print(f"  {kind}: {counts[kind]}")

    print(f"Supabase shortened_urls rows after migration: {len(final_rows)}")
    print(f"Source records missing from Supabase after verification: {len(missing)}")

    if missing:
        print("Missing keys:")
        for provider, kind, code in missing:
            print(f"  - {provider}/{kind}/{code}")
        return 2

    if conflicts or invalid or duplicates_source:
        print("Migration completed with skipped/problem records; review the report above.")
    else:
        print("Migration completed successfully; all source records are present in Supabase.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

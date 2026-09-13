"""Supabase Storage access for protected game scripts.

The bucket is private. This module is server-only and uses the Supabase
secret key configured in the environment. The Roblox client never talks
directly to Supabase Storage.
"""

import asyncio
import json
import time
from functools import lru_cache
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from pathlib import PurePosixPath
from urllib.request import Request, urlopen

from supabase import Client, create_client

from . import config


class SupabaseStorageError(RuntimeError):
    """Raised when a protected game script cannot be retrieved or updated."""


@lru_cache(maxsize=1)
def _get_client() -> Client:
    try:
        return create_client(config.SUPABASE_URL, config.SUPABASE_SECRET_KEY)
    except Exception as exc:
        raise SupabaseStorageError("Failed to initialize Supabase client") from exc


def _validate_path(path: str) -> str:
    path = str(path or "").strip().lstrip("/")
    if not path or ".." in path.split("/"):
        raise SupabaseStorageError("Invalid Supabase Storage object path")
    return path


def _download_sync(path: str) -> bytes:
    path = _validate_path(path)

    try:
        data = _get_client().storage.from_(config.SUPABASE_GAME_SCRIPTS_BUCKET).download(path)
    except Exception as exc:
        raise SupabaseStorageError(
            f"Failed to download protected game script from Supabase Storage: {path!r}"
        ) from exc

    if not isinstance(data, (bytes, bytearray)) or not data:
        raise SupabaseStorageError("Supabase Storage returned an empty/invalid object")

    return bytes(data)


async def fetch_game_script_bytes(path: str) -> bytes:
    """Download a game script object as raw bytes from the private bucket."""
    try:
        return await asyncio.to_thread(_download_sync, path)
    except SupabaseStorageError:
        raise
    except Exception as exc:
        raise SupabaseStorageError("Unexpected Supabase Storage failure") from exc


async def fetch_game_script(path: str) -> str:
    """Download a UTF-8 Luau script from the private game-scripts bucket."""
    try:
        data = await asyncio.to_thread(_download_sync, path)
    except SupabaseStorageError:
        raise
    except Exception as exc:
        raise SupabaseStorageError("Unexpected Supabase Storage failure") from exc

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SupabaseStorageError("Protected game script is not valid UTF-8") from exc


def _list_sync(path: str = "") -> list[dict]:
    """List objects in the private game-scripts bucket under a folder."""
    path = str(path or "").strip().strip("/")
    try:
        options = {"limit": 1000, "offset": 0, "sortBy": {"column": "name", "order": "asc"}}
        data = _get_client().storage.from_(config.SUPABASE_GAME_SCRIPTS_BUCKET).list(path, options)
    except Exception as exc:
        raise SupabaseStorageError(
            f"Failed to list protected game scripts in Supabase Storage under {path or '<root>'!r}"
        ) from exc

    if not isinstance(data, list):
        raise SupabaseStorageError("Supabase Storage returned an invalid object listing")
    return [item for item in data if isinstance(item, dict)]


async def get_game_script_filename(path: str) -> str:
    """Return the filename of an object that exists at the supplied Storage path."""
    normalized = _validate_path(path)
    parent = str(PurePosixPath(normalized).parent)
    if parent == ".":
        parent = ""
    expected_name = PurePosixPath(normalized).name

    try:
        objects = await asyncio.to_thread(_list_sync, parent)
    except SupabaseStorageError:
        raise
    except Exception as exc:
        raise SupabaseStorageError("Unexpected Supabase Storage listing failure") from exc

    for item in objects:
        name = str(item.get("name") or "")
        if name == expected_name:
            return name

    raise SupabaseStorageError(
        f"Game script was not found in Supabase Storage at {normalized!r}"
    )


def _storage_upload_url(path: str) -> str:
    bucket = quote(config.SUPABASE_GAME_SCRIPTS_BUCKET, safe="")
    object_path = "/".join(quote(part, safe="") for part in path.split("/"))
    return f"{config.SUPABASE_URL.rstrip('/')}/storage/v1/object/{bucket}/{object_path}"


def _format_http_error(exc: HTTPError) -> str:
    try:
        raw = exc.read()
        body = raw.decode("utf-8", errors="replace").strip()
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                message = parsed.get("message") or parsed.get("error") or parsed.get("statusCode")
                if message:
                    return f"HTTP {exc.code}: {message}"
        except Exception:
            pass
        return f"HTTP {exc.code}: {body or exc.reason}"
    except Exception:
        return f"HTTP {exc.code}: {exc.reason}"


def _upload_sync(path: str, data: bytes) -> None:
    """Replace/create a Storage object using Supabase's documented x-upsert path.

    The Python Storage client has changed option/exception behavior across
    supabase-py/storage3 releases. For this administrative replacement path we
    use the Storage HTTP endpoint directly, which supports the documented
    `x-upsert: true` behavior for overwriting an existing object.
    """
    path = _validate_path(path)
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise SupabaseStorageError("Cannot upload an empty/invalid object")

    url = _storage_upload_url(path)
    payload = bytes(data)

    # Match the MIME types explicitly allowed by the game-scripts bucket.
    # In particular, .luau must be uploaded as text/x-luau rather than
    # application/octet-stream or text/plain.
    suffix = PurePosixPath(path).suffix.lower()
    mime_types = {
        ".luau": "text/x-luau",
        ".lua": "text/x-lua",
        ".json": "application/json",
    }
    content_type = mime_types.get(suffix)
    if content_type is None:
        raise SupabaseStorageError(
            f"Unsupported game script file type {suffix or '<none>'!r}; "
            "expected .luau, .lua, or .json"
        )

    headers = {
        "Authorization": f"Bearer {config.SUPABASE_SECRET_KEY}",
        "apikey": config.SUPABASE_SECRET_KEY,
        "x-upsert": "true",
        "Content-Type": content_type,
        "Content-Length": str(len(payload)),
    }

    last_error: str | None = None
    for attempt in range(3):
        request = Request(url, data=payload, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=30) as response:
                status = getattr(response, "status", 200)
                if 200 <= status < 300:
                    return
                last_error = f"HTTP {status} from Supabase Storage"
        except HTTPError as exc:
            last_error = _format_http_error(exc)
            # Retry only transient gateway/server/rate-limit failures.
            if exc.code not in (408, 425, 429, 500, 502, 503, 504):
                break
        except (URLError, TimeoutError) as exc:
            last_error = f"Network error: {exc}"
        except Exception as exc:
            last_error = f"Unexpected upload error: {exc}"

        if attempt < 2:
            time.sleep(0.75 * (2 ** attempt))

    raise SupabaseStorageError(
        f"Failed to upload/replace game script in Supabase Storage at {path!r}: {last_error or 'unknown error'}"
    )


async def upload_game_script(path: str, data: bytes) -> None:
    """Upload/replace an object in the private game-scripts bucket."""
    try:
        await asyncio.to_thread(_upload_sync, path, data)
    except SupabaseStorageError:
        raise
    except Exception as exc:
        raise SupabaseStorageError("Unexpected Supabase Storage upload failure") from exc

def _delete_sync(path: str) -> None:
    """Delete a game script object from the private Storage bucket."""
    path = _validate_path(path)
    url = _storage_upload_url(path)
    headers = {
        "Authorization": f"Bearer {config.SUPABASE_SECRET_KEY}",
        "apikey": config.SUPABASE_SECRET_KEY,
    }

    request = Request(url, headers=headers, method="DELETE")
    try:
        with urlopen(request, timeout=30) as response:
            status = getattr(response, "status", 200)
            if 200 <= status < 300:
                return
            raise SupabaseStorageError(
                f"HTTP {status} from Supabase Storage while deleting {path!r}"
            )
    except HTTPError as exc:
        # DELETE is intentionally unchanged for this command:
        # if the object is already absent, the desired end state is satisfied.
        if exc.code in (404,):
            return
        raise SupabaseStorageError(
            f"Failed to delete game script from Supabase Storage at {path!r}: {_format_http_error(exc)}"
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise SupabaseStorageError(
            f"Network error while deleting game script at {path!r}: {exc}"
        ) from exc
    except SupabaseStorageError:
        raise
    except Exception as exc:
        raise SupabaseStorageError(
            f"Unexpected error while deleting game script at {path!r}: {exc}"
        ) from exc


async def delete_game_script(path: str) -> None:
    """Delete an object from the private game-scripts bucket."""
    try:
        await asyncio.to_thread(_delete_sync, path)
    except SupabaseStorageError:
        raise
    except Exception as exc:
        raise SupabaseStorageError("Unexpected Supabase Storage deletion failure") from exc


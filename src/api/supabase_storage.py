"""Supabase Storage access for protected game scripts.

The bucket is private. This module is server-only and uses the Supabase
secret key configured in the environment. The Roblox client never talks
directly to Supabase Storage.
"""

import asyncio
from functools import lru_cache
from typing import Any

from supabase import Client, create_client

from . import config


class SupabaseStorageError(RuntimeError):
    """Raised when a protected game script cannot be retrieved."""


@lru_cache(maxsize=1)
def _get_client() -> Client:
    try:
        return create_client(config.SUPABASE_URL, config.SUPABASE_SECRET_KEY)
    except Exception as exc:
        raise SupabaseStorageError("Failed to initialize Supabase client") from exc


def _download_sync(path: str) -> bytes:
    path = path.lstrip("/")
    if not path or ".." in path.split("/"):
        raise SupabaseStorageError("Invalid Supabase Storage object path")

    try:
        data = _get_client().storage.from_(config.SUPABASE_GAME_SCRIPTS_BUCKET).download(path)
    except Exception as exc:
        raise SupabaseStorageError(
            f"Failed to download protected game script from Supabase Storage: {path!r}"
        ) from exc

    if not isinstance(data, (bytes, bytearray)) or not data:
        raise SupabaseStorageError("Supabase Storage returned an empty/invalid object")

    return bytes(data)


async def fetch_game_script(path: str) -> str:
    """Download a UTF-8 Luau script from the private game-scripts bucket.

    supabase-py exposes Storage as a synchronous client, so the download is
    moved to a worker thread to avoid blocking Flask's request thread if the
    object store is slow.
    """
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

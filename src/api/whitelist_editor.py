"""Short-lived authenticated sessions and helpers for the web whitelist editor.

The editor launch link is a short-lived capability issued only after the bot's
existing role/guild checks pass. The fragment token is exchanged by a small
non-editor gate page for a Discord OAuth authorization-code + PKCE flow. The
editor HTML and APIs are available only behind a server-side session that is
rechecked against current guild membership and the live license record.
"""

import asyncio
import hashlib
import json
import secrets
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

EDITOR_LAUNCH_TTL_SECONDS = 13 * 60
EDITOR_SESSION_TTL_SECONDS = 60 * 60
MAX_EDITOR_JSON_BYTES = 8 * 1024 * 1024
_EDITOR_SESSIONS: Dict[str, Dict[str, Any]] = {}
_EDITOR_SESSIONS_LOCK = threading.RLock()
_EDITOR_SAVE_LOCK = threading.Lock()

# Accept the exact fields emitted by /whitelist export plus their database-style
# snake_case equivalents for compatibility with prior JSON exports.
FIELD_ALIASES = {
    "Identifier": ("Identifier", "identifier"),
    "DiscordId": ("DiscordId", "discord_id"),
    "Key": ("Key", "LicenseKey", "license_key", "key"),
    "Activated": ("Activated", "activated"),
    "Executions": ("Executions", "executions"),
    "Rank": ("Rank", "rank"),
    "Notes": ("Notes", "notes"),
    "Games": ("Games", "games"),
    "Enabled": ("Enabled", "enabled"),
    "ExpiresAt": ("ExpiresAt", "expires_at"),
    "CreatedAt": ("CreatedAt", "created_at"),
    "UpdatedAt": ("UpdatedAt", "updated_at"),
    "HWID": ("HWID", "hwid"),
    "LastHwidReset": ("LastHwidReset", "LastHWIDReset", "last_hwid_reset"),
    "totalHwidResets": ("totalHwidResets", "HwidResets", "HWIDResets", "hwid_resets"),
    "ActivationCountry": ("ActivationCountry", "activation_country_code", "activation_country"),
    "ActivationRobloxUserId": ("ActivationRobloxUserId", "activation_roblox_user_id"),
}
KNOWN_INPUT_FIELDS = {name for aliases in FIELD_ALIASES.values() for name in aliases}
SEMANTIC_FIELDS = (
    "Identifier", "DiscordId", "Key", "Activated", "Executions", "Rank", "Notes",
    "Games", "Enabled", "ExpiresAt", "HWID", "LastHwidReset", "totalHwidResets",
    "ActivationCountry", "ActivationRobloxUserId",
)


def create_whitelist_editor_session(interaction, base_url: str) -> Tuple[str, str]:
    """Create an expiring editor token tied to an authorized Discord interaction."""
    loop = asyncio.get_running_loop()
    token = secrets.token_urlsafe(36)
    editor_url = f"{str(base_url).rstrip('/')}/editwhitelist#session={token}"
    now = time.time()
    session = {
        "token": token,
        "created_at": now,
        # The launch URL is valid only briefly, but the server-side interaction
        # context must outlive OAuth and the authenticated editing session.
        "launch_expires_at": now + EDITOR_LAUNCH_TTL_SECONDS,
        "expires_at": now + EDITOR_SESSION_TTL_SECONDS,
        "interaction": interaction,
        "bot": interaction.client,
        "loop": loop,
        "user_id": int(interaction.user.id),
        "user_name": str(interaction.user),
        "editor_url": editor_url,
        # Once closed, this original Discord response is terminal: save handlers
        # must not replace it with a later status view that re-exposes the URL.
        "launch_closed": False,
        "launch_closed_reason": None,
        "launch_view_lock": asyncio.Lock(),
    }
    with _EDITOR_SESSIONS_LOCK:
        for old_token, old_session in list(_EDITOR_SESSIONS.items()):
            if old_session.get("expires_at", 0) <= now:
                _EDITOR_SESSIONS.pop(old_token, None)
        _EDITOR_SESSIONS[token] = session
    session["launch_expiry_task"] = loop.create_task(_expire_editor_launch(token))
    return token, editor_url


def get_whitelist_editor_session(
    token: Optional[str], *, require_launch_valid: bool = False
) -> Optional[Dict[str, Any]]:
    """Return a live interaction context, optionally requiring an unused launch URL.

    The URL's 13-minute lifetime is separate from the longer-lived server-side
    context needed after OAuth succeeds. Callers exchanging a Discord launch
    token must pass ``require_launch_valid=True``; authenticated-session checks
    only need the overall context lifetime.
    """
    if not isinstance(token, str) or not token or len(token) > 128:
        return None
    now = time.time()
    with _EDITOR_SESSIONS_LOCK:
        session = _EDITOR_SESSIONS.get(token)
        if not session:
            return None
        if session.get("expires_at", 0) <= now:
            _EDITOR_SESSIONS.pop(token, None)
            return None
        if require_launch_valid and (
            session.get("oauth_completed")
            or session.get("launch_expires_at", session.get("created_at", now) + EDITOR_LAUNCH_TTL_SECONDS) <= now
        ):
            return None
        return session


async def _expire_editor_launch(token: str):
    """Close an unused launch URL when its public lifetime elapses."""
    await asyncio.sleep(EDITOR_LAUNCH_TTL_SECONDS)
    session = get_whitelist_editor_session(token)
    if session is None:
        return
    if not session.get("oauth_completed"):
        await close_whitelist_editor_launch(session, "expired")


async def close_whitelist_editor_launch(session: Dict[str, Any], reason: str) -> bool:
    """Set the original Discord response to a terminal closed state exactly once.

    All original-response updates share a per-session async lock, so a save that
    began around the same time cannot overwrite the closed view afterward.
    """
    lock = session.get("launch_view_lock")
    if lock is None:
        lock = asyncio.Lock()
        session["launch_view_lock"] = lock

    async with lock:
        if session.get("launch_closed"):
            return False
        session["launch_closed"] = True
        session["launch_closed_reason"] = reason
        expiry_task = session.get("launch_expiry_task")
        if expiry_task is not None and expiry_task is not asyncio.current_task() and not expiry_task.done():
            expiry_task.cancel()
        try:
            await session["interaction"].edit_original_response(
                view=build_editor_closed_view(reason)
            )
            return True
        except Exception as exc:
            # Keep this state terminal even if Discord is temporarily unavailable;
            # otherwise a later save could unexpectedly reopen the launch message.
            print(f"[Whitelist Editor] Could not update the closed launch message: {exc}")
            return False


def run_on_editor_bot_loop(session: Dict[str, Any], awaitable, *, timeout: float = 60):
    """Run an async DB/Discord operation on the bot's event loop from Flask."""
    loop = session.get("loop")
    if loop is None or not loop.is_running():
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise RuntimeError("The bot event loop is unavailable. Please retry after the bot reconnects.")
    future = asyncio.run_coroutine_threadsafe(awaitable, loop)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError as exc:
        future.cancel()
        raise RuntimeError("The operation timed out. Refresh the editor and check the database before retrying.") from exc


def database_revision(users: List[Dict[str, Any]]) -> str:
    """Hash a stable ordering of all exported values to protect against stale saves."""
    normalized = sorted(
        users or [],
        key=lambda record: str((record or {}).get("Identifier") or "").casefold(),
    )
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _alias_value(row: Dict[str, Any], field: str):
    aliases = FIELD_ALIASES[field]
    present = [key for key in aliases if key in row]
    if not present:
        raise ValueError(f"missing required field `{field}`")
    if len(present) > 1:
        values = [row[key] for key in present]
        if any(value != values[0] for value in values[1:]):
            raise ValueError(f"field `{field}` has conflicting aliases: {', '.join(present)}")
    return row[present[0]]


def _is_timestamp(value: Any) -> bool:
    if value in (None, ""):
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def validate_and_normalize_records(value: Any) -> List[Dict[str, Any]]:
    """Validate JSON whitelist records strictly before any database writes."""
    if not isinstance(value, list):
        raise ValueError("The JSON root must be an array of license objects.")

    normalized_records: List[Dict[str, Any]] = []
    seen_identifiers = set()
    seen_discord_ids = set()
    seen_keys = set()

    # Import lazily to avoid loading Supabase until a valid edit reaches the API.
    from api.supabase_db import record_from_import_row

    for index, raw in enumerate(value, start=1):
        prefix = f"Entry {index}"
        if not isinstance(raw, dict):
            raise ValueError(f"{prefix} must be a JSON object.")
        unknown = sorted(set(raw) - KNOWN_INPUT_FIELDS)
        if unknown:
            raise ValueError(f"{prefix} contains unknown field(s): {', '.join(unknown[:8])}.")

        canonical = {}
        for field in FIELD_ALIASES:
            try:
                canonical[field] = _alias_value(raw, field)
            except ValueError as exc:
                raise ValueError(f"{prefix} {exc}.") from exc

        identifier = canonical["Identifier"]
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError(f"{prefix}: `Identifier` must be a non-empty string.")
        identifier = identifier.strip()
        identifier_key = identifier.casefold()
        if identifier_key in seen_identifiers:
            raise ValueError(f"Duplicate Identifier `{identifier}` (case-insensitive).")
        seen_identifiers.add(identifier_key)

        discord_id = canonical["DiscordId"]
        if discord_id not in (None, ""):
            if not isinstance(discord_id, str) or not discord_id.isdigit():
                raise ValueError(f"{prefix}: `DiscordId` must be a quoted numeric Discord ID or null.")
            if discord_id in seen_discord_ids:
                raise ValueError(f"Duplicate DiscordId `{discord_id}`.")
            seen_discord_ids.add(discord_id)

        key = canonical["Key"]
        if key not in (None, ""):
            if not isinstance(key, str):
                raise ValueError(f"{prefix}: `Key` must be a string or null.")
            if key in seen_keys:
                raise ValueError("Duplicate license `Key` value.")
            seen_keys.add(key)

        if canonical["Rank"] is not None and (not isinstance(canonical["Rank"], str) or not canonical["Rank"].strip()):
            raise ValueError(f"{prefix}: `Rank` must be a non-empty string.")
        if canonical["Notes"] is not None and not isinstance(canonical["Notes"], str):
            raise ValueError(f"{prefix}: `Notes` must be a string or null.")
        if not isinstance(canonical["Enabled"], bool):
            raise ValueError(f"{prefix}: `Enabled` must be true or false.")

        for field in ("Activated", "ExpiresAt", "CreatedAt", "UpdatedAt", "LastHwidReset"):
            if not _is_timestamp(canonical[field]):
                raise ValueError(f"{prefix}: `{field}` must be an ISO-8601 timestamp or null.")

        for field in ("Executions", "totalHwidResets"):
            item = canonical[field]
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError(f"{prefix}: `{field}` must be a non-negative integer.")

        games = canonical["Games"]
        if isinstance(games, str):
            game_items = [piece.strip() for piece in games.split(",") if piece.strip()]
        elif isinstance(games, list) and all(isinstance(item, str) for item in games):
            game_items = [item.strip() for item in games if item.strip()]
        else:
            raise ValueError(f"{prefix}: `Games` must be an array of strings or a comma-separated string.")
        if "*" in game_items:
            game_items = ["*"]
        canonical["Games"] = list(dict.fromkeys(game_items))

        for field in ("HWID", "ActivationCountry"):
            item = canonical[field]
            if item not in (None, "") and not isinstance(item, str):
                raise ValueError(f"{prefix}: `{field}` must be a string or null.")

        roblox_id = canonical["ActivationRobloxUserId"]
        if roblox_id not in (None, "") and (isinstance(roblox_id, bool) or not isinstance(roblox_id, int) or roblox_id < 0):
            raise ValueError(f"{prefix}: `ActivationRobloxUserId` must be a non-negative integer or null.")

        try:
            record = record_from_import_row(canonical)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{prefix}: {exc}") from exc
        normalized_records.append(record)

    return normalized_records


def _semantic_record(record: Dict[str, Any]) -> Dict[str, Any]:
    result = {}
    for field in SEMANTIC_FIELDS:
        value = record.get(field)
        if field == "Games":
            games = [str(game).strip() for game in (value or []) if str(game).strip()]
            value = ["*"] if "*" in games else sorted(set(games))
        elif field == "Enabled":
            value = bool(value)
        elif field in {"Executions", "totalHwidResets"}:
            value = int(value or 0)
        elif isinstance(value, str) and not value.strip():
            value = None
        result[field] = value
    return result


def _match_records(before: List[Dict[str, Any]], after: List[Dict[str, Any]]):
    old_by_identifier = {str(r.get("Identifier") or "").casefold(): i for i, r in enumerate(before)}
    old_by_discord = {str(r.get("DiscordId")): i for i, r in enumerate(before) if r.get("DiscordId") not in (None, "")}
    old_by_key = {str(r.get("Key")): i for i, r in enumerate(before) if r.get("Key") not in (None, "")}
    used_old = set()
    pairs = []
    added = []

    for new_index, record in enumerate(after):
        candidates = [
            old_by_identifier.get(str(record.get("Identifier") or "").casefold()),
            old_by_discord.get(str(record.get("DiscordId"))) if record.get("DiscordId") not in (None, "") else None,
            old_by_key.get(str(record.get("Key"))) if record.get("Key") not in (None, "") else None,
        ]
        match_index = next((candidate for candidate in candidates if candidate is not None and candidate not in used_old), None)
        if match_index is None:
            added.append(record)
            continue
        used_old.add(match_index)
        pairs.append((match_index, new_index, before[match_index], record))

    removed = [record for index, record in enumerate(before) if index not in used_old]
    modified = []
    for old_index, new_index, old_record, new_record in pairs:
        old_values = _semantic_record(old_record)
        new_values = _semantic_record(new_record)
        changes = {
            field: {"before": old_values[field], "after": new_values[field]}
            for field in SEMANTIC_FIELDS
            if old_values[field] != new_values[field]
        }
        if changes:
            modified.append({
                "Identifier": str(new_record.get("Identifier") or old_record.get("Identifier") or "Unknown"),
                "PreviousIdentifier": str(old_record.get("Identifier") or "Unknown"),
                "Changes": changes,
            })

    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "pairs": pairs,
    }


def _prepare_server_timestamps(before: List[Dict[str, Any]], after: List[Dict[str, Any]], diff: Dict[str, Any]) -> None:
    """Preserve creation times and update UpdatedAt only on actual user edits."""
    now = datetime.now(timezone.utc).isoformat()
    modified_indices = {item["Identifier"].casefold() for item in diff["modified"]}
    matched_new_indices = set()
    for old_index, new_index, old_record, new_record in diff["pairs"]:
        matched_new_indices.add(new_index)
        new_record["CreatedAt"] = old_record.get("CreatedAt")
        identifier_key = str(new_record.get("Identifier") or "").casefold()
        if identifier_key in modified_indices:
            new_record["UpdatedAt"] = now
        else:
            new_record["UpdatedAt"] = old_record.get("UpdatedAt")
    for index, record in enumerate(after):
        if index in matched_new_indices:
            continue
        # Creation/update timestamps for genuinely new users are generated by
        # the server, not copied from another user's JSON row.
        record["CreatedAt"] = now
        record["UpdatedAt"] = now


def diff_to_text(diff: Dict[str, Any]) -> str:
    payload = {
        "added": diff["added"],
        "removed": diff["removed"],
        "modified": diff["modified"],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def summary_counts(diff: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "added_count": len(diff.get("added") or []),
        "removed_count": len(diff.get("removed") or []),
        "modified_count": len(diff.get("modified") or []),
        "added": [str(row.get("Identifier") or "Unknown") for row in diff.get("added") or []],
        "removed": [str(row.get("Identifier") or "Unknown") for row in diff.get("removed") or []],
        "modified": [
            {
                "identifier": item["Identifier"],
                "previous_identifier": item.get("PreviousIdentifier"),
                "fields": list((item.get("Changes") or {}).keys()),
                "changes": [
                    {
                        "field": field,
                        "before": values.get("before"),
                        "after": values.get("after"),
                    }
                    for field, values in (item.get("Changes") or {}).items()
                ],
            }
            for item in diff.get("modified") or []
        ],
    }


def build_editor_closed_view(reason: str = "used"):
    """Render the terminal state for a launch URL that can no longer be used."""
    import discord
    from discord.ui import LayoutView, Container, TextDisplay

    messages = {
        "used": (
            "This one-time launch URL has been used to authenticate and is now closed. "
            "It cannot be reused."
        ),
        "expired": (
            "This launch URL has expired and can no longer be used."
        ),
        "denied": (
            "Discord sign-in was denied or could not be completed, so this launch session is closed."
        ),
        "invalidated": (
            "This launch session was invalidated and can no longer be used."
        ),
        "session_expired": (
            "The authenticated editing session has expired or been invalidated."
        ),
    }
    explanation = messages.get(reason, "This launch session is closed and can no longer be used.")
    view = LayoutView(timeout=None)
    view.add_item(Container(
        TextDisplay("# 🛠️ Celestial Whitelist Editor · Launch URL Closed"),
        TextDisplay(
            f"🔒 **{explanation}**\n\n"
            "To open the editor again, run `/whitelist edit` in Discord to generate a fresh launch URL."
        ),
        TextDisplay("-# Celestial • Whitelist Administration"),
        accent_color=discord.Color.orange(),
    ))
    return view


def build_editor_initial_view(editor_url: str):
    """Create the initial Components V2 response with its link inside the container."""
    import discord
    from discord.ui import LayoutView, Container, TextDisplay, ActionRow, Button

    view = LayoutView(timeout=None)
    view.add_item(Container(
        TextDisplay(
            "# 🛠️ Celestial Whitelist Editor\n"
            "Edit the live whitelist database in a secure browser editor with JSON/CSV support, validation, and detailed change previews."
        ),
        TextDisplay(
            "### ✅ Live database integration · Stage 5\n"
            "• Automatically loads the current `licenses` database when opened\n"
            "• Validates records before writing changes\n"
            "• **Save Changes** updates the database and reports added, modified, and removed users\n"
            "• **Refresh** reloads the latest database data"
        ),
        TextDisplay(
            "### 🔒 Secure editor session\n"
            "Discord OAuth is required. Access is granted only to the same Discord account that opened this link when it is currently in the Celestial server and has an enabled, unexpired `Admin` or `Owner` license. The one-time launch link expires after about 13 minutes; after sign-in, the authenticated editing session lasts up to 30 minutes. Run `/whitelist edit` again if the launch link expires."
        ),
        TextDisplay("-# Celestial • Whitelist Administration"),
        ActionRow(Button(label="Open Whitelist Editor", emoji="🌐", style=discord.ButtonStyle.link, url=editor_url)),
        accent_color=discord.Color.blurple(),
    ))
    return view


def build_editor_status_view(session: Dict[str, Any], summary: Dict[str, Any]):
    """Build the replacement Components V2 status response after Save Changes."""
    import discord
    from discord.ui import LayoutView, Container, TextDisplay, ActionRow, Button

    def escaped(value: Any) -> str:
        return discord.utils.escape_markdown(str(value or "Unknown"))

    def value_text(value: Any, limit: int = 110) -> str:
        if value is None:
            text = "null"
        elif isinstance(value, str):
            text = value.replace("\r", "\\r").replace("\n", "\\n")
        else:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if text == "":
            text = '""'
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        return f"`{discord.utils.escape_markdown(text)}`"

    def format_names(title: str, names: List[str]) -> str:
        shown = names[:15]
        lines = [f"• `{escaped(name)}`" for name in shown]
        if len(names) > len(shown):
            lines.append(f"• …and {len(names) - len(shown)} more")
        return f"### {title} ({len(names)})\n" + "\n".join(lines)

    pieces = [TextDisplay("# ✅ Celestial Whitelist Editor · Save Status")]
    counts = []
    for key, label in (("added_count", "added"), ("modified_count", "modified"), ("removed_count", "removed")):
        count = int(summary.get(key) or 0)
        if count:
            counts.append(f"**{count} {label}**")
    if counts:
        pieces.append(TextDisplay("The `licenses` database was successfully updated.\n" + " · ".join(counts) + "."))
    else:
        pieces.append(TextDisplay("✅ **No changes detected.** The submitted JSON already matched the database, so no database write was needed."))

    if summary.get("added_count"):
        pieces.append(TextDisplay(format_names("Added users", summary.get("added") or [])))

    if summary.get("modified_count"):
        lines = []
        modified = summary.get("modified") or []
        for item in modified[:15]:
            name = escaped(item.get("identifier"))
            previous = item.get("previous_identifier")
            renamed = f" (was `{escaped(previous)}`)" if previous and previous != item.get("identifier") else ""
            lines.append(f"• **`{name}`**{renamed}")
            changes = item.get("changes") or []
            for change in changes[:8]:
                field = escaped(change.get("field") or "Field")
                before = value_text(change.get("before"))
                after = value_text(change.get("after"))
                lines.append(f"  • **{field}:** {before} → {after}")
            if len(changes) > 8:
                lines.append(f"  • …and {len(changes) - 8} more changed fields")
        if len(modified) > 15:
            lines.append(f"• …and {len(modified) - 15} more modified users")

        # Split long diffs into valid-sized TextDisplay blocks, keeping the
        # Components V2 message below Discord's per-component content limit.
        chunks = []
        chunk_lines = []
        chunk_size = 0
        for line in lines:
            line_size = len(line) + 1
            if chunk_lines and chunk_size + line_size > 3200:
                chunks.append("\n".join(chunk_lines))
                chunk_lines = []
                chunk_size = 0
            chunk_lines.append(line)
            chunk_size += line_size
        if chunk_lines:
            chunks.append("\n".join(chunk_lines))
        for index, chunk in enumerate(chunks):
            heading = f"### Modified users ({len(modified)})\n" if index == 0 else "### Modified users (continued)\n"
            pieces.append(TextDisplay(heading + chunk))

    if summary.get("removed_count"):
        pieces.append(TextDisplay(format_names("Removed users", summary.get("removed") or [])))

    pieces.append(TextDisplay("-# Database changes were applied through the authorized Celestial web editor."))
    pieces.append(ActionRow(Button(
        label="Open Whitelist Editor",
        emoji="🌐",
        style=discord.ButtonStyle.link,
        url=session["editor_url"],
    )))
    view = LayoutView(timeout=None)
    view.add_item(Container(*pieces, accent_color=discord.Color.green() if counts else discord.Color.blurple()))
    return view


async def finalize_editor_save(session: Dict[str, Any], before, after, diff: Dict[str, Any], diff_text: str) -> bool:
    """Best-effort update the original /whitelist edit response and post a staff diff alert."""
    summary = summary_counts(diff)
    if summary["added_count"] or summary["removed_count"] or summary["modified_count"]:
        try:
            from api.alerts import LicenseDatabaseDiffView, send_component_alert
            actor_mention = f"<@{session['user_id']}>"
            counts = []
            if summary["added_count"]:
                counts.append(f"**Added:** {summary['added_count']}")
            if summary["modified_count"]:
                counts.append(f"**Modified:** {summary['modified_count']}")
            if summary["removed_count"]:
                counts.append(f"**Removed:** {summary['removed_count']}")
            description = f"{actor_mention} updated the whitelist database using the website editor.\n" + " · ".join(counts)
            view = LicenseDatabaseDiffView(description, diff_text, filename="whitelist-editor-diff.json")
            await send_component_alert(session["bot"], view)
        except Exception as exc:
            print(f"[Whitelist Editor] Database save succeeded but staff diff alert failed: {exc}")

    lock = session.get("launch_view_lock")
    try:
        if lock is None:
            lock = asyncio.Lock()
            session["launch_view_lock"] = lock
        async with lock:
            if session.get("launch_closed"):
                # Launch URL closure is terminal. Never replace it with a save
                # status or another view after the browser has consumed/expired it.
                return True
            await session["interaction"].edit_original_response(view=build_editor_status_view(session, summary))
        return True
    except Exception as exc:
        # The save is already committed. Interaction-webhook expiration or an
        # occasional Discord outage must not make the browser report failure.
        print(f"[Whitelist Editor] Database save succeeded but the original Discord response could not be updated: {exc}")
        return False

"""Small Flask server for keep-alive, website, and licensing endpoints.

- `/` -- plain keep-alive endpoint.
- `/client` -- serves the public Potassium license loader.
- `/editwhitelist` -- authorized live JSON editor for the Supabase whitelist.
- `/whitelist/*` -- public license challenge, check, heartbeat, breach, and
  completion endpoints.

There is intentionally no client-shared secret. A public Roblox script cannot
keep a secret from the user executing it. HTTPS, one-use challenges, rate
limits, server-side Supabase access, game restrictions, and payload withholding
are the security boundaries.
"""

import hashlib
import hmac
import json
import os
from threading import Thread

from flask import Flask, request, jsonify, Response
from api import config

app = Flask('')


@app.route('/')
def home():
    return "Bot is alive!", 200


@app.route('/editwhitelist', methods=['GET'])
@app.route('/editwhitelist/', methods=['GET'])
def whitelist_editor_page():
    """Serve the editor shell; live data is exposed only through token-authenticated API routes."""
    from pathlib import Path

    page_path = Path(__file__).resolve().parent / "templates" / "editwhitelist.html"
    try:
        page = page_path.read_text(encoding="utf-8")
    except OSError:
        return "Whitelist editor unavailable", 503, {"Content-Type": "text/plain; charset=utf-8"}

    response = Response(
        page,
        status=200,
        mimetype="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'none'; "
                "script-src https://cdnjs.cloudflare.com 'unsafe-inline'; "
                "style-src https://cdnjs.cloudflare.com 'unsafe-inline'; "
                "font-src https://cdnjs.cloudflare.com data:; "
                "img-src data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'"
            ),
        },
    )
    return response


def _editor_api_json(payload, status=200):
    """Return a non-cacheable JSON response for the editor APIs."""
    response = Response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        status=status,
        mimetype="application/json",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )
    return response


def _authorized_editor_session():
    from api.whitelist_editor import get_whitelist_editor_session

    token = request.headers.get("X-Editor-Session", "")
    session = get_whitelist_editor_session(token)
    return session


@app.route('/editwhitelist/api/data', methods=['GET'])
def whitelist_editor_load_data():
    """Load the current full licenses dataset for an authorized editor session."""
    session = _authorized_editor_session()
    if session is None:
        return _editor_api_json({"ok": False, "error": "Editor session expired or invalid. Run `/whitelist edit` again to open a new session."}, 401)

    try:
        from api.supabase_db import fetch_users
        from api.whitelist_editor import run_on_editor_bot_loop, database_revision

        users = run_on_editor_bot_loop(session, fetch_users())
        return _editor_api_json({"ok": True, "data": users, "revision": database_revision(users)})
    except Exception as exc:
        print(f"[Whitelist Editor] Failed to load the license database: {exc}")
        return _editor_api_json({"ok": False, "error": "Failed to load the live whitelist database. Check the bot logs and retry."}, 500)


@app.route('/editwhitelist/api/save', methods=['POST'])
def whitelist_editor_save_data():
    """Validate and apply an edited full licenses dataset, then update the originating Discord response."""
    session = _authorized_editor_session()
    if session is None:
        return _editor_api_json({"ok": False, "error": "Editor session expired or invalid. Run `/whitelist edit` again to open a new session."}, 401)

    from api.whitelist_editor import (
        MAX_EDITOR_JSON_BYTES,
        _EDITOR_SAVE_LOCK,
        validate_and_normalize_records,
        run_on_editor_bot_loop,
        database_revision,
        _match_records,
        _prepare_server_timestamps,
        summary_counts,
        diff_to_text,
        finalize_editor_save,
    )

    if request.content_length is not None and request.content_length > MAX_EDITOR_JSON_BYTES + 65536:
        return _editor_api_json({"ok": False, "error": "The editor payload is too large (maximum 8 MiB)."}, 413)

    request_data = request.get_json(silent=True)
    if not isinstance(request_data, dict):
        return _editor_api_json({"ok": False, "error": "Expected a JSON request body."}, 400)
    source = request_data.get("source")
    expected_revision = request_data.get("revision")
    if not isinstance(source, str):
        return _editor_api_json({"ok": False, "error": "Missing editor JSON source."}, 400)
    if len(source.encode("utf-8")) > MAX_EDITOR_JSON_BYTES:
        return _editor_api_json({"ok": False, "error": "The whitelist JSON exceeds the 8 MiB limit."}, 413)

    try:
        parsed = json.loads(source)
    except json.JSONDecodeError as exc:
        return _editor_api_json({
            "ok": False,
            "error": f"Invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno}).",
        }, 400)

    try:
        records = validate_and_normalize_records(parsed)
    except ValueError as exc:
        return _editor_api_json({"ok": False, "error": str(exc)}, 400)

    # Prevent two web editors in this process from applying stale full-dataset
    # writes at the same time. The revision comparison also catches ordinary
    # changes made through the Discord commands while this page was open.
    try:
        with _EDITOR_SAVE_LOCK:
            from api.supabase_db import fetch_users, commit_users
            current_users = run_on_editor_bot_loop(session, fetch_users())
            current_revision = database_revision(current_users)
            if not isinstance(expected_revision, str) or not hmac.compare_digest(expected_revision, current_revision):
                return _editor_api_json({
                    "ok": False,
                    "error": "The whitelist database changed after you opened or refreshed this editor. Click Refresh to load the latest database before saving again.",
                    "code": "database_changed",
                }, 409)

            if current_users and not records:
                return _editor_api_json({
                    "ok": False,
                    "error": "Saving an empty array would delete every license. This editor blocks that accidental full-database wipe; keep at least one record and remove users individually.",
                }, 400)

            diff = _match_records(current_users, records)
            protected_fields = {"ActivationCountry", "ActivationRobloxUserId"}
            for item in diff["modified"]:
                changed_fields = protected_fields.intersection((item.get("Changes") or {}).keys())
                if changed_fields:
                    names = ", ".join(sorted(changed_fields))
                    return _editor_api_json({
                        "ok": False,
                        "error": f"System-generated activation history cannot be edited from this page ({names}). Leave those values unchanged.",
                    }, 400)
            if not diff["added"] and not diff["removed"] and not diff["modified"]:
                after_users = current_users
                final_diff = diff
            else:
                _prepare_server_timestamps(current_users, records, diff)
                after_users = run_on_editor_bot_loop(
                    session,
                    commit_users(
                        records,
                        None,
                        f"Web whitelist edit by {session['user_name']}",
                        preserve_empty_games=True,
                    ),
                )
                final_diff = _match_records(current_users, after_users)

            summary = summary_counts(final_diff)
            current_revision = database_revision(after_users)
            diff_text = diff_to_text(final_diff)
            discord_response_updated = False
            try:
                discord_response_updated = bool(run_on_editor_bot_loop(
                    session,
                    finalize_editor_save(session, current_users, after_users, final_diff, diff_text),
                    timeout=35,
                ))
            except Exception as exc:
                # A Discord status/alert failure must not turn a successful
                # Supabase write into a browser-visible database failure.
                print(f"[Whitelist Editor] Save succeeded, but Discord status update failed: {exc}")

        return _editor_api_json({
            "ok": True,
            "data": after_users,
            "revision": current_revision,
            "summary": summary,
            "discord_response_updated": discord_response_updated,
            "message": "Database updated successfully." if (summary["added_count"] or summary["removed_count"] or summary["modified_count"]) else "No changes were necessary; the database already matches the editor.",
        })
    except ValueError as exc:
        return _editor_api_json({"ok": False, "error": str(exc)}, 400)
    except Exception as exc:
        print(f"[Whitelist Editor] Failed to save the license database: {exc}")
        return _editor_api_json({"ok": False, "error": "The database update failed. Check the bot logs before retrying; refresh the editor to confirm the current database state."}, 500)


@app.route('/client', methods=['GET'])
def public_license_client():
    """Serves the public loader. It contains no server secret; the API base
    is substituted from the current Render/external URL so the same client
    source can be distributed through `game:HttpGet(...)` without exposing
    deployment configuration in the file itself."""
    from pathlib import Path
    # Preserve the scheme used to fetch /client for local HTTP development.
    # Render can provide RENDER_EXTERNAL_URL (which is already HTTPS); locally
    # request.host_url keeps http://127.0.0.1:8080 instead of incorrectly
    # upgrading the API endpoints to HTTPS on Flask's plain HTTP listener.
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
    if os.environ.get("RENDER_EXTERNAL_URL"):
        base_url = os.environ["RENDER_EXTERNAL_URL"]
    elif forwarded_proto:
        base_url = f"{forwarded_proto}://{request.host}"
    else:
        base_url = request.host_url
    base_url = base_url.rstrip("/")
    client_path = Path(__file__).resolve().parent.parent / "storage" / "client" / "License Client.luau"
    try:
        source = client_path.read_text(encoding="utf-8")
    except OSError:
        return "-- license client unavailable", 503, {"Content-Type": "text/plain; charset=utf-8"}
    source = source.replace("__LICENSE_API_BASE__", base_url)
    source = source.replace(
        "__LICENSE_TAMPER_DETECTION_ENABLED__",
        "true" if config.LICENSE_TAMPER_DETECTION_ENABLED else "false",
    )
    source = source.replace(
        "__LICENSE_SESSION_HEARTBEAT_ENABLED__",
        "true" if config.LICENSE_SESSION_HEARTBEAT_ENABLED else "false",
    )
    source = source.replace(
        "__LICENSE_SESSION_HEARTBEAT_INTERVAL__",
        str(config.LICENSE_SESSION_HEARTBEAT_INTERVAL_SECONDS),
    )

    # `/client` must remain usable by the executor's HTTP client, but opening
    # the endpoint in a normal browser should never expose the license client
    # source. Browsers advertise HTML navigation through headers such as
    # `Accept: text/html`, `Sec-Fetch-*`, and/or `Upgrade-Insecure-Requests`.
    # For those requests, serve a source-safe page that contains only the two
    # public loader lines. The actual Luau source is returned only to requests
    # that do not look like browser navigation.
    accept = request.headers.get("Accept", "").lower()
    sec_fetch_mode = request.headers.get("Sec-Fetch-Mode", "").lower()
    sec_fetch_dest = request.headers.get("Sec-Fetch-Dest", "").lower()
    browser_navigation = (
        "text/html" in accept
        or sec_fetch_mode == "navigate"
        or sec_fetch_dest == "document"
        or "Upgrade-Insecure-Requests" in request.headers
    )

    if browser_navigation:
        import html

        loader_source = (
            'script_key = "YOUR-LICENSE-KEY"\n'
            f'loadstring(game:HttpGet("{base_url}/client"))()'
        )
        escaped_loader = html.escape(loader_source)
        page = f"""<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Celestial</title>
<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">
<style>
body {{ margin: 0; background: #fff; color: #111; font-family: Consolas, monospace; }}
pre {{ margin: 24px; white-space: pre-wrap; }}
</style>
</head>
<body><pre>{escaped_loader}</pre></body>
</html>"""
        return Response(
            page,
            status=200,
            mimetype="text/html",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            },
        )

    return Response(
        source,
        status=200,
        mimetype="text/plain",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.route('/whitelist/challenge', methods=['POST'])
def whitelist_challenge():
    from api.license_server import handle_challenge_request
    status, body, headers = handle_challenge_request(request.remote_addr or 'unknown')
    return Response(body, status=status, headers=headers)


@app.route('/whitelist/check', methods=['POST'])
def whitelist_check():
    from api.license_server import handle_check_request
    status, body, headers = handle_check_request(
        request.get_data(), request.remote_addr or 'unknown'
    )
    return Response(body, status=status, headers=headers)


@app.route('/whitelist/heartbeat', methods=['POST'])
def whitelist_heartbeat():
    from api.license_server import handle_heartbeat_request
    status, body, headers = handle_heartbeat_request(
        request.get_data(), request.remote_addr or 'unknown'
    )
    return Response(body, status=status, headers=headers)


@app.route('/whitelist/breach', methods=['POST'])
def whitelist_breach():
    from api.license_server import handle_breach_request
    status, body, headers = handle_breach_request(
        request.get_data(), request.remote_addr or 'unknown'
    )
    return Response(body, status=status, headers=headers)


@app.route('/whitelist/complete', methods=['POST'])
def whitelist_complete():
    from api.license_server import handle_complete_request
    status, body, headers = handle_complete_request(
        request.get_data(), request.remote_addr or 'unknown'
    )
    return Response(body, status=status, headers=headers)



def run():
    # Render (and most other PaaS hosts) assign a port dynamically via the
    # PORT env var and expect the app to bind to it -- it's not always
    # 8080. Falls back to 8080 when PORT isn't set (local runs, ngrok).
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)


def keep_alive():
    t = Thread(target=run)
    t.start()

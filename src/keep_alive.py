"""Small Flask server with four jobs:

1. `/` -- plain keep-alive endpoint.
3. `/client` -- serves the public Potassium license loader. It contains no
   server secret and is safe to distribute in the normal two-line loader.
3. `/whitelist/challenge` + `/whitelist/check` + `/whitelist/complete` -- public license API.
   The challenge is one-use/short-lived; the check validates the license key
   and game; the protected game payload is returned only after authorization;
   completion records activation and increments the execution counter.

There is intentionally no client-shared secret. A public Roblox script cannot
keep a secret from the user executing it. HTTPS, one-use challenges, rate
limits, server-side Supabase access, game restrictions, and payload withholding
are the security boundaries.
"""

import hashlib
import hmac
import os
from threading import Thread

from flask import Flask, request, jsonify, Response

app = Flask('')


@app.route('/')
def home():
    return "Bot is alive!", 200


def _license_server_enabled() -> bool:
    """Reads LICENSE_SERVER_ENABLED straight off os.environ rather than
    `from api import config` -- api/__init__.py pulls in discord_helpers.py
    (and, through it, discord itself) the moment any api.* submodule is
    imported, which is exactly the heavier chain keep_alive() runs ahead of
    so this port opens as early as possible (see module docstring). A plain
    os.getenv() here costs nothing and keeps that ordering intact.

    Defaults to true (route registered, matching today's always-on
    behavior) so an existing deployment's .env needs no change; set to
    false to skip creating the route below entirely."""
    return os.environ.get("LICENSE_SERVER_ENABLED", "true").strip().lower() not in ("false", "0", "no", "off")


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
    return Response(source, status=200, mimetype="text/plain", headers={"Cache-Control": "no-store"})


if _license_server_enabled():
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

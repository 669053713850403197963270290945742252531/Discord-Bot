"""Small Flask server with four jobs:

1. `/` -- plain keep-alive endpoint.
2. `/github-webhook` -- refreshes the Users.json cache after GitHub pushes.
3. `/client` -- serves the public Potassium license loader. It contains no
   server secret and is safe to distribute in the normal two-line loader.
4. `/whitelist/challenge` + `/whitelist/check` -- public license API. The
   challenge is one-use/short-lived; the check validates key + HWID +
   game, binds an empty HWID through the authenticated GitHub Contents API,
   and returns the protected game payload only after authorization.

There is intentionally no client-shared HMAC secret. A public Roblox script
cannot keep a secret from the user executing it. HTTPS, one-use challenges,
rate limits, server-side GitHub access, first-claim HWID binding, per-game
license restrictions, and payload withholding are the security boundaries.
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


@app.route('/github-webhook', methods=['POST'])
def github_webhook():
    # Imported lazily, inside the request handler, rather than at module
    # level -- keep_alive() is called in start.py before the heavier
    # discord.py import chain specifically so this server's port is open
    # as early as possible. Importing api.config/api.github at module load
    # time would drag that whole chain in immediately (api/__init__.py
    # pulls in discord_helpers.py, which imports discord). Deferring the
    # import to request time costs nothing -- by the time a real webhook
    # request can arrive, the rest of the bot has long since finished
    # starting up anyway.
    from api import config
    from api.github import trigger_cache_refresh_threadsafe

    if not config.GITHUB_WEBHOOK_SECRET:
        # Fail closed: with no secret configured there's no way to verify
        # a request actually came from GitHub, so refuse rather than let
        # anyone who finds this URL trigger refreshes.
        return jsonify({"error": "Webhook secret not configured"}), 503

    signature = request.headers.get('X-Hub-Signature-256', '')
    expected = 'sha256=' + hmac.new(
        config.GITHUB_WEBHOOK_SECRET.encode(), request.data, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return jsonify({"error": "Invalid signature"}), 401

    event = request.headers.get('X-GitHub-Event', '')
    if event == 'ping':
        # GitHub sends this once, when the webhook is first created, to
        # confirm the endpoint is reachable -- no payload to act on.
        return jsonify({"status": "pong"}), 200
    if event != 'push':
        return jsonify({"status": "ignored", "reason": f"unhandled event: {event}"}), 200

    payload = request.get_json(silent=True) or {}

    # Ignore pushes to any branch other than the one Users.json is actually
    # read/written on -- a push to some feature branch shouldn't invalidate
    # the live cache.
    if payload.get('ref') != f'refs/heads/{config.BRANCH}':
        return jsonify({"status": "ignored", "reason": "different branch"}), 200

    touched = any(
        config.FILE_PATH in (
            commit.get('added', []) + commit.get('removed', []) + commit.get('modified', [])
        )
        for commit in payload.get('commits', [])
    )
    if not touched:
        return jsonify({"status": "ignored", "reason": "Users.json not touched"}), 200

    if trigger_cache_refresh_threadsafe():
        return jsonify({"status": "refresh scheduled"}), 200

    # The bot's event loop isn't registered yet (still starting up) -- the
    # periodic fallback poll in start.py will pick this change up instead.
    return jsonify({"status": "deferred", "reason": "bot still starting up"}), 202


def run():
    # Render (and most other PaaS hosts) assign a port dynamically via the
    # PORT env var and expect the app to bind to it -- it's not always
    # 8080. Falls back to 8080 when PORT isn't set (local runs, ngrok).
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)


def keep_alive():
    t = Thread(target=run)
    t.start()

"""
Small Flask server with two jobs:

1. `/` -- a plain keep-alive endpoint for uptime-monitor / "host needs an
   open port" setups (e.g. Replit/UptimeRobot-style keep-alive), same as
   the original single-file bot.

2. `/github-webhook` -- lets GitHub push a notification here the instant
   Users.json changes on the configured branch, so the bot's in-memory
   cache (api/github.py) can refresh immediately instead of waiting on its
   periodic poll. The actual refresh runs on the bot's asyncio loop, not
   this thread -- see trigger_cache_refresh_threadsafe() in api/github.py
   for that handoff; this route's job is just to verify the request is
   really from GitHub and that it actually touched Users.json before
   asking for a refresh.

   Setup (repo Settings > Webhooks > Add webhook):
     Payload URL:  http://<your-host>:8080/github-webhook
     Content type: application/json
     Secret:       must match GITHUB_WEBHOOK_SECRET in .env
     Events:       "Just the push event"
"""

import hashlib
import hmac
import os
from threading import Thread

from flask import Flask, request, jsonify

app = Flask('')


@app.route('/')
def home():
    return "Bot is alive!", 200


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
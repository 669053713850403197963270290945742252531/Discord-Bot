"""
Keeps the GitHub push webhook's Payload URL (configured on the Users.json
repo -- the one keep_alive.py's /github-webhook route receives) pointed at
wherever this bot process is actually reachable right now, so it doesn't
have to be edited by hand on GitHub every time:

  - On Render (detected via the RENDER env var, which Render sets to
    "true" for every service): uses RENDER_EXTERNAL_URL, which Render also
    sets automatically at runtime to the service's own onrender.com URL.
    Falls back to config.RENDER_FALLBACK_URL in the (essentially
    impossible per Render's docs) case that's unset.

  - Locally: asks the ngrok agent's own local API (127.0.0.1:4040, which
    only exists while `ngrok http ...` is running) what its current public
    tunnel URL is. This is the piece that changes every time a free-tier
    ngrok tunnel is restarted -- the "ngrok juggling" this replaces.

sync_webhook_url() runs once near startup (see start.py) and never raises:
if it can't determine a URL (e.g. ngrok isn't running) or can't reach
GitHub's API (e.g. GITHUB_TOKEN lacks the Webhooks permission), it just
logs why and leaves the existing Payload URL alone. This is a convenience
on top of the manually-configured webhook, not something startup should
ever be blocked on -- worst case, you're back to editing the URL by hand
for that run.
"""

import os
from typing import Any, Dict, List, Optional

import aiohttp

from . import config
from .github import GitHubAPIError
from .tls import get_ssl_context

WEBHOOK_ROUTE = "/github-webhook"


async def _detect_public_base_url(session: aiohttp.ClientSession) -> Optional[str]:
    """Returns this process's current publicly-reachable base URL (no
    trailing slash), or None if it can't be determined."""
    if os.environ.get("RENDER") == "true":
        return (os.environ.get("RENDER_EXTERNAL_URL") or config.RENDER_FALLBACK_URL).rstrip("/")

    # Not on Render -- assume local + ngrok. Match on the same port
    # keep_alive.py actually bound to (PORT if set, else 8080), in case
    # multiple ngrok tunnels are open for different local ports.
    local_port = os.environ.get("PORT", "8080")
    try:
        async with session.get(config.NGROK_API_URL, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except (aiohttp.ClientError, TimeoutError):
        return None  # ngrok isn't running (or its API isn't up yet)

    tunnels = data.get("tunnels", [])
    for tunnel in tunnels:
        addr = tunnel.get("config", {}).get("addr", "")
        if tunnel.get("proto") == "https" and addr.rstrip("/").endswith(f":{local_port}"):
            return tunnel["public_url"].rstrip("/")
    # Looser fallback: any https tunnel, in case ngrok's reported addr
    # doesn't match exactly (e.g. "localhost" vs "127.0.0.1").
    for tunnel in tunnels:
        if tunnel.get("proto") == "https":
            return tunnel["public_url"].rstrip("/")
    return None


async def _find_users_webhook(session: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
    """Finds the webhook on the Users.json repo whose Payload URL ends in
    /github-webhook -- there could be other, unrelated webhooks on that
    repo, so this only ever touches the one that matches."""
    url = f"https://api.github.com/repos/{config.OWNER}/{config.REPO}/hooks"
    async with session.get(url, headers=config.HEADERS, params={"per_page": 100}) as resp:
        if resp.status != 200:
            err = await resp.text()
            raise GitHubAPIError(f"Failed to list webhooks (HTTP {resp.status}): {err}", resp.status)
        hooks: List[Dict[str, Any]] = await resp.json()

    matches = [h for h in hooks if h.get("config", {}).get("url", "").endswith(WEBHOOK_ROUTE)]
    if len(matches) > 1:
        print(
            f"Webhook URL sync: found {len(matches)} webhooks on {config.OWNER}/{config.REPO} "
            f"pointing at {WEBHOOK_ROUTE} -- updating the first one and leaving the rest alone."
        )
    return matches[0] if matches else None


async def sync_webhook_url() -> None:
    """Points the Users.json repo's GitHub webhook at wherever this process
    is currently reachable. Safe to call any time after the event loop is
    up; never raises -- any failure is logged and left for the fallback
    poll (or a manual edit on GitHub) to cover instead."""
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=get_ssl_context())) as session:
            base_url = await _detect_public_base_url(session)
            if base_url is None:
                where = "RENDER_EXTERNAL_URL" if os.environ.get("RENDER") == "true" else "ngrok (is it running?)"
                print(f"Webhook URL sync: couldn't determine a public URL from {where} -- leaving GitHub's Payload URL as-is.")
                return

            target_url = base_url + WEBHOOK_ROUTE

            hook = await _find_users_webhook(session)
            if hook is None:
                print(
                    f"Webhook URL sync: no webhook on {config.OWNER}/{config.REPO} points at "
                    f"{WEBHOOK_ROUTE} yet -- create one manually once on GitHub (the Payload URL "
                    "you enter doesn't matter beyond that, it'll be kept in sync automatically "
                    "from here on)."
                )
                return

            current_url = hook.get("config", {}).get("url")
            if current_url == target_url:
                print(f"Webhook URL sync: already pointed at {target_url}.")
                return

            hook_id = hook["id"]
            config_url = f"https://api.github.com/repos/{config.OWNER}/{config.REPO}/hooks/{hook_id}/config"
            # The dedicated .../config sub-resource, rather than PATCHing
            # /hooks/{hook_id} directly: per GitHub's docs, the latter
            # requires re-sending the secret on every update or it gets
            # wiped, whereas this endpoint updates only the fields given
            # and leaves the existing secret untouched.
            async with session.patch(config_url, headers=config.HEADERS, json={"url": target_url}) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise GitHubAPIError(f"Failed to update webhook config (HTTP {resp.status}): {err}", resp.status)

            print(f"Webhook URL sync: updated Payload URL {current_url!r} -> {target_url!r}.")
    except GitHubAPIError as e:
        print(
            f"Webhook URL sync failed: {e}. If that's a 403/404, check that GITHUB_TOKEN has the "
            "'Webhooks' repository permission (fine-grained PATs need this granted explicitly, "
            "separate from the Contents permission used for Users.json)."
        )
    except Exception as e:  # pragma: no cover -- this must never take the bot down
        print(f"Webhook URL sync failed unexpectedly: {e}")

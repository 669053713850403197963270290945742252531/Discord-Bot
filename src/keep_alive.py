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

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from threading import Thread
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request as URLRequest, urlopen

from flask import Flask, request, jsonify, Response, redirect
from api import config

app = Flask('')

# OAuth and authenticated editor sessions are intentionally held server-side.
# The originating Discord interaction is already stored in memory, so this
# feature follows the same single-process deployment model. Restarting the
# bot invalidates these short-lived sessions safely.
_OAUTH_PENDING = {}
_AUTHENTICATED_EDITOR_SESSIONS = {}
_OAUTH_LOCK = threading.RLock()
_OAUTH_STATE_TTL_SECONDS = 10 * 60
_EDITOR_AUTH_SESSION_TTL_SECONDS = 30 * 60
_AUTHORIZATION_RECHECK_INTERVAL_SECONDS = 60
_EDITOR_AUTH_COOKIE = 'celestial_whitelist_auth'
_OAUTH_STATE_COOKIE = 'celestial_whitelist_oauth_state'


class _DiscordOAuthRequestError(RuntimeError):
    """Discord API failure with an optional HTTP status for authorization decisions."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _oauth_configuration():
    client_id = os.getenv('DISCORD_OAUTH_CLIENT_ID', '').strip()
    client_secret = os.getenv('DISCORD_OAUTH_CLIENT_SECRET', '').strip()
    redirect_uri = os.getenv('DISCORD_OAUTH_REDIRECT_URI', '').strip()
    parsed = urlsplit(redirect_uri) if redirect_uri else None
    if not client_id or not client_id.isdigit() or not client_secret or not redirect_uri:
        return None
    if parsed.scheme != 'https' or not parsed.netloc or parsed.path != '/editwhitelist/oauth/callback':
        return None
    return {'client_id': client_id, 'client_secret': client_secret, 'redirect_uri': redirect_uri}


def _cookie_secure():
    forwarded_proto = request.headers.get('X-Forwarded-Proto', '').split(',', 1)[0].strip().lower()
    config = _oauth_configuration()
    return bool(request.is_secure or forwarded_proto == 'https' or config)


def _trusted_origin():
    origin = request.headers.get('Origin', '').strip()
    config = _oauth_configuration()
    if not origin or not config:
        return False
    try:
        incoming = urlsplit(origin)
        expected = urlsplit(config['redirect_uri'])
        return (
            incoming.scheme.lower() == expected.scheme.lower()
            and incoming.netloc.lower() == expected.netloc.lower()
            and incoming.path in ('', '/')
        )
    except ValueError:
        return False


def _gate_page(message='Checking authorization…', *, show_launch_hint=True):
    """An inert waiting/login page; it never includes the whitelist editor code."""
    from html import escape
    launch_hint = 'Open this page using the temporary link from `/whitelist edit` in the Celestial Discord server.' if show_launch_hint else ''
    safe_message = escape(str(message))
    safe_hint = escape(launch_hint)
    page = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Celestial · Verifying access</title>
<link rel="icon" type="image/png" href="/editwhitelist/favicon.png">
<style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;color:#edf0ff;font:16px/1.6 system-ui,-apple-system,Segoe UI,sans-serif;background:radial-gradient(ellipse at 50% 0%,#27245b 0,transparent 55%),linear-gradient(145deg,#090c1b,#10172d 60%,#080b18)}}main{{width:min(520px,100%);padding:36px 30px;border:1px solid #7771c755;border-radius:24px;background:#10162ae8;box-shadow:0 28px 90px #0008;text-align:center}}.mark{{display:grid;place-items:center;margin:0 auto 20px;width:60px;height:60px;border-radius:18px;border:1px solid #a78bfa66;background:linear-gradient(135deg,#8b5cf633,#22d3ee1a);font-size:28px}}h1{{margin:0 0 8px;font-size:1.55rem;letter-spacing:-.03em}}p{{margin:9px 0;color:#aeb9d8}}.spinner{{width:30px;height:30px;margin:24px auto 10px;border:3px solid #a78bfa33;border-top-color:#a78bfa;border-right-color:#55d9f3;border-radius:50%;animation:spin .8s linear infinite}}.status{{color:#c4b5fd;font-weight:700}}.hint{{font-size:.9rem}}.error{{color:#fda4af}}@keyframes spin{{to{{transform:rotate(360deg)}}}}@media(prefers-reduced-motion:reduce){{.spinner{{animation:none}}}}
</style></head><body><main><div class="mark" aria-hidden="true">✦</div><h1>Celestial <span style="color:#a5b4fc">Whitelist Editor</span></h1>
<p id="status" class="status">{safe_message}</p><div id="spinner" class="spinner" aria-hidden="true"></div>
<p id="details" class="hint">Verifying your Discord identity, server membership, and license permissions before the editor is loaded.</p>
<p id="hint" class="hint">{safe_hint}</p><p id="error" class="error" role="alert"></p></main>
<script>
(async function(){{
 const status=document.getElementById('status'), details=document.getElementById('details'), hint=document.getElementById('hint'), error=document.getElementById('error'), spinner=document.getElementById('spinner');
 const params=new URLSearchParams(location.hash.slice(1)); const token=params.get('session')||'';
 if(token){{ history.replaceState(null,document.title,location.pathname+location.search); }}
 if(!token){{ status.textContent='Authentication required'; details.textContent='The editor is only available through a valid, temporary Discord launch link.'; spinner.hidden=true; return; }}
 status.textContent='Preparing secure Discord sign-in…'; hint.textContent='This page does not contain the editor or whitelist data.';
 try{{
  const response=await fetch('/editwhitelist/auth/start',{{method:'POST',credentials:'same-origin',cache:'no-store',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{launch_token:token}})}});
  const payload=await response.json();
  if(!response.ok||!payload.ok||!payload.authorization_url) throw new Error(payload.error||'Could not start Discord authorization.');
  status.textContent='Redirecting to Discord…'; location.replace(payload.authorization_url);
 }}catch(e){{ status.textContent='Authorization could not start'; details.textContent='No whitelist data has been loaded.'; error.textContent=e&&e.message?e.message:'Please reopen the editor from Discord.'; spinner.hidden=true; hint.textContent='Run `/whitelist edit` again after checking the bot configuration.'; }}
}})();
</script></body></html>'''
    response = Response(page, status=200, mimetype='text/html', headers={
        'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
        'Pragma': 'no-cache', 'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
        'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action https://discord.com",
    })
    return response


def _discord_oauth_request(url, *, method='GET', headers=None, form=None):
    data = urlencode(form).encode('utf-8') if form is not None else None
    request_headers = {'User-Agent': 'CelestialWhitelistEditor/1.0', 'Accept': 'application/json'}
    if headers:
        request_headers.update(headers)
    if form is not None:
        request_headers['Content-Type'] = 'application/x-www-form-urlencoded'
    req = URLRequest(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(req, timeout=12) as response:
            payload = response.read(1024 * 1024)
    except HTTPError as exc:
        raise _DiscordOAuthRequestError('Discord OAuth request failed.', exc.code) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise _DiscordOAuthRequestError('Discord OAuth could not be reached.', None) from exc
    try:
        decoded = json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError('Discord returned an invalid OAuth response.') from exc
    if not isinstance(decoded, dict):
        raise RuntimeError('Discord returned an invalid OAuth response.')
    return decoded


def _oauth_api_get(path, access_token):
    return _discord_oauth_request(
        'https://discord.com/api/v10' + path,
        headers={'Authorization': 'Bearer ' + access_token},
    )


def _license_expired(value):
    if value in (None, ''):
        return False
    if not isinstance(value, str):
        return True
    try:
        expires = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires <= datetime.now(timezone.utc)
    except ValueError:
        return True


async def _check_editor_authorization_async(editor_session, user_id):
    """Fail-closed live guild and license check, run on the bot's asyncio loop."""
    expected_id = str(editor_session.get('user_id') or '')
    if str(user_id) != expected_id:
        return False, 'Discord identity does not match the editor launch.'

    from api.supabase_db import get_license_by_discord_id
    license_record = await get_license_by_discord_id(str(user_id))
    if not isinstance(license_record, dict):
        return False, 'No license is linked to this Discord account.'
    if str(license_record.get('DiscordId') or '') != str(user_id):
        return False, 'The license is not linked to this Discord account.'
    if license_record.get('Enabled') is not True:
        return False, 'The license is disabled.'
    if _license_expired(license_record.get('ExpiresAt')):
        return False, 'The license is expired or has an invalid expiry.'
    if str(license_record.get('Rank') or '').strip().casefold() not in {'admin', 'owner'}:
        return False, 'The license rank is not authorized for the editor.'

    return True, 'License and rank checks passed.'


def _check_editor_authorization(editor_session, user_id, access_token):
    """Recheck current Discord guild membership and the live Supabase license."""
    if not isinstance(access_token, str) or not access_token:
        return False, 'The Discord authorization session is missing.'
    try:
        member = _oauth_api_get(f"/users/@me/guilds/{int(config.GUILD_ID)}/member", access_token)
        member_user = member.get('user') if isinstance(member, dict) else None
        if isinstance(member_user, dict) and str(member_user.get('id') or '') != str(user_id):
            return False, 'Guild membership identity did not match.'
    except _DiscordOAuthRequestError as exc:
        # Missing membership and rejected/invalid credentials are definite
        # denials. Rate limits, server errors, and network outages are transient
        # verification failures and must not disguise as an expired session.
        if exc.status_code in (401, 403, 404):
            return False, 'The Discord account is not currently authorized for the Celestial server.'
        raise
    from api.whitelist_editor import run_on_editor_bot_loop
    return run_on_editor_bot_loop(
        editor_session,
        _check_editor_authorization_async(editor_session, str(user_id)),
        timeout=20,
    )


def _get_authenticated_editor_session(*, recheck=True):
    auth_id = request.cookies.get(_EDITOR_AUTH_COOKIE, '')
    if not auth_id or len(auth_id) > 128:
        return None
    now = time.time()
    with _OAUTH_LOCK:
        auth = _AUTHENTICATED_EDITOR_SESSIONS.get(auth_id)
        if not auth:
            return None
        if auth.get('expires_at', 0) <= now:
            _AUTHENTICATED_EDITOR_SESSIONS.pop(auth_id, None)
            return None
        editor_token = auth.get('editor_token', '')
        user_id = str(auth.get('user_id') or '')
        access_token = auth.get('access_token')
        last_successful_check = float(auth.get('last_authorized_check_at') or auth.get('created_at') or 0)
        last_check_attempt = float(auth.get('last_authorization_check_attempt_at') or last_successful_check)

    from api.whitelist_editor import get_whitelist_editor_session
    editor_session = get_whitelist_editor_session(editor_token)
    if editor_session is None or str(editor_session.get('user_id')) != user_id:
        with _OAUTH_LOCK:
            _AUTHENTICATED_EDITOR_SESSIONS.pop(auth_id, None)
        return None

    # Avoid calling Discord and Supabase multiple times for the callback, page
    # load, and initial data request that occur seconds apart. Fresh entitlement
    # checks still happen at least once per minute during an active session.
    if recheck and now - last_check_attempt >= _AUTHORIZATION_RECHECK_INTERVAL_SECONDS:
        with _OAUTH_LOCK:
            current_auth = _AUTHENTICATED_EDITOR_SESSIONS.get(auth_id)
            if current_auth is None:
                return None
            # Another concurrent request may already have refreshed the check.
            last_check_attempt = float(current_auth.get('last_authorization_check_attempt_at') or 0)
            should_check = now - last_check_attempt >= _AUTHORIZATION_RECHECK_INTERVAL_SECONDS
            if should_check:
                current_auth['last_authorization_check_attempt_at'] = now
        if should_check:
            try:
                allowed, reason = _check_editor_authorization(editor_session, user_id, access_token)
            except Exception as exc:
                # A temporary Discord/Supabase/network outage is not proof that
                # a user's authorization was revoked. Preserve the last verified
                # session until its hard expiry; retry after the interval and log.
                print(f'[Whitelist Editor OAuth] Recheck temporarily unavailable for {user_id}: {exc}')
            else:
                if not allowed:
                    print(f'[Whitelist Editor OAuth] Authorization revoked for {user_id}: {reason}')
                    with _OAUTH_LOCK:
                        _AUTHENTICATED_EDITOR_SESSIONS.pop(auth_id, None)
                    return None
                with _OAUTH_LOCK:
                    current_auth = _AUTHENTICATED_EDITOR_SESSIONS.get(auth_id)
                    if current_auth is None:
                        return None
                    current_auth['last_authorized_check_at'] = time.time()
                    current_auth['last_authorization_check_attempt_at'] = time.time()
    return editor_session


def _close_editor_launch_for_token(token, reason):
    """Best-effort terminal Discord-message update from a Flask request thread."""
    if not isinstance(token, str) or not token:
        return False
    try:
        from api.whitelist_editor import (
            close_whitelist_editor_launch,
            get_whitelist_editor_session,
            run_on_editor_bot_loop,
        )
        editor_session = get_whitelist_editor_session(token)
        if editor_session is None:
            return False
        return run_on_editor_bot_loop(
            editor_session,
            close_whitelist_editor_launch(editor_session, reason),
            timeout=12,
        )
    except Exception as exc:
        print(f'[Whitelist Editor] Could not close launch message ({reason}): {exc}')
        return False


def _authorization_denied_page():
    response = _gate_page('Access denied', show_launch_hint=False)
    # Replace the generic waiting status with a fixed message; don't disclose
    # license/database details to browsers that did not pass authorization.
    body = response.get_data(as_text=True).replace(
        'Verifying your Discord identity, server membership, and license permissions before the editor is loaded.',
        'This account is not authorized to access the whitelist editor. Confirm your Celestial membership and enabled Admin/Owner license, then reopen the editor from Discord.'
    )
    body = body.replace('Authentication required', 'Access denied')
    response.set_data(body)
    response.status_code = 403
    return response


@app.route('/')
def home():
    return "Bot is alive!", 200


@app.route('/editwhitelist/favicon.png', methods=['GET'])
def whitelist_editor_favicon():
    """Serve the locally bundled PNG tab icon for the whitelist editor."""
    from pathlib import Path

    icon_path = Path(__file__).resolve().parent / "static" / "whitelist-editor.png"
    try:
        icon_bytes = icon_path.read_bytes()
    except OSError:
        return "Whitelist editor icon unavailable", 503, {"Content-Type": "text/plain; charset=utf-8"}

    return Response(
        icon_bytes,
        status=200,
        mimetype="image/png",
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.route('/editwhitelist', methods=['GET'])
@app.route('/editwhitelist/', methods=['GET'])
def whitelist_editor_page():
    """Serve the full editor only after server-side Discord OAuth authorization."""
    session = _get_authenticated_editor_session(recheck=True)
    if session is None:
        # A cookie alone is never sufficient. Return only the inert gate page;
        # when a fresh launch fragment exists, its client bootstrap begins OAuth.
        return _gate_page()

    from pathlib import Path
    page_path = Path(__file__).resolve().parent / "templates" / "editwhitelist.html"
    try:
        page = page_path.read_text(encoding="utf-8")
    except OSError:
        return "Whitelist editor unavailable", 503, {"Content-Type": "text/plain; charset=utf-8"}

    return Response(page, status=200, mimetype="text/html", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache", "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": (
            "default-src 'none'; "
            "script-src https://cdnjs.cloudflare.com 'unsafe-inline'; "
            "style-src https://cdnjs.cloudflare.com 'unsafe-inline'; "
            "font-src https://cdnjs.cloudflare.com data:; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        ),
    })


@app.route('/editwhitelist/auth/start', methods=['POST'])
def whitelist_editor_oauth_start():
    """Create a one-use OAuth state/PKCE transaction for a bot-issued launch token."""
    oauth = _oauth_configuration()
    if oauth is None:
        return _editor_api_json({"ok": False, "error": "Discord OAuth is not configured correctly. Set DISCORD_OAUTH_CLIENT_ID, DISCORD_OAUTH_CLIENT_SECRET, and DISCORD_OAUTH_REDIRECT_URI (HTTPS callback path /editwhitelist/oauth/callback)."}, 503)
    if not _trusted_origin():
        return _editor_api_json({"ok": False, "error": "This sign-in request did not originate from the configured website."}, 403)
    body = request.get_json(silent=True)
    token = body.get('launch_token') if isinstance(body, dict) else None
    from api.whitelist_editor import get_whitelist_editor_session
    editor_session = get_whitelist_editor_session(token, require_launch_valid=True)
    if editor_session is None:
        existing_session = get_whitelist_editor_session(token)
        reason = "invalidated"
        if existing_session is not None:
            if existing_session.get("oauth_completed"):
                reason = "used"
            elif existing_session.get("launch_expires_at", 0) <= time.time():
                reason = "expired"
        _close_editor_launch_for_token(token, reason)
        return _editor_api_json({"ok": False, "error": "The temporary editor launch link is expired, already used, or invalid. Run `/whitelist edit` again in Discord."}, 401)

    now = time.time()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
    with _OAUTH_LOCK:
        for old_state, pending in list(_OAUTH_PENDING.items()):
            if pending.get('created_at', 0) + _OAUTH_STATE_TTL_SECONDS <= now:
                _OAUTH_PENDING.pop(old_state, None)
        if editor_session.get('oauth_completed'):
            return _editor_api_json({"ok": False, "error": "This launch link has already been used. Run `/whitelist edit` again for a fresh secure link."}, 409)
        if any(item.get('editor_token') == token for item in _OAUTH_PENDING.values()):
            return _editor_api_json({"ok": False, "error": "Discord sign-in is already in progress for this launch link. Finish that window or run `/whitelist edit` again."}, 409)
        _OAUTH_PENDING[state] = {
            'created_at': now,
            'code_verifier': verifier,
            'editor_token': token,
            'user_id': str(editor_session.get('user_id') or ''),
        }
    params = urlencode({
        'client_id': oauth['client_id'],
        'redirect_uri': oauth['redirect_uri'],
        'response_type': 'code',
        'scope': 'identify guilds.members.read',
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    })
    response = _editor_api_json({"ok": True, "authorization_url": 'https://discord.com/oauth2/authorize?' + params})
    response.set_cookie(
        _OAUTH_STATE_COOKIE, state, max_age=_OAUTH_STATE_TTL_SECONDS,
        httponly=True, secure=_cookie_secure(), samesite='Lax',
        path='/editwhitelist/oauth',
    )
    return response


@app.route('/editwhitelist/oauth/callback', methods=['GET'])
def whitelist_editor_oauth_callback():
    """Complete Discord OAuth and establish a short-lived server-side auth session."""
    oauth = _oauth_configuration()
    state = request.args.get('state', '')
    state_cookie = request.cookies.get(_OAUTH_STATE_COOKIE, '')
    state_valid = bool(state and state_cookie and hmac.compare_digest(state, state_cookie))
    if not state_valid:
        response = _authorization_denied_page()
        response.set_cookie(_OAUTH_STATE_COOKIE, '', max_age=0, httponly=True, secure=_cookie_secure(), samesite='Lax', path='/editwhitelist/oauth')
        return response
    if request.args.get('error'):
        with _OAUTH_LOCK:
            cancelled = _OAUTH_PENDING.pop(state, None)
        if cancelled:
            _close_editor_launch_for_token(cancelled.get('editor_token'), "denied")
        response = _authorization_denied_page()
        response.set_cookie(_OAUTH_STATE_COOKIE, '', max_age=0, httponly=True, secure=_cookie_secure(), samesite='Lax', path='/editwhitelist/oauth')
        return response
    with _OAUTH_LOCK:
        pending = _OAUTH_PENDING.pop(state, None)
    if not pending or time.time() - pending.get('created_at', 0) > _OAUTH_STATE_TTL_SECONDS or oauth is None:
        response = _authorization_denied_page()
        response.set_cookie(_OAUTH_STATE_COOKIE, '', max_age=0, httponly=True, secure=_cookie_secure(), samesite='Lax', path='/editwhitelist/oauth')
        return response

    try:
        from api.whitelist_editor import get_whitelist_editor_session
        editor_session = get_whitelist_editor_session(pending.get('editor_token'))
        if editor_session is None or str(editor_session.get('user_id')) != str(pending.get('user_id')):
            raise RuntimeError('Editor launch expired.')
        code = request.args.get('code', '')
        if not code:
            raise RuntimeError('Authorization code missing.')
        token_data = _discord_oauth_request('https://discord.com/api/v10/oauth2/token', method='POST', form={
            'client_id': oauth['client_id'],
            'client_secret': oauth['client_secret'],
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': oauth['redirect_uri'],
            'code_verifier': pending['code_verifier'],
        })
        access_token = token_data.get('access_token')
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError('Discord did not return an access token.')
        discord_user = _oauth_api_get('/users/@me', access_token)
        discord_user_id = str(discord_user.get('id') or '')
        if not discord_user_id.isdigit() or discord_user_id != str(pending['user_id']):
            raise RuntimeError('The authenticated Discord account does not match the account that opened the editor.')
        # The current membership and license are checked here; ongoing sessions
        # periodically recheck rather than hitting Discord and Supabase on every
        # page/API request.
        allowed, _reason = _check_editor_authorization(editor_session, discord_user_id, access_token)
        if not allowed:
            raise RuntimeError('Discord account failed the server membership/license/rank checks.')

        token_lifetime = token_data.get('expires_in')
        if isinstance(token_lifetime, bool) or not isinstance(token_lifetime, (int, float)) or token_lifetime <= 30:
            raise RuntimeError('Discord returned an invalid or too-short access-token lifetime.')
        auth_id = secrets.token_urlsafe(32)
        session_lifetime = min(_EDITOR_AUTH_SESSION_TTL_SECONDS, int(token_lifetime) - 30)
        expires_at = min(float(editor_session.get('expires_at') or 0), time.time() + session_lifetime)
        if expires_at <= time.time():
            raise RuntimeError('The temporary editor link expired during sign-in.')
        with _OAUTH_LOCK:
            editor_session['oauth_completed'] = True
            _AUTHENTICATED_EDITOR_SESSIONS[auth_id] = {
                'editor_token': pending['editor_token'],
                'user_id': discord_user_id,
                'access_token': access_token,  # server-side only; never written to a cookie or sent to JavaScript
                'created_at': time.time(),
                'last_authorized_check_at': time.time(),
                'last_authorization_check_attempt_at': time.time(),
                'expires_at': expires_at,
            }
        # The one-time launch URL has now been consumed successfully. Make the
        # original bot response terminal before later save callbacks can update it.
        _close_editor_launch_for_token(pending['editor_token'], "used")
        response = redirect('/editwhitelist', code=303)
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.set_cookie(
            _EDITOR_AUTH_COOKIE, auth_id, max_age=max(1, int(expires_at - time.time())),
            httponly=True, secure=_cookie_secure(), samesite='Lax', path='/editwhitelist',
        )
        response.set_cookie(_OAUTH_STATE_COOKIE, '', max_age=0, httponly=True, secure=_cookie_secure(), samesite='Lax', path='/editwhitelist/oauth')
        return response
    except Exception as exc:
        print(f'[Whitelist Editor OAuth] Authorization rejected: {exc}')
        _close_editor_launch_for_token(pending.get('editor_token'), "denied")
        response = _authorization_denied_page()
        response.set_cookie(_OAUTH_STATE_COOKIE, '', max_age=0, httponly=True, secure=_cookie_secure(), samesite='Lax', path='/editwhitelist/oauth')
        return response


@app.route('/editwhitelist/auth/logout', methods=['POST'])
def whitelist_editor_logout():
    if not _trusted_origin():
        return _editor_api_json({"ok": False, "error": "Invalid request origin."}, 403)
    auth_id = request.cookies.get(_EDITOR_AUTH_COOKIE, '')
    with _OAUTH_LOCK:
        _AUTHENTICATED_EDITOR_SESSIONS.pop(auth_id, None)
    response = _editor_api_json({"ok": True})
    response.set_cookie(_EDITOR_AUTH_COOKIE, '', max_age=0, httponly=True, secure=_cookie_secure(), samesite='Lax', path='/editwhitelist')
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
    if status == 401 and request.path.startswith('/editwhitelist/api/'):
        response.set_cookie(
            _EDITOR_AUTH_COOKIE, '', max_age=0, httponly=True,
            secure=_cookie_secure(), samesite='Lax', path='/editwhitelist',
        )
    return response


def _authorized_editor_session():
    """Return the editor capability only behind a valid OAuth-authenticated cookie."""
    return _get_authenticated_editor_session(recheck=True)


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


@app.route('/editwhitelist/api/preview', methods=['POST'])
def whitelist_editor_preview_changes():
    """Validate edits and return a detailed diff without changing the database."""
    if not _trusted_origin():
        return _editor_api_json({"ok": False, "error": "Invalid request origin. Reload the editor and retry."}, 403)
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
    )

    if request.content_length is not None and request.content_length > MAX_EDITOR_JSON_BYTES + 65536:
        return _editor_api_json({"ok": False, "error": "The editor payload is too large (maximum 8 MiB)."}, 413)

    request_data = request.get_json(silent=True)
    if not isinstance(request_data, dict):
        return _editor_api_json({"ok": False, "error": "Expected a JSON request body."}, 400)
    source = request_data.get("source")
    expected_revision = request_data.get("revision")
    if not isinstance(source, str):
        return _editor_api_json({"ok": False, "error": "Missing editor source."}, 400)
    if len(source.encode("utf-8")) > MAX_EDITOR_JSON_BYTES:
        return _editor_api_json({"ok": False, "error": "The whitelist JSON exceeds the 8 MiB limit."}, 413)

    try:
        parsed = json.loads(source)
    except json.JSONDecodeError as exc:
        return _editor_api_json({"ok": False, "error": f"Invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})."}, 400)

    try:
        records = validate_and_normalize_records(parsed)
    except ValueError as exc:
        return _editor_api_json({"ok": False, "error": str(exc)}, 400)

    try:
        with _EDITOR_SAVE_LOCK:
            from api.supabase_db import fetch_users
            current_users = run_on_editor_bot_loop(session, fetch_users())
            current_revision = database_revision(current_users)
            if not isinstance(expected_revision, str) or not hmac.compare_digest(expected_revision, current_revision):
                return _editor_api_json({
                    "ok": False,
                    "error": "The whitelist database changed after you opened or refreshed this editor. Click Refresh to load the latest database before reviewing a save.",
                    "code": "database_changed",
                }, 409)
            if current_users and not records:
                return _editor_api_json({
                    "ok": False,
                    "error": "Saving an empty dataset would delete every license. This editor blocks that accidental full-database wipe.",
                }, 400)

            requested_diff = _match_records(current_users, records)
            protected_fields = {"ActivationCountry", "ActivationRobloxUserId"}
            for item in requested_diff["modified"]:
                changed_fields = protected_fields.intersection((item.get("Changes") or {}).keys())
                if changed_fields:
                    names = ", ".join(sorted(changed_fields))
                    return _editor_api_json({
                        "ok": False,
                        "error": f"System-generated activation history cannot be edited from this page ({names}). Leave those values unchanged.",
                    }, 400)

            # Mirror the save path before rendering the diff so that server-managed
            # CreatedAt/UpdatedAt behavior is included in the user's preview.
            # Exact timestamp values are generated again at commit time.
            if requested_diff["added"] or requested_diff["removed"] or requested_diff["modified"]:
                _prepare_server_timestamps(current_users, records, requested_diff)
            diff = _match_records(current_users, records)
            summary = summary_counts(diff)
            return _editor_api_json({
                "ok": True,
                "revision": current_revision,
                "summary": summary,
                "diff": {
                    "added": diff["added"],
                    "removed": diff["removed"],
                    "modified": diff["modified"],
                },
            })
    except Exception as exc:
        print(f"[Whitelist Editor] Failed to preview database changes: {exc}")
        return _editor_api_json({"ok": False, "error": "Could not prepare a change preview. Check the bot logs and retry."}, 500)


@app.route('/editwhitelist/api/save', methods=['POST'])
def whitelist_editor_save_data():
    """Validate and apply an edited full licenses dataset, then update the originating Discord response."""
    if not _trusted_origin():
        return _editor_api_json({"ok": False, "error": "Invalid request origin. Reload the editor and retry."}, 403)
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

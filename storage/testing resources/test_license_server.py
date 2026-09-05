#!/usr/bin/env python3
"""Offline tests for the public, challenge-based Roblox license API."""
import json
import os
import sys
import time
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
for _var, _val in {
    "DISCORD_TOKEN": "test", "GITHUB_TOKEN": "test", "EZ_HOST_API_KEY": "test",
    "GUILD_ID": "1", "REQUIRED_ROLE_ID": "1", "REGISTRATION_CHANNEL_ID": "1",
    "REACTION_ROLE_CHANNEL_ID": "1", "PANEL_CHANNEL_ID": "1", "BUYER_ROLE_ID": "1",
    "REDEEM_ALERTS_CHANNEL_ID": "1", "MODERATION_ALERTS_CHANNEL_ID": "1",
    "GITHUB_OWNER": "test", "GITHUB_REPO": "test",
}.items():
    os.environ.setdefault(_var, _val)
os.environ.setdefault("LICENSE_GAME_SCRIPTS_JSON", '{"12345":"storage/games/test.lua"}')

from api import config
from api import license_server as ls

HWID_A = "a" * 64
HWID_B = "b" * 64
USERS = [
    {
        "Identifier": "Bound", "HWID": HWID_A, "DiscordId": "111", "Rank": "VIP",
        "Activated": None, "Key": "GOODKEY123", "Notes": None, "Executions": 0,
        "Games": ["12345"], "LastHwidReset": None, "totalHwidResets": 0,
    },
    {
        "Identifier": "Unbound", "HWID": None, "DiscordId": "222", "Rank": "User",
        "Activated": None, "Key": "UNBOUND456", "Notes": None, "Executions": 0,
        "Games": ["12345"], "LastHwidReset": None, "totalHwidResets": 0,
    },
    {
        "Identifier": "Restricted", "HWID": HWID_A, "DiscordId": "333", "Rank": "User",
        "Activated": None, "Key": "OTHER789", "Notes": None, "Executions": 0,
        "Games": ["99999"], "LastHwidReset": None, "totalHwidResets": 0,
    },
]

passed = failed = 0

def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1; print("  PASS", label)
    else:
        failed += 1; print("  FAIL", label, detail)

def make_body(key="GOODKEY123", hwid=HWID_A, game_id=12345, nonce="abcdefghijklmnop", timestamp=None):
    return json.dumps({"key": key, "hwid": hwid, "game_id": game_id, "timestamp": int(time.time()) if timestamp is None else timestamp, "nonce": nonce}).encode()

def main():
    print("\n-- challenge --")
    status, body, headers = ls.handle_challenge_request()
    challenge = json.loads(body)
    check("challenge -> 200 and nonce", status == 200 and isinstance(challenge.get("nonce"), str))

    print("\n-- request validation --")
    now = int(time.time())
    # Seed a known nonce through the real challenge store.
    status, body, _ = ls.handle_challenge_request(); nonce = json.loads(body)["nonce"]
    status, body, _ = ls.handle_check_request(make_body(nonce=nonce, timestamp=now), "127.0.0.1")
    check("valid request reaches evaluator", status == 200)

    # Reusing the same nonce must fail.
    status, body, _ = ls.handle_check_request(make_body(nonce=nonce, timestamp=now), "127.0.0.1")
    check("nonce replay -> 400", status == 400 and json.loads(body)["reason"] == "invalid_nonce")

    status, body, _ = ls.handle_check_request(make_body(nonce="abcdefghijklmnop", timestamp=now - 999), "127.0.0.1")
    check("stale timestamp -> 400", status == 400 and json.loads(body)["reason"] == "stale_timestamp")

    print("\n-- evaluator --")
    with patch.object(ls, "_run") as run:
        def fake_run(coro):
            # Close coroutines we aren't actually awaiting in this unit test.
            try: coro.close()
            except Exception: pass
            if fake_run.calls == 0:
                fake_run.calls += 1
                return USERS, "sha"
            return None
        fake_run.calls = 0
        run.side_effect = fake_run
        ls.config.LICENSE_GAME_SCRIPTS = {"12345": "storage/games/test.lua"}
        with patch.object(ls, "fetch_storage_file", object()):
            # Separate direct tests use patched _run return values below.
            pass

    # Direct evaluator tests: patch the coroutine bridge per call.
    original = ls._run
    try:
        def fetch_only(coro):
            try: coro.close()
            except Exception: pass
            return json.loads(json.dumps(USERS)), "sha"
        ls._run = fetch_only
        result = ls.evaluate_and_load("GOODKEY123", HWID_A, "12345")
        check("correct key+HWID+game -> allowed", result.get("allowed") is True)
        result = ls.evaluate_and_load("GOODKEY123", HWID_B, "12345")
        check("wrong HWID -> mismatch", result == {"allowed": False, "reason": "hwid_mismatch"})
        result = ls.evaluate_and_load("GOODKEY123", HWID_A, "99999")
        check("unconfigured game -> denied", result == {"allowed": False, "reason": "game_not_configured"})
        result = ls.evaluate_and_load("OTHER789", HWID_A, "12345")
        check("license game restriction -> denied", result == {"allowed": False, "reason": "game_not_authorized"})
        result = ls.evaluate_and_load("NOSUCH", HWID_A, "12345")
        check("unknown key -> denied", result == {"allowed": False, "reason": "not_whitelisted"})
    finally:
        ls._run = original

    # First activation: fetch returns a copy with no HWID, then commit succeeds,
    # then script fetch succeeds.
    original = ls._run
    try:
        unbound = json.loads(json.dumps(USERS))
        calls = []
        def bind_run(coro):
            try: coro.close()
            except Exception: pass
            if not calls:
                calls.append("fetch")
                return unbound, "sha"
            if len(calls) == 1:
                calls.append("commit")
                return None
            calls.append("script")
            return "print('ok')"
        ls._run = bind_run
        result = ls.evaluate_and_load("UNBOUND456", HWID_B, "12345")
        check("unbound key binds and allows", result.get("allowed") is True and unbound[1]["HWID"] == HWID_B)
        check("first activation writes then fetches script", calls == ["fetch", "commit", "script"])
        token = result.get("execution_token")
        check("successful check returns execution token", isinstance(token, str) and token)

        def complete_run(coro):
            try: coro.close()
            except Exception: pass
            if len(calls) == 3:
                calls.append("fetch_complete")
                return unbound, "sha2"
            calls.append("commit_complete")
            return None
        ls._run = complete_run
        completed = ls.complete_execution("UNBOUND456", HWID_B, "12345", token)
        check("successful execution records activation", completed.get("completed") is True and unbound[1]["Activated"] is not None)
        check("successful execution increments counter", unbound[1]["Executions"] == 1 and completed.get("executions") == 1)
    finally:
        ls._run = original

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())

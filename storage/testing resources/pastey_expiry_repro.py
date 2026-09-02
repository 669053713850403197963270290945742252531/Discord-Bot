"""
Standalone repro for the "paste with expires_at 404s immediately" issue --
talks to api.pastey.gg directly, no bot involved, to isolate whether this
is something in the bot's request or the live service itself.

Usage: python3 pastey_expiry_repro.py
Needs: pip install aiohttp --break-system-packages   (skip if already installed)
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import aiohttp

BASE_URL = "https://api.pastey.gg"


async def main():
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Local machine's idea of UTC now: {now.isoformat()}")
    print(f"Sending expires_at:              {expires_at}")
    print()

    body = {
        "files": [{"content": "pastey.gg expiry repro -- safe to ignore/delete", "name": None, "language": None}],
        "expires_at": expires_at,
    }

    async with aiohttp.ClientSession() as session:
        print(f"POST {BASE_URL}/pastes")
        print(f"Body: {json.dumps(body)}")
        async with session.post(f"{BASE_URL}/pastes", json=body) as resp:
            print(f"Status: {resp.status}")
            data = await resp.json()
            print(f"Response: {json.dumps(data, indent=2)}")

        if resp.status not in (200, 201):
            print("\nCreate failed -- stopping here, nothing to fetch.")
            return

        paste_id = data.get("id")
        print(f"\nCreated paste id={paste_id}, expires_at (as stored/echoed)={data.get('expires_at')}")

        print(f"\nImmediately fetching GET {BASE_URL}/pastes/{paste_id} ...")
        async with session.get(f"{BASE_URL}/pastes/{paste_id}") as resp2:
            print(f"Status: {resp2.status}")
            try:
                data2 = await resp2.json()
                print(f"Response: {json.dumps(data2, indent=2)}")
            except aiohttp.ContentTypeError:
                text = await resp2.text()
                print(f"Non-JSON response: {text[:500]}")

        if resp2.status == 404:
            print(
                "\n==> Reproduced outside the bot entirely. This means it's api.pastey.gg's "
                "live behavior (or its actual deployed version vs. GitHub HEAD), not something "
                "in the bot's request construction -- the bot is sending exactly this same shape."
            )
        else:
            print(
                "\n==> Did NOT reproduce here. That would point back at something specific to "
                "the bot's actual runtime request (worth diffing this script's `body` against "
                "what the bot's create_paste() actually sends over the wire, e.g. via a print "
                "right before session.post in pastey_gg.py)."
            )


if __name__ == "__main__":
    asyncio.run(main())

# Celestial License Client

This directory contains the public Roblox/Potassium loader. It intentionally contains **no server secret**. Render serves `License Client.luau` through the bot's `/client` endpoint and substitutes the API base URL at request time.

## Normal user invocation

```lua
getgenv().script_key = "YOUR_LICENSE_KEY"
loadstring(game:HttpGet("https://YOUR-RENDER-HOST.onrender.com/client"))()
```

The client obtains the Potassium HWID with `gethwid()`, hashes it with `crypt.hash(..., "sha256")`, gets a short-lived one-use challenge, and posts the license key, HWID hash, and current `game.PlaceId` to `/whitelist/check`.

The server checks Users.json, enforces the license's `Games` list and expiration, binds an empty HWID on the first successful launch, commits that change to GitHub, then fetches the protected game payload from the authenticated storage repository.

## Server configuration

Set `LICENSE_GAME_SCRIPTS_FILE` only when you need a non-default path. By default the bot reads `storage/license_game_scripts.json`. The values are paths in `GITHUB_STORAGE_REPO`; keep that repository private if the game scripts must not be publicly downloadable.

Example:

```json
{"123456789":"storage/games/my_game.lua"}
```

A license entry with `Games: ["*"]` is unrestricted. Otherwise `Games` must contain the current Roblox PlaceId.

## Security boundary

The client is public by design. There is no HMAC/shared secret in it. A first HWID claim cannot be cryptographically authenticated by a public client; therefore the server treats the license key as the claim credential and binds the first valid HWID it receives. Keep license keys private. The protected game source is not shipped in the client or the public `/client` response; it is fetched server-side only after the license check succeeds.

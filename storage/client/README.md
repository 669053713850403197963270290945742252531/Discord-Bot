# Celestial License Client

This public client contains no server secrets. It requests a short-lived challenge, sends the license key + executor HWID + Roblox PlaceId to the license server, and only receives the protected game payload after the server authorizes the request.

Protected game scripts are stored in the private Supabase Storage `game-scripts` bucket. The license server uses its server-only Supabase credential to retrieve the configured object; Roblox never receives Supabase credentials or direct bucket access.

The protected game-script mapping lives in the Supabase `games` table, whose `script_path` points into the private `game-scripts` bucket. The control-panel loader is stored at `loader.lua` in the same bucket.

```json
{
  "123974602339071": "baseplate.luau"
}
```

The public `/client` endpoint fills its own API base URL at request time, so users can run the normal two-line loader without editing the client.

## Executor capability detection

Before a protected game script runs, the License Client loads Quartz and runs `Tester:TestAll()` with Quartz polyfills disabled, so the resulting `executor_functions` table reports the executor capabilities being tested rather than Quartz replacements. The protected script can check values such as `executor_functions.fireclickdetector`. The client also exposes `auth.getExecutorFunctions()` and `auth.isExecutorFunctionSupported(name)`. These values are informational only and are not trusted for license authorization.


## Session heartbeat
The License Client starts a server-side session heartbeat after successful `/whitelist/check`. The heartbeat uses the server-issued session UUID and the authenticated HWID, and it does not send the license key. The server considers a session active while `last_seen` is within `LICENSE_SESSION_TIMEOUT_SECONDS`; a session that remains silent beyond that window expires and must authenticate again. Transient heartbeat request failures are ignored so ordinary connectivity interruptions do not immediately kick the user. If the server disables the license, the next successful heartbeat reports `license_disabled` and the client terminates the session.

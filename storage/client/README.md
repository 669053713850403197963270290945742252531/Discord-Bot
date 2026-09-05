# Celestial License Client

This public client contains no server secrets. It requests a short-lived challenge, sends the license key + executor HWID + Roblox PlaceId to the license server, and only receives the protected game payload after the server authorizes the request.

Protected game scripts are stored in the private Supabase Storage `game-scripts` bucket. The license server uses its server-only Supabase credential to retrieve the configured object; Roblox never receives Supabase credentials or direct bucket access.

The PlaceId-to-object mapping lives in the server-only `storage/license_game_scripts.json` file. For example:

```json
{
  "123974602339071": "baseplate.luau"
}
```

The public `/client` endpoint fills its own API base URL at request time, so users can run the normal two-line loader without editing the client.

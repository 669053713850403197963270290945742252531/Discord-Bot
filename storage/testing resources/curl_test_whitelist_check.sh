#!/usr/bin/env bash
# Usage: ./curl_test_whitelist_check.sh <base-url> <key> <hwid> <place-id>
set -euo pipefail
URL="${1:?Usage: $0 <base-url> <key> <hwid> <place-id>}"
KEY="${2:?Usage: $0 <base-url> <key> <hwid> <place-id>}"
HWID="${3:?Usage: $0 <base-url> <key> <hwid> <place-id>}"
GAME_ID="${4:?Usage: $0 <base-url> <key> <hwid> <place-id>}"

CHALLENGE=$(curl -fsS -X POST "$URL/whitelist/challenge" -H 'Content-Type: application/json' -d '{}')
NONCE=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["nonce"])' "$CHALLENGE")
BODY=$(python3 -c 'import json,sys,time; print(json.dumps({"key":sys.argv[1],"hwid":sys.argv[2],"game_id":int(sys.argv[3]),"timestamp":int(time.time()),"nonce":sys.argv[4]}))' "$KEY" "$HWID" "$GAME_ID" "$NONCE")

echo "POST $URL/whitelist/check"
echo "$BODY"
echo
curl -sS -i -X POST "$URL/whitelist/check" \
  -H 'Content-Type: application/json' \
  -H 'Cache-Control: no-cache' \
  -d "$BODY"
echo

-- Celestial public license client (Potassium)
-- No server secret is embedded here. The URL is public by design.

local HttpService = game:GetService("HttpService")

local ENDPOINT = "__LICENSE_API_BASE__"
local CHALLENGE_ENDPOINT = ENDPOINT .. "/whitelist/challenge"
local CHECK_ENDPOINT = ENDPOINT .. "/whitelist/check"

local ENV = getgenv()
local key = ENV.script_key

local function fail(reason)
    warn("[Celestial] License check failed: " .. tostring(reason))
    return false
end

if type(key) ~= "string" or key == "" then
    return fail("missing_script_key")
end

local function httpPost(url, body)
    local ok, response = pcall(request, {
        Url = url,
        Method = "POST",
        Headers = {
            ["Content-Type"] = "application/json",
            ["Cache-Control"] = "no-cache",
        },
        Body = body,
    })
    if not ok or type(response) ~= "table" then
        return nil, "request_failed"
    end
    return response, nil
end

local function decode(body)
    local ok, value = pcall(HttpService.JSONDecode, HttpService, body)
    if not ok or type(value) ~= "table" then
        return nil
    end
    return value
end

local function get_hwid()
    if type(gethwid) ~= "function" then
        return nil, "hwid_api_unavailable"
    end
    if not crypt or type(crypt.hash) ~= "function" then
        return nil, "hash_api_unavailable"
    end

    local ok, raw = pcall(gethwid)
    if not ok or type(raw) ~= "string" or raw == "" then
        return nil, "hwid_unavailable"
    end

    local hashedOk, hashed = pcall(crypt.hash, raw, "sha256")
    if not hashedOk or type(hashed) ~= "string" or #hashed ~= 64 then
        return nil, "hwid_hash_failed"
    end

    return string.lower(hashed)
end

local hwid, hwidError = get_hwid()
if not hwid then
    return fail(hwidError)
end

local challengeResponse, challengeError = httpPost(CHALLENGE_ENDPOINT, "{}")
if not challengeResponse then
    return fail(challengeError)
end

local challenge = decode(challengeResponse.Body or "")
if not challenge or type(challenge.nonce) ~= "string" then
    return fail("bad_challenge")
end

local payload = {
    key = key,
    hwid = hwid,
    game_id = game.PlaceId,
    timestamp = os.time(),
    nonce = challenge.nonce,
}

local body = HttpService:JSONEncode(payload)
local response, requestError = httpPost(CHECK_ENDPOINT, body)
if not response then
    return fail(requestError)
end

local decoded = decode(response.Body or "")
if not decoded then
    return fail("bad_server_response")
end

if decoded.allowed ~= true then
    return fail(decoded.reason or "denied")
end

if type(decoded.payload) ~= "string" then
    return fail("missing_payload")
end

if not crypt or type(crypt.base64decode) ~= "function" then
    return fail("base64_api_unavailable")
end

local payloadOk, source = pcall(crypt.base64decode, decoded.payload)
if not payloadOk or type(source) ~= "string" or source == "" then
    return fail("payload_decode_failed")
end

local chunk, compileError = loadstring(source)
if type(chunk) ~= "function" then
    return fail("payload_compile_failed: " .. tostring(compileError))
end

local runOk, runError = pcall(chunk)
if not runOk then
    return fail("payload_runtime_failed: " .. tostring(runError))
end

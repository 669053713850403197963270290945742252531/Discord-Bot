-- Celestial public license client (Potassium)
-- This file intentionally contains no server credential.

local Players = game:GetService("Players")
local HttpService = game:GetService("HttpService")

local ENDPOINT = "__LICENSE_API_BASE__"
local CHALLENGE_ENDPOINT = ENDPOINT .. "/whitelist/challenge"
local CHECK_ENDPOINT = ENDPOINT .. "/whitelist/check"
local COMPLETE_ENDPOINT = ENDPOINT .. "/whitelist/complete"

local ENV = getgenv()
local key = ENV.script_key
local localPlayer = Players.LocalPlayer

local REASONS = {
    missing_script_key = "License key was not provided.",
    request_failed = "Unable to contact the license server.",
    challenge_http = "Unable to start authentication with the license server.",
    bad_challenge = "The license server returned an invalid authentication challenge.",
    stale_timestamp = "Authentication request expired. Please execute the loader again.",
    invalid_nonce = "Authentication challenge was invalid or already used.",
    invalid_key = "The supplied license key is invalid.",
    not_whitelisted = "This license key is not whitelisted.",
    hwid_api_unavailable = "Your executor does not provide the required HWID API.",
    hash_api_unavailable = "Your executor does not provide the required hashing API.",
    hwid_unavailable = "Unable to read this device's HWID.",
    hwid_hash_failed = "Unable to securely process this device's HWID.",
    invalid_hwid_format = "Your device HWID could not be validated.",
    hwid_mismatch = "Unauthorized key sharing detected: this license key is bound to another device.",
    game_not_configured = "This game is not configured for Celestial licensing.",
    game_not_authorized = "Your license is not authorized for this game.",
    game_script_not_configured = "The script for this game is not configured correctly.",
    game_script_unavailable = "The script for this game is temporarily unavailable.",
    expired = "This license has expired.",
    backend_unavailable = "The license service is temporarily unavailable.",
    rate_limited = "Too many license requests were made. Please try again shortly.",
    bad_server_response = "The license server returned an invalid response.",
    missing_payload = "The license server did not provide the protected script.",
    base64_api_unavailable = "Your executor does not provide the required payload decoder.",
    payload_decode_failed = "The protected script could not be decoded.",
    payload_compile_failed = "The protected script could not be compiled.",
    payload_runtime_failed = "The protected script failed during startup.",
    invalid_execution_token = "The execution authorization was invalid.",
    execution_token_expired = "The execution authorization expired.",
    execution_token_mismatch = "The execution authorization did not match this session.",
}

local function reason_text(reason)
    return REASONS[reason] or ("License authentication failed (" .. tostring(reason) .. ").")
end

local function reject(reason)
    if reason == "hwid_mismatch" then
        print("[Celestial][SECURITY] Unauthorized key sharing detected.")
        print("[Celestial][SECURITY] The supplied license key is bound to another HWID.")
    end

    local message = reason_text(reason)
    warn("[Celestial] " .. message)

    -- Every authentication failure is terminal for the live game session.
    -- This is deliberately local: the server cannot directly kick a Roblox
    -- player from an executor-side HTTP request.
    if localPlayer then
        pcall(function()
            localPlayer:Kick("Celestial License: " .. message)
        end)
    end

    return false
end

if type(key) ~= "string" or key == "" then
    return reject("missing_script_key")
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
    if type(body) ~= "string" or body == "" then
        return nil
    end

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
    return reject(hwidError)
end

local challengeResponse, challengeError = httpPost(CHALLENGE_ENDPOINT, "{}")
if not challengeResponse then
    return reject(challengeError)
end

if challengeResponse.Success ~= true then
    return reject("challenge_http")
end

local challenge = decode(challengeResponse.Body or "")
if not challenge or type(challenge.nonce) ~= "string" or challenge.nonce == "" then
    return reject("bad_challenge")
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
    return reject(requestError)
end

local decoded = decode(response.Body or "")
if not decoded then
    return reject("bad_server_response")
end

if decoded.allowed ~= true then
    return reject(decoded.reason or "bad_server_response")
end

if type(decoded.payload) ~= "string" or decoded.payload == "" then
    return reject("missing_payload")
end

if type(decoded.execution_token) ~= "string" or decoded.execution_token == "" then
    return reject("invalid_execution_token")
end

if not crypt or type(crypt.base64decode) ~= "function" then
    return reject("base64_api_unavailable")
end

local payloadOk, source = pcall(crypt.base64decode, decoded.payload)
if not payloadOk or type(source) ~= "string" or source == "" then
    return reject("payload_decode_failed")
end

local chunk, compileError = loadstring(source)
if type(chunk) ~= "function" then
    return reject("payload_compile_failed")
end

local runOk, runError = pcall(chunk)
if not runOk then
    return reject("payload_runtime_failed")
end

-- Only after the protected script actually returned successfully do we tell
-- the server to record Activated (first success) and increment Executions.
local completionPayload = {
    key = key,
    hwid = hwid,
    game_id = game.PlaceId,
    timestamp = os.time(),
    execution_token = decoded.execution_token,
}

local completionBody = HttpService:JSONEncode(completionPayload)
local completionResponse, completionError = httpPost(COMPLETE_ENDPOINT, completionBody)
if not completionResponse then
    return reject(completionError)
end

local completion = decode(completionResponse.Body or "")
if not completion or completion.completed ~= true then
    return reject(completion and completion.reason or "bad_server_response")
end

print("[Celestial] License authenticated successfully.")

-- Celestial public license client (Potassium)
-- This file intentionally contains no server credential.

local Players = game:GetService("Players")
local HttpService = game:GetService("HttpService")

local ENDPOINT = "__LICENSE_API_BASE__"
local CHALLENGE_ENDPOINT = ENDPOINT .. "/whitelist/challenge"
local CHECK_ENDPOINT = ENDPOINT .. "/whitelist/check"
local COMPLETE_ENDPOINT = ENDPOINT .. "/whitelist/complete"
local BREACH_ENDPOINT = ENDPOINT .. "/whitelist/breach"
local TAMPER_DETECTION_ENABLED = tostring("__LICENSE_TAMPER_DETECTION_ENABLED__") == "true"

local originalGetgenv = getgenv
local ENV = originalGetgenv()
local GAME = game
local key = ENV.script_key
local localPlayer = Players.LocalPlayer

-- Capture security-sensitive functions and values before authentication.
-- These references are used for the actual license flow instead of repeatedly
-- resolving mutable executor globals. The monitor can then detect later
-- replacement/hooking of those globals or closures. No client-side check can
-- guarantee that a sufficiently privileged external process cannot observe or
-- alter execution at a lower layer.
local originalRequest = ENV.request
local originalGetHwid = ENV.gethwid
local originalIdentifyExecutor = ENV.identifyexecutor
local originalLoadstring = ENV.loadstring
if type(originalLoadstring) ~= "function" then
    originalLoadstring = loadstring
end

local originalCrypt = ENV.crypt
local originalCryptHash = type(originalCrypt) == "table" and originalCrypt.hash or nil
local originalBase64Decode = type(originalCrypt) == "table" and originalCrypt.base64decode or nil

local originalJSONEncode = HttpService.JSONEncode
local originalJSONDecode = HttpService.JSONDecode
local originalKick = localPlayer and localPlayer.Kick or nil

local originalGetFunctionHash = ENV.getfunctionhash
local originalIsFunctionHooked = ENV.isfunctionhooked
local originalRestoreFunction = ENV.restorefunction
local authenticatedHwid = nil

local trackedFunctions = {}

local function capture_function(name, fn)
    if type(fn) ~= "function" then
        return nil
    end

    local snapshot = {
        name = name,
        fn = fn,
        hash = nil,
        flags = {},
    }

    if type(originalGetFunctionHash) == "function" then
        pcall(function()
            snapshot.hash = originalGetFunctionHash(fn)
        end)
    end

    local closureChecks = {
        {name = "executor_closure", fn = ENV.isexecutorclosure},
        {name = "c_closure", fn = ENV.iscclosure},
        {name = "lua_closure", fn = ENV.islclosure},
        {name = "new_c_closure", fn = ENV.isnewcclosure},
    }
    for _, check in ipairs(closureChecks) do
        if type(check.fn) == "function" then
            local ok, value = pcall(check.fn, fn)
            if ok and type(value) == "boolean" then
                snapshot.flags[check.name] = value
            end
        end
    end

    trackedFunctions[#trackedFunctions + 1] = snapshot
    return snapshot
end

if TAMPER_DETECTION_ENABLED then
    capture_function("getgenv", originalGetgenv)
    capture_function("request", originalRequest)
    capture_function("gethwid", originalGetHwid)
    capture_function("identifyexecutor", originalIdentifyExecutor)
    capture_function("loadstring", originalLoadstring)
    capture_function("crypt.hash", originalCryptHash)
    capture_function("crypt.base64decode", originalBase64Decode)
    capture_function("HttpService.JSONEncode", originalJSONEncode)
    capture_function("HttpService.JSONDecode", originalJSONDecode)
    capture_function("Player.Kick", originalKick)

end

-- Optional loader mode:
--   "normal" (default) = authenticate and load the protected script.
--   "info" = authenticate and expose the user API without loading/executing
--            the protected script or recording an execution.
local mode = select(1, ...)
if mode == nil then
    mode = "normal"
else
    mode = string.lower(tostring(mode))
end
local infoOnly = mode == "info" or mode == "no-load" or mode == "no_load"

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
    potential_circumvention = "Potential circumvention detected. Your license has been disabled.",
    game_not_configured = "This game is not configured for Celestial licensing.",
    game_not_supported = "This game is not currently supported by Celestial.",
    game_disabled = "This game is currently disabled by Celestial.",
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

-- Public API for scripts that load this client. The backing user record stays
-- in this module's private scope; getUser() returns a copy rather than the
-- internal table so callers cannot mutate authentication state directly.
local userRecord = nil

local function copy_array(source)
    if type(source) ~= "table" then
        return {}
    end
    local out = {}
    for index, value in ipairs(source) do
        out[index] = value
    end
    return out
end

local function copy_user(source)
    if type(source) ~= "table" then
        return nil
    end

    local out = {}
    for keyName, value in pairs(source) do
        if keyName == "Games" then
            out[keyName] = copy_array(value)
        else
            out[keyName] = value
        end
    end
    return out
end

local function parse_time(value)
    if type(value) ~= "string" or value == "" then
        return nil
    end

    local ok, timestamp = pcall(function()
        return DateTime.fromIsoDate(value).UnixTimestamp
    end)
    if ok and type(timestamp) == "number" then
        return timestamp
    end
    return nil
end

local auth = {}
local kicked = false

function auth.isKicked()
    return kicked == true
end

function auth.isUser()
    return userRecord ~= nil
end

function auth.getUser()
    return copy_user(userRecord)
end

function auth.getIdentifier()
    return userRecord and userRecord.Identifier or nil
end

function auth.getDiscordId()
    return userRecord and userRecord.DiscordId or nil
end

function auth.getKey()
    return userRecord and userRecord.Key or nil
end

function auth.getRank()
    return userRecord and userRecord.Rank or nil
end

function auth.getGames()
    return userRecord and copy_array(userRecord.Games) or {}
end

function auth.getExecutions()
    return userRecord and tonumber(userRecord.Executions) or 0
end

function auth.getHwid()
    return userRecord and userRecord.HWID or nil
end

function auth.getHwidStatus()
    return auth.getHwid() ~= nil and auth.getHwid() ~= ""
end

function auth.getTotalHwidResets()
    return userRecord and tonumber(userRecord.totalHwidResets) or 0
end

function auth.getLastHwidReset()
    return userRecord and userRecord.LastHwidReset or nil
end

function auth.getActivatedAt()
    return userRecord and userRecord.Activated or nil
end

function auth.getCreatedAt()
    return userRecord and userRecord.CreatedAt or nil
end

function auth.getUpdatedAt()
    return userRecord and userRecord.UpdatedAt or nil
end

function auth.getExpiresAt()
    return userRecord and userRecord.ExpiresAt or nil
end

function auth.getTimeLeft()
    local expiresAt = auth.getExpiresAt()
    local expiresUnix = parse_time(expiresAt)
    if not expiresUnix then
        return nil
    end

    return math.max(0, expiresUnix - os.time())
end

function auth.isExpired()
    local timeLeft = auth.getTimeLeft()
    return timeLeft ~= nil and timeLeft <= 0
end

function auth.isEnabled()
    return userRecord ~= nil and userRecord.Enabled == true
end

function auth.getNotes()
    return userRecord and userRecord.Notes or nil
end

function auth.getExecutor()
    if type(originalIdentifyExecutor) ~= "function" then
        return nil
    end

    local ok, executorName, executorVersion = pcall(originalIdentifyExecutor)
    if not ok then
        return nil
    end

    if executorVersion and executorVersion ~= "" then
        return tostring(executorName) .. " " .. tostring(executorVersion)
    end

    return executorName
end


local function get_device()
    local ok, UserInputService = pcall(GAME.GetService, GAME, "UserInputService")
    if not ok or not UserInputService then
        return "Unknown"
    end

    if UserInputService.TouchEnabled and not UserInputService.KeyboardEnabled then
        return "Mobile"
    end

    if UserInputService.GamepadEnabled and not UserInputService.KeyboardEnabled then
        return "Console"
    end

    if UserInputService.KeyboardEnabled then
        return "PC"
    end

    return "Unknown"
end

local breachReported = false
local tamperMonitorStarted = false

local function check_function_integrity(snapshot, currentFn)
    if not snapshot then
        return nil
    end

    if currentFn ~= snapshot.fn then
        return snapshot.name .. "_reference_changed"
    end

    if type(originalIsFunctionHooked) == "function" then
        local ok, hooked = pcall(originalIsFunctionHooked, snapshot.fn)
        if ok and hooked == true then
            return snapshot.name .. "_hooked"
        end
    end

    if snapshot.hash and type(originalGetFunctionHash) == "function" then
        local ok, currentHash = pcall(originalGetFunctionHash, snapshot.fn)
        if ok and type(currentHash) == "string" and currentHash ~= snapshot.hash then
            return snapshot.name .. "_function_hash_changed"
        end
    end

    for flagName, originalValue in pairs(snapshot.flags) do
        local flagFn
        if flagName == "executor_closure" then
            flagFn = ENV.isexecutorclosure
        elseif flagName == "c_closure" then
            flagFn = ENV.iscclosure
        elseif flagName == "lua_closure" then
            flagFn = ENV.islclosure
        elseif flagName == "new_c_closure" then
            flagFn = ENV.isnewcclosure
        end

        if type(flagFn) == "function" then
            local ok, value = pcall(flagFn, snapshot.fn)
            if ok and type(value) == "boolean" and value ~= originalValue then
                return snapshot.name .. "_closure_type_changed:" .. flagName
            end
        end
    end

    return nil
end

local function current_hwid_from_originals()
    if type(originalGetHwid) ~= "function" or type(originalCryptHash) ~= "function" then
        return nil
    end

    local ok, raw = pcall(originalGetHwid)
    if not ok or type(raw) ~= "string" or raw == "" then
        return nil
    end

    local hashedOk, hashed = pcall(originalCryptHash, raw, "sha256")
    if not hashedOk or type(hashed) ~= "string" or #hashed ~= 64 then
        return nil
    end

    return string.lower(hashed)
end

local function request_tamper_reason()
    if not TAMPER_DETECTION_ENABLED then
        return nil
    end

    -- Detect modification of the protected executor environment itself.
    if type(originalGetFunctionHash) == "function" and ENV.getfunctionhash ~= originalGetFunctionHash then
        return "getfunctionhash_reference_changed"
    end
    if type(originalIsFunctionHooked) == "function" and ENV.isfunctionhooked ~= originalIsFunctionHooked then
        return "isfunctionhooked_reference_changed"
    end
    if type(originalRestoreFunction) == "function" and ENV.restorefunction ~= originalRestoreFunction then
        return "restorefunction_reference_changed"
    end

    local currentLoadstring = ENV.loadstring
    if type(currentLoadstring) ~= "function" then
        currentLoadstring = loadstring
    end

    local currentGetgenv = ENV.getgenv
    if type(currentGetgenv) ~= "function" then
        currentGetgenv = getgenv
    end

    local currentFunctions = {
        getgenv = currentGetgenv,
        request = ENV.request,
        gethwid = ENV.gethwid,
        identifyexecutor = ENV.identifyexecutor,
        loadstring = currentLoadstring,
        crypt_hash = type(ENV.crypt) == "table" and ENV.crypt.hash or nil,
        crypt_base64decode = type(ENV.crypt) == "table" and ENV.crypt.base64decode or nil,
        json_encode = HttpService.JSONEncode,
        json_decode = HttpService.JSONDecode,
        kick = localPlayer and localPlayer.Kick or nil,
    }

    for _, snapshot in ipairs(trackedFunctions) do
        local currentFn = currentFunctions[snapshot.name == "crypt.hash" and "crypt_hash"
            or snapshot.name == "crypt.base64decode" and "crypt_base64decode"
            or snapshot.name == "HttpService.JSONEncode" and "json_encode"
            or snapshot.name == "HttpService.JSONDecode" and "json_decode"
            or snapshot.name == "Player.Kick" and "kick"
            or snapshot.name]
        local reason = check_function_integrity(snapshot, currentFn)
        if reason then
            return reason
        end
    end

    if type(originalCrypt) == "table" and ENV.crypt ~= originalCrypt then
        return "crypt_reference_changed"
    end

    if ENV.script_key ~= key then
        return "script_key_changed"
    end

    if GAME ~= game then
        return "game_reference_changed"
    end

    -- The HWID is both a protected source and an authentication factor. Once
    -- captured, a change in the value returned by the original API is treated
    -- as circumvention even when the function reference itself is unchanged.
    if authenticatedHwid then
        local currentHwid = current_hwid_from_originals()
        if currentHwid and currentHwid ~= authenticatedHwid then
            return "gethwid_value_changed"
        end
    end

    return nil
end

local function restore_tracked_hooks()
    if type(originalIsFunctionHooked) ~= "function" or type(originalRestoreFunction) ~= "function" then
        return
    end

    for _, snapshot in ipairs(trackedFunctions) do
        local ok, hooked = pcall(originalIsFunctionHooked, snapshot.fn)
        if ok and hooked == true then
            pcall(originalRestoreFunction, snapshot.fn)
        end
    end
end

local function report_potential_circumvention(detectionReason, currentHwid)
    if not TAMPER_DETECTION_ENABLED or breachReported then
        return false
    end

    local payload = {
        key = key,
        hwid = currentHwid or "",
        game_id = tostring(GAME.PlaceId),
        reason = "potential_circumvention",
        detection = detectionReason,
        executor = auth.getExecutor() or "Unknown",
        job_id = tostring(GAME.JobId or "Unknown"),
        device = get_device(),
    }

    local encoded = originalJSONEncode(HttpService, payload)
    for attempt = 1, 2 do
        local ok, response = pcall(originalRequest, {
            Url = BREACH_ENDPOINT,
            Method = "POST",
            Headers = {
                ["Content-Type"] = "application/json",
                ["Cache-Control"] = "no-cache",
            },
            Body = encoded,
        })

        if ok and type(response) == "table" and response.Success == true then
            breachReported = true
            return true
        end

        if attempt == 1 then
            task.wait(0.25)
        end
    end

    return false
end

local reject

local function trigger_potential_circumvention(detectionReason, fallbackHwid)
    if breachReported then
        return
    end

    -- Restore detected hooks before reading the live HWID or sending the breach
    -- report. The report should use the current value from the captured
    -- gethwid/hash implementations whenever those APIs can be restored.
    restore_tracked_hooks()
    local reportHwid = current_hwid_from_originals() or fallbackHwid

    -- Report before kicking so the enforcement request can complete.
    report_potential_circumvention(detectionReason, reportHwid)
    reject("potential_circumvention")
end

local function start_tamper_monitor(currentHwid)
    if not TAMPER_DETECTION_ENABLED or tamperMonitorStarted then
        return
    end
    tamperMonitorStarted = true

    task.spawn(function()
        while not breachReported and not auth.isKicked() do
            local reason = request_tamper_reason()
            if reason then
                trigger_potential_circumvention(reason, currentHwid)
                break
            end
            task.wait(1)
        end
    end)
end

reject = function(reason)
    if reason == "hwid_mismatch" then
        print("[Celestial][SECURITY] Unauthorized key sharing detected.")
        print("[Celestial][SECURITY] The supplied license key is bound to another HWID.")
    end

    local message = reason_text(reason)
    warn("[Celestial] " .. message)

    -- Info-only mode is intended for development/UI tooling. Authentication
    -- failures are reported to the caller instead of terminating the session.
    if not infoOnly and localPlayer and type(originalKick) == "function" then
        kicked = true

        -- Restore a hooked Kick closure before invoking the captured original.
        -- The direct closure call avoids a mutable :Kick()/__namecall lookup.
        if type(originalIsFunctionHooked) == "function" then
            local ok, hooked = pcall(originalIsFunctionHooked, originalKick)
            if ok and hooked == true and type(originalRestoreFunction) == "function" then
                pcall(originalRestoreFunction, originalKick)
            end
        end

        pcall(originalKick, localPlayer, "Celestial License: " .. message)
    end

    -- Always return the auth API, even when authentication fails, so callers can
    -- safely inspect auth.isKicked() instead of trying to index a boolean.
    return auth
end

if type(key) ~= "string" or key == "" then
    return reject("missing_script_key")
end

local function httpPost(url, body)
    local ok, response = pcall(originalRequest, {
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
    if type(body) ~= "string" or body == "" or type(originalJSONDecode) ~= "function" then
        return nil
    end

    local ok, value = pcall(originalJSONDecode, HttpService, body)
    if not ok or type(value) ~= "table" then
        return nil
    end

    return value
end

local function get_hwid()
    if type(originalGetHwid) ~= "function" then
        return nil, "hwid_api_unavailable"
    end
    if type(originalCryptHash) ~= "function" then
        return nil, "hash_api_unavailable"
    end

    local ok, raw = pcall(originalGetHwid)
    if not ok or type(raw) ~= "string" or raw == "" then
        return nil, "hwid_unavailable"
    end

    local hashedOk, hashed = pcall(originalCryptHash, raw, "sha256")
    if not hashedOk or type(hashed) ~= "string" or #hashed ~= 64 then
        return nil, "hwid_hash_failed"
    end

    return string.lower(hashed)
end

local startupTamper = nil
if TAMPER_DETECTION_ENABLED then
    startupTamper = request_tamper_reason()
    if startupTamper then
        -- Restore any detected hook before collecting the live HWID or sending
        -- the breach report. This keeps the reported HWID and security request
        -- on the captured original implementations whenever the executor
        -- provides the documented restorefunction API.
        restore_tracked_hooks()
    end
end

local hwid, hwidError = get_hwid()
if not hwid then
    return reject(hwidError)
end
authenticatedHwid = hwid

if TAMPER_DETECTION_ENABLED then
    if startupTamper then
        report_potential_circumvention(startupTamper, hwid)
        return reject("potential_circumvention")
    end
    start_tamper_monitor(hwid)
end

local challengeResponse, challengeError = httpPost(CHALLENGE_ENDPOINT, "{}")
if not challengeResponse then
    return reject(challengeError)
end

if challengeResponse.Success ~= true then
    local challengeErrorBody = decode(challengeResponse.Body or "")
    return reject(challengeErrorBody and challengeErrorBody.reason or "challenge_http")
end

local challenge = decode(challengeResponse.Body or "")
if not challenge or type(challenge.nonce) ~= "string" or challenge.nonce == "" then
    return reject("bad_challenge")
end

local payload = {
    key = key,
    hwid = hwid,
    game_id = GAME.PlaceId,
    timestamp = os.time(),
    nonce = challenge.nonce,
    -- Security telemetry for server-side breach alerts. These values are
    -- client-reported and are intentionally treated as untrusted metadata
    -- by the licensing server.
    executor = auth.getExecutor() or "Unknown",
    job_id = tostring(GAME.JobId or "Unknown"),
    device = get_device(),
}

local body = originalJSONEncode(HttpService, payload)
local response, requestError = httpPost(CHECK_ENDPOINT, body)
if not response then
    return reject(requestError)
end

local decoded = decode(response.Body or "")
if not decoded then
    return reject("bad_server_response")
end

if type(decoded.reason) == "string" and decoded.allowed ~= true then
    return reject(decoded.reason)
end

if decoded.allowed ~= true then
    return reject(decoded.reason or "bad_server_response")
end

if type(decoded.user) ~= "table" then
    return reject("bad_server_response")
end

userRecord = copy_user(decoded.user)

if type(decoded.payload) ~= "string" or decoded.payload == "" then
    return reject("missing_payload")
end

if type(decoded.execution_token) ~= "string" or decoded.execution_token == "" then
    return reject("invalid_execution_token")
end

-- Info-only mode stops here: the license has been authenticated and the user
-- record is available through auth, but the protected script is never decoded,
-- compiled, executed, or completed as an execution.
if infoOnly then
    print("[Celestial] License authenticated successfully (info-only mode).")
    return auth
end

if type(originalBase64Decode) ~= "function" then
    return reject("base64_api_unavailable")
end

local payloadOk, source = pcall(originalBase64Decode, decoded.payload)
if not payloadOk or type(source) ~= "string" or source == "" then
    return reject("payload_decode_failed")
end

if type(originalLoadstring) ~= "function" then
    return reject("payload_compile_failed")
end

local chunk, _ = originalLoadstring(source)
if type(chunk) ~= "function" then
    return reject("payload_compile_failed")
end

local runOk, _ = pcall(chunk)
if not runOk then
    return reject("payload_runtime_failed")
end

-- Only after the protected script actually returned successfully do we tell
-- the server to record Activated (first success) and increment Executions.
local completionPayload = {
    key = key,
    hwid = hwid,
    game_id = GAME.PlaceId,
    timestamp = os.time(),
    execution_token = decoded.execution_token,
    executor = auth.getExecutor() or "Unknown",
    job_id = tostring(GAME.JobId or "Unknown"),
    device = get_device(),
}

local completionBody = originalJSONEncode(HttpService, completionPayload)
local completionResponse, completionError = httpPost(COMPLETE_ENDPOINT, completionBody)
if not completionResponse then
    return reject(completionError)
end

local completion = decode(completionResponse.Body or "")
if not completion then
    return reject("bad_server_response")
end
if completion.ok ~= true or completion.completed ~= true then
    return reject(completion.reason or "bad_server_response")
end

-- /whitelist/check returns the execution count before this execution is
-- recorded. Refresh the local value after successful completion so the
-- client-facing API matches the database immediately.
if type(completion.executions) == "number" then
    userRecord.Executions = completion.executions
else
    userRecord.Executions = (tonumber(userRecord.Executions) or 0) + 1
end

if userRecord.Activated == nil and completion.activated ~= nil then
    userRecord.Activated = completion.activated
end

print("[Celestial] License authenticated successfully.")

return auth

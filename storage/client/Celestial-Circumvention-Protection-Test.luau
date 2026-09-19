-- Celestial Circumvention Protection Test v2
--
-- This script is intentionally destructive when DRY_RUN=false. Use a dedicated
-- TEST license. A successful end-to-end detection should disable that license
-- server-side and kick the client.
--
-- Safe default:
--   DRY_RUN = true
--

getgenv().script_key = "nsO8CyaCJ8jQ9VBaqtyPS0aFL8bZUhDq4gx"
local auth = loadstring(game:HttpGet("http://127.0.0.1:8080/client"))(--[["info"]])

if auth.isKicked() then return end
print("license client loaded")
print("loading test..")

local ENV = getgenv()

-- Select one target at a time so it is obvious which protection fired.
-- Supported targets:
--   request
--   gethwid
--   crypt.hash
--   crypt.base64decode
--   identifyexecutor
--   loadstring
--   HttpService.JSONEncode
--   HttpService.JSONDecode
--   Player.Kick
--   script_key
--   crypt
--   getgenv
--   game
--   gethwid
local TEST_TARGET = "script_key"

-- Supported methods:
--   replace  = replace a mutable global/table reference
--   hook     = hook an existing function with hookfunction()
--   spoof    = hook gethwid so it returns a different raw HWID
--   mutate   = change a protected value such as script_key
--   status   = only print available APIs; makes no changes
local TEST_METHOD = "mutate"

local DRY_RUN = false
local RESTORE_AFTER_TEST = true
local RESTORE_DELAY = 8
local START_DELAY = 2
local CONFIRM_DESTRUCTIVE_TEST = "DISABLE_TEST_LICENSE"

local function log(message)
    print("[Celestial][Circumvention Test] " .. tostring(message))
end

local function has_function(name)
    return type(ENV[name]) == "function"
end

local function availability(ok)
    return ok and "available" or "missing"
end

local function check_apis()
    local crypt = ENV.crypt
    local localPlayer = game:GetService("Players").LocalPlayer

    log("Executor capability check:")
    log("  request: " .. availability(has_function("request")))
    log("  gethwid: " .. availability(has_function("gethwid")))
    log("  identifyexecutor: " .. availability(has_function("identifyexecutor")))
    log("  loadstring: " .. availability(has_function("loadstring")))
    log("  hookfunction: " .. availability(has_function("hookfunction")))
    log("  isfunctionhooked: " .. availability(has_function("isfunctionhooked")))
    log("  restorefunction: " .. availability(has_function("restorefunction")))
    log("  getfunctionhash: " .. availability(has_function("getfunctionhash")))
    log("  crypt: " .. availability(type(crypt) == "table"))
    log("  crypt.hash: " .. availability(type(crypt) == "table" and type(crypt.hash) == "function"))
    log("  crypt.base64decode: " .. availability(type(crypt) == "table" and type(crypt.base64decode) == "function"))
    log("  LocalPlayer.Kick: " .. availability(localPlayer ~= nil and type(localPlayer.Kick) == "function"))
end

local function wait_for_client_start()
    log("Waiting " .. tostring(START_DELAY) .. "s for the Licensing Client to initialize...")
    task.wait(START_DELAY)
end

local function new_pass_through(original)
    local replacement = function(...)
        return original(...)
    end

    if type(ENV.newcclosure) == "function" then
        local ok, wrapped = pcall(ENV.newcclosure, replacement)
        if ok and type(wrapped) == "function" then
            replacement = wrapped
        end
    end

    return replacement
end

local function make_hwid_spoof(original)
    local replacement = function(...)
        -- Deliberately return a plausible-looking but different raw value.
        -- The client should detect the hook before trusting this value.
        return "CELSTIAL_TEST_HWID_SPOOF_" .. tostring(os.clock())
    end

    if type(ENV.newcclosure) == "function" then
        local ok, wrapped = pcall(ENV.newcclosure, replacement)
        if ok and type(wrapped) == "function" then
            replacement = wrapped
        end
    end

    return replacement
end

local function hook_target(target, replacement)
    local hookfunction = ENV.hookfunction
    if type(hookfunction) ~= "function" then
        error("hookfunction() is unavailable in this executor.")
    end

    local old
    old = hookfunction(target, replacement)
    log("hookfunction() installed successfully.")
    return old
end

local function replace_global(name)
    local original = ENV[name]
    if type(original) ~= "function" then
        error("Global `" .. name .. "` is not a function.")
    end

    local replacement = new_pass_through(original)
    ENV[name] = replacement

    log(name .. " reference replaced.")
    log("Expected detection: " .. name .. "_reference_changed")

    if RESTORE_AFTER_TEST then
        task.delay(RESTORE_DELAY, function()
            if ENV[name] == replacement then
                ENV[name] = original
                log("Restored original " .. name .. " reference.")
            end
        end)
    end
end

local function replace_crypt_table()
    local original = ENV.crypt
    if type(original) ~= "table" then
        error("getgenv().crypt is not a table.")
    end

    local replacement = {}
    for key, value in pairs(original) do
        replacement[key] = value
    end

    ENV.crypt = replacement
    log("crypt table reference replaced.")
    log("Expected detection: crypt_reference_changed")

    if RESTORE_AFTER_TEST then
        task.delay(RESTORE_DELAY, function()
            if ENV.crypt == replacement then
                ENV.crypt = original
                log("Restored original crypt table.")
            end
        end)
    end
end

local function replace_game_reference()
    local original = ENV.game
    local replacement = {}
    ENV.game = replacement

    log("global game reference replaced.")
    log("Expected detection: game_reference_changed")

    if RESTORE_AFTER_TEST then
        task.delay(RESTORE_DELAY, function()
            if ENV.game == replacement then
                ENV.game = original
                log("Restored original game reference.")
            end
        end)
    end
end

local function hook_crypt_member(member, spoof)
    local crypt = ENV.crypt
    if type(crypt) ~= "table" or type(crypt[member]) ~= "function" then
        error("crypt." .. member .. " is unavailable.")
    end

    local original = crypt[member]
    local replacement = spoof and make_hwid_spoof(original) or new_pass_through(original)
    hook_target(original, replacement)

    log("Expected detection: crypt." .. member .. "_hooked")
end

local function hook_http_method(name)
    local HttpService = game:GetService("HttpService")
    local original = HttpService[name]
    if type(original) ~= "function" then
        error("HttpService." .. name .. " is unavailable.")
    end

    local replacement = new_pass_through(original)
    hook_target(original, replacement)
    log("Expected detection: HttpService." .. name .. "_hooked")
end

local function hook_kick()
    local localPlayer = game:GetService("Players").LocalPlayer
    if not localPlayer or type(localPlayer.Kick) ~= "function" then
        error("LocalPlayer.Kick is unavailable.")
    end

    local original = localPlayer.Kick
    local replacement = new_pass_through(original)
    hook_target(original, replacement)
    log("Expected detection: Player.Kick_hooked")
end

local function mutate_value(name, value)
    local original = ENV[name]
    ENV[name] = value
    log(name .. " value changed.")
    log("Expected detection: " .. name .. "_changed")

    if RESTORE_AFTER_TEST then
        task.delay(RESTORE_DELAY, function()
            if ENV[name] == value then
                ENV[name] = original
                log("Restored original " .. name .. " value.")
            end
        end)
    end
end

local function run_test()
    check_apis()

    if TEST_METHOD == "status" then
        log("Status-only mode selected. No tampering performed.")
        return
    end

    if DRY_RUN then
        log("DRY_RUN=true; no tampering performed.")
        log("Set DRY_RUN=false and keep the confirmation string unchanged for a real test.")
        return
    end

    if CONFIRM_DESTRUCTIVE_TEST ~= "DISABLE_TEST_LICENSE" then
        error("Destructive test refused: confirmation string is incorrect.")
    end

    wait_for_client_start()

    if TEST_TARGET == "request" and TEST_METHOD == "replace" then
        replace_global("request")
    elseif TEST_TARGET == "gethwid" and TEST_METHOD == "replace" then
        replace_global("gethwid")
    elseif TEST_TARGET == "gethwid" and TEST_METHOD == "hook" then
        hook_target(ENV.gethwid, new_pass_through(ENV.gethwid))
        log("Expected detection: gethwid_hooked")
    elseif TEST_TARGET == "gethwid" and TEST_METHOD == "spoof" then
        hook_target(ENV.gethwid, make_hwid_spoof(ENV.gethwid))
        log("Expected detection: gethwid_hooked (and/or gethwid_value_changed)")
    elseif TEST_TARGET == "crypt.hash" and TEST_METHOD == "replace" then
        local crypt = ENV.crypt
        local original = crypt.hash
        crypt.hash = new_pass_through(original)
        log("crypt.hash reference replaced.")
        log("Expected detection: crypt.hash_reference_changed")
    elseif TEST_TARGET == "crypt.hash" and (TEST_METHOD == "hook" or TEST_METHOD == "spoof") then
        hook_crypt_member("hash", false)
    elseif TEST_TARGET == "crypt.base64decode" and TEST_METHOD == "hook" then
        hook_crypt_member("base64decode", false)
    elseif TEST_TARGET == "identifyexecutor" and TEST_METHOD == "replace" then
        replace_global("identifyexecutor")
    elseif TEST_TARGET == "identifyexecutor" and TEST_METHOD == "hook" then
        hook_target(ENV.identifyexecutor, new_pass_through(ENV.identifyexecutor))
        log("Expected detection: identifyexecutor_hooked")
    elseif TEST_TARGET == "loadstring" and TEST_METHOD == "replace" then
        if type(ENV.loadstring) == "function" then
            replace_global("loadstring")
        else
            error("ENV.loadstring is unavailable; use a compatible executor or test via hookfunction().")
        end
    elseif TEST_TARGET == "loadstring" and TEST_METHOD == "hook" then
        local target = ENV.loadstring or loadstring
        if type(target) ~= "function" then
            error("loadstring is unavailable.")
        end
        hook_target(target, new_pass_through(target))
        log("Expected detection: loadstring_hooked")
    elseif TEST_TARGET == "HttpService.JSONEncode" and TEST_METHOD == "hook" then
        hook_http_method("JSONEncode")
    elseif TEST_TARGET == "HttpService.JSONDecode" and TEST_METHOD == "hook" then
        hook_http_method("JSONDecode")
    elseif TEST_TARGET == "Player.Kick" and TEST_METHOD == "hook" then
        hook_kick()
    elseif TEST_TARGET == "script_key" and TEST_METHOD == "mutate" then
        mutate_value("script_key", tostring(ENV.script_key or "") .. "_TAMPER_TEST")
    elseif TEST_TARGET == "crypt" and TEST_METHOD == "replace" then
        replace_crypt_table()
    elseif TEST_TARGET == "getgenv" and TEST_METHOD == "replace" then
        replace_global("getgenv")
    elseif TEST_TARGET == "game" and TEST_METHOD == "replace" then
        replace_game_reference()
    else
        error("Unsupported TEST_TARGET/TEST_METHOD combination: " .. tostring(TEST_TARGET) .. "/" .. tostring(TEST_METHOD))
    end

    log("Tamper injected. Expected flow: detection -> /whitelist/breach -> license enabled=false -> kick.")
end

local ok, err = pcall(run_test)
if not ok then
    warn("[Celestial][Circumvention Test] " .. tostring(err))
end

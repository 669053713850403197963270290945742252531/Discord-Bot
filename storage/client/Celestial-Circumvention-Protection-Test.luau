-- Celestial Circumvention Protection Test
-- IMPORTANT:
--   This script intentionally tampers with the global `request` function so the
--   Licensing Client's protection should detect it, report a breach, disable
--   the license, and kick the player.
--
-- Use a DEDICATED TEST LICENSE. A successful end-to-end test will set that
-- license's `enabled` field to false on the server.
--
-- Set DRY_RUN = true first if you only want to verify that the required
-- executor APIs are available without changing anything.

getgenv().script_key = "nsO8CyaCJ8jQ9VBaqtyPS0aFL8bZUhDq4gx"
local auth = loadstring(game:HttpGet("http://127.0.0.1:8080/client"))(--[["info"]])

if auth.isKicked() then return end

task.wait(2)

local ENV = getgenv()

-- "replace" = replace the global request reference.
-- "hook"    = hook the existing request function in-place.
-- "both"    = perform both tests sequentially.
-- "status"  = only report available APIs; make no changes.
local TEST_MODE = "replace"

-- Set this to true only when intentionally performing a destructive test.
local DRY_RUN = false

-- Set to true to automatically restore the global request reference after the
-- test. This prevents the test script itself from leaving `request` replaced.
local RESTORE_AFTER_TEST = true
local RESTORE_DELAY = 8

-- Small delay after the script starts so the Licensing Client has already
-- captured its original request and started its tamper monitor.
local START_DELAY = 2

-- Destructive-test confirmation. Leave unchanged unless you deliberately want
-- to enable a real tamper test.
local CONFIRM_DESTRUCTIVE_TEST = "DISABLE_TEST_LICENSE"

local function log(message)
    print("[Celestial][Circumvention Test] " .. tostring(message))
end

local function get_global(name)
    local value = ENV[name]
    if value == nil then
        return nil
    end
    return value
end

local function check_apis()
    local requestFn = get_global("request")

    log("Executor capability check:")
    log("  request: " .. (type(requestFn) == "function" and "available" or "missing"))
    log("  hookfunction: " .. (type(ENV.hookfunction) == "function" and "available" or "missing"))
    log("  isfunctionhooked: " .. (type(ENV.isfunctionhooked) == "function" and "available" or "missing"))
    log("  newcclosure: " .. (type(ENV.newcclosure) == "function" and "available" or "missing"))
    log("  getfunctionhash: " .. (type(ENV.getfunctionhash) == "function" and "available" or "missing"))

    return requestFn
end

local function wait_for_client_start()
    log("Waiting " .. tostring(START_DELAY) .. "s for the Licensing Client tamper monitor to start...")
    task.wait(START_DELAY)
end

local function make_pass_through_replacement(original)
    local function replacement(...)
        -- This is intentionally transparent. The only thing being changed is
        -- the identity of ENV.request, which should trigger the client's
        -- `request_reference_changed` detection.
        return original(...)
    end

    if type(ENV.newcclosure) == "function" then
        local ok, wrapped = pcall(ENV.newcclosure, replacement)
        if ok and type(wrapped) == "function" then
            return wrapped
        end
    end

    return replacement
end

local function test_reference_replacement()
    local original = ENV.request
    if type(original) ~= "function" then
        error("getgenv().request is not a function; cannot perform replacement test.")
    end

    log("Testing request reference replacement...")
    local replacement = make_pass_through_replacement(original)
    ENV.request = replacement

    log("request reference changed: " .. tostring(ENV.request ~= original))
    log("Expected client detection: request_reference_changed")

    if RESTORE_AFTER_TEST then
        task.delay(RESTORE_DELAY, function()
            if ENV.request == replacement then
                ENV.request = original
                log("Restored original request reference.")
            end
        end)
    end
end

local function test_in_place_hook()
    local requestFn = ENV.request
    local hookfunction = ENV.hookfunction

    if type(requestFn) ~= "function" then
        error("getgenv().request is not a function; cannot perform hook test.")
    end

    if type(hookfunction) ~= "function" then
        error("hookfunction() is not available in this executor.")
    end

    log("Testing in-place request hook...")

    -- Declare `old` before creating the closure so the closure captures this
    -- local rather than looking for a global named `old`.
    local old
    local replacement = function(...)
        -- Pass through to the original function so this test changes only the
        -- function's hook state.
        return old(...)
    end

    if type(ENV.newcclosure) == "function" then
        local ok, wrapped = pcall(ENV.newcclosure, replacement)
        if ok and type(wrapped) == "function" then
            replacement = wrapped
        end
    end

    old = hookfunction(requestFn, replacement)

    log("Expected client detection: request_hooked")
    if type(ENV.isfunctionhooked) == "function" then
        local ok, hooked = pcall(ENV.isfunctionhooked, requestFn)
        log("isfunctionhooked(request) = " .. tostring(ok and hooked or false))
    end
end

local function run()
    local requestFn = check_apis()

    if TEST_MODE == "status" then
        log("Status-only mode selected. No tampering performed.")
        return
    end

    if type(requestFn) ~= "function" then
        error("No request function is available.")
    end

    if DRY_RUN then
        log("DRY_RUN=true; no tampering will be performed.")
        log("Set DRY_RUN=false and CONFIRM_DESTRUCTIVE_TEST to the exact value")
        log("`DISABLE_TEST_LICENSE` to perform an end-to-end test.")
        return
    end

    if CONFIRM_DESTRUCTIVE_TEST ~= "DISABLE_TEST_LICENSE" then
        error("Destructive test refused: confirmation string is incorrect.")
    end

    wait_for_client_start()

    if TEST_MODE == "replace" then
        test_reference_replacement()
    elseif TEST_MODE == "hook" then
        test_in_place_hook()
    elseif TEST_MODE == "both" then
        test_reference_replacement()
        task.wait(3)
        -- The first test should normally cause the client to kick before this
        -- point. This branch is mainly useful for development where the first
        -- detection is intentionally suppressed.
        if ENV.request ~= requestFn then
            log("Global request was restored/changed before the hook phase.")
        end
        test_in_place_hook()
    else
        error("Unknown TEST_MODE: " .. tostring(TEST_MODE))
    end

    log("Tamper injected. Wait for the Licensing Client to detect it.")
    log("Expected enforcement: breach report -> license enabled=false -> kick.")
end

local ok, err = pcall(run)
if not ok then
    warn("[Celestial][Circumvention Test] " .. tostring(err))
end

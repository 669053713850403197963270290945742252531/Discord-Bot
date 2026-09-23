-- Celestial public license client (Potassium)
-- This file intentionally contains no server credential.

local Players = game:GetService("Players")
local HttpService = game:GetService("HttpService")
local LocalizationService = game:GetService("LocalizationService")

local ENDPOINT = "__LICENSE_API_BASE__"
local CHALLENGE_ENDPOINT = ENDPOINT .. "/whitelist/challenge"
local CHECK_ENDPOINT = ENDPOINT .. "/whitelist/check"
local COMPLETE_ENDPOINT = ENDPOINT .. "/whitelist/complete"
local BREACH_ENDPOINT = ENDPOINT .. "/whitelist/breach"
local TAMPER_DETECTION_ENABLED = tostring("__LICENSE_TAMPER_DETECTION_ENABLED__") == "true"

local originalGetgenv = getgenv
local ENV = originalGetgenv()
local GAME = game

-- Game scripts can populate this global with asset definitions. The License
-- Client observes the table after the protected script starts and synchronizes
-- its declared files into the executor workspace.
ENV.assets = {}

local originalWritefile = ENV.writefile
local originalDelfile = ENV.delfile
local originalIsfile = ENV.isfile
local originalMakefolder = ENV.makefolder
local originalReadfile = ENV.readfile
local originalIsfolder = ENV.isfolder
local originalGetCustomAsset = ENV.getcustomasset

-- `script_key` is intentionally a global supplied by the loader.
local key = script_key
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
local originalGetCountryRegionForPlayerAsync = LocalizationService.GetCountryRegionForPlayerAsync
local originalRobloxUserId = localPlayer and localPlayer.UserId or nil

local originalGetFunctionHash = ENV.getfunctionhash
local originalIsFunctionHooked = ENV.isfunctionhooked
local originalRestoreFunction = ENV.restorefunction
local authenticatedHwid = nil
local activationCountry = nil

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
    key_sharing_detected = "Potential key sharing detected. Your license has been disabled.",
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

local auth

local ASSET_ROOT = "Celestial ScriptHub/Assets/" .. tostring(GAME.PlaceId)
local ASSET_MANIFEST = ASSET_ROOT .. "/manifest.json"
local MAX_ASSETS = 64
local MAX_ASSET_NAME_LENGTH = 128
local MAX_ASSET_URL_LENGTH = 2048
local MAX_ASSET_BYTES = 8 * 1024 * 1024
local ASSET_SYNC_INTERVAL = 1

local ASSET_CONTEXTS = {
    auto = nil,
    image = {
        prefix = "image/",
    },
    audio = {
        prefix = "audio/",
    },
    video = {
        prefix = "video/",
    },
    font = {
        mime = { ["font/ttf"] = true, ["font/otf"] = true, ["application/x-font-ttf"] = true, ["application/vnd.ms-opentype"] = true },
    },
    text = {
        prefix = "text/",
    },
    data = {
        mime = { ["application/json"] = true, ["application/octet-stream"] = true },
    },
}

local MIME_EXTENSIONS = {
    ["image/png"] = ".png",
    ["image/jpeg"] = ".jpg",
    ["image/jpg"] = ".jpg",
    ["image/webp"] = ".webp",
    ["image/gif"] = ".gif",
    ["image/bmp"] = ".bmp",
    ["image/tiff"] = ".tif",
    ["image/x-icon"] = ".ico",
    ["audio/mpeg"] = ".mp3",
    ["audio/mp3"] = ".mp3",
    ["audio/ogg"] = ".ogg",
    ["audio/wav"] = ".wav",
    ["audio/x-wav"] = ".wav",
    ["audio/flac"] = ".flac",
    ["audio/aac"] = ".aac",
    ["audio/mp4"] = ".m4a",
    ["video/mp4"] = ".mp4",
    ["video/webm"] = ".webm",
    ["video/ogg"] = ".ogv",
    ["font/ttf"] = ".ttf",
    ["font/otf"] = ".otf",
    ["application/json"] = ".json",
    ["text/plain"] = ".txt",
    ["text/css"] = ".css",
    ["text/javascript"] = ".js",
}

local EXTENSION_MIME = {
    png = "image/png", jpg = "image/jpeg", jpeg = "image/jpeg", webp = "image/webp", gif = "image/gif", bmp = "image/bmp", tif = "image/tiff", tiff = "image/tiff", ico = "image/x-icon",
    mp3 = "audio/mpeg", ogg = "audio/ogg", wav = "audio/wav", flac = "audio/flac", aac = "audio/aac", m4a = "audio/mp4",
    mp4 = "video/mp4", webm = "video/webm", ogv = "video/ogg",
    ttf = "font/ttf", otf = "font/otf", json = "application/json", txt = "text/plain", css = "text/css", js = "text/javascript",
}

local assetSyncStarted = false
local assetSyncBusy = false

local function asset_count(source)
    local count = 0
    if type(source) ~= "table" then
        return count
    end

    for _ in pairs(source) do
        count += 1
    end

    return count
end

local function asset_info(message)
    print("[Celestial][Assets] " .. tostring(message))
end

local function asset_warning(message)
    warn("[Celestial][Assets] Warning: " .. tostring(message))
end

local function asset_error(message)
    warn("[Celestial][Assets] Error: " .. tostring(message))
end

local function ensure_asset_folder()
    if type(originalMakefolder) ~= "function" then
        return false, "makefolder_api_unavailable"
    end

    local okRoot = pcall(originalMakefolder, "Celestial")
    local okAssets = pcall(originalMakefolder, "Celestial/Assets")
    local okGame = pcall(originalMakefolder, ASSET_ROOT)
    if okRoot and okAssets and okGame then
        return true
    end

    if type(originalIsfolder) == "function" then
        local ok, exists = pcall(originalIsfolder, ASSET_ROOT)
        if ok and exists == true then
            return true
        end
    end

    return false, "asset_folder_unavailable"
end

local function normalize_mime_type(value)
    if type(value) ~= "string" then
        return nil
    end

    local mime = string.match(string.lower(value), "^%s*([^;%s]+)")
    if mime == "" then
        return nil
    end
    return mime
end

local function get_response_header(headers, wanted)
    if type(headers) ~= "table" then
        return nil
    end

    local target = string.lower(wanted)
    for keyName, value in pairs(headers) do
        if string.lower(tostring(keyName)) == target then
            return value
        end
    end
    return nil
end

local function extension_from_url(url)
    if type(url) ~= "string" then
        return nil
    end

    local path = string.match(url, "^[^?#]+") or url
    local extension = string.match(path, "%.([%w]+)$")
    if extension then
        extension = string.lower(extension)
        if EXTENSION_MIME[extension] then
            return "." .. extension
        end
    end
    return nil
end

local function mime_from_extension(extension)
    if type(extension) ~= "string" then
        return nil
    end
    local normalizedExtension = string.gsub(extension, "^%.", "")
    extension = string.lower(normalizedExtension)
    return EXTENSION_MIME[extension]
end

local function asset_mime_allowed(mime, context)
    context = context or "auto"
    context = string.lower(tostring(context))

    -- `auto` intentionally accepts any known or unknown MIME type. The
    -- server's Content-Type (when supplied) is still retained in the manifest.
    if context == "auto" then
        return true
    end

    local rule = ASSET_CONTEXTS[context]
    if not rule then
        return false, "unknown_asset_context"
    end

    mime = normalize_mime_type(mime)
    if not mime then
        return false, "asset_mime_unknown"
    end

    if rule.mime and rule.mime[mime] then
        return true
    end
    if rule.prefix and string.sub(mime, 1, #rule.prefix) == rule.prefix then
        return true
    end
    return false, "asset_type_mismatch"
end

local function infer_asset_extension(name, mime, url)
    local explicitExtension = string.match(name, "%.([%w]+)$")
    if explicitExtension then
        return "." .. string.lower(explicitExtension)
    end

    if mime and MIME_EXTENSIONS[mime] then
        return MIME_EXTENSIONS[mime]
    end

    return extension_from_url(url)
end

local function manifest_entry_url(entry)
    if type(entry) == "string" then
        return entry
    end
    if type(entry) == "table" then
        return entry.url
    end
    return nil
end

local function asset_file_path(name)
    return ASSET_ROOT .. "/" .. name
end

local function manifest_entry_path(name, entry)
    if type(entry) == "table" and type(entry.path) == "string" and entry.path ~= "" then
        return entry.path
    end
    return asset_file_path(name)
end

local function manifest_entry_mime(name, entry)
    if type(entry) == "table" and type(entry.mime) == "string" then
        return normalize_mime_type(entry.mime)
    end

    local extension = string.match(name, "%.([%w]+)$")
    if extension then
        return mime_from_extension(extension)
    end

    if type(entry) == "table" and type(entry.path) == "string" then
        local pathExtension = string.match(entry.path, "%.([%w]+)$")
        if pathExtension then
            return mime_from_extension(pathExtension)
        end
    end

    if type(entry) == "string" then
        return mime_from_extension(string.match(entry, "%.([%w]+)$") or "") or nil
    end

    return nil
end

local function valid_asset_name(name)
    if type(name) ~= "string" then
        return false
    end
    if name == "" or #name > MAX_ASSET_NAME_LENGTH then
        return false
    end
    if name == "manifest.json" or name == "." or name == ".." then
        return false
    end
    if string.find(name, '[%z<>:"/\\|?*]') then
        return false
    end
    return true
end

local function valid_asset_url(url)
    if type(url) ~= "string" then
        return false
    end
    if #url == 0 or #url > MAX_ASSET_URL_LENGTH then
        return false
    end
    return string.match(url, "^https://[%w%._~:/%?#%[%]@!$&'()*+,;=%%%-]+$") ~= nil
end

local function copy_asset_table(source)
    local copied = {}
    if type(source) ~= "table" then
        return copied
    end

    local count = 0
    for name, url in pairs(source) do
        if count >= MAX_ASSETS then
            break
        end
        if valid_asset_name(name) and valid_asset_url(url) then
            copied[name] = url
            count += 1
        end
    end
    return copied
end

local function load_asset_manifest()
    if type(originalReadfile) ~= "function" or type(originalIsfile) ~= "function" then
        return {}
    end

    local okFile, exists = pcall(originalIsfile, ASSET_MANIFEST)
    if not okFile or exists ~= true then
        return {}
    end

    local okRead, raw = pcall(originalReadfile, ASSET_MANIFEST)
    if not okRead or type(raw) ~= "string" or raw == "" then
        return {}
    end

    local okDecode, value = pcall(originalJSONDecode, HttpService, raw)
    if not okDecode or type(value) ~= "table" then
        return {}
    end

    local manifest = {}
    for name, entry in pairs(value) do
        if valid_asset_name(name) then
            local url = manifest_entry_url(entry)
            if valid_asset_url(url) then
                if type(entry) == "table" then
                    manifest[name] = {
                        url = url,
                        path = type(entry.path) == "string" and entry.path or asset_file_path(name),
                        mime = normalize_mime_type(entry.mime),
                        content_hash = type(entry.content_hash) == "string" and entry.content_hash or nil,
                    }
                else
                    manifest[name] = url
                end
            end
        end
    end

    return manifest
end

local function save_asset_manifest(manifest)
    if type(originalWritefile) ~= "function" or type(originalJSONEncode) ~= "function" then
        return false, "writefile_api_unavailable"
    end

    local okEncode, raw = pcall(originalJSONEncode, HttpService, manifest)
    if not okEncode or type(raw) ~= "string" then
        return false, "asset_manifest_encode_failed"
    end

    local okWrite = pcall(originalWritefile, ASSET_MANIFEST, raw)
    if not okWrite then
        return false, "asset_manifest_write_failed"
    end
    return true
end

local function asset_content_hash(data)
    if type(data) ~= "string" or data == "" then
        return nil
    end

    if type(originalCryptHash) == "function" then
        local ok, hash = pcall(originalCryptHash, data, "sha256")
        if ok and type(hash) == "string" and #hash >= 12 then
            return string.lower(hash)
        end
    end

    return nil
end

local function versioned_asset_storage_name(name, extension, contentHash)
    local baseName = name
    local existingExtension = string.match(name, "%.([%w]+)$")
    if existingExtension then
        baseName = string.sub(name, 1, -(#existingExtension + 2))
    end

    local finalExtension = extension
    if not finalExtension and existingExtension then
        finalExtension = "." .. string.lower(existingExtension)
    end

    if not finalExtension then
        finalExtension = ""
    end

    local version = contentHash and string.sub(contentHash, 1, 16) or "current"
    return baseName .. "." .. version .. finalExtension
end

local function download_asset(url)
    if type(originalRequest) ~= "function" then
        return nil, nil, "request_api_unavailable"
    end

    local ok, response = pcall(originalRequest, {
        Url = url,
        Method = "GET",
        Headers = {
            ["Cache-Control"] = "no-cache",
        },
    })
    if not ok or type(response) ~= "table" then
        return nil, nil, "asset_request_failed"
    end

    local status = tonumber(response.StatusCode or response.status_code or 0)
    if response.Success ~= true and status ~= 200 then
        return nil, nil, "asset_http_error"
    end

    local body = response.Body
    if type(body) ~= "string" or body == "" then
        return nil, nil, "asset_empty_response"
    end
    if #body > MAX_ASSET_BYTES then
        return nil, nil, "asset_too_large"
    end

    local mime = normalize_mime_type(get_response_header(response.Headers or response.headers, "content-type"))
    if not mime or mime == "application/octet-stream" then
        mime = mime_from_extension(extension_from_url(url) or "") or mime
    end

    return body, mime, nil
end

local function sync_assets(desired)
    desired = copy_asset_table(desired)
    if assetSyncBusy then
        return false, "asset_sync_busy"
    end

    assetSyncBusy = true
    local desiredCount = asset_count(desired)
    local manifest = load_asset_manifest()
    local removedCount = 0
    local downloadedCount = 0
    local updatedCount = 0

    local okFolder, folderError = ensure_asset_folder()
    if not okFolder then
        asset_error("Unable to prepare the local asset folder (" .. tostring(folderError) .. "). Assets will not be updated.")
        assetSyncBusy = false
        return false, folderError
    end

    -- Remove assets that were explicitly removed from the game's asset table.
    for name, entry in pairs(manifest) do
        if desired[name] == nil then
            if type(originalDelfile) ~= "function" then
                assetSyncBusy = false
                return false, "delfile_api_unavailable"
            end

            local path = manifest_entry_path(name, entry)
            local ok = pcall(originalDelfile, path)
            if not ok and type(originalIsfile) == "function" then
                local existsOk, exists = pcall(originalIsfile, path)
                if existsOk and exists == true then
                    asset_error("Could not remove asset '" .. tostring(name) .. "'.")
                    assetSyncBusy = false
                    return false, "asset_delete_failed"
                end
            elseif not ok and type(originalIsfile) ~= "function" then
                asset_error("Could not remove asset '" .. tostring(name) .. "' because the delete API is unavailable.")
                assetSyncBusy = false
                return false, "asset_delete_failed"
            end
            asset_info("Removed local asset '" .. tostring(name) .. "'.")
            removedCount += 1
            manifest[name] = nil
        end
    end

    -- Download only new/changed assets. Existing files stay untouched until
    -- the replacement data has been downloaded successfully.
    for name, url in pairs(desired) do
        local existingEntry = manifest[name]
        local existingUrl = manifest_entry_url(existingEntry)
        local existingPath = existingUrl and manifest_entry_path(name, existingEntry) or nil
        local existingFileMissing = false
        if existingPath and type(originalIsfile) == "function" then
            local existsOk, exists = pcall(originalIsfile, existingPath)
            existingFileMissing = not existsOk or exists ~= true
        end

        -- Legacy manifests stored only name -> URL. Re-fetch those entries once
        -- so the new metadata/path information and MIME-derived extension are
        -- persisted. Also repair a missing local file even when its URL did not
        -- change.
        if existingUrl ~= url or type(existingEntry) ~= "table" or existingFileMissing then
            local wasExisting = existingUrl ~= nil
            asset_info((wasExisting and "Updating" or "Downloading") .. " asset '" .. tostring(name) .. "'...")

            local data, mime, errorReason = download_asset(url)
            if not data then
                asset_error("Could not " .. (wasExisting and "update" or "download") .. " asset '" .. tostring(name) .. "' (" .. tostring(errorReason) .. ").")
                assetSyncBusy = false
                return false, errorReason
            end

            local extension = infer_asset_extension(name, mime, url)
            local contentHash = asset_content_hash(data)
            local storageName = versioned_asset_storage_name(name, extension, contentHash)
            if not valid_asset_name(storageName) then
                asset_error("Cannot save asset '" .. tostring(name) .. "' because its generated filename is invalid.")
                assetSyncBusy = false
                return false, "asset_name_invalid"
            end

            local path = asset_file_path(storageName)
            if type(originalWritefile) ~= "function" then
                asset_error("Cannot save asset '" .. tostring(name) .. "' because the writefile API is unavailable.")
                assetSyncBusy = false
                return false, "writefile_api_unavailable"
            end

            local okWrite = pcall(originalWritefile, path, data)
            if not okWrite then
                asset_error("Could not save asset '" .. tostring(name) .. "' to the local workspace.")
                assetSyncBusy = false
                return false, "asset_write_failed"
            end

            if wasExisting then
                local previousPath = manifest_entry_path(name, existingEntry)
                if previousPath ~= path and type(originalDelfile) == "function" then
                    pcall(originalDelfile, previousPath)
                end
            end

            manifest[name] = {
                url = url,
                path = path,
                mime = mime,
                content_hash = contentHash,
            }
            if wasExisting then
                updatedCount += 1
                asset_info("Updated local asset '" .. tostring(name) .. "'.")
            else
                downloadedCount += 1
                asset_info("Downloaded asset '" .. tostring(name) .. "'.")
            end
        end
    end

    local saved, saveError = save_asset_manifest(manifest)
    assetSyncBusy = false
    if not saved then
        asset_error("Could not save the asset manifest (" .. tostring(saveError) .. ").")
        return false, saveError
    end

    if removedCount > 0 or downloadedCount > 0 or updatedCount > 0 then
        asset_info(string.format(
            "Asset sync complete: %d available, %d downloaded, %d updated, %d removed.",
            desiredCount,
            downloadedCount,
            updatedCount,
            removedCount
        ))
    end

    return true
end

local function extract_declared_assets(source)
    local result = {}
    if type(source) ~= "string" then
        return result, false
    end

    local start = string.find(source, "%f[%a_]assets%f[^%w_]%s*=%s*{")
    if not start then
        return result, false
    end

    local openBrace = string.find(source, "{", start, true)
    if not openBrace then
        return result, true
    end

    local block = string.match(string.sub(source, openBrace), "^%b{}")
    if not block then
        return result, true
    end

    for name, url in string.gmatch(block, "%[%s*\"([^\"]+)\"%s*%]%s*=%s*\"([^\"]+)\"") do
        if valid_asset_name(name) and valid_asset_url(url) then
            result[name] = url
        end
    end
    for name, url in string.gmatch(block, "%[%s*'([^']+)'%s*%]%s*=%s*'([^']+)'") do
        if valid_asset_name(name) and valid_asset_url(url) then
            result[name] = url
        end
    end

    return copy_asset_table(result), true
end

local function current_assets_snapshot()
    return copy_asset_table(assets)
end

-- Quick asset accessor for protected game scripts.
-- Usage examples:
--     local eye = getasset("eye", "image")
--     local clickSound = getasset("click", "audio")
--     local introVideo = getasset("intro", "video")
--
-- The function returns the executor's custom-asset ID, so callers do not need
-- to know the internal `Celestial/Assets/<PlaceId>/...` path.
local function get_asset_context_mime(name, entry)
    local mime = manifest_entry_mime(name, entry)
    if mime == "application/octet-stream" then
        local url = manifest_entry_url(entry)
        mime = mime_from_extension(extension_from_url(url) or "") or mime
    end
    return mime
end

local function resolve_asset_key(name)
    if type(name) ~= "string" or name == "" then
        return nil, "invalid_asset_name"
    end

    -- Prefer an exact key match.
    if type(assets[name]) == "string" then
        return name
    end

    -- Allow callers to omit the extension, e.g. getasset("eye", "image")
    -- for a declaration such as ["eye.png"] = "https://...".
    if string.match(name, "%.([%w]+)$") then
        return nil, "asset_not_declared"
    end

    local match = nil
    for assetName, value in pairs(assets) do
        if type(assetName) == "string"
            and type(value) == "string"
            and string.match(assetName, "^" .. name:gsub("([%%%^%$%(%)%.%[%]%*%+%-%?])", "%%%1") .. "%.([%w]+)$") then
            if match ~= nil then
                return nil, "asset_name_ambiguous"
            end
            match = assetName
        end
    end

    if match then
        return match
    end
    return nil, "asset_not_declared"
end

local function getasset(name, context)
    if not valid_asset_name(name) then
        return nil, "invalid_asset_name"
    end

    context = context == nil and "auto" or string.lower(tostring(context))
    if not ASSET_CONTEXTS[context] then
        return nil, "unknown_asset_context"
    end

    -- Synchronize the complete current declaration every time an asset is
    -- requested. This makes getasset() resilient to assets being added, removed,
    -- renamed, or having their URLs changed after the protected script starts.
    -- The sync itself is cheap when nothing changed because sync_assets() only
    -- downloads new/missing/changed entries.
    local currentAssets = current_assets_snapshot()
    local syncOk, syncError = sync_assets(currentAssets)
    if not syncOk then
        return nil, syncError
    end

    local resolvedName, resolveError = resolve_asset_key(name)
    if not resolvedName then
        return nil, resolveError
    end

    local declaredUrl = currentAssets[resolvedName]
    if type(declaredUrl) ~= "string" or not valid_asset_url(declaredUrl) then
        return nil, "asset_not_declared"
    end

    local manifest = load_asset_manifest()
    local entry = manifest[resolvedName]
    if manifest_entry_url(entry) ~= declaredUrl then
        -- The declaration may have changed while the first synchronization was
        -- running. Re-synchronize the full current table rather than only this
        -- one entry so related additions/removals cannot be missed.
        local retryAssets = current_assets_snapshot()
        local retryOk, retryError = sync_assets(retryAssets)
        if not retryOk then
            return nil, retryError
        end
        declaredUrl = retryAssets[resolvedName]
        manifest = load_asset_manifest()
        entry = manifest[resolvedName]
    end

    if type(declaredUrl) ~= "string" or manifest_entry_url(entry) ~= declaredUrl then
        return nil, "asset_not_synced"
    end

    local mime = get_asset_context_mime(resolvedName, entry)
    if context ~= "auto" then
        local allowed, reason = asset_mime_allowed(mime, context)
        if not allowed then
            -- A legacy manifest may not have MIME metadata. Infer it from the
            -- stored filename before failing the context check.
            local path = manifest_entry_path(resolvedName, entry)
            local pathExtension = string.match(path, "%.([%w]+)$")
            mime = mime or mime_from_extension(pathExtension or "")
            allowed, reason = asset_mime_allowed(mime, context)
            if not allowed then
                asset_warning("Asset '" .. tostring(name) .. "' is not compatible with context '" .. tostring(context) .. "'.")
                return nil, reason
            end
        end
    end

    local path = manifest_entry_path(resolvedName, entry)
    if type(originalIsfile) == "function" then
        local ok, exists = pcall(originalIsfile, path)
        if not ok or exists ~= true then
            local retryAssets = current_assets_snapshot()
            local synced, retryError = sync_assets(retryAssets)
            if not synced then
                return nil, retryError
            end
            manifest = load_asset_manifest()
            entry = manifest[resolvedName]
            path = manifest_entry_path(resolvedName, entry)
        end
    end

    if type(originalGetCustomAsset) ~= "function" then
        return nil, "getcustomasset_api_unavailable"
    end

    local ok, assetId = pcall(originalGetCustomAsset, path)
    if not ok or type(assetId) ~= "string" or assetId == "" then
        asset_error("Could not create a local asset reference for '" .. tostring(name) .. "'.")
        return nil, "getcustomasset_failed"
    end

    return assetId
end

ENV.getasset = getasset

local function start_asset_monitor()
    if assetSyncStarted then
        return
    end
    assetSyncStarted = true

    task.spawn(function()
        while not auth.isKicked() do
            -- Reconcile the complete live assets table every interval.
            -- sync_assets() performs a manifest-level diff, so unchanged assets
            -- do not generate downloads or user-facing messages. This catches
            -- additions, removals, URL changes, and table replacement reliably.
            local current = current_assets_snapshot()
            local ok = sync_assets(current)
            if ok then
            end
            task.wait(ASSET_SYNC_INTERVAL)
        end
    end)
end

local kicked = false
auth = {}

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

local function get_country_code()
    if not localPlayer or type(originalGetCountryRegionForPlayerAsync) ~= "function" then
        return nil
    end

    local ok, country = pcall(
        originalGetCountryRegionForPlayerAsync,
        LocalizationService,
        localPlayer
    )
    if not ok or type(country) ~= "string" then
        return nil
    end

    local normalizedCountry = string.gsub(country, "[^A-Za-z]", "")
    country = string.upper(normalizedCountry)
    if #country ~= 2 then
        return nil
    end
    return country
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
        country_code = activationCountry or "",
        roblox_user_id = originalRobloxUserId,
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
activationCountry = get_country_code()

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
    country_code = activationCountry or "",
    roblox_user_id = originalRobloxUserId,
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

-- The game script can declare a simple `assets = { ["name"] = "https://..." }`
-- table. Parse that declaration before execution so required files already
-- exist when the protected script calls getcustomasset/readfile/loadfile. Runtime
-- inspection below then catches additions/removals made while the script runs.
local declaredAssets, hasAssetDeclaration = extract_declared_assets(source)

if hasAssetDeclaration and asset_count(declaredAssets) == 0 then
    asset_warning("The game script declared an assets table, but no valid HTTPS assets were found.")
end

local assetsOk, assetsError = sync_assets(declaredAssets)
if not assetsOk then
    asset_error("Asset setup could not be completed (" .. tostring(assetsError) .. ").")
    return reject("asset_sync_failed")
end

local chunk, _ = originalLoadstring(source)
if type(chunk) ~= "function" then
    return reject("payload_compile_failed")
end

local runOk, runError = pcall(chunk)
if not runOk then
    warn("[Celestial] Protected script runtime error: " .. tostring(runError))
    return reject("payload_runtime_failed")
end

local runtimeAssets = current_assets_snapshot()
local runtimeAssetsOk = sync_assets(runtimeAssets)
if not runtimeAssetsOk then
    asset_warning("One or more runtime assets could not be synchronized. Some assets may be unavailable.")
else
end
start_asset_monitor()

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
    country_code = activationCountry or "",
    roblox_user_id = originalRobloxUserId,
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

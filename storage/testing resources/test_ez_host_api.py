#!/usr/bin/env python3
"""
test_ez_host_api.py -- exercises e-z.host's API directly.

Why this exists instead of `import ez_api`:
The `ez-api` package on PyPI (https://pypi.org/project/ez-api/) installs
successfully but ships zero source files -- both the wheel and the sdist
contain only packaging metadata, no `ez_api` module. `import ez_api` will
always raise ModuleNotFoundError. This was confirmed by installing it in a
clean venv and inspecting the wheel/sdist contents directly. So there's
nothing importable to test -- this script instead hits the same three
e-z.host features (shorten, upload, paste) directly over HTTP with
`requests`, which is the one real dependency that broken package declared.

Confidence per endpoint, updated after a real test run against a live,
paid e-z.host account:
  - shorten_url  : CONFIRMED WORKING. One correction from the old docs: the
                   success response's field is "shortendUrl" (their typo,
                   missing the middle "e"), not "shortenedUrl", and the live
                   short-link domain is i.e-z.host, not i.e-z.gg. Delete
                   round trip via deletionUrl also confirmed working.
  - create_paste : CONFIRMED WORKING on the first guess -- endpoint, body
                   shape, and response fields (pasteUrl/rawUrl/deletionUrl)
                   all matched. Delete round trip confirmed working too.
  - upload_file  : CONFIRMED WORKING for real image content. Endpoint,
                   header, and field name ("file") all matched the
                   account's own ShareX export on the first try. A minimal
                   PNG succeeds and returns imageUrl/deletionUrl; delete
                   round trip via deletionUrl also confirmed working.
  - upload_file_txt : Separate, standalone test (not the same function as
                   upload_file) for a plain .txt upload against the same
                   endpoint. Reliably returns a 422 with a broken, non-JSON
                   error body -- E-Z's own error handler crashes trying to
                   report the validation failure. So despite the dashboard
                   destination being labeled "Image uploader, File
                   uploader," this endpoint appears to require actual
                   image content, not arbitrary file data. A FAIL here
                   with a 422 is the known/expected outcome, not a script
                   bug; kept as its own test (rather than folded into
                   upload_file) so a future change in this specific
                   behavior is visible on its own line in the summary.

Usage (any one of these -- try them in order if one doesn't work for you):
    1. Command-line argument:
         python3 test_ez_host_api.py YOUR_API_KEY

    2. Environment variable:
         export EZ_API_KEY="your-upload-key-from-the-e-z.host-dashboard"
         python3 test_ez_host_api.py

    3. Interactive prompt (falls back to plain, visible input() if your
       terminal doesn't support hidden input -- VS Code's console, IDLE,
       and some Windows terminals commonly break getpass() silently):
         python3 test_ez_host_api.py
"""

import io
import os
import sys
import tempfile
from getpass import getpass, GetPassWarning

import requests

BASE_URL = "https://api.e-z.host"
TIMEOUT = 15
TEST_SHORTEN_TARGET = "https://example.com"


def banner(title: str):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def show_response(resp: requests.Response):
    """Always print status + raw body -- this is the debugging signal
    when a guessed endpoint/field name doesn't match reality."""
    print(f"HTTP {resp.status_code}")
    try:
        print(resp.json())
    except ValueError:
        print(f"(non-JSON body, {len(resp.content)} bytes): {resp.text[:500]!r}")


def explain_status(resp: requests.Response):
    """Maps the documented shortener error codes to plain-English notes.
    Harmless to show for the other endpoints too."""
    notes = {
        400: "400 Bad Request -- malformed input (e.g. invalid URL).",
        401: "401 Unauthorized -- API key is wrong/expired. Check the "
             "'upload key' on your e-z.host dashboard.",
        422: "422 Unprocessable Entity -- missing/invalid required field.",
        429: "429 Too Many Requests -- rate limited, back off and retry.",
    }
    if resp.status_code in notes:
        print(f"  -> {notes[resp.status_code]}")


# ---------------------------------------------------------------------------
# 1. URL Shortener  (HIGH confidence -- confirmed schema)
# ---------------------------------------------------------------------------

def test_shorten(session: requests.Session, api_key: str):
    banner("1) Shortener: POST /shortener")
    try:
        resp = session.post(
            f"{BASE_URL}/shortener",
            headers={"Content-Type": "application/json", "key": api_key},
            json={"url": TEST_SHORTEN_TARGET},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"Network error: {e}")
        return False, None

    show_response(resp)
    explain_status(resp)

    if resp.status_code != 200:
        return False, None

    data = resp.json()
    if not data.get("success"):
        print("Response came back HTTP 200 but success=false -- treating as a failure.")
        return False, None

    # Confirmed against a live response: E-Z's actual field is "shortendUrl"
    # (their typo, missing the middle "e") -- NOT "shortenedUrl" like the
    # old e-zdocs page claimed. Keeping the old name as a fallback in case
    # they ever fix the typo server-side.
    short_url = data.get("shortendUrl") or data.get("shortenedUrl")
    print(f"Shortened URL : {short_url}")
    print(f"Deletion URL  : {data.get('deletionUrl')}  (save this -- it's the only way to delete this link later)")
    return True, data.get("deletionUrl")


def test_delete_shortened(session: requests.Session, deletion_url: str):
    """Round-trips the deletion flow so 'delete' is actually exercised too,
    not just 'create'. deletion_url already carries its own key as a query
    param per the docs, so no auth header is sent here."""
    banner("1b) Shortener: following deletionUrl to delete the test link")
    if not deletion_url:
        print("No deletion URL available (creation failed) -- skipping.")
        return False
    try:
        resp = session.get(deletion_url, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"Network error: {e}")
        return False

    show_response(resp)
    return resp.status_code == 200


# ---------------------------------------------------------------------------
# 2. File upload  (CONFIRMED WORKING for images -- .txt tested separately)
#
# Endpoint, header, and multipart field name all match the account's own
# exported ShareX custom uploader config (Settings -> "Export" on the
# e-z.host dashboard):
#   Method: POST   URL: https://api.e-z.host/files
#   Header: key: <api key>
#   Body: multipart/form-data, file form name: "file"
#   Success fields: {json:imageUrl}, {json:deletionUrl}
#   Error field:    {json:error}
#
# That request shape was never the problem. What actually distinguishes
# success from failure is the file content: a plain .txt payload reliably
# gets a 422 with a broken, non-JSON crash body (E-Z's own error handler
# throws trying to report the validation failure), while a minimal but
# valid PNG succeeds and returns a clean 200 with imageUrl/deletionUrl.
# So even though the dashboard labels this destination "Image uploader,
# File uploader," the endpoint appears to require real image bytes, not
# arbitrary file content. test_file_upload below covers the confirmed-
# working image path; test_file_upload_txt is a separate, standalone test
# for the .txt case, tracked on its own line in the summary.
# ---------------------------------------------------------------------------

# A minimal valid 1x1 transparent PNG -- just format bytes, not any
# creative work, used only to test whether the endpoint cares about real
# image content vs. a plain text file.
_MINIMAL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010800000000"
    "3a7e9b550000000a49444154789c6360000002000155a3bf3b0000000049454e44ae426082"
)


def _attempt_file_upload(session: requests.Session, api_key: str, filename: str, content: bytes, content_type: str):
    print(f"\n--- attempt: filename={filename!r}, content_type={content_type!r}, {len(content)} bytes ---")
    try:
        resp = session.post(
            f"{BASE_URL}/files",
            headers={"key": api_key},  # no Content-Type -- requests sets the multipart boundary itself
            files={"file": (filename, io.BytesIO(content), content_type)},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"Network error: {e}")
        return None

    show_response(resp)
    interesting_headers = {k: v for k, v in resp.headers.items() if k.lower() in
                            ("x-request-id", "x-ratelimit-remaining", "server", "cf-ray")}
    if interesting_headers:
        print(f"Response headers of note: {interesting_headers}")
    return resp


def test_file_upload(session: requests.Session, api_key: str):
    banner("2) Files: POST /files with a real image (PNG)")

    resp = _attempt_file_upload(session, api_key, "ez_api_test.png", _MINIMAL_PNG, "image/png")
    if resp is not None and resp.status_code == 200:
        return _report_file_success(resp)

    print(
        "\nThe image upload failed, which is new/different behavior -- the "
        "request shape still matches the account's own ShareX export "
        "exactly, so this is worth taking straight to E-Z's Discord "
        "support with the printed request/response details above."
    )
    return False, None


def test_file_upload_txt(session: requests.Session, api_key: str):
    """Dedicated, standalone test for a plain .txt upload -- deliberately
    not folded into test_file_upload above. The two exercise different
    validation paths on E-Z's side (prior testing showed .txt content
    reliably triggers a 422 with a broken, non-JSON error body, while real
    image bytes succeed cleanly), so keeping them separate means a future
    change in either one's behavior shows up on its own line in the
    summary instead of being buried inside the other test's logic."""
    banner("2c) Files: POST /files with a .txt payload (separate from the image test)")

    resp = _attempt_file_upload(
        session, api_key, "ez_api_test.txt",
        b"e-z.host API test upload -- safe to delete.\n", "text/plain",
    )
    if resp is None:
        return False, None

    if resp.status_code == 200:
        print("  -> .txt upload succeeded -- E-Z appears to now accept non-image files here.")
        return _report_file_success(resp)

    if resp.status_code == 422:
        print(
            "  -> Known/expected failure: .txt content triggers E-Z's broken "
            "422 handler (see module docstring). Counted as FAIL below "
            "since the upload itself didn't succeed, but this matches "
            "previously confirmed behavior -- it isn't a bug in this script."
        )
    else:
        print(f"  -> Unexpected status {resp.status_code} for a .txt upload -- worth a closer look.")

    return False, None


def _report_file_success(resp: requests.Response):
    data = resp.json()
    if not data.get("success", True) and "imageUrl" not in data:
        return False, None
    print(f"Image URL     : {data.get('imageUrl')}")
    print(f"Deletion URL  : {data.get('deletionUrl')}")
    return True, data.get("deletionUrl")


def test_delete_file(session: requests.Session, deletion_url: str):
    """Same deletionUrl round trip as the shortener/paste -- keeps the test
    account's file list from accumulating one PNG per run."""
    banner("2b) Files: following deletionUrl to delete the test upload")
    if not deletion_url:
        print("No deletion URL available (upload failed) -- skipping.")
        return False
    try:
        resp = session.get(deletion_url, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"Network error: {e}")
        return False

    show_response(resp)
    return resp.status_code == 200


# ---------------------------------------------------------------------------
# 3. Paste  (confirmed against a live account -- endpoint/schema as guessed
#    from the old wrapper README turned out to be correct on the first try)
# ---------------------------------------------------------------------------

def test_paste(session: requests.Session, api_key: str):
    banner("3) Paste: POST /paste")
    try:
        resp = session.post(
            f"{BASE_URL}/paste",
            headers={"Content-Type": "application/json", "key": api_key},
            json={
                "text": "This is a test paste from test_ez_host_api.py.",
                "language": "plaintext",
                "title": "API test paste",
                "description": "Safe to delete.",
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"Network error: {e}")
        return False, None

    show_response(resp)
    explain_status(resp)

    if resp.status_code != 200:
        return False, None

    data = resp.json()
    if not data.get("success"):
        return False, None

    print(f"Paste URL     : {data.get('pasteUrl')}")
    print(f"Raw URL       : {data.get('rawUrl')}")
    print(f"Deletion URL  : {data.get('deletionUrl')}")
    return True, data.get("deletionUrl")


def test_delete_paste(session: requests.Session, deletion_url: str):
    """Same deletionUrl round trip as the shortener -- pastes return the
    same shape."""
    banner("3b) Paste: following deletionUrl to delete the test paste")
    if not deletion_url:
        print("No deletion URL available (creation failed) -- skipping.")
        return False
    try:
        resp = session.get(deletion_url, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"Network error: {e}")
        return False

    show_response(resp)
    return resp.status_code == 200


# ---------------------------------------------------------------------------

def get_api_key() -> str:
    # 1. Command-line argument -- most reliable, works regardless of
    #    terminal/IDE quirks.
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()

    # 2. Environment variable.
    env_key = os.environ.get("EZ_API_KEY", "").strip()
    if env_key:
        return env_key

    # 3. Interactive prompt. getpass() needs a real terminal to hide input
    #    and just hangs/fails/produces no visible cursor in a lot of IDE
    #    consoles (VS Code, IDLE, some Windows terminals) -- if that
    #    happens, fall back to a plain, visible input() instead of leaving
    #    you stuck typing into a prompt that never receives it.
    try:
        return getpass("e-z.host API key (your dashboard 'upload key', input hidden): ").strip()
    except (GetPassWarning, Exception):
        print("(Hidden input isn't supported in this terminal -- falling back to plain, visible input.)")
        return input("e-z.host API key (your dashboard 'upload key'): ").strip()


def main():
    api_key = get_api_key()

    if not api_key:
        print("No API key provided -- aborting.")
        print("Try: python3 test_ez_host_api.py YOUR_API_KEY")
        sys.exit(1)

    session = requests.Session()
    results = {}

    ok, deletion_url = test_shorten(session, api_key)
    results["shorten_url"] = ok
    results["delete_shortened_url"] = test_delete_shortened(session, deletion_url) if ok else False

    ok, file_deletion_url = test_file_upload(session, api_key)
    results["upload_file"] = ok
    results["delete_file"] = test_delete_file(session, file_deletion_url) if ok else False

    txt_ok, txt_deletion_url = test_file_upload_txt(session, api_key)
    results["upload_file_txt"] = txt_ok
    results["delete_file_txt"] = test_delete_file(session, txt_deletion_url) if txt_ok else False

    ok, paste_deletion_url = test_paste(session, api_key)
    results["create_paste"] = ok
    results["delete_paste"] = test_delete_paste(session, paste_deletion_url) if ok else False

    banner("Summary")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  -  {name}")

    if not all(results.values()):
        print(
            "\nAny FAIL above with a 404 means the guessed endpoint path is "
            "wrong, not that your key/account is bad -- fix that one function "
            "and re-run. A 401 across the board means the key itself is the "
            "problem."
        )
        if not results.get("upload_file_txt", True):
            print(
                "Note: upload_file_txt FAILing is expected/known behavior -- "
                "a plain .txt body triggers E-Z's own broken 422 handler. "
                "It isn't a bug in this script (see the module docstring)."
            )


if __name__ == "__main__":
    main()
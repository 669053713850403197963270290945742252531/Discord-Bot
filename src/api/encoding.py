"""
Encode/decode algorithms for /encode and /decode, plus an Identify heuristic
that guesses which of them produced a given piece of text.

Design note on the three hex-based algorithms (Hexadecimal, UTF-8, UTF-16,
UTF-32): they'd be visually indistinguishable from each other as plain hex
strings, which would make Identify unable to tell them apart. To avoid that,
each uses a distinct, deliberate grouping so the *shape* of the output alone
identifies which one produced it:

    Hexadecimal  continuous, no spaces        "48656c6c6f"
    UTF-8        2-hex-digit byte groups      "48 65 6c 6c 6f"
    UTF-16       4-hex-digit code-unit groups "0048 0065 006c 006c 006f"
    UTF-32       8-hex-digit code-unit groups "00000048 00000065 ..."
"""

import base64
import codecs
import json
import re
import string
import zlib
from typing import Any, Callable, Dict, List, Tuple
from urllib.parse import quote, unquote
import quopri


# =========================================================================
# Base64 / Base64URL
# =========================================================================

def _encode_base64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _decode_base64(text: str) -> str:
    cleaned = re.sub(r"\s+", "", text.strip())
    padded = cleaned + "=" * (-len(cleaned) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError("Not a valid Base64 string.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("That Base64 decodes fine, but the resulting bytes aren't valid UTF-8 text.")


def _encode_base64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _decode_base64url(text: str) -> str:
    cleaned = re.sub(r"\s+", "", text.strip())
    padded = cleaned + "=" * (-len(cleaned) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (ValueError, base64.binascii.Error):
        raise ValueError("Not a valid Base64URL string.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("That Base64URL decodes fine, but the resulting bytes aren't valid UTF-8 text.")


# =========================================================================
# URL Encode / Quoted-Printable
# =========================================================================

def _encode_url(text: str) -> str:
    return quote(text, safe="")


def _decode_url(text: str) -> str:
    try:
        return unquote(text, errors="strict")
    except UnicodeDecodeError:
        raise ValueError("Contains %XX sequences that don't decode to valid UTF-8 text.")


def _encode_quoted_printable(text: str) -> str:
    return quopri.encodestring(text.encode("utf-8")).decode("ascii")


def _decode_quoted_printable(text: str) -> str:
    try:
        raw = quopri.decodestring(text.encode("ascii", errors="strict"))
    except UnicodeEncodeError:
        raise ValueError("Quoted-Printable text should only contain ASCII characters.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("That decodes fine as Quoted-Printable, but the resulting bytes aren't valid UTF-8 text.")


# =========================================================================
# SAML Encode (HTTP-Redirect binding: raw DEFLATE, then Base64)
# =========================================================================

def _encode_saml(text: str) -> str:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    compressed = compressor.compress(text.encode("utf-8")) + compressor.flush()
    return base64.b64encode(compressed).decode("ascii")


def _decode_saml(text: str) -> str:
    cleaned = re.sub(r"\s+", "", text.strip())
    padded = cleaned + "=" * (-len(cleaned) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError("Not valid Base64 -- SAML encoding is Base64 wrapped around raw-DEFLATEd bytes.")
    try:
        decompressed = zlib.decompress(raw, -15)
    except zlib.error as e:
        raise ValueError(f"That's valid Base64, but it isn't valid raw-DEFLATE data once decoded: {e}")
    try:
        return decompressed.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("That decompresses fine, but the resulting bytes aren't valid UTF-8 text.")


# =========================================================================
# Pretty JSON (encode = pretty-print, decode = minify)
# =========================================================================

def _encode_pretty_json(text: str) -> str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Not valid JSON: {e}")
    return json.dumps(parsed, indent=2, ensure_ascii=False)


def _decode_pretty_json(text: str) -> str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Not valid JSON: {e}")
    return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)


# =========================================================================
# Hexadecimal / UTF-8 / UTF-16 / UTF-32 (see module docstring for the
# grouping convention that keeps these four distinguishable)
# =========================================================================

def _clean_hex_input(text: str) -> str:
    cleaned = re.sub(r"[\s,]+", "", text.strip())
    if cleaned.lower().startswith("0x"):
        cleaned = cleaned[2:]
    return cleaned


def _encode_hex(text: str) -> str:
    return text.encode("utf-8").hex()


def _decode_hex(text: str) -> str:
    cleaned = _clean_hex_input(text)
    if not cleaned or len(cleaned) % 2 != 0 or not all(c in string.hexdigits for c in cleaned):
        raise ValueError("Not a valid hexadecimal string -- expected an even number of hex digits, e.g. `48656c6c6f`.")
    raw = bytes.fromhex(cleaned)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("That's valid hex, but the decoded bytes aren't valid UTF-8 text.")


def _encode_utf8_hex(text: str) -> str:
    return " ".join(f"{b:02x}" for b in text.encode("utf-8"))


def _decode_utf8_hex(text: str) -> str:
    cleaned = _clean_hex_input(text)
    if not cleaned or len(cleaned) % 2 != 0 or not all(c in string.hexdigits for c in cleaned):
        raise ValueError("Expected 2-hex-digit byte groups, e.g. `48 65 6c 6c 6f`.")
    raw = bytes.fromhex(cleaned)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("That's valid hex, but the decoded bytes aren't valid UTF-8 text.")


def _encode_utf16_hex(text: str) -> str:
    raw = text.encode("utf-16-be")
    return " ".join(f"{raw[i]:02x}{raw[i + 1]:02x}" for i in range(0, len(raw), 2))


def _decode_utf16_hex(text: str) -> str:
    cleaned = _clean_hex_input(text)
    if not cleaned or len(cleaned) % 4 != 0 or not all(c in string.hexdigits for c in cleaned):
        raise ValueError("Expected 4-hex-digit code-unit groups (one per UTF-16 unit), e.g. `0048 0065 006c`.")
    raw = bytes.fromhex(cleaned)
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError as e:
        raise ValueError(f"That's valid hex, but it isn't valid UTF-16: {e}")


def _encode_utf32_hex(text: str) -> str:
    raw = text.encode("utf-32-be")
    return " ".join(f"{raw[i]:02x}{raw[i + 1]:02x}{raw[i + 2]:02x}{raw[i + 3]:02x}" for i in range(0, len(raw), 4))


def _decode_utf32_hex(text: str) -> str:
    cleaned = _clean_hex_input(text)
    if not cleaned or len(cleaned) % 8 != 0 or not all(c in string.hexdigits for c in cleaned):
        raise ValueError("Expected 8-hex-digit code-unit groups (one per UTF-32 unit), e.g. `00000048 00000065`.")
    raw = bytes.fromhex(cleaned)
    try:
        return raw.decode("utf-32-be")
    except UnicodeDecodeError as e:
        raise ValueError(f"That's valid hex, but it isn't valid UTF-32: {e}")


# =========================================================================
# ROT13 (self-inverse -- same function handles both directions)
# =========================================================================

def _rot13(text: str) -> str:
    return codecs.encode(text, "rot13")


# =========================================================================
# Registry
# =========================================================================

ENCODING_ALGORITHMS: Dict[str, Dict[str, Any]] = {
    "base64": {"name": "Base64", "encode": _encode_base64, "decode": _decode_base64},
    "base64url": {"name": "Base64URL", "encode": _encode_base64url, "decode": _decode_base64url},
    "url": {"name": "URL Encode", "encode": _encode_url, "decode": _decode_url},
    "quoted_printable": {"name": "Quoted-Printable", "encode": _encode_quoted_printable, "decode": _decode_quoted_printable},
    "saml": {"name": "SAML Encode", "encode": _encode_saml, "decode": _decode_saml},
    "pretty_json": {"name": "Pretty JSON", "encode": _encode_pretty_json, "decode": _decode_pretty_json},
    "utf8": {"name": "UTF-8", "encode": _encode_utf8_hex, "decode": _decode_utf8_hex},
    "utf16": {"name": "UTF-16", "encode": _encode_utf16_hex, "decode": _decode_utf16_hex},
    "utf32": {"name": "UTF-32", "encode": _encode_utf32_hex, "decode": _decode_utf32_hex},
    "hex": {"name": "Hexadecimal", "encode": _encode_hex, "decode": _decode_hex},
    "rot13": {"name": "ROT13", "encode": _rot13, "decode": _rot13},
}

# "Identify" isn't a real algorithm -- it doesn't appear in
# ENCODING_ALGORITHMS and has no encode()/decode() of its own. It's handled
# specially by the command layer (see commands/utility.py), which is what
# actually calls identify_encoding() below and then dispatches to whichever
# real algorithm it guesses.
IDENTIFY_CHOICE_VALUE = "identify"

ENCODING_CHOICES: List[Tuple[str, str]] = [(v["name"], key) for key, v in ENCODING_ALGORITHMS.items()] + [("Identify", IDENTIFY_CHOICE_VALUE)]


def encode_text(algorithm_key: str, text: str) -> str:
    """Encodes `text` with the named algorithm. Raises ValueError if the key isn't recognized, or if the algorithm rejects the input (e.g. Pretty JSON on non-JSON text)."""
    entry = ENCODING_ALGORITHMS.get(algorithm_key)
    if entry is None:
        raise ValueError(f"'{algorithm_key}' isn't a supported encoding algorithm.")
    return entry["encode"](text)


def decode_text(algorithm_key: str, text: str) -> str:
    """Decodes `text` with the named algorithm. Raises ValueError if the key isn't recognized, or if `text` isn't validly encoded for that algorithm."""
    entry = ENCODING_ALGORITHMS.get(algorithm_key)
    if entry is None:
        raise ValueError(f"'{algorithm_key}' isn't a supported encoding algorithm.")
    return entry["decode"](text)


# =========================================================================
# Identify -- best-effort guess at what encoded a piece of text
# =========================================================================

_ROT13_COMMON_WORDS = {
    "the", "a", "an", "is", "are", "was", "were", "and", "to", "of", "in",
    "that", "it", "you", "this", "for", "on", "with", "as", "be", "at",
    "by", "have", "has", "not", "but", "or", "if", "your", "my", "we",
}


def identify_encoding(text: str) -> List[Tuple[str, str]]:
    """
    Returns a best-first list of (algorithm_key, reason) guesses for what
    likely produced `text`, checked roughly most-specific-first (e.g. SAML
    -- Base64 wrapping DEFLATE -- is checked before plain Base64, since a
    SAML string also happens to be syntactically valid Base64).

    Returns an empty list if nothing matched -- not a guarantee that
    nothing "worked" as a decode, just that no distinctive shape was
    recognized in the text.
    """
    stripped = text.strip()
    guesses: List[Tuple[str, str]] = []
    if not stripped:
        return guesses

    try:
        json.loads(stripped)
        guesses.append(("pretty_json", "Parses as valid JSON."))
    except (json.JSONDecodeError, ValueError):
        pass

    compact = re.sub(r"\s+", "", stripped)
    if compact and len(compact) % 2 == 0 and all(c in string.hexdigits for c in compact):
        groups = stripped.split()
        has_spaces = len(groups) > 1
        if not has_spaces:
            guesses.append(("hex", "A continuous hex string with no grouping -- matches this bot's plain Hexadecimal format."))
        elif all(len(g) == 2 for g in groups):
            guesses.append(("utf8", "Hex grouped in 2-digit bytes, space-separated -- matches this bot's UTF-8 format."))
        elif all(len(g) == 4 for g in groups):
            guesses.append(("utf16", "Hex grouped in 4-digit code units, space-separated -- matches this bot's UTF-16 format."))
        elif all(len(g) == 8 for g in groups):
            guesses.append(("utf32", "Hex grouped in 8-digit code units, space-separated -- matches this bot's UTF-32 format."))
        else:
            guesses.append(("hex", "Looks hexadecimal, though the grouping doesn't cleanly match a specific width."))

    # Real Base64 blobs are essentially never space-separated -- gating on
    # that here stops an ordinary multi-word phrase (e.g. ROT13 output,
    # which is also just letters) from spuriously matching the Base64
    # alphabet check below.
    b64_candidate = compact
    if not re.search(r"\s", stripped) and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", b64_candidate):
        try:
            raw = base64.b64decode(b64_candidate + "=" * (-len(b64_candidate) % 4), validate=True)
            try:
                zlib.decompress(raw, -15)
                guesses.append(("saml", "Valid Base64 that also decompresses as raw DEFLATE data -- matches SAML HTTP-Redirect encoding."))
            except zlib.error:
                try:
                    raw.decode("utf-8")
                    guesses.append(("base64", "Valid standard Base64 that decodes to readable UTF-8 text."))
                except UnicodeDecodeError:
                    guesses.append(("base64", "Valid standard Base64, though the decoded bytes aren't valid UTF-8 text."))
        except (ValueError, base64.binascii.Error):
            pass
    elif not re.search(r"\s", stripped) and re.fullmatch(r"[A-Za-z0-9\-_]+={0,2}", b64_candidate) and ("-" in b64_candidate or "_" in b64_candidate):
        guesses.append(("base64url", "Uses the URL-safe Base64 alphabet (- and _ instead of + and /)."))

    if re.search(r"%[0-9A-Fa-f]{2}", stripped):
        guesses.append(("url", "Contains %XX percent-encoded sequences."))

    if re.search(r"=[0-9A-Fa-f]{2}", stripped):
        guesses.append(("quoted_printable", "Contains =XX soft-encoded byte sequences, typical of Quoted-Printable."))

    if re.fullmatch(r"[A-Za-z\s.,!?'\"-]+", stripped) and re.search(r"[A-Za-z]", stripped):
        rot13_result = codecs.encode(stripped, "rot13")
        original_words = re.findall(r"[a-z']+", stripped.lower())
        rot13_words = re.findall(r"[a-z']+", rot13_result.lower())
        original_hits = sum(1 for w in original_words if w in _ROT13_COMMON_WORDS)
        rot13_hits = sum(1 for w in rot13_words if w in _ROT13_COMMON_WORDS)
        if rot13_hits > 0 and rot13_hits > original_hits:
            guesses.append(("rot13", f"Applying ROT13 reveals {rot13_hits} common English word(s) not present in the original text."))

    return guesses

"""
Encode/decode algorithms for /encode encode and /encode decode, plus an Identify heuristic
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
import html as html_module
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
# Base32 / Base58 / Base85
# =========================================================================

def _encode_base32(text: str) -> str:
    return base64.b32encode(text.encode("utf-8")).decode("ascii")


def _decode_base32(text: str) -> str:
    cleaned = re.sub(r"\s+", "", text.strip()).upper()
    padded = cleaned + "=" * (-len(cleaned) % 8)
    try:
        raw = base64.b32decode(padded)
    except (ValueError, base64.binascii.Error):
        raise ValueError("Not a valid Base32 string -- expected the A-Z2-7 alphabet, optionally padded with `=`.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("That Base32 decodes fine, but the resulting bytes aren't valid UTF-8 text.")


_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _encode_base58(text: str) -> str:
    data = text.encode("utf-8")
    if not data:
        raise ValueError("Nothing to encode.")
    n = int.from_bytes(data, "big")
    encoded = ""
    while n > 0:
        n, rem = divmod(n, 58)
        encoded = _BASE58_ALPHABET[rem] + encoded
    n_leading_zero_bytes = len(data) - len(data.lstrip(b"\x00"))
    return "1" * n_leading_zero_bytes + encoded


def _decode_base58(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("Nothing to decode.")
    if not all(c in _BASE58_ALPHABET for c in cleaned):
        raise ValueError(
            "Not a valid Base58 string -- it must only use the Bitcoin Base58 alphabet "
            "(letters and digits, excluding `0`, `O`, `I`, and `l`)."
        )
    n = 0
    for c in cleaned:
        n = n * 58 + _BASE58_ALPHABET.index(c)
    n_leading_ones = len(cleaned) - len(cleaned.lstrip("1"))
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n > 0 else b""
    raw = b"\x00" * n_leading_ones + body
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("That Base58 decodes fine, but the resulting bytes aren't valid UTF-8 text.")


def _encode_base85(text: str) -> str:
    return base64.b85encode(text.encode("utf-8")).decode("ascii")


def _decode_base85(text: str) -> str:
    cleaned = re.sub(r"\s+", "", text.strip())
    try:
        raw = base64.b85decode(cleaned)
    except ValueError:
        raise ValueError("Not a valid Base85 string.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("That Base85 decodes fine, but the resulting bytes aren't valid UTF-8 text.")


# =========================================================================
# Binary (8-bit byte groups, space-separated -- the same grouping idea as
# the hex family above, just base-2 instead of base-16)
# =========================================================================

def _encode_binary(text: str) -> str:
    return " ".join(f"{b:08b}" for b in text.encode("utf-8"))


def _decode_binary(text: str) -> str:
    cleaned = re.sub(r"[\s,]+", "", text.strip())
    if not cleaned or len(cleaned) % 8 != 0 or not all(c in "01" for c in cleaned):
        raise ValueError("Not a valid binary string -- expected 8-bit byte groups of 0s and 1s, e.g. `01001000 01100101`.")
    raw = bytes(int(cleaned[i:i + 8], 2) for i in range(0, len(cleaned), 8))
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("That's valid binary, but the decoded bytes aren't valid UTF-8 text.")


# =========================================================================
# Decimal (space-separated decimal Unicode code points)
# =========================================================================

def _encode_decimal(text: str) -> str:
    if not text:
        raise ValueError("Nothing to encode.")
    return " ".join(str(ord(ch)) for ch in text)


def _decode_decimal(text: str) -> str:
    tokens = text.split()
    if not tokens:
        raise ValueError("Expected space-separated decimal code points, e.g. `72 101 108 108 111`.")
    try:
        codes = [int(t) for t in tokens]
    except ValueError:
        raise ValueError("Expected space-separated decimal code points, e.g. `72 101 108 108 111`.")
    try:
        return "".join(chr(c) for c in codes)
    except ValueError:
        raise ValueError("One of those numbers isn't a valid Unicode code point.")


# =========================================================================
# Unicode Escape (\\uXXXX for the BMP, \\UXXXXXXXX beyond it -- Python/C-style)
# =========================================================================

_UNICODE_ESCAPE_PATTERN = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})")


def _encode_unicode_escape(text: str) -> str:
    if not text:
        raise ValueError("Nothing to encode.")
    out = []
    for ch in text:
        cp = ord(ch)
        out.append(f"\\U{cp:08x}" if cp > 0xFFFF else f"\\u{cp:04x}")
    return "".join(out)


def _decode_unicode_escape(text: str) -> str:
    cleaned = text.strip()
    if not cleaned or _UNICODE_ESCAPE_PATTERN.sub("", cleaned):
        raise ValueError("Expected only \\u/\\U Unicode escape sequences, e.g. `\\u0048\\u0065\\u006c\\u006c\\u006f`.")
    return _UNICODE_ESCAPE_PATTERN.sub(lambda m: chr(int(m.group(1) or m.group(2), 16)), cleaned)


# =========================================================================
# HTML Entities
# =========================================================================

def _encode_html(text: str) -> str:
    return html_module.escape(text, quote=True)


def _decode_html(text: str) -> str:
    return html_module.unescape(text)


# =========================================================================
# Morse Code
# =========================================================================

_MORSE_TABLE: Dict[str, str] = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "'": ".----.", "!": "-.-.--",
    "/": "-..-.", "(": "-.--.", ")": "-.--.-", "&": ".-...", ":": "---...",
    ";": "-.-.-.", "=": "-...-", "+": ".-.-.", "-": "-....-", "_": "..--.-",
    '"': ".-..-.", "$": "...-..-", "@": ".--.-.",
}
_MORSE_REVERSE: Dict[str, str] = {v: k for k, v in _MORSE_TABLE.items()}


def _encode_morse(text: str) -> str:
    out_words = []
    for word in text.split(" "):
        codes = []
        for ch in word:
            code = _MORSE_TABLE.get(ch.upper())
            if code is None:
                raise ValueError(f"'{ch}' doesn't have a Morse code mapping.")
            codes.append(code)
        if codes:
            out_words.append(" ".join(codes))
    return " / ".join(out_words)


def _decode_morse(text: str) -> str:
    out_words = []
    for word in text.split("/"):
        letters = []
        for tok in word.split():
            code = tok.strip()
            if code not in _MORSE_REVERSE:
                raise ValueError(f"'{tok}' isn't a recognized Morse code sequence.")
            letters.append(_MORSE_REVERSE[code])
        out_words.append("".join(letters))
    return " ".join(out_words)


# =========================================================================
# Braille (Grade 1 / uncontracted -- letters a-z + digits 0-9, each digit
# individually prefixed with the number sign for unambiguous round-tripping,
# since digits reuse the a-j dot patterns in real Braille)
# =========================================================================

def _dots_to_braille_char(dots) -> str:
    mask = sum(1 << (d - 1) for d in dots)
    return chr(0x2800 + mask)


_BRAILLE_LETTER_DOTS: Dict[str, Tuple[int, ...]] = {
    "a": (1,), "b": (1, 2), "c": (1, 4), "d": (1, 4, 5), "e": (1, 5),
    "f": (1, 2, 4), "g": (1, 2, 4, 5), "h": (1, 2, 5), "i": (2, 4), "j": (2, 4, 5),
    "k": (1, 3), "l": (1, 2, 3), "m": (1, 3, 4), "n": (1, 3, 4, 5), "o": (1, 3, 5),
    "p": (1, 2, 3, 4), "q": (1, 2, 3, 4, 5), "r": (1, 2, 3, 5), "s": (2, 3, 4), "t": (2, 3, 4, 5),
    "u": (1, 3, 6), "v": (1, 2, 3, 6), "w": (2, 4, 5, 6), "x": (1, 3, 4, 6),
    "y": (1, 3, 4, 5, 6), "z": (1, 3, 5, 6),
}
_BRAILLE_DIGIT_DOTS: Dict[str, Tuple[int, ...]] = {
    "1": (1,), "2": (1, 2), "3": (1, 4), "4": (1, 4, 5), "5": (1, 5),
    "6": (1, 2, 4), "7": (1, 2, 4, 5), "8": (1, 2, 5), "9": (2, 4), "0": (2, 4, 5),
}
_BRAILLE_LETTER_CHARS: Dict[str, str] = {k: _dots_to_braille_char(v) for k, v in _BRAILLE_LETTER_DOTS.items()}
_BRAILLE_DIGIT_CHARS: Dict[str, str] = {k: _dots_to_braille_char(v) for k, v in _BRAILLE_DIGIT_DOTS.items()}
_BRAILLE_LETTER_REVERSE: Dict[str, str] = {v: k for k, v in _BRAILLE_LETTER_CHARS.items()}
_BRAILLE_DIGIT_REVERSE: Dict[str, str] = {v: k for k, v in _BRAILLE_DIGIT_CHARS.items()}
_BRAILLE_SPACE = chr(0x2800)
_BRAILLE_NUMBER_CHAR = _dots_to_braille_char((3, 4, 5, 6))


def _encode_braille(text: str) -> str:
    if not text:
        raise ValueError("Nothing to encode.")
    out = []
    for ch in text:
        if ch == " ":
            out.append(_BRAILLE_SPACE)
        elif ch in _BRAILLE_DIGIT_CHARS:
            out.append(_BRAILLE_NUMBER_CHAR)
            out.append(_BRAILLE_DIGIT_CHARS[ch])
        elif ch.lower() in _BRAILLE_LETTER_CHARS:
            out.append(_BRAILLE_LETTER_CHARS[ch.lower()])
        else:
            raise ValueError(f"'{ch}' doesn't have a Braille mapping -- only letters, digits, and spaces are supported.")
    return "".join(out)


def _decode_braille(text: str) -> str:
    if not text.strip():
        raise ValueError("Nothing to decode.")
    out = []
    pending_number = False
    for ch in text:
        if ch == _BRAILLE_NUMBER_CHAR:
            pending_number = True
            continue
        if ch == _BRAILLE_SPACE or ch == " ":
            out.append(" ")
            pending_number = False
            continue
        if pending_number:
            if ch not in _BRAILLE_DIGIT_REVERSE:
                raise ValueError(f"'{ch}' isn't a valid Braille digit cell after a number sign.")
            out.append(_BRAILLE_DIGIT_REVERSE[ch])
            pending_number = False
        else:
            if ch not in _BRAILLE_LETTER_REVERSE:
                raise ValueError(f"'{ch}' isn't a recognized Braille cell.")
            out.append(_BRAILLE_LETTER_REVERSE[ch])
    return "".join(out)


# =========================================================================
# Phonetic (NATO alphabet)
# =========================================================================

_NATO_TABLE: Dict[str, str] = {
    "A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta", "E": "Echo",
    "F": "Foxtrot", "G": "Golf", "H": "Hotel", "I": "India", "J": "Juliett",
    "K": "Kilo", "L": "Lima", "M": "Mike", "N": "November", "O": "Oscar",
    "P": "Papa", "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango",
    "U": "Uniform", "V": "Victor", "W": "Whiskey", "X": "X-ray", "Y": "Yankee", "Z": "Zulu",
    "0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four",
    "5": "Five", "6": "Six", "7": "Seven", "8": "Eight", "9": "Nine",
}
_NATO_REVERSE: Dict[str, str] = {v.upper(): k for k, v in _NATO_TABLE.items()}


def _encode_phonetic(text: str) -> str:
    out_words = []
    for word in text.split(" "):
        codes = []
        for ch in word:
            code = _NATO_TABLE.get(ch.upper())
            if code is None:
                raise ValueError(f"'{ch}' doesn't have a phonetic-alphabet mapping -- only letters and digits are supported.")
            codes.append(code)
        if codes:
            out_words.append(" ".join(codes))
    return " / ".join(out_words)


def _decode_phonetic(text: str) -> str:
    out_words = []
    for word in text.split("/"):
        letters = []
        for tok in word.split():
            key = tok.strip().upper()
            if key not in _NATO_REVERSE:
                raise ValueError(f"'{tok}' isn't a recognized phonetic-alphabet word.")
            letters.append(_NATO_REVERSE[key])
        out_words.append("".join(letters))
    return " ".join(out_words)


# =========================================================================
# Emoji (regional-indicator letters + keycap digits)
# =========================================================================

def _encode_emoji(text: str) -> str:
    if not text:
        raise ValueError("Nothing to encode.")
    out = []
    for ch in text:
        if ch == " ":
            out.append(" ")
        elif "a" <= ch.lower() <= "z":
            out.append(chr(0x1F1E6 + (ord(ch.lower()) - ord("a"))))
        elif ch in "0123456789":
            out.append(ch + "\ufe0f\u20e3")
        else:
            raise ValueError(f"'{ch}' doesn't have an emoji mapping -- only letters, digits, and spaces are supported.")
    return "".join(out)


def _decode_emoji(text: str) -> str:
    if not text.strip():
        raise ValueError("Nothing to decode.")
    out = []
    chars = list(text)
    n = len(chars)
    i = 0
    while i < n:
        ch = chars[i]
        cp = ord(ch)
        if ch == " ":
            out.append(" ")
            i += 1
        elif 0x1F1E6 <= cp <= 0x1F1FF:
            out.append(chr(ord("a") + (cp - 0x1F1E6)))
            i += 1
        elif ch in "0123456789" and i + 2 < n and chars[i + 1] == "\ufe0f" and chars[i + 2] == "\u20e3":
            out.append(ch)
            i += 3
        else:
            raise ValueError(f"'{ch}' isn't a recognized emoji-encoded character.")
    return "".join(out)


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
    "base32": {"name": "Base32", "encode": _encode_base32, "decode": _decode_base32},
    "base58": {"name": "Base58", "encode": _encode_base58, "decode": _decode_base58},
    "base85": {"name": "Base85", "encode": _encode_base85, "decode": _decode_base85},
    "binary": {"name": "Binary", "encode": _encode_binary, "decode": _decode_binary},
    "decimal": {"name": "Decimal (Code Points)", "encode": _encode_decimal, "decode": _decode_decimal},
    "unicode_escape": {"name": "Unicode Escape", "encode": _encode_unicode_escape, "decode": _decode_unicode_escape},
    "html": {"name": "HTML Entities", "encode": _encode_html, "decode": _decode_html},
    "morse": {"name": "Morse Code", "encode": _encode_morse, "decode": _decode_morse},
    "braille": {"name": "Braille", "encode": _encode_braille, "decode": _decode_braille},
    "phonetic": {"name": "Phonetic Alphabet", "encode": _encode_phonetic, "decode": _decode_phonetic},
    "emoji": {"name": "Emoji", "encode": _encode_emoji, "decode": _decode_emoji},
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

_COMMON_HTML_ENTITY_NAMES = (
    "amp", "lt", "gt", "quot", "apos", "nbsp", "copy", "reg", "trade",
    "hellip", "mdash", "ndash", "eacute", "egrave", "agrave", "ccedil",
    "uuml", "ouml", "auml", "szlig", "euro", "pound", "yen", "cent",
    "sect", "para", "middot", "laquo", "raquo", "deg", "plusmn", "times",
    "divide", "frac12", "frac14", "frac34", "sup1", "sup2", "sup3",
)


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

    if re.fullmatch(r"[.\-/\s]+", stripped) and re.search(r"[.\-]", stripped):
        guesses.append(("morse", "Consists only of dots, dashes, slashes, and spaces -- matches Morse Code."))

    if any(0x2800 <= ord(c) <= 0x28FF for c in stripped):
        guesses.append(("braille", "Contains Unicode Braille Pattern characters (U+2800-U+28FF)."))

    if any(0x1F1E6 <= ord(c) <= 0x1F1FF for c in stripped) or "\ufe0f\u20e3" in stripped:
        guesses.append(("emoji", "Contains regional-indicator letter or keycap digit emoji -- matches this bot's Emoji encoding."))

    if re.search(r"&(?:#\d+|#x[0-9A-Fa-f]+|" + "|".join(_COMMON_HTML_ENTITY_NAMES) + r");", stripped):
        guesses.append(("html", "Contains `&...;` HTML entity sequences."))

    if _UNICODE_ESCAPE_PATTERN.search(stripped) and not _UNICODE_ESCAPE_PATTERN.sub("", stripped).strip():
        guesses.append(("unicode_escape", "Consists entirely of \\u/\\U Unicode escape sequences."))

    decimal_tokens = stripped.split()
    is_decimal_shaped = (
        len(decimal_tokens) >= 2
        and all(re.fullmatch(r"\d{1,7}", t) for t in decimal_tokens)
        and all(int(t) <= 0x10FFFF for t in decimal_tokens)
    )
    if is_decimal_shaped:
        guesses.append(("decimal", "Space-separated decimal numbers, all within the valid Unicode code point range -- matches this bot's Decimal (Code Points) format."))

    compact = re.sub(r"\s+", "", stripped)
    is_binary_shaped = bool(compact) and len(compact) % 8 == 0 and len(compact) >= 8 and all(c in "01" for c in compact)
    if is_binary_shaped:
        guesses.append(("binary", "Made up entirely of 0s and 1s in 8-bit groups -- matches this bot's Binary format."))
    elif not is_decimal_shaped and compact and len(compact) % 2 == 0 and all(c in string.hexdigits for c in compact):
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

    base32_candidate = compact.upper()
    if not is_binary_shaped and not re.search(r"\s", stripped) and re.fullmatch(r"[A-Z2-7]+=*", base32_candidate) and re.search(r"[2-7]", base32_candidate):
        try:
            _decode_base32(base32_candidate)
            guesses.append(("base32", "Uses only the Base32 alphabet (A-Z, 2-7), optionally `=`-padded, and decodes cleanly to UTF-8 text."))
        except ValueError:
            pass

    # Real Base64 blobs are essentially never space-separated -- gating on
    # that here stops an ordinary multi-word phrase (e.g. ROT13 output,
    # which is also just letters) from spuriously matching the Base64
    # alphabet check below.
    b64_candidate = compact
    matched_base64_family = False
    if not re.search(r"\s", stripped) and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", b64_candidate):
        try:
            raw = base64.b64decode(b64_candidate + "=" * (-len(b64_candidate) % 4), validate=True)
            matched_base64_family = True
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
        matched_base64_family = True
        guesses.append(("base64url", "Uses the URL-safe Base64 alphabet (- and _ instead of + and /)."))

    if (
        not matched_base64_family
        and not re.search(r"\s", stripped)
        and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]+", b64_candidate)
        and len(b64_candidate) >= 4
    ):
        try:
            _decode_base58(b64_candidate)
            guesses.append(("base58", "Uses only the Bitcoin Base58 alphabet (no 0, O, I, or l) and decodes cleanly to UTF-8 text."))
        except ValueError:
            pass

    if (
        not matched_base64_family
        and not re.search(r"\s", stripped)
        and re.search(r"[!#$%&()*+\-;<=>?@^_`{|}~]", b64_candidate)
        and re.fullmatch(r"[\x21-\x7e]+", b64_candidate)
    ):
        try:
            _decode_base85(b64_candidate)
            guesses.append(("base85", "Contains Base85-only punctuation and decodes cleanly to UTF-8 text."))
        except ValueError:
            pass

    if re.search(r"%[0-9A-Fa-f]{2}", stripped):
        guesses.append(("url", "Contains %XX percent-encoded sequences."))

    if re.search(r"=[0-9A-Fa-f]{2}", stripped):
        guesses.append(("quoted_printable", "Contains =XX soft-encoded byte sequences, typical of Quoted-Printable."))

    phonetic_tokens = [w for w in re.split(r"[\s/]+", stripped) if w]
    if len(phonetic_tokens) >= 2 and all(w.upper() in _NATO_REVERSE for w in phonetic_tokens):
        guesses.append(("phonetic", "Every word matches a NATO phonetic-alphabet code word."))

    if re.fullmatch(r"[A-Za-z\s.,!?'\"-]+", stripped) and re.search(r"[A-Za-z]", stripped):
        rot13_result = codecs.encode(stripped, "rot13")
        original_words = re.findall(r"[a-z']+", stripped.lower())
        rot13_words = re.findall(r"[a-z']+", rot13_result.lower())
        original_hits = sum(1 for w in original_words if w in _ROT13_COMMON_WORDS)
        rot13_hits = sum(1 for w in rot13_words if w in _ROT13_COMMON_WORDS)
        if rot13_hits > 0 and rot13_hits > original_hits:
            guesses.append(("rot13", f"Applying ROT13 reveals {rot13_hits} common English word(s) not present in the original text."))

    return guesses

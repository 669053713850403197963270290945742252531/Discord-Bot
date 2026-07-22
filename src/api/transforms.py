"""
Text transforms for /transform.

Most styles are a straight per-character swap into one of Unicode's
"Mathematical Alphanumeric Symbols" blocks (bold, italic, fraktur,
double-struck, monospace...), which are laid out A-Z/a-z/0-9 in order
with a handful of exceptions where Unicode reserves that code point for
a legacy Letterlike Symbol instead (e.g. italic h -> ℎ). A few styles
(mirror, zalgo, inverted, sparkle) don't map to a clean Unicode block and
get their own small bespoke function instead. Characters a style has no
mapping for are always passed through unchanged rather than dropped, so
spacing/punctuation survives every style intact.
"""

import random
import string
from typing import Any, Dict, List, Optional, Tuple


def _build_alphabet_map(
    upper_start: Optional[int],
    lower_start: Optional[int],
    digit_start: Optional[int] = None,
    upper_exceptions: Optional[Dict[str, str]] = None,
    lower_exceptions: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    Builds an A-Z / a-z / 0-9 -> Unicode-block character lookup table from
    a block's starting code point, assuming the block is laid out
    sequentially (true of every Mathematical Alphanumeric Symbols block
    used below). `upper_exceptions`/`lower_exceptions` patch in the
    letters Unicode reserves in favor of a pre-existing Letterlike Symbol
    instead, keyed by the plain letter (e.g. {"H": "ℌ"}).
    """
    mapping: Dict[str, str] = {}
    if upper_start is not None:
        for i, ch in enumerate(string.ascii_uppercase):
            mapping[ch] = chr(upper_start + i)
    if lower_start is not None:
        for i, ch in enumerate(string.ascii_lowercase):
            mapping[ch] = chr(lower_start + i)
    if digit_start is not None:
        for i, ch in enumerate(string.digits):
            mapping[ch] = chr(digit_start + i)
    mapping.update(upper_exceptions or {})
    mapping.update(lower_exceptions or {})
    return mapping


def _apply_char_map(text: str, mapping: Dict[str, str]) -> str:
    """Passes each character through `mapping`, leaving anything unmapped untouched."""
    return "".join(mapping.get(ch, ch) for ch in text)


def _apply_combining_mark(text: str, mark: str) -> str:
    """
    Threads a combining diacritic after every non-whitespace character.
    Used for the line/underline-style transforms, which are all "one
    combining mark, repeated" -- whitespace is skipped since a mark with
    nothing to attach to just floats visibly on its own.
    """
    return "".join(ch + mark if not ch.isspace() else ch for ch in text)


def _transform_regional_indicators(text: str) -> str:
    """
    A-Z/a-z -> 🇦-🇿 Regional Indicator Symbols. Two of these placed
    directly next to each other render as a single flag emoji (e.g. "US"
    -> 🇺🇸) instead of two separate letters, so a zero-width space is
    threaded between consecutive indicator characters to keep every
    letter rendering on its own.
    """
    out = []
    prev_was_indicator = False
    for ch in text:
        if ch.isalpha() and ch.upper() in string.ascii_uppercase:
            if prev_was_indicator:
                out.append("\u200b")
            out.append(chr(0x1F1E6 + (ord(ch.upper()) - ord("A"))))
            prev_was_indicator = True
        else:
            out.append(ch)
            prev_was_indicator = False
    return "".join(out)


def _transform_emoji_letters(text: str) -> str:
    """A-Z/a-z -> 🅐-🅩 (Negative Circled Latin Capital Letter), the colorful 'badge' alphabet emoji clients render these as."""
    mapping = _build_alphabet_map(0x1F150, 0x1F150)  # only one case of glyph exists; same result regardless of input case
    return _apply_char_map(text, mapping)


_CURSIVE_UPPER_EXCEPTIONS = {"B": "ℬ", "E": "ℰ", "F": "ℱ", "H": "ℋ", "I": "ℐ", "L": "ℒ", "M": "ℳ", "R": "ℛ"}
_CURSIVE_LOWER_EXCEPTIONS = {"e": "ℯ", "g": "ℊ", "o": "ℴ"}

def _transform_cursive(text: str) -> str:
    """A-Z/a-z -> Mathematical Script Letters, patched with the legacy Letterlike Symbols Unicode substitutes for the letters it reserves."""
    mapping = _build_alphabet_map(0x1D49C, 0x1D4B6, upper_exceptions=_CURSIVE_UPPER_EXCEPTIONS, lower_exceptions=_CURSIVE_LOWER_EXCEPTIONS)
    return _apply_char_map(text, mapping)


_SUPERSCRIPT_LOWER = {
    "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ", "h": "ʰ", "i": "ⁱ", "j": "ʲ",
    "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "n": "ⁿ", "o": "ᵒ", "p": "ᵖ", "q": "q", "r": "ʳ", "s": "ˢ", "t": "ᵗ",
    "u": "ᵘ", "v": "ᵛ", "w": "ʷ", "x": "ˣ", "y": "ʸ", "z": "ᶻ",
}
_SUPERSCRIPT_UPPER = {
    "A": "ᴬ", "B": "ᴮ", "D": "ᴰ", "E": "ᴱ", "G": "ᴳ", "H": "ᴴ", "I": "ᴵ", "J": "ᴶ", "K": "ᴷ", "L": "ᴸ",
    "M": "ᴹ", "N": "ᴺ", "O": "ᴼ", "P": "ᴾ", "R": "ᴿ", "T": "ᵀ", "U": "ᵁ", "V": "ⱽ", "W": "ᵂ",
}
_SUPERSCRIPT_OTHER = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
}

def _transform_superscript(text: str) -> str:
    """
    Unicode only defines superscript capitals for about two-thirds of the
    alphabet (C, F, Q, S, X, Y, Z are missing) -- the ones it's missing
    fall back to the lowercase superscript glyph rather than the
    full-size letter, since "small and raised" matters more here than
    preserving case.
    """
    out = []
    for ch in text:
        if ch in _SUPERSCRIPT_UPPER:
            out.append(_SUPERSCRIPT_UPPER[ch])
        elif ch.lower() in _SUPERSCRIPT_LOWER:
            out.append(_SUPERSCRIPT_LOWER[ch.lower()])
        else:
            out.append(_SUPERSCRIPT_OTHER.get(ch, ch))
    return "".join(out)


_SUBSCRIPT_LOWER = {
    "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "l": "ₗ", "m": "ₘ", "n": "ₙ",
    "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ", "u": "ᵤ", "v": "ᵥ", "x": "ₓ",
}
_SUBSCRIPT_OTHER = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
    "+": "₊", "-": "₋", "=": "₌", "(": "₍", ")": "₎",
}

def _transform_subscript(text: str) -> str:
    """
    Subscript coverage is much sparser than superscript -- only 17 letters
    have a defined Unicode subscript glyph at all (no b, c, d, f, g, q, w,
    y, z...). Letters outside that set are left as their normal-size
    original rather than dropped or faked.
    """
    out = []
    for ch in text:
        lower = ch.lower()
        if lower in _SUBSCRIPT_LOWER:
            out.append(_SUBSCRIPT_LOWER[lower])
        else:
            out.append(_SUBSCRIPT_OTHER.get(ch, ch))
    return "".join(out)


_MIRROR_LETTERS = {
    "b": "d", "d": "b", "p": "q", "q": "p",
    "B": "d", "D": "b", "P": "q", "Q": "p",  # no distinct uppercase mirror glyphs exist -- closest visual match reuses the lowercase swap
}
_MIRROR_PUNCTUATION = {"(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{", "<": ">", ">": "<", "/": "\\", "\\": "/"}

def _transform_mirror(text: str) -> str:
    """
    A left-right ("held up to a mirror") flip: the string order is
    reversed and a small set of naturally mirror-symmetric characters
    (b/d, p/q, brackets, slashes) are swapped so they still point the
    right way once reversed. Most letters have no true horizontally-
    mirrored Unicode lookalike, so they're otherwise left alone -- see
    "Inverted" for a full upside-down (180°-rotated) character remap.
    """
    swapped = [_MIRROR_LETTERS.get(ch, _MIRROR_PUNCTUATION.get(ch, ch)) for ch in text]
    return "".join(reversed(swapped))


_ZALGO_UP = ["\u0300", "\u0301", "\u0302", "\u0303", "\u0304", "\u0305", "\u0306", "\u0307", "\u0308", "\u030a", "\u030f", "\u0311", "\u0323", "\u0327", "\u0328"]
_ZALGO_MID = ["\u0334", "\u0335", "\u0336", "\u0337", "\u0338", "\u034e"]
_ZALGO_DOWN = ["\u0316", "\u0317", "\u0318", "\u0319", "\u031c", "\u031d", "\u031e", "\u031f", "\u0320", "\u0324", "\u0325", "\u0326", "\u0329", "\u032a", "\u032b", "\u032c", "\u032d"]

def _transform_zalgo(text: str, *, intensity: int = 3) -> str:
    """
    Layers random combining diacritical marks above, through, and below
    each non-whitespace character. `intensity` caps how many marks of
    each type can stack per character -- kept modest so results stay
    reasonably within Discord's 1024-char embed field limit and don't
    turn into unreadable noise.
    """
    out = []
    for ch in text:
        out.append(ch)
        if ch.isspace():
            continue
        out.extend(random.choices(_ZALGO_UP, k=random.randint(0, intensity)))
        out.extend(random.choices(_ZALGO_MID, k=random.randint(0, max(1, intensity - 2))))
        out.extend(random.choices(_ZALGO_DOWN, k=random.randint(0, intensity)))
    return "".join(out)


def _transform_monospace(text: str) -> str:
    """A-Z/a-z/0-9 -> Mathematical Monospace (fully sequential, no exceptions)."""
    mapping = _build_alphabet_map(0x1D670, 0x1D68A, 0x1D7F6)
    return _apply_char_map(text, mapping)


_INVERTED_LOWER = {
    "a": "ɐ", "b": "q", "c": "ɔ", "d": "p", "e": "ǝ", "f": "ɟ", "g": "ƃ", "h": "ɥ", "i": "ᴉ", "j": "ɾ",
    "k": "ʞ", "l": "l", "m": "ɯ", "n": "u", "o": "o", "p": "d", "q": "b", "r": "ɹ", "s": "s", "t": "ʇ",
    "u": "n", "v": "ʌ", "w": "ʍ", "x": "x", "y": "ʎ", "z": "z",
}
_INVERTED_OTHER = {
    "0": "0", "1": "Ɩ", "2": "ᄅ", "3": "Ɛ", "4": "ㄣ", "5": "5", "6": "9", "7": "ㄥ", "8": "8", "9": "6",
    ".": "˙", ",": "'", "'": ",", '"': "„", "?": "¿", "!": "¡",
    "(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{", "<": ">", ">": "<", "&": "⅋", "_": "‾",
}

def _transform_inverted(text: str) -> str:
    """
    Classic 'upside-down text': every character is swapped for its
    rotated-180° lookalike and the whole string is reversed, since
    flipping a word vertically also reverses its reading order. Case is
    folded to lowercase for letters -- Unicode has no clean rotated
    glyph for most capitals, so generators conventionally flatten case
    rather than reach for obscure/poorly-supported code points.
    """
    out = [_INVERTED_LOWER.get(ch.lower(), _INVERTED_OTHER.get(ch, ch)) for ch in text]
    return "".join(reversed(out))


_CIRCLED_DIGIT_START = 0x2460  # ①..⑨

def _transform_circled(text: str) -> str:
    """A-Z/a-z -> Ⓐ-Ⓩ/ⓐ-ⓩ, 1-9 -> ①-⑨, 0 -> ⓪ (0 sits outside the 1-9 block, so it's patched in separately)."""
    mapping = _build_alphabet_map(0x24B6, 0x24D0)
    mapping["0"] = "⓪"
    for i, d in enumerate("123456789"):
        mapping[d] = chr(_CIRCLED_DIGIT_START + i)
    return _apply_char_map(text, mapping)


def _transform_squared(text: str) -> str:
    """A-Z/a-z -> 🄰-🆉 (Squared Latin Capital Letter). No separate lowercase squared block exists, so both cases map to the same glyph."""
    mapping = _build_alphabet_map(0x1F130, 0x1F130)
    return _apply_char_map(text, mapping)


def _transform_serif_bold(text: str) -> str:
    """A-Z/a-z/0-9 -> Mathematical Bold (fully sequential, no exceptions)."""
    mapping = _build_alphabet_map(0x1D400, 0x1D41A, 0x1D7CE)
    return _apply_char_map(text, mapping)


def _transform_serif_italic(text: str) -> str:
    """A-Z/a-z -> Mathematical Italic. Digits have no italic variant in Unicode, so they pass through unchanged; italic h is reserved in favor of the pre-existing ℎ (PLANCK CONSTANT)."""
    mapping = _build_alphabet_map(0x1D434, 0x1D44E, lower_exceptions={"h": "ℎ"})
    return _apply_char_map(text, mapping)


def _transform_sans_bold_italic(text: str) -> str:
    """A-Z/a-z -> Mathematical Sans-Serif Bold Italic. Digits have no bold-italic sans variant in Unicode, so they pass through unchanged."""
    mapping = _build_alphabet_map(0x1D63C, 0x1D656)
    return _apply_char_map(text, mapping)


_BLACKLETTER_UPPER_EXCEPTIONS = {"C": "ℭ", "H": "ℌ", "I": "ℑ", "R": "ℜ", "Z": "ℨ"}

def _transform_blackletter(text: str) -> str:
    """A-Z/a-z -> Mathematical Fraktur, patched with the legacy Letterlike Symbols Unicode substitutes for the 5 capitals it reserves."""
    mapping = _build_alphabet_map(0x1D504, 0x1D51E, upper_exceptions=_BLACKLETTER_UPPER_EXCEPTIONS)
    return _apply_char_map(text, mapping)


_DOUBLE_STRUCK_UPPER_EXCEPTIONS = {"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"}

def _transform_double_struck(text: str) -> str:
    """A-Z/a-z/0-9 -> Mathematical Double-Struck ('blackboard bold'), patched with the legacy Letterlike Symbols substitutes for the 7 capitals it reserves."""
    mapping = _build_alphabet_map(0x1D538, 0x1D552, 0x1D7D8, upper_exceptions=_DOUBLE_STRUCK_UPPER_EXCEPTIONS)
    return _apply_char_map(text, mapping)


def _transform_fullwidth(text: str) -> str:
    """
    Shifts every printable ASCII character (0x21-0x7E) up by 0xFEE0 into
    its Fullwidth Form equivalent -- covers letters, digits, AND
    punctuation in one pass, unlike the letter-only styles above. The
    plain space is mapped separately to U+3000 IDEOGRAPHIC SPACE, since
    0xFEE0 lands outside the printable range for it.
    """
    out = []
    for ch in text:
        if ch == " ":
            out.append("\u3000")
        elif "!" <= ch <= "~":
            out.append(chr(ord(ch) + 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def _transform_sparkle(text: str) -> str:
    """Bookends the text with ✨ and threads one between each word for a decorative 'aesthetic' look."""
    words = [w for w in text.split(" ") if w != ""] or [text]
    return "✨ " + " ✨ ".join(words) + " ✨"


def _transform_text_to_binary(text: str) -> str:
    """UTF-8 encodes `text`, then renders each byte as an 8-bit binary string, space-separated (e.g. 'Hi' -> '01001000 01101001'). UTF-8 means multi-byte characters (emoji, accents, etc.) round-trip correctly, just as more than one 8-bit group."""
    return " ".join(f"{byte:08b}" for byte in text.encode("utf-8"))


def _transform_binary_to_text(text: str) -> str:
    """
    Reverses text_to_binary(): parses `text` as either whitespace-separated
    8-bit groups ("01001000 01101001") or one unbroken run of bits
    ("0100100001101001", sliced into 8-bit chunks), turns those back into
    raw bytes, and UTF-8 decodes them.

    Raises ValueError if the input has no non-whitespace content, contains
    anything besides 0s and 1s, doesn't come in complete 8-bit bytes, or
    decodes to invalid UTF-8 -- all of which point at a copy/paste mistake
    rather than something this function should silently guess around.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("No binary data to convert -- provide 8-bit groups like '01001000 01101001' (spaces optional).")

    if any(ch.isspace() for ch in stripped):
        groups = stripped.split()
    else:
        groups = [stripped[i:i + 8] for i in range(0, len(stripped), 8)]
    bits = "".join(groups)

    if any(ch not in "01" for ch in bits):
        raise ValueError("That doesn't look like valid binary -- it should only contain 0s and 1s (optionally grouped in 8-bit bytes separated by spaces).")
    if len(bits) % 8 != 0:
        raise ValueError(f"Binary input must come in complete 8-bit bytes -- got {len(bits)} bits total, which isn't a multiple of 8.")

    byte_values = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
    try:
        return byte_values.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Those bytes aren't valid UTF-8 once decoded -- double check the binary was copied correctly and hasn't been split into the wrong byte boundaries.")


TRANSFORM_FORMATS: Dict[str, Dict[str, Any]] = {
    "regional_indicators": {"name": "Regional Indicators", "func": _transform_regional_indicators},
    "emoji_letters": {"name": "Emoji Letters", "func": _transform_emoji_letters},
    "cursive": {"name": "Cursive", "func": _transform_cursive},
    "superscript": {"name": "Superscript", "func": _transform_superscript},
    "subscript": {"name": "Subscript", "func": _transform_subscript},
    "mirror": {"name": "Mirror", "func": _transform_mirror},
    "zalgo": {"name": "Zalgo", "func": _transform_zalgo},
    "monospace": {"name": "Monospace", "func": _transform_monospace},
    "inverted": {"name": "Inverted", "func": _transform_inverted},
    "middle_line": {"name": "Middle Line", "func": lambda t: _apply_combining_mark(t, "\u0336")},
    "overlined": {"name": "Overlined", "func": lambda t: _apply_combining_mark(t, "\u0305")},
    "true_underline": {"name": "True Underline", "func": lambda t: _apply_combining_mark(t, "\u0332")},
    "double_underline": {"name": "Double Underline", "func": lambda t: _apply_combining_mark(t, "\u0333")},
    "circled_letters": {"name": "Circled Letters", "func": _transform_circled},
    "squared_letters": {"name": "Squared Letters", "func": _transform_squared},
    "serif_bold": {"name": "Serif Bold", "func": _transform_serif_bold},
    "serif_italic": {"name": "Serif Italic", "func": _transform_serif_italic},
    "sans_bold_italic": {"name": "Sans Serif Bold Italic", "func": _transform_sans_bold_italic},
    "blackletter": {"name": "Blackletter", "func": _transform_blackletter},
    "double_struck": {"name": "Double-Struck", "func": _transform_double_struck},
    "fullwidth": {"name": "Fullwidth", "func": _transform_fullwidth},
    "sparkle": {"name": "Sparkle", "func": _transform_sparkle},
    "binary": {"name": "Binary (Text \u2192 Binary)", "func": _transform_text_to_binary},
    "binary_decode": {"name": "Binary Decode (Binary \u2192 Text)", "func": _transform_binary_to_text},
}

# (label, value) pairs in display order, ready to drop straight into
# @app_commands.choices(format=[...]) so this dict stays the one source of
# truth for both the Discord option list and the implementation.
TRANSFORM_FORMAT_CHOICES: List[Tuple[str, str]] = [(v["name"], key) for key, v in TRANSFORM_FORMATS.items()]


def transform_text(format_key: str, text: str) -> str:
    """
    Applies the named /transform style to `text`. `format_key` must be one
    of TRANSFORM_FORMATS' keys -- Discord only ever sends one of these
    back since /transform's `format` option is a fixed choice list rather
    than free-typed + autocompleted text.

    Raises ValueError if `format_key` isn't recognized.
    """
    entry = TRANSFORM_FORMATS.get(format_key)
    if entry is None:
        raise ValueError(f"'{format_key}' isn't a supported transform format.")
    return entry["func"](text)

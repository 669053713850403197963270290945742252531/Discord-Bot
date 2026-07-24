"""
Classical cipher algorithms for /cipher and /decipher.

This mirrors encoding.py's shape (a registry dict + encode_text()/decode_text()
style entry points) but every algorithm here can also take a *key* (a shift
amount, a keyword, a rail count, etc.), so the registry additionally records
how each algorithm's key should be resolved:

    "none"                no key is used at all (e.g. Atbash, Pigpen)
    "optional_default"    a key is accepted but not required -- a sensible
                          default is substituted if omitted (e.g. Caesar
                          defaults its shift to 3)
    "required"            the caller must supply a key, there's no safe
                          default to fall back on (e.g. Vigenere's keyword)
    "required_or_generate" like "required", but /cipher will generate a
                          random one on the fly if none is given -- used by
                          Simple Substitution, where the whole point is a
                          secret mapping. /decipher still requires it
                          explicitly, since there's no way to guess it back.

cipher_text()/decipher_text() below resolve all of that and hand back
(result, key_actually_used) so the command layer can tell the user exactly
what key was applied -- especially important for the generated/defaulted
cases, since that's the only place they'll see it.
"""

import random
import string
from typing import Any, Callable, Dict, List, Optional, Tuple

# =========================================================================
# Caesar
# =========================================================================

def _parse_shift(key: str) -> int:
    try:
        shift = int(str(key).strip())
    except ValueError:
        raise ValueError("Caesar's key must be a whole number (the shift amount), e.g. `3`.")
    return shift % 26


def _caesar_shift(text: str, shift: int) -> str:
    out = []
    for ch in text:
        if "a" <= ch <= "z":
            out.append(chr((ord(ch) - ord("a") + shift) % 26 + ord("a")))
        elif "A" <= ch <= "Z":
            out.append(chr((ord(ch) - ord("A") + shift) % 26 + ord("A")))
        else:
            out.append(ch)
    return "".join(out)


def _encrypt_caesar(text: str, key: str) -> str:
    return _caesar_shift(text, _parse_shift(key))


def _decrypt_caesar(text: str, key: str) -> str:
    return _caesar_shift(text, -_parse_shift(key) % 26)


# =========================================================================
# Atbash (self-inverse, no key)
# =========================================================================

def _atbash(text: str, _key: Optional[str] = None) -> str:
    out = []
    for ch in text:
        if "a" <= ch <= "z":
            out.append(chr(ord("z") - (ord(ch) - ord("a"))))
        elif "A" <= ch <= "Z":
            out.append(chr(ord("Z") - (ord(ch) - ord("A"))))
        else:
            out.append(ch)
    return "".join(out)


# =========================================================================
# Simple Substitution
# =========================================================================

def _generate_substitution_key() -> str:
    letters = list(string.ascii_uppercase)
    random.shuffle(letters)
    return "".join(letters)


def _parse_substitution_key(key: str) -> Dict[str, str]:
    cleaned = str(key).strip().upper()
    if len(cleaned) != 26 or not cleaned.isalpha() or len(set(cleaned)) != 26:
        raise ValueError(
            "Simple Substitution's key must be all 26 letters of the alphabet, each used exactly "
            "once (a shuffled alphabet), e.g. `QWERTYUIOPASDFGHJKLZXCVBNM`."
        )
    return dict(zip(string.ascii_uppercase, cleaned))


def _encrypt_substitution(text: str, key: str) -> str:
    mapping = _parse_substitution_key(key)
    out = []
    for ch in text:
        if ch.isupper():
            out.append(mapping[ch])
        elif ch.islower():
            out.append(mapping[ch.upper()].lower())
        else:
            out.append(ch)
    return "".join(out)


def _decrypt_substitution(text: str, key: str) -> str:
    mapping = _parse_substitution_key(key)
    inverse = {v: k for k, v in mapping.items()}
    out = []
    for ch in text:
        if ch.isupper() and ch in inverse:
            out.append(inverse[ch])
        elif ch.islower() and ch.upper() in inverse:
            out.append(inverse[ch.upper()].lower())
        else:
            out.append(ch)
    return "".join(out)


# =========================================================================
# Vigenere
# =========================================================================

def _clean_vigenere_key(key: str) -> str:
    cleaned = "".join(c for c in str(key) if c.isalpha())
    if not cleaned:
        raise ValueError("Vigenère's key must contain at least one letter, e.g. `LEMON`.")
    return cleaned.upper()


def _vigenere(text: str, key: str, decrypt: bool) -> str:
    key_clean = _clean_vigenere_key(key)
    out = []
    ki = 0
    for ch in text:
        if "a" <= ch <= "z" or "A" <= ch <= "Z":
            base = ord("a") if ch.islower() else ord("A")
            k = ord(key_clean[ki % len(key_clean)]) - ord("A")
            shift = -k if decrypt else k
            out.append(chr((ord(ch) - base + shift) % 26 + base))
            ki += 1
        else:
            out.append(ch)
    return "".join(out)


def _encrypt_vigenere(text: str, key: str) -> str:
    return _vigenere(text, key, decrypt=False)


def _decrypt_vigenere(text: str, key: str) -> str:
    return _vigenere(text, key, decrypt=True)


# =========================================================================
# Playfair (I/J share a cell; X pads double letters and odd length)
# =========================================================================

def _build_playfair_grid(key: str) -> List[str]:
    cleaned = "".join(c for c in str(key).upper() if c.isalpha())
    if not cleaned:
        raise ValueError("Playfair's key must contain at least one letter, e.g. `MONARCHY`.")
    seen = set()
    grid: List[str] = []
    for ch in cleaned + string.ascii_uppercase:
        ch = "I" if ch == "J" else ch
        if ch not in seen:
            seen.add(ch)
            grid.append(ch)
    return grid  # 25 letters, row-major 5x5


def _playfair_pos(grid: List[str], ch: str) -> Tuple[int, int]:
    idx = grid.index(ch)
    return idx // 5, idx % 5


def _playfair_digraphs(text: str) -> List[Tuple[str, str]]:
    letters = ["I" if c == "J" else c for c in text.upper() if c.isalpha()]
    digraphs = []
    i = 0
    while i < len(letters):
        a = letters[i]
        if i + 1 < len(letters) and letters[i + 1] != a:
            digraphs.append((a, letters[i + 1]))
            i += 2
        else:
            digraphs.append((a, "X"))
            i += 1
    return digraphs


def _encrypt_playfair(text: str, key: str) -> str:
    grid = _build_playfair_grid(key)
    digraphs = _playfair_digraphs(text)
    if not digraphs:
        raise ValueError("Playfair needs at least one letter of text to encrypt.")
    out = []
    for a, b in digraphs:
        ra, ca = _playfair_pos(grid, a)
        rb, cb = _playfair_pos(grid, b)
        if ra == rb:
            out.append(grid[ra * 5 + (ca + 1) % 5])
            out.append(grid[rb * 5 + (cb + 1) % 5])
        elif ca == cb:
            out.append(grid[((ra + 1) % 5) * 5 + ca])
            out.append(grid[((rb + 1) % 5) * 5 + cb])
        else:
            out.append(grid[ra * 5 + cb])
            out.append(grid[rb * 5 + ca])
    return " ".join("".join(out[i:i + 2]) for i in range(0, len(out), 2))


def _decrypt_playfair(text: str, key: str) -> str:
    grid = _build_playfair_grid(key)
    cleaned = "".join("I" if c == "J" else c for c in text.upper() if c.isalpha())
    if not cleaned:
        raise ValueError("Playfair ciphertext must contain at least one letter.")
    if len(cleaned) % 2 != 0:
        raise ValueError("Playfair ciphertext should have an even number of letters -- it's encrypted in pairs.")
    out = []
    for i in range(0, len(cleaned), 2):
        a, b = cleaned[i], cleaned[i + 1]
        ra, ca = _playfair_pos(grid, a)
        rb, cb = _playfair_pos(grid, b)
        if ra == rb:
            out.append(grid[ra * 5 + (ca - 1) % 5])
            out.append(grid[rb * 5 + (cb - 1) % 5])
        elif ca == cb:
            out.append(grid[((ra - 1) % 5) * 5 + ca])
            out.append(grid[((rb - 1) % 5) * 5 + cb])
        else:
            out.append(grid[ra * 5 + cb])
            out.append(grid[rb * 5 + ca])
    return "".join(out)


# =========================================================================
# Rail Fence
# =========================================================================

def _parse_rails(key: str) -> int:
    try:
        rails = int(str(key).strip())
    except ValueError:
        raise ValueError("Rail Fence's key must be a whole number \u2265 2 (the number of rails), e.g. `3`.")
    if rails < 2:
        raise ValueError("Rail Fence needs at least 2 rails.")
    return rails


def _rail_fence_pattern(length: int, rails: int) -> List[int]:
    pattern = []
    row, direction = 0, 1
    for _ in range(length):
        pattern.append(row)
        if row == 0:
            direction = 1
        elif row == rails - 1:
            direction = -1
        row += direction
    return pattern


def _encrypt_rail_fence(text: str, key: str) -> str:
    rails = _parse_rails(key)
    pattern = _rail_fence_pattern(len(text), rails)
    rows: List[List[str]] = [[] for _ in range(rails)]
    for ch, r in zip(text, pattern):
        rows[r].append(ch)
    return "".join("".join(row) for row in rows)


def _decrypt_rail_fence(text: str, key: str) -> str:
    rails = _parse_rails(key)
    pattern = _rail_fence_pattern(len(text), rails)
    counts = [pattern.count(r) for r in range(rails)]
    rows = []
    idx = 0
    for c in counts:
        rows.append(list(text[idx:idx + c]))
        idx += c
    row_iters = [iter(row) for row in rows]
    return "".join(next(row_iters[r]) for r in pattern)


# =========================================================================
# Columnar Transposition (keyword, or a digit permutation like "3142")
# =========================================================================

def _columnar_order(key: str) -> Tuple[List[int], int]:
    cleaned = str(key).strip()
    if cleaned.isdigit():
        digits = [int(d) for d in cleaned]
        n = len(digits)
        if sorted(digits) != list(range(1, n + 1)):
            raise ValueError(
                f"A numeric Columnar Transposition key must be a permutation of 1..{n} "
                f"(each digit used exactly once), e.g. `3142`."
            )
        reading_order = sorted(range(n), key=lambda i: digits[i])
        return reading_order, n

    letters = [c for c in cleaned.upper() if c.isalpha()]
    if len(letters) < 2:
        raise ValueError("Columnar Transposition's key must be a keyword (letters) or a digit permutation like `3142`.")
    reading_order = sorted(range(len(letters)), key=lambda i: (letters[i], i))
    return reading_order, len(letters)


def _encrypt_columnar(text: str, key: str) -> str:
    reading_order, n = _columnar_order(key)
    if n < 2:
        raise ValueError("Columnar Transposition needs a key covering at least 2 columns.")
    filler = "X"
    remainder = len(text) % n
    padded = text + filler * (n - remainder) if remainder else text
    rows = [padded[i:i + n] for i in range(0, len(padded), n)]
    columns = ["".join(row[c] for row in rows) for c in range(n)]
    return "".join(columns[c] for c in reading_order)


def _decrypt_columnar(text: str, key: str) -> str:
    reading_order, n = _columnar_order(key)
    if n < 2:
        raise ValueError("Columnar Transposition needs a key covering at least 2 columns.")
    if len(text) % n != 0:
        raise ValueError(
            f"This ciphertext's length ({len(text)}) isn't a multiple of the key length ({n}) -- "
            "Columnar Transposition ciphertext should always fill a full grid."
        )
    num_rows = len(text) // n
    columns: List[Optional[str]] = [None] * n
    idx = 0
    for c in reading_order:
        columns[c] = text[idx:idx + num_rows]
        idx += num_rows
    rows = ["".join(columns[c][r] for c in range(n)) for r in range(num_rows)]
    return "".join(rows)


# =========================================================================
# Baconian (classic 24-letter table -- I/J share a code, as do U/V)
# =========================================================================

_BACON_LETTERS = "ABCDEFGHIKLMNOPQRSTUWXYZ"  # 24 canonical letters (no J, no V)


def _bacon_code(index: int) -> str:
    return "".join("B" if b == "1" else "A" for b in format(index, "05b"))


_BACON_TABLE: Dict[str, str] = {letter: _bacon_code(i) for i, letter in enumerate(_BACON_LETTERS)}
_BACON_TABLE["J"] = _BACON_TABLE["I"]
_BACON_TABLE["V"] = _BACON_TABLE["U"]
_BACON_REVERSE: Dict[str, str] = {code: letter for letter, code in zip(_BACON_LETTERS, (_bacon_code(i) for i in range(24)))}


def _encrypt_baconian(text: str, _key: Optional[str] = None) -> str:
    out_words = []
    for word in text.split(" "):
        codes = [_BACON_TABLE[ch.upper()] for ch in word if ch.isalpha()]
        if codes:
            out_words.append(" ".join(codes))
    return " / ".join(out_words)


def _decrypt_baconian(text: str, _key: Optional[str] = None) -> str:
    out_words = []
    for word in text.split("/"):
        letters = []
        for tok in word.split():
            code = tok.strip().upper()
            if code not in _BACON_REVERSE:
                raise ValueError(f"'{tok}' isn't a valid 5-letter A/B Baconian code.")
            letters.append(_BACON_REVERSE[code])
        out_words.append("".join(letters))
    return " ".join(out_words)


# =========================================================================
# Pigpen -- a fixed symbol substitution standing in for the traditional
# grid/dot drawings, which can't be rendered as plain Discord text/embeds.
# =========================================================================

_PIGPEN_TABLE: Dict[str, str] = {
    # Tic-tac-toe grid, no dot (9 cells -> A-I)
    "A": "┌", "B": "┬", "C": "┐",
    "D": "├", "E": "┼", "F": "┤",
    "G": "└", "H": "┴", "I": "┘",
    # Same 9 cells, dotted (J-R)
    "J": "┌•", "K": "┬•", "L": "┐•",
    "M": "├•", "N": "┼•", "O": "┤•",
    "P": "└•", "Q": "┴•", "R": "┘•",
    # X-shape, no dot (4 cells -> S-V)
    "S": "◸", "T": "◹", "U": "◺", "V": "◿",
    # X-shape, dotted (W-Z)
    "W": "◸•", "X": "◹•", "Y": "◺•", "Z": "◿•",
}
_PIGPEN_REVERSE: Dict[str, str] = {symbol: letter for letter, symbol in _PIGPEN_TABLE.items()}


def _encrypt_pigpen(text: str, _key: Optional[str] = None) -> str:
    out_words = []
    for word in text.split(" "):
        symbols = [_PIGPEN_TABLE[ch.upper()] for ch in word if ch.isalpha()]
        if symbols:
            out_words.append(" ".join(symbols))
    return " / ".join(out_words)


def _decrypt_pigpen(text: str, _key: Optional[str] = None) -> str:
    out_words = []
    for word in text.split("/"):
        letters = []
        for tok in word.split():
            symbol = tok.strip()
            if symbol not in _PIGPEN_REVERSE:
                raise ValueError(f"'{tok}' isn't a recognized Pigpen symbol.")
            letters.append(_PIGPEN_REVERSE[symbol])
        out_words.append("".join(letters))
    return " ".join(out_words)


# =========================================================================
# Polybius Square (optional keyword; I/J share a square)
# =========================================================================

def _build_polybius_grid(key: Optional[str]) -> List[str]:
    cleaned = "".join(c for c in str(key or "").upper() if c.isalpha())
    seen = set()
    grid: List[str] = []
    for ch in cleaned + string.ascii_uppercase:
        ch = "I" if ch == "J" else ch
        if ch not in seen:
            seen.add(ch)
            grid.append(ch)
    return grid  # 25 letters, row-major 5x5


def _encrypt_polybius(text: str, key: Optional[str]) -> str:
    grid = _build_polybius_grid(key)
    out_words = []
    for word in text.split(" "):
        codes = []
        for ch in word:
            if ch.isalpha():
                c = "I" if ch.upper() == "J" else ch.upper()
                idx = grid.index(c)
                codes.append(f"{idx // 5 + 1}{idx % 5 + 1}")
        if codes:
            out_words.append(" ".join(codes))
    return " / ".join(out_words)


def _decrypt_polybius(text: str, key: Optional[str]) -> str:
    grid = _build_polybius_grid(key)
    out_words = []
    for word in text.split("/"):
        letters = []
        for tok in word.split():
            coord = tok.strip()
            if len(coord) != 2 or not coord.isdigit():
                raise ValueError(f"'{tok}' isn't a valid two-digit Polybius coordinate, e.g. `24`.")
            row, col = int(coord[0]), int(coord[1])
            if not (1 <= row <= 5 and 1 <= col <= 5):
                raise ValueError(f"'{tok}' is out of range -- Polybius rows/columns must be 1-5.")
            letters.append(grid[(row - 1) * 5 + (col - 1)])
        out_words.append("".join(letters))
    return " ".join(out_words)


# =========================================================================
# Registry
# =========================================================================

CIPHER_ALGORITHMS: Dict[str, Dict[str, Any]] = {
    "caesar": {
        "name": "Caesar",
        "key_mode": "optional_default",
        "default_key": "3",
        "key_hint": "A whole number (the shift amount), e.g. `3`. Defaults to `3` if omitted.",
        "encrypt": _encrypt_caesar,
        "decrypt": _decrypt_caesar,
    },
    "atbash": {
        "name": "Atbash",
        "key_mode": "none",
        "key_hint": "Atbash has no key -- it's a fixed mirror-alphabet substitution (A\u2194Z, B\u2194Y, ...).",
        "encrypt": _atbash,
        "decrypt": _atbash,
    },
    "substitution": {
        "name": "Simple Substitution",
        "key_mode": "required_or_generate",
        "key_hint": (
            "All 26 letters, each used exactly once (a shuffled alphabet), e.g. "
            "`QWERTYUIOPASDFGHJKLZXCVBNM`. Leave blank when ciphering to get a random one "
            "generated for you -- save it, you'll need the exact same key to decipher."
        ),
        "encrypt": _encrypt_substitution,
        "decrypt": _decrypt_substitution,
        "generate_key": _generate_substitution_key,
    },
    "vigenere": {
        "name": "Vigenère",
        "key_mode": "required",
        "key_hint": "A keyword made of letters, e.g. `LEMON`.",
        "encrypt": _encrypt_vigenere,
        "decrypt": _decrypt_vigenere,
    },
    "playfair": {
        "name": "Playfair",
        "key_mode": "required",
        "key_hint": "A keyword made of letters, e.g. `MONARCHY`. J is treated as I, and X pads double letters/odd length.",
        "encrypt": _encrypt_playfair,
        "decrypt": _decrypt_playfair,
        "note": "J is merged into I, and X is used as filler for double letters or an odd number of letters.",
    },
    "rail_fence": {
        "name": "Rail Fence",
        "key_mode": "optional_default",
        "default_key": "3",
        "key_hint": "A whole number \u2265 2 (the number of rails/rows), e.g. `3`. Defaults to `3` if omitted.",
        "encrypt": _encrypt_rail_fence,
        "decrypt": _decrypt_rail_fence,
    },
    "columnar": {
        "name": "Columnar Transposition",
        "key_mode": "required",
        "key_hint": "A keyword (e.g. `ZEBRA`) or a digit permutation (e.g. `3142`) setting the column order.",
        "encrypt": _encrypt_columnar,
        "decrypt": _decrypt_columnar,
        "note": "Text is padded with X to fill a full grid -- trailing X's after deciphering may need manual trimming.",
    },
    "baconian": {
        "name": "Baconian",
        "key_mode": "none",
        "key_hint": "Baconian has no key -- it's the classic 24-letter table (I/J share a code, as do U/V).",
        "encrypt": _encrypt_baconian,
        "decrypt": _decrypt_baconian,
        "note": "I/J share a code and U/V share a code -- deciphering always returns I and U respectively.",
    },
    "pigpen": {
        "name": "Pigpen",
        "key_mode": "none",
        "key_hint": "Pigpen has no key -- it's a fixed symbol substitution.",
        "encrypt": _encrypt_pigpen,
        "decrypt": _decrypt_pigpen,
        "note": "Discord text can't render the traditional grid/dot drawings, so this uses a fixed set of Unicode symbols standing in for them.",
    },
    "polybius": {
        "name": "Polybius Square",
        "key_mode": "optional_default",
        "default_key": "",
        "key_hint": "An optional keyword to build a keyed grid, e.g. `KEYWORD`. Leave blank for the standard alphabetical grid.",
        "encrypt": _encrypt_polybius,
        "decrypt": _decrypt_polybius,
        "note": "I/J share a square -- deciphering always returns I.",
    },
}

CIPHER_CHOICES: List[Tuple[str, str]] = [(v["name"], key) for key, v in CIPHER_ALGORITHMS.items()]


def _get_entry(algorithm_key: str) -> Dict[str, Any]:
    entry = CIPHER_ALGORITHMS.get(algorithm_key)
    if entry is None:
        raise ValueError(f"'{algorithm_key}' isn't a supported cipher.")
    return entry


def cipher_text(algorithm_key: str, text: str, key: Optional[str]) -> Tuple[str, Optional[str]]:
    """
    Encrypts `text` with the named cipher. `key` may be None/blank for
    ciphers that don't strictly need one (their default or a freshly
    generated key is used instead). Returns (result, key_actually_used) --
    `key_actually_used` is None for key-less ciphers.

    Raises ValueError if the algorithm key isn't recognized, a required key
    is missing, or the supplied key/text is invalid for that cipher.
    """
    entry = _get_entry(algorithm_key)
    mode = entry["key_mode"]

    if mode == "none":
        return entry["encrypt"](text, None), None
    if mode == "optional_default":
        used_key = key.strip() if key and key.strip() else entry["default_key"]
        return entry["encrypt"](text, used_key), used_key
    if mode == "required":
        if not key or not key.strip():
            raise ValueError(f"{entry['name']} requires a key. {entry['key_hint']}")
        used_key = key.strip()
        return entry["encrypt"](text, used_key), used_key
    if mode == "required_or_generate":
        used_key = key.strip() if key and key.strip() else entry["generate_key"]()
        return entry["encrypt"](text, used_key), used_key
    raise ValueError(f"Unhandled key mode for '{algorithm_key}'.")  # pragma: no cover -- registry bug guard


def decipher_text(algorithm_key: str, text: str, key: Optional[str]) -> Tuple[str, Optional[str]]:
    """
    Decrypts `text` with the named cipher. For ciphers that need a key,
    `key` must be exactly the one that was used to cipher it (there's no
    identify-style guessing for these -- shift amounts and keywords aren't
    recoverable from shape alone). Returns (result, key_actually_used).
    """
    entry = _get_entry(algorithm_key)
    mode = entry["key_mode"]

    if mode == "none":
        return entry["decrypt"](text, None), None
    if mode == "optional_default":
        used_key = key.strip() if key and key.strip() else entry["default_key"]
        return entry["decrypt"](text, used_key), used_key
    if mode in ("required", "required_or_generate"):
        if not key or not key.strip():
            raise ValueError(
                f"{entry['name']} needs the exact same key that was used to cipher this text. {entry['key_hint']}"
            )
        used_key = key.strip()
        return entry["decrypt"](text, used_key), used_key
    raise ValueError(f"Unhandled key mode for '{algorithm_key}'.")  # pragma: no cover -- registry bug guard

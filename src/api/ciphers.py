"""
Classical cipher algorithms for /cipher encrypt and /cipher decrypt.

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
    "required_or_generate" like "required", but /cipher encrypt will
                          generate a random one on the fly if none is given
                          -- used by Simple Substitution, where the whole
                          point is a secret mapping. /cipher decrypt still
                          requires it explicitly, since there's no way to
                          guess it back.

cipher_text()/decipher_text() below resolve all of that and hand back
(result, key_actually_used) so the command layer can tell the user exactly
what key was applied -- especially important for the generated/defaulted
cases, since that's the only place they'll see it.
"""

import math
import random
import re
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
# ROT47 (self-inverse, no key -- like ROT13 but over the full printable
# ASCII range 33-126, so digits/punctuation get scrambled too)
# =========================================================================

def _rot47(text: str, _key: Optional[str] = None) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if 33 <= code <= 126:
            out.append(chr(33 + ((code - 33 + 47) % 94)))
        else:
            out.append(ch)
    return "".join(out)


# =========================================================================
# Affine (C = a*P + b mod 26; `a` must be coprime with 26)
# =========================================================================

def _egcd(a: int, b: int) -> Tuple[int, int, int]:
    if a == 0:
        return (b, 0, 1)
    g, x1, y1 = _egcd(b % a, a)
    return (g, y1 - (b // a) * x1, x1)


def _modinv(a: int, m: int) -> int:
    g, x, _ = _egcd(a % m, m)
    if g != 1:
        raise ValueError(f"{a} has no inverse mod {m}.")
    return x % m


def _parse_affine_key(key: str) -> Tuple[int, int]:
    parts = [p for p in re.split(r"[,\s]+", str(key).strip()) if p]
    if len(parts) != 2:
        raise ValueError("Affine's key must be two whole numbers `a,b`, e.g. `5,8`.")
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError("Affine's key must be two whole numbers `a,b`, e.g. `5,8`.")
    a %= 26
    b %= 26
    if math.gcd(a, 26) != 1:
        raise ValueError(
            f"`a={a}` isn't valid for Affine -- it must be coprime with 26 "
            "(try 1, 3, 5, 7, 9, 11, 15, 17, 19, 21, 23, or 25)."
        )
    return a, b


def _encrypt_affine(text: str, key: str) -> str:
    a, b = _parse_affine_key(key)
    out = []
    for ch in text:
        if "a" <= ch <= "z" or "A" <= ch <= "Z":
            base = ord("a") if ch.islower() else ord("A")
            p = ord(ch.upper()) - ord("A")
            out.append(chr((a * p + b) % 26 + base))
        else:
            out.append(ch)
    return "".join(out)


def _decrypt_affine(text: str, key: str) -> str:
    a, b = _parse_affine_key(key)
    a_inv = _modinv(a, 26)
    out = []
    for ch in text:
        if "a" <= ch <= "z" or "A" <= ch <= "Z":
            base = ord("a") if ch.islower() else ord("A")
            c = ord(ch.upper()) - ord("A")
            out.append(chr((a_inv * (c - b)) % 26 + base))
        else:
            out.append(ch)
    return "".join(out)


# =========================================================================
# Autokey (Vigenère variant -- the keyword is extended with the plaintext
# itself instead of repeating, so it never reuses key letters)
# =========================================================================

def _autokey(text: str, key: str, decrypt: bool) -> str:
    key_clean = _clean_vigenere_key(key)
    plain_vals: List[int] = []  # plaintext letter values, built up as we go
    out = []
    ki = 0
    for ch in text:
        if "a" <= ch <= "z" or "A" <= ch <= "Z":
            base = ord("a") if ch.islower() else ord("A")
            k_val = (ord(key_clean[ki]) - ord("A")) if ki < len(key_clean) else plain_vals[ki - len(key_clean)]
            if decrypt:
                c_val = ord(ch.upper()) - ord("A")
                p_val = (c_val - k_val) % 26
            else:
                p_val = ord(ch.upper()) - ord("A")
                out_val = (p_val + k_val) % 26
            plain_vals.append(p_val)
            out.append(chr((p_val if decrypt else out_val) + base))
            ki += 1
        else:
            out.append(ch)
    return "".join(out)


def _encrypt_autokey(text: str, key: str) -> str:
    return _autokey(text, key, decrypt=False)


def _decrypt_autokey(text: str, key: str) -> str:
    return _autokey(text, key, decrypt=True)


# =========================================================================
# Beaufort (C = K - P mod 26 -- reciprocal, so the same function both
# encrypts and decrypts, like Atbash but with a keyword)
# =========================================================================

def _beaufort(text: str, key: Optional[str] = None) -> str:
    key_clean = _clean_vigenere_key(key)
    out = []
    ki = 0
    for ch in text:
        if "a" <= ch <= "z" or "A" <= ch <= "Z":
            base = ord("a") if ch.islower() else ord("A")
            val = ord(ch.upper()) - ord("A")
            k = ord(key_clean[ki % len(key_clean)]) - ord("A")
            out.append(chr((k - val) % 26 + base))
            ki += 1
        else:
            out.append(ch)
    return "".join(out)


# =========================================================================
# Trithemius (progressive Caesar -- shift grows by 1 with every letter;
# key is an optional whole-number starting offset, default 0)
# =========================================================================

def _parse_trithemius_offset(key: Optional[str]) -> int:
    if key is None or not str(key).strip():
        return 0
    try:
        return int(str(key).strip()) % 26
    except ValueError:
        raise ValueError("Trithemius's key must be a whole number (a starting shift offset), e.g. `5`. Leave blank for the classic 0-start.")


def _trithemius(text: str, key: Optional[str], decrypt: bool) -> str:
    offset = _parse_trithemius_offset(key)
    out = []
    i = 0
    for ch in text:
        if "a" <= ch <= "z" or "A" <= ch <= "Z":
            base = ord("a") if ch.islower() else ord("A")
            shift = (offset + i) % 26
            shift = -shift if decrypt else shift
            val = ord(ch.upper()) - ord("A")
            out.append(chr((val + shift) % 26 + base))
            i += 1
        else:
            out.append(ch)
    return "".join(out)


def _encrypt_trithemius(text: str, key: Optional[str]) -> str:
    return _trithemius(text, key, decrypt=False)


def _decrypt_trithemius(text: str, key: Optional[str]) -> str:
    return _trithemius(text, key, decrypt=True)


# =========================================================================
# Bifid (fractionated Polybius Square -- optional `keyword:period`, e.g.
# `MONARCHY:5`; period defaults to the whole message when omitted)
# =========================================================================

def _parse_bifid_key(key: Optional[str]) -> Tuple[str, Optional[int]]:
    raw = str(key or "").strip()
    if ":" not in raw:
        return raw, None
    keyword_part, period_part = raw.split(":", 1)
    period_part = period_part.strip()
    if not period_part:
        return keyword_part.strip(), None
    try:
        period = int(period_part)
    except ValueError:
        raise ValueError("Bifid's optional period (after the `:`) must be a whole number, e.g. `MONARCHY:5`.")
    if period < 1:
        raise ValueError("Bifid's period must be at least 1.")
    return keyword_part.strip(), period


def _encrypt_bifid(text: str, key: Optional[str]) -> str:
    keyword, period = _parse_bifid_key(key)
    grid = _build_polybius_grid(keyword)
    letters = ["I" if c.upper() == "J" else c.upper() for c in text if c.isalpha()]
    if not letters:
        raise ValueError("Bifid needs at least one letter of text to encrypt.")
    block_size = period or len(letters)
    out_letters = []
    for start in range(0, len(letters), block_size):
        block = letters[start:start + block_size]
        rows = [grid.index(ch) // 5 + 1 for ch in block]
        cols = [grid.index(ch) % 5 + 1 for ch in block]
        sequence = rows + cols
        for j in range(0, len(sequence), 2):
            rr, cc = sequence[j], sequence[j + 1]
            out_letters.append(grid[(rr - 1) * 5 + (cc - 1)])
    return "".join(out_letters)


def _decrypt_bifid(text: str, key: Optional[str]) -> str:
    keyword, period = _parse_bifid_key(key)
    grid = _build_polybius_grid(keyword)
    letters = ["I" if c.upper() == "J" else c.upper() for c in text if c.isalpha()]
    if not letters:
        raise ValueError("Bifid ciphertext must contain at least one letter.")
    block_size = period or len(letters)
    out_letters = []
    for start in range(0, len(letters), block_size):
        block = letters[start:start + block_size]
        n = len(block)
        flat = []
        for ch in block:
            idx = grid.index(ch)
            flat.append(idx // 5 + 1)
            flat.append(idx % 5 + 1)
        rows, cols = flat[:n], flat[n:]
        for rr, cc in zip(rows, cols):
            out_letters.append(grid[(rr - 1) * 5 + (cc - 1)])
    return "".join(out_letters)


# =========================================================================
# Hill (matrix cipher -- key is a perfect-square count of numbers giving
# an n x n matrix row by row, e.g. `3,3,2,5` for 2x2; must be invertible
# mod 26, i.e. gcd(determinant, 26) == 1)
# =========================================================================

def _matrix_det(m: List[List[int]]) -> int:
    n = len(m)
    if n == 1:
        return m[0][0]
    if n == 2:
        return m[0][0] * m[1][1] - m[0][1] * m[1][0]
    det = 0
    for col in range(n):
        minor = [row[:col] + row[col + 1:] for row in m[1:]]
        det += ((-1) ** col) * m[0][col] * _matrix_det(minor)
    return det


def _matrix_adjugate(m: List[List[int]]) -> List[List[int]]:
    n = len(m)
    if n == 1:
        return [[1]]
    cof = [[0] * n for _ in range(n)]
    for r in range(n):
        for c in range(n):
            minor = [row[:c] + row[c + 1:] for i, row in enumerate(m) if i != r]
            cof[r][c] = ((-1) ** (r + c)) * _matrix_det(minor)
    return [[cof[c][r] for c in range(n)] for r in range(n)]  # transpose of cofactors


def _hill_parse_key(key: str) -> Tuple[List[List[int]], int]:
    parts = [p for p in re.split(r"[,\s]+", str(key).strip()) if p]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        raise ValueError("Hill's key must be whole numbers only (a matrix, row by row), e.g. `3,3,2,5` for a 2x2 matrix.")
    n = int(round(len(nums) ** 0.5))
    if n < 2 or n * n != len(nums):
        raise ValueError(
            "Hill's key must contain a perfect-square count of numbers (4 for 2x2, 9 for 3x3, ...), "
            "e.g. `3,3,2,5` for a 2x2 matrix."
        )
    matrix = [nums[i * n:(i + 1) * n] for i in range(n)]
    det = _matrix_det(matrix) % 26
    if math.gcd(det, 26) != 1:
        raise ValueError(
            f"That matrix isn't valid for Hill -- its determinant ({det} mod 26) shares a common factor "
            "with 26, so it can't be inverted. Pick different numbers."
        )
    return matrix, n


def _hill_inverse(matrix: List[List[int]], n: int) -> List[List[int]]:
    det = _matrix_det(matrix) % 26
    det_inv = _modinv(det, 26)
    adj = _matrix_adjugate(matrix)
    return [[(adj[r][c] * det_inv) % 26 for c in range(n)] for r in range(n)]


def _encrypt_hill(text: str, key: str) -> str:
    matrix, n = _hill_parse_key(key)
    letters = [ord(c.upper()) - ord("A") for c in text if c.isalpha()]
    if not letters:
        raise ValueError("Hill needs at least one letter of text to encrypt.")
    remainder = len(letters) % n
    if remainder:
        letters += [ord("X") - ord("A")] * (n - remainder)
    out = []
    for i in range(0, len(letters), n):
        vec = letters[i:i + n]
        for row in matrix:
            val = sum(row[j] * vec[j] for j in range(n)) % 26
            out.append(chr(val + ord("A")))
    return "".join(out)


def _decrypt_hill(text: str, key: str) -> str:
    matrix, n = _hill_parse_key(key)
    inv = _hill_inverse(matrix, n)
    letters = [ord(c.upper()) - ord("A") for c in text if c.isalpha()]
    if not letters:
        raise ValueError("Hill ciphertext must contain at least one letter.")
    if len(letters) % n != 0:
        raise ValueError(
            f"This ciphertext's length ({len(letters)}) isn't a multiple of the matrix size ({n}) -- "
            "Hill ciphertext should always fill complete blocks."
        )
    out = []
    for i in range(0, len(letters), n):
        vec = letters[i:i + n]
        for row in inv:
            val = sum(row[j] * vec[j] for j in range(n)) % 26
            out.append(chr(val + ord("A")))
    return "".join(out)


# =========================================================================
# Identify -- best-effort guess at which classic cipher likely produced a
# given ciphertext. Mirrors encoding.py's identify_encoding() in shape:
# checks structural/shape clues first (Baconian, Pigpen, Polybius/Bifid,
# Playfair), then falls back to statistical analysis (index of coincidence
# to separate mono- vs polyalphabetic, then chi-squared frequency fitting
# to brute-force a likely Caesar shift or confirm Atbash/Trithemius).
# This is a heuristic, not a solver -- keyed ciphers (Vigenère, Beaufort,
# Playfair, Columnar, Hill, ...) can be *suggested* but never auto-solved,
# since their keys aren't recoverable from shape/statistics alone.
# =========================================================================

_ENGLISH_LETTER_FREQ = {
    "A": 8.2, "B": 1.5, "C": 2.8, "D": 4.3, "E": 12.7, "F": 2.2, "G": 2.0,
    "H": 6.1, "I": 7.0, "J": 0.15, "K": 0.77, "L": 4.0, "M": 2.4, "N": 6.7,
    "O": 7.5, "P": 1.9, "Q": 0.095, "R": 6.0, "S": 6.3, "T": 9.1, "U": 2.8,
    "V": 0.98, "W": 2.4, "X": 0.15, "Y": 2.0, "Z": 0.074,
}

_CIPHER_IDENTIFY_COMMON_WORDS = {
    "the", "a", "an", "is", "are", "was", "were", "and", "to", "of", "in",
    "that", "it", "you", "this", "for", "on", "with", "as", "be", "at",
    "by", "have", "has", "not", "but", "or", "if", "your", "my", "we",
    "he", "she", "they", "will", "can", "all", "one", "there", "what",
}


def _letters_only_upper(text: str) -> str:
    return "".join(c for c in text.upper() if "A" <= c <= "Z")


def _index_of_coincidence(letters: str) -> float:
    n = len(letters)
    if n < 2:
        return 0.0
    counts: Dict[str, int] = {}
    for c in letters:
        counts[c] = counts.get(c, 0) + 1
    numerator = sum(v * (v - 1) for v in counts.values())
    return numerator / (n * (n - 1))


def _chi_squared_vs_english(letters: str) -> float:
    n = len(letters)
    if n == 0:
        return float("inf")
    counts = {c: 0 for c in string.ascii_uppercase}
    for c in letters:
        counts[c] += 1
    chi = 0.0
    for c in string.ascii_uppercase:
        expected = _ENGLISH_LETTER_FREQ[c] / 100.0 * n
        if expected > 0:
            chi += (counts[c] - expected) ** 2 / expected
    return chi


def _word_match_score(text: str) -> float:
    words = re.findall(r"[A-Za-z']+", text.lower())
    if not words:
        return 0.0
    hits = sum(1 for w in words if w in _CIPHER_IDENTIFY_COMMON_WORDS)
    return hits / len(words)


def identify_cipher(text: str) -> List[Tuple[str, str]]:
    """
    Returns a best-first list of (algorithm_key, reason) guesses for which
    classic cipher likely produced `text`. Always a heuristic -- see the
    module note above for why keyed ciphers can only ever be suggested,
    not auto-solved. Returns an empty list if nothing distinctive was found.
    """
    stripped = text.strip()
    guesses: List[Tuple[str, str]] = []
    if not stripped:
        return guesses

    # -- Structural/shape checks (checked first, most specific) --
    compact_ab = re.sub(r"[\s/]+", "", stripped.upper())
    if compact_ab and len(compact_ab) % 5 == 0 and all(c in "AB" for c in compact_ab):
        guesses.append(("baconian", "Consists entirely of A/B letters in groups of 5 -- matches Baconian's 24-letter code."))

    if any(sym in stripped for sym in ("┌", "┬", "┐", "├", "┼", "┤", "└", "┴", "┘", "◸", "◹", "◺", "◿")):
        guesses.append(("pigpen", "Contains this bot's Pigpen symbol set."))

    non_digit_non_sep = re.sub(r"[\d\s/]+", "", stripped)
    digit_tokens = re.findall(r"\d{1,2}", stripped)
    if digit_tokens and not non_digit_non_sep and all(len(t) == 2 and t[0] in "12345" and t[1] in "12345" for t in digit_tokens):
        guesses.append(("polybius", "Space-separated two-digit groups using only digits 1-5 -- matches a Polybius Square coordinate grid."))
        guesses.append(("bifid", "The same digit shape can also come from Bifid (a fractionated Polybius variant) -- try this if Polybius doesn't decipher cleanly."))

    if re.fullmatch(r"[A-Za-z]{2}(?: [A-Za-z]{2})*", stripped):
        pairs = stripped.split(" ")
        if len(pairs) >= 2 and all(p[0].upper() != p[1].upper() for p in pairs):
            guesses.append(("playfair", "Letters grouped into space-separated pairs with no doubled letter within a pair -- matches Playfair's digraph output."))

    letters = _letters_only_upper(stripped)
    if len(letters) < 8:
        if not guesses:
            guesses.append(("caesar", "Too short for reliable frequency analysis -- Caesar, Atbash, and Simple Substitution are all reasonable to try by hand."))
        return guesses

    # -- ROT47 (deterministic, no key -- just try it and score the result) --
    if re.search(r"[^A-Za-z0-9\s]", stripped):
        rot47_attempt = _rot47(stripped)
        if _word_match_score(rot47_attempt) >= 0.2 and _word_match_score(rot47_attempt) > _word_match_score(stripped):
            guesses.insert(0, ("rot47", f"Applying ROT47 reveals readable English (word-match score {_word_match_score(rot47_attempt):.0%})."))

    ic = _index_of_coincidence(letters)

    if ic >= 0.047:
        # High IC: monoalphabetic substitution or transposition (both
        # preserve/shift the underlying English frequency distribution).
        best_shift, best_chi = 0, float("inf")
        for shift in range(26):
            shifted = "".join(chr((ord(c) - ord("A") - shift) % 26 + ord("A")) for c in letters)
            chi = _chi_squared_vs_english(shifted)
            if chi < best_chi:
                best_chi, best_shift = chi, shift
        atbash_attempt = "".join(chr(ord("Z") - (ord(c) - ord("A"))) for c in letters)
        atbash_chi = _chi_squared_vs_english(atbash_attempt)

        if atbash_chi < 60 and atbash_chi < best_chi:
            guesses.append(("atbash", f"Mirroring the alphabet (Atbash) closely matches English letter frequencies (chi-squared {atbash_chi:.1f})."))
        if best_chi < 60:
            if best_shift == 0:
                guesses.append(("caesar", f"Letter frequencies already resemble English without any shift (chi-squared {best_chi:.1f}) -- may be unshifted, or shift 0."))
            else:
                guesses.append(("caesar", f"Letter frequencies best match English with a shift of {best_shift} (chi-squared {best_chi:.1f}) -- try deciphering with key `{best_shift}`."))
        if not any(k in ("caesar", "atbash") for k, _ in guesses):
            guesses.append((
                "rail_fence",
                "Letter frequencies resemble ordinary English (high index of coincidence) but no single shift or "
                "mirror matches -- likely a transposition cipher (Rail Fence or Columnar Transposition), which "
                "can't be auto-solved without the rail count/keyword.",
            ))
        guesses.append((
            "substitution",
            "A general Simple Substitution (each letter mapped to a different fixed letter) always remains "
            "possible too -- it needs its exact 26-letter key to reverse, which can't be guessed from shape alone.",
        ))
    else:
        # Low IC: polyalphabetic-family cipher, or a matrix cipher -- both
        # flatten single-letter frequencies toward random.
        trithemius_attempt = "".join(chr((ord(c) - ord("A") - i) % 26 + ord("A")) for i, c in enumerate(letters))
        trithemius_chi = _chi_squared_vs_english(trithemius_attempt)
        if trithemius_chi < 80:
            guesses.append(("trithemius", f"Undoing a position-based progressive shift (Trithemius) reasonably matches English letter frequencies (chi-squared {trithemius_chi:.1f})."))
        guesses.append((
            "vigenere",
            f"A low index of coincidence ({ic:.3f} vs ~0.067 for plain English) points to a polyalphabetic cipher "
            "-- most likely Vigenère, Beaufort, or Autokey. None of these can be auto-solved without their keyword.",
        ))
        guesses.append((
            "hill",
            "A matrix-based Hill cipher would also flatten letter frequencies like this -- worth trying if the "
            "ciphertext's length is a multiple of a small block size (2, 3, ...).",
        ))

    return guesses


IDENTIFY_CHOICE_VALUE = "identify"


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
    "rot47": {
        "name": "ROT47",
        "key_mode": "none",
        "key_hint": "ROT47 has no key -- it's a fixed 47-position rotation over the printable ASCII range (letters, digits, and punctuation).",
        "encrypt": _rot47,
        "decrypt": _rot47,
        "note": "Unlike ROT13, ROT47 also scrambles digits and punctuation, not just letters.",
    },
    "affine": {
        "name": "Affine",
        "key_mode": "required",
        "key_hint": "Two whole numbers `a,b`, e.g. `5,8`. `a` must be coprime with 26 (1, 3, 5, 7, 9, 11, 15, 17, 19, 21, 23, or 25).",
        "encrypt": _encrypt_affine,
        "decrypt": _decrypt_affine,
        "note": "Formula: C = a\u00d7P + b (mod 26). Caesar is the special case a=1.",
    },
    "autokey": {
        "name": "Autokey",
        "key_mode": "required",
        "key_hint": "A keyword made of letters, e.g. `LEMON`.",
        "encrypt": _encrypt_autokey,
        "decrypt": _decrypt_autokey,
        "note": "Like Vigen\u00e8re, but once the keyword runs out the plaintext itself extends the key, so it never repeats.",
    },
    "beaufort": {
        "name": "Beaufort",
        "key_mode": "required",
        "key_hint": "A keyword made of letters, e.g. `LEMON`.",
        "encrypt": _beaufort,
        "decrypt": _beaufort,
        "note": "A Vigen\u00e8re variant (C = K \u2212 P mod 26) that's reciprocal -- the exact same operation both ciphers and deciphers.",
    },
    "trithemius": {
        "name": "Trithemius",
        "key_mode": "optional_default",
        "default_key": "0",
        "key_hint": "An optional whole-number starting offset (default `0`), e.g. `5`.",
        "encrypt": _encrypt_trithemius,
        "decrypt": _decrypt_trithemius,
        "note": "The classic progressive-shift cipher -- shift starts at the offset and increases by 1 for every letter.",
    },
    "bifid": {
        "name": "Bifid",
        "key_mode": "optional_default",
        "default_key": "",
        "key_hint": "An optional keyword, and/or a `:period` block size, e.g. `MONARCHY`, `MONARCHY:5`, or just `:5`. Period defaults to the whole message.",
        "encrypt": _encrypt_bifid,
        "decrypt": _decrypt_bifid,
        "note": "A fractionated Polybius Square -- I/J share a square, and a shorter period increases diffusion but must match exactly to decipher.",
    },
    "hill": {
        "name": "Hill",
        "key_mode": "required",
        "key_hint": "A perfect-square count of whole numbers giving an n\u00d7n matrix row by row, e.g. `3,3,2,5` for 2x2. Must be invertible mod 26.",
        "encrypt": _encrypt_hill,
        "decrypt": _decrypt_hill,
        "note": "Text is padded with X to fill complete blocks, non-letters are stripped, and case isn't preserved -- output is always uppercase letters.",
    },
}

CIPHER_CHOICES: List[Tuple[str, str]] = [(v["name"], key) for key, v in CIPHER_ALGORITHMS.items()] + [("Identify", IDENTIFY_CHOICE_VALUE)]


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

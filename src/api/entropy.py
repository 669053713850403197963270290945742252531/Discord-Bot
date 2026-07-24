"""
Password/string strength estimation for /entropy.

Computes a character-pool-based "raw" entropy, then discounts it for
common weaknesses (exact common-password matches, sequential runs like
'abcd'/'4321', keyboard walks like 'qwerty', and repeated
characters/blocks like 'aaaa'/'abab') to get an "effective" entropy --
similar in spirit to what a tool like zxcvbn does, without needing an
external dependency. Crack times are then projected across a handful of
standard attacker throughput profiles.

This is a heuristic, not a guarantee: it has no access to real breach
databases and only recognizes the pattern classes implemented below, so
it can both under- and over-estimate real-world guessability.
"""

import math
import string
from dataclasses import dataclass
from typing import List, Tuple

# A short list of extremely common passwords -- anything on this list is
# assumed to be among the very first guesses any real attacker (or an
# off-the-shelf leaked-password dictionary) would try, so an exact
# case-insensitive match short-circuits straight to "trivially guessable"
# regardless of what its raw character-pool entropy would otherwise say.
COMMON_PASSWORDS = frozenset({
    "123456", "password", "12345678", "qwerty", "123456789", "12345",
    "1234", "111111", "1234567", "dragon", "123123", "baseball",
    "abc123", "football", "monkey", "letmein", "shadow", "master",
    "666666", "qwertyuiop", "123321", "mustang", "1234567890",
    "michael", "654321", "superman", "1qaz2wsx", "7777777", "121212",
    "000000", "qazwsx", "123qwe", "killer", "trustno1", "jordan",
    "jennifer", "hunter", "buster", "soccer", "harley", "batman",
    "andrew", "tigger", "sunshine", "iloveyou", "charlie", "robert",
    "thomas", "hockey", "ranger", "daniel", "starwars", "112233",
    "george", "computer", "michelle", "jessica", "pepper", "1111",
    "zxcvbn", "555555", "11111111", "131313", "freedom", "777777",
    "pass", "159753", "maggie", "aaaaaa", "asdfgh", "asdfghjkl",
    "qwerty123", "admin", "welcome", "login", "princess", "solo",
    "passw0rd", "whatever", "ninja", "azerty", "loveme", "flower",
    "hottie", "loveyou", "letmein1", "password1", "password123",
    "changeme", "guest", "iloveyou1", "monkey1", "dragon1",
})

# Adjacent-key runs for a standard US QWERTY layout -- each string is one
# "walk" that's trivially fast to type (and therefore to guess) despite
# looking randomish. Checked in both directions.
KEYBOARD_ROWS = [
    "qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890",
    "!@#$%^&*()", "`1234567890-=",
]

SYMBOLS = string.punctuation + " "

# Attacker throughput profiles, in guesses/second -- the same rough tiers
# tools like zxcvbn use: a rate-limited login form, an unthrottled one,
# an offline attacker hashing with a slow KDF (bcrypt/scrypt/Argon2), and
# an offline attacker with GPU/ASIC hardware against a fast, unsalted hash.
ATTACK_PROFILES = [
    ("Online, rate-limited (100/hr)", 100 / 3600),
    ("Online, no rate limit (10/s)", 10.0),
    ("Offline, slow hash e.g. bcrypt (10k/s)", 1e4),
    ("Offline, fast hash + GPU rig (10B/s)", 1e10),
]


def _char_pool_size(text: str) -> Tuple[int, List[str]]:
    """
    Determines the size of the character space a brute-force attacker
    would have to search, based on which character *classes* appear in
    `text` (not how many of each) -- the standard approach used by most
    password strength estimators. Returns (pool_size, class_labels).
    """
    pool = 0
    labels = []
    if any(c.islower() and c.isascii() for c in text):
        pool += 26
        labels.append("lowercase")
    if any(c.isupper() and c.isascii() for c in text):
        pool += 26
        labels.append("uppercase")
    if any(c.isdigit() and c.isascii() for c in text):
        pool += 10
        labels.append("digits")
    if any(c in SYMBOLS for c in text):
        pool += len(SYMBOLS)
        labels.append("symbols")
    if any(not c.isascii() for c in text):
        # Conservative bucket for accented letters, CJK, emoji, etc. --
        # not a precise alphabet size, just enough to reflect that these
        # meaningfully widen the search space beyond ASCII-only text.
        pool += 100
        labels.append("extended/unicode")
    return pool, labels


def _find_sequential_runs(text: str, min_len: int = 3) -> List[Tuple[int, int]]:
    """Finds runs of >=min_len characters that are consecutive ascending
    or descending code points (e.g. 'abcd', '4321', 'ZYXW')."""
    spans = []
    n = len(text)
    i = 0
    while i < n - 1:
        step = ord(text[i + 1]) - ord(text[i])
        if step not in (1, -1):
            i += 1
            continue
        j = i
        while j + 1 < n and ord(text[j + 1]) - ord(text[j]) == step:
            j += 1
        if j - i + 1 >= min_len:
            spans.append((i, j))
        i = j + 1
    return spans


def _find_keyboard_runs(text: str, min_len: int = 3) -> List[Tuple[int, int]]:
    """Finds runs of >=min_len characters that walk along a QWERTY row,
    forwards or backwards (e.g. 'qwerty', 'asdf', '0987')."""
    lowered = text.lower()
    spans = []
    for row in KEYBOARD_ROWS:
        for direction in (row, row[::-1]):
            start = 0
            needle = direction[:min_len]
            while True:
                idx = lowered.find(needle, start)
                if idx == -1:
                    break
                match_len = min_len
                while (idx + match_len < len(lowered)
                       and match_len < len(direction)
                       and lowered[idx + match_len] == direction[match_len]):
                    match_len += 1
                spans.append((idx, idx + match_len - 1))
                start = idx + 1
    return spans


def _find_repeat_runs(text: str, min_len: int = 3) -> List[Tuple[int, int]]:
    """Finds runs of the same character repeated >=min_len times (e.g.
    'aaaa', '1111'), and short repeated blocks (e.g. 'abab', '123123')."""
    spans = []
    n = len(text)

    # Same character repeated
    i = 0
    while i < n:
        j = i
        while j + 1 < n and text[j + 1] == text[i]:
            j += 1
        if j - i + 1 >= min_len:
            spans.append((i, j))
        i = j + 1

    # Short repeated block (period 2 or 3), e.g. "abab", "123123". Jumps
    # `i` past any match it finds so this stays roughly linear instead of
    # rescanning the same repeated region character by character.
    for period in (2, 3):
        i = 0
        while i + period * 2 <= n:
            block = text[i:i + period]
            j = i + period
            while j + period <= n and text[j:j + period] == block:
                j += period
            if j - i >= max(min_len, period * 2):
                spans.append((i, j - 1))
                i = j
            else:
                i += 1

    return spans


def _merge_spans(spans: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Merges overlapping spans and keeps them sorted, so overlapping
    weaknesses (e.g. a sequential run that's also a keyboard run) aren't
    double-discounted."""
    if not spans:
        return []
    spans = sorted(spans)
    merged = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


@dataclass
class EntropyResult:
    length: int
    pool_size: int
    char_classes: List[str]
    raw_bits: float             # pure pool-size-based entropy, no discounting
    effective_bits: float       # discounted for detected weak patterns
    weaknesses: List[str]
    rating: str
    crack_times: List[Tuple[str, str]]  # (attacker profile label, human duration)


def rating_for(bits: float) -> str:
    """Public wrapper around _rating_for -- lets other entropy sources
    (e.g. /genpass's word-based passphrase entropy) classify a bit count
    the same way /entropy does, without reaching into a private helper."""
    return _rating_for(bits)


def crack_times_for_bits(effective_bits: float) -> List[Tuple[str, str]]:
    """
    Projects an effective-entropy bit count into human-readable crack
    times across ATTACK_PROFILES. Split out from _build_result so entropy
    that isn't computed via analyze_entropy's character-pool math (e.g.
    /genpass's word-based passphrase entropy, or a random-mode password
    whose real pool size is already known exactly rather than re-scanned)
    can still reuse the identical crack-time projection and duration
    formatting that /entropy uses.
    """
    bits = max(effective_bits, 0.0)
    guesses = 2 ** max(bits - 1, 0)  # average case: attacker finds it halfway through the keyspace
    return [(label, _format_duration(guesses / rate)) for label, rate in ATTACK_PROFILES]


def _rating_for(bits: float) -> str:
    if bits < 28:
        return "Very Weak"
    if bits < 36:
        return "Weak"
    if bits < 60:
        return "Reasonable"
    if bits < 80:
        return "Strong"
    if bits < 128:
        return "Very Strong"
    return "Excellent"


def _format_duration(seconds: float) -> str:
    if seconds < 1:
        return "instantly"

    UNITS = [
        ("century", "centuries", 60 * 60 * 24 * 365 * 100),
        ("year", "years", 60 * 60 * 24 * 365),
        ("month", "months", 60 * 60 * 24 * 30),
        ("day", "days", 60 * 60 * 24),
        ("hour", "hours", 60 * 60),
        ("minute", "minutes", 60),
        ("second", "seconds", 1),
    ]

    # Past 33 billion years -- the spec's chosen stand-in for "the
    # universe's estimated end point" -- raw math here would otherwise
    # print something unreadable like "1.9e+19 years", so just call it
    # what it functionally is instead of a number nobody can use.
    IMPOSSIBLE_THRESHOLD_YEARS = 33e9
    if seconds / UNITS[1][2] > IMPOSSIBLE_THRESHOLD_YEARS:
        return "Impossible / Indefinite"

    for singular, plural, unit_seconds in UNITS:
        if seconds >= unit_seconds:
            value = seconds / unit_seconds
            name = singular if round(value) == 1 else plural
            return f"~{value:,.0f} {name}"
    return "instantly"


def analyze_entropy(text: str) -> EntropyResult:
    """
    Core estimator behind /entropy. Computes a pool-size-based "raw"
    entropy, then walks the text looking for common weaknesses (exact
    common-password match, sequential runs, keyboard walks, repeated
    characters/blocks) and discounts the entropy accordingly, since
    these patterns collapse an attacker's real search space far below
    what raw character-class math alone would suggest. Projects the
    resulting effective entropy into crack times across ATTACK_PROFILES.

    Raises ValueError on empty input.
    """
    if not text:
        raise ValueError("Nothing to analyze -- provide some text.")

    length = len(text)
    pool_size, char_classes = _char_pool_size(text)
    raw_bits = length * math.log2(pool_size) if pool_size else 0.0

    weaknesses = []

    if text.lower() in COMMON_PASSWORDS:
        # Exact match against a known common password overrides everything
        # else -- these are the first things any real attacker's
        # dictionary tries, regardless of character variety. Effective
        # entropy becomes roughly "how many bits to enumerate this list",
        # not the pool-based math, since character variety doesn't matter
        # once the exact string is already in every cracker's wordlist.
        weaknesses.append("Matches a well-known common password")
        effective_bits = min(raw_bits, math.log2(len(COMMON_PASSWORDS)))
        return _build_result(length, pool_size, char_classes, raw_bits, effective_bits, weaknesses)

    weak_spans: List[Tuple[int, int]] = []

    if seq_spans := _find_sequential_runs(text):
        weaknesses.append("Contains a sequential run (like 'abcd' or '4321')")
        weak_spans.extend(seq_spans)

    if kb_spans := _find_keyboard_runs(text):
        weaknesses.append("Contains a keyboard-adjacent run (like 'qwerty' or 'asdf')")
        weak_spans.extend(kb_spans)

    if rep_spans := _find_repeat_runs(text):
        weaknesses.append("Contains a repeated character or repeated block (like 'aaaa' or 'abab')")
        weak_spans.extend(rep_spans)

    merged = _merge_spans(weak_spans)

    # Each weak span is charged like a single cheap "move" (log2 of the
    # span's own length + 1) instead of full pool-size-per-character --
    # a cracking tool tries "qwerty" as one guess, not 26^6 random ones.
    covered = 0
    discounted_bits = 0.0
    for start, end in merged:
        span_len = end - start + 1
        covered += span_len
        discounted_bits += math.log2(span_len + 1)

    remaining_chars = max(length - covered, 0)
    effective_bits = (remaining_chars * math.log2(pool_size) + discounted_bits) if pool_size else discounted_bits
    effective_bits = min(effective_bits, raw_bits)

    return _build_result(length, pool_size, char_classes, raw_bits, effective_bits, weaknesses)


def _build_result(
    length: int, pool_size: int, char_classes: List[str],
    raw_bits: float, effective_bits: float, weaknesses: List[str],
) -> EntropyResult:
    effective_bits = max(effective_bits, 0.0)
    crack_times = crack_times_for_bits(effective_bits)
    return EntropyResult(
        length=length,
        pool_size=pool_size,
        char_classes=char_classes,
        raw_bits=raw_bits,
        effective_bits=effective_bits,
        weaknesses=weaknesses or ["No common weak patterns detected"],
        rating=_rating_for(effective_bits),
        crack_times=crack_times,
    )

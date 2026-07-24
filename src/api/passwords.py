"""
Password/passphrase generation backing /genpass.

Adapted from a web password-generator spec into a Discord-native command:
each character-class checkbox becomes a boolean option, the "toggle to
reveal a length text box" control collapses into a single optional
`length` option (omitted -- i.e. the toggle being off -- falls back to a
random 8-26 length, exactly like the web version's default), and the
sci-fi themed "help icon next to every setting" becomes each option's own
`@app_commands.describe()` text, which Discord already renders as inline
help under every option in the command builder.

Every random choice in this module -- character selection, which slots get
the guaranteed-mandatory characters, the final shuffle, and passphrase word
picks -- goes through `secrets` (a CSPRNG), never the `random` module,
which is predictable and unsuitable for anything security-sensitive. This
mirrors the spec's explicit requirement to use window.crypto.getRandomValues()
rather than Math.random() in the browser version.

No-log guarantee: nothing generated here is written to disk, cached, or
sent to GitHub/Users.json -- it only ever exists in memory for the
duration of building the response, and the response itself is always sent
ephemeral by commands/genpass.py.
"""

import math
import secrets
import string
from pathlib import Path
from typing import Dict, List, Optional

_RNG = secrets.SystemRandom()

# -------------------------------------------------------------------------
# Character sets
# -------------------------------------------------------------------------

LOWERCASE = string.ascii_lowercase
UPPERCASE = string.ascii_uppercase
DIGITS = string.digits
SYMBOLS = string.punctuation  # deliberately excludes space -- spec says no spaces/empties

# Ambiguous-when-handwritten-or-misread characters, merged from both
# examples in the spec ("0, O, l, I, |" and "i, l, 1, L, o, 0, O").
AMBIGUOUS_CHARS = frozenset("0OoIil1L|")

# The "Extended" toggle's added value on top of the four base classes
# above (which already cover "every possible keyboard key" on a standard
# US layout): a curated span of accented Latin, Greek, and Cyrillic
# letters plus common currency/typographic symbols, standing in for the
# spec's "random foreign language symbols". Curated rather than
# exhaustive -- there's no single canonical "every foreign keyboard key"
# set -- and hand-picked to exclude combining marks, control characters,
# and anything that renders blank/invisible in Discord.
EXTENDED_CHARS = (
    "àáâãäåæçèéêëìíîïñòóôõöøùúûüýÿ"
    "ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÑÒÓÔÕÖØÙÚÛÜÝ"
    "αβγδεζηθικλμνξοπρστυφχψω"
    "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"
    "бвгджзиклмнпрстфцчшщэюя"
    "БВГДЖЗИКЛМНПРСТФЦЧШЩЭЮЯ"
    "€£¥¢§µ×÷±°©®™•‰†‡¤¶"
)

# Fallback range used whenever `length` is omitted (i.e. the web spec's
# length-toggle is "off") -- same 8-26 range as the original spec.
MIN_RANDOM_LENGTH = 8
MAX_RANDOM_LENGTH = 26

DEFAULT_WORD_COUNT = 5
DEFAULT_SEPARATOR = "-"

_WORDLIST_PATH = Path(__file__).parent / "data" / "eff_large_wordlist.txt"
_wordlist_cache: Optional[List[str]] = None


def load_wordlist() -> List[str]:
    """
    Loads (and caches) the bundled EFF Long Wordlist for Diceware-style
    passphrases. Each line on disk is `key<TAB>word`; only the word column
    is kept. Raises RuntimeError if the data file is missing so a
    misconfigured deployment fails loudly instead of silently falling back
    to something weaker.
    """
    global _wordlist_cache
    if _wordlist_cache is not None:
        return _wordlist_cache

    if not _WORDLIST_PATH.exists():
        raise RuntimeError(
            f"Passphrase wordlist missing at {_WORDLIST_PATH} -- Passphrase mode can't run without it."
        )

    words = []
    with open(_WORDLIST_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            words.append(line.split("\t")[-1])

    if not words:
        raise RuntimeError(f"Passphrase wordlist at {_WORDLIST_PATH} is empty.")

    _wordlist_cache = words
    return words


# -------------------------------------------------------------------------
# Random-character generation
# -------------------------------------------------------------------------

def random_length() -> int:
    """Picks a fresh random length in [MIN_RANDOM_LENGTH, MAX_RANDOM_LENGTH]
    -- used whenever the `length` option is omitted, matching the web
    spec's "length toggle off" behavior. Called fresh on every generation
    (including Regenerate), not just once, so repeatedly regenerating
    without a fixed length keeps varying the length too."""
    return _RNG.randint(MIN_RANDOM_LENGTH, MAX_RANDOM_LENGTH)


def generate_random_password(
    *,
    length: int,
    uppercase: bool = True,
    lowercase: bool = True,
    numbers: bool = True,
    symbols: bool = True,
    extended: bool = False,
    exclude_ambiguous: bool = False,
    easy_to_read: bool = False,
    min_numbers: int = 0,
    min_symbols: int = 0,
) -> str:
    """
    Generates one cryptographically-random password from the enabled
    character classes, guaranteeing at least one character from every
    enabled class (the spec's "Minimum Character Requirement Check"),
    bumped up further for `min_numbers`/`min_symbols` when set. Raises
    ValueError (with a message safe to show the user directly) for any
    combination of options that can't be satisfied.
    """
    if easy_to_read:
        # "Limits character types to make passwords easier to type
        # manually while remaining secure" -- no symbols, no extended
        # charset, and ambiguous characters always stripped, regardless
        # of what those three options were otherwise set to.
        symbols = False
        extended = False
        exclude_ambiguous = True

    classes: Dict[str, str] = {}
    if lowercase:
        classes["lowercase"] = LOWERCASE
    if uppercase:
        classes["uppercase"] = UPPERCASE
    if numbers:
        classes["numbers"] = DIGITS
    if symbols:
        classes["symbols"] = SYMBOLS
    if extended:
        classes["extended"] = EXTENDED_CHARS

    if not classes:
        raise ValueError(
            "At least one character type (`uppercase`, `lowercase`, `numbers`, `symbols`, or "
            "`extended_charset`) must be enabled -- or turn on `easy_to_read` for a sensible default."
        )

    if exclude_ambiguous:
        for label in list(classes):
            filtered = "".join(c for c in classes[label] if c not in AMBIGUOUS_CHARS)
            if filtered:
                classes[label] = filtered
            else:
                # Filtering wiped out this whole class (can't happen for
                # the built-in classes above, but stays safe if that ever
                # changes) -- drop it rather than generate from nothing.
                del classes[label]
        if not classes:
            raise ValueError(
                "Excluding ambiguous characters removed every enabled character type -- enable "
                "another type or turn off `exclude_ambiguous`."
            )

    if min_numbers and "numbers" not in classes:
        raise ValueError("`min_numbers` requires `numbers` to be enabled.")
    if min_symbols and "symbols" not in classes:
        raise ValueError("`min_symbols` requires `symbols` to be enabled.")

    # Mandatory characters: at least one from every enabled class, bumped
    # up to min_numbers/min_symbols for whichever classes those apply to.
    mandatory: List[str] = []
    for label, chars in classes.items():
        count = 1
        if label == "numbers":
            count = max(count, min_numbers)
        elif label == "symbols":
            count = max(count, min_symbols)
        mandatory.extend(_RNG.choice(chars) for _ in range(count))

    if len(mandatory) > length:
        raise ValueError(
            f"`length` ({length}) is too short to fit the required characters ({len(mandatory)} "
            "needed -- one of each enabled type, plus any min_numbers/min_symbols). Raise `length` "
            "or lower the minimums."
        )

    combined_pool = "".join(classes.values())
    remaining = length - len(mandatory)
    body = [_RNG.choice(combined_pool) for _ in range(remaining)]

    result_chars = mandatory + body
    _RNG.shuffle(result_chars)
    return "".join(result_chars)


# -------------------------------------------------------------------------
# Passphrase (Diceware) generation
# -------------------------------------------------------------------------

def generate_passphrase(
    *,
    word_count: int = DEFAULT_WORD_COUNT,
    separator: str = DEFAULT_SEPARATOR,
    capitalize_words: bool = False,
) -> str:
    """Picks `word_count` words uniformly at random from the EFF Long
    Wordlist (mathematically equivalent to rolling 5 dice per word, since
    the list has exactly 6**5 = 7776 entries) and joins them with
    `separator`, e.g. correct-horse-battery-staple."""
    wordlist = load_wordlist()
    words = [_RNG.choice(wordlist) for _ in range(word_count)]
    if capitalize_words:
        words = [w.capitalize() for w in words]
    return separator.join(words)


def passphrase_entropy_bits(word_count: int) -> float:
    """Diceware entropy is just word_count * log2(wordlist size) -- each
    word is one independent, uniform draw from the full list. Deliberately
    separate from api.entropy.analyze_entropy's character-pool math, which
    would badly *underestimate* a passphrase's real strength (it can only
    see letters + a separator character, not "one of 7776 possibilities
    per word")."""
    return word_count * math.log2(len(load_wordlist()))

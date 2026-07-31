"""
Beginner-friendly explanations for /cipherhelp, one entry per algorithm key
in CIPHER_ALGORITHMS (see ciphers.py). Pairs with encoding_help.py, which
does the same job for /encodehelp.

Each entry only carries prose written in plain language -- no advanced
cryptography terms or abbreviations. The actual before/after example shown
to the user is NOT hard-coded here: commands/ciphers.py generates it at
request time by calling the real cipher_text() engine in ciphers.py against
DEMO_PLAINTEXT (and each entry's optional "demo_key"), so the example can
never drift out of sync with what /cipher and /decipher actually produce.

"demo_key" is only needed for algorithms whose key_mode is "required" or
"required_or_generate" -- anything left out falls back to that algorithm's
own default/no-key behavior in cipher_text().
"""

from typing import Any, Dict, Optional

DEMO_PLAINTEXT = "HELLO WORLD"

CIPHER_HELP: Dict[str, Dict[str, Any]] = {
    "caesar": {
        "how_it_works": (
            "Every letter in your message slides the same number of spots down the alphabet. With a shift of "
            "3, A becomes D, B becomes E, and so on, wrapping back to A after Z. To read it, the receiver "
            "just slides each letter backward by the same amount."
        ),
        "security": (
            "Not secure at all. There are only 25 possible shift amounts, so trying every single one instantly "
            "reveals the readable message -- no special tools needed, just patience."
        ),
        "standard": (
            "No. It's one of the oldest ciphers on record, said to be used by Julius Caesar himself, and today "
            "it's used mainly to teach the basic idea of encryption rather than to protect anything real."
        ),
    },
    "atbash": {
        "how_it_works": (
            "Atbash mirrors the alphabet: A swaps with Z, B swaps with Y, and so on, meeting in the middle at "
            "M and N. There's no key to remember -- the swap never changes, so knowing it's Atbash is enough "
            "to read it."
        ),
        "security": (
            "Not secure. With no key and a fixed, well-known swap, anyone who recognizes the pattern can read "
            "it immediately."
        ),
        "standard": (
            "No. It's mostly remembered today for its use in ancient Hebrew texts, and now shows up in puzzles "
            "rather than anything protecting real information."
        ),
    },
    "substitution": {
        "how_it_works": (
            "Every letter of the alphabet gets swapped for a different letter, based on a secret shuffled "
            "version of the alphabet used as the key. Unlike Caesar, letters don't shift by a fixed amount -- "
            "each one can map to any other letter, as long as no two letters share a replacement."
        ),
        "security": (
            "Not secure. Even though there's an enormous number of possible letter-swaps, everyday English has "
            "predictable patterns (some letters, like E, show up far more often than others), and those "
            "patterns let both people and computers crack it quickly."
        ),
        "standard": (
            "No. It's the classic cipher behind newspaper cryptogram puzzles, not something used to protect "
            "real information today."
        ),
        "demo_key": "QWERTYUIOPASDFGHJKLZXCVBNM",
    },
    "vigenere": {
        "how_it_works": (
            "Vigenère repeats a secret keyword underneath your message, letter by letter, and uses each "
            "keyword letter to decide how far to shift that position -- like Caesar, but the shift amount "
            "keeps changing instead of staying fixed."
        ),
        "security": (
            "Not secure by modern standards. It went unbroken for centuries and even earned the nickname "
            "'the unbreakable cipher,' but 19th-century codebreakers found reliable ways to figure out the "
            "keyword's length and unravel it from there."
        ),
        "standard": (
            "No. It's a genuinely important milestone in the history of cryptography, but real-world security "
            "today relies on entirely different, computer-based methods."
        ),
        "demo_key": "LEMON",
    },
    "playfair": {
        "how_it_works": (
            "Playfair arranges a keyword and the rest of the alphabet into a 5x5 grid of letters (I and J "
            "share a square). It then encrypts letters two at a time, looking up each pair's position in the "
            "grid and swapping them using simple row-and-column rules."
        ),
        "security": (
            "Not secure. It was a real improvement over simple substitution since it hides common letter "
            "patterns better, but it's still fully solvable by hand with enough sample text, and trivial for "
            "a computer."
        ),
        "standard": (
            "No, though it has genuine history -- the British military used it around World War One. It isn't "
            "used to protect anything today."
        ),
        "demo_key": "MONARCHY",
    },
    "rail_fence": {
        "how_it_works": (
            "Rail Fence writes your message in a zigzag down and up across a set number of rows, then reads "
            "the letters off row by row instead of in their original order. The number of rows is the only "
            "'key' -- the receiver needs it to rebuild the zigzag and read the message."
        ),
        "security": (
            "Not secure. There are usually only a small number of reasonable row counts to try, so it can be "
            "broken almost instantly just by testing each one."
        ),
        "standard": (
            "No. It's a simple rearranging cipher used mostly for teaching and puzzles, not real protection."
        ),
    },
    "columnar": {
        "how_it_works": (
            "Your message is written into rows underneath a keyword, then read back out column by column, in "
            "an order decided by alphabetizing the keyword's letters. Nothing is replaced -- the letters just "
            "get rearranged."
        ),
        "security": (
            "Not secure. Once someone knows or guesses roughly how the columns were arranged, unscrambling it "
            "back into readable text is a solvable puzzle, and a computer can automate that search very "
            "quickly."
        ),
        "standard": (
            "No. It's a classic pen-and-paper rearranging technique, historically sometimes combined with a "
            "substitution cipher for extra scrambling, but not used alone for real security."
        ),
        "demo_key": "ZEBRA",
    },
    "baconian": {
        "how_it_works": (
            "Baconian hides a message inside a pattern of two symbols (traditionally two subtly different "
            "typefaces, shown here as A's and B's). Every letter of the alphabet has its own unique 5-symbol "
            "combination of A's and B's."
        ),
        "security": (
            "Not secure by itself -- with no key involved, anyone who recognizes the A/B pattern can convert "
            "it straight back to letters. Historically, its real strength came from hiding that pattern inside "
            "an innocent-looking cover message, not from the code itself."
        ),
        "standard": (
            "No. It's named after Sir Francis Bacon and is mainly of historical and puzzle interest today."
        ),
    },
    "pigpen": {
        "how_it_works": (
            "Pigpen swaps each letter for a symbol based on where that letter sits within a small grid pattern "
            "(like a tic-tac-toe board) -- a fixed substitution, so there's no key to keep track of."
        ),
        "security": (
            "Not secure. Anyone who's seen a Pigpen key before can read it about as easily as reading normal "
            "text."
        ),
        "standard": (
            "No. It's popular in puzzles, games, and even scouting traditions, but was never meant for serious "
            "secrecy."
        ),
    },
    "polybius": {
        "how_it_works": (
            "Polybius Square lays the alphabet out in a 5x5 grid (I and J share a square) and represents each "
            "letter with two numbers -- its row and its column. A keyword can optionally scramble the grid's "
            "letter order for an extra layer; otherwise it just uses the alphabet in order."
        ),
        "security": (
            "Not secure. Without a scrambling keyword it's a completely fixed, well-known lookup table, and "
            "even with one, there just aren't enough possible grid arrangements to stand up to a determined "
            "computer."
        ),
        "standard": (
            "No. It dates back to ancient Greece, where it was used to signal letters using torches, and "
            "today it's mostly a building block for other, more complex ciphers rather than something used on "
            "its own."
        ),
    },
    "rot47": {
        "how_it_works": (
            "ROT47 is like ROT13, but bigger -- it rotates through 94 printable keyboard characters (letters, "
            "digits, and punctuation) instead of just the 26 letters, so numbers and symbols get scrambled "
            "too, not just words."
        ),
        "security": (
            "None. Like ROT13, it was never meant to be secure -- it's just a way to make text unreadable at "
            "a glance."
        ),
        "standard": (
            "No. It's mostly an internet-culture trick for briefly hiding spoilers or shock content in forum "
            "posts and code comments."
        ),
    },
    "affine": {
        "how_it_works": (
            "Affine combines two numbers, called 'a' and 'b,' into one formula applied to every letter's "
            "position in the alphabet: multiply by a, then add b, then wrap around if needed. Caesar is "
            "actually a simplified special case of Affine where a is always 1."
        ),
        "security": (
            "Not secure. There are only a limited number of valid 'a' values that work mathematically, which "
            "keeps the total number of possible keys small enough to try them all quickly."
        ),
        "standard": (
            "No. It's mainly used to teach how a little simple math can build a cipher, not to protect real "
            "information."
        ),
        "demo_key": "5,8",
    },
    "autokey": {
        "how_it_works": (
            "Autokey works like Vigenère -- shifting letters using a repeating keyword -- but once the keyword "
            "runs out, it starts using your own message's letters to keep extending the key. This means the "
            "shifting pattern never repeats the way Vigenère's does."
        ),
        "security": (
            "Not secure by modern standards, though it's a genuine improvement over Vigenère since the "
            "never-repeating pattern removes the main weakness codebreakers used against it. It's still fully "
            "breakable with today's computing power."
        ),
        "standard": (
            "No. It's a historically clever refinement of Vigenère, not a method used for real security today."
        ),
        "demo_key": "LEMON",
    },
    "beaufort": {
        "how_it_works": (
            "Beaufort is a close cousin of Vigenère that flips the math around slightly (subtracting the "
            "message letter from the keyword letter instead of the other way around). This makes it "
            "'reciprocal' -- running the exact same process twice on the ciphertext gets you back to the "
            "original message."
        ),
        "security": (
            "Not secure by modern standards, for the same underlying reasons as Vigenère -- the repeating "
            "keyword pattern can eventually be spotted and unraveled."
        ),
        "standard": (
            "No. It's mostly notable for being self-reversing, which made it convenient for manual encryption "
            "in the past, not for real-world protection today."
        ),
        "demo_key": "LEMON",
    },
    "trithemius": {
        "how_it_works": (
            "Trithemius shifts each letter progressively further than the last one -- the first letter shifts "
            "by a starting number, the second shifts one more than that, the third one more again, and so on "
            "through the whole message."
        ),
        "security": (
            "Not secure. Since the shift pattern always increases in a predictable, fixed way, there's no real "
            "secret left to protect once someone recognizes the method."
        ),
        "standard": (
            "No. It's one of the earliest ciphers to shift by a changing amount rather than a fixed one, which "
            "makes it historically important, but it isn't used for real security today."
        ),
    },
    "bifid": {
        "how_it_works": (
            "Bifid first converts every letter into a pair of numbers using a Polybius Square grid, then "
            "splits all those numbers apart and recombines them in a different order before turning them back "
            "into letters. That extra scrambling step spreads each letter's information across the message, "
            "making patterns harder to spot than a simple substitution."
        ),
        "security": (
            "Not secure by modern standards. It's a cleverer, more resistant classical cipher than most on "
            "this list, but it's still solvable with pen-and-paper codebreaking techniques, let alone a "
            "computer."
        ),
        "standard": (
            "No. It's considered one of the more sophisticated pen-and-paper ciphers historically, but it "
            "predates modern cryptography and isn't used for real protection today."
        ),
    },
    "hill": {
        "how_it_works": (
            "Hill groups letters together and encrypts each group at once using a grid of numbers (called a "
            "matrix) instead of working one letter at a time. It's built entirely on number math rather than "
            "simple letter-swapping."
        ),
        "security": (
            "Not secure by modern standards. It was genuinely novel for introducing real mathematics into "
            "cipher design, but if someone gets hold of matching pieces of an original message and its "
            "encrypted version, the whole number grid can be worked out and the cipher broken."
        ),
        "standard": (
            "No. It's a historically significant bridge between classical ciphers and modern, math-based "
            "cryptography, but it isn't used for real security today -- current encryption uses far more "
            "sophisticated math and much larger keys."
        ),
        "demo_key": "3,3,2,5",
    },
}


def get_demo_key(algorithm_key: str) -> Optional[str]:
    """Returns the fixed demo key to feed cipher_text() for this algorithm's
    example, or None to let cipher_text() fall back to that algorithm's own
    default/no-key behavior."""
    return CIPHER_HELP.get(algorithm_key, {}).get("demo_key")

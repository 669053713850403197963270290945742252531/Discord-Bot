"""
Beginner-friendly explanations for /encodehelp, one entry per algorithm key
in ENCODING_ALGORITHMS (see encoding.py). Pairs with cipher_help.py, which
does the same job for /cipherhelp.

Each entry only carries prose written in plain language -- no advanced
cryptography terms or abbreviations. The actual before/after example shown
to the user is NOT hard-coded here: commands/utility.py generates it at
request time by calling the real encode_text() engine in encoding.py against
DEFAULT_DEMO_TEXT (or an entry's own "demo_text" override), so the example
can never drift out of sync with what /encode and /decode actually produce.

Most algorithms happily encode any text, so they just use DEFAULT_DEMO_TEXT.
A few need a different demo:
  - Morse/Braille/Phonetic/Emoji only support letters, digits, and spaces
    (no punctuation), so they use a letters-only demo phrase instead.
  - Pretty JSON's "encode" is really "reformat", so it needs a small valid
    JSON snippet as input rather than a plain sentence.
"""

from typing import Any, Dict

DEFAULT_DEMO_TEXT = "Hello, World!"
_LETTERS_ONLY_DEMO = "HELLO WORLD"

ENCODING_HELP: Dict[str, Dict[str, Any]] = {
    "base64": {
        "how_it_works": (
            "Base64 takes the raw bytes of your text and re-packages them into a set of 64 safe characters "
            "(A-Z, a-z, 0-9, plus `+` and `/`), so that text -- or even non-text data like images -- can "
            "travel safely through systems that only handle basic text, such as email attachments or being "
            "embedded inside other files."
        ),
        "security": (
            "None. Base64 isn't encryption, it's just a different way of writing the same data. Anyone can "
            "decode it back instantly using any online tool, with no key or password required."
        ),
        "standard": (
            "Yes, as an encoding format -- Base64 is an official, widely used internet standard, seen "
            "everywhere from email attachments to web APIs. Just remember 'standard' here means widely used "
            "for compatibility, not that it keeps anything secret."
        ),
    },
    "base64url": {
        "how_it_works": (
            "This works exactly like Base64, but swaps out two characters (`+` and `/`) for URL-safe "
            "alternatives (`-` and `_`), so the result can be dropped directly into a web address without "
            "needing extra escaping."
        ),
        "security": (
            "None -- same as regular Base64, it's simply a different arrangement of the original data that "
            "anyone can reverse instantly."
        ),
        "standard": (
            "Yes, it's a standard variant of Base64 used specifically wherever encoded data needs to live "
            "safely inside a URL, like login tokens or shared links."
        ),
    },
    "url": {
        "how_it_works": (
            "URL Encode replaces characters that aren't allowed in web addresses (spaces, symbols, etc.) with "
            "a percent sign followed by their code in hexadecimal, like turning a space into `%20`. It lets "
            "any text safely travel inside a web link."
        ),
        "security": (
            "None. It's purely about making text web-safe, not about hiding it -- anyone can turn a `%20` "
            "back into a space by hand."
        ),
        "standard": (
            "Yes -- it's a core part of how web addresses work everywhere, though again, it's about "
            "compatibility, not secrecy."
        ),
    },
    "quoted_printable": {
        "how_it_works": (
            "Quoted-Printable keeps normal readable letters and numbers as-is, but rewrites anything unusual "
            "(special characters, non-English letters) as an equals sign followed by two hexadecimal digits. "
            "It was designed so text stays mostly human-readable even after encoding."
        ),
        "security": (
            "None -- it's a text-safety format for older email systems, not a way to hide information."
        ),
        "standard": (
            "Yes, in a specific context -- it's a real, long-established internet standard still used in some "
            "email systems today, though less common than it used to be."
        ),
    },
    "saml": {
        "how_it_works": (
            "This squeezes your text down using compression (making it smaller), then wraps the compressed "
            "result in Base64 so it's safe to put in a web link. It's the same trick used behind the scenes "
            "when logging into a website through 'Single Sign-On.'"
        ),
        "security": (
            "None as a security measure -- it doesn't hide the content at all, it just compresses and "
            "re-encodes it, and anyone can reverse it just as easily."
        ),
        "standard": (
            "Yes, in its specific use case -- it matches how the SAML single sign-on standard packs login "
            "data into a web address, though outside that context it isn't a general-purpose format."
        ),
    },
    "pretty_json": {
        "how_it_works": (
            "This isn't really encoding -- it takes JSON data (a common way apps store structured information) "
            "and reformats it with clean indentation and line breaks so a person can actually read it. "
            "Decoding does the opposite: squeezing it back down to the smallest possible size."
        ),
        "security": (
            "None -- this only changes how the data looks on the page, not what it says. It doesn't hide or "
            "protect anything."
        ),
        "standard": (
            "The 'pretty' indentation style itself isn't a strict standard (different tools indent slightly "
            "differently), but JSON as a data format absolutely is, and it's used constantly in modern "
            "software and web services."
        ),
        "demo_text": '{"name": "Ada", "role": "Programmer"}',
    },
    "utf8": {
        "how_it_works": (
            "This shows the raw bytes your computer actually stores for the text, written out in hexadecimal "
            "(base-16) and grouped two digits at a time -- one group per byte -- using the UTF-8 system that "
            "basically every modern computer uses to represent text."
        ),
        "security": (
            "None -- this is just displaying the computer's internal representation of the text; nothing "
            "about it is hidden or scrambled."
        ),
        "standard": (
            "Yes, extremely so -- UTF-8 is the dominant way text is stored and transmitted across nearly the "
            "entire internet today."
        ),
    },
    "utf16": {
        "how_it_works": (
            "Similar to the UTF-8 version, but shows the bytes according to UTF-16, an older text system that "
            "groups things into 4-hex-digit chunks instead of 2. It mainly shows how the same letters can "
            "look different depending on which text system is used underneath."
        ),
        "security": (
            "None -- again, this only reveals an internal representation, not a hidden or protected form of "
            "the text."
        ),
        "standard": (
            "It's a real, still-used standard (notably inside Windows and Java internally), but UTF-8 has "
            "become the more common choice for the web."
        ),
    },
    "utf32": {
        "how_it_works": (
            "Like UTF-8 and UTF-16, but every character always gets exactly 8 hex digits, no matter how "
            "simple or complex it is. It's the most spelled-out but least space-efficient of the three."
        ),
        "security": "None -- purely a technical representation, not a way to hide information.",
        "standard": (
            "It's a defined standard, but it's rarely used in practice since it wastes a lot of space compared "
            "to UTF-8 or UTF-16."
        ),
    },
    "hex": {
        "how_it_works": (
            "Hexadecimal (base-16, using digits 0-9 and letters A-F) writes out the exact same underlying "
            "bytes as the UTF-8 version above, but without any spaces breaking up the byte groups -- just one "
            "long string of hex digits."
        ),
        "security": (
            "None. It's a very common way to display raw data, not to protect it -- any hex-to-text converter "
            "reveals the original instantly."
        ),
        "standard": (
            "Yes, hexadecimal is an extremely common way to represent bytes across computing in general (web "
            "design colors, memory addresses, and more)."
        ),
    },
    "rot13": {
        "how_it_works": (
            "ROT13 shifts every letter exactly 13 places through the alphabet -- half of 26 -- so running "
            "ROT13 on the same text twice gets you right back to the original. Non-letters are left untouched."
        ),
        "security": (
            "None whatsoever, and it was never meant to have any -- it's a 'joke' cipher traditionally used "
            "online to briefly hide spoilers or punchlines, not to protect real information."
        ),
        "standard": (
            "It's a long-standing internet convention (going back to old newsgroups) for that specific "
            "'don't spoil it for me yet' use case, but not a security standard in any sense."
        ),
    },
    "base32": {
        "how_it_works": (
            "Base32 works like Base64, but uses a smaller set of 32 characters (A-Z and the digits 2-7), "
            "which makes the result longer to write out but easier for a person to read aloud or type without "
            "mixing up similar-looking characters."
        ),
        "security": (
            "None -- exactly like Base64, this is just a different, fully reversible way of writing the same "
            "data."
        ),
        "standard": (
            "Yes, it's an official standard, commonly used for things like two-step-login backup codes, "
            "where accuracy when typing matters more than compactness."
        ),
    },
    "base58": {
        "how_it_works": (
            "Base58 is similar to Base64, but deliberately leaves out characters that are easy to confuse "
            "with each other when handwritten or read quickly -- like the number `0` and the letter `O`, or "
            "the letter `l` and the number `1`."
        ),
        "security": (
            "None -- it's a reversible re-encoding, not encryption, even though it's popularly associated "
            "with cryptocurrency wallet addresses."
        ),
        "standard": (
            "It's a de facto standard in one particular world -- cryptocurrencies like Bitcoin -- but it "
            "isn't a general internet-wide standard the way Base64 or Base32 are."
        ),
    },
    "base85": {
        "how_it_works": (
            "Base85 packs data slightly more efficiently than Base64 by using a bigger set of 85 possible "
            "characters per digit, at the cost of including some punctuation marks that Base64 avoids."
        ),
        "security": "None -- like the other Base-N encodings, it's fully and easily reversible by design.",
        "standard": (
            "It's a recognized, if less common, standard -- notably used inside PDF and PostScript files, and "
            "in several programming tools."
        ),
    },
    "binary": {
        "how_it_works": (
            "This shows the exact 1s and 0s a computer uses to store each character in memory, grouped into "
            "8-digit chunks -- one chunk per letter or character."
        ),
        "security": (
            "None -- it's just another way of displaying the same underlying data, easily reversible by "
            "anyone."
        ),
        "standard": (
            "Binary itself underlies literally all modern computing, but writing text out digit-by-digit like "
            "this is mostly used for teaching how computers represent text, not for real-world data transfer."
        ),
    },
    "decimal": {
        "how_it_works": (
            "Every character is converted to its numeric position in Unicode (the master list of every "
            "character a computer can display) and written out as an ordinary base-10 number, separated by "
            "spaces."
        ),
        "security": "None -- this is a plain numeric relabeling of each character, trivially reversible.",
        "standard": (
            "Not really used as a data-transfer format on its own, but the underlying idea -- every character "
            "has a numeric Unicode 'code point' -- is fundamental to how all modern text works."
        ),
    },
    "unicode_escape": {
        "how_it_works": (
            "Each character is rewritten as a backslash, the letter u (or U for less common characters), and "
            "its numeric Unicode code in hexadecimal -- the same notation many programming languages use "
            "inside their source code to represent characters that are hard to type directly."
        ),
        "security": (
            "None -- it's a readable stand-in notation for characters, not a hidden or scrambled form."
        ),
        "standard": (
            "Yes, in the sense that this exact backslash-u notation is standard syntax across many popular "
            "programming languages."
        ),
    },
    "html": {
        "how_it_works": (
            "Characters that have special meaning in web pages (like `<`, `>`, and `&`) get replaced with a "
            "text stand-in starting with `&` and ending with `;`, so a browser displays them as normal text "
            "instead of trying to interpret them as code."
        ),
        "security": (
            "None as a security tool by itself -- though correctly applying it does help prevent one specific "
            "kind of web attack (malicious code being snuck into a page) by making sure user text always "
            "displays as plain text rather than running as code."
        ),
        "standard": "Yes, it's a core, official part of how HTML works, used across virtually every website.",
    },
    "morse": {
        "how_it_works": (
            "Morse code represents each letter and digit as a sequence of short and long signals "
            "(traditionally called dots and dashes), historically sent as clicks, light flashes, or radio "
            "beeps rather than typed text."
        ),
        "security": (
            "None -- it was designed for reliably transmitting messages over telegraph and radio, not for "
            "keeping them secret. Anyone familiar with the code (or a lookup chart) can read it instantly."
        ),
        "standard": (
            "Yes, historically -- International Morse Code was a genuine global communication standard for "
            "over a century, and it's still learned and used today by ham radio operators, though it's now a "
            "niche skill rather than a mainstream way to communicate."
        ),
        "demo_text": _LETTERS_ONLY_DEMO,
    },
    "braille": {
        "how_it_works": (
            "This represents each letter using the Braille tactile writing system, where each character is a "
            "small pattern of raised dots that can be read by touch. Since this is plain text, it shows the "
            "matching Unicode symbol for each dot pattern instead of an actual raised surface."
        ),
        "security": (
            "None -- this is an accessibility-focused writing system, not a code meant to keep anything secret."
        ),
        "standard": (
            "Yes, genuinely -- Braille is a real, standardized writing system used worldwide, not a puzzle "
            "cipher, even though it appears here alongside more playful or historical formats."
        ),
        "demo_text": _LETTERS_ONLY_DEMO,
    },
    "phonetic": {
        "how_it_works": (
            "Each letter is spelled out using its official phonetic alphabet word (A becomes 'Alpha,' B "
            "becomes 'Bravo,' and so on), designed to be clearly understood over a noisy radio or phone "
            "connection where individual letters can otherwise sound alike."
        ),
        "security": (
            "None -- the whole point is to make communication clearer, not to hide it. Anyone can look up the "
            "same standard word list."
        ),
        "standard": (
            "Yes -- it's an official international standard used by aviation, military, and emergency "
            "services worldwide specifically because it's so unambiguous over voice communication."
        ),
        "demo_text": _LETTERS_ONLY_DEMO,
    },
    "emoji": {
        "how_it_works": (
            "Each letter is swapped for its corresponding regional-indicator emoji (the flag-building-block "
            "letters sometimes used to spell out words in chat apps), and digits become their 'keycap' emoji "
            "version, like 5\ufe0f\u20e3."
        ),
        "security": (
            "None at all -- it's a fun, visual substitution that's arguably easier to spot and reverse than "
            "plain text, not something meant to hide a message."
        ),
        "standard": "No, this is just a playful, novelty format popular in casual chat, not a formal standard.",
        "demo_text": _LETTERS_ONLY_DEMO,
    },
}


def get_demo_text(algorithm_key: str) -> str:
    """Returns the demo phrase to feed encode_text() for this algorithm's
    example -- either that entry's own override (needed for Pretty JSON and
    the letters-only formats), or DEFAULT_DEMO_TEXT otherwise."""
    return ENCODING_HELP.get(algorithm_key, {}).get("demo_text", DEFAULT_DEMO_TEXT)

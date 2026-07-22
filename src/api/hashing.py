"""
Hash utilities for /hash. Algorithm names are pulled live from hashlib
rather than hardcoded, so this automatically tracks whatever md5/sha2/sha3/
blake2/etc. -- plus anything OpenSSL adds on top -- the Python this bot
happens to be running on actually supports.
"""

import hashlib
from typing import List

# Output length (in bytes) used for the two SHAKE algorithms. SHAKE-128/256
# are variable-length (XOF) digests with no fixed size of their own --
# hexdigest()/digest() raise TypeError without an explicit length here.
# 32 bytes (256 bits) matches the strength of a typical SHA-256 digest and
# is called out explicitly in the /hash output so it's never ambiguous.
SHAKE_OUTPUT_BYTES = 32


def get_available_hash_algorithms() -> List[str]:
    """
    Every hash algorithm this Python build's hashlib can actually construct
    right now: the cross-platform-guaranteed set
    (hashlib.algorithms_guaranteed) plus whatever this build's OpenSSL
    binding adds on top (hashlib.algorithms_available). The latter can
    report the same algorithm more than once in different cases (e.g. both
    "sha256" and "SHA256") depending on the OpenSSL build, so this
    lowercases and de-duplicates before sorting.
    """
    names = hashlib.algorithms_guaranteed | hashlib.algorithms_available
    return sorted({name.lower() for name in names})


def hash_text(algorithm: str, text: str) -> str:
    """
    Hashes `text` (UTF-8 encoded) with `algorithm` and returns the hex
    digest. `algorithm` is matched case-insensitively against
    get_available_hash_algorithms().

    Raises ValueError if `algorithm` isn't recognized, or whatever
    TypeError/ValueError hashlib itself raises if construction fails
    despite the name being recognized (e.g. a SHAKE variant used wrong).
    """
    algo = algorithm.lower().strip()
    if algo not in get_available_hash_algorithms():
        raise ValueError(f"'{algorithm}' isn't a supported hash algorithm on this bot's Python build.")

    hasher = hashlib.new(algo)
    hasher.update(text.encode("utf-8"))

    # SHAKE-128/256 are variable-length XOFs -- hexdigest() requires an
    # explicit output length that fixed-size algorithms don't take.
    if algo.startswith("shake_"):
        return hasher.hexdigest(SHAKE_OUTPUT_BYTES)
    return hasher.hexdigest()

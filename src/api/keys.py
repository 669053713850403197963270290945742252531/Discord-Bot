"""Key generation and input validation utilities."""

import random
import re
import string
from datetime import datetime
from typing import List, Set, Tuple


def generate_key(min_length: int = 25, max_length: int = 40) -> str:
    chars = string.ascii_letters + string.digits
    length = random.randint(min_length, max_length)
    return "".join(random.choices(chars, k=length))


def generate_unique_key(users, min_length: int = 25, max_length: int = 40) -> str:
    """Generates a key guaranteed not to collide with any existing user's Key."""
    existing_keys = {u.get("Key") for u in users}
    key = generate_key(min_length, max_length)
    while key in existing_keys:
        key = generate_key(min_length, max_length)
    return key


def generate_unique_keys(count: int, existing_keys: Set[str], min_length: int = 25, max_length: int = 40) -> List[str]:
    """
    Generates `count` keys, each guaranteed unique against `existing_keys`
    and against every other key generated in this same call. Used by
    /genkey's bulk path -- `existing_keys` should be the union of every
    whitelisted user's Key *and* every key currently sitting in
    permittedKeys.txt, so a freshly generated key can never collide with
    one that's already assigned or already pending redemption.
    """
    seen = set(existing_keys)
    new_keys = []
    for _ in range(count):
        key = generate_key(min_length, max_length)
        while key in seen:
            key = generate_key(min_length, max_length)
        seen.add(key)
        new_keys.append(key)
    return new_keys


def parse_key_length_range(length_str: str) -> Tuple[int, int]:
    """
    Parses a /genkey `length` option into a (min_length, max_length) pair
    for generate_key()/generate_unique_keys(). Accepts either a single
    number ("20", a fixed length) or a range ("5-10", inclusive on both
    ends) -- one option covers both instead of separate min/max options.

    Raises ValueError with a human-readable reason on anything malformed,
    so callers can hand that straight to send_error().
    """
    length_str = length_str.strip()

    range_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", length_str)
    if range_match:
        min_length, max_length = int(range_match.group(1)), int(range_match.group(2))
    elif length_str.isdigit():
        min_length = max_length = int(length_str)
    else:
        raise ValueError("`length` must be a single number (e.g. `20`) or a range (e.g. `5-10`).")

    if min_length < 1:
        raise ValueError("`length` must be at least 1.")
    if min_length > max_length:
        raise ValueError(f"`length`'s minimum ({min_length}) can't be greater than its maximum ({max_length}).")
    if max_length > 256:
        raise ValueError("`length`'s maximum can't exceed 256.")

    return min_length, max_length


def is_valid_hwid(hwid: str) -> bool:
    # sha256 hash = 64 hex characters
    return bool(re.fullmatch(r"[a-fA-F0-9]{64}", hwid))


def is_valid_discord_id(discord_id: str) -> bool:
    if not discord_id.isdigit():
        return False
    snowflake = int(discord_id)
    return 1 << 17 < snowflake < 2**64


def is_valid_date(d: str) -> bool:
    try:
        datetime.strptime(d, "%m/%d/%Y, %I:%M:%S %p")
        return True
    except ValueError:
        return False

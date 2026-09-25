"""Per-build encrypted string literals."""

from __future__ import annotations

import random
import re

from ..ast import Edit, SyntaxTree
from ..lexer import scan_tokens


def _decode_lua_string(token: bytes) -> bytes:
    """Decode Luau string escapes without executing source code."""
    if len(token) < 2 or token[:1] not in (b'"', b"'"):
        return token
    body = token[1:-1]
    out = bytearray()
    i = 0
    escapes = {
        ord("a"): 7, ord("b"): 8, ord("f"): 12, ord("n"): 10,
        ord("r"): 13, ord("t"): 9, ord("v"): 11, ord("\\"): 92,
        ord('"'): 34, ord("'"): 39,
    }

    def append_codepoint(value: int) -> None:
        try:
            out.extend(chr(value).encode("utf-8"))
        except (ValueError, UnicodeEncodeError):
            out.append(value & 0xFF)

    while i < len(body):
        if body[i] != 92:
            out.append(body[i]); i += 1; continue
        i += 1
        if i >= len(body):
            break
        c = body[i]
        if c in escapes:
            out.append(escapes[c]); i += 1; continue
        if c in (10, 13):
            # A backslash-newline is a source-line continuation.
            if c == 13 and i + 1 < len(body) and body[i + 1] == 10:
                i += 2
            else:
                i += 1
            out.append(10)
            continue
        if c == ord("z"):
            i += 1
            while i < len(body) and body[i] in b" \t\r\n\f\v":
                i += 1
            continue
        if c == ord("x") and i + 2 < len(body):
            try:
                out.append(int(body[i + 1 : i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        if c == ord("u"):
            if i + 1 < len(body) and body[i + 1] == ord("{"):
                close = body.find(b"}", i + 2)
                if close != -1:
                    try:
                        append_codepoint(int(body[i + 2 : close], 16))
                        i = close + 1
                        continue
                    except ValueError:
                        pass
            elif i + 4 < len(body):
                try:
                    append_codepoint(int(body[i + 1 : i + 5], 16))
                    i += 5
                    continue
                except ValueError:
                    pass
        if c == ord("U") and i + 8 < len(body):
            try:
                append_codepoint(int(body[i + 1 : i + 9], 16))
                i += 9
                continue
            except ValueError:
                pass
        if 48 <= c <= 57:
            j = i
            while j < len(body) and j < i + 3 and 48 <= body[j] <= 57:
                j += 1
            out.append(int(body[i:j]) & 0xFF)
            i = j
            continue
        # The parser already rejects genuinely malformed Luau escapes. Keeping
        # the escaped character here is the safest fallback for grammar drift.
        out.append(c)
        i += 1
    return bytes(out)


def transform(syntax: SyntaxTree, *, key: int, min_length: int = 1) -> tuple[list[Edit], dict[int, bytes]]:
    rng = random.Random(key ^ 0x4E5D_019B)
    edits: list[Edit] = []
    payloads: dict[int, bytes] = {}
    token_index = 0
    used_keys: set[int] = set()

    for token in scan_tokens(syntax.source):
        if token.kind not in {"string", "long_string"}:
            continue
        if token.kind == "long_string":
            match = re.match(rb"^\[(=*)\[(.*)\]\1\]$", token.text, re.DOTALL)
            if not match:
                continue
            value = match.group(2)
            if value.startswith(b"\n"):
                value = value[1:]
        else:
            value = _decode_lua_string(token.text)
        if len(value) < min_length:
            continue
        while True:
            item_key = rng.randrange(1, 0x7FFF_FFFF)
            if item_key not in used_keys:
                used_keys.add(item_key)
                break
        payloads[item_key] = value
        # NUL is forbidden in uploaded source, so the intermediate marker can
        # never collide with user code. It is removed before the final parse.
        marker = f"__CELESTIAL_STRING_{token_index}_{item_key}_{key & 0xFFFFFFFFFFFFFFFF:x}__".encode() + b"\x00"
        edits.append(Edit(token.start, token.end, marker, "encrypted string literal"))
        token_index += 1
    return edits, payloads

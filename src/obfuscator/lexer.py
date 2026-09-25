"""Token utilities used by transforms that need lexical safety.

Tree-sitter owns syntax recognition.  This small lexer is deliberately only a
lexical scanner: it is used to identify comments/strings/numbers while never
trying to parse Lua grammar itself.  That keeps string/comment edits safe even
for syntax added by future Luau grammar revisions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LexToken:
    kind: str
    start: int
    end: int
    text: bytes


def scan_tokens(source: bytes) -> list[LexToken]:
    out: list[LexToken] = []
    i = 0
    n = len(source)

    def is_alpha(b: int) -> bool:
        return b == 95 or 65 <= b <= 90 or 97 <= b <= 122

    def is_digit(b: int) -> bool:
        return 48 <= b <= 57

    def long_bracket_end(pos: int) -> int | None:
        if pos >= n or source[pos] != 91:
            return None
        j = pos + 1
        while j < n and source[j] == 61:
            j += 1
        if j < n and source[j] == 91:
            eqs = source[pos + 1 : j]
            close = b"]" + eqs + b"]"
            end = source.find(close, j + 1)
            return n if end < 0 else end + len(close)
        return None

    while i < n:
        b = source[i]
        if b in b" \t\r\n\f\v":
            i += 1
            continue
        if b == 45 and i + 1 < n and source[i + 1] == 45:  # -- comment
            lb = long_bracket_end(i + 2)
            if lb is not None:
                out.append(LexToken("comment", i, lb, source[i:lb]))
                i = lb
                continue
            j = source.find(b"\n", i + 2)
            j = n if j < 0 else j
            out.append(LexToken("comment", i, j, source[i:j]))
            i = j
            continue
        if b in (34, 39):
            quote = b
            j = i + 1
            while j < n:
                if source[j] == 92:
                    j += 2
                    continue
                if source[j] == quote:
                    j += 1
                    break
                j += 1
            out.append(LexToken("string", i, min(j, n), source[i:min(j, n)]))
            i = min(j, n)
            continue
        lb = long_bracket_end(i)
        if lb is not None:
            out.append(LexToken("long_string", i, lb, source[i:lb]))
            i = lb
            continue
        if is_alpha(b):
            j = i + 1
            while j < n and (is_alpha(source[j]) or is_digit(source[j])):
                j += 1
            out.append(LexToken("identifier", i, j, source[i:j]))
            i = j
            continue
        if is_digit(b) or (b == 46 and i + 1 < n and is_digit(source[i + 1])):
            j = i + 1
            while j < n and source[j] in b"0123456789abcdefABCDEFxX.+-pPeE_":
                # Stop signs unless they are part of an exponent.  Tree-sitter
                # has already validated numeric literals; this scanner is only
                # used for deciding whether a range is numeric.
                if source[j] in (43, 45) and source[j - 1] not in b"pPeE":
                    break
                j += 1
            out.append(LexToken("number", i, j, source[i:j]))
            i = j
            continue
        # Operators/punctuation are only useful as opaque lexical units here.
        out.append(LexToken("punct", i, i + 1, source[i : i + 1]))
        i += 1
    return out

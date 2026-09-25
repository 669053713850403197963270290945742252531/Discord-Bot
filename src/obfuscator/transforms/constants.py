"""AST-safe arithmetic/MBA rewrites for numeric literals.

The transform deliberately stays within exact integer arithmetic for small
values. A different identity family is selected for every literal so the
output does not exhibit a single fixed MBA template.
"""

from __future__ import annotations

import random
import re

from ..ast import Edit, SyntaxTree
from ..lexer import scan_tokens

_INT_RE = re.compile(rb"^(?:0[xX][0-9a-fA-F_]+|[0-9][0-9_]*)$")


def _clean_int(raw: bytes) -> int:
    return int(raw.decode().replace("_", ""), 0)


def _mba(value: int, rng: random.Random) -> str:
    a = rng.randint(3, 19)
    b = rng.randint(2, 13)
    c = rng.randint(3, 17)
    d = rng.randint(2, 11)
    e = rng.randint(2, 15)
    family = rng.randrange(8)

    # Every family simplifies exactly to `value` for integer values and uses
    # only operations with much smaller intermediate magnitudes than the
    # previous fixed template.
    if family == 0:
        return f"((({value}*{a})-({value}*({a}-1)))+(({b}*{c})-({b}*{c})))"
    if family == 1:
        return f"((({value}+{b})-{b})+(({c}-{c})*{d}))"
    if family == 2:
        return f"((({value}*{a})+({value}*{b}))-({value}*({a}+{b}-1)))"
    if family == 3:
        return f"((((({value}+{c})*{d})-({c}*{d}))-{value}*({d}-1))+({e}-{e}))"
    if family == 4:
        return f"((({value}-{c})+({c}*{a})-({c}*({a}-1)))+({b}-{b}))"
    if family == 5:
        return f"((({value}*{d})-({value}*({d}-1)))+(({b}*{e})-({b}*{e})))"
    if family == 6:
        return f"((({value}+({b}*{c}))-({b}*{c}))+(({d}*{e})-({d}*{e})))"
    return f"(((({value}*({a}+{d}))-({value}*{a}))-{value}*({d}-1))+({c}-{c}))"


def transform(syntax: SyntaxTree, *, seed: int) -> list[Edit]:
    rng = random.Random(seed ^ 0x31AD72E1)
    edits: list[Edit] = []
    for token in scan_tokens(syntax.source):
        if token.kind != "number" or not _INT_RE.match(token.text):
            continue
        try:
            value = _clean_int(token.text)
        except ValueError:
            continue
        if value in {0, 1} or abs(value) > 2_000_000_000:
            continue
        edits.append(Edit(token.start, token.end, _mba(value, rng).encode(), "MBA integer literal"))
    return edits

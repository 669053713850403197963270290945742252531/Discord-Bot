"""AST-selected control-flow expression polymorphism.

Conditions are rewritten only where Luau truthiness is preserved. The pass
mixes boolean normalization, opaque predicates, and arithmetic identities so
there is no single wrapper shape in the generated source.
"""

from __future__ import annotations

import random

from ..ast import Edit, SyntaxTree


def _opaque_true(rng: random.Random) -> str:
    a = rng.randrange(3, 97)
    b = rng.randrange(2, 31)
    mode = rng.randrange(5)
    if mode == 0:
        return f"(({a}*{b})==({a}*{b}))"
    if mode == 1:
        return f"((({a}+{b})-{b})=={a})"
    if mode == 2:
        return f"(({a}-{a})==0)"
    if mode == 3:
        return f"((({a}*{a})%{a})==0)"
    return f"((({a}+({b}*{a}))-{b}*{a})=={a})"


def _opaque_false(rng: random.Random) -> str:
    a = rng.randrange(3, 97)
    b = rng.randrange(2, 31)
    return f"(({a}+{b})==({a}+{b}+1))"


def transform(syntax: SyntaxTree, *, seed: int) -> list[Edit]:
    rng = random.Random(seed ^ 0xF10C6A7D)
    edits: list[Edit] = []
    node_types = {"if_statement", "elseif_statement", "if_expression", "elseif_clause", "while_statement", "repeat_statement"}

    for node in syntax.walk():
        if getattr(node, "type", "") not in node_types:
            continue
        cond = None
        if hasattr(node, "child_by_field_name"):
            cond = node.child_by_field_name("condition") or node.child_by_field_name("test")
        if cond is None or cond.end_byte <= cond.start_byte:
            continue

        original = syntax.source[cond.start_byte : cond.end_byte].decode("utf-8", "replace")
        true_expr = _opaque_true(rng)
        false_expr = _opaque_false(rng)
        shape = rng.randrange(5)
        if shape == 0:
            replacement = f"((not not ({original})) and {true_expr} or {false_expr})"
        elif shape == 1:
            replacement = f"((({original}) and ({true_expr})) or ({false_expr}))"
        elif shape == 2:
            replacement = f"(not ((not ({original})) or ({false_expr})))"
        elif shape == 3:
            replacement = f"((({original}) and ({true_expr})) and true)"
        else:
            salt = rng.randrange(5, 37)
            replacement = f"((({original}) or (({salt}-{salt})==1)) and {true_expr})"

        edits.append(Edit(cond.start_byte, cond.end_byte, replacement.encode(), "scrambled branch condition"))
    return edits

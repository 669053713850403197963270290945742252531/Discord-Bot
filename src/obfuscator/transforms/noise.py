"""Build-polymorphic semantic no-op blocks.

These blocks are generated as valid Luau statements and contain only locals,
opaque predicates, and bounded loops. They never observe or modify user state.
They are inserted before subsequent AST passes, so scope-aware renaming treats
their locals exactly like any other local binding.
"""

from __future__ import annotations

import random


def _name(rng: random.Random) -> str:
    return "_" + "".join(rng.choice("IlOoQqZz") for _ in range(rng.randint(6, 11)))


def generate(seed: int, *, blocks: int = 5) -> tuple[str, int]:
    rng = random.Random(seed ^ 0x6E01B10C)
    out: list[str] = []
    count = 0
    for _ in range(max(0, blocks)):
        a, b, c = rng.randrange(3, 71), rng.randrange(2, 19), rng.randrange(2, 29)
        x, y, z = _name(rng), _name(rng), _name(rng)
        family = rng.randrange(4)
        if family == 0:
            out.append(
                f"do local {x}=(({a}*{b})-({a}*({b}-1)));local {y}=({x}-{a});"
                f"if ({y}~={a * (b - 1)}) and (({c}-{c})==1) then local {z}=0 end;"
                f"local {z}=({x}-{x});end;"
            )
        elif family == 1:
            out.append(
                f"do local {x}={a};local {y}=0;for {z}=0,1 do if {z}==1 then {y}={x}-{a} end end;"
                f"if {y}~=0 then {x}={x}+{y} end;end;"
            )
        elif family == 2:
            fn = _name(rng)
            out.append(
                f"do local function {fn}({x},{y}) local {z}=({x}+{y})-{y};"
                f"if (({z}=={x}) and true) then return {z} end return {y} end;"
                f"local {z}={fn}({a},{b});{z}={z}-{z};end;"
            )
        else:
            out.append(
                f"do local {x}={a};local {y}={b};local {z}=0;repeat {z}={z}+1 until {z}>({y} or 0);"
                f"if {z}<0 then {x}={x}+1 end;end;"
            )
        count += 1
    return "".join(out), count

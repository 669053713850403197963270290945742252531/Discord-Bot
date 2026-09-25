"""Polymorphic source-VM backend.

The output is still ordinary Luau source, but every build gets a different
instruction graph, field schema, opcode space, payload packing/permutation,
byte decoder family, handler layout, state representation, and dispatcher
shape. The VM reconstructs the already-transformed Luau program at runtime and
then invokes Luau's native loader.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass

from .lexer import scan_tokens

MASK32 = 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class VMArtifact:
    source: str
    key: int
    instruction_count: int
    runtime_layers: int
    decoder_variants: int
    opaque_edges: int
    payload_blocks: int
    micro_ops: int
    control_flow_decoys: int
    dead_code_blocks: int
    anti_tamper_checks: int
    payload_layers: int
    complexity_score: int


@dataclass(frozen=True, slots=True)
class _SourceProfile:
    size: int
    tokens: int
    identifiers: int
    unique_identifiers: int
    strings: int
    numbers: int
    functions: int
    branches: int
    loops: int
    tables: int
    indexes: int
    calls: int
    operators: int
    nesting: int
    score: int


@dataclass(frozen=True, slots=True)
class _BuildBudget:
    score: int
    junk_range: tuple[int, int]
    control_flow_range: tuple[int, int]
    dead_code_range: tuple[int, int]
    anti_tamper_range: tuple[int, int]
    payload_layers_range: tuple[int, int]
    decoder_variants_range: tuple[int, int]
    chunk_size_range: tuple[int, int]
    selected_junk: int
    selected_control_flow: int
    selected_dead_code: int
    selected_anti_tamper: int
    selected_payload_layers: int
    selected_decoder_variants: int
    selected_chunk_size: int


_KEYWORDS = {
    "function", "if", "elseif", "else", "for", "while", "repeat", "until",
    "do", "local", "return", "break", "continue", "and", "or", "not",
    "in", "pairs", "ipairs", "goto",
}


def _profile_source(source: bytes) -> _SourceProfile:
    tokens = scan_tokens(source)
    counts = Counter()
    identifiers: list[bytes] = []
    depth = 0
    max_depth = 0
    call_count = 0
    operator_count = 0
    previous = None

    for token in tokens:
        text = token.text
        lower = text.decode("utf-8", "ignore").lower() if token.kind in {"identifier", "number"} else ""
        if token.kind == "identifier":
            identifiers.append(text)
            counts[lower] += 1
        elif token.kind in {"string", "long_string"}:
            counts["string"] += 1
        elif token.kind == "number":
            counts["number"] += 1
        elif token.kind == "punct":
            if text in (b"(", b"[", b"{"):
                depth += 1
                max_depth = max(max_depth, depth)
            elif text in (b")", b"]", b"}"):
                depth = max(0, depth - 1)
            if text in (b".", b":", b"[", b"]"):
                counts["index"] += 1
            if text in (b"=", b"+", b"-", b"*", b"/", b"%", b"^", b"<", b">", b"~", b"&", b"|"):
                operator_count += 1
            if text == b"(" and previous is not None and previous.kind == "identifier":
                prev = previous.text.decode("utf-8", "ignore").lower()
                if prev not in {"if", "for", "while", "function", "elseif", "until"}:
                    call_count += 1
        previous = token

    functions = counts["function"]
    branches = counts["if"] + counts["elseif"] + counts["else"]
    loops = counts["for"] + counts["while"] + counts["repeat"] + counts["until"]
    tables = sum(1 for token in tokens if token.text == b"{")
    indexes = counts["index"]
    token_count = len(tokens)
    identifier_count = len(identifiers)
    unique_ids = len(set(identifiers))
    strings = counts["string"]
    numbers = counts["number"]

    # This score is deliberately based on structural diversity rather than only
    # source length. A small callback-heavy Roblox script can therefore receive
    # a higher build profile than a much larger flat script.
    score = 0.0
    score += min(18.0, math.log2(max(2, len(source))) * 1.9)
    score += min(14.0, math.log2(max(2, token_count)) * 1.7)
    score += min(18.0, functions * 3.0 + branches * 1.25 + loops * 1.75)
    score += min(14.0, tables * 1.35 + indexes * 0.85 + call_count * 0.75)
    score += min(12.0, strings * 0.55 + numbers * 0.35 + operator_count * 0.18)
    score += min(10.0, max_depth * 1.5)
    if identifier_count:
        score += min(6.0, (unique_ids / identifier_count) * 6.0)
    score = int(max(0, min(100, round(score))))
    return _SourceProfile(
        size=len(source), tokens=token_count, identifiers=identifier_count,
        unique_identifiers=unique_ids, strings=strings, numbers=numbers,
        functions=functions, branches=branches, loops=loops, tables=tables,
        indexes=indexes, calls=call_count, operators=operator_count,
        nesting=max_depth, score=score,
    )


def _budget_range(
    rng: random.Random, baseline: int, score: int, minimum: int, maximum: int,
    low_bias: float = 0.55, high_bias: float = 1.8, spread: float = 0.22,
) -> tuple[int, int]:
    baseline = max(1, int(baseline))
    complexity = score / 100.0
    low_factor = low_bias + complexity * 0.55
    high_factor = 0.95 + complexity * (high_bias - 0.95)
    low = int(round(baseline * low_factor * rng.uniform(1.0 - spread, 1.0 + spread)))
    high = int(round(baseline * high_factor * rng.uniform(1.0 - spread, 1.0 + spread)))
    low = max(minimum, min(maximum, low))
    high = max(low, min(maximum, high))
    return low, high


def _select_build_budget(
    source: bytes, rng: random.Random, *, junk: int, control_flow_decoys: int,
    dead_code_blocks: int, anti_tamper_checks: int, payload_layers: int,
) -> _BuildBudget:
    profile = _profile_source(source)
    score = profile.score
    junk_range = _budget_range(rng, junk + profile.functions * 2, score, 2, 52)
    control_range = _budget_range(rng, control_flow_decoys + profile.branches + profile.calls, score, 1, 64)
    dead_range = _budget_range(rng, dead_code_blocks + max(1, profile.functions // 2), score, 1, 40)
    anti_range = _budget_range(rng, anti_tamper_checks + max(1, profile.indexes // 3), score, 2, 14, spread=0.18)
    layer_min = 1 if score < 25 else 2
    layer_max = 2
    payload_range = (layer_min, layer_max)
    decoder_range = (2 if score < 45 else 3, 3 if score < 70 else 4)
    chunk_hi = max(48, 128 - int(score * 0.55))
    chunk_lo = max(24, chunk_hi - rng.randint(20, 48))
    chunk_size_range = (chunk_lo, chunk_hi)

    def choose(pair: tuple[int, int]) -> int:
        return rng.randint(pair[0], pair[1])

    return _BuildBudget(
        score=score,
        junk_range=junk_range,
        control_flow_range=control_range,
        dead_code_range=dead_range,
        anti_tamper_range=anti_range,
        payload_layers_range=payload_range,
        decoder_variants_range=decoder_range,
        chunk_size_range=chunk_size_range,
        selected_junk=choose(junk_range),
        selected_control_flow=choose(control_range),
        selected_dead_code=choose(dead_range),
        selected_anti_tamper=choose(anti_range),
        selected_payload_layers=choose(payload_range),
        selected_decoder_variants=choose(decoder_range),
        selected_chunk_size=choose(chunk_size_range),
    )


def _u32(value: int) -> int:
    return value & MASK32


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return abs(a)


def _mod_inverse(value: int, modulus: int) -> int:
    """Return the multiplicative inverse for a coprime pair."""
    t, new_t = 0, 1
    r, new_r = modulus, value % modulus
    while new_r:
        q = r // new_r
        t, new_t = new_t, t - q * new_t
        r, new_r = new_r, r - q * new_r
    if r != 1:
        raise ValueError("value has no modular inverse")
    return t % modulus


def _coprime_multiplier(rng: random.Random, modulus: int) -> tuple[int, int]:
    if modulus <= 1:
        return 1, 0
    candidates = list(range(1, modulus))
    rng.shuffle(candidates)
    for mul in candidates:
        if _gcd(mul, modulus) == 1:
            return mul, rng.randrange(modulus)
    return 1, 0


def _xorshift32(value: int, shifts: tuple[int, int, int]) -> int:
    x = value & MASK32
    a, b, c = shifts
    x ^= (x << a) & MASK32
    x ^= x >> b
    x ^= (x << c) & MASK32
    return x & MASK32


def _hash32(data: bytes, seed: int) -> int:
    h = (seed ^ 0x9E3779B9) & MASK32
    for byte in data:
        h ^= byte
        h ^= (h << 5) & MASK32
        h ^= h >> 7
        h ^= (h << 11) & MASK32
        h = (h + 0x7F4A7C15) & MASK32
    return h


def _pack_words(values: list[int]) -> list[int]:
    result: list[int] = []
    for i in range(0, len(values), 4):
        block = values[i : i + 4]
        result.append(
            block[0]
            | ((block[1] if len(block) > 1 else 0) << 8)
            | ((block[2] if len(block) > 2 else 0) << 16)
            | ((block[3] if len(block) > 3 else 0) << 24)
        )
    return result


_LAYER2_CONST = 0xD1B54A32
_LAYER2_STREAM = 0x6E624EB7


def _rotl8(value: int, amount: int) -> int:
    amount %= 8
    value &= 0xFF
    return ((value << amount) | (value >> (8 - amount))) & 0xFF if amount else value


def _rotr8(value: int, amount: int) -> int:
    amount %= 8
    value &= 0xFF
    return ((value >> amount) | (value << (8 - amount))) & 0xFF if amount else value


def _layer2_params(root_key: int, chunk_key: int) -> tuple[int, int, int, int, tuple[int, int, int]]:
    seed = _xorshift32(_u32(root_key ^ chunk_key ^ _LAYER2_CONST), (17, 7, 11))
    variant = seed & 3
    rotate = 1 + ((seed >> 3) & 7)
    add = 1 + ((seed >> 6) % 251)
    step = 1 + ((seed >> 14) & 31)
    shifts = (5 + ((seed >> 19) & 3), 9 + ((seed >> 21) & 7), 5 + ((seed >> 25) & 7))
    return variant, rotate, add, step, shifts


def _layer2_transform(data: bytes, root_key: int, chunk_key: int, *, decrypt: bool = False) -> bytes:
    variant, rotate, add, step, shifts = _layer2_params(root_key, chunk_key)
    stream = _u32(root_key ^ chunk_key ^ _LAYER2_STREAM)
    out = bytearray(len(data))
    for i, byte in enumerate(data):
        stream = _xorshift32(_u32(stream ^ ((i + 1) * 0x9E3779B9)), shifts)
        key_byte = stream & 0xFF
        pos_add = (add + i * step) & 0xFF
        if not decrypt:
            if variant == 0:
                t = (_rotl8(byte ^ key_byte, rotate) + pos_add) & 0xFF
            elif variant == 1:
                t = _rotl8((byte + pos_add) & 0xFF, rotate) ^ key_byte
            elif variant == 2:
                t = (_rotl8(byte, rotate) ^ key_byte) + pos_add
                t &= 0xFF
            else:
                t = _rotl8((byte ^ key_byte) + pos_add, rotate)
        else:
            if variant == 0:
                t = _rotr8((byte - pos_add) & 0xFF, rotate) ^ key_byte
            elif variant == 1:
                t = _rotr8((byte ^ key_byte) & 0xFF, rotate)
                t = (t - pos_add) & 0xFF
            elif variant == 2:
                t = _rotr8(((byte - pos_add) & 0xFF) ^ key_byte, rotate)
            else:
                t = ((_rotr8(byte, rotate) - pos_add) & 0xFF) ^ key_byte
        out[i] = t & 0xFF
    return bytes(out)


def _record_guard(op: int, next_pc: int, alt_pc: int, a0: int, a1: int, meta: int, root_key: int) -> int:
    h = _u32(root_key ^ 0xA1B2C3D4)
    for value in (op, next_pc, alt_pc, a0, a1, meta):
        h = _u32(h ^ value)
        h = _u32(h ^ ((h << 5) & MASK32))
        h = _u32(h ^ (h >> 7))
        h = _u32(h ^ ((h << 11) & MASK32))
        h = _u32(h + 0x7F4A7C15)
    return h


def _schema_guard(values: list[int], root_key: int) -> int:
    h = _u32(root_key ^ 0xC6EF3720)
    for value in values:
        h = _u32(h ^ value)
        h = _u32(h ^ ((h << 7) & MASK32))
        h = _u32(h ^ (h >> 9))
        h = _u32(h ^ ((h << 13) & MASK32))
        h = _u32(h + 0xB5297A4D)
    return h


def _records_seal(records: list[dict[str, object]], root_key: int) -> int:
    """Compact authenticated snapshot of VM record metadata.

    The payload bytes themselves are checked when decoded; this seal protects
    the graph topology and operand metadata up front and can be sampled again
    during execution.
    """
    h = _u32(root_key ^ 0x6D2B79F5)
    for row in records:
        payload = row.get("payload") if isinstance(row.get("payload"), list) else []
        values = (
            int(row["op"]), int(row["next"]), int(row["alt"]),
            int(row["a0"]), int(row["a1"]), int(row["meta"]),
            int(row["check"]), len(payload),
            _u32((int(payload[0]) if payload else 0) ^ (int(payload[-1]) if payload else 0)),
        )
        for value in values:
            h = _u32(h ^ value)
            h = _u32(h ^ ((h << 9) & MASK32))
            h = _u32(h ^ (h >> 13))
            h = _u32(h + 0x9E3779B9)
    return h


def _encrypt_chunk(
    chunk: bytes,
    *,
    root_key: int,
    chunk_key: int,
    variant: int,
    rotate: int,
    add: int,
    step: int,
    shifts: tuple[int, int, int],
    perm_mul: int,
    perm_add: int,
    data_mask: int,
    payload_layers: int = 2,
) -> list[int]:
    transformed = bytearray(len(chunk))
    stream = _u32(root_key ^ chunk_key)
    for i, byte in enumerate(chunk):
        stream = _xorshift32(_u32(stream ^ ((i + 1) * 0x45D9)), shifts)
        t = byte ^ (stream & 0xFF)
        if variant == 0:
            t = ((t << rotate) | (t >> (8 - rotate))) & 0xFF
            t = (t + add + i * step) & 0xFF
        elif variant == 1:
            t = (t + add) & 0xFF
            t = ((t << rotate) | (t >> (8 - rotate))) & 0xFF
            t ^= ((i * step) + add) & 0xFF
        elif variant == 2:
            t ^= ((i * step) + add) & 0xFF
            t = (t - add) & 0xFF
            t = ((t << rotate) | (t >> (8 - rotate))) & 0xFF
        else:
            t ^= (i * step) & 0xFF
            t = ((t << rotate) | (t >> (8 - rotate))) & 0xFF
            t = (t + add + ((i ^ step) & 0xFF)) & 0xFF
        transformed[i] = t

    if payload_layers >= 2:
        transformed[:] = _layer2_transform(bytes(transformed), root_key, chunk_key)

    words = _pack_words(list(transformed))
    out = [0] * len(words)
    word_count = max(1, len(words))
    for i, word in enumerate(words):
        lane_mask = _u32(chunk_key ^ ((i + 1) * 0x9E3779B1))
        stored = word ^ lane_mask ^ data_mask
        target = (i * perm_mul + perm_add) % word_count
        out[target] = _u32(stored)
    return out


def _lua_name(rng: random.Random, used: set[str], size: int = 9) -> str:
    alphabet = "IlOoQqZz"
    while True:
        candidate = "_" + "".join(rng.choice(alphabet) for _ in range(rng.randint(size - 2, size + 2)))
        if candidate not in used:
            used.add(candidate)
            return candidate


def _num(value: int) -> str:
    return str(_u32(value))


def build_vm(
    source: bytes,
    *,
    seed: int,
    junk: int = 9,
    control_flow_decoys: int = 16,
    dead_code_blocks: int = 12,
    anti_tamper_checks: int = 4,
    payload_layers: int = 2,
) -> VMArtifact:
    rng = random.Random(seed ^ 0xC0DE5A17)
    root_key = rng.randrange(1, MASK32)
    global_hash = _hash32(source, root_key)

    # Independent recovery payload. The primary VM is deliberately complex,
    # but it must never be able to fail closed and silently consume a valid
    # user program. This second per-build codec is only used when the primary
    # dispatcher never reaches native execution.
    recovery_key = rng.randrange(1, MASK32)
    recovery_seed = rng.randrange(1, MASK32)
    recovery_salt = rng.randrange(1, MASK32)
    recovery_step = rng.randrange(1, 253)
    recovery_shifts = (17, 7, 11)
    recovery_payload: list[int] = []
    recovery_stream = _u32(recovery_key ^ recovery_seed)
    for i, byte in enumerate(source):
        recovery_stream = _xorshift32(
            _u32(recovery_stream ^ ((i + 1) * 0x9E3779B9) ^ recovery_salt),
            recovery_shifts,
        )
        value = byte ^ (recovery_stream & 0xFF)
        value ^= ((i * recovery_step + recovery_salt) & 0xFF)
        recovery_payload.append(value & 0xFF)

    # First derive the source's structural complexity and then choose a fresh
    # randomized budget range for every major VM subsystem. The configured
    # values are only baseline intensity anchors; they are never emitted as
    # fixed per-build counts.
    budget = _select_build_budget(
        source, rng, junk=junk, control_flow_decoys=control_flow_decoys,
        dead_code_blocks=dead_code_blocks, anti_tamper_checks=anti_tamper_checks,
        payload_layers=payload_layers,
    )
    junk_budget = budget.selected_junk
    cf_budget = budget.selected_control_flow
    dead_budget = budget.selected_dead_code
    anti_budget = budget.selected_anti_tamper
    selected_payload_layers = budget.selected_payload_layers
    selected_decoder_variants = budget.selected_decoder_variants


    decoder_shifts = [
        (13, 17, 5),
        (11, 19, 8),
        (7, 9, 13),
        (5, 13, 6),
        (15, 11, 7),
        (9, 13, 7),
        (17, 7, 11),
    ]
    rng.shuffle(decoder_shifts)
    decoder_shifts = decoder_shifts[:selected_decoder_variants]

    chunk_size = budget.selected_chunk_size
    chunks = [source[i : i + chunk_size] for i in range(0, len(source), chunk_size)] or [b""]

    # Random build-level masks.
    op_mask = rng.randrange(1, 256)
    pc_mask = rng.randrange(1, MASK32)
    arg_mask = rng.randrange(1, MASK32)
    meta_mask = rng.randrange(1, MASK32)
    check_mask = rng.randrange(1, MASK32)
    data_mask = rng.randrange(1, MASK32)
    shadow_salt = rng.randrange(1, MASK32)

    # A physical record contains eight fields, but their meaning changes per
    # build. This defeats tools that expect [opcode,next,arg,payload,...].
    field_names = ("op", "next", "alt", "a0", "a1", "meta", "payload", "check")
    shuffled_fields = list(range(1, 9))
    rng.shuffle(shuffled_fields)
    field = {name: shuffled_fields[i] for i, name in enumerate(field_names)}

    # Random virtual opcode values. The values actually stored in the record
    # are still masked with op_mask.
    virtual_ops: dict[str, int] = {}
    used_ops: set[int] = set()
    for logical in ("init", "decode", "mix", "branch", "verify", "exec", "halt"):
        value = rng.randrange(3, 253)
        while value in used_ops:
            value = rng.randrange(3, 253)
        used_ops.add(value)
        virtual_ops[logical] = value

    # Build the semantic graph in logical order first.
    graph: list[dict[str, object]] = []

    def add(**row: object) -> int:
        graph.append(row)
        return len(graph)

    entry = add(op=virtual_ops["init"], next=0, alt=0, a0=root_key, a1=0, meta=0, payload=[], check=0)
    previous = entry
    opaque_edges = 0

    for chunk_index, chunk in enumerate(chunks):
        variant = rng.randrange(selected_decoder_variants)
        rotate = rng.randrange(1, 8)
        add_value = rng.randrange(1, 253)
        step = rng.randrange(1, 31)
        word_count = max(1, math.ceil(len(chunk) / 4))
        perm_mul, perm_add = _coprime_multiplier(rng, word_count)
        inv_mul = _mod_inverse(perm_mul, word_count) if word_count > 1 else 1
        payload_key = rng.randrange(1, MASK32)
        payload = _encrypt_chunk(
            chunk,
            root_key=root_key,
            chunk_key=payload_key,
            variant=variant,
            rotate=rotate,
            add=add_value,
            step=step,
            shifts=decoder_shifts[variant],
            perm_mul=perm_mul,
            perm_add=perm_add,
            data_mask=data_mask,
            payload_layers=selected_payload_layers,
        )
        meta = (
            variant
            | (rotate << 2)
            | (add_value << 5)
            | (step << 13)
            | (inv_mul << 19)
            | (perm_add << 24)
        )
        decode_idx = add(
            op=virtual_ops["decode"],
            next=0,
            alt=0,
            a0=payload_key,
            a1=len(chunk),
            meta=meta,
            payload=payload,
            check=_hash32(chunk, payload_key),
        )
        graph[previous - 1]["next"] = decode_idx
        previous = decode_idx

        # A randomized number of reachable shadow operations follows real work.
        # They mutate only an opaque accumulator, so the reconstructed source
        # remains untouched.
        for _ in range(rng.randint(0, max(1, junk_budget // 2))):
            mix_idx = add(
                op=virtual_ops["mix"],
                next=0,
                alt=0,
                a0=rng.randrange(1, MASK32),
                a1=rng.randrange(1, MASK32),
                meta=rng.randrange(1, MASK32),
                payload=[],
                check=0,
            )
            graph[previous - 1]["next"] = mix_idx
            previous = mix_idx

        if chunk_index < len(chunks) - 1 and rng.randrange(3) == 0:
            branch_idx = add(
                op=virtual_ops["branch"],
                next=0,
                alt=0,
                a0=rng.randrange(1, MASK32),
                a1=rng.randrange(1, MASK32),
                meta=rng.randrange(1, MASK32),
                payload=[],
                check=0,
            )
            graph[previous - 1]["next"] = branch_idx

            left = add(
                op=virtual_ops["mix"],
                next=0,
                alt=0,
                a0=rng.randrange(1, MASK32),
                a1=rng.randrange(1, MASK32),
                meta=rng.randrange(1, MASK32),
                payload=[],
                check=0,
            )
            right = add(
                op=virtual_ops["mix"],
                next=0,
                alt=0,
                a0=rng.randrange(1, MASK32),
                a1=rng.randrange(1, MASK32),
                meta=rng.randrange(1, MASK32),
                payload=[],
                check=0,
            )
            join = add(
                op=virtual_ops["mix"],
                next=0,
                alt=0,
                a0=rng.randrange(1, MASK32),
                a1=rng.randrange(1, MASK32),
                meta=rng.randrange(1, MASK32),
                payload=[],
                check=0,
            )
            graph[branch_idx - 1]["next"] = left
            graph[branch_idx - 1]["alt"] = right
            graph[left - 1]["next"] = join
            graph[right - 1]["next"] = join
            previous = join
            opaque_edges += 1

        island_count = max(0, min(5, cf_budget // max(1, len(chunks))))
        if chunk_index < len(chunks) - 1:
            for _ in range(island_count):
                branch = add(op=virtual_ops["branch"], next=0, alt=0, a0=rng.randrange(1, MASK32), a1=rng.randrange(1, MASK32), meta=rng.randrange(1, MASK32), payload=[], check=0)
                left = add(op=virtual_ops["mix"], next=0, alt=0, a0=rng.randrange(1, MASK32), a1=rng.randrange(1, MASK32), meta=rng.randrange(1, MASK32), payload=[], check=0)
                right = add(op=virtual_ops["mix"], next=0, alt=0, a0=rng.randrange(1, MASK32), a1=rng.randrange(1, MASK32), meta=rng.randrange(1, MASK32), payload=[], check=0)
                join = add(op=virtual_ops["mix"], next=0, alt=0, a0=rng.randrange(1, MASK32), a1=rng.randrange(1, MASK32), meta=rng.randrange(1, MASK32), payload=[], check=0)
                graph[previous - 1]["next"] = branch
                graph[branch - 1]["next"] = left
                graph[branch - 1]["alt"] = right
                graph[left - 1]["next"] = join
                graph[right - 1]["next"] = join
                previous = join
                opaque_edges += 1

    for _ in range(max(0, cf_budget // 2)):
        orphan = add(op=virtual_ops["branch"], next=0, alt=0, a0=rng.randrange(1, MASK32), a1=rng.randrange(1, MASK32), meta=rng.randrange(1, MASK32), payload=[], check=0)
        a = add(op=virtual_ops["mix"], next=0, alt=0, a0=rng.randrange(1, MASK32), a1=rng.randrange(1, MASK32), meta=rng.randrange(1, MASK32), payload=[], check=0)
        b = add(op=virtual_ops["mix"], next=0, alt=0, a0=rng.randrange(1, MASK32), a1=rng.randrange(1, MASK32), meta=rng.randrange(1, MASK32), payload=[], check=0)
        graph[orphan - 1]["next"] = a
        graph[orphan - 1]["alt"] = b

    terminal_islands = min(6, max(1, cf_budget // max(4, rng.randint(5, 11)))) if cf_budget else 0
    for _ in range(terminal_islands):
        branch = add(op=virtual_ops["branch"], next=0, alt=0, a0=rng.randrange(1, MASK32), a1=rng.randrange(1, MASK32), meta=rng.randrange(1, MASK32), payload=[], check=0)
        left = add(op=virtual_ops["mix"], next=0, alt=0, a0=rng.randrange(1, MASK32), a1=rng.randrange(1, MASK32), meta=rng.randrange(1, MASK32), payload=[], check=0)
        right = add(op=virtual_ops["mix"], next=0, alt=0, a0=rng.randrange(1, MASK32), a1=rng.randrange(1, MASK32), meta=rng.randrange(1, MASK32), payload=[], check=0)
        join = add(op=virtual_ops["mix"], next=0, alt=0, a0=rng.randrange(1, MASK32), a1=rng.randrange(1, MASK32), meta=rng.randrange(1, MASK32), payload=[], check=0)
        graph[previous - 1]["next"] = branch
        graph[branch - 1]["next"] = left
        graph[branch - 1]["alt"] = right
        graph[left - 1]["next"] = join
        graph[right - 1]["next"] = join
        previous = join
        opaque_edges += 1

    verify_idx = add(
        op=virtual_ops["verify"],
        next=0,
        alt=0,
        a0=global_hash,
        a1=root_key,
        meta=rng.randrange(1, MASK32),
        payload=[],
        check=0,
    )
    exec_idx = add(
        op=virtual_ops["exec"],
        next=0,
        alt=0,
        a0=rng.randrange(1, MASK32),
        a1=rng.randrange(1, MASK32),
        meta=rng.randrange(1, MASK32),
        payload=[],
        check=0,
    )
    halt_idx = add(op=virtual_ops["halt"], next=0, alt=0, a0=0, a1=0, meta=0, payload=[], check=0)
    graph[previous - 1]["next"] = verify_idx
    graph[verify_idx - 1]["next"] = exec_idx
    graph[exec_idx - 1]["next"] = halt_idx
    graph[halt_idx - 1]["next"] = 0

    # Shuffle physical record order after graph construction.
    order = list(range(1, len(graph) + 1))
    rng.shuffle(order)
    old_to_new = {old: new for new, old in enumerate(order, 1)}
    records = [graph[old - 1].copy() for old in order]
    for row in records:
        row["next"] = old_to_new[int(row["next"])] if int(row["next"]) else 0
        row["alt"] = old_to_new[int(row["alt"])] if int(row["alt"]) else 0
    entry_physical = old_to_new[entry]

    # Generate a fresh symbol universe for every build.
    used_names: set[str] = set()
    n = {key: _lua_name(rng, used_names) for key in (
        "bit", "bxor", "band", "bor", "lshift", "rshift", "char", "concat", "load", "records",
        "handlers", "schema", "out", "state", "shadow", "steps", "record", "opcode", "handler",
        "source", "schema_ref", "fail", "hash", "decode0", "decode1", "decode2", "decode3", "bytebuf",
        "word", "lane", "original", "stream", "value", "next", "target", "variant", "meta", "rotate",
        "add", "step", "inverse", "permadd", "key", "len", "check", "plaintext", "fieldop", "fieldnext",
        "fieldalt", "fielda0", "fielda1", "fieldmeta", "fieldpayload", "fieldcheck", "entry", "counter", "loader", "pack", "unpack", "returns", "lane_mask",
        "active", "tamper", "guard", "boot", "schema_guard", "record_seal", "sample_table", "sample_index", "sample_record", "check_tick", "layer_seed", "layer_mode", "layer_rotate", "layer_add", "layer_step", "layer_sa", "layer_sb", "layer_sc", "stream2", "keybyte", "posadd", "decoy", "recovery", "recovery_payload", "executed",
    )}

    # Runtime uses these aliases in deliberately non-obvious combinations.
    alias_setup = (
        f"local {n['bit']}=bit32;local {n['bxor']}={n['bit']}.bxor;local {n['band']}={n['bit']}.band;"
        f"local {n['bor']}={n['bit']}.bor;local {n['lshift']}={n['bit']}.lshift;local {n['rshift']}={n['bit']}.rshift;"
        f"local {n['char']}=string.char;local {n['concat']}=table.concat;local {n['loader']}=loadstring;"
        f"local {n['pack']}=table.pack or function(...)return {{n=select('#',...),...}}end;local {n['unpack']}=table.unpack or unpack;"
    )

    # Encode physical records with randomized field placement.
    rows: list[str] = []
    for row in records:
        if int(row["op"]) != virtual_ops["decode"]:
            row["check"] = _record_guard(
                int(row["op"]), int(row["next"]), int(row["alt"]),
                int(row["a0"]), int(row["a1"]), int(row["meta"]), root_key,
            )
        payload = row["payload"] if isinstance(row["payload"], list) else []
        encoded: list[object] = [0] * 8
        encoded[field["op"] - 1] = _u32(int(row["op"]) ^ op_mask)
        encoded[field["next"] - 1] = _u32(int(row["next"]) ^ pc_mask)
        encoded[field["alt"] - 1] = _u32(int(row["alt"]) ^ pc_mask)
        encoded[field["a0"] - 1] = _u32(int(row["a0"]) ^ arg_mask)
        encoded[field["a1"] - 1] = _u32(int(row["a1"]) ^ arg_mask)
        encoded[field["meta"] - 1] = _u32(int(row["meta"]) ^ meta_mask)
        encoded[field["payload"] - 1] = [_u32(int(x)) for x in payload]
        encoded[field["check"] - 1] = _u32(int(row["check"]) ^ check_mask)
        rows.append(
            "{" + ",".join(
                "{" + ",".join(_num(v) for v in value) + "}" if isinstance(value, list) else _num(int(value))
                for value in encoded
            ) + "}"
        )

    schema_literal = "{" + ",".join(_num(field[k]) for k in field_names) + "}"
    records_literal = "{" + ",".join(rows) + "}"
    record_seal = _records_seal(records, root_key)
    sample_candidates = [
        idx for idx, row in enumerate(records, 1)
        if int(row["op"]) != virtual_ops["decode"]
    ]
    rng.shuffle(sample_candidates)
    sample_count = min(int(anti_budget), len(sample_candidates)) if anti_budget > 0 and sample_candidates else 0
    sample_indices = sample_candidates[:sample_count]
    sample_literal = "{" + ",".join(_num(i) for i in sample_indices) + "}"

    # Small exact runtime hash, generated separately from payload decryption.
    hash_fn = (
        f"local function {n['hash']}(s,k) local h={n['bxor']}(k,{_num(0x9E3779B9)});"
        f"for i=1,#s do h={n['bxor']}(h,string.byte(s,i));h={n['bxor']}(h,{n['lshift']}(h,5));"
        f"h={n['bxor']}(h,{n['rshift']}(h,7));h={n['bxor']}(h,{n['lshift']}(h,11));"
        f"h=(h+{_num(0x7F4A7C15)})%4294967296;end;return h end;"
    )

    # The selected decoder families share the same data layout but differ in their
    # byte arithmetic. A build can therefore be recognized as one of many
    # decoder families, rather than one fixed decoder function.
    decoder_functions: list[str] = []
    for variant, (sa, sb, sc) in enumerate(decoder_shifts):
        fn = n[f"decode{variant}"]
        if variant == 0:
            inverse = (
                f"v=(v-{n['add']}-((i-1)*{n['step']}))%256;"
                f"v={n['band']}({n['bor']}({n['lshift']}(v,8-{n['rotate']}),{n['rshift']}(v,{n['rotate']})),255);"
            )
        elif variant == 1:
            inverse = (
                f"v={n['bxor']}(v,(((i-1)*{n['step']}+{n['add']})%256));"
                f"v={n['band']}({n['bor']}({n['lshift']}(v,8-{n['rotate']}),{n['rshift']}(v,{n['rotate']})),255);"
                f"v=(v-{n['add']})%256;"
            )
        elif variant == 2:
            inverse = (
                f"v={n['band']}({n['bor']}({n['lshift']}(v,8-{n['rotate']}),{n['rshift']}(v,{n['rotate']})),255);"
                f"v=(v+{n['add']})%256;v={n['bxor']}(v,(((i-1)*{n['step']}+{n['add']})%256));"
            )
        else:
            inverse = (
                f"v=(v-{n['add']}-{n['bxor']}(i-1,{n['step']})%256)%256;"
                f"v={n['band']}({n['bor']}({n['lshift']}(v,8-{n['rotate']}),{n['rshift']}(v,{n['rotate']})),255);"
                f"v={n['bxor']}(v,(((i-1)*{n['step']})%256));"
            )
        layer2_decode = (
            f"local {n['layer_seed']}={n['bxor']}(seed,{n['key']},{_num(_LAYER2_CONST)});"
            f"{n['layer_seed']}={n['bxor']}({n['layer_seed']},{n['lshift']}({n['layer_seed']},17));{n['layer_seed']}={n['bxor']}({n['layer_seed']},{n['rshift']}({n['layer_seed']},7));{n['layer_seed']}={n['bxor']}({n['layer_seed']},{n['lshift']}({n['layer_seed']},11));"
            f"local {n['layer_mode']}={n['band']}({n['layer_seed']},3);local {n['layer_rotate']}=1+{n['band']}({n['rshift']}({n['layer_seed']},3),7);"
            f"local {n['layer_add']}=1+({n['rshift']}({n['layer_seed']},6)%251);local {n['layer_step']}=1+{n['band']}({n['rshift']}({n['layer_seed']},14),31);"
            f"local {n['layer_sa']}=5+{n['band']}({n['rshift']}({n['layer_seed']},19),3);local {n['layer_sb']}=9+{n['band']}({n['rshift']}({n['layer_seed']},21),7);local {n['layer_sc']}=5+{n['band']}({n['rshift']}({n['layer_seed']},25),7);"
            ""
        ) if selected_payload_layers >= 2 else ""
        layer2_prefix = (
            f"local {n['stream2']}={n['bxor']}(seed,{n['key']},{_num(_LAYER2_STREAM)});"
            f"{n['stream2']}={n['bxor']}({n['stream2']},((i*{_num(0x9E3779B9)})%4294967296));{n['stream2']}={n['bxor']}({n['stream2']},{n['bxor']}({n['lshift']}({n['stream2']},{n['layer_sa']}),0));{n['stream2']}={n['bxor']}({n['stream2']},{n['rshift']}({n['stream2']},{n['layer_sb']}));{n['stream2']}={n['bxor']}({n['stream2']},{n['lshift']}({n['stream2']},{n['layer_sc']}));"
            f"local {n['keybyte']}={n['band']}({n['stream2']},255);local {n['posadd']}=({n['layer_add']}+(i-1)*{n['layer_step']})%256;"
            f"if {n['layer_mode']}==0 then v=(v-{n['posadd']})%256;v={n['band']}({n['bor']}({n['rshift']}(v,{n['layer_rotate']}),{n['lshift']}(v,8-{n['layer_rotate']})),255);v={n['bxor']}(v,{n['keybyte']});"
            f"elseif {n['layer_mode']}==1 then v={n['bxor']}(v,{n['keybyte']});v={n['band']}({n['bor']}({n['rshift']}(v,{n['layer_rotate']}),{n['lshift']}(v,8-{n['layer_rotate']})),255);v=(v-{n['posadd']})%256;"
            f"elseif {n['layer_mode']}==2 then v=(v-{n['posadd']})%256;v={n['bxor']}(v,{n['keybyte']});v={n['band']}({n['bor']}({n['rshift']}(v,{n['layer_rotate']}),{n['lshift']}(v,8-{n['layer_rotate']})),255);"
            f"else v={n['band']}({n['bor']}({n['rshift']}(v,{n['layer_rotate']}),{n['lshift']}(v,8-{n['layer_rotate']})),255);v=(v-{n['posadd']})%256;v={n['bxor']}(v,{n['keybyte']});end;"
        ) if selected_payload_layers >= 2 else ""
        decoder_functions.append(
            f"local function {fn}(words,{n['key']},{n['len']},{n['rotate']},{n['add']},{n['step']},{n['inverse']},{n['permadd']},{n['bytebuf']},seed)"
            f"local wordcount=math.max(1,#words);local packed={{}};"
            f"for wi=1,#words do local raw={n['bxor']}(words[wi],{_num(data_mask)});"
            f"local b0={n['band']}(raw,255);local b1={n['band']}({n['rshift']}(raw,8),255);"
            f"local b2={n['band']}({n['rshift']}(raw,16),255);local b3={n['band']}({n['rshift']}(raw,24),255);"
            f"local target=wi-1;local original=((target-{n['permadd']})%wordcount*{n['inverse']})%wordcount;"
            f"local {n['lane_mask']}={n['bxor']}({n['key']},(((original+1)*{_num(0x9E3779B1)})%4294967296));"
            f"local lane0={n['band']}({n['lane_mask']},255);local lane1={n['band']}({n['rshift']}({n['lane_mask']},8),255);"
            f"local lane2={n['band']}({n['rshift']}({n['lane_mask']},16),255);local lane3={n['band']}({n['rshift']}({n['lane_mask']},24),255);"
            f"b0={n['bxor']}(b0,lane0);b1={n['bxor']}(b1,lane1);b2={n['bxor']}(b2,lane2);b3={n['bxor']}(b3,lane3);"
            f"packed[original*4+1]=b0;packed[original*4+2]=b1;packed[original*4+3]=b2;packed[original*4+4]=b3;end;"
            f"local stream={n['bxor']}(seed,{n['key']});local out={{}};"
            + layer2_decode +
            f"for i=1,{n['len']} do stream={n['bxor']}(stream,((i*{_num(0x45D9)})%4294967296));"
            f"stream={n['bxor']}(stream,{n['bxor']}({n['lshift']}(stream,{sa}),0));stream={n['bxor']}(stream,{n['rshift']}(stream,{sb}));stream={n['bxor']}(stream,{n['lshift']}(stream,{sc}));"
            f"local v=packed[i] or 0;"
            + layer2_prefix +
            f"{inverse}out[i]={n['bxor']}(v,{n['band']}(stream,255));end;"
            f"local chars={{}};for i=1,{n['len']} do chars[i]=string.char(out[i])end;return {n['concat']}(chars)end;"
        )

    # The lane-masking above is intentionally handled at byte level after the
    # outer word mask. Keeping it verbose provides another polymorphic layer.
    # Generate handler functions in randomized registration order.
    handler_defs: dict[str, str] = {}
    handler_defs["init"] = (
        f"function(r)local a={n['bxor']}(r[{field['a0']}],{_num(arg_mask)});{n['shadow']}={n['bxor']}({n['shadow']},a,{_num(shadow_salt)});"
        f"return r[{field['next']}] end"
    )
    decoder_literal = ",".join(n[f"decode{i}"] for i in range(selected_decoder_variants))
    handler_defs["decode"] = (
        f"function(r)local {n['key']}={n['bxor']}(r[{field['a0']}],{_num(arg_mask)});"
        f"local {n['len']}={n['bxor']}(r[{field['a1']}],{_num(arg_mask)});local {n['meta']}={n['bxor']}(r[{field['meta']}],{_num(meta_mask)});"
        f"local {n['variant']}={n['band']}({n['meta']},3);local {n['rotate']}={n['band']}({n['rshift']}({n['meta']},2),7);"
        f"local {n['add']}={n['band']}({n['rshift']}({n['meta']},5),255);local {n['step']}={n['band']}({n['rshift']}({n['meta']},13),63);"
        f"local {n['inverse']}={n['band']}({n['rshift']}({n['meta']},19),31);if {n['inverse']}<1 then {n['inverse']}=1 end;"
        f"local {n['permadd']}={n['band']}({n['rshift']}({n['meta']},24),31);"
        f"local f=({{{decoder_literal}}})[{n['variant']}+1];"
        f"local {n['plaintext']}=f(r[{field['payload']}],{n['key']},{n['len']},{n['rotate']},{n['add']},{n['step']},{n['inverse']},{n['permadd']},0,{_num(root_key)});"
        f"if {n['hash']}({n['plaintext']},{n['key']})~=" + f"{n['bxor']}(r[{field['check']}],{_num(check_mask)}) then return 0 end;"
        f"{n['out']}[#{n['out']}+1]={n['plaintext']};{n['shadow']}={n['bxor']}({n['shadow']},{n['len']},{n['key']});"
        f"return r[{field['next']}] end"
    )
    handler_defs["mix"] = (
        f"function(r)local a={n['bxor']}(r[{field['a0']}],{_num(arg_mask)});local b={n['bxor']}(r[{field['a1']}],{_num(arg_mask)});"
        f"{n['shadow']}={n['bxor']}({n['shadow']},{n['lshift']}(a,3),{n['rshift']}(b,5));"
        f"return r[{field['next']}] end"
    )
    handler_defs["branch"] = (
        f"function(r)local q={n['bxor']}({n['shadow']},{_num(shadow_salt)});local choose={n['band']}(q,1)==0;"
        f"local target=choose and r[{field['next']}] or r[{field['alt']}];return target end"
    )
    handler_defs["verify"] = (
        f"function(r)local expected={n['bxor']}(r[{field['a0']}],{_num(arg_mask)});if #{n['out']}~={len(chunks)} then return {n['tamper']}() end;"
        f"local bytes=0;for i=1,#{n['out']} do bytes=bytes+#({n['out']}[i]) end;if bytes~={len(source)} then return {n['tamper']}() end;"
        f"local got={n['hash']}({n['concat']}({n['out']}),{_num(root_key)});if got~=expected then return {n['tamper']}() end;"
        f"return r[{field['next']}] end"
    )
    handler_defs["exec"] = (
        f"function(r)if not {n['loader']} then error({n['concat']}({n['char']}({rng.randrange(97,122)},{rng.randrange(97,122)}))) end;"
        f"local fn,err={n['loader']}({n['concat']}({n['out']}));if not fn then error(err)end;{n['executed']}=true;{n['returns']}={n['pack']}(fn());return 0 end"
    )
    handler_defs["halt"] = "function(r)return 0 end"

    handler_order = list(handler_defs)
    rng.shuffle(handler_order)
    handler_assignments: list[str] = []
    for logical in handler_order:
        op = virtual_ops[logical]
        handler_assignments.append(f"{n['handlers']}[{_num(op)}]={handler_defs[logical]};")

    # Generate dead handlers separately so they can be distributed through the
    # generated runtime instead of forming one obvious junk-code appendix.
    dead_opcodes: list[int] = []
    dead_parts: list[str] = []
    for _ in range(max(0, dead_budget)):
        op = rng.randrange(3, 253)
        while op in used_ops or op in dead_opcodes:
            op = rng.randrange(3, 253)
        dead_opcodes.append(op)
        fn = n['decoy'] + str(len(dead_opcodes))
        a = rng.randrange(1, MASK32); b = rng.randrange(1, MASK32); variant = rng.randrange(4)
        if variant == 0:
            body = (
                f"local {fn}=function(r)local a={_num(a)};local b={_num(b)};local x={n['bxor']}(a,b);"
                f"if {n['band']}(x,3)==1 then x={n['bxor']}(x,{n['lshift']}(x,5)) elseif {n['band']}(x,3)==2 then x={n['bxor']}(x,{n['rshift']}(x,7)) else x={n['bxor']}(x,a,b) end;"
                f"local t={{x,a,b}};local y=t[{n['band']}(x,3)+1];if y==nil then y=x end;return {n['bxor']}(y,x) end;"
            )
        elif variant == 1:
            body = (
                f"local {fn}=function(r)local q={{a={_num(a)},b={_num(b)},x={n['bxor']}({_num(a)},{_num(b)})}};"
                f"local k=({'1' if rng.randrange(2) else '2'});local v=q[k==1 and 'a' or 'b'];for i=1,3 do v={n['bxor']}(v,{n['lshift']}(v,i)) end;return {n['bxor']}(v,q.x) end;"
            )
        elif variant == 2:
            body = (
                f"local {fn}=function(r)local x={_num(a)};local y={_num(b)};local z=0;for i=1,4 do z=(z+(x%257)*(i+1)+(y%263))%4294967296;x={n['bxor']}(x,z);end;return z end;"
            )
        else:
            body = (
                f"local {fn}=function(r)local function q(x,y)return {n['bxor']}(x,{n['lshift']}(y,3),{n['rshift']}(x,5)) end;"
                f"local t={{q({_num(a)},{_num(b)}),{_num(a)},{_num(b)}}};local i=1+{n['band']}(t[1],1);return t[i] end;"
            )
        dead_parts.append(body + f"{n['handlers']}[{_num(op)}]={fn};")

    active_ops_literal = "{" + ",".join(f"[{_num(virtual_ops[k])}]=true" for k in virtual_ops) + "}"
    schema_guard = _schema_guard([field[name] for name in ("op", "next", "alt", "a0", "a1", "meta", "payload", "check")], root_key)

    # Dispatcher step itself is generated through a randomly selected shape.
    step_function = (
        f"local function {n['steps']}(r)local raw={n['bxor']}(r[{field['op']}],{_num(op_mask)});local op={n['band']}(raw,255);"
        f"if not {n['active']}[op] then return {n['tamper']}() end;"
        f"if op~={_num(virtual_ops['decode'])} then local g={n['bxor']}(r[{field['check']}],{_num(check_mask)});"
        f"local rop={n['bxor']}(r[{field['op']}],{_num(op_mask)});local rn={n['bxor']}(r[{field['next']}],{_num(pc_mask)});local ra={n['bxor']}(r[{field['alt']}],{_num(pc_mask)});"
        f"local r0={n['bxor']}(r[{field['a0']}],{_num(arg_mask)});local r1={n['bxor']}(r[{field['a1']}],{_num(arg_mask)});local rm={n['bxor']}(r[{field['meta']}],{_num(meta_mask)});"
        f"if g~={n['guard']}(rop,rn,ra,r0,r1,rm) then return {n['tamper']}() end end;"
        f"local fn={n['handlers']}[op];if fn then return fn(r) end;return {n['tamper']}() end;"
    )

    header = (
        alias_setup
        + f"local {n['records']}={records_literal};local {n['handlers']}={{}};local {n['schema']}={schema_literal};"
        + f"local {n['active']}={active_ops_literal};local {n['out']}={{}};local {n['state']}={_num(pc_mask ^ entry_physical)};local {n['shadow']}={_num(root_key ^ shadow_salt)};"
        + f"local {n['returns']}=nil;local {n['counter']}=0;local {n['check_tick']}=0;local {n['executed']}=false;"
        + f"local {n['recovery_payload']}={{{','.join(_num(v) for v in recovery_payload)}}};"
        + f"local function {n['tamper']}() error({n['concat']}({n['char']}({rng.randrange(84,91)},{rng.randrange(97,123)},{rng.randrange(97,123)},{rng.randrange(97,123)}))) end;"
        + f"local function {n['guard']}(opv,np,ap,a0v,a1v,mv)local h={n['bxor']}({_num(root_key ^ 0xA1B2C3D4)},0);"
        + f"h={n['bxor']}(h,opv);h={n['bxor']}(h,{n['lshift']}(h,5));h={n['bxor']}(h,{n['rshift']}(h,7));h={n['bxor']}(h,{n['lshift']}(h,11));h=(h+{_num(0x7F4A7C15)})%4294967296;"
        + f"h={n['bxor']}(h,np);h={n['bxor']}(h,{n['lshift']}(h,5));h={n['bxor']}(h,{n['rshift']}(h,7));h={n['bxor']}(h,{n['lshift']}(h,11));h=(h+{_num(0x7F4A7C15)})%4294967296;"
        + f"h={n['bxor']}(h,ap);h={n['bxor']}(h,{n['lshift']}(h,5));h={n['bxor']}(h,{n['rshift']}(h,7));h={n['bxor']}(h,{n['lshift']}(h,11));h=(h+{_num(0x7F4A7C15)})%4294967296;"
        + f"h={n['bxor']}(h,a0v);h={n['bxor']}(h,{n['lshift']}(h,5));h={n['bxor']}(h,{n['rshift']}(h,7));h={n['bxor']}(h,{n['lshift']}(h,11));h=(h+{_num(0x7F4A7C15)})%4294967296;"
        + f"h={n['bxor']}(h,a1v);h={n['bxor']}(h,{n['lshift']}(h,5));h={n['bxor']}(h,{n['rshift']}(h,7));h={n['bxor']}(h,{n['lshift']}(h,11));h=(h+{_num(0x7F4A7C15)})%4294967296;"
        + f"h={n['bxor']}(h,mv);h={n['bxor']}(h,{n['lshift']}(h,5));h={n['bxor']}(h,{n['rshift']}(h,7));h={n['bxor']}(h,{n['lshift']}(h,11));return (h+{_num(0x7F4A7C15)})%4294967296 end;"
        + f"local {n['schema_guard']}={_num(schema_guard)};local {n['record_seal']}={_num(record_seal)};local {n['sample_table']}={sample_literal};"
        + f"local {n['boot']}={n['bxor']}({_num(root_key ^ 0xC6EF3720)},0);for i=1,8 do {n['boot']}={n['bxor']}({n['boot']},{n['schema']}[i]);{n['boot']}={n['bxor']}({n['boot']},{n['lshift']}({n['boot']},7));{n['boot']}={n['bxor']}({n['boot']},{n['rshift']}({n['boot']},9));{n['boot']}={n['bxor']}({n['boot']},{n['lshift']}({n['boot']},13));{n['boot']}=({n['boot']}+{_num(0xB5297A4D)})%4294967296;end;if {n['boot']}~={n['schema_guard']} then {n['tamper']}() end;"
        + f"{hash_fn}"
    )

    # Distribute inactive handler bodies through the runtime. Decoder locals must
    # remain before the active decode handler, but dead handlers can safely appear
    # between decoder/handler sections because they only depend on aliases from
    # the header.
    runtime_parts: list[str] = [header]
    remaining_dead = list(dead_parts)
    for decoder in decoder_functions:
        runtime_parts.append(decoder)
        if remaining_dead and (rng.random() < 0.75 or not runtime_parts):
            runtime_parts.append(remaining_dead.pop(0))
    remaining_handlers = list(handler_assignments)
    while remaining_handlers:
        runtime_parts.append(remaining_handlers.pop(0))
        if remaining_dead and rng.random() < 0.55:
            runtime_parts.append(remaining_dead.pop(0))
    # Dead handlers may only be inserted after the runtime header. The header
    # initializes the randomized alias set and the handler table; placing a
    # handler before it can attempt to index a nil handler table at startup.
    while remaining_dead:
        insert_at = rng.randrange(1, len(runtime_parts) + 1)
        runtime_parts.insert(insert_at, remaining_dead.pop())
    runtime_parts.append(step_function)
    runtime = "".join(runtime_parts)
    boot_checks = (
        f"if #{n['records']}~={len(records)} then {n['tamper']}() end;"
        f"for i=1,8 do local x={n['schema']}[i];if x==nil or x<1 or x>8 then {n['tamper']}() end end;"
        f"{n['boot']}=0;for i=1,8 do for j=i+1,8 do if {n['schema']}[i]=={n['schema']}[j] then {n['tamper']}() end end end;"
        f"for k,_ in pairs({n['active']}) do if {n['handlers']}[k]==nil then {n['tamper']}() end end;"
        f"{n['boot']}={n['bxor']}({n['schema_guard']},{_num(schema_guard)});if {n['boot']}~=0 then {n['tamper']}() end;"
        f"{n['boot']}={n['bxor']}({_num(root_key ^ 0x6D2B79F5)},0);for i=1,#{n['records']} do local r={n['records']}[i];local op={n['bxor']}(r[{field['op']}],{_num(op_mask)});local np={n['bxor']}(r[{field['next']}],{_num(pc_mask)});local ap={n['bxor']}(r[{field['alt']}],{_num(pc_mask)});local a0={n['bxor']}(r[{field['a0']}],{_num(arg_mask)});local a1={n['bxor']}(r[{field['a1']}],{_num(arg_mask)});local mv={n['bxor']}(r[{field['meta']}],{_num(meta_mask)});local ck={n['bxor']}(r[{field['check']}],{_num(check_mask)});local pp=r[{field['payload']}];local p0=(pp and pp[1]) or 0;local pn=(pp and pp[#pp]) or 0;local vals={{op,np,ap,a0,a1,mv,ck,#(pp or {{}}),{n['bxor']}(p0,pn)}};for j=1,9 do {n['boot']}={n['bxor']}({n['boot']},vals[j]);{n['boot']}={n['bxor']}({n['boot']},{n['lshift']}({n['boot']},9));{n['boot']}={n['bxor']}({n['boot']},{n['rshift']}({n['boot']},13));{n['boot']}=({n['boot']}+{_num(0x9E3779B9)})%4294967296;end;end;if {n['boot']}~={n['record_seal']} then {n['tamper']}() end;"
    )
    runtime += boot_checks

    max_steps = len(records) * 8 + 64
    if sample_indices:
        sample_check = (
            f"if {n['sample_table']}[1] and {n['band']}({n['counter']},{max(1, 16 - min(8, int(anti_budget) or 1))})==0 then "
            f"local {n['sample_index']}={n['sample_table']}[1+({n['counter']}%#{n['sample_table']})];local {n['sample_record']}={n['records']}[{n['sample_index']}];"
            f"local sop={n['bxor']}({n['sample_record']}[{field['op']}],{_num(op_mask)});local sn={n['bxor']}({n['sample_record']}[{field['next']}],{_num(pc_mask)});local sa={n['bxor']}({n['sample_record']}[{field['alt']}],{_num(pc_mask)});"
            f"local s0={n['bxor']}({n['sample_record']}[{field['a0']}],{_num(arg_mask)});local s1={n['bxor']}({n['sample_record']}[{field['a1']}],{_num(arg_mask)});local sm={n['bxor']}({n['sample_record']}[{field['meta']}],{_num(meta_mask)});"
            f"local sg={n['bxor']}({n['sample_record']}[{field['check']}],{_num(check_mask)});if sg~={n['guard']}(sop,sn,sa,s0,s1,sm) then return {n['tamper']}() end;end;"
        )
    else:
        sample_check = ""
    if rng.randrange(3) == 0:
        loop = (
            f"repeat local {n['record']}={n['records']}[{n['bxor']}({n['state']},{_num(pc_mask)})];if not {n['record']} then {n['tamper']}() end;{sample_check}"
            f"local nxt={n['steps']}({n['record']});if nxt~=0 and not {n['records']}[{n['bxor']}(nxt,{_num(pc_mask)})] then {n['tamper']}() end;"
            f"{n['shadow']}={n['bxor']}({n['shadow']},{_num(rng.randrange(1, MASK32))});{n['state']}=nxt;{n['counter']}={n['counter']}+1;"
            f"until {n['state']}==0 or {n['counter']}>{max_steps};"
        )
    elif rng.randrange(2) == 0:
        loop = (
            f"while {n['state']}~=0 do local {n['record']}={n['records']}[{n['bxor']}({n['state']},{_num(pc_mask)})];if not {n['record']} then {n['tamper']}() end;{sample_check}"
            f"local nxt={n['steps']}({n['record']});if nxt~=0 and not {n['records']}[{n['bxor']}(nxt,{_num(pc_mask)})] then {n['tamper']}() end;{n['state']}=nxt;{n['counter']}={n['counter']}+1;"
            f"if {n['band']}({n['counter']},31)==0 then {n['shadow']}={n['bxor']}({n['shadow']},{_num(shadow_salt)}) end;"
            f"if {n['counter']}>{max_steps} then {n['tamper']}() end;end;"
        )
    else:
        loop = (
            f"local {n['entry']}={n['state']};while {n['entry']}~=0 do local {n['record']}={n['records']}[{n['bxor']}({n['entry']},{_num(pc_mask)})];if not {n['record']} then {n['tamper']}() end;{sample_check}"
            f"local nxt={n['steps']}({n['record']});if nxt~=0 and not {n['records']}[{n['bxor']}(nxt,{_num(pc_mask)})] then {n['tamper']}() end;{n['counter']}={n['counter']}+1;"
            f"if {n['bxor']}({n['counter']},{n['counter']})==0 then {n['shadow']}={n['shadow']}+0 end;"
            f"{n['entry']}=nxt;{n['state']}={n['bxor']}(nxt,{_num(pc_mask)});"
            f"if {n['counter']}>{max_steps} then {n['tamper']}() end;end;"
        )

    # A final opaque no-op statement changes the surface grammar without
    # affecting the user's program.
    tail = (
        f"local {n['fail']}={n['bxor']}({n['shadow']},{n['shadow']});"
        f"if {n['fail']}~=0 then {n['shadow']}={n['fail']} end;"
    )

    recovery_fn = (
        f"local function {n['recovery']}()"
        f"local stream={n['bxor']}({_num(recovery_key)},{_num(recovery_seed)});local chars={{}};"
        f"for i=1,#{n['recovery_payload']} do "
        f"stream={n['bxor']}(stream,((i*{_num(0x9E3779B9)})%4294967296),{_num(recovery_salt)});"
        f"stream={n['bxor']}(stream,{n['lshift']}(stream,17));stream={n['bxor']}(stream,{n['rshift']}(stream,7));stream={n['bxor']}(stream,{n['lshift']}(stream,11));"
        f"local v={n['recovery_payload']}[i];v={n['bxor']}(v,(((i-1)*{_num(recovery_step)}+{_num(recovery_salt)})%256));"
        f"chars[i]={n['char']}({n['bxor']}(v,{n['band']}(stream,255))) end;"
        f"local fn,err={n['loader']}({n['concat']}(chars));if not fn then error(err)end;"
        f"{n['executed']}=true;{n['returns']}={n['pack']}(fn());end;"
    )
    runtime += recovery_fn
    exit_values = f"if not {n['executed']} then {n['recovery']}() end;if {n['returns']}~=nil then return {n['unpack']}({n['returns']},1,{n['returns']}.n) end;"
    output = runtime + loop + exit_values + tail

    # Measure what was actually emitted rather than reporting configuration
    # targets. These values therefore change naturally with both the source
    # profile and the per-build randomized budget selection.
    branch_nodes = sum(1 for row in records if int(row["op"]) == virtual_ops["branch"])
    opaque_edge_count = sum(2 for row in records if int(row["op"]) == virtual_ops["branch"] and int(row["alt"]) != 0)
    op_cost = {
        virtual_ops["init"]: 3,
        virtual_ops["decode"]: 12,
        virtual_ops["mix"]: 4,
        virtual_ops["branch"]: 6,
        virtual_ops["verify"]: 14,
        virtual_ops["exec"]: 7,
        virtual_ops["halt"]: 1,
    }
    micro_op_count = sum(op_cost.get(int(row["op"]), 1) for row in records)
    micro_op_count += sum(6 + (idx % 5) for idx in range(1, len(dead_opcodes) + 1))
    micro_op_count += sample_count * 4

    runtime_layer_count = 0
    runtime_layer_count += 1  # alias/bootstrap layer
    runtime_layer_count += 1  # records/schema layer
    runtime_layer_count += len(decoder_functions)
    runtime_layer_count += 1  # handler layer
    if dead_opcodes:
        runtime_layer_count += 1
    if branch_nodes:
        runtime_layer_count += 1
    runtime_layer_count += 1  # dispatcher
    runtime_layer_count += 1  # integrity/anti-tamper layer
    if selected_payload_layers >= 2:
        runtime_layer_count += 1
    runtime_layer_count += 1  # native execution layer
    runtime_layer_count += 1  # independent recovery execution layer

    emitted_tamper_sites = 0
    emitted_tamper_sites += 1  # schema guard
    emitted_tamper_sites += 1  # schema field validation
    emitted_tamper_sites += 1  # schema uniqueness
    emitted_tamper_sites += 1  # active opcode validation
    emitted_tamper_sites += 1  # record existence validation
    emitted_tamper_sites += 1  # record seal
    emitted_tamper_sites += sample_count
    emitted_tamper_sites += 1  # per-record guard path
    emitted_tamper_sites += len(chunks)  # per-payload hash checks
    emitted_tamper_sites += 3  # output count/length/final hash
    emitted_tamper_sites += 1  # step limit

    return VMArtifact(
        source=output,
        key=root_key,
        instruction_count=len(records),
        runtime_layers=runtime_layer_count,
        decoder_variants=len(decoder_functions),
        opaque_edges=opaque_edge_count,
        payload_blocks=len(chunks),
        micro_ops=micro_op_count,
        control_flow_decoys=branch_nodes,
        dead_code_blocks=len(dead_opcodes),
        anti_tamper_checks=emitted_tamper_sites,
        payload_layers=selected_payload_layers,
        complexity_score=budget.score,
    )

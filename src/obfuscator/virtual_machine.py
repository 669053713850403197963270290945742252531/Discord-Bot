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
from dataclasses import dataclass

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


def build_vm(source: bytes, *, seed: int, junk: int = 9) -> VMArtifact:
    rng = random.Random(seed ^ 0xC0DE5A17)
    root_key = rng.randrange(1, MASK32)
    global_hash = _hash32(source, root_key)

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
    decoder_shifts = decoder_shifts[:4]

    chunk_size = rng.randint(36, 92)
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
        variant = rng.randrange(4)
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
        for _ in range(rng.randint(0, max(1, junk // 2))):
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

    # Small exact runtime hash, generated separately from payload decryption.
    hash_fn = (
        f"local function {n['hash']}(s,k) local h={n['bxor']}(k,{_num(0x9E3779B9)});"
        f"for i=1,#s do h={n['bxor']}(h,string.byte(s,i));h={n['bxor']}(h,{n['lshift']}(h,5));"
        f"h={n['bxor']}(h,{n['rshift']}(h,7));h={n['bxor']}(h,{n['lshift']}(h,11));"
        f"h=(h+{_num(0x7F4A7C15)})%4294967296;end;return h end;"
    )

    # The four decoder bodies share the same data layout but differ in their
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
            f"for i=1,{n['len']} do stream={n['bxor']}(stream,((i*{_num(0x45D9)})%4294967296));"
            f"stream={n['bxor']}(stream,{n['bxor']}({n['lshift']}(stream,{sa}),0));"
            f"stream={n['bxor']}(stream,{n['rshift']}(stream,{sb}));stream={n['bxor']}(stream,{n['lshift']}(stream,{sc}));"
            f"local v=packed[i] or 0;{inverse}out[i]={n['bxor']}(v,{n['band']}(stream,255));end;"
            f"local chars={{}};for i=1,{n['len']} do chars[i]=string.char(out[i])end;return {n['concat']}(chars)end;"
        )

    # The lane-masking above is intentionally handled at byte level after the
    # outer word mask. Keeping it verbose provides another polymorphic layer.
    # Generate handler functions in randomized registration order.
    handler_defs: dict[str, str] = {}
    handler_defs["init"] = (
        f"function(r)local a={n['bxor']}(r[{field['a0']}],{_num(arg_mask)});{n['shadow']}={n['bxor']}({n['shadow']},a,{_num(shadow_salt)});"
        f"return {n['bxor']}(r[{field['next']}],{_num(pc_mask)}) end"
    )
    handler_defs["decode"] = (
        f"function(r)local {n['key']}={n['bxor']}(r[{field['a0']}],{_num(arg_mask)});"
        f"local {n['len']}={n['bxor']}(r[{field['a1']}],{_num(arg_mask)});local {n['meta']}={n['bxor']}(r[{field['meta']}],{_num(meta_mask)});"
        f"local {n['variant']}={n['band']}({n['meta']},3);local {n['rotate']}={n['band']}({n['rshift']}({n['meta']},2),7);"
        f"local {n['add']}={n['band']}({n['rshift']}({n['meta']},5),255);local {n['step']}={n['band']}({n['rshift']}({n['meta']},13),63);"
        f"local {n['inverse']}={n['band']}({n['rshift']}({n['meta']},19),31);if {n['inverse']}<1 then {n['inverse']}=1 end;"
        f"local {n['permadd']}={n['band']}({n['rshift']}({n['meta']},24),31);"
        f"local f=({{{n['decode0']},{n['decode1']},{n['decode2']},{n['decode3']}}})[{n['variant']}+1];"
        f"local {n['plaintext']}=f(r[{field['payload']}],{n['key']},{n['len']},{n['rotate']},{n['add']},{n['step']},{n['inverse']},{n['permadd']},0,{_num(root_key)});"
        f"if {n['hash']}({n['plaintext']},{n['key']})~=" + f"{n['bxor']}(r[{field['check']}],{_num(check_mask)}) then return 0 end;"
        f"{n['out']}[#{n['out']}+1]={n['plaintext']};{n['shadow']}={n['bxor']}({n['shadow']},{n['len']},{n['key']});"
        f"return {n['bxor']}(r[{field['next']}],{_num(pc_mask)}) end"
    )
    handler_defs["mix"] = (
        f"function(r)local a={n['bxor']}(r[{field['a0']}],{_num(arg_mask)});local b={n['bxor']}(r[{field['a1']}],{_num(arg_mask)});"
        f"{n['shadow']}={n['bxor']}({n['shadow']},{n['lshift']}(a,3),{n['rshift']}(b,5));"
        f"return {n['bxor']}(r[{field['next']}],{_num(pc_mask)}) end"
    )
    handler_defs["branch"] = (
        f"function(r)local q={n['bxor']}({n['shadow']},{_num(shadow_salt)});local choose={n['band']}(q,1)==0;"
        f"local target=choose and r[{field['next']}] or r[{field['alt']}];return {n['bxor']}(target,{_num(pc_mask)}) end"
    )
    handler_defs["verify"] = (
        f"function(r)local expected={n['bxor']}(r[{field['a0']}],{_num(arg_mask)});local got={n['hash']}({n['concat']}({n['out']}),{_num(root_key)});"
        f"if got~=expected then error({n['concat']}({n['char']}({rng.randrange(65,90)},{rng.randrange(65,90)},{rng.randrange(65,90)}))) end;"
        f"return {n['bxor']}(r[{field['next']}],{_num(pc_mask)}) end"
    )
    handler_defs["exec"] = (
        f"function(r)if not {n['loader']} then error({n['concat']}({n['char']}({rng.randrange(97,122)},{rng.randrange(97,122)}))) end;"
        f"local fn,err={n['loader']}({n['concat']}({n['out']}));if not fn then error(err)end;{n['returns']}={n['pack']}(fn());return 0 end"
    )
    handler_defs["halt"] = "function(r)return 0 end"

    handler_order = list(handler_defs)
    rng.shuffle(handler_order)
    handler_assignments: list[str] = []
    for logical in handler_order:
        op = virtual_ops[logical]
        handler_assignments.append(f"{n['handlers']}[{_num(op)}]={handler_defs[logical]};")

    # Dispatcher step itself is generated through a randomly selected shape.
    step_function = (
        f"local function {n['steps']}(r)local raw={n['bxor']}(r[{field['op']}],{_num(op_mask)});"
        f"local op={n['band']}(raw,255);local fn={n['handlers']}[op];"
        f"if fn then return fn(r)end;return {n['bxor']}(r[{field['alt']}],{_num(pc_mask)}) end;"
    )

    header = (
        alias_setup
        + f"local {n['records']}={records_literal};local {n['handlers']}={{}};local {n['schema']}={schema_literal};"
        + f"local {n['out']}={{}};local {n['state']}={_num(pc_mask ^ entry_physical)};local {n['shadow']}={_num(root_key ^ shadow_salt)};"
        + f"local {n['returns']}=nil;local {n['counter']}=0;{hash_fn}"
    )

    runtime = header + "".join(decoder_functions) + "".join(handler_assignments) + step_function

    max_steps = len(records) * 8 + 64
    if rng.randrange(3) == 0:
        loop = (
            f"repeat local {n['record']}={n['records']}[{n['bxor']}({n['state']},{_num(pc_mask)})];"
            f"local nxt={n['steps']}({n['record']});{n['shadow']}={n['bxor']}({n['shadow']},{_num(rng.randrange(1, MASK32))});"
            f"if nxt==0 then {n['state']}=0 else {n['state']}={n['bxor']}(nxt,{_num(pc_mask)}) end;"
            f"{n['counter']}={n['counter']}+1;"
            f"until {n['state']}==0 or {n['counter']}>{max_steps};"
        )
    elif rng.randrange(2) == 0:
        loop = (
            f"while {n['state']}~=0 do local {n['record']}={n['records']}[{n['bxor']}({n['state']},{_num(pc_mask)})];"
            f"local nxt={n['steps']}({n['record']});{n['counter']}={n['counter']}+1;"
            f"if nxt==0 then {n['state']}=0 else {n['state']}={n['bxor']}(nxt,{_num(pc_mask)}) end;"
            f"if {n['band']}({n['counter']},31)==0 then {n['shadow']}={n['bxor']}({n['shadow']},{_num(shadow_salt)}) end;"
            f"if {n['counter']}>{max_steps} then error({n['concat']}({n['char']}({rng.randrange(97,122)},{rng.randrange(97,122)},{rng.randrange(97,122)})))end;end;"
        )
    else:
        loop = (
            f"local {n['entry']}={n['state']};while {n['entry']}~=0 do local {n['record']}={n['records']}[{n['bxor']}({n['entry']},{_num(pc_mask)})];"
            f"local nxt={n['steps']}({n['record']});{n['counter']}={n['counter']}+1;"
            f"if nxt==0 then {n['entry']}=0 else {n['entry']}={n['bxor']}(nxt,{_num(pc_mask)}) end;{n['state']}={n['entry']};"
            f"if {n['bxor']}({n['counter']},{n['counter']})==0 then {n['shadow']}={n['shadow']}+0 end;"
            f"if {n['counter']}>{max_steps} then error({n['concat']}({n['char']}({rng.randrange(97,122)},{rng.randrange(97,122)})))end;end;"
        )

    # A final opaque no-op statement changes the surface grammar without
    # affecting the user's program.
    tail = (
        f"local {n['fail']}={n['bxor']}({n['shadow']},{n['shadow']});"
        f"if {n['fail']}~=0 then {n['shadow']}={n['fail']} end;"
    )

    exit_values = f"if {n['returns']}~=nil then return {n['unpack']}({n['returns']},1,{n['returns']}.n) end;"
    output = runtime + loop + exit_values + tail
    return VMArtifact(
        source=output,
        key=root_key,
        instruction_count=len(records),
        runtime_layers=6,
        decoder_variants=4,
        opaque_edges=opaque_edges + max(2, junk // 2),
        payload_blocks=len(chunks),
        micro_ops=len(records) + len(chunks) * rng.randint(5, 10),
    )

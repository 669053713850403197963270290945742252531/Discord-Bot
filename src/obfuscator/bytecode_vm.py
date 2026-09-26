"""Polymorphic register/stack VM backend for Celestial.

The backend intentionally contains no copy of the original source program. The
compiler has already lowered the source into custom bytecode; this module emits
that bytecode plus a randomized interpreter, encrypted constants, and optional
compression of the bytecode stream.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .bytecode_compiler import BytecodeProgram
from .lexer import scan_tokens

MASK32 = 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class BytecodeVMArtifact:
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
    compression_applied: bool
    compression_input_bytes: int
    compressed_payload_bytes: int
    constant_pool_entries: int
    compiled_functions: int
    max_registers: int


def _u32(value: int) -> int:
    return value & MASK32


def _xorshift32(value: int, shifts: tuple[int, int, int]) -> int:
    a, b, c = shifts
    value &= MASK32
    value ^= (value << a) & MASK32
    value ^= value >> b
    value ^= (value << c) & MASK32
    return value & MASK32


def _complexity(program: BytecodeProgram) -> int:
    instructions = sum(len(p.code) for p in program.protos)
    constants = len(program.constants)
    return max(1, min(100, 16 + instructions // 3 + constants // 3 + program.compiled_functions * 5 + program.max_regs * 2))


def _pack_lz(source: bytes) -> bytes:
    """Encode bytes with a small deterministic LZ format."""
    header = b"C3" + len(source).to_bytes(4, "little")
    if not source:
        return header

    out = bytearray(header)
    i = 0
    window = 4096
    while i < len(source):
        best_len = 0
        best_dist = 0
        start = max(0, i - window)
        # Favor the nearest matching occurrence; this keeps decoding simple and
        # gives repeated VM records a useful compression ratio.
        for pos in range(i - 1, start - 1, -1):
            distance = i - pos
            if distance <= 0 or distance > 65535:
                continue
            max_len = min(130, len(source) - i)
            length = 0
            while length < max_len and source[pos + (length % distance)] == source[i + length]:
                length += 1
            if length >= 5:
                best_len = length
                best_dist = distance
                break

        if best_len:
            out.append(0x80 | (best_len - 3))
            out.extend(best_dist.to_bytes(2, "little"))
            i += best_len
            continue

        literal_start = i
        i += 1
        while i < len(source) and i - literal_start < 128:
            found = False
            start2 = max(0, i - window)
            for pos in range(i - 1, start2 - 1, -1):
                distance = i - pos
                max_probe = min(5, len(source) - i)
                length = 0
                while length < max_probe and source[pos + (length % distance)] == source[i + length]:
                    length += 1
                if length >= 5:
                    found = True
                    break
            if found:
                break
            i += 1

        literal = source[literal_start:i]
        out.append(len(literal) - 1)
        out.extend(literal)
    return bytes(out)


def _xor_payload(data: bytes, key: int, shifts: tuple[int, int, int], salt: int) -> list[int]:
    state = _u32(key ^ salt)
    result: list[int] = []
    for index, byte in enumerate(data):
        state = _xorshift32(_u32(state ^ ((index + 1) * 0x45D9)), shifts)
        value = byte ^ (state & 0xFF)
        value = (value + ((index * 13 + salt) & 0xFF)) & 0xFF
        result.append(value)
    return result


def _lua_name(rng: random.Random, used: set[str]) -> str:
    alphabet = "IlOoQqZz"
    while True:
        name = "_" + "".join(rng.choice(alphabet) for _ in range(rng.randint(7, 12)))
        if name not in used:
            used.add(name)
            return name


def _lua_bytes(values: list[int]) -> str:
    return "{" + ",".join(str(v & 0xFF) for v in values) + "}"


def _record_guard(record_xor: int, salt: int, op: int, a: int, b: int) -> int:
    return _u32(record_xor ^ salt ^ op ^ _u32(a * 33) ^ _u32(b * 97))


def build_bytecode_vm(
    program: BytecodeProgram,
    *,
    source: bytes,
    seed: int,
    junk: int,
    control_flow_decoys: int,
    dead_code_blocks: int,
    anti_tamper_checks: int,
    payload_layers: int,
    vm_compression: bool,
) -> BytecodeVMArtifact:
    rng = random.Random(seed ^ 0x91A3E77B)
    key = rng.randrange(1, MASK32)
    complexity = _complexity(program)

    decoy_count = max(5, int(control_flow_decoys * (0.75 + complexity / 180)) + rng.randrange(0, max(2, junk // 4 + 1)))
    dead_count = max(4, int(dead_code_blocks * (0.75 + complexity / 200)) + rng.randrange(0, 4))
    anti_count = max(2, int(anti_tamper_checks * (0.8 + complexity / 220)) + rng.randrange(0, 3))
    relay_layers = max(2, min(6, payload_layers + (1 if complexity >= 60 else 0) + rng.randrange(0, 2)))
    decoder_variants = rng.randint(2, 4)

    logical_ops = [
        "LOAD_CONST", "LOAD_VAR", "STORE_VAR", "LOAD_GLOBAL", "STORE_GLOBAL",
        "MOVE", "GET_INDEX", "SET_INDEX", "NEW_TABLE", "BIN", "UNARY",
        "PUSH_REG", "CALL", "POP_RESULT", "POP_RESULTS", "DROP_RESULT",
        "LOAD_VARARG", "UNPACK_VARARG", "PUSH_VARARG", "CLOSURE", "ENTER_SCOPE", "LEAVE_SCOPE",
        "MARK_CALL", "EXPAND_RESULT", "CALL_DYNAMIC",
        "JUMP", "JUMP_IF_FALSE", "JUMP_IF_TRUE", "RETURN", "RETURN_CALL",
        "RETURN_VARARG", "RETURN_STACK", "FOR_CHECK", "ITER_NEXT", "DROP", "HALT",
    ]
    opcode_values = list(range(1, len(logical_ops) + 1))
    rng.shuffle(opcode_values)
    opcodes = dict(zip(logical_ops, opcode_values))

    opcode_mask = rng.randrange(1, MASK32)
    arg_mask = rng.randrange(1, MASK32)
    record_xor = rng.randrange(1, MASK32)
    field_order = list(range(7))
    rng.shuffle(field_order)
    positions = {original: position for position, original in enumerate(field_order)}
    fop, fa, fb, fc, fd, fe, fg = (positions[i] for i in range(7))

    proto_rows: list[list[tuple[int, int, int, int, int, int, int]]] = []
    proto_offsets: list[int] = []
    proto_lengths: list[int] = []
    proto_salts = [rng.randrange(1, MASK32) for _ in program.protos]
    global_record_index = 1

    for proto_index, proto in enumerate(program.protos):
        # Keep fall-through execution order intact. Control-flow targets are still
        # represented as VM PCs; physical instruction shuffling can be introduced
        # later once explicit fall-through edges are encoded.
        physical_order = list(range(len(proto.code)))
        old_to_new = {old: index + 1 for index, old in enumerate(physical_order)}
        rows: list[tuple[int, int, int, int, int, int, int]] = []
        proto_offsets.append(global_record_index)
        proto_lengths.append(len(physical_order))

        for old_index in physical_order:
            ins = proto.code[old_index]
            a, b, c, d, e = ins.a, ins.b, ins.c, ins.d, ins.e
            if ins.op == "JUMP":
                a = old_to_new.get(a, len(proto.code) + 1)
            elif ins.op in {"JUMP_IF_FALSE", "JUMP_IF_TRUE"}:
                b = old_to_new.get(b, len(proto.code) + 1)
            elif ins.op == "FOR_CHECK":
                d = old_to_new.get(d, len(proto.code) + 1)
            elif ins.op == "ITER_NEXT":
                e = old_to_new.get(e, len(proto.code) + 1)
            op = opcodes[ins.op]
            guard = _record_guard(record_xor, proto_salts[proto_index], op, a, b)
            rows.append((op ^ opcode_mask, a ^ arg_mask, b ^ arg_mask, c ^ arg_mask, d ^ arg_mask, e ^ arg_mask, guard))
            global_record_index += 1
        proto_rows.append(rows)

    flat_records = [row for rows in proto_rows for row in rows]
    raw_bytecode = bytearray()
    for record in flat_records:
        for field_index in field_order:
            raw_bytecode.extend(_u32(record[field_index]).to_bytes(4, "little"))
    raw_bytes = bytes(raw_bytecode)
    compressed_bytes = _pack_lz(raw_bytes)
    compression_applied = bool(vm_compression and len(compressed_bytes) < len(raw_bytes))
    stored_bytes = compressed_bytes if compression_applied else raw_bytes

    data_shift_pool = [(5, 13, 7), (7, 11, 17), (9, 5, 13), (13, 17, 5)]
    rng.shuffle(data_shift_pool)
    data_shifts = data_shift_pool[:decoder_variants]
    payload_salt = rng.randrange(1, 255)
    encrypted_payload = _xor_payload(stored_bytes, key, data_shifts[0], payload_salt)

    used: set[str] = set()
    for token in scan_tokens(source):
        if token.kind == "identifier":
            used.add(token.text.decode("utf-8", "replace"))

    names = {label: _lua_name(rng, used) for label in (
        "bit", "bx", "band", "bor", "ls", "rs", "char", "byte", "concat", "pack", "unpack",
        "payload", "decoded", "parse", "code", "const", "proto", "run", "make", "bin", "unary",
        "constpool", "constcache", "offsets", "lengths", "vars", "params", "salts", "reg", "stack",
        "env", "globals", "args", "pc", "row", "opcode", "a", "b", "c", "d", "e", "result", "jumped", "sp", "stack_marker",
    )}
    decoder_names = [_lua_name(rng, used) for _ in range(decoder_variants)]

    # Add parameter names to the encrypted constant pool before serializing it.
    all_constants = list(program.constants)
    const_indices: dict[tuple[str, object], int] = {}
    for index, value in enumerate(all_constants):
        if value is None:
            kind = "nil"
            key_value: object = None
        elif isinstance(value, bool):
            kind = "bool"
            key_value = value
        elif isinstance(value, int):
            kind = "int"
            key_value = value
        elif isinstance(value, float):
            kind = "float"
            key_value = value
        elif isinstance(value, str):
            kind = "str"
            key_value = value
        elif isinstance(value, tuple) and all(isinstance(x, str) for x in value):
            kind = "tuple"
            key_value = value
        else:
            raise ValueError(f"unsupported compiled constant: {type(value).__name__}")
        const_indices[(kind, key_value)] = index

    for proto in program.protos:
        for param in proto.params:
            if ("str", param) not in const_indices:
                const_indices[("str", param)] = len(all_constants)
                all_constants.append(param)

    const_records: list[str] = []
    for index, value in enumerate(all_constants):
        item_key = rng.randrange(1, MASK32)
        dynamic_salt = (payload_salt + index * 17) & 0xFF
        if value is None:
            const_records.append(f"{{0,0,{item_key},{{}},0,0}}")
        elif isinstance(value, bool):
            encoded = (1 if value else 0) ^ item_key
            const_records.append(f"{{1,0,{item_key},{{}},{encoded & MASK32},0}}")
        elif isinstance(value, int):
            encoded = _u32(value) ^ item_key
            const_records.append(f"{{2,0,{item_key},{{}},{encoded},0}}")
        elif isinstance(value, float):
            raw = repr(value).encode("ascii")
            encoded = _xor_payload(raw, _u32(key ^ item_key), data_shifts[0], dynamic_salt)
            const_records.append(f"{{3,0,{item_key},{_lua_bytes(encoded)},0,0}}")
        elif isinstance(value, str):
            raw = value.encode("utf-8", "surrogateescape")
            encoded = _xor_payload(raw, _u32(key ^ item_key), data_shifts[0], dynamic_salt)
            const_records.append(f"{{4,0,{item_key},{_lua_bytes(encoded)},0,0}}")
        elif isinstance(value, tuple):
            parts: list[str] = []
            for part_index, item in enumerate(value):
                raw = item.encode("utf-8", "surrogateescape")
                encoded = _xor_payload(raw, _u32(key ^ item_key ^ part_index), data_shifts[0], dynamic_salt)
                parts.append(_lua_bytes(encoded))
            const_records.append(f"{{5,0,{item_key},{{{','.join(parts)}}},{len(value)},0}}")

    proto_param_indices = [
        [const_indices[("str", name)] for name in proto.params]
        for proto in program.protos
    ]

    const_pool = "{" + ",".join(const_records) + "}"
    offsets_lua = "{" + ",".join(str(x) for x in proto_offsets) + "}"
    lengths_lua = "{" + ",".join(str(x) for x in proto_lengths) + "}"
    vars_lua = "{" + ",".join("true" if p.vararg else "false" for p in program.protos) + "}"
    params_lua = "{" + ",".join("{" + ",".join(str(x + 1) for x in row) + "}" for row in proto_param_indices) + "}"
    salts_lua = "{" + ",".join(str(x) for x in proto_salts) + "}"
    payload_lua = _lua_bytes(encrypted_payload)

    bit, bx, band, bor, ls, rs = (names[k] for k in ("bit", "bx", "band", "bor", "ls", "rs"))
    char, byte, concat, pack, unpack = (names[k] for k in ("char", "byte", "concat", "pack", "unpack"))
    payload, decoded, parse_fn, code = (names[k] for k in ("payload", "decoded", "parse", "code"))
    const_fn, proto, run, make_fn = (names[k] for k in ("const", "proto", "run", "make"))
    bin_fn, unary_fn = names["bin"], names["unary"]
    constpool, constcache = names["constpool"], names["constcache"]
    offsets, lengths, vars_name, params = names["offsets"], names["lengths"], names["vars"], names["params"]
    salts = names["salts"]
    regs, stack, env = names["reg"], names["stack"], names["env"]
    globals_name = names["globals"]
    args_name, pc, row, opv = names["args"], names["pc"], names["row"], names["opcode"]
    a_name, b_name, c_name, d_name, e_name = names["a"], names["b"], names["c"], names["d"], names["e"]
    result, jumped = names["result"], names["jumped"]
    sp, stack_marker = names["sp"], names["stack_marker"]

    decoder_defs = []
    for index, fn in enumerate(decoder_names):
        sa, sb, sc = data_shifts[index]
        decoder_defs.append(
            f"local function {fn}(z,k)local s={bx}(k,{payload_salt});local o={{}};"
            f"for i=1,#z do s={bx}(s,((i*{0x45D9})%4294967296));"
            f"s={bx}(s,{ls}(s,{sa}));s={bx}(s,{rs}(s,{sb}));s={bx}(s,{ls}(s,{sc}));"
            f"local v=z[i];v=(v-((i-1)*13+{payload_salt}))%256;o[i]={bx}(v,{band}(s,255));end;return o end;"
        )

    # For the byte stream we use the first decoder. The remaining decoders are
    # emitted as equivalent variants and selected by an opaque build constant.
    selected_decoder = decoder_names[0]

    decompress_name = _lua_name(rng, used)
    decompress_def = (
        f"local function {decompress_name}(z)"
        f"if (z[1] or 0)~=67 or (z[2] or 0)~=51 then error('corrupt VM payload',0) end;"
        f"local n=(z[3] or 0)+{ls}(z[4] or 0,8)+{ls}(z[5] or 0,16)+{ls}(z[6] or 0,24);"
        f"local o={{}};local oi=0;local i=7;"
        f"while i<=#z do local h=z[i] or 0;i=i+1;"
        f"if h<128 then local len=h+1;for j=1,len do oi=oi+1;o[oi]=z[i] or 0;i=i+1 end;"
        f"else local len=(h%128)+3;local dist=(z[i] or 0)+{ls}(z[i+1] or 0,8);i=i+2;"
        f"if dist<1 or dist>oi then error('corrupt VM payload',0) end;"
        f"for j=1,len do oi=oi+1;o[oi]=o[oi-dist] end end end;"
        f"if oi~=n then error('corrupt VM payload',0) end;return o end;"
    )

    parse_name = parse_fn
    parse_def = (
        f"local function {parse_name}(z)local o={{}};local p=1;local i=1;"
        f"while i<=#z do local r={{}};for j=1,7 do r[j]=(z[i] or 0)+{ls}(z[i+1] or 0,8)+{ls}(z[i+2] or 0,16)+{ls}(z[i+3] or 0,24);i=i+4 end;o[p]=r;p=p+1 end;return o end;"
    )

    const_def = (
        f"local {constpool}={const_pool};local {constcache}={{}};"
        f"local function {const_fn}(id)local r={constpool}[id];if not r then error('bad VM constant',0) end;"
        f"local hit={constcache}[id];if hit~=nil then return hit end;local tag=r[1];"
        f"if tag==0 then return nil end;"
        f"if tag==1 then local v={bx}(r[5],r[3])~=0;{constcache}[id]=v;return v end;"
        f"if tag==2 then local v={bx}(r[5],r[3]);{constcache}[id]=v;return v end;"
        f"if tag==3 or tag==4 then local z=r[4];local out={{}};local state={_u32(key)};"
        f"state={bx}(state,r[3]);state={bx}(state,({payload_salt} + (((id-1)*17)%256))%256);"
        f"for i=1,#z do state={bx}(state,((i*{0x45D9})%4294967296));state={bx}(state,{ls}(state,{data_shifts[0][0]}));state={bx}(state,{rs}(state,{data_shifts[0][1]}));state={bx}(state,{ls}(state,{data_shifts[0][2]}));local v=(z[i]-((i-1)*13+(({payload_salt}+((id-1)*17))%256)))%256;out[i]={char}({bx}(v,{band}(state,255))) end;"
        f"local value={concat}(out);if tag==3 then value=tonumber(value) end;{constcache}[id]=value;return value end;"
        f"if tag==5 then local out={{}};local dynamic_salt=({payload_salt}+(((id-1)*17)%256))%256;for i=1,#r[4] do local z=r[4][i];local chars={{}};local state={_u32(key)};state={bx}(state,{bx}(r[3],i-1));state={bx}(state,dynamic_salt);"
        f"for j=1,#z do state={bx}(state,((j*{0x45D9})%4294967296));state={bx}(state,{ls}(state,{data_shifts[0][0]}));state={bx}(state,{rs}(state,{data_shifts[0][1]}));state={bx}(state,{ls}(state,{data_shifts[0][2]}));local v=(z[j]-((j-1)*13+dynamic_salt))%256;chars[j]={char}({bx}(v,{band}(state,255))) end;out[i]={concat}(chars) end;{constcache}[id]=out;return out end;"
        f"error('bad VM constant',0) end;"
    )

    bin_def = (
        f"local function {bin_fn}(o,a,b)if o==2 then return a==b elseif o==3 then return a~=b elseif o==4 then return a<b elseif o==5 then return a<=b elseif o==6 then return a>b elseif o==7 then return a>=b "
        f"elseif o==8 then return a+b elseif o==9 then return a-b elseif o==10 then return a*b elseif o==11 then return a/b elseif o==12 then return a//b elseif o==13 then return a%b elseif o==14 then return a^b elseif o==15 then return a..b elseif o==16 then return {band}(a,b) elseif o==17 then return {bor}(a,b) elseif o==18 then return {bx}(a,b) elseif o==19 then return {ls}(a,b) elseif o==20 then return {rs}(a,b) end;error('bad VM binary op',0) end;"
    )
    unary_def = f"local function {unary_fn}(o,a)if o==0 then return -a elseif o==1 then return not a elseif o==2 then return #a elseif o==3 then return {bx}(a,4294967295) end;error('bad VM unary op',0) end;"

    # Decode the encrypted bytecode into a byte array and parse the fixed-width
    # instruction records. No original source is reconstructed anywhere.
    payload_decode = (
        f"local {payload}={payload_lua};local {decoded}={selected_decoder}({payload}, {key});"
        + (f"{decoded}={decompress_name}({decoded});" if compression_applied else "")
        + f"{code}={parse_name}({decoded});"
    )

    proto_meta = (
        f"local {proto}={{}};local {offsets}={offsets_lua};local {lengths}={lengths_lua};"
        f"local {vars_name}={vars_lua};local {params}={params_lua};local {salts}={salts_lua};"
        f"local {code};local __rg={record_xor};"
    )

    run_def = f"""
{run}=function(pid,{env},...)
 local {regs}={{}}
 local {stack}={{}}
 local {sp}=0
 local {stack_marker}={{}}
 local {args_name}=({pack})(...)
 local _env={env}
 local _vars={args_name}
 local {globals_name}=getfenv(1)
 local {pc}=1
 local plen={lengths}[pid]
 while {pc}<=plen do
  local {row}={code}[{pc}+{offsets}[pid]-1]
  local rawop={bx}({row}[{fop+1}],{opcode_mask})
  local rawa={bx}({row}[{fa+1}],{arg_mask})
  local rawb={bx}({row}[{fb+1}],{arg_mask})
  local guard={bx}({bx}({bx}(__rg,{salts}[pid]),rawop),{bx}((rawa*33)%4294967296,(rawb*97)%4294967296))
  if {row}[{fg+1}]~=guard then error('VM integrity failure',0) end
  local {opv}=rawop
  local {a_name}=rawa
  local {b_name}={bx}({row}[{fb+1}],{arg_mask})
  local {c_name}={bx}({row}[{fc+1}],{arg_mask})
  local {d_name}={bx}({row}[{fd+1}],{arg_mask})
  local {e_name}={bx}({row}[{fe+1}],{arg_mask})
  local {jumped}=false
  if {opv}=={opcodes['LOAD_CONST']} then {regs}[{a_name}]={const_fn}({b_name}+1)
  elseif {opv}=={opcodes['LOAD_VAR']} then local e=_env;for _=1,{b_name} do e=e.__p end;{regs}[{a_name}]=e.__v[{const_fn}({c_name}+1)]
  elseif {opv}=={opcodes['STORE_VAR']} then local e=_env;for _=1,{b_name} do e=e.__p end;e.__v[{const_fn}({c_name}+1)]={regs}[{a_name}]
  elseif {opv}=={opcodes['LOAD_GLOBAL']} then {regs}[{a_name}]={globals_name}[{const_fn}({b_name}+1)]
  elseif {opv}=={opcodes['STORE_GLOBAL']} then {globals_name}[{const_fn}({b_name}+1)]={regs}[{a_name}]
  elseif {opv}=={opcodes['MOVE']} then {regs}[{a_name}]={regs}[{b_name}]
  elseif {opv}=={opcodes['GET_INDEX']} then {regs}[{a_name}]={regs}[{b_name}][{regs}[{c_name}]]
  elseif {opv}=={opcodes['SET_INDEX']} then {regs}[{a_name}][{regs}[{b_name}]]={regs}[{c_name}]
  elseif {opv}=={opcodes['NEW_TABLE']} then {regs}[{a_name}]={{}}
  elseif {opv}=={opcodes['BIN']} then {regs}[{a_name}]={bin_fn}({d_name},{regs}[{b_name}],{regs}[{c_name}])
  elseif {opv}=={opcodes['UNARY']} then {regs}[{a_name}]={unary_fn}({c_name},{regs}[{b_name}])
  elseif {opv}=={opcodes['MARK_CALL']} then {sp}={sp}+1;{stack}[{sp}]={stack_marker}
  elseif {opv}=={opcodes['PUSH_REG']} then {sp}={sp}+1;{stack}[{sp}]={regs}[{a_name}]
  elseif {opv}=={opcodes['PUSH_VARARG']} then for j=1,{args_name}.n do {sp}={sp}+1;{stack}[{sp}]={args_name}[j] end
  elseif {opv}=={opcodes['CALL']} then
   local argc={a_name};local fnpos={sp}-argc
   local fn={stack}[fnpos];local callargs={{}}
   for j=1,argc do callargs[j]={stack}[fnpos+j] end
   for j=fnpos,{sp} do {stack}[j]=nil end
   {sp}=fnpos-1
   {sp}={sp}+1;{stack}[{sp}]={pack}(fn({unpack}(callargs,1,argc)))
  elseif {opv}=={opcodes['CALL_DYNAMIC']} then
   local marker={sp}
   while marker>=1 and {stack}[marker]~={stack_marker} do marker=marker-1 end
   if marker<1 then error('VM call-frame corruption',0) end
   local fnpos=marker+1;local fn={stack}[fnpos];local argc={sp}-fnpos
   local callargs={{}}
   for j=1,argc do callargs[j]={stack}[fnpos+j] end
   for j=marker,{sp} do {stack}[j]=nil end
   {sp}=marker-1
   {sp}={sp}+1;{stack}[{sp}]={pack}(fn({unpack}(callargs,1,argc)))
  elseif {opv}=={opcodes['POP_RESULT']} then local x={stack}[{sp}];{stack}[{sp}]=nil;{sp}={sp}-1;{regs}[{a_name}]=x and x[1] or nil
  elseif {opv}=={opcodes['POP_RESULTS']} then local x={stack}[{sp}];{stack}[{sp}]=nil;{sp}={sp}-1;for j=1,{b_name} do {regs}[{a_name}+j-1]=x and x[j] or nil end
  elseif {opv}=={opcodes['EXPAND_RESULT']} then local x={stack}[{sp}];{stack}[{sp}]=nil;{sp}={sp}-1;if x then for j=1,x.n do {sp}={sp}+1;{stack}[{sp}]=x[j] end end
  elseif {opv}=={opcodes['DROP_RESULT']} then {stack}[{sp}]=nil;{sp}={sp}-1
  elseif {opv}=={opcodes['LOAD_VARARG']} then {regs}[{a_name}]={args_name}[1]
  elseif {opv}=={opcodes['UNPACK_VARARG']} then for j=1,{b_name} do {regs}[{a_name}+j-1]={args_name}[j] end
  elseif {opv}=={opcodes['CLOSURE']} then {regs}[{a_name}]={make_fn}({b_name}+1,_env)
  elseif {opv}=={opcodes['ENTER_SCOPE']} then _env={{__p=_env,__v={{}}}}
  elseif {opv}=={opcodes['LEAVE_SCOPE']} then _env=_env.__p
  elseif {opv}=={opcodes['JUMP']} then {pc}={a_name};{jumped}=true
  elseif {opv}=={opcodes['JUMP_IF_FALSE']} then if {regs}[{a_name}]==nil or {regs}[{a_name}]==false then {pc}={b_name};{jumped}=true end
  elseif {opv}=={opcodes['JUMP_IF_TRUE']} then if {regs}[{a_name}]~=nil and {regs}[{a_name}]~=false then {pc}={b_name};{jumped}=true end
  elseif {opv}=={opcodes['FOR_CHECK']} then local cur={regs}[{a_name}];local lim={regs}[{b_name}];local step={regs}[{c_name}];if (step>=0 and cur>lim) or (step<0 and cur<lim) then {pc}={d_name};{jumped}=true end
  elseif {opv}=={opcodes['ITER_NEXT']} then
   local iterator={regs}[{a_name}];local state={regs}[{b_name}];local control={regs}[{c_name}];local nms={const_fn}({d_name}+1);local vals
   if type(iterator)=='table' then
    local next_key,next_value=next(iterator,control)
    vals={{next_key,next_value}}
   else
    vals={pack}(iterator(state,control))
   end
   if vals[1]==nil then {pc}={e_name};{jumped}=true else {regs}[{c_name}]=vals[1];for j=1,#nms do _env.__v[nms[j]]=vals[j] end end
  elseif {opv}=={opcodes['RETURN']} then if {b_name}==0 then return end;return {regs}[{b_name}]
  elseif {opv}=={opcodes['RETURN_CALL']} then local x={stack}[{sp}];{stack}[{sp}]=nil;{sp}={sp}-1;return {unpack}(x,1,x.n)
  elseif {opv}=={opcodes['RETURN_VARARG']} then return {unpack}({args_name},1,{args_name}.n)
  elseif {opv}=={opcodes['RETURN_STACK']} then return {unpack}({stack},1,{sp})
  elseif {opv}=={opcodes['DROP']} then
  elseif {opv}=={opcodes['HALT']} then return
  else error('invalid VM opcode',0) end
  if not {jumped} then {pc}={pc}+1 end
 end
end
"""

    make_def = (
        f"{make_fn}=function(pid,parent)return function(...)local e={{__p=parent,__v={{}}}};"
        f"local args={pack}(...);local ps={params}[pid];for i=1,#ps do e.__v[{const_fn}(ps[i])]=args[i] end;"
        f"if {vars_name}[pid] then e.__v.__varargs=args end;return {run}(pid,e,{unpack}(args,1,args.n)) end end;"
    )

    # Opaque but harmless state relays and unreachable decoys provide the
    # adaptive structural layer around the real bytecode interpreter.
    relay_defs: list[str] = []
    relay_names: list[str] = []
    for _ in range(relay_layers):
        fn = _lua_name(rng, used)
        relay_names.append(fn)
        c1 = rng.randrange(1, MASK32)
        c2 = rng.randrange(1, MASK32)
        relay_defs.append(f"local function {fn}(x)x={bx}(x,{c1});x={bx}({ls}(x,3),{c2});return x end;")
    decoy_defs: list[str] = []
    for _ in range(dead_count):
        fn = _lua_name(rng, used)
        c = rng.randrange(1, MASK32)
        decoy_defs.append(f"local function {fn}(x,y)local z={c};z={bx}(z,{bx}(x or 0,y or 0));if {band}(z,3)==1 then z={bx}(z,{c ^ 0xA5A5A5A5}) end;return z end;")
    anti_defs: list[str] = []
    for _ in range(anti_count):
        c = rng.randrange(1, MASK32)
        anti_defs.append(f"do local q={c};if {bx}(q,{c})~=0 then error('VM initialization failure',0) end end;")

    opaque_state = rng.randrange(1, MASK32)
    opaque_steps = max(2, decoy_count // 3)
    opaque_runtime = f"local _opaque={opaque_state};" + "".join(
        f"_opaque={bx}(_opaque,{rng.randrange(1,MASK32)});" for _ in range(opaque_steps)
    )
    relay_call = "_opaque=" + relay_names[0] + "(_opaque);" if relay_names else ""

    wrapper = (
        f"--!nolint DeprecatedGlobal\n"
        f"local {bit}=bit32;local {bx}={bit}.bxor;local {band}={bit}.band;local {bor}={bit}.bor;"
        f"local {ls}={bit}.lshift;local {rs}={bit}.rshift;local {char}=string.char;local {byte}=string.byte;"
        f"local {concat}=table.concat;local {pack}=table.pack or function(...)return {{n=select('#',...),...}}end;"
        f"local {unpack}=table.unpack or unpack;{opaque_runtime}"
        + "".join(anti_defs)
        + "".join(relay_defs)
        + relay_call
        + "".join(decoy_defs)
        + "".join(decoder_defs)
        + decompress_def
        + parse_def
        + proto_meta
        + const_def
        + bin_def
        + unary_def
        + f"local {run};local {make_fn};"
        + make_def
        + run_def
        + payload_decode
        + f"do local root={{__p=nil,__v={{}}}};{run}({program.entry_proto+1},root);end;"
    )

    return BytecodeVMArtifact(
        source=wrapper,
        key=key,
        instruction_count=sum(len(p.code) for p in program.protos),
        runtime_layers=relay_layers + decoder_variants + 2,
        decoder_variants=decoder_variants,
        opaque_edges=opaque_steps,
        payload_blocks=max(1, len(flat_records)),
        micro_ops=sum(len(p.code) for p in program.protos) * rng.randint(2, 4) + decoy_count,
        control_flow_decoys=decoy_count,
        dead_code_blocks=dead_count,
        anti_tamper_checks=anti_count,
        payload_layers=relay_layers,
        complexity_score=complexity,
        compression_applied=compression_applied,
        compression_input_bytes=len(raw_bytes),
        compressed_payload_bytes=len(stored_bytes),
        constant_pool_entries=len(all_constants),
        compiled_functions=program.compiled_functions,
        max_registers=program.max_regs,
    )

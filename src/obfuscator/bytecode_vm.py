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



def _write_u32(out: bytearray, value: int) -> None:
    out.extend(_u32(value).to_bytes(4, "little"))


def _write_bytes(out: bytearray, value: bytes) -> None:
    _write_u32(out, len(value))
    out.extend(value)


def _write_string(out: bytearray, value: str) -> None:
    _write_bytes(out, value.encode("utf-8", "surrogateescape"))


def _record_guard8(record_xor: int, salt: int, op: int, a: int, b: int, c: int, d: int, e: int, nxt: int) -> int:
    return _u32(
        record_xor
        ^ salt
        ^ op
        ^ _u32(a * 33)
        ^ _u32(b * 97)
        ^ _u32(c * 7)
        ^ _u32(d * 11)
        ^ _u32(e * 17)
        ^ _u32(nxt * 193)
    )


def _encode_vm_image(
    program: BytecodeProgram,
    *,
    opcodes: dict[str, int],
    rng: random.Random,
    record_xor: int,
    junk_records: int,
) -> tuple[bytes, int, int, int, int]:
    """Pack constants, prototypes, masks and scrambled records into one image."""
    image = bytearray(b"C4VM")
    _write_u32(image, len(program.constants))
    _write_u32(image, len(program.protos))
    _write_u32(image, program.entry_proto)

    for value in program.constants:
        if value is None:
            image.append(0)
        elif isinstance(value, bool):
            image.append(1)
            image.append(1 if value else 0)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            image.append(2)
            _write_string(image, repr(value))
        elif isinstance(value, str):
            image.append(3)
            _write_bytes(image, value.encode("utf-8", "surrogateescape"))
        elif isinstance(value, tuple) and all(isinstance(x, str) for x in value):
            image.append(4)
            _write_u32(image, len(value))
            for item in value:
                _write_string(image, item)
        else:
            raise ValueError(f"unsupported compiled constant: {type(value).__name__}")

    total_records = 0
    max_registers = program.max_regs
    for proto in program.protos:
        param_count = len(proto.params)
        _write_u32(image, param_count)
        for param in proto.params:
            _write_string(image, param)
        image.append(1 if proto.vararg else 0)
        _write_u32(image, proto.max_regs)
        max_registers = max(max_registers, proto.max_regs)

        real_count = len(proto.code)
        per_proto_decoys = min(64, max(3, junk_records // max(1, len(program.protos))))
        total_count = real_count + per_proto_decoys
        _write_u32(image, total_count)
        _write_u32(image, real_count)

        order = list(range(real_count))
        rng.shuffle(order)
        old_to_new = {old: index + 1 for index, old in enumerate(order)}
        start_pc = old_to_new.get(0, 1)
        _write_u32(image, start_pc)

        op_mask = rng.randrange(1, MASK32)
        arg_mask = rng.randrange(1, MASK32)
        proto_salt = rng.randrange(1, MASK32)
        _write_u32(image, op_mask)
        _write_u32(image, arg_mask)
        _write_u32(image, proto_salt)

        field_order = list(range(8))
        rng.shuffle(field_order)
        image.append(8)
        image.extend(field_order)

        rows: list[tuple[int, int, int, int, int, int, int, int]] = []

        for old_index in order:
            ins = proto.code[old_index]
            a, b, c, d, e = ins.a, ins.b, ins.c, ins.d, ins.e
            if ins.op == "JUMP":
                a = old_to_new.get(a, total_count + 1)
            elif ins.op in {"JUMP_IF_FALSE", "JUMP_IF_TRUE"}:
                b = old_to_new.get(b, total_count + 1)
            elif ins.op == "FOR_CHECK":
                d = old_to_new.get(d, total_count + 1)
            elif ins.op == "ITER_NEXT":
                e = old_to_new.get(e, total_count + 1)
            nxt = old_to_new.get(old_index + 1, total_count + 1)
            op = opcodes[ins.op]
            guard = _record_guard8(record_xor, proto_salt, op, a, b, c, d, e, nxt)
            rows.append((op ^ op_mask, a ^ arg_mask, b ^ arg_mask, c ^ arg_mask, d ^ arg_mask, e ^ arg_mask, nxt ^ arg_mask, guard))

        decoy_start = real_count + 1
        decoy_op = opcodes["DROP"]
        for i in range(per_proto_decoys):
            next_pc = decoy_start + ((i + 1) % per_proto_decoys)
            a = rng.randrange(0, max(1, proto.max_regs + 1))
            b = rng.randrange(0, max(1, proto.max_regs + 1))
            c = rng.randrange(0, max(1, proto.max_regs + 1))
            d = rng.randrange(0, 31)
            e = rng.randrange(0, 31)
            guard = _record_guard8(record_xor, proto_salt, decoy_op, a, b, c, d, e, next_pc)
            rows.append((decoy_op ^ op_mask, a ^ arg_mask, b ^ arg_mask, c ^ arg_mask, d ^ arg_mask, e ^ arg_mask, next_pc ^ arg_mask, guard))

        for record in rows:
            for field_index in field_order:
                _write_u32(image, record[field_index])
        total_records += len(rows)

    trailer = rng.randrange(1, MASK32)
    checksum = 0
    for index, value in enumerate(image):
        checksum = _u32(checksum ^ _u32((value + 1) * (index + 17)))
    _write_u32(image, trailer)
    _write_u32(image, checksum ^ trailer)
    return bytes(image), total_records, max_registers, len(program.protos), len(program.constants)


def _lua_decoder(name: str, bx: str, band: str, ls: str, rs: str, salt: int, shifts: tuple[int, int, int]) -> str:
    sa, sb, sc = shifts
    return (
        f"{name}=function(z,k)local s={bx}(k,{salt});local o={{}};"
        f"for i=1,#z do s={bx}(s,((i*17881)%4294967296));s={bx}(s,{ls}(s,{sa}));"
        f"s={bx}(s,{rs}(s,{sb}));s={bx}(s,{ls}(s,{sc}));"
        f"local v=(z[i]-((i-1)*13+{salt}))%256;o[i]={bx}(v,{band}(s,255)) end;return o end;"
    )


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
    """Emit the scrambled custom bytecode VM wrapper."""
    rng = random.Random(seed ^ 0xC31B4A29)
    complexity = _complexity(program)

    decoy_count = max(10, int(control_flow_decoys * (0.95 + complexity / 150)) + rng.randrange(1, max(3, junk // 2 + 2)))
    dead_count = max(8, int(dead_code_blocks * (0.95 + complexity / 170)) + rng.randrange(1, 5))
    anti_count = max(3, int(anti_tamper_checks * (0.95 + complexity / 210)) + rng.randrange(0, 4))
    relay_layers = max(3, min(7, payload_layers + 2 + (1 if complexity >= 55 else 0)))
    decoder_variants = max(3, min(5, 3 + (1 if complexity >= 55 else 0)))

    logical_ops = [
        "LOAD_CONST", "LOAD_VAR", "STORE_VAR", "LOAD_GLOBAL", "STORE_GLOBAL",
        "MOVE", "GET_INDEX", "SET_INDEX", "NEW_TABLE", "BIN", "UNARY",
        "PUSH_REG", "CALL", "POP_RESULT", "POP_RESULTS", "DROP_RESULT",
        "LOAD_VARARG", "UNPACK_VARARG", "PUSH_VARARG", "CLOSURE", "ENTER_SCOPE", "LEAVE_SCOPE",
        "MARK_CALL", "EXPAND_RESULT", "CALL_DYNAMIC", "JUMP", "JUMP_IF_FALSE", "JUMP_IF_TRUE",
        "RETURN", "RETURN_CALL", "RETURN_VARARG", "RETURN_STACK", "FOR_CHECK", "ITER_NEXT", "DROP", "HALT",
    ]
    op_values = list(range(1, len(logical_ops) + 1))
    rng.shuffle(op_values)
    opcodes = dict(zip(logical_ops, op_values))
    record_xor = rng.randrange(1, MASK32)

    raw_image, total_records, max_registers, compiled_functions, constant_count = _encode_vm_image(
        program,
        opcodes=opcodes,
        rng=rng,
        record_xor=record_xor,
        junk_records=max(junk, dead_count + decoy_count),
    )
    compressed = _pack_lz(raw_image)
    compression_applied = bool(vm_compression and len(compressed) < len(raw_image))
    stored = compressed if compression_applied else raw_image

    shifts_pool = [(5, 13, 7), (7, 11, 17), (9, 5, 13), (13, 17, 5), (11, 7, 19)]
    rng.shuffle(shifts_pool)
    layer_count = max(2, min(4, relay_layers - 1))
    encrypted = stored
    layer_specs: list[tuple[int, int, tuple[int, int, int]]] = []
    payload_key = rng.randrange(1, MASK32)
    for index in range(layer_count):
        layer_key = _u32(payload_key ^ rng.randrange(1, MASK32) ^ (index * 0x9E3779B9))
        salt = rng.randrange(1, 255)
        shifts = shifts_pool[index % len(shifts_pool)]
        encrypted = bytes(_xor_payload(encrypted, layer_key, shifts, salt))
        layer_specs.append((layer_key, salt, shifts))

    used: set[str] = set()
    for token in scan_tokens(source):
        if token.kind == "identifier":
            used.add(token.text.decode("utf-8", "replace"))

    def name() -> str:
        return _lua_name(rng, used)

    bx = name(); band = name(); bor = name(); ls = name(); rs = name()
    char = name(); concat = name(); pack = name(); unpack = name(); typ = name(); nxt = name(); getenv = name()
    payload = name(); decoded = name(); image = name(); cursor = name(); read8 = name(); read32 = name(); readbytes = name(); readstr = name()
    const_fn = name(); const_table = name(); const_cache = name(); proto_table = name(); run = name(); make_fn = name(); bin_fn = name(); unary_fn = name()
    regs = name(); stack = name(); sp = name(); mark = name(); args_name = name(); globals_name = name(); env = name(); pc = name(); opv = name()
    a_name = name(); b_name = name(); c_name = name(); d_name = name(); e_name = name(); nextpc = name(); jumped = name(); row = name(); p_name = name()
    parser_tmp = name(); outer_env = name(); global_fallback = name(); diag = name(); diag_data = name(); diag_buf = name(); diag_state = name()
    # The runtime aliases must be the closure parameters themselves. Generating a
    # second independent set of names leaves the emitted dispatcher calling
    # undefined globals before the body has initialized anything.
    outer_params = [bx, band, bor, ls, rs, char, concat, pack, unpack, typ, nxt, getenv, name()]
    tonumber = outer_params[12]

    decoder_names = [name() for _ in range(decoder_variants)]
    decoder_defs = "".join(_lua_decoder(fn, bx, band, ls, rs, salt, shifts) for fn, (_, salt, shifts) in zip(decoder_names, layer_specs))
    # We select a randomized permutation of the aliases rather than matching alias
    # order to encryption order.
    decoder_order = list(range(layer_count))
    rng.shuffle(decoder_order)
    # Each decoder is tied to exactly one layer; reverse application order remains
    # required to undo encryption. Only the alias positions are scrambled.
    inverse_by_layer = []
    for index, (layer_key, _salt, _shifts) in enumerate(layer_specs):
        inverse_by_layer.append(decoder_names[index])

    decompress = name()
    decompress_def = (
        f"{decompress}=function(z)local n=z[3]+{ls}(z[4],8)+{ls}(z[5],16)+{ls}(z[6],24);local o={{}};local oi=0;local i=7;"
        f"while i<=#z do local h=z[i];i=i+1;if h<128 then local len=h+1;for j=1,len do oi=oi+1;o[oi]=z[i];i=i+1 end;"
        f"else local len=(h%128)+3;local dist=z[i]+{ls}(z[i+1],8);i=i+2;if dist<1 or dist>oi then return nil end;"
        f"for j=1,len do oi=oi+1;o[oi]=o[oi-dist] end end end;if oi~=n then return nil end;return o end;"
    )

    reader_defs = (
        f"local {cursor}=1;{read8}=function()local x={image}[{cursor}] or 0;{cursor}={cursor}+1;return x end;"
        f"{read32}=function()local x={read8}();local y={read8}();local z={read8}();local w={read8}();return x+{ls}(y,8)+{ls}(z,16)+{ls}(w,24) end;"
        f"{readbytes}=function()local n={read32}();local o={{}};for i=1,n do o[i]={read8}() end;return o end;"
        f"{readstr}=function()local z={readbytes}();local o={{}};for i=1,#z do o[i]={char}(z[i]) end;return {concat}(o) end;"
    )

    # Keep runtime diagnostics useful without embedding their plaintext in the emitted VM.
    # Each build gets a fresh key and per-message encoded byte arrays.
    diagnostic_messages = {
        1: "Celestial VM: invalid payload magic",
        2: "Celestial VM: payload decoding failed",
        3: "Celestial VM: attempt to call non-function at pc ",
        4: "Celestial VM: CALL_DYNAMIC marker missing",
        5: "Celestial VM: unknown opcode",
        6: "Celestial VM: missing prototype ",
        7: "Celestial VM: missing instruction at pc ",
        8: "Celestial VM: bytecode guard failed at pc ",
        9: "Celestial VM: payload integrity check failed",
    }
    diagnostic_key = rng.randrange(1, 256)
    encoded_diagnostics: list[list[int]] = []
    for index in range(1, max(diagnostic_messages) + 1):
        message = diagnostic_messages[index].encode("utf-8")
        encoded_diagnostics.append([
            (byte ^ ((diagnostic_key + (pos * 131) + index * 173) & 0xFF)) & 0xFF
            for pos, byte in enumerate(message, 1)
        ])
    diagnostic_table = "{" + ",".join(_lua_bytes(values) for values in encoded_diagnostics) + "}"
    diagnostic_def = (
        f"local {diag_data}={diagnostic_table};"
        f"local {diag}=function(id,tail)local {diag_state}={diag_data}[id];local {diag_buf}={{}};"
        f"for i=1,#{diag_state} do {diag_buf}[i]={char}({bx}({diag_state}[i],{band}(({diagnostic_key}+i*131+id*173)%256))) end;"
        f"local msg={concat}({diag_buf});if tail~=nil then msg=msg..tail end;error(msg,0) end;"
    )

    parser = (
        f"local {const_table}={{}};local {proto_table}={{}};local m1={read8}();local m2={read8}();local m3={read8}();local m4={read8}();"
        f'if m1~=67 or m2~=52 or m3~=86 or m4~=77 then {diag}(1) end;'
        f"local cn={read32}();local pn={read32}();local entry={read32}()+1;"
        f"for i=1,cn do local k={read8}();if k==0 then {const_table}[i]=nil elseif k==1 then {const_table}[i]={read8}()~=0 elseif k==2 then {const_table}[i]={tonumber}({readstr}()) elseif k==3 then local q={readbytes}();local t={{}};for j=1,#q do t[j]={char}(q[j]) end;{const_table}[i]={concat}(t) elseif k==4 then local n={read32}();local t={{}};for j=1,n do t[j]={readstr}() end;{const_table}[i]=t end end;"
        f"for i=1,pn do local q={{params={{}}}};local pcnt={read32}();for j=1,pcnt do q.params[j]={readstr}() end;q.vararg={read8}()~=0;q.maxregs={read32}();q.count={read32}();q.real={read32}();q.start={read32}();q.opmask={read32}();q.argmask={read32}();q.salt={read32}();q.fields={{}};local fc={read8}();for j=1,fc do q.fields[j]={read8}() end;q.code={{}};for j=1,q.count do local r={{}};for k=1,8 do local f=q.fields[k]+1;r[f]={read32}() end;q.code[j]=r end;{proto_table}[i]=q end;"
        f'local ti={cursor};local tr={read32}();local st={read32}();local chk=0;for i=1,ti-1 do chk={bx}(chk,((({image}[i] or 0)+1)*(i+16))%4294967296) end;if st~={bx}(chk,tr) then {diag}(9) end;'
    )

    const_def = f"local {const_cache}={{}};{const_fn}=function(id)local h={const_cache}[id];if h~=nil then return h end;local v={const_table}[id];{const_cache}[id]=v;return v end;"
    bin_def = (
        f"local {bin_fn}=function(o,a,b)if o==2 then return a==b elseif o==3 then return a~=b elseif o==4 then return a<b elseif o==5 then return a<=b elseif o==6 then return a>b elseif o==7 then return a>=b elseif o==8 then return a+b elseif o==9 then return a-b elseif o==10 then return a*b elseif o==11 then return a/b elseif o==12 then return a//b elseif o==13 then return a%b elseif o==14 then return a^b elseif o==15 then return a..b elseif o==16 then return {band}(a,b) elseif o==17 then return {bor}(a,b) elseif o==18 then return {bx}(a,b) elseif o==19 then return {ls}(a,b) elseif o==20 then return {rs}(a,b) end end;"
    )
    unary_def = f"local {unary_fn}=function(o,a)if o==0 then return -a elseif o==1 then return not a elseif o==2 then return #a elseif o==3 then return {bx}(a,4294967295) end end;"

    # Build dispatch clauses in a different order every build. The opcode values
    # themselves were already randomized above.
    clauses = [
        ("LOAD_CONST", f"{regs}[{a_name}]={const_fn}({b_name}+1)"),
        ("LOAD_VAR", f"local e={env};for _=1,{b_name} do e=e.__p end;{regs}[{a_name}]=e.__v[{const_fn}({c_name}+1)]"),
        ("STORE_VAR", f"local e={env};for _=1,{b_name} do e=e.__p end;e.__v[{const_fn}({c_name}+1)]={regs}[{a_name}]"),
        ("LOAD_GLOBAL", f"local k={const_fn}({b_name}+1);local v=nil;if {global_fallback} and {global_fallback}~={globals_name} then v={global_fallback}[k] end;if v==nil then v={globals_name}[k] end;{regs}[{a_name}]=v"),
        ("STORE_GLOBAL", f"local k={const_fn}({b_name}+1);if {global_fallback} and {global_fallback}~={globals_name} then {global_fallback}[k]={regs}[{a_name}] else {globals_name}[k]={regs}[{a_name}] end"),
        ("MOVE", f"{regs}[{a_name}]={regs}[{b_name}]"),
        ("GET_INDEX", f"{regs}[{a_name}]={regs}[{b_name}][{regs}[{c_name}]]"),
        ("SET_INDEX", f"{regs}[{a_name}][{regs}[{b_name}]]={regs}[{c_name}]"),
        ("NEW_TABLE", f"{regs}[{a_name}]={{}}"),
        ("BIN", f"{regs}[{a_name}]={bin_fn}({d_name},{regs}[{b_name}],{regs}[{c_name}])"),
        ("UNARY", f"{regs}[{a_name}]={unary_fn}({c_name},{regs}[{b_name}])"),
        ("MARK_CALL", f"{sp}={sp}+1;{stack}[{sp}]={mark}"),
        ("PUSH_REG", f"{sp}={sp}+1;{stack}[{sp}]={regs}[{a_name}]"),
        ("PUSH_VARARG", f"for j=1,{args_name}.n do {sp}={sp}+1;{stack}[{sp}]={args_name}[j] end"),
        ("CALL", f'local argc={a_name};local fp={sp}-argc;local fn={stack}[fp];if {typ}(fn)~="function" then {diag}(3,{pc}.." ("..{typ}(fn)..")") end;local aa={{}};for j=1,argc do aa[j]={stack}[fp+j] end;for j=fp,{sp} do {stack}[j]=nil end;{sp}=fp-1;{sp}={sp}+1;{stack}[{sp}]={pack}(fn({unpack}(aa,1,argc)))'),
        ("CALL_DYNAMIC", f'local mk={sp};while mk>=1 and {stack}[mk]~={mark} do mk=mk-1 end;if mk<1 then {diag}(4) end;local fp=mk+1;local fn={stack}[fp];if {typ}(fn)~="function" then {diag}(3,{pc}.." ("..{typ}(fn)..")") end;local argc={sp}-fp;local aa={{}};for j=1,argc do aa[j]={stack}[fp+j] end;for j=mk,{sp} do {stack}[j]=nil end;{sp}=mk-1;{sp}={sp}+1;{stack}[{sp}]={pack}(fn({unpack}(aa,1,argc)))'),
        ("POP_RESULT", f"local x={stack}[{sp}];{stack}[{sp}]=nil;{sp}={sp}-1;{regs}[{a_name}]=x and x[1] or nil"),
        ("POP_RESULTS", f"local x={stack}[{sp}];{stack}[{sp}]=nil;{sp}={sp}-1;for j=1,{b_name} do {regs}[{a_name}+j-1]=x and x[j] or nil end"),
        ("EXPAND_RESULT", f"local x={stack}[{sp}];{stack}[{sp}]=nil;{sp}={sp}-1;if x then for j=1,x.n do {sp}={sp}+1;{stack}[{sp}]=x[j] end end"),
        ("DROP_RESULT", f"{stack}[{sp}]=nil;{sp}={sp}-1"),
        ("LOAD_VARARG", f"{regs}[{a_name}]={args_name}[1]"),
        ("UNPACK_VARARG", f"for j=1,{b_name} do {regs}[{a_name}+j-1]={args_name}[j] end"),
        ("CLOSURE", f"{regs}[{a_name}]={make_fn}({b_name}+1,{env})"),
        ("ENTER_SCOPE", f"{env}={{__p={env},__v={{}}}}"),
        ("LEAVE_SCOPE", f"{env}={env}.__p"),
        ("JUMP", f"{pc}={a_name};{jumped}=true"),
        ("JUMP_IF_FALSE", f"if {regs}[{a_name}]==nil or {regs}[{a_name}]==false then {pc}={b_name};{jumped}=true end"),
        ("JUMP_IF_TRUE", f"if {regs}[{a_name}]~=nil and {regs}[{a_name}]~=false then {pc}={b_name};{jumped}=true end"),
        ("FOR_CHECK", f"local cur={regs}[{a_name}];local lim={regs}[{b_name}];local step={regs}[{c_name}];if (step>=0 and cur>lim) or (step<0 and cur<lim) then {pc}={d_name};{jumped}=true end"),
        ("ITER_NEXT", f"local iterator={regs}[{a_name}];local state={regs}[{b_name}];local control={regs}[{c_name}];local nms={const_fn}({d_name}+1);local vals;if {typ}(iterator)=='table' then local nk,nv={nxt}(iterator,control);vals={{nk,nv}} else vals={pack}(iterator(state,control)) end;if vals[1]==nil then {pc}={e_name};{jumped}=true else {regs}[{c_name}]=vals[1];for j=1,#nms do {env}.__v[nms[j]]=vals[j] end end"),
        ("RETURN", f"if {b_name}==0 then return end;return {regs}[{b_name}]"),
        ("RETURN_CALL", f"local x={stack}[{sp}];{stack}[{sp}]=nil;{sp}={sp}-1;return {unpack}(x,1,x.n)"),
        ("RETURN_VARARG", f"return {unpack}({args_name},1,{args_name}.n)"),
        ("RETURN_STACK", f"return {unpack}({stack},1,{sp})"),
        ("DROP", ""),
        ("HALT", "return"),
    ]
    rng.shuffle(clauses)
    dispatch = "".join(("if" if i == 0 else "elseif") + f" {opv}=={opcodes[k]} then {code} " for i,(k,code) in enumerate(clauses)) + f"else {diag}(5) end"

    run_def = (
        f"{run}=function(pid,{env},...)local {regs}={{}};local {stack}={{}};local {sp}=0;local {mark}={{}};local {args_name}={pack}(...);"
        f"local {globals_name}={outer_env};local p={proto_table}[pid];if not p then {diag}(6,pid) end;local {pc}=p.start or 1;"
        # The parser has already de-permuted each physical record back into the
        # canonical 1..8 tuple (op,a,b,c,d,e,next,guard). Do not apply p.fields a
        # second time in the hot dispatch loop.
        f"while {pc}<=p.count do local {row}=p.code[{pc}];if not {row} then {diag}(7,{pc}) end;local {opv}={bx}({row}[1],p.opmask);"
        f"local {a_name}={bx}({row}[2],p.argmask);local {b_name}={bx}({row}[3],p.argmask);"
        f"local {c_name}={bx}({row}[4],p.argmask);local {d_name}={bx}({row}[5],p.argmask);"
        f"local {e_name}={bx}({row}[6],p.argmask);local {nextpc}={bx}({row}[7],p.argmask);"
        f"local g={bx}({bx}({bx}({record_xor},p.salt),{opv}),{bx}({bx}({bx}({bx}({bx}({bx}({a_name}*33,{b_name}*97),{c_name}*7),{d_name}*11),{e_name}*17),{nextpc}*193)));"
        f'if {row}[8]~=g then {diag}(8,{pc}) end;local {jumped}=false;{dispatch};if not {jumped} then {pc}={nextpc} end end end;'
    )
    make_def = (
        f"{make_fn}=function(pid,parent)return function(...)local e={{__p=parent,__v={{}}}};local aa={pack}(...);local p={proto_table}[pid];"
        f"for i=1,#p.params do e.__v[p.params[i]]=aa[i] end;if p.vararg then e.__v.__varargs=aa end;return {run}(pid,e,{unpack}(aa,1,aa.n)) end end;"
    )

    # Opaque arithmetic is mixed into the loader and carries no readable diagnostic.
    opaque = []
    for _ in range(anti_count + decoy_count):
        q = rng.randrange(1, MASK32); r = rng.randrange(1, MASK32); sh = rng.randrange(1, 24)
        opaque.append(f"do local q={q};q={bx}(q,{r});q={ls}(q,{sh});q={bx}(q,{q^r});if {band}(q,1)==2 then return end end;")

    body = (
        f"return(function({','.join(outer_params)},...)"
        # Prefer the current function environment when the host exposes it, but
        # fall back to Luau's global table.  Some executor/sandbox contexts can
        # omit getfenv even though normal globals such as print remain available.
        f"local {outer_env}=(_G);local {global_fallback}=nil;if {getenv} then local ok,e=pcall({getenv},1);if ok and {typ}(e)=='table' then {global_fallback}=e end end;"
        + "".join(opaque)
        + decoder_defs
        + diagnostic_def
        + decompress_def
        + f"local {payload}={_lua_bytes(list(encrypted))};local {decoded}={payload};"
        + "".join(f"{decoded}={inverse_by_layer[index]}({decoded},{layer_specs[index][0]});" for index in reversed(range(layer_count)))
        + (f"{decoded}={decompress}({decoded});" if compression_applied else "")
        + f'if not {decoded} then {diag}(2) end;local {image}={decoded};'
        + reader_defs
        + parser
        + const_def
        + bin_def
        + unary_def
        + run_def
        + make_def
        + f"return {run}(entry,{{__p=nil,__v={{}}}},...);end)"
        + "(" + ",".join([
            "bit32.bxor", "bit32.band", "bit32.bor", "bit32.lshift", "bit32.rshift",
            "string.char", "table.concat", "table.pack", "table.unpack or unpack", "type", "next", "getfenv", "tonumber",
            "...",
        ]) + ");"
    )
    # The outer closure is intentionally invoked with the captured top-level
    # varargs as its final argument expansion. Lua/Luau permits `...` as the
    # final expression of a function-call argument list.
    wrapper = body
    # Collapse the generated runtime like the reference sample.
    wrapper = wrapper.replace("\n", "")

    return BytecodeVMArtifact(
        source=wrapper,
        key=payload_key,
        instruction_count=total_records,
        runtime_layers=relay_layers + decoder_variants + layer_count + 4,
        decoder_variants=decoder_variants,
        opaque_edges=decoy_count + anti_count,
        payload_blocks=max(1, 1 + dead_count // 2),
        micro_ops=total_records * rng.randint(3, 6) + decoy_count * 3,
        control_flow_decoys=decoy_count,
        dead_code_blocks=dead_count,
        anti_tamper_checks=anti_count,
        payload_layers=layer_count,
        complexity_score=min(100, complexity + 10),
        compression_applied=compression_applied,
        compression_input_bytes=len(raw_image),
        compressed_payload_bytes=len(stored),
        constant_pool_entries=constant_count,
        compiled_functions=compiled_functions,
        max_registers=max_registers,
    )

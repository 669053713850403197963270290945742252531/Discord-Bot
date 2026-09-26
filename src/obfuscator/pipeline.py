"""Obfuscation pipeline: parse -> AST transforms -> source generation -> VM."""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass

from .ast import Edit
from .config import DEFAULT_CONFIG, ObfuscationConfig
from .generator import SourceGenerator
from .lexer import scan_tokens
from .parser import parse
from .transforms.constants import transform as transform_constants
from .transforms.control_flow import transform as transform_control_flow
from .transforms.noise import generate as generate_noise
from .transforms.rename import transform as transform_rename
from .transforms.strings import transform as transform_strings
from .virtual_machine import build_vm


@dataclass(slots=True)
class ObfuscationStats:
    input_bytes: int
    output_bytes: int
    edits: int = 0
    renamed_locals: int = 0
    encrypted_strings: int = 0
    obfuscated_constants: int = 0
    scrambled_conditions: int = 0
    vm_instructions: int = 0
    runtime_layers: int = 0
    decoder_variants: int = 0
    opaque_edges: int = 0
    payload_blocks: int = 0
    micro_ops: int = 0
    noise_blocks: int = 0
    string_decoder_variants: int = 0
    string_pool_parts: int = 0
    control_flow_decoys: int = 0
    dead_code_blocks: int = 0
    anti_tamper_checks: int = 0
    payload_layers: int = 0
    source_complexity: int = 0
    vm_compression_requested: bool = False
    vm_compression_applied: bool = False
    vm_compression_source_bytes: int = 0
    vm_compression_payload_bytes: int = 0
    vm_compression_baseline_output_bytes: int = 0
    vm_compression_saved_bytes: int = 0


@dataclass(slots=True)
class ObfuscationResult:
    source: bytes
    seed: int
    stats: ObfuscationStats


def _comment_edits(syntax) -> list[Edit]:
    edits: list[Edit] = []
    for node in syntax.descendants_of_types("comment"):
        raw = syntax.source[node.start_byte:node.end_byte]
        # Preserve newlines so removing a comment cannot fuse two lexical
        # tokens across lines.
        replacement = b"".join(b"\n" if c == 10 else b" " for c in raw)
        edits.append(Edit(node.start_byte, node.end_byte, replacement, "strip comment"))
    return edits


def _replace_markers(source: bytes, string_payloads: dict[int, bytes], seed: int) -> bytes:
    """Replace temporary markers with a polymorphic encrypted string pool."""
    rng = random.Random(seed ^ 0xA77A11CC)

    def lua_name(used: set[str]) -> str:
        alphabet = "IlOoQqZz"
        while True:
            value = "_" + "".join(rng.choice(alphabet) for _ in range(rng.randint(7, 12)))
            if value not in used:
                used.add(value)
                return value

    used_identifiers = {
        token.text.decode("utf-8", "replace")
        for token in scan_tokens(source)
        if token.kind == "identifier"
    }
    names = set(used_identifiers)
    pool_name = lua_name(names)
    getter_name = lua_name(names)
    b_name = lua_name(names)
    bx_name = lua_name(names)
    band_name = lua_name(names)
    bor_name = lua_name(names)
    ls_name = lua_name(names)
    rs_name = lua_name(names)
    concat_name = lua_name(names)

    shift_pool = [(13, 17, 5), (11, 19, 8), (7, 9, 13), (5, 13, 6), (15, 11, 7)]
    rng.shuffle(shift_pool)
    variants = shift_pool[:3]
    pool_key = rng.randrange(1, 0xFFFFFFFF)

    def xs(x: int, shifts: tuple[int, int, int]) -> int:
        a, b, c = shifts
        x &= 0xFFFFFFFF
        x ^= (x << a) & 0xFFFFFFFF
        x ^= x >> b
        x ^= (x << c) & 0xFFFFFFFF
        return x & 0xFFFFFFFF

    def encrypt(value: bytes, item_key: int, variant: int, rotate: int, add: int, step: int) -> list[int]:
        shifts = variants[variant]
        stream = (pool_key ^ item_key) & 0xFFFFFFFF
        out: list[int] = []
        for i, byte in enumerate(value):
            stream = xs((stream ^ ((i + 1) * 0x45D9)) & 0xFFFFFFFF, shifts)
            t = byte ^ (stream & 0xFF)
            if variant == 0:
                t = ((t << rotate) | (t >> (8 - rotate))) & 0xFF
                t = (t + add + i * step) & 0xFF
            elif variant == 1:
                t = (t + add) & 0xFF
                t = ((t << rotate) | (t >> (8 - rotate))) & 0xFF
                t ^= ((i * step) + add) & 0xFF
            else:
                t ^= ((i * step) + add) & 0xFF
                t = (t - add) & 0xFF
                t = ((t << rotate) | (t >> (8 - rotate))) & 0xFF
            out.append(t)
        return out

    # Each literal is split into a randomized number of independent records.
    # A handful of decoy records are added to make frequency-based pool
    # extraction less useful after a runtime snapshot/decompile.
    entries: dict[int, str] = {}
    marker_re = re.compile(rb"__CELESTIAL_STRING_(\d+)_(\d+)_([0-9a-f]+)__\x00")
    for item_key, value in string_payloads.items():
        part_count = min(len(value), rng.randint(1, 3)) or 1
        cuts = sorted(rng.sample(range(1, len(value)), part_count - 1)) if part_count > 1 else []
        bounds = [0, *cuts, len(value)]
        parts: list[str] = []
        for part_index in range(part_count):
            fragment = value[bounds[part_index] : bounds[part_index + 1]]
            variant = rng.randrange(3)
            rotate = rng.randrange(1, 8)
            add = rng.randrange(1, 253)
            step = rng.randrange(1, 31)
            local_key = rng.randrange(1, 0xFFFFFFFF)
            encrypted = encrypt(fragment, local_key, variant, rotate, add, step)
            # [variant, key, rotate, add, step, encrypted-byte-array]
            parts.append(
                "{" + ",".join(
                    [
                        str(variant),
                        str(local_key),
                        str(rotate),
                        str(add),
                        str(step),
                        "{" + ",".join(str(x) for x in encrypted) + "}",
                    ]
                ) + "}"
            )
        entries[item_key] = "{" + ",".join(parts) + "}"

    for _ in range(rng.randint(4, 10)):
        fake_id = rng.randrange(1, 0x7FFFFFFF)
        while fake_id in entries:
            fake_id = rng.randrange(1, 0x7FFFFFFF)
        fake_variant = rng.randrange(3)
        fake_bytes = bytes(rng.randrange(0, 256) for _ in range(rng.randint(4, 15)))
        local_key = rng.randrange(1, 0xFFFFFFFF)
        entries[fake_id] = (
            "{{"
            + ",".join(
                [
                    str(fake_variant), str(local_key), str(rng.randrange(1, 8)), str(rng.randrange(1, 253)),
                    str(rng.randrange(1, 31)), "{" + ",".join(str(x) for x in encrypt(fake_bytes, local_key, fake_variant, 3, 17, 5)) + "}",
                ]
            )
            + "}}"
        )

    pool_literal = "{" + ",".join(f"[{k}]={v}" for k, v in entries.items()) + "}"
    decoder_names = [lua_name(names) for _ in range(3)]
    decoder_defs: list[str] = []
    for variant, (sa, sb, sc) in enumerate(variants):
        fn = decoder_names[variant]
        if variant == 0:
            inverse = f"v=(v-a-((i-1)*st))%256;v={band_name}({bor_name}({ls_name}(v,8-r),{rs_name}(v,r)),255)"
        elif variant == 1:
            inverse = f"v={bx_name}(v,(((i-1)*st+a)%256));v={band_name}({bor_name}({ls_name}(v,8-r),{rs_name}(v,r)),255);v=(v-a)%256"
        else:
            inverse = f"v={band_name}({bor_name}({ls_name}(v,8-r),{rs_name}(v,r)),255);v=(v+a)%256;v={bx_name}(v,(((i-1)*st+a)%256))"
        decoder_defs.append(
            f"local function {fn}(bytes,key,r,a,st)local stream={bx_name}(key,{_u32_literal(pool_key)});local out={{}};"
            f"for i=1,#bytes do stream={bx_name}(stream,((i*{str(0x45D9)})%4294967296));"
            f"stream={bx_name}(stream,{bx_name}({ls_name}(stream,{sa}),0));stream={bx_name}(stream,{rs_name}(stream,{sb}));"
            f"stream={bx_name}(stream,{ls_name}(stream,{sc}));local v=bytes[i];{inverse};"
            f"out[i]={bx_name}(v,{band_name}(stream,255))end;local chars={{}};"
            f"for i=1,#out do chars[i]=string.char(out[i])end;return {concat_name}(chars)end;"
        )

    dispatch_literal = "{" + ",".join(f"[{i+1}]={decoder_names[i]}" for i in range(3)) + "}"
    getter = (
        f"local function {getter_name}(id)local e={pool_name}[id];if not e then return '' end;local out={{}};"
        f"for p=1,#e do local q=e[p];local f=({{{decoder_names[0]},{decoder_names[1]},{decoder_names[2]}}})[q[1]+1];"
        f"out[p]=f(q[6],q[2],q[3],q[4],q[5])end;return {concat_name}(out)end;"
    )
    runtime = (
        f"local {b_name}=bit32;local {bx_name}={b_name}.bxor;local {band_name}={b_name}.band;"
        f"local {bor_name}={b_name}.bor;local {ls_name}={b_name}.lshift;local {rs_name}={b_name}.rshift;"
        f"local {concat_name}=table.concat;local {pool_name}={pool_literal};"
        + "".join(decoder_defs)
        + getter
    )

    def replace(match: re.Match[bytes]) -> bytes:
        item_key = int(match.group(2))
        # Refer to the pool through an arithmetic-equivalent key expression on
        # some literals. This prevents every string call from having the exact
        # same syntactic form.
        mode = rng.randrange(3)
        if mode == 0:
            expr = f"{getter_name}({item_key})"
        elif mode == 1:
            salt = rng.randrange(2, 37)
            expr = f"{getter_name}(({item_key}+{salt})-{salt})"
        else:
            salt = rng.randrange(2, 37)
            expr = f"{getter_name}({item_key}+({salt}-{salt}))"
        return expr.encode()

    return runtime.encode() + b"\n" + marker_re.sub(replace, source)


def _u32_literal(value: int) -> str:
    return str(value & 0xFFFFFFFF)


def obfuscate(source_text: str | bytes, *, config: ObfuscationConfig = DEFAULT_CONFIG) -> ObfuscationResult:
    source = source_text.encode("utf-8") if isinstance(source_text, str) else bytes(source_text)
    if len(source) > config.max_input_bytes:
        raise ValueError(f"Input exceeds the {config.max_input_bytes:,}-byte obfuscation limit.")
    if b"\x00" in source:
        raise ValueError("Luau source contains a NUL byte and cannot be safely processed as text.")

    seed = int.from_bytes(hashlib.blake2b(source + random.randbytes(16), digest_size=8).digest(), "big")
    parsed = parse(source)
    current = parsed.syntax
    all_edit_count = 0
    renamed_count = 0
    constant_count = 0
    condition_count = 0
    string_payloads: dict[int, bytes] = {}

    # The production/default profile keeps the user's source intact. This is
    # deliberate: syntax-only rewrites cannot prove preservation for arbitrary
    # Luau features such as callbacks, metatables, executor APIs, overloads,
    # custom environments, and debug-sensitive code. When semantic_safe is
    # disabled, the experimental AST passes below can be enabled individually.
    source_phase = not config.semantic_safe
    current_source = source

    if source_phase and config.strip_comments:
        parsed_stage = parse(current_source).syntax
        edits = _comment_edits(parsed_stage)
        current_source = SourceGenerator(current_source).apply(edits)
        all_edit_count += len(edits)

    # Synthetic noise is deliberately kept outside the AST rewrite pipeline.
    # Parsing a large generated wrapper together with user code makes a parser
    # failure in generated scaffolding look like a failure in perfectly valid
    # user code.  The noise is already generated as complete Luau statement
    # blocks and is isolated by `do ... end` scopes, so it can safely be attached
    # after the user program has completed its AST transformations.
    if source_phase and config.noise_blocks:
        noise_source, noise_count = generate_noise(seed, blocks=config.noise_blocks)
    else:
        noise_source, noise_count = "", 0

    if source_phase and config.rename_locals:
        parsed_stage = parse(current_source).syntax
        edits = transform_rename(parsed_stage, seed=seed)
        current_source = SourceGenerator(current_source).apply(edits)
        all_edit_count += len(edits)
        renamed_count += sum(1 for e in edits if "rename local" in e.reason)

    if source_phase and config.scramble_control_flow:
        parsed_stage = parse(current_source).syntax
        edits = transform_control_flow(parsed_stage, seed=seed)
        current_source = SourceGenerator(current_source).apply(edits)
        all_edit_count += len(edits)
        condition_count += sum(1 for e in edits if e.reason == "scrambled branch condition")

    if source_phase and config.obfuscate_integer_constants:
        parsed_stage = parse(current_source).syntax
        edits = transform_constants(parsed_stage, seed=seed)
        current_source = SourceGenerator(current_source).apply(edits)
        all_edit_count += len(edits)
        constant_count += sum(1 for e in edits if e.reason == "MBA integer literal")

    if source_phase and config.encrypt_strings:
        parsed_stage = parse(current_source).syntax
        edits, string_payloads = transform_strings(
            parsed_stage, key=seed, min_length=config.string_min_length
        )
        current_source = SourceGenerator(current_source).apply(edits)
        all_edit_count += len(edits)
        current_source = _replace_markers(current_source, string_payloads, seed)

    # Always validate the actual user payload before handing it to the VM. In
    # semantic-safe mode this is the original source; in experimental mode it
    # is the transformed source.
    parse(current_source)

    if noise_source:
        current_source = noise_source.encode() + b"\n" + current_source

    vm_instructions = 0
    runtime_layers = decoder_variants = opaque_edges = payload_blocks = micro_ops = 0
    control_flow_decoys = dead_code_blocks = anti_tamper_checks = payload_layers = 0
    source_complexity = 0
    vm_compression_requested = bool(config.vm_compression)
    vm_compression_applied = False
    vm_compression_source_bytes = 0
    vm_compression_payload_bytes = 0
    vm_compression_baseline_output_bytes = 0
    vm_compression_saved_bytes = 0
    if config.virtualize:
        vm = build_vm(
            current_source,
            seed=seed,
            junk=config.junk_instructions,
            control_flow_decoys=config.control_flow_decoys,
            dead_code_blocks=config.dead_code_blocks,
            anti_tamper_checks=config.anti_tamper_checks,
            payload_layers=config.payload_layers,
            vm_compression=config.vm_compression,
        )
        output = vm.source.encode("utf-8")
        vm_instructions = vm.instruction_count
        runtime_layers = vm.runtime_layers
        decoder_variants = vm.decoder_variants
        opaque_edges = vm.opaque_edges
        payload_blocks = vm.payload_blocks
        micro_ops = vm.micro_ops
        control_flow_decoys = vm.control_flow_decoys
        dead_code_blocks = vm.dead_code_blocks
        anti_tamper_checks = vm.anti_tamper_checks
        payload_layers = vm.payload_layers
        source_complexity = vm.complexity_score
        vm_compression_applied = vm.compression_applied
        vm_compression_source_bytes = vm.compression_input_bytes
        vm_compression_payload_bytes = vm.compressed_payload_bytes

        # Build an identical non-compressed artifact with the same seed so the
        # command can report whether compression actually reduced the final
        # protected file, rather than merely reporting that the payload codec
        # found a shorter intermediate representation. The disabled build uses
        # the same source and complexity profile.
        if config.vm_compression:
            baseline_vm = build_vm(
                current_source,
                seed=seed,
                junk=config.junk_instructions,
                control_flow_decoys=config.control_flow_decoys,
                dead_code_blocks=config.dead_code_blocks,
                anti_tamper_checks=config.anti_tamper_checks,
                payload_layers=config.payload_layers,
                vm_compression=False,
            )
            vm_compression_baseline_output_bytes = len(baseline_vm.source.encode("utf-8"))
            vm_compression_saved_bytes = vm_compression_baseline_output_bytes - len(output)
    else:
        output = current_source

    stats = ObfuscationStats(
        input_bytes=len(source),
        output_bytes=len(output),
        edits=all_edit_count,
        renamed_locals=renamed_count,
        encrypted_strings=len(string_payloads),
        obfuscated_constants=constant_count,
        scrambled_conditions=condition_count,
        vm_instructions=vm_instructions,
        runtime_layers=runtime_layers,
        decoder_variants=decoder_variants,
        opaque_edges=opaque_edges,
        payload_blocks=payload_blocks,
        micro_ops=micro_ops,
        noise_blocks=noise_count,
        string_decoder_variants=3 if string_payloads else 0,
        string_pool_parts=len(string_payloads),
        control_flow_decoys=control_flow_decoys,
        dead_code_blocks=dead_code_blocks,
        anti_tamper_checks=anti_tamper_checks,
        payload_layers=payload_layers,
        source_complexity=source_complexity,
        vm_compression_requested=vm_compression_requested,
        vm_compression_applied=vm_compression_applied,
        vm_compression_source_bytes=vm_compression_source_bytes,
        vm_compression_payload_bytes=vm_compression_payload_bytes,
        vm_compression_baseline_output_bytes=vm_compression_baseline_output_bytes,
        vm_compression_saved_bytes=vm_compression_saved_bytes,
    )
    return ObfuscationResult(output, seed, stats)


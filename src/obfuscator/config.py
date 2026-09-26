"""Configuration for the Celestial Luau source protection pipeline.

The default Discord profile is intentionally semantic-safe: it does not rewrite
user identifiers, conditions, numeric literals, strings, or comments. Virtualization
uses the comprehensive custom Luau-to-bytecode backend first; only constructs that
require runtime semantics outside the VM fall back to the legacy encrypted source VM.
Experimental source transforms remain available for controlled/offline use.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ObfuscationConfig:
    """Build-time switches.

    `semantic_safe=True` is the default because the source itself is not rewritten.
    The VM backend lowers normal Luau syntax into custom bytecode; only runtime
    facilities that cannot be represented without changing semantics use the legacy
    source VM.
    """

    semantic_safe: bool = True

    # Experimental source transforms. These are ignored while semantic_safe is
    # enabled. Experimental source transforms remain opt-in for controlled
    # offline use.
    rename_locals: bool = False
    encrypt_strings: bool = False
    obfuscate_integer_constants: bool = False
    scramble_control_flow: bool = False
    strip_comments: bool = False

    virtualize: bool = True
    vm_compression: bool = False
    # VM values below are baseline intensity anchors. build_vm derives a
    # source-complexity profile and converts each anchor into a randomized
    # per-build range before selecting the actual budget.
    junk_instructions: int = 14
    noise_blocks: int = 0
    control_flow_decoys: int = 16
    dead_code_blocks: int = 12
    anti_tamper_checks: int = 4
    payload_layers: int = 2
    string_min_length: int = 1
    max_input_bytes: int = 5 * 1024 * 1024


DEFAULT_CONFIG = ObfuscationConfig()

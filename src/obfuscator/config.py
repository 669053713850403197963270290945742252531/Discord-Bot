"""Configuration for the Celestial Luau source protection pipeline.

The default Discord profile is intentionally semantic-safe: it does not rewrite
user identifiers, conditions, numeric literals, strings, or comments. The
polymorphic VM still virtualizes and encrypts the complete original source.
Experimental source transforms remain available for controlled/offline use.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ObfuscationConfig:
    """Build-time switches.

    `semantic_safe=True` is the default because arbitrary Luau can contain
    dynamic callbacks, metatables, overloaded operators, executor globals,
    debug-sensitive code, and other constructs that cannot be proven safe by a
    syntax-only rewrite. In that profile the source is kept byte-for-byte
    intact and protection happens in the polymorphic VM layer.
    """

    semantic_safe: bool = True

    # Experimental source transforms. These are ignored while semantic_safe is
    # enabled and can be enabled together for controlled testing.
    rename_locals: bool = False
    encrypt_strings: bool = False
    obfuscate_integer_constants: bool = False
    scramble_control_flow: bool = False
    strip_comments: bool = False

    virtualize: bool = True
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

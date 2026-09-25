"""Source generator/edit engine for AST-driven Luau transformations."""

from __future__ import annotations

from dataclasses import dataclass

from .ast import Edit


class GenerationError(ValueError):
    pass


@dataclass(slots=True)
class SourceGenerator:
    source: bytes

    def apply(self, edits: list[Edit]) -> bytes:
        if not edits:
            return self.source
        ordered = sorted(edits, key=lambda e: (e.start_byte, e.end_byte))
        last_end = -1
        for edit in ordered:
            if edit.start_byte < 0 or edit.end_byte < edit.start_byte or edit.end_byte > len(self.source):
                raise GenerationError(f"Invalid source edit: {edit.reason}")
            if edit.start_byte < last_end:
                raise GenerationError(f"Overlapping source edits are not supported: {edit.reason}")
            last_end = edit.end_byte

        out = bytearray()
        pos = 0
        for edit in ordered:
            out.extend(self.source[pos : edit.start_byte])
            out.extend(edit.replacement)
            pos = edit.end_byte
        out.extend(self.source[pos:])
        return bytes(out)

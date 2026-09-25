"""Thin AST abstractions around the Luau Tree-sitter concrete syntax tree.

The transformations operate on this structured tree rather than doing regex
or global string replacement.  We keep the original source bytes around so
edits can be applied losslessly to ranges that the grammar identifies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional


@dataclass(frozen=True, slots=True)
class Edit:
    start_byte: int
    end_byte: int
    replacement: bytes
    reason: str


@dataclass(slots=True)
class SyntaxTree:
    tree: object
    source: bytes

    @property
    def root(self):
        return self.tree.root_node

    def walk(self, node=None) -> Iterator[object]:
        node = self.root if node is None else node
        yield node
        cursor = node.walk()
        for child in node.children:
            yield from self.walk(child)

    @staticmethod
    def node_text(node: object, source: bytes) -> str:
        text = getattr(node, "text", None)
        if text is None:
            text = source[node.start_byte : node.end_byte]
            if isinstance(text, bytes):
                return text.decode("utf-8", "replace")
        if isinstance(text, bytes):
            return text.decode("utf-8", "replace")
        return str(text)

    @staticmethod
    def parent(node: object) -> Optional[object]:
        return getattr(node, "parent", None)

    @staticmethod
    def ancestors(node: object) -> Iterator[object]:
        cur = getattr(node, "parent", None)
        while cur is not None:
            yield cur
            cur = getattr(cur, "parent", None)

    def descendants_of_types(self, *types: str) -> Iterator[object]:
        wanted = set(types)
        for node in self.walk():
            if getattr(node, "type", None) in wanted:
                yield node

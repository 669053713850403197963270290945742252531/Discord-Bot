"""Luau parser adapter backed by tree-sitter-luau."""

from __future__ import annotations

from dataclasses import dataclass

from .ast import SyntaxTree


class ParserDependencyError(RuntimeError):
    pass


class LuauSyntaxError(ValueError):
    pass


@dataclass(slots=True)
class ParsedLuau:
    syntax: SyntaxTree


def _build_parser():
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_luau
    except ImportError as exc:
        raise ParserDependencyError(
            "Luau AST parsing requires `tree-sitter` and `tree-sitter-luau`. "
            "Install them with `pip install -r requirements.txt`."
        ) from exc

    language_fn = getattr(tree_sitter_luau, "language", None)
    if language_fn is None:
        raise ParserDependencyError("Installed tree-sitter-luau package does not expose language().")

    language_obj = language_fn()
    try:
        language = Language(language_obj)
    except TypeError:
        language = language_obj

    parser = Parser()
    if hasattr(parser, "set_language"):
        parser.set_language(language)
    else:
        parser.language = language
    return parser


def parse(source: bytes) -> ParsedLuau:
    parser = _build_parser()
    tree = parser.parse(source)
    root = tree.root_node
    if root.has_error:
        # Include a useful source location without dumping attacker-controlled
        # source into the exception.
        for node in _walk(root):
            if getattr(node, "is_error", False) or getattr(node, "is_missing", False):
                point = getattr(node, "start_point", (0, 0))
                raise LuauSyntaxError(
                    f"Luau syntax error near line {point[0] + 1}, column {point[1] + 1}."
                )
        raise LuauSyntaxError("Luau syntax error.")
    return ParsedLuau(SyntaxTree(tree=tree, source=source))


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)

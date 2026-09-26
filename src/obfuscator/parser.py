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

    # `tree-sitter-luau` exposes its grammar as a PyCapsule. The capsule must
    # be wrapped by the matching `tree_sitter.Language` binding before it is
    # assigned to a parser. Passing the capsule directly produces:
    # `set_language() argument must tree_sitter.Language, not PyCapsule`.
    language_obj = language_fn()
    try:
        language = Language(language_obj)
    except (TypeError, ValueError) as exc:
        raise ParserDependencyError(
            "The installed tree-sitter and tree-sitter-luau packages are "
            "incompatible. Install the pinned versions from requirements.txt: "
            "tree-sitter==0.25.2 and tree-sitter-luau==1.2.0."
        ) from exc

    if not isinstance(language, Language):
        raise ParserDependencyError(
            "tree-sitter-luau returned a language handle that could not be "
            "converted into tree_sitter.Language. Install the pinned versions "
            "from requirements.txt: tree-sitter==0.25.2 and "
            "tree-sitter-luau==1.2.0."
        )

    # Parser(language) is the modern API and avoids accidentally handing a
    # PyCapsule to `set_language()`.
    try:
        return Parser(language)
    except TypeError:
        parser = Parser()
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

"""Scope-aware local identifier renaming using Luau AST bindings."""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..ast import Edit, SyntaxTree
from ..lexer import scan_tokens


_RESERVED = {
    "and", "break", "continue", "do", "else", "elseif", "end", "export",
    "false", "for", "function", "if", "in", "local", "nil", "not", "or",
    "repeat", "return", "then", "true", "type", "until", "while",
}

_SCOPE_TYPES = {
    "chunk",
    "block",
    "do_statement",
    "while_statement",
    "repeat_statement",
    "if_statement",
    "for_statement",
    "function_declaration",
    "function_definition",
}
_LOCAL_DECL_TYPES = {"variable_declaration", "local_declaration", "local_variable_declaration"}


@dataclass(frozen=True, slots=True)
class _Definition:
    node: object
    name: str
    scope: object
    activation: int
    visibility_end: int
    recursive_body_start: int | None = None
    recursive_body_end: int | None = None


def _scope_for(node: object):
    cur = getattr(node, "parent", None)
    while cur is not None:
        if getattr(cur, "type", None) in _SCOPE_TYPES:
            return cur
        cur = getattr(cur, "parent", None)
    return None


def _node_text(node: object, source: bytes) -> str:
    text = getattr(node, "text", None)
    if isinstance(text, bytes):
        return text.decode("utf-8", "replace")
    return str(text) if text is not None else source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _ancestors(syntax: SyntaxTree, node: object):
    return list(syntax.ancestors(node))


def _first_identifier(syntax: SyntaxTree, node: object):
    candidates = [n for n in syntax.descendants_of_types("identifier") if n.start_byte >= node.start_byte and n.end_byte <= node.end_byte]
    return min(candidates, key=lambda n: n.start_byte) if candidates else None


def _definition_nodes(syntax: SyntaxTree) -> list[_Definition]:
    """Collect only actual local bindings; never type names or RHS references."""
    definitions: list[_Definition] = []

    # `local a, b: T = ...` is represented by a variable_declaration whose
    # variable_list contains only the binding variables; type nodes are sibling
    # children of that variable_list and are deliberately ignored.
    declaration_nodes = [
        n for typ in _LOCAL_DECL_TYPES for n in syntax.descendants_of_types(typ)
    ]
    for declaration in declaration_nodes:
        variable_list = declaration.child_by_field_name("variables") if hasattr(declaration, "child_by_field_name") else None
        if variable_list is None:
            variable_list = next((c for c in declaration.children if getattr(c, "type", "") == "variable_list"), None)
        if variable_list is None:
            continue
        for child in variable_list.children:
            if getattr(child, "type", "") != "variable":
                continue
            ident = _first_identifier(syntax, child)
            if ident is None:
                continue
            name = _node_text(ident, syntax.source)
            if name in _RESERVED or name.startswith("_"):
                continue
            scope = _scope_for(declaration) or declaration
            definitions.append(_Definition(ident, name, scope, declaration.end_byte, scope.end_byte))

    # `local function foo()` binds foo in the enclosing scope and recursively
    # in its own body. A method path (`function A:b()`) is never a local name.
    for decl in syntax.descendants_of_types("function_declaration"):
        name_node = decl.child_by_field_name("name") if hasattr(decl, "child_by_field_name") else None
        if name_node is None or getattr(name_node, "type", "") != "identifier":
            continue
        prefix = syntax.source[decl.start_byte:name_node.start_byte].lstrip()
        if not prefix.startswith(b"local function"):
            continue
        name = _node_text(name_node, syntax.source)
        if name in _RESERVED or name.startswith("_"):
            continue
        scope = _scope_for(decl) or decl
        body = decl.child_by_field_name("body") if hasattr(decl, "child_by_field_name") else None
        definitions.append(_Definition(
            name_node, name, scope, decl.end_byte, scope.end_byte,
            body.start_byte if body is not None else None,
            body.end_byte if body is not None else None,
        ))

    # Function parameters: the first identifier inside each `parameter` is the
    # binding. A typed parameter may contain many later identifiers in its type.
    for parameter in syntax.descendants_of_types("parameter"):
        ident = _first_identifier(syntax, parameter)
        if ident is None:
            continue
        name = _node_text(ident, syntax.source)
        if name in _RESERVED or name.startswith("_"):
            continue
        scope = _scope_for(parameter) or parameter
        fn = next((a for a in _ancestors(syntax, parameter) if getattr(a, "type", "") in {"function_declaration", "function_definition"}), None)
        activation = getattr(fn, "start_byte", parameter.end_byte) if fn is not None else parameter.end_byte
        if fn is not None:
            body = fn.child_by_field_name("body") if hasattr(fn, "child_by_field_name") else None
            if body is not None:
                activation = body.start_byte
        definitions.append(_Definition(
            ident, name, scope, activation,
            getattr(scope, "end_byte", parameter.end_byte),
        ))

    # Numeric/generic for variables are explicit AST bindings. Their loop
    # variable is not in scope while iterator/start/end expressions evaluate.
    for clause in syntax.descendants_of_types("for_numeric_clause"):
        name_node = clause.child_by_field_name("name") if hasattr(clause, "child_by_field_name") else None
        if name_node is None or getattr(name_node, "type", "") != "identifier":
            continue
        name = _node_text(name_node, syntax.source)
        if name in _RESERVED or name.startswith("_"):
            continue
        parent_for = getattr(clause, "parent", None)
        scope = parent_for if getattr(parent_for, "type", "") == "for_statement" else _scope_for(clause) or clause
        body = scope.child_by_field_name("body") if hasattr(scope, "child_by_field_name") else None
        activation = body.start_byte if body is not None else clause.end_byte
        definitions.append(_Definition(
            name_node, name, scope, activation,
            getattr(scope, "end_byte", clause.end_byte),
        ))

    for clause in syntax.descendants_of_types("for_generic_clause"):
        variable_list = next((c for c in clause.children if getattr(c, "type", "") == "variable_list"), None)
        if variable_list is None:
            continue
        parent_for = getattr(clause, "parent", None)
        scope = parent_for if getattr(parent_for, "type", "") == "for_statement" else _scope_for(clause) or clause
        body = scope.child_by_field_name("body") if hasattr(scope, "child_by_field_name") else None
        activation = body.start_byte if body is not None else clause.end_byte
        for child in variable_list.children:
            if getattr(child, "type", "") != "variable":
                continue
            ident = _first_identifier(syntax, child)
            if ident is None:
                continue
            name = _node_text(ident, syntax.source)
            if name in _RESERVED or name.startswith("_"):
                continue
            definitions.append(_Definition(
                ident, name, scope, activation,
                getattr(scope, "end_byte", clause.end_byte),
            ))

    unique: dict[tuple[int, int], _Definition] = {}
    for definition in definitions:
        unique[(definition.node.start_byte, definition.node.end_byte)] = definition
    return list(unique.values())


def _reference_nodes(syntax: SyntaxTree, definitions: list[_Definition]):
    """Find value identifiers only; exclude bindings, properties, and types.

    Tree-sitter grammar revisions do not always expose the same member-node
    shape. AST binding resolution remains authoritative, while the lexical
    barrier below guarantees that a name following `.` or `:` is never treated
    as a renameable local reference.
    """
    definition_ranges = {(d.node.start_byte, d.node.end_byte) for d in definitions}
    refs = []

    significant = [t for t in scan_tokens(syntax.source) if t.kind != "comment"]
    previous_by_range: dict[tuple[int, int], object | None] = {}
    previous = None
    for token in significant:
        previous_by_range[(token.start, token.end)] = previous
        previous = token

    for node in syntax.descendants_of_types("identifier"):
        rng = (node.start_byte, node.end_byte)
        if rng in definition_ranges:
            continue

        ancestors = _ancestors(syntax, node)
        atypes = {getattr(a, "type", "") for a in ancestors}
        if "type" in atypes or "generic_type_list" in atypes:
            continue

        parent = getattr(node, "parent", None)
        if parent is None:
            continue

        prev_token = previous_by_range.get((node.start_byte, node.end_byte))
        if prev_token is not None and prev_token.kind == "punct" and prev_token.text in {b".", b":"}:
            # Member/property names are part of the public API surface and must
            # never be renamed (`obj.foo`, `obj:foo`, `Enum.KeyCode`, etc.).
            continue

        ptype = getattr(parent, "type", "")

        # Exact field metadata is safer than assuming an identifier is the last
        # child of a member/index expression.
        for field_name in ("field", "method"):
            member = parent.child_by_field_name(field_name) if hasattr(parent, "child_by_field_name") else None
            if member is not None and member.start_byte == node.start_byte and member.end_byte == node.end_byte:
                break
        else:
            member = None
        if member is not None and ptype in {"dot_index_expression", "method_index_expression", "bracket_index_expression"}:
            continue

        # Table keys such as `{ [x] = y }` are expressions and stay references;
        # bare `{ key = value }` names are field labels, not variables.
        if ptype in {"field", "table_field", "table_item"}:
            key = parent.child_by_field_name("name") if hasattr(parent, "child_by_field_name") else None
            if key is not None and key.start_byte == node.start_byte and key.end_byte == node.end_byte:
                continue

        # A function declaration name is a declaration/property, never a value
        # reference. Local function bindings were already collected above.
        if ptype == "function_declaration":
            name_node = parent.child_by_field_name("name") if hasattr(parent, "child_by_field_name") else None
            if name_node is not None and name_node.start_byte == node.start_byte and name_node.end_byte == node.end_byte:
                continue

        refs.append(node)
    return refs


def transform(syntax: SyntaxTree, *, seed: int) -> list[Edit]:
    rng = random.Random(seed ^ 0xA91F_17D3)
    definitions = _definition_nodes(syntax)
    refs = _reference_nodes(syntax, definitions)
    if not definitions:
        return []

    counter = 0
    scope_maps: dict[int, dict[str, str]] = {}
    scope_defs: dict[int, list[_Definition]] = {}
    existing_identifiers = {
        token.text.decode("utf-8", "replace")
        for token in scan_tokens(syntax.source)
        if token.kind == "identifier"
    }
    alphabet = "IlOoQqZzuvwxy"

    def new_name(used: set[str]) -> str:
        nonlocal counter
        while True:
            counter += 1
            if counter < 100:
                candidate = "_" + alphabet[rng.randrange(len(alphabet))] + "x" + format(counter, "x")
            else:
                candidate = "_" + "".join(rng.choice(alphabet) for _ in range(5)) + format(counter, "x")
            if (
                candidate not in used
                and candidate not in _RESERVED
                and candidate not in existing_identifiers
            ):
                return candidate

    for definition in sorted(definitions, key=lambda d: d.node.start_byte):
        mapping = scope_maps.setdefault(id(definition.scope), {})
        if definition.name not in mapping:
            mapping[definition.name] = new_name(set(mapping.values()) | _RESERVED)
        scope_defs.setdefault(id(definition.scope), []).append(definition)

    edits: list[Edit] = []
    for definition in definitions:
        edits.append(Edit(
            definition.node.start_byte,
            definition.node.end_byte,
            scope_maps[id(definition.scope)][definition.name].encode(),
            "rename local definition",
        ))

    def nearest_scope_chain(ref: object):
        scope = _scope_for(ref)
        seen: set[int] = set()
        while scope is not None and id(scope) not in seen:
            seen.add(id(scope))
            yield scope
            # Repeat conditions can see locals declared in the repeat body even
            # though the condition is a sibling of that body block.
            if getattr(scope, "type", "") == "repeat_statement":
                body = scope.child_by_field_name("body") if hasattr(scope, "child_by_field_name") else None
                if body is not None:
                    yield body
            scope = _scope_for(scope)

    def resolve(ref: object) -> str | None:
        name = _node_text(ref, syntax.source)
        for scope in nearest_scope_chain(ref):
            candidates = [
                d for d in scope_defs.get(id(scope), ())
                if d.name == name
                and ref.start_byte < d.visibility_end
                and (
                    d.activation <= ref.start_byte
                    or (
                        d.recursive_body_start is not None
                        and d.recursive_body_end is not None
                        and d.recursive_body_start <= ref.start_byte < d.recursive_body_end
                    )
                )
            ]
            # A local function's binding is visible recursively inside its body
            # even though the body begins before the declaration statement ends.
            # Outside that body, its ordinary local visibility starts afterward.
            if candidates:
                filtered = []
                for d in candidates:
                    recursive_here = (
                        d.recursive_body_start is not None
                        and d.recursive_body_end is not None
                        and d.recursive_body_start <= ref.start_byte < d.recursive_body_end
                    )
                    if d.recursive_body_start is not None and ref.start_byte < d.activation and not recursive_here:
                        continue
                    filtered.append(d)
                candidates = filtered
            if candidates:
                chosen = max(candidates, key=lambda d: (d.activation, d.node.start_byte))
                return scope_maps[id(chosen.scope)][name]
        return None

    for ref in refs:
        renamed = resolve(ref)
        if renamed:
            edits.append(Edit(ref.start_byte, ref.end_byte, renamed.encode(), "rename local reference"))

    return edits

"""Comprehensive Luau -> Celestial bytecode lowering.

The compiler is deliberately grammar-driven instead of relying on a small set
of hand-picked AST shapes.  Luau's tree-sitter grammar is a Lua grammar with
Luau-specific additions layered on top, so the compiler normalizes aliases,
wrapper nodes, binding lists, and expression lists before lowering them.

The goal is to compile normal Luau/Roblox programs into Celestial bytecode and
reserve the legacy source VM only for language constructs that genuinely need
runtime behavior that this VM does not yet model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ast import SyntaxTree
from .transforms.strings import _decode_lua_string


class UnsupportedLuau(ValueError):
    """The source uses a construct the custom VM cannot faithfully lower."""


@dataclass(frozen=True, slots=True)
class BCInstr:
    op: str
    a: int = 0
    b: int = 0
    c: int = 0
    d: int = 0
    e: int = 0


@dataclass(slots=True)
class BCProto:
    params: list[str]
    vararg: bool
    code: list[BCInstr] = field(default_factory=list)
    max_regs: int = 0


@dataclass(slots=True)
class BytecodeProgram:
    protos: list[BCProto]
    constants: list[Any]
    entry_proto: int
    instruction_count: int
    max_regs: int
    compiled_functions: int
    local_bindings: int


@dataclass(slots=True)
class _Scope:
    parent: "_Scope | None"
    names: set[str] = field(default_factory=set)

    def depth(self, name: str) -> int | None:
        depth = 0
        cur: _Scope | None = self
        while cur is not None:
            if name in cur.names:
                return depth
            cur = cur.parent
            depth += 1
        return None


@dataclass(slots=True)
class _FunctionContext:
    proto: BCProto
    scope: _Scope
    loop_stack: list[tuple[list[int], int, int]] = field(default_factory=list)
    scope_depth: int = 0
    next_reg: int = 0
    max_reg: int = 0

    def alloc(self) -> int:
        reg = self.next_reg
        self.next_reg += 1
        self.max_reg = max(self.max_reg, self.next_reg)
        return reg


_OPERATOR_CODES = {
    "or": 0,
    "and": 1,
    "==": 2,
    "~=": 3,
    "<": 4,
    "<=": 5,
    ">": 6,
    ">=": 7,
    "+": 8,
    "-": 9,
    "*": 10,
    "/": 11,
    "//": 12,
    "%": 13,
    "^": 14,
    "..": 15,
    "&": 16,
    "|": 17,
    "~": 18,
    "<<": 19,
    ">>": 20,
}
_UNARY_CODES = {"-": 0, "not": 1, "#": 2, "~": 3}

def _field(node: object, name: str):
    fn = getattr(node, "child_by_field_name", None)
    if callable(fn):
        return fn(name)
    return None


def _named_children(node: object) -> list[object]:
    children = getattr(node, "named_children", None)
    if children is not None:
        return list(children)
    return [c for c in getattr(node, "children", []) if getattr(c, "is_named", True)]


def _children(node: object) -> list[object]:
    return list(getattr(node, "children", []) or [])


def _text(node: object, source: bytes) -> str:
    value = getattr(node, "text", None)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if value is not None:
        return str(value)
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _ident_name(node: object, source: bytes) -> str:
    return _text(node, source).strip()


def _node_location(node: object) -> str:
    point = getattr(node, "start_point", None)
    if point is None:
        return ""
    try:
        return f" at line {int(point[0]) + 1}, column {int(point[1]) + 1}"
    except Exception:
        return ""


def _unsupported(message: str, node: object | None = None) -> UnsupportedLuau:
    return UnsupportedLuau(message + (_node_location(node) if node is not None else ""))


def _is_type(node: object) -> bool:
    return getattr(node, "type", "") in {
        "type", "builtin_type", "tuple_type", "function_type", "generic_type",
        "object_type", "empty_type", "field_type", "intersection_type",
        "union_type", "optional_type", "literal_type", "variadic_type",
        "generic_type_list",
    }


def _is_call(node: object) -> bool:
    return getattr(node, "type", "") == "function_call"


def _is_vararg(node: object) -> bool:
    return getattr(node, "type", "") == "vararg_expression"


def _number_value(raw: str) -> int | float:
    value = raw.replace("_", "")
    lower = value.lower()
    # Luau's number grammar permits integer suffixes in the inherited grammar.
    if lower.endswith("ull"):
        value = value[:-3]
        lower = lower[:-3]
    if lower.startswith("0x"):
        if "p" in lower:
            return float.fromhex(value)
        return int(value, 16)
    if lower.startswith("0b"):
        return int(value, 2)
    if any(ch in value for ch in ".eE"):
        return float(value)
    return int(value, 10)


def _operator_between(node: object, left: object, right: object, source: bytes) -> str:
    """Get a binary/update operator without depending on a nonexistent AST field."""
    # tree-sitter-luau's grammar uses literal anonymous operator tokens, not an
    # `operator` field.  The bytes between the two operand nodes are therefore
    # the most stable representation across grammar revisions.
    try:
        middle = source[left.end_byte : right.start_byte].decode("utf-8", "replace").strip()
    except Exception:
        middle = ""
    if middle in _OPERATOR_CODES or middle in {
        "+=", "-=", "*=", "/=", "//=", "%=", "^=", "..=",
    }:
        return middle
    # Fall back to scanning anonymous children so whitespace/comments never
    # become part of the operator.
    for child in _children(node):
        if getattr(child, "is_named", False):
            continue
        raw = _text(child, source).strip()
        if raw in _OPERATOR_CODES or raw in {"+=", "-=", "*=", "/=", "//=", "%=", "^=", "..="}:
            return raw
    return middle


def _unary_operator(node: object, operand: object, source: bytes) -> str:
    try:
        raw = source[node.start_byte : operand.start_byte].decode("utf-8", "replace").strip()
    except Exception:
        raw = ""
    if raw in _UNARY_CODES:
        return raw
    for child in _children(node):
        if getattr(child, "is_named", False):
            continue
        raw = _text(child, source).strip()
        if raw in _UNARY_CODES:
            return raw
    return raw


def _descendants(node: object):
    yield node
    for child in _children(node):
        yield from _descendants(child)


def compile_luau(syntax: SyntaxTree) -> BytecodeProgram:
    return _Compiler(syntax).compile()


class _Compiler:
    def __init__(self, syntax: SyntaxTree):
        self.syntax = syntax
        self.source = syntax.source
        self.constants: list[Any] = []
        self.constant_map: dict[tuple[str, object], int] = {}
        self.protos: list[BCProto] = []
        self.compiled_functions = 0
        self.local_bindings = 0

    # ------------------------------ constants -----------------------------
    def const(self, value: Any) -> int:
        if value is None:
            key = ("nil", None)
        elif isinstance(value, bool):
            key = ("bool", value)
        elif isinstance(value, int):
            key = ("int", value)
        elif isinstance(value, float):
            key = ("float", value)
        elif isinstance(value, bytes):
            value = value.decode("utf-8", "surrogateescape")
            key = ("str", value)
        elif isinstance(value, str):
            key = ("str", value)
        elif isinstance(value, tuple) and all(isinstance(x, str) for x in value):
            key = ("tuple", value)
        else:
            raise _unsupported(f"unsupported constant type {type(value).__name__}")
        if key not in self.constant_map:
            self.constant_map[key] = len(self.constants)
            self.constants.append(value)
        return self.constant_map[key]

    # ------------------------------- compile ------------------------------
    def compile(self) -> BytecodeProgram:
        # Global functions are resolved by the generated VM through the host
        # environment.  Executor-provided globals such as getgenv(), getsenv(),
        # hookfunction(), newcclosure(), etc. are therefore ordinary callable
        # globals from the compiler's perspective and must not force the entire
        # script into the legacy source-payload backend.
        entry = BCProto(params=[], vararg=False)
        self.protos.append(entry)
        self.compiled_functions += 1
        ctx = _FunctionContext(entry, _Scope(None))
        self._compile_body(ctx, self.syntax.root)
        if not entry.code or entry.code[-1].op not in {"RETURN", "RETURN_CALL", "RETURN_VARARG", "RETURN_STACK", "HALT"}:
            entry.code.append(BCInstr("RETURN", 0))
        entry.max_regs = ctx.max_reg

        total = sum(len(p.code) for p in self.protos)
        return BytecodeProgram(
            protos=self.protos,
            constants=self.constants,
            entry_proto=0,
            instruction_count=total,
            max_regs=max((p.max_regs for p in self.protos), default=0),
            compiled_functions=self.compiled_functions,
            local_bindings=self.local_bindings,
        )

    # ------------------------------ blocks ---------------------------------
    def _compile_body(self, ctx: _FunctionContext, node: object) -> None:
        for child in _named_children(node):
            typ = getattr(child, "type", "")
            if typ in {"comment", "hash_bang_line"} or _is_type(child):
                continue
            if typ in {"statement", "expression_statement", "expression"}:
                inner = [c for c in _named_children(child) if not _is_type(c)]
                if len(inner) == 1:
                    child = inner[0]
                    typ = getattr(child, "type", "")
            if typ == "return_statement":
                self._compile_return(ctx, child)
            else:
                self._compile_statement(ctx, child)

    def _compile_statement(self, ctx: _FunctionContext, node: object) -> None:
        typ = getattr(node, "type", "")
        if typ in {"empty_statement", "implicit_variable_declaration", "comment", "hash_bang_line", "type_definition"}:
            return
        if typ == "variable_declaration":
            self._compile_local_declaration(ctx, node)
            return
        if typ == "assignment_statement":
            self._compile_assignment(ctx, node)
            return
        if typ == "update_statement":
            self._compile_update(ctx, node)
            return
        if typ == "function_declaration":
            self._compile_function_declaration(ctx, node)
            return
        if typ == "function_call":
            self._compile_call(ctx, node, discard=True)
            return
        if typ == "if_statement":
            self._compile_if(ctx, node)
            return
        if typ == "while_statement":
            self._compile_while(ctx, node)
            return
        if typ == "repeat_statement":
            self._compile_repeat(ctx, node)
            return
        if typ == "for_statement":
            self._compile_for(ctx, node)
            return
        if typ == "do_statement":
            self._compile_do(ctx, node)
            return
        if typ == "break_statement":
            self._compile_break(ctx, node)
            return
        if typ == "continue_statement":
            self._compile_continue(ctx, node)
            return
        # A few grammar builds expose an extra transparent expression wrapper.
        children = [c for c in _named_children(node) if not _is_type(c)]
        if len(children) == 1 and typ not in {
            "binary_expression", "unary_expression", "variable", "dot_index_expression", "bracket_index_expression",
        }:
            inner = children[0]
            if getattr(inner, "type", "") != typ:
                return self._compile_statement(ctx, inner)
        # Expression statements are not generally valid Lua statements, but a
        # direct expression wrapper may appear in parser compatibility layers.
        if typ in {
            "binary_expression", "unary_expression", "parenthesized_expression", "table_constructor",
            "function_definition", "if_expression", "cast_expression", "variable", "identifier",
            "dot_index_expression", "bracket_index_expression",
        }:
            reg = self._compile_expr(ctx, node)
            ctx.proto.code.append(BCInstr("DROP", a=reg))
            return
        raise _unsupported(f"unsupported statement node '{typ}'", node)

    # -------------------------- declarations / assignment ------------------
    def _binding_names(self, variable_list: object) -> list[str]:
        names: list[str] = []
        for child in _named_children(variable_list):
            typ = getattr(child, "type", "")
            if typ == "identifier":
                name = _ident_name(child, self.source)
            elif typ == "variable":
                ident = next((c for c in _named_children(child) if getattr(c, "type", "") == "identifier"), None)
                name = _ident_name(ident, self.source) if ident is not None else _ident_name(child, self.source)
            elif typ == "parameter":
                ident = next((c for c in _named_children(child) if getattr(c, "type", "") == "identifier"), None)
                if ident is None:
                    continue
                name = _ident_name(ident, self.source)
            else:
                continue
            if not name:
                raise _unsupported("empty local binding", child)
            names.append(name)
        return names

    def _find_variable_list(self, node: object) -> object | None:
        direct = next((c for c in _named_children(node) if getattr(c, "type", "") == "variable_list"), None)
        if direct is not None:
            return direct
        for child in _named_children(node):
            if getattr(child, "type", "") == "assignment_statement":
                direct = next((c for c in _named_children(child) if getattr(c, "type", "") == "variable_list"), None)
                if direct is not None:
                    return direct
        for child in _descendants(node):
            if getattr(child, "type", "") == "variable_list":
                return child
        return None

    def _find_expression_list(self, node: object) -> object | None:
        direct = next((c for c in _named_children(node) if getattr(c, "type", "") == "expression_list"), None)
        if direct is not None:
            return direct
        for child in _descendants(node):
            if getattr(child, "type", "") == "expression_list":
                return child
        return None

    def _expr_items(self, node: object | None) -> list[object]:
        if node is None:
            return []
        return [c for c in _named_children(node) if not _is_type(c) and getattr(c, "type", "") not in {"comment"}]

    def _compile_local_declaration(self, ctx: _FunctionContext, node: object) -> None:
        variables = self._find_variable_list(node)
        if variables is None:
            raise _unsupported("local declaration has no variable list", node)
        names = self._binding_names(variables)
        if not names:
            raise _unsupported("local declaration has no simple variable bindings", node)
        values_node = self._find_expression_list(node)
        values = self._expr_items(values_node)

        # New locals do not enter the active scope until their RHS is evaluated.
        targets = [("var", name, 0) for name in names]
        self._assign_expression_values(ctx, targets, values, local_declare=True, prepared=True)
        for name in names:
            if name not in ctx.scope.names:
                ctx.scope.names.add(name)
                self.local_bindings += 1

    def _variable_list_targets(self, variables_node: object, node: object) -> list[object]:
        targets = [c for c in _named_children(variables_node) if not _is_type(c)]
        if not targets:
            raise _unsupported("assignment has no targets", node)
        return targets

    def _compile_assignment(self, ctx: _FunctionContext, node: object) -> None:
        variables_node = self._find_variable_list(node)
        values_node = self._find_expression_list(node)
        if variables_node is None or values_node is None:
            raise _unsupported("malformed assignment: expected variable and expression lists", node)
        targets = self._prepare_targets(ctx, self._variable_list_targets(variables_node, node), local_declare=False)
        self._assign_expression_values(ctx, targets, self._expr_items(values_node), local_declare=False, prepared=True)

    def _compile_update(self, ctx: _FunctionContext, node: object) -> None:
        variables_node = self._find_variable_list(node)
        values_node = self._find_expression_list(node)
        if variables_node is None or values_node is None:
            raise _unsupported("malformed compound assignment", node)
        targets = self._variable_list_targets(variables_node, node)
        values = self._expr_items(values_node)
        if len(targets) != 1 or len(values) != 1:
            raise _unsupported("compound assignment must have exactly one target and one value", node)
        left, right = targets[0], values[0]
        op = _operator_between(node, variables_node, values_node, self.source)
        if not op.endswith("=") or op[:-1] not in _OPERATOR_CODES:
            raise _unsupported(f"unsupported compound assignment operator '{op}'", node)
        prepared = self._prepare_targets(ctx, [left], local_declare=False)
        old_reg = self._load_prepared_target(ctx, prepared[0])
        rhs_reg = self._compile_expr(ctx, right)
        dest = ctx.alloc()
        ctx.proto.code.append(BCInstr("BIN", a=dest, b=old_reg, c=rhs_reg, d=_OPERATOR_CODES[op[:-1]]))
        self._store_prepared_target(ctx, prepared[0], dest, local_declare=False)

    def _assign_expression_values(
        self,
        ctx: _FunctionContext,
        targets: list[tuple] | list[object],
        values: list[object],
        *,
        local_declare: bool,
        prepared: bool = False,
    ) -> None:
        prepared_targets = targets if prepared else self._prepare_targets(ctx, targets, local_declare=local_declare)
        assigned: list[int] = []
        wanted = len(prepared_targets)

        for index, value in enumerate(values):
            remaining = wanted - len(assigned)
            last = index == len(values) - 1
            if remaining <= 0:
                # Extra RHS values are still evaluated, but in single-value
                # context.  This preserves function side effects without
                # pretending those results are assigned anywhere.
                self._emit_single_value(ctx, value, discard=True)
                continue
            if last and (_is_call(value) or _is_vararg(value)):
                if _is_call(value):
                    self._compile_call(ctx, value, discard=False, leave_results=True)
                    regs = [ctx.alloc() for _ in range(remaining)]
                    ctx.proto.code.append(BCInstr("POP_RESULTS", a=regs[0], b=remaining))
                else:
                    regs = [ctx.alloc() for _ in range(remaining)]
                    ctx.proto.code.append(BCInstr("UNPACK_VARARG", a=regs[0], b=remaining))
                assigned.extend(regs)
                continue
            assigned.append(self._emit_single_value(ctx, value, discard=False))

        while len(assigned) < wanted:
            reg = ctx.alloc()
            ctx.proto.code.append(BCInstr("LOAD_CONST", a=reg, b=self.const(None)))
            assigned.append(reg)

        for target, value_reg in zip(prepared_targets, assigned):
            self._store_prepared_target(ctx, target, value_reg, local_declare=local_declare)

    def _prepare_targets(self, ctx: _FunctionContext, targets: list[object], *, local_declare: bool) -> list[tuple]:
        prepared: list[tuple] = []
        for target in targets:
            typ = getattr(target, "type", "")
            if typ == "variable":
                inner = _named_children(target)
                if len(inner) == 1 and getattr(inner[0], "type", "") in {"dot_index_expression", "bracket_index_expression"}:
                    target = inner[0]
                    typ = getattr(target, "type", "")
                else:
                    ident = next((c for c in inner if getattr(c, "type", "") == "identifier"), None)
                    name = _ident_name(ident, self.source) if ident is not None else _ident_name(target, self.source)
                    depth = 0 if local_declare else ctx.scope.depth(name)
                    prepared.append(("var", name, depth))
                    continue
            if typ == "identifier":
                name = _ident_name(target, self.source)
                depth = 0 if local_declare else ctx.scope.depth(name)
                prepared.append(("var", name, depth))
                continue
            if typ in {"dot_index_expression", "bracket_index_expression"}:
                table = _field(target, "table")
                if table is None:
                    children = _named_children(target)
                    table = children[0] if children else None
                if table is None:
                    raise _unsupported("indexed assignment has no table", target)
                table_reg = self._compile_expr(ctx, table)
                if typ == "dot_index_expression":
                    field_node = _field(target, "field")
                    if field_node is None:
                        raise _unsupported("dot assignment has no field", target)
                    key_reg = ctx.alloc()
                    ctx.proto.code.append(BCInstr("LOAD_CONST", a=key_reg, b=self.const(_ident_name(field_node, self.source))))
                else:
                    field_node = _field(target, "field")
                    if field_node is None:
                        raise _unsupported("indexed assignment has no key", target)
                    key_reg = self._compile_expr(ctx, field_node)
                prepared.append(("index", table_reg, key_reg))
                continue
            raise _unsupported(f"unsupported assignment target '{typ}'", target)
        return prepared

    def _load_prepared_target(self, ctx: _FunctionContext, target: tuple) -> int:
        if target[0] == "var":
            _, name, depth = target
            dest = ctx.alloc()
            if depth is None:
                ctx.proto.code.append(BCInstr("LOAD_GLOBAL", a=dest, b=self.const(name)))
            else:
                ctx.proto.code.append(BCInstr("LOAD_VAR", a=dest, b=depth, c=self.const(name)))
            return dest
        if target[0] == "index":
            dest = ctx.alloc()
            ctx.proto.code.append(BCInstr("GET_INDEX", a=dest, b=target[1], c=target[2]))
            return dest
        raise _unsupported("unknown prepared assignment target")

    def _store_prepared_target(self, ctx: _FunctionContext, target: tuple, value_reg: int, *, local_declare: bool) -> None:
        if target[0] == "var":
            _, name, depth = target
            if depth is None:
                ctx.proto.code.append(BCInstr("STORE_GLOBAL", a=value_reg, b=self.const(name)))
            else:
                ctx.proto.code.append(BCInstr("STORE_VAR", a=value_reg, b=depth, c=self.const(name)))
            return
        if target[0] == "index":
            ctx.proto.code.append(BCInstr("SET_INDEX", a=target[1], b=target[2], c=value_reg))
            return
        raise _unsupported("unknown prepared assignment target")

    # ------------------------------- functions ------------------------------
    def _parameter_names(self, params_node: object | None) -> tuple[list[str], bool]:
        if params_node is None:
            return [], False
        names: list[str] = []
        vararg = False
        for child in _named_children(params_node):
            typ = getattr(child, "type", "")
            if typ == "identifier":
                names.append(_ident_name(child, self.source))
            elif typ == "vararg_expression":
                vararg = True
            elif typ == "parameter":
                inner = _named_children(child)
                ident = next((x for x in inner if getattr(x, "type", "") == "identifier"), None)
                if ident is not None:
                    names.append(_ident_name(ident, self.source))
                if any(getattr(x, "type", "") == "vararg_expression" for x in inner):
                    vararg = True
        return names, vararg

    def _compile_nested_function(self, node: object, captured_scope: _Scope, *, method_self: bool = False) -> int:
        params, vararg = self._parameter_names(_field(node, "parameters"))
        if method_self:
            params.insert(0, "self")
        body = _field(node, "body")
        if body is None:
            body = node
        proto = BCProto(params=params, vararg=vararg)
        proto_index = len(self.protos)
        self.protos.append(proto)
        self.compiled_functions += 1
        scope = _Scope(captured_scope)
        ctx = _FunctionContext(proto, scope)
        for name in params:
            if not name:
                raise _unsupported("empty function parameter", node)
            scope.names.add(name)
            self.local_bindings += 1
        self._compile_body(ctx, body)
        if not proto.code or proto.code[-1].op not in {"RETURN", "RETURN_CALL", "RETURN_VARARG", "RETURN_STACK", "HALT"}:
            proto.code.append(BCInstr("RETURN", 0))
        proto.max_regs = ctx.max_reg
        return proto_index

    def _function_name_parts(self, node: object) -> tuple[object, list[str], bool]:
        """Normalize function foo.bar:baz() -> (base expression, keys, method)."""
        typ = getattr(node, "type", "")
        if typ == "identifier":
            return node, [], False
        if typ == "dot_index_expression":
            table = _field(node, "table")
            field = _field(node, "field")
            if table is None or field is None:
                raise _unsupported("malformed function name", node)
            base, keys, method = self._function_name_parts(table)
            return base, [*keys, _ident_name(field, self.source)], method
        if typ == "method_index_expression":
            table = _field(node, "table")
            method_node = _field(node, "method")
            if table is None or method_node is None:
                raise _unsupported("malformed method function name", node)
            base, keys, _ = self._function_name_parts(table)
            return base, [*keys, _ident_name(method_node, self.source)], True
        # Some parsers may expose the outer prefix as a `variable` wrapper.
        if typ == "variable":
            children = _named_children(node)
            if len(children) == 1:
                return self._function_name_parts(children[0])
        raise _unsupported(f"unsupported function declaration name '{typ}'", node)

    def _compile_function_declaration(self, ctx: _FunctionContext, node: object) -> None:
        name_node = _field(node, "name")
        if name_node is None:
            raise _unsupported("function declaration has no name", node)
        text = _text(node, self.source).lstrip()
        local_declare = text.startswith("local function")
        base, keys, method = self._function_name_parts(name_node)

        if not keys and getattr(base, "type", "") == "identifier" and local_declare:
            local_name = _ident_name(base, self.source)
            if ctx.scope.depth(local_name) is None:
                ctx.scope.names.add(local_name)
                self.local_bindings += 1

        proto_index = self._compile_nested_function(node, ctx.scope, method_self=method)
        closure_reg = ctx.alloc()
        ctx.proto.code.append(BCInstr("CLOSURE", a=closure_reg, b=proto_index))

        if not keys:
            name = _ident_name(base, self.source)
            if local_declare:
                depth = 0
            else:
                depth = ctx.scope.depth(name)
            if depth is None:
                ctx.proto.code.append(BCInstr("STORE_GLOBAL", a=closure_reg, b=self.const(name)))
            else:
                ctx.proto.code.append(BCInstr("STORE_VAR", a=closure_reg, b=depth, c=self.const(name)))
            return

        table_reg = self._compile_expr(ctx, base)
        # Any additional dot/method keys are plain table writes; the method flag
        # only affects the implicit `self` parameter of the closure.
        for key in keys[:-1]:
            kreg = ctx.alloc()
            ctx.proto.code.append(BCInstr("LOAD_CONST", a=kreg, b=self.const(key)))
            next_table = ctx.alloc()
            ctx.proto.code.append(BCInstr("GET_INDEX", a=next_table, b=table_reg, c=kreg))
            table_reg = next_table
        final_key = ctx.alloc()
        ctx.proto.code.append(BCInstr("LOAD_CONST", a=final_key, b=self.const(keys[-1])))
        ctx.proto.code.append(BCInstr("SET_INDEX", a=table_reg, b=final_key, c=closure_reg))

    # -------------------------------- return --------------------------------
    def _compile_return(self, ctx: _FunctionContext, node: object) -> None:
        values = self._expr_items(self._find_expression_list(node))
        if not values:
            ctx.proto.code.append(BCInstr("RETURN", 0))
            return
        if len(values) == 1:
            value = values[0]
            if _is_call(value):
                self._compile_call(ctx, value, discard=False, leave_results=True)
                ctx.proto.code.append(BCInstr("RETURN_CALL"))
                return
            if _is_vararg(value):
                ctx.proto.code.append(BCInstr("RETURN_VARARG"))
                return
            reg = self._compile_expr(ctx, value)
            ctx.proto.code.append(BCInstr("RETURN", a=1, b=reg))
            return
        for value in values[:-1]:
            reg = self._emit_single_value(ctx, value, discard=False)
            ctx.proto.code.append(BCInstr("PUSH_REG", a=reg))
        last = values[-1]
        if _is_call(last):
            self._compile_call(ctx, last, discard=False, leave_results=True)
            ctx.proto.code.append(BCInstr("EXPAND_RESULT"))
        elif _is_vararg(last):
            ctx.proto.code.append(BCInstr("PUSH_VARARG"))
        else:
            reg = self._compile_expr(ctx, last)
            ctx.proto.code.append(BCInstr("PUSH_REG", a=reg))
        ctx.proto.code.append(BCInstr("RETURN_STACK"))

    # ---------------------------------- if ---------------------------------
    def _compile_if(self, ctx: _FunctionContext, node: object) -> None:
        cond = _field(node, "condition")
        consequence = _field(node, "consequence")
        if cond is None or consequence is None:
            raise _unsupported("malformed if statement", node)

        false_patch = -1
        end_jumps: list[int] = []
        cond_reg = self._compile_expr(ctx, cond)
        false_patch = len(ctx.proto.code)
        ctx.proto.code.append(BCInstr("JUMP_IF_FALSE", a=cond_reg, b=0))
        self._compile_scoped_body(ctx, consequence)
        end_jumps.append(len(ctx.proto.code))
        ctx.proto.code.append(BCInstr("JUMP", a=0))

        alternatives = [c for c in _named_children(node) if getattr(c, "type", "") in {"elseif_statement", "else_statement"}]
        for alt in alternatives:
            target = len(ctx.proto.code)
            ctx.proto.code[false_patch] = BCInstr("JUMP_IF_FALSE", a=ctx.proto.code[false_patch].a, b=target)
            if getattr(alt, "type", "") == "elseif_statement":
                acond = _field(alt, "condition")
                abody = _field(alt, "consequence")
                if acond is None or abody is None:
                    raise _unsupported("malformed elseif branch", alt)
                areg = self._compile_expr(ctx, acond)
                false_patch = len(ctx.proto.code)
                ctx.proto.code.append(BCInstr("JUMP_IF_FALSE", a=areg, b=0))
                self._compile_scoped_body(ctx, abody)
                end_jumps.append(len(ctx.proto.code))
                ctx.proto.code.append(BCInstr("JUMP", a=0))
            else:
                body = _field(alt, "body")
                self._compile_scoped_body(ctx, body)
                false_patch = -1
                break

        end = len(ctx.proto.code)
        if false_patch >= 0:
            ctx.proto.code[false_patch] = BCInstr("JUMP_IF_FALSE", a=ctx.proto.code[false_patch].a, b=end)
        for idx in end_jumps:
            ctx.proto.code[idx] = BCInstr("JUMP", a=end)

    def _compile_scoped_body(self, ctx: _FunctionContext, body: object | None) -> None:
        ctx.proto.code.append(BCInstr("ENTER_SCOPE"))
        ctx.scope_depth += 1
        old_scope = ctx.scope
        ctx.scope = _Scope(old_scope)
        if body is not None:
            self._compile_body(ctx, body)
        ctx.scope = old_scope
        ctx.proto.code.append(BCInstr("LEAVE_SCOPE"))
        ctx.scope_depth -= 1

    # -------------------------------- loops --------------------------------
    def _compile_do(self, ctx: _FunctionContext, node: object) -> None:
        self._compile_scoped_body(ctx, _field(node, "body"))

    def _compile_break(self, ctx: _FunctionContext, node: object) -> None:
        if not ctx.loop_stack:
            raise _unsupported("break outside of a loop", node)
        break_sites, _, loop_depth = ctx.loop_stack[-1]
        # Leave nested block scopes, but not the loop scope itself; the common
        # loop cleanup label performs that final pop.
        for _ in range(max(0, ctx.scope_depth - loop_depth)):
            ctx.proto.code.append(BCInstr("LEAVE_SCOPE"))
        site = len(ctx.proto.code)
        ctx.proto.code.append(BCInstr("JUMP", a=0))
        break_sites.append(site)

    def _compile_continue(self, ctx: _FunctionContext, node: object) -> None:
        if not ctx.loop_stack:
            raise _unsupported("continue outside of a loop", node)
        _, continue_pc, loop_depth = ctx.loop_stack[-1]
        for _ in range(max(0, ctx.scope_depth - loop_depth)):
            ctx.proto.code.append(BCInstr("LEAVE_SCOPE"))
        ctx.proto.code.append(BCInstr("JUMP", a=continue_pc))

    def _compile_while(self, ctx: _FunctionContext, node: object) -> None:
        cond = _field(node, "condition")
        body = _field(node, "body")
        if cond is None or body is None:
            raise _unsupported("malformed while loop", node)
        loop_start = len(ctx.proto.code)
        cond_reg = self._compile_expr(ctx, cond)
        exit_idx = len(ctx.proto.code)
        ctx.proto.code.append(BCInstr("JUMP_IF_FALSE", a=cond_reg, b=0))
        ctx.proto.code.append(BCInstr("ENTER_SCOPE"))
        ctx.scope_depth += 1
        old_scope = ctx.scope
        ctx.scope = _Scope(old_scope)
        breaks: list[int] = []
        ctx.loop_stack.append((breaks, loop_start, ctx.scope_depth))
        self._compile_body(ctx, body)
        ctx.loop_stack.pop()
        ctx.scope = old_scope
        ctx.proto.code.append(BCInstr("LEAVE_SCOPE"))
        ctx.scope_depth -= 1
        ctx.proto.code.append(BCInstr("JUMP", a=loop_start))
        cleanup = len(ctx.proto.code)
        ctx.proto.code[exit_idx] = BCInstr("JUMP_IF_FALSE", a=cond_reg, b=cleanup)
        for site in breaks:
            ctx.proto.code[site] = BCInstr("JUMP", a=cleanup)

    def _compile_repeat(self, ctx: _FunctionContext, node: object) -> None:
        body = _field(node, "body")
        cond = _field(node, "condition")
        if body is None or cond is None:
            raise _unsupported("malformed repeat loop", node)
        loop_start = len(ctx.proto.code)
        ctx.proto.code.append(BCInstr("ENTER_SCOPE"))
        ctx.scope_depth += 1
        old_scope = ctx.scope
        ctx.scope = _Scope(old_scope)
        condition_anchor = None
        breaks: list[int] = []
        ctx.loop_stack.append((breaks, -1, ctx.scope_depth))
        self._compile_body(ctx, body)
        condition_anchor = len(ctx.proto.code)
        # Continue jumps to condition evaluation, while repeat itself jumps to
        # the body start when false.
        ctx.loop_stack[-1] = (breaks, condition_anchor, ctx.scope_depth)
        cond_reg = self._compile_expr(ctx, cond)
        exit_idx = len(ctx.proto.code)
        ctx.proto.code.append(BCInstr("JUMP_IF_TRUE", a=cond_reg, b=0))
        ctx.proto.code.append(BCInstr("JUMP", a=loop_start))
        cleanup = len(ctx.proto.code)
        ctx.proto.code[exit_idx] = BCInstr("JUMP_IF_TRUE", a=cond_reg, b=cleanup)
        for site in breaks:
            ctx.proto.code[site] = BCInstr("JUMP", a=cleanup)
        ctx.loop_stack.pop()
        ctx.scope = old_scope
        ctx.proto.code.append(BCInstr("LEAVE_SCOPE"))
        ctx.scope_depth -= 1

    def _compile_for(self, ctx: _FunctionContext, node: object) -> None:
        clause = _field(node, "clause")
        body = _field(node, "body")
        if clause is None or body is None:
            raise _unsupported("malformed for loop", node)
        ctype = getattr(clause, "type", "")
        if ctype == "for_numeric_clause":
            name_node = _field(clause, "name")
            start = _field(clause, "start")
            end = _field(clause, "end")
            step = _field(clause, "step")
            if name_node is None or start is None or end is None:
                raise _unsupported("malformed numeric for clause", clause)
            name = _ident_name(name_node, self.source)
            rcontrol = self._compile_expr(ctx, start)
            rlimit = self._compile_expr(ctx, end)
            rstep = self._compile_expr(ctx, step) if step is not None else ctx.alloc()
            if step is None:
                ctx.proto.code.append(BCInstr("LOAD_CONST", a=rstep, b=self.const(1)))

            ctx.proto.code.append(BCInstr("ENTER_SCOPE"))
            ctx.scope_depth += 1
            old_scope = ctx.scope
            ctx.scope = _Scope(old_scope)
            ctx.scope.names.add(name)
            self.local_bindings += 1

            loop_check = len(ctx.proto.code)
            exit_idx = len(ctx.proto.code)
            ctx.proto.code.append(BCInstr("FOR_CHECK", a=rcontrol, b=rlimit, c=rstep, d=0))
            breaks: list[int] = []
            ctx.loop_stack.append((breaks, loop_check, ctx.scope_depth))
            ctx.proto.code.append(BCInstr("STORE_VAR", a=rcontrol, b=0, c=self.const(name)))
            self._compile_body(ctx, body)
            ctx.loop_stack.pop()
            next_value = ctx.alloc()
            ctx.proto.code.append(BCInstr("BIN", a=next_value, b=rcontrol, c=rstep, d=_OPERATOR_CODES["+"]))
            ctx.proto.code.append(BCInstr("MOVE", a=rcontrol, b=next_value))
            ctx.proto.code.append(BCInstr("JUMP", a=loop_check))
            cleanup = len(ctx.proto.code)
            ctx.proto.code[exit_idx] = BCInstr("FOR_CHECK", a=rcontrol, b=rlimit, c=rstep, d=cleanup)
            for site in breaks:
                ctx.proto.code[site] = BCInstr("JUMP", a=cleanup)
            ctx.scope = old_scope
            ctx.proto.code.append(BCInstr("LEAVE_SCOPE"))
            ctx.scope_depth -= 1
            return

        if ctype == "for_generic_clause":
            variables_node = self._find_variable_list(clause)
            expr_node = self._find_expression_list(clause)
            if variables_node is None or expr_node is None:
                raise _unsupported("malformed generic for clause", clause)
            names = self._binding_names(variables_node)
            if not names:
                raise _unsupported("generic for has no bindings", clause)
            exprs = self._expr_items(expr_node)
            if not exprs:
                raise _unsupported("generic for has no iterator expression", clause)

            iterator_regs: list[int] = []
            for index, value in enumerate(exprs):
                remaining = 3 - len(iterator_regs)
                if remaining <= 0:
                    self._emit_single_value(ctx, value, discard=True)
                    continue
                if index == len(exprs) - 1 and (_is_call(value) or _is_vararg(value)):
                    if _is_call(value):
                        self._compile_call(ctx, value, discard=False, leave_results=True)
                        regs = [ctx.alloc() for _ in range(remaining)]
                        ctx.proto.code.append(BCInstr("POP_RESULTS", a=regs[0], b=remaining))
                    else:
                        regs = [ctx.alloc() for _ in range(remaining)]
                        ctx.proto.code.append(BCInstr("UNPACK_VARARG", a=regs[0], b=remaining))
                    iterator_regs.extend(regs)
                else:
                    iterator_regs.append(self._emit_single_value(ctx, value, discard=False))
            while len(iterator_regs) < 3:
                r = ctx.alloc()
                ctx.proto.code.append(BCInstr("LOAD_CONST", a=r, b=self.const(None)))
                iterator_regs.append(r)

            ctx.proto.code.append(BCInstr("ENTER_SCOPE"))
            ctx.scope_depth += 1
            old_scope = ctx.scope
            ctx.scope = _Scope(old_scope)
            for name in names:
                ctx.scope.names.add(name)
                self.local_bindings += 1
            loop_check = len(ctx.proto.code)
            exit_idx = None
            breaks: list[int] = []
            # Constant tuple gives ITER_NEXT the exact loop variable names.
            names_const = self.const(tuple(names))
            exit_idx = len(ctx.proto.code)
            ctx.proto.code.append(BCInstr(
                "ITER_NEXT",
                a=iterator_regs[0], b=iterator_regs[1], c=iterator_regs[2], d=names_const, e=0,
            ))
            ctx.loop_stack.append((breaks, loop_check, ctx.scope_depth))
            self._compile_body(ctx, body)
            ctx.loop_stack.pop()
            ctx.proto.code.append(BCInstr("JUMP", a=loop_check))
            cleanup = len(ctx.proto.code)
            ctx.proto.code[exit_idx] = BCInstr(
                "ITER_NEXT",
                a=iterator_regs[0], b=iterator_regs[1], c=iterator_regs[2], d=names_const, e=cleanup,
            )
            for site in breaks:
                ctx.proto.code[site] = BCInstr("JUMP", a=cleanup)
            ctx.scope = old_scope
            ctx.proto.code.append(BCInstr("LEAVE_SCOPE"))
            ctx.scope_depth -= 1
            return

        raise _unsupported(f"unsupported for clause '{ctype}'", clause)

    # -------------------------------- expressions ---------------------------
    def _compile_expr(self, ctx: _FunctionContext, node: object) -> int:
        typ = getattr(node, "type", "")
        if typ in {"expression", "primary_expression"}:
            children = [c for c in _named_children(node) if not _is_type(c)]
            if len(children) == 1:
                return self._compile_expr(ctx, children[0])
            raise _unsupported(f"malformed expression wrapper '{typ}'", node)
        if typ == "parenthesized_expression":
            children = [c for c in _named_children(node) if not _is_type(c)]
            if not children:
                raise _unsupported("empty parenthesized expression", node)
            return self._compile_expr(ctx, children[0])
        if typ == "cast_expression":
            expr = _field(node, "expression")
            if expr is None:
                expr = next((c for c in _named_children(node) if not _is_type(c)), None)
            if expr is None:
                raise _unsupported("malformed cast expression", node)
            return self._compile_expr(ctx, expr)
        if typ == "number":
            reg = ctx.alloc()
            ctx.proto.code.append(BCInstr("LOAD_CONST", a=reg, b=self.const(_number_value(_text(node, self.source)))))
            return reg
        if typ == "string":
            return self._compile_string(ctx, node)
        if typ in {"true", "false"}:
            reg = ctx.alloc(); ctx.proto.code.append(BCInstr("LOAD_CONST", a=reg, b=self.const(typ == "true"))); return reg
        if typ == "nil":
            reg = ctx.alloc(); ctx.proto.code.append(BCInstr("LOAD_CONST", a=reg, b=self.const(None))); return reg
        if typ == "vararg_expression":
            reg = ctx.alloc(); ctx.proto.code.append(BCInstr("LOAD_VARARG", a=reg)); return reg
        if typ == "identifier":
            return self._load_name(ctx, _ident_name(node, self.source))
        if typ == "variable":
            children = _named_children(node)
            if len(children) == 1 and getattr(children[0], "type", "") in {"identifier", "dot_index_expression", "bracket_index_expression"}:
                return self._compile_expr(ctx, children[0])
            name = _ident_name(node, self.source)
            if name.isidentifier():
                return self._load_name(ctx, name)
            raise _unsupported("malformed variable expression", node)
        if typ in {"dot_index_expression", "bracket_index_expression"}:
            table = _field(node, "table")
            field = _field(node, "field")
            if table is None or field is None:
                children = _named_children(node)
                table = table or (children[0] if children else None)
                field = field or (children[1] if len(children) > 1 else None)
            if table is None or field is None:
                raise _unsupported("malformed member/index expression", node)
            table_reg = self._compile_expr(ctx, table)
            if typ == "dot_index_expression":
                key_reg = ctx.alloc()
                ctx.proto.code.append(BCInstr("LOAD_CONST", a=key_reg, b=self.const(_ident_name(field, self.source))))
            else:
                key_reg = self._compile_expr(ctx, field)
            dest = ctx.alloc()
            ctx.proto.code.append(BCInstr("GET_INDEX", a=dest, b=table_reg, c=key_reg))
            return dest
        if typ == "method_index_expression":
            raise _unsupported("bare method reference is not a valid value", node)
        if typ == "binary_expression":
            left = _field(node, "left")
            right = _field(node, "right")
            if left is None or right is None:
                children = _named_children(node)
                if len(children) >= 2:
                    left, right = children[0], children[-1]
            if left is None or right is None:
                raise _unsupported("binary expression is missing an operand", node)
            op = _operator_between(node, left, right, self.source)
            if op not in _OPERATOR_CODES:
                raise _unsupported(f"unsupported binary operator '{op}'", node)
            lreg = self._compile_expr(ctx, left)
            if op == "and":
                false_idx = len(ctx.proto.code)
                ctx.proto.code.append(BCInstr("JUMP_IF_FALSE", a=lreg, b=0))
                rreg = self._compile_expr(ctx, right)
                ctx.proto.code.append(BCInstr("MOVE", a=lreg, b=rreg))
                ctx.proto.code[false_idx] = BCInstr("JUMP_IF_FALSE", a=lreg, b=len(ctx.proto.code))
                return lreg
            if op == "or":
                true_idx = len(ctx.proto.code)
                ctx.proto.code.append(BCInstr("JUMP_IF_TRUE", a=lreg, b=0))
                rreg = self._compile_expr(ctx, right)
                ctx.proto.code.append(BCInstr("MOVE", a=lreg, b=rreg))
                ctx.proto.code[true_idx] = BCInstr("JUMP_IF_TRUE", a=lreg, b=len(ctx.proto.code))
                return lreg
            rreg = self._compile_expr(ctx, right)
            dest = ctx.alloc()
            ctx.proto.code.append(BCInstr("BIN", a=dest, b=lreg, c=rreg, d=_OPERATOR_CODES[op]))
            return dest
        if typ == "unary_expression":
            operand = _field(node, "operand")
            if operand is None:
                children = [c for c in _named_children(node) if not _is_type(c)]
                operand = children[-1] if children else None
            if operand is None:
                raise _unsupported("unary expression is missing an operand", node)
            op = _unary_operator(node, operand, self.source)
            if op not in _UNARY_CODES:
                raise _unsupported(f"unsupported unary operator '{op}'", node)
            src = self._compile_expr(ctx, operand)
            dest = ctx.alloc()
            ctx.proto.code.append(BCInstr("UNARY", a=dest, b=src, c=_UNARY_CODES[op]))
            return dest
        if typ == "function_call":
            return self._compile_call(ctx, node, discard=False)
        if typ == "function_definition":
            proto_index = self._compile_nested_function(node, ctx.scope)
            dest = ctx.alloc()
            ctx.proto.code.append(BCInstr("CLOSURE", a=dest, b=proto_index))
            return dest
        if typ == "table_constructor":
            return self._compile_table(ctx, node)
        if typ == "if_expression":
            return self._compile_if_expression(ctx, node)
        raise _unsupported(f"unsupported expression node '{typ}'", node)

    def _load_name(self, ctx: _FunctionContext, name: str) -> int:
        dest = ctx.alloc()
        depth = ctx.scope.depth(name)
        if depth is None:
            ctx.proto.code.append(BCInstr("LOAD_GLOBAL", a=dest, b=self.const(name)))
        else:
            ctx.proto.code.append(BCInstr("LOAD_VAR", a=dest, b=depth, c=self.const(name)))
        return dest

    def _compile_string(self, ctx: _FunctionContext, node: object) -> int:
        raw = _text(node, self.source)
        if raw.startswith("`"):
            children = _named_children(node)
            if not any(getattr(c, "type", "") == "interpolation" for c in children):
                # Backtick strings without interpolation are ordinary string
                # values after escapes are interpreted.
                try:
                    value = _decode_lua_string(("\"" + raw[1:-1].replace('"', '\\"') + "\"").encode("utf-8"))
                except Exception:
                    value = raw[1:-1]
                reg = ctx.alloc(); ctx.proto.code.append(BCInstr("LOAD_CONST", a=reg, b=self.const(value))); return reg
            result = ctx.alloc()
            ctx.proto.code.append(BCInstr("LOAD_CONST", a=result, b=self.const("")))
            for child in children:
                typ = getattr(child, "type", "")
                if typ == "string_content":
                    if _text(child, self.source):
                        part = ctx.alloc(); ctx.proto.code.append(BCInstr("LOAD_CONST", a=part, b=self.const(_text(child, self.source))))
                    else:
                        continue
                elif typ == "escape_sequence":
                    esc = _text(child, self.source)
                    try:
                        part_value = _decode_lua_string(("\"" + esc + "\"").encode("utf-8"))
                    except Exception:
                        part_value = esc[1:] if esc.startswith("\\") else esc
                    part = ctx.alloc(); ctx.proto.code.append(BCInstr("LOAD_CONST", a=part, b=self.const(part_value)))
                elif typ == "interpolation":
                    expr = next((c for c in _named_children(child) if not _is_type(c)), None)
                    if expr is None:
                        raise _unsupported("interpolation has no expression", child)
                    part = self._compile_expr(ctx, expr)
                else:
                    continue
                merged = ctx.alloc()
                ctx.proto.code.append(BCInstr("BIN", a=merged, b=result, c=part, d=_OPERATOR_CODES[".."]))
                result = merged
            return result
        if raw.startswith("["):
            # Long strings may contain = padding: [=[...]=].
            first = raw.find("[")
            second = raw.find("[", first + 1)
            if second >= 0:
                pad = raw[first + 1 : second]
                end_marker = "]" + pad + "]"
                if raw.endswith(end_marker):
                    value = raw[second + 1 : -len(end_marker)]
                    reg = ctx.alloc(); ctx.proto.code.append(BCInstr("LOAD_CONST", a=reg, b=self.const(value))); return reg
        try:
            value = _decode_lua_string(raw.encode("utf-8"))
        except Exception as exc:
            raise _unsupported("string literal could not be decoded", node) from exc
        reg = ctx.alloc(); ctx.proto.code.append(BCInstr("LOAD_CONST", a=reg, b=self.const(value))); return reg

    def _compile_table(self, ctx: _FunctionContext, node: object) -> int:
        dest = ctx.alloc()
        ctx.proto.code.append(BCInstr("NEW_TABLE", a=dest))
        next_index = 1
        fields = [c for c in _named_children(node) if getattr(c, "type", "") == "field"]
        for field in fields:
            name_node = _field(field, "name")
            value_node = _field(field, "value")
            if value_node is None:
                children = _named_children(field)
                value_node = children[-1] if children else None
            if value_node is None:
                raise _unsupported("table field has no value", field)
            if name_node is None:
                key_reg = ctx.alloc()
                ctx.proto.code.append(BCInstr("LOAD_CONST", a=key_reg, b=self.const(next_index)))
                next_index += 1
            elif getattr(name_node, "type", "") == "identifier":
                key_reg = ctx.alloc()
                ctx.proto.code.append(BCInstr("LOAD_CONST", a=key_reg, b=self.const(_ident_name(name_node, self.source))))
            else:
                key_reg = self._compile_expr(ctx, name_node)
            value_reg = self._compile_expr(ctx, value_node)
            ctx.proto.code.append(BCInstr("SET_INDEX", a=dest, b=key_reg, c=value_reg))
        return dest

    def _compile_if_expression(self, ctx: _FunctionContext, node: object) -> int:
        condition = _field(node, "condition")
        consequence = _field(node, "consequence")
        if condition is None or consequence is None:
            raise _unsupported("malformed if expression", node)
        result = ctx.alloc()
        ctx.proto.code.append(BCInstr("LOAD_CONST", a=result, b=self.const(None)))
        cond_reg = self._compile_expr(ctx, condition)
        false_site = len(ctx.proto.code)
        ctx.proto.code.append(BCInstr("JUMP_IF_FALSE", a=cond_reg, b=0))
        cons = self._compile_expr(ctx, consequence)
        ctx.proto.code.append(BCInstr("MOVE", a=result, b=cons))
        end_jumps = [len(ctx.proto.code)]
        ctx.proto.code.append(BCInstr("JUMP", a=0))

        next_false = false_site
        for alt in [c for c in _named_children(node) if getattr(c, "type", "") == "elseif_clause"]:
            ctx.proto.code[next_false] = BCInstr("JUMP_IF_FALSE", a=ctx.proto.code[next_false].a, b=len(ctx.proto.code))
            acond = _field(alt, "condition")
            acons = _field(alt, "consequence")
            if acond is None or acons is None:
                raise _unsupported("malformed elseif expression", alt)
            ar = self._compile_expr(ctx, acond)
            next_false = len(ctx.proto.code)
            ctx.proto.code.append(BCInstr("JUMP_IF_FALSE", a=ar, b=0))
            rr = self._compile_expr(ctx, acons)
            ctx.proto.code.append(BCInstr("MOVE", a=result, b=rr))
            end_jumps.append(len(ctx.proto.code))
            ctx.proto.code.append(BCInstr("JUMP", a=0))

        else_clause = next((c for c in _named_children(node) if getattr(c, "type", "") == "else_clause"), None)
        if else_clause is not None:
            ctx.proto.code[next_false] = BCInstr("JUMP_IF_FALSE", a=ctx.proto.code[next_false].a, b=len(ctx.proto.code))
            er = _field(else_clause, "consequence")
            if er is None:
                raise _unsupported("malformed else expression", else_clause)
            rr = self._compile_expr(ctx, er)
            ctx.proto.code.append(BCInstr("MOVE", a=result, b=rr))
            next_false = -1

        end = len(ctx.proto.code)
        if next_false >= 0:
            ctx.proto.code[next_false] = BCInstr("JUMP_IF_FALSE", a=ctx.proto.code[next_false].a, b=end)
        for site in end_jumps:
            ctx.proto.code[site] = BCInstr("JUMP", a=end)
        return result

    # ---------------------------------- call --------------------------------
    def _compile_call(
        self,
        ctx: _FunctionContext,
        node: object,
        *,
        discard: bool,
        leave_results: bool = False,
    ) -> int:
        name_node = _field(node, "name") or _field(node, "function")
        args_node = _field(node, "arguments") or _field(node, "argument_list")
        if name_node is None or args_node is None:
            children = _named_children(node)
            args_node = args_node or next((c for c in children if getattr(c, "type", "") in {"arguments", "argument_list", "table_constructor", "string"}), None)
            if name_node is None:
                name_node = next((c for c in children if c is not args_node), None)
        if name_node is None or args_node is None:
            raise _unsupported("function call is missing callee or arguments", node)

        if getattr(args_node, "type", "") in {"arguments", "argument_list"}:
            args = _named_children(args_node)
        else:
            args = [args_node]
        args = [c for c in args if not _is_type(c) and getattr(c, "type", "") not in {"comment", ","}]
        method = getattr(name_node, "type", "") == "method_index_expression"

        final_expands = bool(args and (_is_call(args[-1]) or _is_vararg(args[-1])))
        if final_expands:
            # The call frame starts with a private marker. This is essential for
            # Lua's multiple-return adjustment rules: the final call/vararg can
            # contribute any number of arguments, including nil values.
            ctx.proto.code.append(BCInstr("MARK_CALL"))

        if method:
            obj = _field(name_node, "table")
            method_node = _field(name_node, "method")
            if obj is None or method_node is None:
                raise _unsupported("malformed method call", name_node)
            obj_reg = self._compile_expr(ctx, obj)
            key_reg = ctx.alloc()
            ctx.proto.code.append(BCInstr("LOAD_CONST", a=key_reg, b=self.const(_ident_name(method_node, self.source))))
            fn_reg = ctx.alloc()
            ctx.proto.code.append(BCInstr("GET_INDEX", a=fn_reg, b=obj_reg, c=key_reg))
            ctx.proto.code.append(BCInstr("PUSH_REG", a=fn_reg))
            ctx.proto.code.append(BCInstr("PUSH_REG", a=obj_reg))
        else:
            fn_reg = self._compile_expr(ctx, name_node)
            ctx.proto.code.append(BCInstr("PUSH_REG", a=fn_reg))

        fixed_arg_count = 1 if method else 0
        for index, arg in enumerate(args):
            last = index == len(args) - 1
            if last and (_is_call(arg) or _is_vararg(arg)):
                if _is_call(arg):
                    self._compile_call(ctx, arg, discard=False, leave_results=True)
                    ctx.proto.code.append(BCInstr("EXPAND_RESULT"))
                else:
                    ctx.proto.code.append(BCInstr("PUSH_VARARG"))
                ctx.proto.code.append(BCInstr("CALL_DYNAMIC", a=fixed_arg_count))
                if discard:
                    ctx.proto.code.append(BCInstr("DROP_RESULT"))
                    return 0
                if leave_results:
                    return 0
                dest = ctx.alloc()
                ctx.proto.code.append(BCInstr("POP_RESULT", a=dest))
                return dest
            reg = self._compile_expr(ctx, arg)
            ctx.proto.code.append(BCInstr("PUSH_REG", a=reg))
            fixed_arg_count += 1

        ctx.proto.code.append(BCInstr("CALL", a=fixed_arg_count))
        if discard:
            ctx.proto.code.append(BCInstr("DROP_RESULT"))
            return 0
        if leave_results:
            return 0
        dest = ctx.alloc()
        ctx.proto.code.append(BCInstr("POP_RESULT", a=dest))
        return dest

    def _emit_single_value(self, ctx: _FunctionContext, node: object, *, discard: bool) -> int:
        if _is_call(node):
            if discard:
                self._compile_call(ctx, node, discard=True)
                return -1
            return self._compile_call(ctx, node, discard=False)
        if _is_vararg(node):
            reg = ctx.alloc(); ctx.proto.code.append(BCInstr("LOAD_VARARG", a=reg))
            if discard:
                ctx.proto.code.append(BCInstr("DROP", a=reg)); return -1
            return reg
        reg = self._compile_expr(ctx, node)
        if discard:
            ctx.proto.code.append(BCInstr("DROP", a=reg)); return -1
        return reg

"""Compare compiled functions against execution of the unmodified test suite."""

import ast
import bisect
import dis
import hashlib
import struct
import sys
import types
from functools import lru_cache


def scope_name(code):
    return f"{code.co_firstlineno}:{code.co_qualname}"


def constant(value):
    if isinstance(value, types.CodeType):
        return ("code", value.co_name)
    # marshal includes reference/interning flags that can differ when the same
    # literal is compiled from source text and from a reconstructed AST.
    if type(value) is tuple:
        return (tuple, tuple(constant(item) for item in value))
    if type(value) is frozenset:
        return (frozenset, frozenset(constant(item) for item in value))
    if type(value) is float:
        return (float, struct.pack(">d", value))
    if type(value) is complex:
        return (complex, struct.pack(">dd", value.real, value.imag))
    return (type(value), value)


def structure(code):
    return (code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount, code.co_flags,
            code.co_varnames, code.co_freevars, code.co_cellvars)


def signature(code):
    return (structure(code), code.co_code, code.co_names, code.co_exceptiontable,
            tuple(constant(value) for value in code.co_consts))


def bytecode_key(code):
    # Per-offset data only applies when this interpreter generated the measured bytecode.
    return hashlib.sha256(code.co_code + code.co_exceptiontable).hexdigest()[:20]


def block_starts(code):
    ins = list(dis.get_instructions(code))
    starts = {ins[0].offset}
    for i, op in enumerate(ins):
        if op.is_jump_target:
            starts.add(op.offset)
        if op.opcode in dis.hasjump:
            starts.add(op.argval)
            if i + 1 < len(ins):
                starts.add(ins[i + 1].offset)
    for entry in dis.Bytecode(code).exception_entries:
        starts.update((entry.start, entry.end, entry.target))
    return sorted(starts)


def instructions(code):
    ins = list(dis.get_instructions(code))
    positions = {op.offset: i for i, op in enumerate(ins)}
    result = []
    for op in ins:
        if op.opcode in dis.hasjump:
            value = positions.get(op.argval, -1)
        elif op.opcode in dis.hasconst:
            value = constant(op.argval)
        else:
            value = (op.argval, op.argrepr)
        result.append((op.opname, value))
    return ins, result


class ScopeChange(Exception):
    """A lexical change can affect a call before the edited statement runs."""


def changes(old, new):
    """List changed deferred code, or reject a change to import/class execution."""
    children_old = [c for c in old.co_consts if isinstance(c, types.CodeType)]
    children_new = [c for c in new.co_consts if isinstance(c, types.CodeType)]
    if len(children_old) != len(children_new):
        raise ScopeChange("changed definition layout")
    changed = []
    if signature(old) != signature(new):
        # Class and module bodies run eagerly. A function's signature and docstring
        # are also observable without calling it.
        if structure(old) != structure(new):
            raise ScopeChange("changed lexical bindings or function flags")
        if not old.co_flags & 1:
            raise ValueError("changed eager code")
        if old.co_consts and new.co_consts and old.co_consts[0] != new.co_consts[0]:
            if isinstance(old.co_consts[0], str) or isinstance(new.co_consts[0], str):
                raise ScopeChange("possible docstring change")
        changed.append((old, new))
    for before, after in zip(children_old, children_new):
        if before.co_qualname != after.co_qualname:
            raise ScopeChange("changed function name")
        changed.extend(changes(before, after))
    return changed


def immutable_shape(before, after):
    """Keep the exact built-in type and nesting of a loaded constant."""
    if type(before) is not type(after):
        return False
    if type(before) in (bool, int, float, str, bytes, type(None)):
        return True
    if type(before) is tuple:
        return len(before) == len(after) and all(immutable_shape(a, b) for a, b in zip(before, after))
    if type(before) is frozenset:
        # No iteration order is defined. Restrict sets to homogeneous primitive keys.
        if not before or len(before) != len(after):
            return False
        key_type = type(next(iter(before)))
        return (key_type in (bool, int, str, bytes)
                and all(type(x) is key_type for items in (before, after) for x in items))
    return False


def literal_members(before, after):
    """Allow a membership test to gain or lose homogeneous built-in keys."""
    if type(before) not in (tuple, frozenset) or type(before) is not type(after):
        return False
    items = (*before, *after)
    if not items:
        return False
    key = type(items[0])
    return key in (str, int, bytes, bool) and all(type(item) is key for item in items)


def scalar_replacements(old, new):
    """Recognize local substitutions with the same calls, bindings and stores."""
    if old.co_names != new.co_names or old.co_exceptiontable != new.co_exceptiontable:
        return False
    old_ops, before = instructions(old)
    new_ops, after = instructions(new)
    if len(before) != len(after):
        return False
    truth_jumps = {"POP_JUMP_IF_TRUE", "POP_JUMP_IF_FALSE"}
    for i, (left, right) in enumerate(zip(before, after)):
        if left == right:
            continue
        a, b = old_ops[i], new_ops[i]
        # Both conditional jumps pop the same value; the original branch is
        # the earliest place at which a changed edge can affect execution.
        if a.opname in truth_jumps and b.opname in truth_jumps:
            continue
        if a.opname != b.opname:
            return False
        if a.opname in ("COMPARE_OP", "IS_OP", "CONTAINS_OP", "BINARY_OP"):
            continue
        if a.opname not in ("LOAD_CONST", "RETURN_CONST"):
            return False
        if not immutable_shape(a.argval, b.argval):
            if (i + 1 >= len(before) or old_ops[i + 1].opname != "CONTAINS_OP"
                    or new_ops[i + 1].opname != "CONTAINS_OP" or not literal_members(a.argval, b.argval)):
                return False
    return True


def local_condition(node, local_names):
    if isinstance(node, ast.Name):
        return isinstance(node.ctx, ast.Load) and node.id in local_names
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return local_condition(node.operand, local_names)
    if isinstance(node, ast.BoolOp):
        return all(local_condition(value, local_names) for value in node.values)
    if (isinstance(node, ast.Compare) and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.Is, ast.IsNot))):
        a, b = node.left, node.comparators[0]
        return ((isinstance(a, ast.Name) and a.id in local_names and isinstance(b, ast.Constant)
                 and (b.value is None or type(b.value) is bool))
                or (isinstance(b, ast.Name) and b.id in local_names and isinstance(a, ast.Constant)
                    and (a.value is None or type(a.value) is bool)))
    return False


def only_local_conditions(original, candidate, old, new):
    """Allow a different guard instruction sequence while keeping the rest of a function identical."""
    if original is None:
        return False
    def definition(tree, code):
        return next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and not n.decorator_list
                     and n.lineno == code.co_firstlineno and n.name == code.co_name), None)
    before, after = definition(original, old), definition(candidate, new)
    if before is None or after is None:
        return False
    local_names = set(old.co_varnames)
    changed_guard = False

    def match(a, b):
        nonlocal changed_guard
        if type(a) is not type(b):
            return False
        if isinstance(a, list):
            return len(a) == len(b) and all(match(x, y) for x, y in zip(a, b))
        if not isinstance(a, ast.AST):
            return constant(a) == constant(b)
        if a is not before and isinstance(a, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef, ast.Lambda)):
            return ast.dump(a) == ast.dump(b)
        for field in a._fields:
            x, y = getattr(a, field), getattr(b, field)
            if (field == "test" and isinstance(a, (ast.If, ast.IfExp, ast.While))
                    and local_condition(x, local_names) and local_condition(y, local_names)):
                changed_guard |= ast.dump(x) != ast.dump(y)
            elif not match(x, y):
                return False
        return True

    return match(before, after) and changed_guard


class ExecutionIndex:
    def __init__(self, data, ids, children):
        if data["ids"] != ids:
            raise ValueError("execution map uses another test inventory")
        self.complete = data["complete"]
        self.files = data["files"]
        self.masks = [int(encoded, 16) for encoded in data["sets"]]
        self.untraced = self.masks[data["untraced"]]
        self.compatible = data["python"] == sys.implementation.cache_tag
        if children["ids"] != ids:
            raise ValueError("child coverage uses another inventory")
        matched = children["python"] == sys.implementation.cache_tag and children["platform"] == sys.platform
        self.child_complete = int(children["complete"], 16) & self.untraced if matched else 0
        self.child_calls = {k: int(v, 16) for k, v in children["scopes"].items()} if matched else {}
        self.child_reads = {k: int(v, 16) for k, v in children["reads"].items()} if matched else {}

    @staticmethod
    @lru_cache(maxsize=12)
    def baseline(source, path):
        return compile(source, path, "exec", dont_inherit=True)

    def select(self, path, source, tree, *, local_substitutions_only=False, original_tree=None):
        if not self.complete or path not in self.files or not path.startswith("sqlglot/"):
            return None
        try:
            changed = changes(self.baseline(source, path), compile(tree, path, "exec", dont_inherit=True))
        except (ValueError, RecursionError):
            return None
        selected = self.untraced & ~self.child_complete
        for old, new in changed:
            if old.co_flags & (0x20 | 0x80 | 0x200):
                return None
            if local_substitutions_only and (not self.compatible or not (scalar_replacements(old, new)
                    or only_local_conditions(original_tree, tree, old, new))):
                # New calls, stores and control flow can introduce unmeasured
                # interactions between modules or tests.
                return None
            name = scope_name(old)
            selected |= self.child_calls.get(f"{path}|{name}", 0)
            record = self.files[path].get(name)
            if record is None:
                # Only the measured interpreter can distinguish a never-called scope
                # from a scope the compiler emitted under another name.
                if not self.compatible:
                    return None
                continue
            early, calls, fingerprint, blocks = record
            if early:
                return None
            possible = self.masks[calls]
            if possible and self.compatible and bytecode_key(old) == fingerprint and old.co_exceptiontable == new.co_exceptiontable:
                before, a = instructions(old)
                after, b = instructions(new)
                if len(a) == len(b) and old.co_names == new.co_names:
                    starts = block_starts(old)
                    touched = {starts[bisect.bisect_right(starts, op.offset) - 1]
                               for op, left, right in zip(before, a, b) if left != right}
                    if touched:
                        block_masks = {offset: self.masks[m] for offset, m in blocks}
                        possible = 0
                        for offset in touched:
                            possible |= block_masks.get(offset, 0)
            selected |= possible
        return selected

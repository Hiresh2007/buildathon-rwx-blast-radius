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


def scalar_replacements(old, new):
    """Compare instructions with jump destinations and constants resolved."""
    if old.co_names != new.co_names or old.co_exceptiontable != new.co_exceptiontable:
        return False
    old_ops, before = instructions(old)
    new_ops, after = instructions(new)
    if len(before) != len(after):
        return False
    simple = (bool, int, float, str, bytes, type(None))
    for i, (left, right) in enumerate(zip(before, after)):
        if left == right:
            continue
        a, b = old_ops[i], new_ops[i]
        if (a.opname not in ("LOAD_CONST", "RETURN_CONST") or a.opname != b.opname
                or type(a.argval) not in simple or type(a.argval) is not type(b.argval)):
            return False
    return True


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

    def select(self, path, source, tree, *, whole_scopes=False):
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
            if whole_scopes and (not self.compatible or not scalar_replacements(old, new)):
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
            if not whole_scopes and possible and self.compatible and bytecode_key(old) == fingerprint and old.co_exceptiontable == new.co_exceptiontable:
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

"""Compare compiled functions against execution of the unmodified test suite."""

import ast
from ast_syntax import same_syntax
import bisect
import dis
import hashlib
import platform
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


def class_origin_only(old, new):
    """Identify CPython's compiler-generated __firstlineno__ class prologue."""
    if (old.co_flags & 1 or old.co_name == "<module>" or old.co_firstlineno == new.co_firstlineno
            or old.co_code != new.co_code or old.co_names != new.co_names
            or old.co_exceptiontable != new.co_exceptiontable):
        return False
    before, a = instructions(old)
    after, b = instructions(new)
    changed = [i for i, (left, right) in enumerate(zip(a, b)) if left != right]
    if len(a) != len(b) or len(changed) != 1:
        return False
    i = changed[0]
    return (i + 1 < len(a) and before[i].opname == "LOAD_CONST"
            and before[i].argval == old.co_firstlineno and after[i].argval == new.co_firstlineno
            and before[i + 1].opname == "STORE_NAME" and before[i + 1].argval == "__firstlineno__")


def changes(old, new, *, class_origins=False):
    """List changed deferred code, or reject a change to import/class execution."""
    children_old = [c for c in old.co_consts if isinstance(c, types.CodeType)]
    children_new = [c for c in new.co_consts if isinstance(c, types.CodeType)]
    if len(children_old) != len(children_new):
        raise ScopeChange("changed definition layout")
    changed = []
    if signature(old) != signature(new) and not (class_origins and old.co_qualname in class_origins and class_origin_only(old, new)):
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
        changed.extend(changes(before, after, class_origins=class_origins))
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
            return same_syntax(a, b)
        for field in a._fields:
            x, y = getattr(a, field), getattr(b, field)
            if (field == "test" and isinstance(a, (ast.If, ast.IfExp, ast.While))
                    and local_condition(x, local_names) and local_condition(y, local_names)):
                changed_guard |= not same_syntax(x, y)
            elif not match(x, y):
                return False
        return True

    return match(before, after) and changed_guard


def first_changed_blocks(old, new, old_ops, old_values, new_ops, new_values):
    """Walk both ordinary control-flow graphs up to their first semantic differences."""
    def handlers(code, operations):
        positions = {op.offset: i for i, op in enumerate(operations)}
        entries = tuple(dis.Bytecode(code).exception_entries)
        result = {}
        for entry in entries:
            if entry.target not in positions:
                return None
            for position in range(entry.start, entry.end, 2):
                if position in positions:
                    result.setdefault(positions[position], (entry.depth, entry.lasti, positions[entry.target]))
        return result
    old_handlers, new_handlers = handlers(old, old_ops), handlers(new, new_ops)
    if old_handlers is None or new_handlers is None:
        return None
    limit = 3 * (len(old_ops) + len(new_ops))
    starts = block_starts(old)
    changed = set()
    pending = [(0, 0)]
    visited = set()
    unconditional = {"JUMP_FORWARD", "JUMP_BACKWARD", "JUMP_BACKWARD_NO_INTERRUPT"}
    terminal = {"RETURN_VALUE", "RETURN_CONST", "RAISE_VARARGS", "RERAISE"}
    while pending:
        left, right = pending.pop()
        if (left, right) in visited:
            continue
        visited.add((left, right))
        if len(visited) > limit or not 0 <= left < len(old_ops) or not 0 <= right < len(new_ops):
            return None
        old_op, new_op = old_ops[left], new_ops[right]
        left_handler, right_handler = old_handlers.get(left), new_handlers.get(right)
        if left_handler and right_handler and left_handler[:2] == right_handler[:2]:
            pending.append((left_handler[2], right_handler[2]))
        if (left_handler or right_handler) and ((left_handler or ())[:2] != (right_handler or ())[:2]):
            differs = True
        elif old_op.opname != new_op.opname:
            differs = True
        elif old_op.opcode in dis.hasjump:
            # Compare both successors. Physical offsets shift when a branch grows.
            pending.append((old_values[left][1], new_values[right][1]))
            if old_op.opname not in unconditional:
                pending.append((left + 1, right + 1))
            continue
        else:
            differs = old_values[left] != new_values[right]
        if differs:
            changed.add(starts[bisect.bisect_right(starts, old_op.offset) - 1])
        elif old_op.opname not in terminal:
            pending.append((left + 1, right + 1))
    return changed


def guarded_returns_only(original, candidate, old, new):
    """Recognize insertion or removal of early returns on local identity checks."""
    if original is None:
        return False
    def definition(tree, code):
        return next((node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                     and not node.decorator_list and node.lineno == code.co_firstlineno
                     and node.name == code.co_name), None)
    before, after = definition(original, old), definition(candidate, new)
    if before is None or after is None:
        return False
    locals_ = set(old.co_varnames[:old.co_argcount + old.co_kwonlyargcount])
    changed = False
    def guard(node):
        if not (isinstance(node, ast.If) and not node.orelse and len(node.body) == 1
                and isinstance(node.body[0], ast.Return)):
            return False
        test = node.test
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1
                and isinstance(test.ops[0], (ast.Is, ast.IsNot))):
            return False
        left, right = test.left, test.comparators[0]
        if isinstance(left, ast.Constant):
            left, right = right, left
        passed = node.body[0].value
        return (isinstance(left, ast.Name) and left.id in locals_ and isinstance(right, ast.Constant)
                and (right.value is None or type(right.value) is bool)
                and (passed is None or isinstance(passed, ast.Constant)
                     or isinstance(passed, ast.Name) and passed.id in locals_))
    def match(a, b):
        nonlocal changed
        if type(a) is not type(b):
            return False
        if isinstance(a, list):
            i = j = 0
            while i < len(a) or j < len(b):
                if i < len(a) and j < len(b) and match(a[i], b[j]):
                    i += 1
                    j += 1
                elif i < len(a) and guard(a[i]):
                    changed = True
                    i += 1
                elif j < len(b) and guard(b[j]):
                    changed = True
                    j += 1
                else:
                    return False
            return True
        if not isinstance(a, ast.AST):
            return constant(a) == constant(b)
        if a is not before and isinstance(a, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef, ast.Lambda)):
            return same_syntax(a, b)
        return all(match(getattr(a, field), getattr(b, field)) for field in a._fields)
    return match(before, after) and changed


class ExecutionIndex:
    def __init__(self, data, ids, children, import_blocks=None, generators=None):
        if data["ids"] != ids:
            raise ValueError("execution map uses another test inventory")
        self.complete = data["complete"]
        self.files = data["files"]
        self.masks = [int(encoded, 16) for encoded in data["sets"]]
        self.untraced = self.masks[data["untraced"]]
        self.compatible = data["python"] == sys.implementation.cache_tag
        self.generator_files = {}
        self.generator_masks = []
        self.generator_untraced = 0
        if (generators and generators["complete"] and generators["ids"] == ids
                and generators["python"] == sys.implementation.cache_tag and generators["platform"] == sys.platform
                and generators.get("machine") == platform.machine()):
            self.generator_files = generators["files"]
            self.generator_masks = [int(encoded, 16) for encoded in generators["sets"]]
            self.generator_untraced = self.generator_masks[generators["untraced"]]
        self.import_blocks = {}
        if (import_blocks and import_blocks["python"] == sys.implementation.cache_tag
                and import_blocks["platform"] == sys.platform and import_blocks.get("machine") == platform.machine()):
            self.import_blocks = import_blocks["files"]
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

    def select(self, path, source, tree, *, local_substitutions_only=False, original_tree=None, class_origins=False):
        if not self.complete or path not in self.files or not path.startswith("sqlglot/"):
            return None
        try:
            changed = changes(self.baseline(source, path), compile(tree, path, "exec", dont_inherit=True), class_origins=class_origins)
        except (ValueError, RecursionError):
            return None
        selected = self.untraced & ~self.child_complete
        for old, new in changed:
            if old.co_flags & (0x80 | 0x200):
                return None
            generator = bool(old.co_flags & 0x20)
            if generator and not (scalar_replacements(old, new)
                    or only_local_conditions(original_tree, tree, old, new)):
                return None
            if local_substitutions_only and (not self.compatible or not (scalar_replacements(old, new)
                    or only_local_conditions(original_tree, tree, old, new)
                    or guarded_returns_only(original_tree, tree, old, new))):
                # New calls, stores and control flow can introduce unmeasured
                # interactions between modules or tests.
                return None
            name = scope_name(old)
            selected |= self.child_calls.get(f"{path}|{name}", 0)
            masks = self.masks
            if generator:
                record = self.generator_files.get(path, {}).get(name)
                if not record:
                    return None
                masks = self.generator_masks
                selected |= self.generator_untraced & ~self.child_complete
            else:
                record = self.files[path].get(name)
            if record is None:
                # Only the measured interpreter can distinguish a never-called scope
                # from a scope the compiler emitted under another name.
                if not self.compatible:
                    return None
                continue
            early, calls, fingerprint, blocks = record[:4]
            if generator:
                first = [fingerprint, record[4]] if early and record[4] else None
            else:
                first = self.import_blocks.get(path, {}).get(name) if early else None
            if early and (not first or first[0] != fingerprint):
                return None
            possible = masks[calls]
            localized = False
            if (possible or early) and self.compatible and bytecode_key(old) == fingerprint:
                before, a = instructions(old)
                after, b = instructions(new)
                if len(a) == len(b) and old.co_names == new.co_names and old.co_exceptiontable == new.co_exceptiontable:
                    starts = block_starts(old)
                    touched = {starts[bisect.bisect_right(starts, op.offset) - 1]
                               for op, left, right in zip(before, a, b) if left != right}
                else:
                    touched = first_changed_blocks(old, new, before, a, after, b)
                if touched:
                    if early and not touched.isdisjoint(first[1]):
                        return None
                    localized = True
                    block_masks = {offset: masks[m] for offset, m in blocks}
                    possible = 0
                    for offset in touched:
                        possible |= block_masks.get(offset, 0)
            if early and not localized:
                return None
            selected |= possible
        return selected

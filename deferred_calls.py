"""Combine cold module imports with observed calls to changed, deferred functions."""

import ast
import dis
import gzip
import hashlib
import json
from collections import Counter
from functools import lru_cache
from pathlib import Path
from types import CodeType

from ast_syntax import same_syntax
from cold_coverage import digest
from execution import compiled_candidate, scope_name, signature
from forwarding_decorators import deferred_decorators


@lru_cache(maxsize=4)
def code_readers(index):
    """Retain code-object and source inspection, including helpers used by other tests."""
    from cold_coverage import profile

    data = profile()
    if not data:
        return index.all_bits
    try:
        with gzip.open(Path(__file__).with_name("code-readers.json.gz"), "rt") as stream:
            stored = json.load(stream)
        tree = hashlib.sha256(
            json.dumps(data["hashes"], separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        if tree != stored["library"]:
            return index.all_bits
        mask = 0
        for path, expected in stored["readers"].items():
            if hashlib.sha256((index.root / path).read_bytes()).hexdigest() != expected:
                return index.all_bits
            mask |= index.test_files.get(path, 0) | index.isolated.get(path, index.all_bits)
            for calls in index.files.get(path, {}).values():
                mask |= calls
    except (OSError, ValueError, KeyError, TypeError):
        return index.all_bits
    return mask


def updates_external_state(node, code):
    locals_ = set(code.co_varnames) | set(code.co_cellvars) | set(code.co_freevars)
    external_calls = {"open", "exec", "eval", "globals", "setattr", "delattr", "__import__"}
    external_methods = {
        "write_text",
        "write_bytes",
        "unlink",
        "rename",
        "remove",
        "system",
        "popen",
        "setdefault",
        "update",
        "extend",
        "append",
        "add",
        "clear",
        "discard",
        "pop",
    }
    for child in ast.walk(node):
        if isinstance(child, (ast.Global, ast.Nonlocal, ast.Import, ast.ImportFrom)):
            return True
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name) and child.func.id in external_calls:
                return True
            if not isinstance(child.func, ast.Attribute) or child.func.attr not in external_methods:
                continue
            target = child.func.value
        elif isinstance(child, (ast.Attribute, ast.Subscript)) and isinstance(
            child.ctx, (ast.Store, ast.Del)
        ):
            target = child.value
        else:
            continue
        while isinstance(target, (ast.Attribute, ast.Subscript)):
            if isinstance(target, ast.Attribute) and target.attr in {
                "__class__",
                "__dict__",
                "__globals__",
            }:
                return True
            target = target.value
        if not isinstance(target, ast.Name) or target.id not in locals_:
            return True
    return False


def global_call_targets(node, code):
    """Include module-owned callables referenced by this function and its nested code."""
    globals_ = set()
    pending = [code]
    while pending:
        current = pending.pop()
        globals_.update(op.argval for op in dis.get_instructions(current) if op.opname == "LOAD_GLOBAL")
        pending.extend(item for item in current.co_consts if type(item) is CodeType)
    calls = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        target = child.func
        path = []
        while isinstance(target, ast.Attribute):
            path.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name) and target.id in globals_:
            calls.add((target.id, *reversed(path)))
    return calls


def changed_functions(before, after, original, candidate):
    """Keep eager bytecode, function metadata and closure creation identical."""
    changed = []

    def visit(a_body, b_body, old_code, new_code):
        if signature(old_code) != signature(new_code) or len(a_body) != len(b_body):
            return False
        old = {n.co_name: n for n in old_code.co_consts if type(n) is CodeType}
        new = {n.co_name: n for n in new_code.co_consts if type(n) is CodeType}
        old_names = Counter(n.co_name for n in old_code.co_consts if type(n) is CodeType)
        new_names = Counter(n.co_name for n in new_code.co_consts if type(n) is CodeType)
        for a, b in zip(a_body, b_body):
            if same_syntax(a, b):
                continue
            if (
                type(a) is not type(b)
                or not isinstance(a, (ast.FunctionDef, ast.ClassDef))
                or a.name != b.name
            ):
                return False
            if any(not same_syntax(getattr(a, f), getattr(b, f)) for f in a._fields if f != "body"):
                return False
            c, d = old.get(a.name), new.get(a.name)
            if (
                old_names[a.name] != 1
                or new_names[b.name] != 1
                or c is None
                or d is None
                or c.co_flags != d.co_flags
                or c.co_freevars != d.co_freevars
            ):
                return False
            if isinstance(a, ast.ClassDef):
                if not visit(a.body, b.body, c, d):
                    return False
            else:
                if (
                    c.co_flags & (0x20 | 0x80 | 0x200)
                    or a.name.startswith("__")
                    or ast.get_docstring(a, clean=False) != ast.get_docstring(b, clean=False)
                    or not deferred_decorators(a, before, after)
                ):
                    return False
                if (
                    updates_external_state(a, c)
                    or updates_external_state(b, d)
                    or not global_call_targets(b, d) <= global_call_targets(a, c)
                ):
                    return False
                changed.append(c)
        return True

    return changed if visit(before.body, after.body, original, candidate) else None


def observed_calls(index, path, entry, data):
    source, _, tree, replaced, inserted = entry
    if (
        not getattr(index.execution, "complete", False)
        or not index.execution.compatible
        or path not in index.execution.files
        or path not in index.isolated
        or data["hashes"].get(path) != digest(source.text)
    ):
        return None
    if not any(source.runtime_scope(line) is not None for line in replaced | inserted):
        return None
    original = index.execution.baseline(source.text, path)
    codes = changed_functions(source.tree, tree, original, compiled_candidate(tree, path))
    if not codes:
        return None
    bits = index.unknown | index.incomplete | index.execution.untraced | index.symbols.incomplete
    bits |= (
        index.reads.get(path, 0)
        | index.execution.child_reads.get(path, 0)
        | index.cache_consumers.floor(path, replaced, inserted)
    )
    groups = int(data["reads"].get(path, "0"), 16)
    while codes:
        code = codes.pop()
        scope = scope_name(code)
        warm = index.execution.files[path].get(scope)
        if warm:
            if warm[0]:
                return None
            bits |= index.execution.masks[warm[1]]
        bits |= index.execution.child_calls.get(f"{path}|{scope}", 0)
        cold = data["files"].get(path, {}).get(scope)
        if cold:
            groups |= int(cold[1], 16) | int(cold[2], 16)
        codes.extend(child for child in code.co_consts if type(child) is CodeType)
    witnessed = 0
    for n, members in enumerate(data["groups"]):
        group = int(members, 16)
        witnessed |= group
        if groups & (1 << n):
            bits |= group
    return bits | (index.isolated[path] & ~witnessed) | code_readers(index)

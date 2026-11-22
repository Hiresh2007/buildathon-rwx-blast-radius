"""Cross-check broad schedules against cold test file and warm individual coverage."""
import bisect
from functools import lru_cache
import gzip
import hashlib
import inspect
import json
from pathlib import Path
import platform
import sys

from execution import (ScopeChange, block_starts, bytecode_key, changes, compiled_candidate, first_changed_blocks,
                       instructions, scope_name)
from impact import _parse_diff, _prepare_change


@lru_cache(maxsize=2)
def profile(partial=False):
    prefix = "cold-partial" if partial else "cold"
    filename = f"{prefix}-{sys.platform}-{platform.machine()}.json.gz"
    path = Path(__file__).with_name(filename)
    if not path.is_file():
        return profile() if partial else None
    try:
        with gzip.open(path, "rt") as reader:
            data = json.load(reader)
    except (OSError, EOFError, UnicodeError, ValueError):
        return None
    if (data.get("python") != sys.implementation.cache_tag or data.get("version") != list(sys.version_info[:3])
            or data.get("platform") != sys.platform or data.get("machine") != platform.machine()):
        return None
    return data


@lru_cache(maxsize=1)
def profiled_tests(index):
    data = profile(partial=True)
    if not data or data["ids"] != index.execution_ids:
        return set()
    members = 0
    for group in data["groups"]:
        members |= int(group, 16)
    return index.tests_from_bits(members) - index.unmeasured


@lru_cache(maxsize=40)
def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def changed_offsets(old, new):
    before, left = instructions(old)
    after, right = instructions(new)
    starts = block_starts(old)
    if len(left) == len(right) and old.co_names == new.co_names and old.co_exceptiontable == new.co_exceptiontable:
        offsets = {op.offset for op, a, b in zip(before, left, right) if a != b}
        leaders = {starts[bisect.bisect_right(starts, offset) - 1] for offset in offsets}
    else:
        leaders = first_changed_blocks(old, new, before, left, after, right)
        # A first basic block can begin at RESUME, which emits no instruction event.
        # Retain callers that executed any observable instruction in a changed block.
        offsets = {op.offset for op in before
                   if leaders and starts[bisect.bisect_right(starts, op.offset) - 1] in leaders}
    return leaders or set(), sum(1 << (offset // 2) for offset in offsets)



def warm_hits(execution, path, scope, fingerprint, leaders, generator):
    reached = execution.child_calls.get(f"{path}|{scope}", 0)
    measured = execution.files.get(path, {}).get(scope)
    if measured:
        _, calls, original_key, blocks = measured
        if generator or not leaders or original_key != fingerprint:
            return reached | execution.masks[calls]
        for offset, mask in blocks:
            if offset in leaders:
                reached |= execution.masks[mask]
    return reached


def cold_hits(data, path, scope, fingerprint, offsets, generator):
    measured = data["files"].get(path, {}).get(scope)
    if not measured:
        return 0
    original_key, calls, uncertain, hits = measured
    reached = int(uncertain, 16)
    if generator or not offsets or original_key != fingerprint:
        return reached | int(calls, 16)
    for groups, visited in hits:
        if offsets & int(visited, 16):
            reached |= int(groups, 16)
    return reached

def unaffected(text, index, prepared=None, *, partial=False):
    """Find tests that miss every edit in both cold and warm original executions."""
    data = profile(partial)
    if not data or data["ids"] != index.execution_ids:
        return set()
    groups = (1 << len(data["groups"])) - 1
    warm = index.all_bits & ~index.execution.untraced & ~index.unknown
    try:
        changed_files = _parse_diff(text)
        paths = {change.path for change in changed_files}
        if (not paths or len(paths) != len(changed_files)
                or any(not path.startswith("sqlglot/") or not path.endswith(".py") for path in paths)):
            return set()
        for change in changed_files:
            path = change.path
            entry = prepared.get(path) if prepared is not None else None
            source, _, tree, replaced, inserted = entry if entry is not None else _prepare_change(change, index)
            if data["hashes"].get(path) != digest(source.text):
                return set()
            groups &= ~int(data["reads"].get(path, "0"), 16)
            warm &= ~(index.reads.get(path, 0) | index.execution.child_reads.get(path, 0)
                      | index.cache_consumers.floor(path, replaced, inserted))
            if not groups or not warm:
                return set()
            baseline = index.execution.baseline(source.text, path)
            candidate = compiled_candidate(tree, path)
            for old, new in changes(baseline, candidate):
                if old.co_flags & (inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR):
                    return set()
                scope = scope_name(old)
                key = bytecode_key(old)
                leaders, offsets = changed_offsets(old, new)
                generator = bool(old.co_flags & inspect.CO_GENERATOR)
                warm &= ~warm_hits(index.execution, path, scope, key, leaders, generator)
                groups &= ~cold_hits(data, path, scope, key, offsets, generator)
                if not groups or not warm:
                    return set()
    except (ScopeChange, UnicodeError, OSError, SyntaxError, ValueError, RecursionError):
        return set()
    cold_tests = 0
    for i, mask in enumerate(data["groups"]):
        if groups & (1 << i):
            cold_tests |= int(mask, 16)
    return index.tests_from_bits(cold_tests & warm) - index.unmeasured

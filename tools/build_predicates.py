"""Measure bounded comparison operand domains on the pinned, unmodified SQLGlot checkout."""
import argparse
import ast
import builtins
import concurrent.futures
import gzip
import importlib.machinery
import json
import os
import sys
from pathlib import Path
from functools import lru_cache
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from predicates import OPS, UNKNOWN, boolean_names, encode, semantic_value
from build_map import TestTrace, TracedPool

UNMODELLED = (UNKNOWN, UNKNOWN, False, None)
TRUTH_CLASSES = {id(cls) for cls in (bool, int, str, tuple, list, dict, set, frozenset, bytes, float, type(None))}


class Comparisons(ast.NodeTransformer):
    def __init__(self, trace, path):
        self.trace = trace
        self.path = path

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]).__name__ not in OPS:
            return node
        key = (self.path, f"{node.lineno}:{node.col_offset}")
        site = self.trace.locations.setdefault(key, len(self.trace.locations))
        if site == len(self.trace.records):
            self.trace.records.append({})
        op = type(node.ops[0]).__name__
        return ast.copy_location(ast.Call(ast.Name("_rwx_observe_comparison", ast.Load()), [
            ast.Constant(site), ast.Constant(op), node.left, node.comparators[0]
        ], []), node)

    def instrument_condition(self, node):
        names = boolean_names(node.test)
        self.generic_visit(node)
        if names is not None:
            key = (self.path, f"B:{node.test.lineno}:{node.test.col_offset}")
            site = self.trace.locations.setdefault(key, len(self.trace.locations))
            if site == len(self.trace.records):
                self.trace.records.append({})
            probe = ast.Call(ast.Name("_rwx_observe_guard", ast.Load()), [
                ast.Constant(site), ast.Constant(names), ast.Constant(isinstance(node.test.op, ast.And))
            ], [])
            node.test = ast.copy_location(ast.BoolOp(ast.And(), [probe, node.test]), node.test)
        return node

    visit_If = visit_While = visit_IfExp = instrument_condition


@lru_cache(maxsize=None)
def local_names(code):
    return set(code.co_varnames + code.co_cellvars + code.co_freevars)


@lru_cache(maxsize=512)
def always_truthy(cls):
    return all("__bool__" not in type.__getattribute__(base, "__dict__")
               and "__len__" not in type.__getattribute__(base, "__dict__")
               for base in type.__getattribute__(cls, "__mro__"))


class PredicateTrace(TestTrace):
    def __init__(self, source, tests):
        super().__init__(source, tests)
        self.locations = {}
        self.records = []
        self.everyone = (1 << len(self.ids)) - 1
        self.importing = 0

    def set_context(self, mask, trace_lines):
        self.active, self.trace_lines = mask, trace_lines

    def observe(self, site, op, a, b):
        mask = self.everyone if self.importing or self.collecting or not self.active else self.active
        observations = self.records[site]
        try:
            result = OPS[op](a, b)
        except BaseException:
            observations[UNMODELLED] = observations.get(UNMODELLED, 0) | mask
            raise
        if observations.get(UNMODELLED, 0) & mask == mask:
            return result
        first = encode(a)
        second = encode(b) if first != UNKNOWN else UNKNOWN
        # Identity-only classes can prove comparisons against another identity
        # or None. A reflected operation from a third type remains unmodelled.
        kinds = {semantic_value(first)[0], semantic_value(second)[0]}
        mixed = "o" in kinds and not kinds <= {"n", "o"}
        if first == UNKNOWN or second == UNKNOWN or mixed or type(result) is not bool:
            observation = UNMODELLED
        else:
            observation = (first, second, a is b, result)
            if len(observations) >= 64 and observation not in observations:
                observation = UNMODELLED
        observations[observation] = observations.get(observation, 0) | mask
        return result

    def guard(self, site, names, conjunction):
        mask = self.everyone if self.importing or self.collecting or not self.active else self.active
        observations = self.records[site]
        if observations.get(UNMODELLED, 0) & mask == mask:
            return True
        frame = sys._getframe(1)
        values = frame.f_locals
        local = local_names(frame.f_code)
        signs = []
        state = UNMODELLED
        for name, negated in names:
            if name not in local or name not in values:
                break
            value = values[name]
            cls = type(value)
            if id(cls) in TRUTH_CLASSES:
                truth = bool(value)
            elif type(cls) is type and always_truthy(cls):
                truth = True
            else:
                break
            signs.append(truth != negated)
        else:
            state = ("truth", tuple(signs), all(signs) if conjunction else any(signs))
        observations[state] = observations.get(state, 0) | mask
        return True

    def collect(self):
        loader_type = importlib.machinery.SourceFileLoader
        normal_code, normal_exec = loader_type.get_code, loader_type.exec_module

        def get_code(loader, name):
            path = self.source_path(loader.path)
            if not path or not path.startswith("sqlglot/"):
                return normal_code(loader, name)
            tree = ast.parse(loader.get_data(loader.path), filename=loader.path)
            tree = ast.fix_missing_locations(Comparisons(self, path).visit(tree))
            return compile(tree, loader.path, "exec", dont_inherit=True)

        def execute(loader, module):
            self.importing += 1
            try:
                normal_exec(loader, module)
            finally:
                self.importing -= 1

        sys.addaudithook(self.audit)
        with (patch.object(loader_type, "get_code", get_code), patch.object(loader_type, "exec_module", execute),
              patch.object(builtins, "_rwx_observe_comparison", self.observe, create=True),
              patch.object(builtins, "_rwx_observe_guard", self.guard, create=True),
              patch.object(concurrent.futures, "ProcessPoolExecutor", TracedPool)):
            return pytest.main(["tests", "-q", "--assert=plain", "-p", "no:cacheprovider"], plugins=[self])

    def write(self, output):
        files = {}
        for (path, key), site in sorted(self.locations.items()):
            seen = self.records[site]
            if seen.get(UNMODELLED, 0) == self.everyone:
                continue
            files.setdefault(path, {})[key] = [
                (state, format(mask, "x")) for state, mask in seen.items()
            ]
        data = {"ids": self.ids, "complete": format(self.passed & ~self.failed & ~self.outside_process, "x"), "files": files}
        output.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
        print(f"Observed {len(self.records)} comparison sites; {output.stat().st_size:,} compressed bytes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["source", "tests_json", "output"]:
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    trace = PredicateTrace(root, json.loads(args.tests_json.read_bytes())["tests"])
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    os.environ.update(SKIP_INTEGRATION="0", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    if trace.collect() not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        raise RuntimeError("baseline collection failed")
    trace.write(output)


if __name__ == "__main__":
    main()

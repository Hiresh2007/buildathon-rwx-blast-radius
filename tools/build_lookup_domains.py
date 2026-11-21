"""Record exact string-key sets for bounded builtin lookups on the pinned baseline."""

import argparse
import ast
import builtins
import concurrent.futures
import gzip
import importlib.machinery
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lookup_domains import lookup, site
from tools.build_map import TestTrace, TracedPool


class Calls(ast.NodeTransformer):
    def __init__(self, trace, path):
        self.trace = trace
        self.path = path

    def visit(self, node):
        desc = lookup(node)
        if not desc or (desc[0] == "literal_members") != self.trace.only_members:
            return super().visit(node)
        kind, key = desc
        tag = (self.path, site(node, key))
        registry = self.trace.registry
        if tag not in registry:
            registry[tag] = len(self.trace.records)
            self.trace.records.append([node.end_lineno, 0, 0, {}])
        number = registry[tag]
        self.generic_visit(node)
        if kind == "get":
            args = [ast.Constant(number), node.func, *node.args]
            name = "_rwx_record_get"
        else:
            args = [
                ast.Constant(number),
                node.left,
                node.comparators[0],
                ast.Constant(isinstance(node.ops[0], ast.NotIn)),
            ]
            name = "_rwx_record_needle" if kind == "literal_members" else "_rwx_record_membership"
        return ast.copy_location(ast.Call(ast.Name(name, ast.Load()), args, []), node)


class Trace(TestTrace):
    def __init__(self, source, tests, only_members=False):
        super().__init__(source, tests)
        self.only_members = only_members
        self.everyone = (1 << len(self.ids)) - 1
        self.registry = {}
        self.records = []
        self.importing = 0

    def set_context(self, mask, trace_lines):
        self.active, self.trace_lines = mask, trace_lines

    def record(self, number, collection):
        mask = self.everyone if self.importing or self.collecting or not self.active else self.active
        rec = self.records[number]
        rec[1] |= mask
        if rec[2] & mask == mask:
            return
        if collection is None or len(collection) > 32:
            rec[2] |= mask
            return
        if any(type(key) is not str or len(key) > 96 for key in collection):
            rec[2] |= mask
            return
        keys = frozenset(collection)
        patterns = rec[3]
        if keys not in patterns and len(patterns) >= 96:
            rec[2] |= mask
        else:
            patterns[keys] = patterns.get(keys, 0) | mask

    def get(self, number, bound, *args):
        valid = (
            type(bound) is types.BuiltinMethodType
            and type(bound.__self__) is dict
            and bound.__name__ == "get"
        )
        self.record(number, bound.__self__ if valid else None)
        return bound(*args)

    def membership(self, number, key, container, negate):
        self.record(number, container if type(container) in (dict, set, frozenset, tuple, list) else None)
        return key not in container if negate else key in container

    def needle(self, number, key, container, negate):
        self.record(number, (key,) if type(key) is str else None)
        return key not in container if negate else key in container

    def collect(self):
        loader = importlib.machinery.SourceFileLoader
        get_code, execute = loader.get_code, loader.exec_module

        def instrument(loader, name):
            path = self.source_path(loader.path)
            if not path or not path.startswith("sqlglot/"):
                return get_code(loader, name)
            tree = ast.parse(loader.get_data(loader.path), filename=loader.path)
            tree = ast.fix_missing_locations(Calls(self, path).visit(tree))
            return compile(tree, loader.path, "exec", dont_inherit=True)

        def load(loader, module):
            self.importing += 1
            try:
                execute(loader, module)
            finally:
                self.importing -= 1

        sys.addaudithook(self.audit)
        with (
            patch.object(loader, "get_code", instrument),
            patch.object(loader, "exec_module", load),
            patch.object(builtins, "_rwx_record_get", self.get, create=True),
            patch.object(builtins, "_rwx_record_membership", self.membership, create=True),
            patch.object(builtins, "_rwx_record_needle", self.needle, create=True),
            patch.object(concurrent.futures, "ProcessPoolExecutor", TracedPool),
        ):
            return pytest.main(["tests", "-q", "--assert=plain", "-p", "no:cacheprovider"], plugins=[self])

    def save(self, output):
        files = {}
        for (path, name), n in sorted(self.registry.items()):
            end, observed, unknown, patterns = self.records[n]
            if observed & ~unknown:
                files.setdefault(path, {})[name] = [
                    end,
                    format(observed, "x"),
                    format(unknown, "x"),
                    [(sorted(keys), format(mask, "x")) for keys, mask in patterns.items()],
                ]
        result = {
            "ids": self.ids,
            "complete": format(self.passed & ~self.failed & ~self.outside_process, "x"),
            "files": files,
        }
        output.write_bytes(gzip.compress(json.dumps(result, separators=(",", ":")).encode(), mtime=0))
        print(sum(map(len, files.values())), "recorded lookup sites;", output.stat().st_size, "bytes")


if __name__ == "__main__":
    args = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "tests_json", "output"):
        args.add_argument(name, type=Path)
    args.add_argument("--literal-members", action="store_true")
    args = args.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    trace = Trace(root, json.loads(args.tests_json.read_bytes())["tests"], args.literal_members)
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.update(
        SKIP_INTEGRATION="0",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
    )
    if trace.collect() not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        raise RuntimeError("baseline collection failed")
    trace.save(output)

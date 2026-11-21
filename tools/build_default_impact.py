"""Observe use and introspection of declared scalar defaults on the pinned suite."""

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
from default_impact import functions, parameters
from tools.build_map import TestTrace, TracedPool


class Defaults(TestTrace):
    def __init__(self, root, tests):
        super().__init__(root, tests)
        self.everyone = (1 << len(self.ids)) - 1
        self.importing = 0
        self.reflect = {}
        self.sites = {}
        self.records = []

    def set_context(self, mask, trace_lines):
        self.active, self.trace_lines = mask, trace_lines

    def mask(self):
        return self.everyone if self.importing or self.collecting or not self.active else self.active

    def access(self, event, args):
        if event in ("object.__getattr__", "object.__setattr__", "object.__delattr__") and len(args) >= 2:
            obj, attr = args[:2]
            if type(obj) is types.FunctionType and attr in ("__defaults__", "__kwdefaults__"):
                code = obj.__code__
                path = self.source_path(code.co_filename)
                if path and path.startswith("sqlglot/"):
                    site = (path, code.co_firstlineno)
                    self.reflect[site] = self.reflect.get(site, 0) | self.mask()

    def at_entry(self, number, *values):
        mask = self.mask()
        for i, value in enumerate(values):
            _, _, default, current = self.records[number][i]
            if current & mask == mask:
                continue
            # A different type, or an unequal value of the same builtin type,
            # can only come from an explicitly supplied argument.
            if type(value) is type(default) and value == default:
                self.records[number][i][3] = current | mask

    def instrument(self, tree, path):
        for name, fn in functions(tree):
            params = list(parameters(fn))
            if not params:
                continue
            key = path, name
            if key not in self.sites:
                self.sites[key] = (fn.lineno, len(self.records))
                self.records.append([[arg, n.lineno, n.value, 0] for arg, n in params])
            _, number = self.sites[key]
            call = ast.Expr(
                ast.Call(
                    ast.Name("_rwx_observe_defaults", ast.Load()),
                    [ast.Constant(number)] + [ast.Name(arg, ast.Load()) for arg, _ in params],
                    [],
                )
            )
            # Preserve any Python docstring and all function bindings.
            after_doc = int(
                isinstance(fn.body[0], ast.Expr)
                and isinstance(fn.body[0].value, ast.Constant)
                and isinstance(fn.body[0].value.value, str)
            )
            fn.body.insert(after_doc, ast.copy_location(call, fn.body[0]))
        return ast.fix_missing_locations(tree)

    def collect(self):
        loader = importlib.machinery.SourceFileLoader
        get_code, execute = loader.get_code, loader.exec_module

        def instrument(loader, name):
            path = self.source_path(loader.path)
            if not path or not path.startswith("sqlglot/"):
                return get_code(loader, name)
            tree = ast.parse(loader.get_data(loader.path), filename=loader.path)
            return compile(self.instrument(tree, path), loader.path, "exec", dont_inherit=True)

        def load(loader, module):
            self.importing += 1
            try:
                execute(loader, module)
            finally:
                self.importing -= 1

        sys.addaudithook(self.audit)
        sys.addaudithook(self.access)
        with (
            patch.object(loader, "get_code", instrument),
            patch.object(loader, "exec_module", load),
            patch.object(builtins, "_rwx_observe_defaults", self.at_entry, create=True),
            patch.object(concurrent.futures, "ProcessPoolExecutor", TracedPool),
        ):
            return pytest.main(["tests", "-q", "--assert=plain", "-p", "no:cacheprovider"], plugins=[self])

    def save(self, target):
        files = {}
        for (path, name), (line, number) in self.sites.items():
            reflection = self.reflect.get((path, line), 0)
            for arg, line, _, mask in self.records[number]:
                result = mask | reflection
                if result != self.everyone:
                    files.setdefault(path, {})[name + "|" + arg] = (line, format(result, "x"))
        data = {
            "ids": self.ids,
            "complete": format(self.passed & ~self.failed & ~self.outside_process, "x"),
            "files": files,
        }
        target.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
        print(sum(map(len, files.values())), "default fields;", target.stat().st_size, "bytes")


if __name__ == "__main__":
    args = argparse.ArgumentParser(description=__doc__)
    for arg in ("root", "tests", "out"):
        args.add_argument(arg, type=Path)
    args = args.parse_args()
    root, target = args.root.resolve(), args.out.resolve()
    trace = Defaults(root, json.loads(args.tests.read_bytes())["tests"])
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.update(
        SKIP_INTEGRATION="0",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
    )
    if trace.collect() not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        raise RuntimeError("baseline execution failed")
    trace.save(target)

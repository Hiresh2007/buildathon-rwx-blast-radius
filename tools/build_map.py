"""Trace the unmodified, pinned SQLGlot checkout against the official test inventory.

Run in an isolated Python 3.12+ environment with the kit's requirements.lock.
Inputs are a SQLGlot checkout and tests.json; no changes or failure labels are read.
"""
import argparse
import ast
import builtins
import concurrent.futures
import gzip
import importlib.machinery
import json
import os
from pathlib import Path
import pickle
import sys
from collections import defaultdict
from contextlib import contextmanager
from functools import lru_cache
from unittest.mock import patch

import pytest


class AttributeReads(ast.NodeTransformer):
    """Instrument reads of configuration fields without changing their actual values."""

    def __init__(self, names):
        self.names = names

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr in self.names:
            call = ast.Call(ast.Name("_rwx_attr", ast.Load()), [node.value, ast.Constant(node.attr)], [])
            return ast.copy_location(call, node)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if (isinstance(node.func, ast.Name) and node.func.id in {"vars", "getattr"}
                and node.args and not node.keywords):
            node.args.insert(0, node.func)
            node.func = ast.copy_location(ast.Name("_rwx_lookup", ast.Load()), node.func)
        return node


class ConfigTrace:
    """Measure ordinary scalar class settings and reads of their raw class namespaces."""

    def __init__(self, trace, root):
        self.trace = trace
        self.keys = {}
        self.seen = defaultdict(int)
        self.names = {"__dict__"}
        self.importing = 0
        self.all_tests = (1 << len(trace.ids)) - 1
        for path in (root / "sqlglot").rglob("*.py"):
            module = ".".join(path.relative_to(root).with_suffix("").parts).removesuffix(".__init__")
            tree = ast.parse(path.read_bytes())
            for cls in [node for node in tree.body if isinstance(node, ast.ClassDef)]:
                if cls.decorator_list:
                    continue
                # A bare use while the class is being built can cache a derived value.
                class_code = [s for s in cls.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))]
                aliases = {n.id for s in class_code for n in ast.walk(s) if isinstance(n, ast.Name)
                           and isinstance(n.ctx, ast.Load)}
                for stmt in cls.body:
                    targets = (stmt.targets if isinstance(stmt, ast.Assign) else
                               [stmt.target] if isinstance(stmt, ast.AnnAssign) else [])
                    if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                        continue
                    name = targets[0].id
                    if (not name.isupper() or "_" not in name or name in aliases
                            or not isinstance(stmt.value, ast.Constant)
                            or type(stmt.value.value) not in (bool, int, float, str)):
                        continue
                    self.names.add(name)
                    self.keys[(module, cls.name, name)] = f"{path.relative_to(root)}:{cls.name}.{name}"

    @lru_cache(maxsize=None)
    def resolve(self, cls, name):
        # Custom metaclasses and subclass hooks may act on raw values at class creation.
        if type(cls) is not type or any("__init_subclass__" in c.__dict__ for c in cls.__mro__[:-1]):
            return None
        for owner in cls.__mro__:
            if name in owner.__dict__:
                return self.keys.get((owner.__module__, owner.__qualname__, name))
        return None

    def record(self, obj, name):
        if isinstance(obj, super):
            # Inherited class settings can also be reached through super().
            owner = obj.__self_class__
            if owner is not None:
                for base in owner.__mro__:
                    if base is not owner:
                        self.record(base, name)
            return
        cls = obj if isinstance(obj, type) else type(obj)
        if type(cls) is not type:
            return
        if name == "__dict__":
            if isinstance(obj, type):
                for base in cls.__mro__:
                    for field in base.__dict__.keys() & self.names:
                        if field != "__dict__":
                            self.record(base, field)
            return
        key = self.resolve(cls, name)
        if key:
            mask = self.all_tests if self.importing or self.trace.collecting else self.trace.active
            self.seen[key] |= mask

    def attr(self, obj, name):
        self.record(obj, name)
        return getattr(obj, name)

    def lookup(self, fn, *args):
        if fn is vars and len(args) == 1:
            self.record(args[0], "__dict__")
        if fn is getattr and len(args) in (2, 3) and isinstance(args[1], str) and args[1] in self.names:
            self.record(args[0], args[1])
        return fn(*args)

    @contextmanager
    def instrument(self):
        loader_type = importlib.machinery.SourceFileLoader
        normal_code = loader_type.get_code
        normal_exec = loader_type.exec_module

        def get_code(loader, fullname):
            if not self.trace.source_path(loader.path):
                return normal_code(loader, fullname)
            tree = ast.parse(loader.get_data(loader.path), filename=loader.path)
            tree = ast.fix_missing_locations(AttributeReads(self.names).visit(tree))
            return compile(tree, loader.path, "exec", dont_inherit=True)

        def execute(loader, module):
            self.importing += 1
            try:
                normal_exec(loader, module)
            finally:
                self.importing -= 1

        with (patch.object(loader_type, "get_code", get_code), patch.object(loader_type, "exec_module", execute),
              patch.object(builtins, "_rwx_attr", self.attr, create=True),
              patch.object(builtins, "_rwx_lookup", self.lookup, create=True)):
            yield


class TracedPool(concurrent.futures.Executor):
    """Execute pool jobs under the submitting test's line monitor.

    Round-trip arguments and results through pickle, as ProcessPoolExecutor does.
    SQLGlot's optimizer and executor tests use their pools only for pure jobs.
    """

    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            result = fn(*args, **kwargs)
            future.set_result(pickle.loads(pickle.dumps(result)))
        except Exception as error:
            future.set_exception(error)
        return future


class TestTrace:
    def __init__(self, source, tests):
        self.root = str(source) + os.sep
        self.ids = [test["id"] for test in sorted(tests, key=lambda t: t["number"])]
        self.bits = {nodeid: 1 << i for i, nodeid in enumerate(self.ids)}
        self.scopes = defaultdict(int)
        for nodeid, bit in self.bits.items():
            parts = nodeid.split("::")
            for n in range(1, len(parts)):
                self.scopes["::".join(parts[:n])] |= bit
        self.files = defaultdict(dict)
        self.reads = defaultdict(int)
        self.import_lines = defaultdict(set)
        self.collecting = True
        self.passed = 0
        self.failed = 0
        self.outside_process = 0
        self.active = 0
        self.trace_lines = False
        self.monitor = sys.monitoring
        self.tool = self.monitor.COVERAGE_ID
        self.config = ConfigTrace(self, source)

    @lru_cache(maxsize=None)
    def source_path(self, path):
        if path.startswith(self.root):
            return path[len(self.root):]
        return None

    def line(self, code, lineno):
        path = self.source_path(code.co_filename)
        if path:
            if self.collecting:
                self.import_lines[path].add(lineno)
            elif self.active and self.trace_lines:
                lines = self.files[path]
                lines[lineno] = lines.get(lineno, 0) | self.active
        # Measuring each site once per context avoids tracing tight loops repeatedly.
        return self.monitor.DISABLE

    def audit(self, event, args):
        if not self.active:
            return
        if event == "subprocess.Popen":
            self.outside_process |= self.active
        if event != "open" or not isinstance(args[0], str):
            return
        filename, mode, flags = args
        if (mode and "r" not in mode) or (flags & os.O_ACCMODE) == os.O_WRONLY:
            return
        path = self.source_path(os.path.abspath(filename))
        if path and "__pycache__" not in path and not path.startswith("."):
            # A cold import opens .py, while a warm import reads the cached bytecode.
            # Keep explicit source inspections and measure imports through line events.
            caller = sys._getframe(1).f_code
            if caller.co_name == "get_data" and "importlib" in caller.co_filename:
                return
            if self.trace_lines or not path.endswith((".py", ".pyc")):
                self.reads[path] |= self.active

    def set_context(self, mask, trace_lines):
        if (mask, trace_lines) != (self.active, self.trace_lines):
            self.active = mask
            self.trace_lines = trace_lines
            self.monitor.restart_events()

    @contextmanager
    def context(self, mask, trace_lines=True):
        previous = self.active, self.trace_lines
        self.set_context(mask, trace_lines)
        try:
            yield
        finally:
            self.set_context(*previous)

    @pytest.hookimpl(wrapper=True)
    def pytest_make_collect_report(self, collector):
        # A fixture opened in a module's top-level code belongs to every test in it.
        mask = self.scopes.get(collector.nodeid, 0)
        with self.context(mask, False):
            return (yield)

    def pytest_collection_modifyitems(self, items):
        # The official test list excludes cases skipped on the scoring platform.
        items[:] = [item for item in items if item.nodeid in self.bits]

    def pytest_collection_finish(self):
        self.collecting = False

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        with self.context(self.bits[item.nodeid]):
            return (yield)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        # In particular, unittest setUpClass can read inputs on behalf of many tests.
        if fixturedef.scope == "session":
            mask = (1 << len(self.ids)) - 1
        else:
            mask = self.scopes.get(request.node.nodeid, self.active)
        with self.context(mask):
            return (yield)

    def pytest_runtest_logreport(self, report):
        bit = self.bits.get(report.nodeid, 0)
        if report.failed or report.skipped:
            self.failed |= bit
        elif report.when == "call" and report.passed:
            self.passed |= bit

    def collect(self):
        self.monitor.use_tool_id(self.tool, "sqlglot-test-map")
        self.monitor.register_callback(self.tool, self.monitor.events.LINE, self.line)
        self.monitor.set_events(self.tool, self.monitor.events.LINE)
        sys.addaudithook(self.audit)
        try:
            with patch.object(concurrent.futures, "ProcessPoolExecutor", TracedPool), self.config.instrument():
                # Plain assertions use the source loader so test-side attribute reads are traced too.
                return pytest.main(["tests", "-q", "--assert=plain", "-p", "no:cacheprovider"], plugins=[self])
        finally:
            self.active = 0
            self.monitor.set_events(self.tool, 0)
            self.monitor.register_callback(self.tool, self.monitor.events.LINE, None)
            self.monitor.free_tool_id(self.tool)

    def write(self, target):
        sets = {}

        def intern(mask):
            if mask not in sets:
                sets[mask] = len(sets)
            return sets[mask]

        missing = (1 << len(self.ids)) - 1 - self.passed
        incomplete = self.failed | self.outside_process | missing
        data = {
            "ids": self.ids,
            "import_lines": {
                path: sorted(lines) for path, lines in sorted(self.import_lines.items())
            },
            "files": {
                path: {line: intern(mask) for line, mask in sorted(lines.items())}
                for path, lines in sorted(self.files.items())
            },
            "reads": {
                path: intern(mask) for path, mask in sorted(self.reads.items())
                if (Path(self.root) / path).is_file()
            },
            "incomplete": intern(incomplete),
            "config": {key: intern(mask) for key, mask in sorted(self.config.seen.items())
                       if mask and mask != self.config.all_tests},
            "sets": [format(mask, "x") for mask in sets],
        }
        payload = json.dumps(data, separators=(",", ":")).encode()
        target.write_bytes(gzip.compress(payload, compresslevel=6, mtime=0))
        print(f"Mapped {len(self.files)} source files and {len(data['reads'])} file reads. "
              f"Tests requiring conservative selection: {incomplete.bit_count()}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("tests_json", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.source.resolve()
    output = args.output.resolve()
    trace = TestTrace(root, json.loads(args.tests_json.read_text())["tests"])
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["SKIP_INTEGRATION"] = "0"
    code = trace.collect()
    if code not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        raise SystemExit(code)
    trace.write(output)


if __name__ == "__main__":
    main()

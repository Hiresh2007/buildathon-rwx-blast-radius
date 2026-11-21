"""Trace pinned SQLGlot class, module and dispatch dependencies by pytest node ID."""

import argparse
import ast
import builtins
import enum
from collections import defaultdict
from contextlib import contextmanager, ExitStack
import concurrent.futures
from functools import lru_cache
import gzip
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import patch

import pytest
from build_map import TestTrace, TracedPool


def declared_names(tree, module):
    bindings = {}

    def visit(body, owner):
        for s in body:
            if isinstance(s, ast.ClassDef):
                bases = [
                    b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else ""
                    for b in s.bases
                ]
                if set(bases) & {"Enum", "IntEnum", "AutoName"}:
                    continue
                visit(s.body, f"{owner}.{s.name}" if owner else s.name)
            if isinstance(s, (ast.AnnAssign, ast.Assign)):
                targets = s.targets if isinstance(s, ast.Assign) else [s.target]
                for t in targets:
                    data = isinstance(
                        s.value,
                        (ast.Constant, ast.List, ast.Tuple, ast.Set, ast.Dict, ast.Name, ast.Attribute),
                    )
                    if (
                        isinstance(t, ast.Name)
                        and not t.id.startswith("__")
                        and (t.id.isupper() or owner and data)
                    ):
                        bindings[f"{module}:{owner}.{t.id}" if owner else f"{module}:{t.id}"] = t.id

    visit(tree.body, "")
    return bindings


def label(obj):
    return f"{obj.__module__}:{obj.__qualname__}"


def accumulate(mapping, key, mask):
    old = mapping.get(key, 0)
    # Replacing an equal integer allocates live memory in a memory-sensitive test.
    if old & mask != mask:
        mapping[key] = old | mask


def key_label(value):
    if isinstance(value, type):
        return "class:" + label(value)
    typ = type(value)
    if isinstance(value, enum.Enum):
        return "enum:" + label(typ) + "." + str(value.name)
    if type(value) in (str, int, bool, float) or value is None:
        return type(value).__name__ + ":" + str(value)
    return None


def call(name, *args):
    return ast.Call(ast.Name("_rwx_obs_" + name, ast.Load()), list(args), [])


class Rewriter(ast.NodeTransformer):
    def __init__(self, observer, module, package):
        self.o = observer
        self.module = module
        self.package = package
        self.classes = []
        self.function_depth = 0
        self.function = None
        self.aliases = dict(observer.module_variables.get(module, {}))

    def visit_ClassDef(self, n):
        self.classes.append(n.name)
        self.generic_visit(n)
        self.classes.pop()
        return n

    def visit_FunctionDef(self, n):
        previous, self.function = self.function, n.name
        self.function_depth += 1
        self.generic_visit(n)
        self.function_depth -= 1
        self.function = previous
        if self.classes and n.name in {"__init__", "__new__"}:
            args = n.args.posonlyargs + n.args.args
            if args and args[0].arg in {"self", "cls"}:
                hit = ast.Expr(call("activity", ast.Name(args[0].arg, ast.Load())))
                pos = int(
                    bool(
                        n.body
                        and isinstance(n.body[0], ast.Expr)
                        and isinstance(n.body[0].value, ast.Constant)
                    )
                )
                n.body.insert(pos, ast.copy_location(hit, n))
        return n

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ImportFrom(self, n):
        if n.module == "__future__":
            return n
        base = n.module or ""
        if n.level:
            try:
                base = importlib.util.resolve_name("." * n.level + base, self.package)
            except (ImportError, ValueError):
                return n
        for a in n.names:
            origin = f"{base}:{a.name}"
            if origin in self.o.declared:
                self.aliases[a.asname or a.name] = origin
        return n

    def visit_Name(self, n):
        if isinstance(n.ctx, ast.Load) and n.id in self.aliases:
            return ast.copy_location(call("variable", ast.Constant(self.aliases[n.id]), n), n)
        return n

    def visit_Attribute(self, n):
        self.generic_visit(n)
        if isinstance(n.ctx, ast.Load) and n.attr in self.o.names:
            return ast.copy_location(call("attr", n.value, ast.Constant(n.attr)), n)
        return n

    def table(self, n):
        if isinstance(n, ast.Attribute) and n.attr in self.o.names:
            return call("table", self.visit(n.value), ast.Constant(n.attr))
        if isinstance(n, ast.Name) and n.id in self.aliases:
            return call("named_table", ast.Constant(self.aliases[n.id]), n)
        return None

    def visit_Subscript(self, n):
        t = (
            self.table(n.value)
            if isinstance(n.ctx, ast.Load) and not isinstance(n.slice, ast.Slice)
            else None
        )
        if t is not None:
            return ast.copy_location(call("item", t, self.visit(n.slice)), n)
        return self.generic_visit(n)

    def visit_Call(self, n):
        # SQLGlot copies its TRANSFORMS table once per concrete generator, then
        # queries the resulting _dispatch table. Measure each operation separately.
        if (
            self.module == "sqlglot.generator"
            and self.function == "_build_dispatch"
            and isinstance(n.func, ast.Name)
            and n.func.id == "dict"
            and len(n.args) == 1
            and not n.keywords
            and isinstance(n.args[0], ast.Attribute)
            and n.args[0].attr == "TRANSFORMS"
        ):
            return ast.copy_location(call("dispatch_copy", self.table(n.args[0])), n)
        if (
            isinstance(n.func, ast.Attribute)
            and n.func.attr in {"get", "pop"}
            and 1 <= len(n.args) <= 2
            and not n.keywords
        ):
            t = self.table(n.func.value)
            if t is not None and not any(isinstance(a, ast.Starred) for a in n.args):
                return ast.copy_location(call(n.func.attr, t, *(self.visit(a) for a in n.args)), n)
        self.generic_visit(n)
        if (
            isinstance(n.func, ast.Name)
            and n.func.id in {"getattr", "vars"}
            and n.args
            and not n.keywords
        ):
            return ast.copy_location(call("dynamic", n.func, *n.args), n)
        return n

    def visit_Compare(self, n):
        if len(n.ops) == 1 and isinstance(n.ops[0], (ast.In, ast.NotIn)):
            t = self.table(n.comparators[0])
            if t is not None:
                value = call("contains", self.visit(n.left), t)
                if isinstance(n.ops[0], ast.NotIn):
                    value = ast.UnaryOp(ast.Not(), value)
                return ast.copy_location(value, n)
        return self.generic_visit(n)

    def observe_assignment(self, n, target):
        if isinstance(target, ast.Name) and not self.function_depth and target.id in self.o.names:
            scope = ".".join(self.classes)
            field = (scope + "." if scope else "") + target.id
            ctx = call("assignment", ast.Constant(f"{self.module}:{field}"))
        elif (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.attr in self.o.names
            and target.value.id in {"cls", "klass"}
        ):
            ctx = call(
                "class_assignment", ast.Name(target.value.id, ast.Load()), ast.Constant(target.attr)
            )
        else:
            return self.generic_visit(n)
        self.generic_visit(n)
        return ast.copy_location(ast.With([ast.withitem(ctx)], [n]), n)

    def visit_Assign(self, n):
        return self.observe_assignment(n, n.targets[0]) if len(n.targets) == 1 else self.generic_visit(n)

    def visit_AnnAssign(self, n):
        return self.observe_assignment(n, n.target) if n.value is not None else self.generic_visit(n)


class Observer:
    def __init__(self, source, trace):
        self.source = source
        self.trace = trace
        self.names = {"__dict__", "_dispatch", "arg_types"}
        self.declared = set()
        self.module_variables = defaultdict(dict)
        for path in (source / "sqlglot").rglob("*.py"):
            module = ".".join(path.relative_to(source).with_suffix("").parts).removesuffix(".__init__")
            for symbol, name in declared_names(ast.parse(path.read_bytes()), module).items():
                self.declared.add(symbol)
                self.names.add(name)
                if symbol == f"{module}:{name}":
                    self.module_variables[module][name] = symbol
        self.raw = defaultdict(int)
        self.keyed = defaultdict(lambda: defaultdict(int))
        self.classes = defaultdict(int)
        self.modules = defaultdict(int)
        self.derived = defaultdict(set)
        self.initialized = defaultdict(set)
        self.initialized_keys = defaultdict(lambda: defaultdict(set))
        self.copies = defaultdict(int)
        self.assigning = []
        self.loading = []
        self.last_activity = {}

    @contextmanager
    def assignment(self, symbol):
        self.assigning.append(symbol)
        try:
            yield
        finally:
            self.assigning.pop()

    def class_assignment(self, cls, field):
        return self.assignment(label(cls) + "." + field)

    @lru_cache(maxsize=None)
    def owners(self, cls):
        return tuple((label(c), c.__module__) for c in cls.__mro__ if c.__module__.startswith("sqlglot"))

    def activity(self, obj):
        mask = self.trace.active
        if not mask or self.loading:
            return
        cls = obj if isinstance(obj, type) else type(obj)
        if type(cls).__hash__ is None or self.last_activity.get(cls) == mask:
            return
        self.last_activity[cls] = mask
        for owner, module in self.owners(cls):
            accumulate(self.classes, owner, mask)
            accumulate(self.modules, module, mask)

    @lru_cache(maxsize=None)
    def class_symbol(self, cls, name):
        for c in cls.__mro__:
            if name in c.__dict__ and c.__module__.startswith("sqlglot"):
                return label(c) + "." + name
        return None

    def symbol(self, obj, name):
        if isinstance(obj, ModuleType):
            if obj.__name__.startswith("sqlglot"):
                return f"{obj.__name__}:{name}"
            return None
        cls = obj if isinstance(obj, type) else type(obj)
        if type(cls).__hash__ is None:
            return None
        return self.class_symbol(cls, name)

    def read(self, symbol, key=None):
        if not symbol:
            return
        if self.assigning:
            target = self.assigning[-1]
            if symbol != target:
                self.derived[symbol].add(target)
        elif self.loading:
            if key is None:
                self.initialized[symbol].add(self.loading[-1])
            else:
                self.initialized_keys[symbol][key].add(self.loading[-1])
        if self.trace.active and not self.loading:
            if key is None:
                accumulate(self.raw, symbol, self.trace.active)
            else:
                accumulate(self.keyed[symbol], key, self.trace.active)

    def variable(self, symbol, value):
        self.read(symbol)
        return value

    def observe_obj(self, obj, name):
        if name == "__dict__" and isinstance(obj, type) and obj.__module__.startswith("sqlglot"):
            for cls in obj.__mro__:
                for field in cls.__dict__.keys() & self.names:
                    if field != "__dict__":
                        self.read(label(cls) + "." + field)
        else:
            self.read(self.symbol(obj, name))

    def attr(self, obj, name):
        self.observe_obj(obj, name)
        return getattr(obj, name)

    def dynamic(self, fn, *args):
        if fn is vars and len(args) == 1:
            self.observe_obj(args[0], "__dict__")
        elif (
            fn is getattr and len(args) in (2, 3) and isinstance(args[1], str) and args[1] in self.names
        ):
            self.observe_obj(args[0], args[1])
        return fn(*args)

    def table(self, obj, name):
        symbol = self.symbol(obj, "TRANSFORMS" if name == "_dispatch" else name)
        return symbol, getattr(obj, name), obj if name == "__dict__" and isinstance(obj, type) else None

    @staticmethod
    def named_table(symbol, value):
        return symbol, value, None

    def read_entry(self, table, key):
        if table[2] is not None and isinstance(key, str):
            self.read(self.symbol(table[2], key))
        elif table[0]:
            self.read(table[0], key_label(key))

    def item(self, table, key):
        self.read_entry(table, key)
        return table[1][key]

    def get(self, table, key, *default):
        self.read_entry(table, key)
        return table[1].get(key, *default)

    def pop(self, table, key, *default):
        self.read_entry(table, key)
        return table[1].pop(key, *default)

    def dispatch_copy(self, table):
        if self.loading or self.assigning:
            self.read(table[0])
        elif self.trace.active and table[0]:
            accumulate(self.copies, table[0], self.trace.active)
        return dict(table[1])

    def contains(self, key, table):
        self.read_entry(table, key)
        return key in table[1]

    @contextmanager
    def instrument(self):
        loader_type = importlib.machinery.SourceFileLoader
        original_code = loader_type.get_code
        original_exec = loader_type.exec_module

        def get_code(loader, fullname):
            if not self.trace.source_path(loader.path):
                return original_code(loader, fullname)
            tree = ast.parse(loader.get_data(loader.path), filename=loader.path)
            package = fullname if loader.is_package(fullname) else fullname.rpartition(".")[0]
            rewriter = Rewriter(self, fullname, package)
            return compile(
                ast.fix_missing_locations(rewriter.visit(tree)), loader.path, "exec", dont_inherit=True
            )

        def execute(loader, module):
            tracked = self.trace.source_path(loader.path)
            if tracked:
                self.loading.append(module.__name__)
            try:
                original_exec(loader, module)
            finally:
                if tracked:
                    self.loading.pop()

        with ExitStack() as stack:
            stack.enter_context(patch.object(loader_type, "get_code", get_code))
            stack.enter_context(patch.object(loader_type, "exec_module", execute))
            for method in (
                "dispatch_copy",
                "pop",
                "activity",
                "attr",
                "variable",
                "get",
                "item",
                "contains",
                "table",
                "named_table",
                "dynamic",
                "assignment",
                "class_assignment",
            ):
                stack.enter_context(
                    patch.object(builtins, "_rwx_obs_" + method, getattr(self, method), create=True)
                )
            yield


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "tests", "coverage", "output"):
        p.add_argument(name, type=Path)
    args = p.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    warm = json.load(gzip.open(args.coverage))
    trace = TestTrace(root, json.loads(args.tests.read_text())["tests"])
    observer = Observer(root, trace)
    sys.path.insert(0, str(root))
    os.chdir(root)
    os.environ.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", SKIP_INTEGRATION="0")
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    with patch.object(concurrent.futures, "ProcessPoolExecutor", TracedPool), observer.instrument():
        status = pytest.main(
            ["tests", "-q", "--assert=plain", "-p", "no:cacheprovider"], plugins=[trace]
        )
    if status not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        raise SystemExit(status)
    masks = [int(s, 16) for s in warm["sets"]]
    for i, nodeid in enumerate(trace.ids):
        mod = nodeid.split("::")[0][:-3].replace("/", ".")
        observer.modules[mod] |= 1 << i
    for path, lines in warm["files"].items():
        if path.startswith(("sqlglot/", "tests/")):
            mod = path[:-3].replace("/", ".").removesuffix(".__init__")
            for i in set(lines.values()):
                observer.modules[mod] |= masks[i]
    for path, i in warm["reads"].items():
        if path.startswith(("sqlglot/", "tests/")) and path.endswith(".py"):
            mod = path[:-3].replace("/", ".").removesuffix(".__init__")
            observer.modules[mod] |= masks[i]
    pool = {}

    def intern(v):
        return pool.setdefault(v, len(pool))

    data = {
        "ids": trace.ids,
        "declared": sorted(observer.declared),
        "reads": {k: intern(v) for k, v in sorted(observer.raw.items())},
        "keys": {
            k: {item: intern(v) for item, v in sorted(items.items())}
            for k, items in sorted(observer.keyed.items())
        },
        "classes": {k: intern(v) for k, v in sorted(observer.classes.items())},
        "modules": {k: intern(v) for k, v in sorted(observer.modules.items())},
        "derived": {k: sorted(v) for k, v in sorted(observer.derived.items())},
        "initialized": {k: sorted(v) for k, v in sorted(observer.initialized.items())},
        "initialized_keys": {
            k: {key: sorted(mods) for key, mods in sorted(group.items())}
            for k, group in sorted(observer.initialized_keys.items())
        },
        "copies": {k: intern(v) for k, v in sorted(observer.copies.items())},
        "incomplete": intern(
            masks[warm["incomplete"]]
            | trace.failed
            | trace.outside_process
            | (((1 << len(trace.ids)) - 1) ^ trace.passed)
        ),
        "sets": [format(v, "x") for v in pool],
    }
    output.write_bytes(
        gzip.compress(json.dumps(data, separators=(",", ":")).encode(), compresslevel=6, mtime=0)
    )
    print(
        f"Measured {len(observer.raw)} fields, {len(observer.keyed)} keyed fields and {len(observer.derived)} initialization dependencies."
    )


if __name__ == "__main__":
    main()

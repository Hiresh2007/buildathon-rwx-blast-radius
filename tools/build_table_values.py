"""Trace values read from pinned class tables, including the tokenizer's forwarded options."""

import argparse
import ast
import builtins
import concurrent.futures
from contextlib import ExitStack
import gzip
import importlib.machinery
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from table_values import declarations, entries
from tools.build_map import TestTrace, TracedPool


def call(name, *args):
    return ast.Call(ast.Name("_rwx_table_" + name, ast.Load()), list(args), [])


class Rewrite(ast.NodeTransformer):
    def __init__(self, trace, path, tree):
        self.path = path
        self.trace = trace
        self.parents = {
            child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
        }
        self.definitions = {
            node: name for name, node in declarations(tree) if (path, name) in trace.definitions
        }

    def visit_Assign(self, node):
        self.generic_visit(node)
        if node in self.definitions:
            node.value = call(
                "define", ast.Constant(self.path), ast.Constant(self.definitions[node]), node.value
            )
        return node

    visit_AnnAssign = visit_Assign

    def reference(self, node):
        if not isinstance(node.ctx, ast.Load):
            return self.generic_visit(node)
        name = node.id if isinstance(node, ast.Name) else node.attr
        owner = self.parents.get(node)
        if name == "__dict__" and isinstance(node, ast.Attribute):
            # Membership checks only the namespace's attribute names. Keep the
            # original operation so custom namespace behavior still executes.
            if (
                isinstance(owner, ast.Compare)
                and len(owner.ops) == 1
                and isinstance(owner.ops[0], (ast.In, ast.NotIn))
                and owner.comparators[0] is node
            ):
                return self.generic_visit(node)
            return ast.copy_location(call("namespace", self.visit(node.value)), node)
        if name not in self.trace.names:
            return self.generic_visit(node)
        use = owner
        # A short-circuit operand used solely for truth cannot observe a table's values.
        while isinstance(use, ast.BoolOp):
            use = self.parents.get(use)
        if (
            (
                isinstance(use, (ast.If, ast.IfExp, ast.While))
                and use.test is (owner if owner is not use else node)
            )
            or (
                isinstance(use, ast.Call)
                and isinstance(use.func, ast.Name)
                and use.func.id == "bool"
                and len(use.args) == 1
                and not use.keywords
            )
            or (isinstance(owner, ast.UnaryOp) and isinstance(owner.op, ast.Not))
            or (
                isinstance(owner, ast.Compare)
                and len(owner.ops) == 1
                and isinstance(owner.ops[0], (ast.In, ast.NotIn))
                and owner.comparators[0] is node
            )
        ):
            return self.generic_visit(node)
        if (
            isinstance(owner, ast.Attribute) and owner.attr == "keys"
            and isinstance(self.parents.get(owner), ast.Call) and self.parents[owner].func is owner
        ) or (
            isinstance(owner, ast.Call) and isinstance(owner.func, ast.Name)
            and owner.func.id == "len" and len(owner.args) == 1 and not owner.keywords
        ):
            return ast.copy_location(call("shape", self.generic_visit(node)), node)
        # Only these audited direct constructors can store a supplied dict for later lookups.
        if (
            isinstance(owner, ast.keyword)
            and self.path == "sqlglot/tokens.py"
            and (name, owner.arg) in self.trace.forward
        ):
            callee = self.parents.get(owner)
            if (
                isinstance(callee, ast.Call)
                and isinstance(callee.func, ast.Name)
                and callee.func.id == "TokenizerCore"
            ):
                return self.generic_visit(node)
        if isinstance(owner, ast.Assign) and owner.value is node and len(owner.targets) == 1:
            destination = owner.targets[0]
            if (
                isinstance(destination, ast.Name)
                and destination.id in self.trace.names
                and self.inside_function(owner)
            ):
                return self.generic_visit(node)
            if (
                self.path == "sqlglot/tokenizer_core.py"
                and isinstance(destination, ast.Attribute)
                and isinstance(destination.value, ast.Name)
                and destination.value.id == "self"
                and destination.attr == name
            ):
                return self.generic_visit(node)
        # Keep normal lookup/evaluation order. The observer returns the exact original dict.
        return ast.copy_location(call("raw", self.generic_visit(node)), node)

    def inside_function(self, node):
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        return False

    visit_Name = reference
    visit_Attribute = reference

    def tracked(self, node):
        return (
            isinstance(node, ast.Name)
            and node.id in self.trace.names
            or isinstance(node, ast.Attribute)
            and node.attr in self.trace.names
        )

    def visit_Compare(self, node):
        if (len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn))
                and self.tracked(node.comparators[0])):
            # The membership needle must be evaluated before the container.
            tested = call("contains", self.visit(node.left), self.visit(node.comparators[0]))
            if isinstance(node.ops[0], ast.NotIn):
                tested = ast.UnaryOp(ast.Not(), tested)
            return ast.copy_location(tested, node)
        return self.generic_visit(node)

    def visit_Subscript(self, node):
        if (
            self.tracked(node.value)
            and isinstance(node.ctx, ast.Load)
            and not isinstance(node.slice, ast.Slice)
        ):
            return ast.copy_location(
                call("item", self.generic_visit(node.value), self.visit(node.slice)), node
            )
        return self.generic_visit(node)

    def visit_Call(self, node):
        fn = node.func
        if (
            isinstance(fn, ast.Attribute)
            and self.tracked(fn.value)
            and fn.attr == "get"
            and len(node.args) in (1, 2)
            and not node.keywords
            and not any(isinstance(a, ast.Starred) for a in node.args)
        ):
            node.func = ast.copy_location(call("get", self.generic_visit(fn.value)), fn)
            node.args = [self.visit(arg) for arg in node.args]
            return node
        self.generic_visit(node)
        if (
            isinstance(fn, ast.Name)
            and fn.id in ("getattr", "vars")
            and node.args
            and not node.keywords
        ):
            return ast.copy_location(call("reflection", node.func, *node.args), node)
        return node


class Trace(TestTrace):
    def __init__(self, root, tests):
        super().__init__(root, tests)
        self.definitions = set()
        self.names = set()
        self.forward = set()
        self.tables = {}
        self.raw_masks = {}
        self.keys = {}
        self.shapes = {}
        self.members = {}
        self.importing = 0
        self.everyone = (1 << len(self.ids)) - 1
        tokenizer = ast.parse((root / "sqlglot/tokens.py").read_bytes())
        for node in ast.walk(tokenizer):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "TokenizerCore"
            ):
                self.forward.update(
                    (kw.value.attr, kw.arg)
                    for kw in node.keywords
                    if isinstance(kw.value, ast.Attribute)
                )
        for path in sorted((root / "sqlglot").rglob("*.py")):
            local = path.relative_to(root).as_posix()
            for name, declaration in declarations(ast.parse(path.read_bytes())):
                field = name.split(".")[-1]
                if entries(declaration.value) is not None:
                    self.definitions.add((local, name))
                    self.names.add(field)
        self.names.update(alias for field, alias in self.forward if field in self.names)
        # Local aliases are tracked only when every use of that name is instrumented.
        for path in sorted((root / "sqlglot").rglob("*.py")):
            for n in ast.walk(ast.parse(path.read_bytes())):
                if (
                    isinstance(n, ast.Assign)
                    and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)
                    and isinstance(n.value, ast.Attribute)
                    and n.value.attr in self.names
                ):
                    self.names.add(n.targets[0].id)

    def set_context(self, mask, trace_lines):
        self.active, self.trace_lines = mask, trace_lines

    def mask(self):
        return (
            self.everyone if self.importing or self.collecting or not self.active else self.active
        )

    def define(self, path, name, value):
        if type(value) is dict:
            site = (path, name)
            self.tables[id(value)] = (value, site)
        return value

    def mark(self, site):
        self.raw_masks[site] = self.raw_masks.get(site, 0) | self.mask()

    def raw(self, value):
        known = self.tables.get(id(value))
        if known is not None:
            self.mark(known[1])
        return value

    def shape(self, value):
        known = self.tables.get(id(value))
        if known is not None:
            site = known[1]
            self.shapes[site] = self.shapes.get(site, 0) | self.mask()
        return value

    def contains(self, key, value):
        match = self.tables.get(id(value))
        if match is not None:
            if type(key) is str:
                seen = self.members.setdefault(match[1], {})
                seen[key] = seen.get(key, 0) | self.mask()
            else:
                self.shape(value)
        return key in value

    def accessed(self, site, key):
        if type(key) is not str:
            self.mark(site)
            return
        rows = self.keys.setdefault(site, {})
        rows[key] = rows.get(key, 0) | self.mask()

    def get(self, value):
        get = value.get
        match = self.tables.get(id(value))
        if match is None:
            return get

        def observe(key, *default):
            self.accessed(match[1], key)
            return get(key, *default)

        return observe

    def item(self, value, key):
        match = self.tables.get(id(value))
        if match is not None:
            self.accessed(match[1], key)
        return value[key]

    def namespace(self, obj):
        namespace = obj.__dict__
        if isinstance(obj, type) or isinstance(obj, type(sys)):
            for key in self.names.intersection(namespace):
                self.raw(namespace[key])
        return namespace

    def reflection(self, fn, *args):
        value = fn(*args)
        if fn is vars and len(args) == 1:
            if isinstance(args[0], (type, type(sys))):
                for key in self.names.intersection(value):
                    self.raw(value[key])
        elif fn is getattr and len(args) in (2, 3):
            self.raw(value)
        return value

    def collect(self, out):
        loader = importlib.machinery.SourceFileLoader
        normal_code, normal_exec = loader.get_code, loader.exec_module

        def get_code(loader, fullname):
            path = self.source_path(loader.path)
            if not path:
                return normal_code(loader, fullname)
            tree = ast.parse(loader.get_data(loader.path), filename=loader.path)
            return compile(
                ast.fix_missing_locations(Rewrite(self, path, tree).visit(tree)),
                loader.path,
                "exec",
                dont_inherit=True,
            )

        def execute(loader, module):
            self.importing += 1
            try:
                normal_exec(loader, module)
            finally:
                self.importing -= 1

        sys.addaudithook(self.audit)
        with ExitStack() as stack:
            stack.enter_context(patch.object(loader, "get_code", get_code))
            stack.enter_context(patch.object(loader, "exec_module", execute))
            stack.enter_context(patch.object(concurrent.futures, "ProcessPoolExecutor", TracedPool))
            for name in ("define", "namespace", "reflection", "raw", "item", "get", "shape", "contains"):
                stack.enter_context(
                    patch.object(builtins, "_rwx_table_" + name, getattr(self, name), create=True)
                )
            status = pytest.main(
                ["tests", "-q", "--assert=plain", "-p", "no:cacheprovider"], plugins=[self]
            )
        if status != pytest.ExitCode.OK:
            raise RuntimeError("baseline suite must pass")
        files = {}
        for _, (path, name) in self.tables.values():
            site = (path, name)
            raw = self.raw_masks.get(site, 0)
            if raw != self.everyone and site in self.keys:
                files.setdefault(path, {})[name] = (
                    format(raw, "x"),
                    {k: format(m, "x") for k, m in self.keys[site].items()},
                    format(self.shapes.get(site, 0), "x"),
                    {k: format(m, "x") for k, m in self.members.get(site, {}).items()},
                )
        record = dict(
            ids=self.ids,
            complete=format(self.passed & ~self.failed & ~self.outside_process, "x"),
            files=files,
        )
        out.write_bytes(gzip.compress(json.dumps(record, separators=(",", ":")).encode(), mtime=0))
        print(sum(map(len, files.values())), "tables with value-specific observations")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "tests", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    root, target = args.root.resolve(), args.output.resolve()
    trace = Trace(root, json.loads(args.tests.read_bytes())["tests"])
    sys.path.insert(0, str(root))
    os.chdir(root)
    os.environ.update(
        SKIP_INTEGRATION="0",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
    )
    trace.collect(target)

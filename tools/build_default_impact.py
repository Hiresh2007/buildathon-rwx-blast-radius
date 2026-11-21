"""Observe use and introspection of declared scalar defaults on the pinned suite."""

import argparse
import ast
import builtins
import concurrent.futures
import copy
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


def simple_choice(node, arguments):
    if isinstance(node, ast.Name):
        return node.id in arguments
    if isinstance(node, ast.Constant):
        return node.value is None or type(node.value) is bool
    if isinstance(node, ast.IfExp):
        test = node.test
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1
                and isinstance(test.ops[0], (ast.Is, ast.IsNot))):
            return False
        operands = (test.left, test.comparators[0])
        return (any(isinstance(x, ast.Constant) and x.value is None for x in operands)
                and all(simple_choice(x, arguments) for x in operands)
                and simple_choice(node.body, arguments) and simple_choice(node.orelse, arguments))
    return False


class Uses(ast.NodeTransformer):
    """Observe scalar argument values at their original point of consumption."""

    def __init__(self, fn, number, tracked):
        self.number = number
        self.tracked = tracked
        self.parents = {child: n for n in ast.walk(fn) for child in ast.iter_child_nodes(n)}
        arguments = fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs
        assigned = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Del)}
        assigned.update(n.name for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler))
        self.bound = {a.arg for a in arguments} - assigned

    def visit_Name(self, node):
        if not isinstance(node.ctx, ast.Load) or node.id not in self.tracked:
            return node
        parent = self.parents[node]
        i = self.tracked[node.id]
        kind = 0  # any difference in the scalar may be visible
        extra = []
        if (isinstance(parent, (ast.If, ast.IfExp, ast.While)) and parent.test is node
                or isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not)):
            kind = 1
            branch_arguments = self.bound - {node.id}
            if (isinstance(parent, ast.IfExp) and simple_choice(parent.body, branch_arguments)
                    and simple_choice(parent.orelse, branch_arguments)):
                # Both expressions only load bound arguments and compare identities.
                # Returning the very same object makes either truth branch equivalent.
                extra = [copy.deepcopy(parent.body), copy.deepcopy(parent.orelse)]
        elif (isinstance(parent, ast.Compare) and len(parent.ops) == 1
              and isinstance(parent.ops[0], (ast.Is, ast.IsNot))
              and any(isinstance(x, ast.Constant) and x.value is None
                      for x in (parent.left, parent.comparators[0]))):
            kind = 2
        elif isinstance(parent, ast.BoolOp) and parent.values[-1] is not node:
            kind = 3 if isinstance(parent.op, ast.Or) else 4
        flag = ast.Name(f"_rwx_was_omitted_{i}", ast.Load())
        observe = ast.Call(ast.Name("_rwx_default_use", ast.Load()),
                           [ast.Constant(self.number), ast.Constant(i), ast.Constant(kind), node, *extra], [])
        return ast.copy_location(ast.IfExp(flag, observe, node), node)


class Defaults(TestTrace):
    def __init__(self, root, tests):
        super().__init__(root, tests)
        self.everyone = (1 << len(self.ids)) - 1
        self.importing = 0
        self.reflect = {}
        self.sites = {}
        self.records = []
        self.in_frame_hook = False

    def set_context(self, mask, trace_lines):
        self.active, self.trace_lines = mask, trace_lines

    def mask(self):
        return self.everyone if self.importing or self.collecting or not self.active else self.active

    def access(self, event, args):
        if event == "sys._getframe" and not self.in_frame_hook:
            # Consumers can inspect an argument without executing a LOAD_FAST.
            self.in_frame_hook = True
            try:
                frame = args[0]
                while frame is not None:
                    code = frame.f_code
                    path = self.source_path(code.co_filename)
                    if path and path.startswith("sqlglot/"):
                        site = (path, code.co_firstlineno)
                        self.reflect[site] = self.reflect.get(site, 0) | self.mask()
                    frame = frame.f_back
            finally:
                self.in_frame_hook = False
        if event in ("object.__getattr__", "object.__setattr__", "object.__delattr__") and len(args) >= 2:
            obj, attr = args[:2]
            if type(obj) is types.FunctionType and attr in ("__defaults__", "__kwdefaults__"):
                code = obj.__code__
                path = self.source_path(code.co_filename)
                if path and path.startswith("sqlglot/"):
                    site = (path, code.co_firstlineno)
                    self.reflect[site] = self.reflect.get(site, 0) | self.mask()

    def use(self, number, parameter, kind, value, *branches):
        if branches and branches[0] is branches[1]:
            return value
        masks = self.records[number][parameter][3]
        mask = self.mask()
        if masks[kind] & mask != mask:
            masks[kind] |= mask
        return value

    def instrument(self, tree, path):
        for name, fn in functions(tree):
            params = list(parameters(fn))
            if not params:
                continue
            key = path, name
            if key not in self.sites:
                self.sites[key] = (fn.lineno, len(self.records))
                self.records.append([[arg, n.lineno, n.value, [0] * 5] for arg, n in params])
            _, number = self.sites[key]
            # Replace only regular functions' scalar defaults. Inside the body,
            # restore the exact constant before the original first operation.
            # An explicit argument equal to the old default remains distinguishable.
            after_doc = int(
                isinstance(fn.body[0], ast.Expr)
                and isinstance(fn.body[0].value, ast.Constant)
                and isinstance(fn.body[0].value.value, str)
            )
            active = []
            # Closures can outlive this invocation. Reflection and local namespace
            # consumers can bypass the source-level argument loads.
            bindings = {arg: i for i, (arg, _) in enumerate(params)}
            reflective = any(isinstance(n, ast.Name) and n.id in
                             {"locals", "vars", "eval", "exec", "currentframe", "_getframe"}
                             for n in ast.walk(fn))
            indirect = set(bindings) if reflective else set()
            for nested in ast.walk(fn):
                if isinstance(nested, ast.AugAssign) and isinstance(nested.target, ast.Name):
                    # Augmented assignment reads its target without a Name(Load).
                    indirect.add(nested.target.id)
                if nested is not fn and isinstance(nested, (ast.Lambda, ast.ListComp, ast.DictComp,
                        ast.SetComp, ast.GeneratorExp, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    indirect.update(n.id for n in ast.walk(nested) if isinstance(n, ast.Name) and n.id in bindings)
            transform = Uses(fn, number, {arg: i for arg, i in bindings.items() if arg not in indirect})
            fn.body = [transform.visit(statement) for statement in fn.body]
            for i, (arg, original) in enumerate(params):
                replacement = ast.copy_location(ast.Name("_rwx_default_missing", ast.Load()), original)
                for values in (fn.args.defaults, fn.args.kw_defaults):
                    for j, value in enumerate(values):
                        if value is original:
                            values[j] = replacement
                missing = ast.Compare(ast.Name(arg, ast.Load()), [ast.Is()],
                                      [ast.Name("_rwx_default_missing", ast.Load())])
                flag = f"_rwx_was_omitted_{i}"
                active.append(ast.copy_location(ast.Assign([ast.Name(flag, ast.Store())], missing), fn.body[0]))
                restore = ast.Assign([ast.Name(arg, ast.Store())], original)
                statements = [restore]
                if arg in indirect:
                    statements.append(ast.Expr(ast.Call(ast.Name("_rwx_default_use", ast.Load()),
                                      [ast.Constant(number), ast.Constant(i), ast.Constant(0), original], [])))
                active.append(ast.copy_location(ast.If(ast.Name(flag, ast.Load()), statements, []), fn.body[0]))
            fn.body[after_doc:after_doc] = active
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
            patch.object(builtins, "_rwx_default_use", self.use, create=True),
            patch.object(builtins, "_rwx_default_missing", object(), create=True),
            patch.object(concurrent.futures, "ProcessPoolExecutor", TracedPool),
        ):
            return pytest.main(["tests", "-q", "--assert=plain", "-p", "no:cacheprovider"], plugins=[self])

    def save(self, target):
        files = {}
        for (path, name), (line, number) in self.sites.items():
            reflection = self.reflect.get((path, line), 0)
            for arg, line, _, masks in self.records[number]:
                masks[0] |= reflection
                if masks[0] != self.everyone:
                    files.setdefault(path, {})[name + "|" + arg] = (line, [format(v, "x") for v in masks])
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
    if trace.collect() != pytest.ExitCode.OK:
        raise RuntimeError("baseline suite must pass")
    trace.save(target)

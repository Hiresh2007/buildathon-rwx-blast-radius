"""Trace only unchanged SQLGlot schemas and actual baseline validator operands."""

import argparse
import ast
import builtins
import concurrent.futures
from contextlib import ExitStack
from dataclasses import dataclass, field
from functools import lru_cache
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
from arguments import fingerprint, schemas
from build_map import TestTrace, TracedPool

CORE = "sqlglot/expressions/core.py"
CHECK = """\
for k in self.required_args:
    v = self.args.get(k)
    if v is None or (isinstance(v, list) and not v):
        if errors is None:
            errors = []
        errors.append(f"Required keyword: '{k}' missing for {self.__class__}")
"""
INIT = "cls.required_args = {k for k, v in cls.arg_types.items() if v}"
CHECK_AST = ast.dump(ast.parse(CHECK).body[0])
INIT_AST = ast.dump(ast.parse(INIT).body[0])
PLAIN_VALUES = {id(cls) for cls in (str, bool, int, float, bytes, tuple, dict, list)}
ATTRIBUTES = {"arg_types", "required_args"}


def call(name, *args):
    return ast.Call(ast.Name("_rwx_arguments_" + name, ast.Load()), list(args), [])


class Reads(ast.NodeTransformer):
    def __init__(self, tree, path):
        self.parent = {
            child: n for n in ast.walk(tree) for child in ast.iter_child_nodes(n)
        }
        self.safe = {}
        if path == CORE:
            for cls in tree.body:
                if not isinstance(cls, ast.ClassDef):
                    continue
                for fn in cls.body:
                    if not isinstance(fn, ast.FunctionDef):
                        continue
                    for node in ast.walk(fn):
                        if (
                            (cls.name, fn.name) == ("Expr", "__init_subclass__")
                            and isinstance(node, ast.Assign)
                            and ast.dump(node) == INIT_AST
                        ):
                            self.safe.update(
                                (item, "init")
                                for item in ast.walk(node.value)
                                if isinstance(item, ast.Attribute)
                                and item.attr == "arg_types"
                            )
                        if (
                            (cls.name, fn.name) == ("Expression", "error_messages")
                            and isinstance(node, ast.For)
                            and ast.dump(node) == CHECK_AST
                        ):
                            self.safe[node.iter] = "validate"

    def visit_Attribute(self, node):
        parent = self.parent.get(node)
        if not isinstance(node.ctx, ast.Load) or node.attr not in {
            "arg_types",
            "required_args",
            "__dict__",
        }:
            return self.generic_visit(node)
        mode = self.safe.get(node, "raw")
        consumer = ast.Constant(None)
        if node.attr == "arg_types" and mode == "raw":
            if isinstance(parent, (ast.For, ast.comprehension)) and parent.iter is node:
                mode = "keys"
            elif (
                isinstance(parent, ast.Compare)
                and len(parent.ops) == 1
                and isinstance(parent.ops[0], (ast.In, ast.NotIn))
                and parent.comparators[0] is node
            ):
                mode = "keys"
            elif (
                isinstance(parent, ast.Call)
                and isinstance(parent.func, ast.Name)
                and parent.func.id in {"len", "list", "tuple", "set", "zip"}
                and node in parent.args
            ):
                mode, consumer = "call", ast.Name(parent.func.id, ast.Load())
        value = self.visit(node.value)
        return ast.copy_location(
            call("read", value, ast.Constant(node.attr), ast.Constant(mode), consumer),
            node,
        )

    def visit_Call(self, node):
        self.generic_visit(node)
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "vars", "setattr", "delattr"}
            and node.args
            and not node.keywords
        ):
            node.args.insert(0, node.func)
            node.func = ast.Name("_rwx_arguments_reflect", ast.Load())
        return node


@lru_cache(maxsize=1024)
def argument_slot(cls):
    mro = type.__getattribute__(cls, "__mro__")
    if type.__getattribute__(cls, "__getattribute__") is not object.__getattribute__:
        return None
    return next((vars(b)["args"] for b in mro if "args" in vars(b)), None)


@lru_cache(maxsize=1024)
def usual_nonlist(cls):
    mro = type.__getattribute__(cls, "__mro__")
    return (
        not any(base is list for base in mro)
        and type.__getattribute__(cls, "__getattribute__") is object.__getattribute__
        and all("__class__" not in vars(base) for base in mro if base is not object)
    )


def concrete(cls):
    return (
        type.__getattribute__(cls, "__module__")
        + ":"
        + type.__getattribute__(cls, "__qualname__")
    )


@dataclass
class Uses:
    observed: int = 0
    raw: int = 0
    multiple_errors: int = 0
    missing: dict[str, int] = field(default_factory=dict)


class Trace(TestTrace):
    def __init__(self, source, tests):
        super().__init__(source, tests)
        self.everyone = (1 << len(self.ids)) - 1
        self.importing = 0
        self.instrumented = {}
        self.untracked = 0
        self.definitions = {}
        self.sources = {}
        self.results = {}
        for path in (source / "sqlglot/expressions").glob("*.py"):
            module = ".".join(path.relative_to(source).with_suffix("").parts)
            tree = ast.parse(path.read_bytes())
            self.sources[path.relative_to(source).as_posix()] = fingerprint(
                ast.dump(tree)
            )
            for name, _, fields in schemas(tree):
                self.definitions[module + ":" + name] = fields

    def set_context(self, mask, trace_lines):
        self.active, self.trace_lines = mask, trace_lines

    def mask(self):
        return (
            self.everyone
            if self.importing or self.collecting or not self.active
            else self.active
        )

    def remember(self, code):
        self.instrumented[id(code)] = code
        for child in code.co_consts:
            if isinstance(child, types.CodeType):
                self.remember(child)
        return code

    def entered(self, code, offset):
        # Generated Python and dependencies outside the source loader can also
        # consume the schema. Retain any test that reached an uninstrumented site.
        if id(code) in self.instrumented or code.co_filename == __file__:
            return self.monitor.DISABLE
        if ATTRIBUTES.intersection(code.co_names) or any(
            type(value) is str and value in ATTRIBUTES for value in code.co_consts
        ):
            self.untracked |= self.mask()
            return None
        return self.monitor.DISABLE

    def audit(self, event, args):
        if event == "subprocess.Popen":
            self.outside_process |= self.mask()
        super().audit(event, args)

    @lru_cache(maxsize=4096)
    def owner(self, cls):
        return self.find_owner(cls)

    def find_owner(self, cls):
        mro = type.__getattribute__(cls, "__mro__")
        for base in mro:
            ns = type.__getattribute__(base, "__dict__")
            if "arg_types" not in ns:
                continue
            name = concrete(base)
            fields = self.definitions.get(name)
            if fields is None:
                return None
            if name not in self.results:
                self.results[name] = Uses()
            return self.results[name], fields, ns["arg_types"]
        return None

    def observe(self, obj, mode, consumer, value):
        cls = obj if issubclass(type(obj), type) else type(obj)
        item = self.owner(cls) if type(cls) is type else self.find_owner(cls)
        if item is None:
            return
        counts, flags, original = item
        mask = self.mask()
        positive = self.active if not self.importing and not self.collecting else 0
        if type(cls) is not type or type(original) is not dict:
            counts.raw |= mask
            return
        if mode == "init":
            # Other class construction hooks can cache the contents independently.
            hooks = [
                concrete(b)
                for b in cls.__mro__
                if "__init_subclass__" in vars(b) and b is not object
            ]
            if (
                hooks == ["sqlglot.expressions.core:Expr"]
                and value is original
                and all(type(k) is str and type(v) is bool for k, v in value.items())
                and list(value.items()) == list(flags.items())
            ):
                return
        if counts.raw & mask == mask:
            return
        if mode == "call" and any(consumer is f for f in (len, list, tuple, set, zip)):
            mode = "keys"
        if mode == "keys" and value is original:
            counts.observed |= positive
            return
        if (
            mode == "validate"
            and type(value) is set
            and all(type(k) is str for k in value)
            and value == {k for k, v in flags.items() if v}
        ):
            descriptor = argument_slot(cls)
            if type(descriptor) is types.MemberDescriptorType:
                args = descriptor.__get__(obj)
                if type(args) is dict and all(type(k) is str for k in args):
                    missing = []
                    for field in flags:
                        v = args.get(field)
                        if v is None or (type(v) is list and not v):
                            missing.append(field)
                        elif id(type(v)) not in PLAIN_VALUES:
                            if type(type(v)) is not type or not usual_nonlist(type(v)):
                                break
                    else:
                        counts.observed |= positive
                        # Changing the set of required keys can reorder multiple
                        # preexisting error messages, even if the changed field exists.
                        if sum(flags[f] for f in missing) >= 2:
                            counts.multiple_errors |= mask
                        for field in missing:
                            counts.missing[field] = counts.missing.get(field, 0) | mask
                        return
        counts.raw |= mask

    def read(self, obj, name, mode, consumer):
        value = getattr(obj, name)
        self.observe(obj, mode, consumer, value)
        return value

    def reflect(self, fn, *args):
        if fn is vars and len(args) == 1 and isinstance(args[0], type):
            self.observe(args[0], "raw", None, None)
        elif (
            any(fn is f for f in (getattr, setattr, delattr))
            and len(args) >= 2
            and type(args[1]) is str
            and args[1] in {"arg_types", "required_args", "__dict__"}
        ):
            self.observe(args[0], "raw", None, None)
        return fn(*args)

    def collect(self):
        loader = importlib.machinery.SourceFileLoader
        original_code, original_exec = loader.get_code, loader.exec_module

        def code(source_loader, name):
            path = self.source_path(source_loader.path)
            if path is None:
                return original_code(source_loader, name)
            tree = ast.parse(
                source_loader.get_data(source_loader.path), filename=source_loader.path
            )
            return self.remember(
                compile(
                    ast.fix_missing_locations(Reads(tree, path).visit(tree)),
                    source_loader.path,
                    "exec",
                    dont_inherit=True,
                )
            )

        def execute(source_loader, module):
            self.importing += 1
            try:
                return original_exec(source_loader, module)
            finally:
                self.importing -= 1

        sys.addaudithook(self.audit)
        with ExitStack() as context:
            self.monitor.use_tool_id(self.tool, "schema-arguments")
            context.callback(self.monitor.free_tool_id, self.tool)
            context.callback(self.monitor.set_events, self.tool, 0)
            self.monitor.register_callback(
                self.tool, self.monitor.events.PY_START, self.entered
            )
            self.monitor.set_events(self.tool, self.monitor.events.PY_START)
            for obj, key, value in [
                (loader, "get_code", code),
                (loader, "exec_module", execute),
                (concurrent.futures, "ProcessPoolExecutor", TracedPool),
            ]:
                context.enter_context(patch.object(obj, key, value))
            for name, method in [("read", self.read), ("reflect", self.reflect)]:
                context.enter_context(
                    patch.object(
                        builtins, "_rwx_arguments_" + name, method, create=True
                    )
                )
            return pytest.main(
                ["tests", "-q", "--assert=plain", "-p", "no:cacheprovider"],
                plugins=[self],
            )

    def write(self, out):
        complete = self.passed & ~self.failed & ~self.outside_process & ~self.untracked
        classes = {
            name: [
                format(use.observed, "x"),
                format(use.raw, "x"),
                format(use.multiple_errors, "x"),
                {key: format(bits, "x") for key, bits in use.missing.items()},
            ]
            for name, use in self.results.items()
            if use.observed & ~use.raw & complete
        }
        data = {
            "ids": self.ids,
            "sources": self.sources,
            "complete": format(complete, "x"),
            "classes": classes,
        }
        out.write_bytes(
            gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0)
        )
        print(
            len(classes),
            "usable schema owners;",
            out.stat().st_size,
            "compressed bytes",
        )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ["source", "manifest", "output"]:
        p.add_argument(key, type=Path)
    args = p.parse_args()
    root = args.source.resolve()
    out = args.output.resolve()
    trace = Trace(root, json.loads(args.manifest.read_bytes())["tests"])
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["PATH"] = (
        str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    )
    os.environ.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", SKIP_INTEGRATION="0")
    if trace.collect() not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        raise RuntimeError("baseline failed to collect")
    trace.write(out)


if __name__ == "__main__":
    main()

"""Record which tests only compare None with operands whose equality is identity."""
import ast
import builtins
import concurrent.futures
import gzip
import importlib.machinery
import inspect
import json
import os
import sys
import textwrap
import tracemalloc
import types
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ast_syntax import same_syntax
from none_comparisons import singleton_operand
from tools.build_map import TestTrace, TracedPool


BUILTIN_TYPES = (object, type(None), bool, int, float, complex, str, bytes,
                 bytearray, list, tuple, dict, set, frozenset, range)
BUILTIN_TYPE_IDS = {id(cls) for cls in BUILTIN_TYPES}
SAFE_EQ = {id(cls.__eq__) for cls in BUILTIN_TYPES}
SAFE_NE = {id(cls.__ne__) for cls in BUILTIN_TYPES}
MISSING = object()


@lru_cache(maxsize=512)
def has_type_guard(function, code):
    """A different receiver type makes this exact Python method return False."""
    if code.co_freevars or "type" in code.co_varnames or "__wrapped__" in function.__dict__:
        return False
    try:
        definition = ast.parse(textwrap.dedent(inspect.getsource(function))).body[0]
    except (OSError, TypeError, SyntaxError):
        return False
    if not isinstance(definition, ast.FunctionDef) or definition.decorator_list:
        return False
    if len(definition.body) != 1 or not isinstance(definition.body[0], ast.Return):
        return False
    parameters = definition.args.posonlyargs + definition.args.args
    if len(parameters) != 2:
        return False
    a, b = (argument.arg for argument in parameters)
    result = definition.body[0].value
    if not isinstance(result, ast.BoolOp) or not isinstance(result.op, ast.Or) or len(result.values) != 2:
        return False
    first, tail = result.values
    if not isinstance(tail, ast.BoolOp) or not isinstance(tail.op, ast.And):
        return False
    identity = ast.parse(f"{a} is {b}", mode="eval").body
    same_type = ast.parse(f"type({a}) is type({b})", mode="eval").body
    return same_syntax(first, identity) and same_syntax(tail.values[0], same_type)


def safe_types(cls):
    if id(cls) in BUILTIN_TYPE_IDS:
        return True, True
    if type(cls) is not type:
        return False, False
    # Look up both current special methods without invoking descriptors or
    # caching a mutable class's special-method resolution across observations.
    eq = ne = MISSING
    mro = type.__getattribute__(cls, "__mro__")
    for base in mro:
        namespace = type.__getattribute__(base, "__dict__")
        if eq is MISSING and "__eq__" in namespace:
            eq = namespace["__eq__"]
        if ne is MISSING and "__ne__" in namespace:
            ne = namespace["__ne__"]
        if eq is not MISSING and ne is not MISSING:
            break
    safe_eq = id(eq) in SAFE_EQ and any(base is eq.__objclass__ for base in mro)
    if not safe_eq and type(eq) is types.FunctionType:
        binding = eq.__globals__.get("type", eq.__builtins__.get("type"))
        safe_eq = binding is type and has_type_guard(eq, eq.__code__)
    # object.__ne__ delegates to __eq__. The other supported built-in
    # comparisons reject the unrelated None type without inspecting elements.
    safe_ne = id(ne) in SAFE_NE and any(base is ne.__objclass__ for base in mro)
    return safe_eq, safe_eq and safe_ne


class Rewriter(ast.NodeTransformer):
    def __init__(self, trace, path):
        self.trace = trace
        self.path = path

    def visit_Compare(self, node):
        operand = singleton_operand(node)
        self.generic_visit(node)
        if operand is None:
            return node
        key = self.path, f"{node.lineno}:{node.col_offset}"
        site = self.trace.registry.setdefault(key, len(self.trace.registry))
        if site == len(self.trace.records):
            self.trace.records.append([node.end_lineno, 0, 0, 0])
        right = node.comparators[0]
        operand_on_left = isinstance(right, ast.Constant) and right.value is None
        operand = node.left if operand_on_left else right
        call = ast.Call(ast.Name("_rwx_none_operand", ast.Load()), [ast.Constant(site), operand], [])
        if operand_on_left:
            node.left = ast.copy_location(call, operand)
        else:
            node.comparators[0] = ast.copy_location(call, operand)
        return node


class Trace(TestTrace):
    rewriter = Rewriter
    hook = "_rwx_none_operand"

    def __init__(self, root, tests):
        super().__init__(root, tests)
        self.registry = {}
        self.records = []
        self.importing = 0
        self.metered = set()
        self.everyone = (1 << len(self.ids)) - 1

    def set_context(self, mask, trace_lines):
        self.active = mask
        self.trace_lines = trace_lines

    def observe(self, site, value):
        mask = self.everyone if self.importing or self.collecting or not self.active else self.active
        if tracemalloc.is_tracing():
            self.metered.add(mask)
            return value
        row = self.records[site]
        row[1] |= mask
        eq, ne = safe_types(type(value))
        if not eq:
            row[2] |= mask
        if not ne:
            row[3] |= mask
        return value

    def run(self, destination):
        loader = importlib.machinery.SourceFileLoader
        default_code, default_exec = loader.get_code, loader.exec_module

        def instrument(loader, name):
            path = self.source_path(loader.path)
            if not path or not path.startswith("sqlglot/"):
                return default_code(loader, name)
            tree = ast.parse(loader.get_data(loader.path), filename=loader.path)
            tree = ast.fix_missing_locations(self.rewriter(self, path).visit(tree))
            return compile(tree, loader.path, "exec", dont_inherit=True)

        def loading(loader, module):
            self.importing += 1
            try:
                return default_exec(loader, module)
            finally:
                self.importing -= 1

        sys.addaudithook(self.audit)
        with (
            patch.object(loader, "get_code", instrument),
            patch.object(loader, "exec_module", loading),
            patch.object(builtins, self.hook, self.observe, create=True),
            patch.object(concurrent.futures, "ProcessPoolExecutor", TracedPool),
        ):
            status = pytest.main(["tests", "-q", "--assert=plain", "-p", "no:cacheprovider"], plugins=[self])
        if status not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
            raise ValueError("incomplete baseline collection")
        self.save(destination)

    def complete_tests(self):
        complete = self.passed & ~self.failed & ~self.outside_process
        for mask in self.metered:
            complete &= ~mask
        return complete

    def save(self, destination):
        complete = self.complete_tests()
        files = {}
        for (path, key), site in self.registry.items():
            end, seen, unsafe_eq, unsafe_ne = self.records[site]
            if seen & ~(unsafe_eq & unsafe_ne):
                masks = [format(x, "x") for x in (seen, unsafe_eq, unsafe_ne)]
                files.setdefault(path, {})[key] = [end, *masks]
        data = {"ids": self.ids, "complete": format(complete, "x"), "files": files}
        destination.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
        print(sum(map(len, files.values())), "comparison sites,", complete.bit_count(), "complete tests")


if __name__ == "__main__":
    root, tests, destination = (Path(arg).resolve() for arg in sys.argv[1:])
    trace = Trace(root, json.loads(tests.read_bytes())["tests"])
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.update(SKIP_INTEGRATION="0", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                      PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    trace.run(destination)

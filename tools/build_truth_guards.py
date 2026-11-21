"""Record receiver states in simple truth and singleton guards."""
import ast
import gzip
import json
import os
import sys
import tracemalloc
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.build_none_comparisons import BUILTIN_TYPE_IDS, Trace
from truth_guards import guard_form


IMMUTABLE_TRUTHY = {id(cls) for cls in (types.FunctionType, types.MethodType,
                                         types.BuiltinFunctionType, types.ModuleType, type, object)}


def state(value):
    if value is None:
        return 2
    if value is False:
        return 3
    if value is True:
        return 4
    cls = type(value)
    if id(cls) in BUILTIN_TYPE_IDS:
        return int(bool(value))
    if id(cls) in IMMUTABLE_TRUTHY:
        return 1
    if type(cls) is not type:
        return None
    for base in type.__getattribute__(cls, "__mro__"):
        namespace = type.__getattribute__(base, "__dict__")
        if "__bool__" in namespace or "__len__" in namespace:
            return None
    return 1


class Rewriter(ast.NodeTransformer):
    def __init__(self, trace, path):
        self.trace = trace
        self.path = path

    def guard(self, node):
        self.generic_visit(node)
        form = guard_form(node.test)
        if form is None:
            return node
        kind, operand = form
        key = self.path, f"{node.test.lineno}:{node.test.col_offset}"
        site = self.trace.registry.setdefault(key, len(self.trace.registry))
        if site == len(self.trace.records):
            self.trace.records.append([node.test.end_lineno, 0, 0, 0, 0, 0, 0, 0])
        wrapper = ast.copy_location(
            ast.Call(ast.Name("_rwx_guard_operand", ast.Load()), [ast.Constant(site), operand], []), operand)
        if kind == "truth":
            node.test = wrapper
        elif kind == "not":
            node.test.operand = wrapper
        elif node.test.left is operand:
            node.test.left = wrapper
        else:
            node.test.comparators[0] = wrapper
        return node

    visit_If = visit_While = visit_IfExp = guard


class GuardTrace(Trace):
    rewriter = Rewriter
    hook = "_rwx_guard_operand"

    def observe(self, site, value):
        mask = self.everyone if self.importing or self.collecting or not self.active else self.active
        if tracemalloc.is_tracing():
            self.metered.add(mask)
            return value
        row = self.records[site]
        row[1] |= mask
        if row[2] & mask != mask:
            value_state = state(value)
            if value_state is None:
                row[2] |= mask
            else:
                row[3 + value_state] |= mask
        return value

    def save(self, destination):
        files = {}
        for (path, key), site in self.registry.items():
            end, seen, unknown, *states = self.records[site]
            if seen & ~unknown:
                files.setdefault(path, {})[key] = [end, *[format(i, "x") for i in (seen, unknown, *states)]]
        complete = self.complete_tests()
        data = {"ids": self.ids, "complete": format(complete, "x"), "files": files}
        destination.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
        print(sum(map(len, files.values())), "conditional sites,", complete.bit_count(), "complete tests")


if __name__ == "__main__":
    root, tests, destination = (Path(arg).resolve() for arg in sys.argv[1:])
    trace = GuardTrace(root, json.loads(tests.read_bytes())["tests"])
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.update(SKIP_INTEGRATION="0", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                      PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    trace.run(destination)

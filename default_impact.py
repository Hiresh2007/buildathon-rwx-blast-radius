"""Selection for an isolated edit to a scalar default of a regular Python function."""

import ast
import gzip
import json


def functions(tree):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            for method in node.body:
                if isinstance(method, ast.FunctionDef):
                    yield node.name + "." + method.name, method


def parameters(fn):
    if (
        fn.decorator_list
        or fn.name.startswith("__")
        or any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(fn))
    ):
        return
    args = fn.args
    pos = args.posonlyargs + args.args
    annotated = list(zip(pos[len(pos) - len(args.defaults) :], args.defaults)) + list(
        zip(args.kwonlyargs, args.kw_defaults)
    )
    for arg, value in annotated:
        if isinstance(value, ast.Constant) and type(value.value) in (bool, int, str, type(None)):
            yield arg.arg, value


class DefaultIndex:
    def __init__(self, path, ids):
        with gzip.open(path, "rt") as stream:
            raw = json.load(stream)
        if raw["ids"] != ids:
            raise ValueError("default inventory differs")
        self.complete = int(raw["complete"], 16)
        self.files = raw["files"]
        self.lines = {}
        for name, entries in self.files.items():
            self.lines[name] = set()
            for tag, (line, mask) in entries.items():
                self.lines[name].add(line)
                entries[tag] = int(mask, 16)

    def select(self, path, source, candidate, replaced, inserted, index):
        if inserted or not replaced or not (replaced & self.lines.get(path, set())):
            return None
        before = {
            f"{name}|{arg}": (val, fn.lineno)
            for name, fn in functions(source.tree)
            for arg, val in parameters(fn)
            if val.lineno in replaced
        }
        for name, fn in functions(candidate):
            for arg, new in parameters(fn):
                tag = f"{name}|{arg}"
                if tag not in before or tag not in self.files[path]:
                    continue
                old, definition_line = before[tag]
                if old.value == new.value:
                    continue
                if type(new.value) is not type(old.value) and new.value is not None and old.value is not None:
                    continue
                original = new.value
                new.value = old.value
                try:
                    if ast.dump(candidate) != source.syntax:
                        continue
                finally:
                    new.value = original
                cold = index.isolated.get(path, index.all_bits) | index.unknown
                executions = index.execution
                child = executions.child_calls.get(f"{path}|{definition_line}:{name}", 0)
                unseen = (
                    ~self.complete | index.incomplete | (executions.untraced & ~executions.child_complete)
                ) & cold
                return (
                    self.files[path][tag]
                    | child
                    | unseen
                    | index.reads.get(path, 0)
                    | index.execution.child_reads.get(path, 0)
                )
        return None

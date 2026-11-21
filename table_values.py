"""Select consumers of changed values in an otherwise identical literal configuration table."""

import ast
import gzip
import json

from execution import constant, immutable_shape


def declarations(tree, scope=""):
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            yield from declarations(node, scope + node.name + ".")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if (
                len(targets) == 1
                and isinstance(targets[0], ast.Name)
                and targets[0].id.isupper()
                and isinstance(node.value, ast.Dict)
            ):
                yield scope + targets[0].id, node


def entries(node):
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None
    if (
        type(value) is not dict
        or not value
        or len(value) != len(node.keys)
        or not all(type(key) is str for key in value)
        or not all(immutable_shape(x, x) for x in value.values())
    ):
        return None
    return value


class TableValues:
    def __init__(self, path, ids):
        with gzip.open(path, "rt") as stream:
            data = json.load(stream)
        if data["ids"] != ids:
            raise ValueError("configuration table inventory differs")
        self.complete = int(data["complete"], 16)
        self.files = data["files"]

    def select(self, path, source, candidate, replaced, inserted, index):
        if inserted or not replaced or path not in self.files:
            return None
        current = self.files[path]
        originals = dict(declarations(source.tree))
        for name, revised in declarations(candidate):
            if name not in current or name not in originals:
                continue
            original = originals[name]
            if not all(original.lineno <= n <= original.end_lineno for n in replaced):
                continue
            before, after = entries(original.value), entries(revised.value)
            if before is None or after is None or list(before) != list(after):
                continue
            keys = [key for key in before if constant(before[key]) != constant(after[key])]
            if not keys or not all(immutable_shape(before[k], after[k]) for k in keys):
                continue
            saved = revised.value
            revised.value = original.value
            try:
                if ast.dump(candidate) != source.syntax:
                    continue
            finally:
                revised.value = saved
            whole, keyed = current[name]
            cold = index.isolated.get(path, index.all_bits) | index.unknown
            selected = int(whole, 16) | ((~self.complete | index.incomplete) & cold)
            for key in keys:
                selected |= int(keyed.get(key, "0"), 16)
            return selected | index.reads.get(path, 0) | index.execution.child_reads.get(path, 0)
        return None

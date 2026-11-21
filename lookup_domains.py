"""Bounded observations of built-in keyed lookups in unchanged SQLGlot tests."""

import ast
import gzip
import json


def lookup(node):
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and len(node.args) in (1, 2)
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and type(node.args[0].value) is str
        and not any(isinstance(a, ast.Starred) for a in node.args)
    ):
        return "get", node.args[0]
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], (ast.In, ast.NotIn))
        and isinstance(node.left, ast.Constant)
        and type(node.left.value) is str
    ):
        return "membership", node.left
    return None


def site(node, key):
    return f"{node.lineno}:{node.col_offset}:{key.lineno}:{key.col_offset}"


class LookupDomains:
    def __init__(self, path, ids):
        with gzip.open(path, "rt") as stream:
            data = json.load(stream)
        if data["ids"] != ids:
            raise ValueError("lookup trace uses another inventory")
        self.complete = int(data["complete"], 16)
        self.files = data["files"]
        self.lines = {}
        for name, locations in self.files.items():
            self.lines[name] = set()
            for tag, (span, observed, unknown, patterns) in locations.items():
                self.lines[name].update(range(int(tag.split(":")[0]), span + 1))
                locations[tag] = (
                    int(observed, 16),
                    int(unknown, 16),
                    [(frozenset(keys), int(mask, 16)) for keys, mask in patterns],
                )

    def exemptions(self, path, source, candidate, replaced, inserted):
        if inserted or not replaced or not replaced.intersection(self.lines.get(path, ())):
            return 0
        locations = self.files[path]
        edits = {}
        for old in ast.walk(source.tree):
            desc = lookup(old)
            if desc and any(old.lineno <= line <= old.end_lineno for line in replaced):
                tag = site(old, desc[1])
                if tag in locations:
                    edits[tag] = old
        for new in ast.walk(candidate):
            desc = lookup(new)
            if not desc:
                continue
            kind, key = desc
            tag = site(new, key)
            old = edits.get(tag)
            if old is None or lookup(old)[0] != kind:
                continue
            before = lookup(old)[1].value
            after = key.value
            changed = key
            if before == after:
                if (
                    kind != "get"
                    or len(new.args) != 2
                    or len(old.args) != 2
                    or not isinstance(new.args[1], ast.Constant)
                    or not isinstance(old.args[1], ast.Constant)
                ):
                    continue
                changed = new.args[1]
                if type(changed.value) is not type(old.args[1].value) or changed.value == old.args[1].value:
                    continue
                restore = old.args[1].value
            else:
                restore = before
            # Prove that this one scalar operand accounts for the entire patch.
            original = changed.value
            changed.value = restore
            try:
                if ast.dump(candidate) != source.syntax:
                    continue
            finally:
                changed.value = original
            observed, unknown, samples = locations[tag]
            safe = self.complete & observed & ~unknown
            for keys, mask in samples:
                if before == after:
                    equal = before in keys
                elif kind == "get":
                    equal = before not in keys and after not in keys
                else:
                    equal = (before in keys) == (after in keys)
                if not equal:
                    safe &= ~mask
            return safe
        return 0

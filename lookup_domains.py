"""Bounded observations of built-in keyed lookups in unchanged SQLGlot tests."""

import ast
from ast_syntax import same_syntax
import gzip
import json


def string_collection(node):
    if (isinstance(node, (ast.Tuple, ast.List, ast.Set)) and len(node.elts) <= 32
            and all(isinstance(item, ast.Constant) and type(item.value) is str for item in node.elts)):
        return frozenset(item.value for item in node.elts)
    return None


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
    if (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn))
            and string_collection(node.comparators[0]) is not None):
        return "literal_members", node.comparators[0]
    return None


def site(node, key):
    return f"{node.lineno}:{node.col_offset}:{key.lineno}:{key.col_offset}"


class LookupDomains:
    def __init__(self, path, ids, membership=None):
        with gzip.open(path, "rt") as stream:
            data = json.load(stream)
        if data["ids"] != ids:
            raise ValueError("lookup trace uses another inventory")
        self.complete = int(data["complete"], 16)
        self.files = data["files"]
        if membership is not None:
            with gzip.open(membership, "rt") as stream:
                extra = json.load(stream)
            if extra["ids"] != ids:
                raise ValueError("membership trace uses another inventory")
            self.complete &= int(extra["complete"], 16)
            for name, locations in extra["files"].items():
                self.files.setdefault(name, {}).update(locations)
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
            if kind == "literal_members":
                before = old.comparators[0]
                changed_keys = string_collection(before) ^ string_collection(key)
                new.comparators[0] = before
                try:
                    if not same_syntax(candidate, source.tree):
                        continue
                finally:
                    new.comparators[0] = key
                seen, unknown, values = locations[tag]
                safe = self.complete & seen & ~unknown
                for needles, mask in values:
                    if not needles.isdisjoint(changed_keys):
                        safe &= ~mask
                return safe
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
                if not same_syntax(candidate, source.tree):
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

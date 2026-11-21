"""Select tests affected by a required/optional flag on a literal Expr argument schema."""

import ast
from ast_syntax import same_syntax
import gzip
import hashlib
import json


def schemas(tree, class_name=None, span=None):
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef) or cls.decorator_list or cls.keywords:
            continue
        if class_name is not None and cls.name != class_name:
            continue
        if span is not None and (cls.lineno > span[0] or cls.end_lineno < span[1]):
            continue
        for stmt in cls.body:
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                continue
            if span is not None and (
                stmt.lineno > span[0] or stmt.end_lineno < span[1]
            ):
                continue
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            value = stmt.value
            if not (
                len(targets) == 1
                and isinstance(targets[0], ast.Name)
                and targets[0].id == "arg_types"
                and isinstance(value, ast.Dict)
            ):
                continue
            if not all(
                isinstance(k, ast.Constant) and type(k.value) is str for k in value.keys
            ):
                continue
            if not all(
                isinstance(v, ast.Constant) and type(v.value) is bool
                for v in value.values
            ):
                continue
            fields = {k.value: v.value for k, v in zip(value.keys, value.values)}
            if len(fields) == len(value.keys):
                yield cls.name, stmt, fields


def fingerprint(syntax):
    return hashlib.sha256(syntax.encode("utf-8")).hexdigest()


class ArgumentIndex:
    def __init__(self, filename, ids):
        with gzip.open(filename, "rt") as stream:
            data = json.load(stream)
        self.classes = data["classes"] if data["ids"] == ids else {}
        self.sources = data["sources"]
        self.complete = int(data["complete"], 16)

    def exemptions(self, path, old, new, replaced, inserted, baseline_dump=None):
        """Return only completed test bits with a positive, unaffected observation."""
        if inserted or not replaced or not path.startswith("sqlglot/expressions/"):
            return 0
        module = path[:-3].replace("/", ".")
        for name, stmt, before in schemas(old, span=(min(replaced), max(replaced))):
            key = module + ":" + name
            if key not in self.classes:
                continue
            revisions = list(schemas(new, class_name=name))
            if len(revisions) != 1:
                continue
            _, revised, after = revisions[0]
            if list(before) != list(after):
                continue
            fields = [field for field in before if before[field] != after[field]]
            if not fields:
                continue
            declarations = [
                n for n in old.body if isinstance(n, ast.ClassDef) and n.name == name
            ]
            if len(declarations) != 1:
                continue
            names = [n for n in ast.walk(declarations[0]) if isinstance(n, ast.Name)]
            # Other bare references can consume flags while the class namespace is
            # still being constructed, before attribute instrumentation can see it.
            if sum(n.id == "arg_types" for n in names) != 1 or any(
                n.id in {"locals", "vars", "eval", "exec"} for n in names
            ):
                continue
            syntax = baseline_dump if baseline_dump is not None else ast.dump(old)
            if fingerprint(syntax) != self.sources.get(path):
                continue
            original = revised.value
            revised.value = stmt.value
            try:
                if not same_syntax(old, new):
                    continue
            finally:
                revised.value = original
            eligible, raw, ambiguous, missing = self.classes[key]
            danger = int(raw, 16) | int(ambiguous, 16)
            for field in fields:
                danger |= int(missing.get(field, "0"), 16)
            return int(eligible, 16) & self.complete & ~danger
        return 0

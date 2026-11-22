"""Recognize module-owned regular expressions used only by deferred searches."""
import ast
from functools import lru_cache
import re


_METHODS = {"match", "search", "fullmatch", "findall", "finditer", "split", "sub", "subn"}
_DYNAMIC = {"globals", "locals", "vars", "eval", "exec", "__getattr__", "__dict__"}


def pattern_value(stmt):
    if (not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1
            or not isinstance(stmt.targets[0], ast.Name) or not stmt.targets[0].id.isupper()):
        return None
    call = stmt.value
    if (not isinstance(call, ast.Call) or len(call.args) != 1 or call.keywords
            or not isinstance(call.func, ast.Attribute) or call.func.attr != "compile"
            or not isinstance(call.func.value, ast.Name) or call.func.value.id != "re"):
        return None
    literal = call.args[0]
    if not isinstance(literal, ast.Constant) or type(literal.value) is not str:
        return None
    return stmt.targets[0].id, literal.value


def deferred_uses(tree, stmt, name):
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    prefix = tree.body[:tree.body.index(stmt)]
    if not any(isinstance(node, ast.Import) and any(alias.name == "re" and alias.asname is None for alias in node.names)
               for node in prefix):
        return False
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and node.id in _DYNAMIC
                or isinstance(node, ast.Attribute) and node.attr in _DYNAMIC):
            return False
        if isinstance(node, ast.Constant) and node.value == name:
            return False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in {name, "re"}:
            return False
        if isinstance(node, ast.alias) and (node.asname or node.name.split(".")[0]) in {name, "re"}:
            if not (isinstance(parents[node], ast.Import) and node.name == "re" and node.asname is None):
                return False
        if isinstance(node, ast.Name) and node.id == "re" and isinstance(node.ctx, (ast.Store, ast.Del)):
            return False
        if not isinstance(node, ast.Name) or node.id != name or node is stmt.targets[0]:
            continue
        parent = parents[node]
        if not isinstance(parent, ast.Attribute) or parent.attr not in _METHODS:
            return False
        call = parents[parent]
        if not isinstance(call, ast.Call) or call.func is not parent:
            return False
        child = call
        while child in parents:
            parent = parents[child]
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) and child in parent.body:
                break
            child = parent
        else:
            return False
    return True


def changed_pattern(old, new, a, b):
    left, right = pattern_value(a), pattern_value(b)
    if left is None or right is None or left[0] != right[0] or left[1] == right[1]:
        return None
    field = left[0]
    if not deferred_uses(old, a, field) or not deferred_uses(new, b, field):
        return None
    return field, right[1]


@lru_cache(maxsize=32)
def private_users(root, path, field):
    name = re.compile(rb"\b" + re.escape(field.encode()) + rb"\b")
    for directory in ("sqlglot", "tests"):
        for source in (root / directory).rglob("*.py"):
            if source == root / path:
                continue
            data = source.read_bytes()
            if name.search(data) or b"re._cache" in data or b"re.purge(" in data:
                return False
    return True

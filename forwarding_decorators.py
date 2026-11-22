"""Identify local decorators which only construct a deferred wrapper function."""

import ast
from weakref import WeakKeyDictionary

from ast_syntax import same_syntax


_forwarders = WeakKeyDictionary()


def forwarding_decorators(module):
    if module in _forwarders:
        return _forwarders[module]
    valid = prove_forwarding_decorators(module)
    _forwarders[module] = valid
    return valid


def prove_forwarding_decorators(module):
    postponed = any(
        isinstance(n, ast.ImportFrom)
        and n.module == "__future__"
        and any(alias.name == "annotations" for alias in n.names)
        for n in module.body
    )
    if not postponed:
        return {}
    # During module/class construction these operations can manufacture or
    # replace a binding without a normal STORE_NAME in its syntax tree.
    pending = list(module.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            pending.extend(getattr(node, "decorator_list", ()))
            pending.extend(x for x in (*node.args.defaults, *node.args.kw_defaults) if x is not None)
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"exec", "eval", "globals", "locals", "vars", "setattr", "delattr"}
        ):
            return {}
        pending.extend(ast.iter_child_nodes(node))
    def is_forwarder(outer, imports):
        if (
            outer.decorator_list
            or outer.type_params
            or len(outer.args.args) != 1
            or outer.args.defaults
            or outer.args.posonlyargs
            or outer.args.vararg
            or outer.args.kwarg
            or outer.args.kwonlyargs
        ):
            return False
        parameter = outer.args.args[0].arg
        body = list(outer.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        if (
            len(body) != 2
            or not isinstance(body[0], ast.FunctionDef)
            or not isinstance(body[1], ast.Return)
        ):
            return False
        wrapper, returned = body[0], body[1].value
        if (
            wrapper.type_params
            or not isinstance(returned, ast.Name)
            or returned.id != wrapper.name
            or len(wrapper.decorator_list) > 1
        ):
            return False
        needs = set()
        for decoration in wrapper.decorator_list:
            if (
                not isinstance(decoration, ast.Call)
                or not isinstance(decoration.func, ast.Name)
                or decoration.func.id not in imports
                or decoration.func.id in {parameter, wrapper.name}
                or len(decoration.args) != 1
                or decoration.keywords
                or not isinstance(decoration.args[0], ast.Name)
                or decoration.args[0].id != parameter
            ):
                return False
            needs.add(decoration.func.id)
        if any(
            not isinstance(item, ast.Constant)
            for item in (*wrapper.args.defaults, *wrapper.args.kw_defaults)
            if item
        ):
            return False
        return needs

    def bound_once(name, declaration):
        for item in ast.walk(module):
            if item is declaration:
                continue
            if (
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and item.name == name
            ):
                return False
            if (
                isinstance(item, ast.Name)
                and item.id == name
                and isinstance(item.ctx, (ast.Store, ast.Del))
            ):
                return False
            if isinstance(item, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and item.name == name:
                return False
            if isinstance(item, ast.MatchMapping) and item.rest == name:
                return False
            if isinstance(item, (ast.Import, ast.ImportFrom)):
                for alias in item.names:
                    if alias.name == "*" or (alias.asname or alias.name.split(".")[0]) == name:
                        return False
        return True

    valid_imports = {}
    for stmt in module.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "functools" and not stmt.level:
            for alias in stmt.names:
                name = alias.asname or alias.name
                if (alias.name == "wraps"
                        and sum((a.asname or a.name) == name for a in stmt.names) == 1
                        and bound_once(name, stmt)):
                    valid_imports[name] = stmt.lineno
    valid = {}
    for outer in module.body:
        if isinstance(outer, ast.FunctionDef):
            needed = is_forwarder(outer, valid_imports)
            if (needed is not False and all(valid_imports[name] < outer.lineno for name in needed)
                    and bound_once(outer.name, outer)):
                valid[outer.name] = outer
    return valid


def decorator_scope(node, module):
    for statement in module.body:
        if statement is node:
            return True
        if isinstance(statement, ast.ClassDef) and node in statement.body:
            return not statement.bases and not statement.keywords and not statement.decorator_list
    return False


def deferred_decorators(node, previous, current):
    ordinary = {"staticmethod", "classmethod", "property"}
    if all(isinstance(d, ast.Name) and d.id in ordinary for d in node.decorator_list):
        return True
    if len(node.decorator_list) != 1 or not isinstance(node.decorator_list[0], ast.Name):
        return False
    name = node.decorator_list[0].id
    if not any(isinstance(stmt, ast.FunctionDef) and stmt.name == name for stmt in previous.body):
        return False
    before = forwarding_decorators(previous).get(name)
    if before is None or before.lineno >= node.lineno or not decorator_scope(node, previous):
        return False
    after = forwarding_decorators(current).get(name)
    return after is not None and same_syntax(before, after)

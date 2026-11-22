"""Conservative file-level fallback for changes confined to deferred declarations."""
import ast
from collections import Counter
from functools import lru_cache
import gzip
import json
from pathlib import Path
import subprocess
import sys

from ast_syntax import same_syntax
from cold_coverage import digest, profile
from deferred_calls import observed_calls
from impact import _apply, _parse_diff
from symbols import _factory_form, _module, _signature, declarations as declared_fields


def declaration_key(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name
    return None


def attribute_path(node):
    parts = []
    while isinstance(node, ast.Attribute):
        if node.attr.startswith("__"):
            return None
        parts.append(node.attr)
        node = node.value
    if not parts or len(parts) > 3 or not isinstance(node, ast.Name) or node.id.startswith("__"):
        return None
    return ".".join([node.id, *reversed(parts)])


def passive_operand(node, kind, checks):
    if isinstance(node, ast.Constant) and type(node.value) in (str, int, bool, type(None)):
        return True
    path = attribute_path(node)
    if path and checks is not None:
        checks.add((kind, path))
        return True
    return False


def set_expression(old, new, checks):
    if isinstance(old, ast.Set) and isinstance(new, ast.Set):
        return all(passive_operand(n, "key", checks) for n in [*old.elts, *new.elts])
    if (isinstance(old, ast.BinOp) and isinstance(new, ast.BinOp) and type(old.op) is type(new.op)
            and isinstance(old.op, (ast.BitOr, ast.BitAnd, ast.Sub))):
        return set_expression(old.left, new.left, checks) and set_expression(old.right, new.right, checks)
    path = attribute_path(old)
    if checks is not None and path and same_syntax(old, new):
        checks.add(("set", path))
        return True
    return False


def dispatch_change(old, new, checks=None):
    if not isinstance(old, ast.Assign) or not isinstance(new, ast.Assign):
        return False
    if (not same_syntax(old.targets, new.targets) or len(old.targets) != 1
            or not isinstance(old.targets[0], ast.Name) or not old.targets[0].id.isupper()):
        return False
    if not isinstance(old.value, ast.Dict) or not isinstance(new.value, ast.Dict):
        return set_expression(old.value, new.value, checks)

    def entries(mapping):
        grouped = []
        for key, item in zip(mapping.keys, mapping.values):
            grouped.append(((_signature(key), _signature(item)), key, item))
        return grouped

    before, after = entries(old.value), entries(new.value)
    common = Counter(row[0] for row in before) & Counter(row[0] for row in after)
    factories = {_factory_form(value) for _, _, value in before}

    def retained(rows):
        remaining = common.copy()
        ordered = []
        for fingerprint, key, value in rows:
            if remaining[fingerprint]:
                remaining[fingerprint] -= 1
                ordered.append(fingerprint)
                continue
            if not passive_operand(key, "key", checks):
                return None
            if isinstance(value, ast.Lambda) and not value.args.defaults and not any(value.args.kw_defaults):
                continue
            if passive_operand(value, "reference", checks):
                continue
            form = _factory_form(value)
            if checks is None or form is None or form not in factories:
                return None
            checks.add(("factory", form[0]))
        return ordered

    left, right = retained(before), retained(after)
    return left is not None and left == right


def deferred_suffix(body, names, postponed):
    """A new class binding must be followed only by ordinary deferred methods."""
    after_change = False
    for node in body:
        if declaration_key(node) in names:
            after_change = True
        if after_change and (not declaration_key(node) or not simple_function(node, postponed)):
            return False
    return True


def can_yield(node):
    pending = list(node.body)
    while pending:
        current = pending.pop()
        if isinstance(current, (ast.Yield, ast.YieldFrom)):
            return True
        if not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(current))
    return False


def plain_decorators(node):
    return all(isinstance(d, ast.Name) and d.id in ("staticmethod", "classmethod", "property")
               for d in node.decorator_list)


def simple_function(node, postponed):
    if node.name.startswith("__") or node.type_params:
        return False
    if any(not isinstance(d, ast.Name) or d.id not in ("staticmethod", "classmethod")
           for d in node.decorator_list):
        return False
    if any(not isinstance(value, ast.Constant) for value in (*node.args.defaults, *node.args.kw_defaults) if value is not None):
        return False
    args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs, node.args.vararg, node.args.kwarg]
    return postponed or (node.returns is None and all(arg is None or arg.annotation is None for arg in args))


def declarations(path, old, new, import_lines, operands=None):
    dialect = path.startswith(("sqlglot/parsers/", "sqlglot/generators/"))
    postponed = any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
                    and any(alias.name == "annotations" for alias in n.names) for n in old.body)
    verify = set()
    def visit(before, after, owner=""):
        left = {n.name: n for n in before if declaration_key(n)}
        right = {n.name: n for n in after if declaration_key(n)}
        frozen = set()
        if (sum(bool(declaration_key(n)) for n in before) != len(left)
                or sum(bool(declaration_key(n)) for n in after) != len(right)):
            for body in (before, after):
                counts = Counter(declaration_key(n) for n in body if declaration_key(n))
                frozen.update(name for name, count in counts.items() if count > 1)
            # Overloads and property accessors can bind a name several times.
            # Treat them as fixed module statements whose syntax and original
            # position must match in both revisions.
            prior = [(i, n) for i, n in enumerate(before) if declaration_key(n) in frozen]
            revised = [(i, n) for i, n in enumerate(after) if declaration_key(n) in frozen]
            if len(prior) != len(revised) or any(i != j or not same_syntax(n, m)
                    for (i, n), (j, m) in zip(prior, revised)):
                return False
            for name in frozen:
                left.pop(name, None)
                right.pop(name, None)
        common = left.keys() & right.keys()
        changed_names = left.keys() ^ right.keys()
        if changed_names and (not owner or not dialect
                              or not deferred_suffix(before, changed_names, postponed)
                              or not deferred_suffix(after, changed_names, postponed)):
            return False
        def ordering(body):
            return [declaration_key(n) if declaration_key(n) else None for n in body
                    if not declaration_key(n) or declaration_key(n) in common or declaration_key(n) in frozen]
        if ordering(before) != ordering(after):
            return False
        for name in left.keys() | right.keys():
            a, b = left.get(name), right.get(name)
            if a is not None and b is not None and same_syntax(a, b):
                continue
            if a is not None and any(a.body[0].lineno <= line <= a.end_lineno for line in import_lines):
                return False
            if a is not None and b is not None:
                if (type(a) is not type(b) or not plain_decorators(a) or can_yield(a) != can_yield(b)
                        or not all(same_syntax(getattr(a, field), getattr(b, field))
                        for field in a._fields if field != "body") or ast.get_docstring(a, clean=False) != ast.get_docstring(b, clean=False)):
                    return False
                continue
            node = a if a is not None else b
            if not simple_function(node, postponed):
                return False
            verify.add(owner)
        a_rest = [n for n in before if not declaration_key(n) or declaration_key(n) in frozen]
        b_rest = [n for n in after if not declaration_key(n) or declaration_key(n) in frozen]
        if len(a_rest) != len(b_rest):
            return False
        for a, b in zip(a_rest, b_rest):
            if same_syntax(a, b):
                continue
            if isinstance(a, ast.ClassDef) and isinstance(b, ast.ClassDef) and not owner:
                if (not all(same_syntax(getattr(a, field), getattr(b, field)) for field in a._fields if field != "body")
                        or not visit(a.body, b.body, a.name)
                        or (a.name in verify and (a is not before[-1] or b is not after[-1]))):
                    return False
            elif dialect and owner:
                local = set() if operands is not None else None
                if not dispatch_change(a, b, local):
                    return False
                if local:
                    operands.update((owner, a.targets[0].id, kind, name) for kind, name in local)
                verify.add(owner)
            else:
                return False
        return True
    return verify if visit(old.body, new.body) else None


@lru_cache(maxsize=1)
def test_sources():
    try:
        with gzip.open(Path(__file__).with_name("test-module-sources.json.gz"), "rt") as stream:
            return json.load(stream)
    except (OSError, EOFError, UnicodeError, ValueError):
        return {}


def literal_test_body(node, helpers):
    receiver = node.args.args[0].arg
    for stmt in node.body:
        if not isinstance(stmt, ast.Expr):
            return False
        call = stmt.value
        if isinstance(call, ast.Constant) and isinstance(call.value, str):
            continue
        if (not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute)
                or call.func.attr not in helpers or not isinstance(call.func.value, ast.Name)
                or call.func.value.id != receiver or any(kw.arg is None for kw in call.keywords)):
            return False
        for argument in [*call.args, *(kw.value for kw in call.keywords)]:
            if not all(isinstance(n, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Load)) for n in ast.walk(argument)):
                return False
    return True


def only_added_tests(old, new, classes):
    if len(old.body) != len(new.body):
        return False
    validators = {alias.asname or alias.name for node in old.body if isinstance(node, ast.ImportFrom)
                  and node.level == 0 and node.module == "tests.dialects.test_dialect" for alias in node.names
                  if alias.name == "Validator"}
    added = False
    for a, b in zip(old.body, new.body):
        if same_syntax(a, b):
            continue
        if (not isinstance(a, ast.ClassDef) or not isinstance(b, ast.ClassDef) or a.name not in classes
                or a.decorator_list or a.keywords
                or not all(same_syntax(getattr(a, k), getattr(b, k)) for k in a._fields if k != "body")):
            return False
        existing = {n.name for n in a.body if declaration_key(n)}
        existing.update(n.id for n in ast.walk(a) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
        if len(a.bases) != 1 or not isinstance(a.bases[0], ast.Name) or a.bases[0].id not in validators:
            return False
        helpers = {"validate_all", "validate_identity", "assertEqual", "assertTrue", "assertFalse"} - existing
        retained = []
        for item in b.body:
            if (isinstance(item, ast.FunctionDef) and item.name.startswith("test_") and item.name not in existing
                    and not item.decorator_list and item.returns is None and not item.type_params
                    and not item.args.defaults and not item.args.kwonlyargs and not item.args.posonlyargs
                    and not item.args.vararg and not item.args.kwarg and len(item.args.args) == 1
                    and item.args.args[0].annotation is None and literal_test_body(item, helpers)):
                existing.add(item.name)
                added = True
                continue
            retained.append(item)
        if not same_syntax(a.body, retained):
            return False
    return added


@lru_cache(maxsize=64)
def module_tests(index, path):
    data = profile()
    if not data or not index.execution.complete or data["ids"] != index.execution_ids or path not in index.isolated:
        return index.all_bits
    bits = index.isolated[path] | index.unknown | index.incomplete | index.execution.untraced | index.symbols.incomplete
    bits |= index.reads.get(path, 0) | index.execution.child_reads.get(path, 0)
    for reached in index.files.get(path, {}).values():
        bits |= reached
    module = _module(path)
    bits |= index.symbols.modules.get(module, 0)
    for symbol, consumers in index.symbols.classes.items():
        if symbol.startswith(module + ":"):
            bits |= consumers
    symbols = {name for name in index.symbols.declared if name.startswith(module + ":")}
    pending = list(symbols)
    while pending:
        symbol = pending.pop()
        bits |= index.symbols.raw.get(symbol, 0) | index.symbols.copies.get(symbol, 0)
        for consumers in index.symbols.keys.get(symbol, {}).values():
            bits |= consumers
        imports = set(index.symbols.initialized.get(symbol, ()))
        for readers in index.symbols.initialized_keys.get(symbol, {}).values():
            imports.update(readers)
        for imported in imports:
            bits |= index.symbols.modules.get(imported, index.all_bits)
        for dependent in index.symbols.derived.get(symbol, ()):
            if dependent not in symbols:
                symbols.add(dependent)
                pending.append(dependent)
    groups = int(data["reads"].get(path, "0"), 16)
    for _, calls, uncertain, _ in data["files"].get(path, {}).values():
        groups |= int(calls, 16) | int(uncertain, 16)
    for n, members in enumerate(data["groups"]):
        if groups & (1 << n):
            bits |= int(members, 16)
    return bits


def joint_configuration(index, prepared):
    if len(prepared) < 2 or any(not path.startswith("sqlglot/") for path in prepared):
        return None
    roots, sources = [], {}
    floor = index.unknown | index.symbols.incomplete
    for path, (source, changed, tree, replaced, inserted) in prepared.items():
        if not any(source.runtime_scope(line) is None and isinstance(source.statement(line), (ast.Assign, ast.AnnAssign))
                   for line in replaced | inserted):
            return None
        factories = set()
        fields = declared_fields(path, source.tree, tree, index.symbols.declared, factories)
        if not fields or factories:
            return None
        roots.extend(fields)
        sources[path] = changed
        floor |= index.reads.get(path, 0) | index.execution.child_reads.get(path, 0)
        floor |= index.cache_consumers.floor(path, replaced, inserted)
        floor |= (index.incomplete | index.execution.untraced) & index.isolated.get(path, index.all_bits)
    bits = index.symbols.select_fields(roots, sources, set())
    return None if bits is None else index.tests_from_bits(bits | floor)


def boundary_selection(text, index, prepared, selected, price, protected):
    if (not prepared or not any(path.startswith("sqlglot/") for path in prepared)
            or any(not path.endswith(".py") or (not path.startswith("sqlglot/") and path not in index.test_files)
                   for path in prepared)):
        return selected
    data = profile()
    if not data or data["ids"] != index.execution_ids:
        return selected
    try:
        changes = _parse_diff(text)
        paths = {change.path for change in changes}
        if len(paths) != len(changes) or not set(prepared) <= paths:
            return selected
        fixtures = [change for change in changes if change.path not in prepared]
        if any(not change.path.startswith("tests/fixtures/") or not change.path.endswith(".sql")
               or change.path not in index.reads or change.path not in index.isolated for change in fixtures):
            return selected
    except (OSError, UnicodeError, ValueError):
        return selected
    if not fixtures:
        configuration = joint_configuration(index, prepared)
        if configuration is not None:
            result = (selected & configuration) | protected
            if price(selected) - price(result) >= 2000:
                return result
    bits = 0
    for change in fixtures:
        path = change.path
        bits |= index.reads[path] | index.isolated[path] | index.execution.child_reads.get(path, 0)
        bits |= index.incomplete | index.unknown | index.execution.untraced
        read_groups = int(data["reads"].get(path, "0"), 16)
        for i, members in enumerate(data["groups"]):
            if read_groups & (1 << i):
                bits |= int(members, 16)
    for path, (_, _, _, replaced, inserted) in prepared.items():
        if path in index.test_files:
            bits |= index.test_files[path] | index.isolated.get(path, index.all_bits) | index.unknown
            bits |= index.reads.get(path, 0) | index.execution.child_reads.get(path, 0)
        else:
            consumers = module_tests(index, path)
            calls = observed_calls(index, path, prepared[path], data)
            if calls is not None:
                consumers &= calls
            bits |= consumers | index.cache_consumers.floor(path, replaced, inserted)
    revised = selected & index.tests_from_bits(bits)
    revised |= protected
    if revised == selected:
        return selected
    saved_ms = price(selected) - price(revised)
    if saved_ms < 2000:
        return selected
    checks = {}
    operands = {}
    try:
        for change in fixtures:
            _apply((index.root / change.path).read_text(encoding="utf-8"), change.hunks)
        for path, (source, changed, tree, _, _) in prepared.items():
            if path in index.test_files:
                classes = {name.split("::")[1] for name in index.execution_ids
                           if name.startswith(path + "::") and name.count("::") >= 2}
                if test_sources().get(path) != digest(source.text) or not only_added_tests(source.tree, tree, classes):
                    return selected
                continue
            if data["hashes"].get(path) != digest(source.text):
                return selected
            local = set()
            names = declarations(path, source.tree, tree, index.import_lines.get(path, set()), local)
            if local:
                operands[path] = sorted(local)
            if names is None:
                return selected
            if names:
                checks[path] = sorted(names)
        if checks:
            payload = {"files": {path: entry[1] for path, entry in prepared.items() if path not in index.test_files}, "classes": checks, "operands": operands}
            done = subprocess.run([sys.executable, "-I", "-B", "-S", str(Path(__file__).with_name("symbols.py")),
                                   "--verify-declarations", str(index.root)], input=json.dumps(payload), text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  timeout=min(18.0, saved_ms / 2000), check=True)
            if done.stdout.strip() != "verified":
                return selected
    except (OSError, UnicodeError, ValueError, SyntaxError, RecursionError, subprocess.SubprocessError):
        return selected
    return revised

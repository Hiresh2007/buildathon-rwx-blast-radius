"""Map shared lazy-table population and memoized functions to their consumers."""

import ast
from collections import defaultdict
import gzip
from importlib.util import resolve_name
import json
from pathlib import Path
import sys


FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)
MEMOIZERS = {'functools.cache', 'functools.lru_cache', 'functools.cached_property'}
MAP_FACTORIES = {'dict', 'collections.defaultdict', 'collections.OrderedDict', 'collections.Counter'}


def children(node):
    """Inspect the body executed by this scope, without entering nested definitions."""
    yield node
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (*FUNCTION, ast.ClassDef, ast.Lambda)):
            yield from children(child)


def start(node):
    return min([node.lineno] + [d.lineno for d in getattr(node, 'decorator_list', ())])


class Module:
    def __init__(self, path, tree):
        self.path = path
        self.name = path[:-3].replace('/', '.').removesuffix('.__init__')
        package = self.name if path.endswith('/__init__.py') else self.name.rpartition('.')[0]
        self.globals = {}
        self.functions = {}
        self.maps = {}
        self.bases = {}
        for item in tree.body:
            if isinstance(item, ast.ImportFrom):
                target = resolve_name('.' * item.level + (item.module or ''), package)
                for member in item.names:
                    self.globals[member.asname or member.name] = target + '.' + member.name
            elif isinstance(item, ast.Import):
                for member in item.names:
                    first = member.name.split('.')[0]
                    self.globals[member.asname or first] = member.name if member.asname else first
        self.definitions(tree.body, '')

    def name_of(self, expr, owner):
        if isinstance(expr, ast.Name):
            if owner and expr.id in {'self', 'cls'}:
                return self.name + '.' + owner
            return self.globals.get(expr.id, self.name + '.' + expr.id)
        if isinstance(expr, ast.Attribute):
            return self.name_of(expr.value, owner) + '.' + expr.attr
        return ''

    def definitions(self, body, owner):
        for node in body:
            prefix = owner + '.' if owner else ''
            if isinstance(node, ast.ClassDef):
                self.bases[self.name + '.' + prefix + node.name] = [
                    self.name_of(base, owner) for base in node.bases]
                self.definitions(node.body, prefix + node.name)
            elif isinstance(node, FUNCTION):
                self.functions[self.name + '.' + prefix + node.name] = (owner, node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                mapping = isinstance(value, (ast.Dict, ast.DictComp)) or (
                    isinstance(value, ast.Call) and (
                        self.name_of(value.func, owner) in MAP_FACTORIES or
                        isinstance(value.func, ast.Name) and value.func.id == 'dict'))
                if mapping:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            field = prefix + target.id
                            self.maps[self.name + '.' + field] = (self.name + ':' + field, self.path)


def build(source):
    modules = [Module(p.relative_to(source).as_posix(), ast.parse(p.read_bytes()))
               for p in sorted((source / 'sqlglot').rglob('*.py'))]
    tables = {k: v for mod in modules for k, v in mod.maps.items()}
    functions = {k: (mod, owner, node) for mod in modules for k, (owner, node) in mod.functions.items()}
    bases = {k: v for mod in modules for k, v in mod.bases.items()}

    def table_name(name):
        owner, _, attr = name.rpartition('.')
        pending = [owner]
        visited = set()
        while pending:
            cls = pending.pop()
            if cls in visited:
                continue
            visited.add(cls)
            candidate = cls + '.' + attr
            if candidate in tables:
                return candidate
            pending.extend(reversed(bases.get(cls, ())))
        return name

    edges = defaultdict(set)
    roots = defaultdict(set)
    ranges = defaultdict(set)
    lazy_tables = set()
    all_stores = []

    for name, (mod, owner, fn) in functions.items():
        def resolve(expr):
            return mod.name_of(expr, owner)

        for dec in fn.decorator_list:
            if resolve(dec.func if isinstance(dec, ast.Call) else dec) in MEMOIZERS:
                # No project field stores this wrapper's internal dictionary.
                roots[('', mod.path)].add(name)
        nodes = list(children(fn))
        parents = {child: parent for parent in nodes for child in ast.iter_child_nodes(parent)}
        assigned = defaultdict(list)
        reads = set()
        writes = defaultdict(list)
        for n in nodes:
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
                for target in n.targets if isinstance(n, ast.Assign) else [n.target]:
                    if isinstance(target, ast.Name):
                        assigned[target.id].append((n, n.value))
                    elif isinstance(target, ast.Subscript):
                        writes[table_name(resolve(target.value))].append((n, [target.slice, n.value]))
            elif isinstance(n, ast.Call):
                called = resolve(n.func)
                if called in functions:
                    edges[name].add(called)
                if isinstance(n.func, ast.Attribute):
                    table = table_name(resolve(n.func.value))
                    if n.func.attr in {'get', 'setdefault'}:
                        reads.add(table)
                    if n.func.attr in {'setdefault', 'update'}:
                        writes[table].append((n, [*n.args, *[k.value for k in n.keywords]]))
            elif isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load):
                reads.add(table_name(resolve(n.value)))
            elif isinstance(n, ast.Compare):
                for op, rhs in zip(n.ops, n.comparators):
                    if isinstance(op, (ast.In, ast.NotIn)):
                        reads.add(table_name(resolve(rhs)))

        # A function that reads a persistent mapping and also fills it can run
        # once on a miss, even when far more tests subsequently read that key.
        lazy_tables.update(reads & writes.keys() & tables.keys())
        for table in writes.keys() & tables.keys():
            all_stores.append((mod, owner, table, writes[table], parents, assigned))

    for mod, owner, table, stores, parents, assigned in all_stores:
        if table not in lazy_tables:
            continue
        field = tables[table]
        for store, values in stores:
            pending = [(store, value) for value in values]
            parent = parents.get(store)
            while parent is not None:
                if isinstance(parent, (ast.If, ast.While)):
                    pending.append((parent.test, parent.test))
                elif isinstance(parent, (ast.For, ast.AsyncFor)):
                    pending.append((parent.iter, parent.iter))
                parent = parents.get(parent)
            seen = set()
            while pending:
                stmt, value = pending.pop()
                if value in seen:
                    continue
                seen.add(value)
                ranges[mod.path].add((stmt.lineno, stmt.end_lineno, *field))
                for node in children(value):
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                        pending.extend(assigned.get(node.id, ()))
                    elif isinstance(node, ast.Call):
                        callee = mod.name_of(node.func, owner)
                        if callee in functions:
                            roots[field].add(callee)

    for field, initial in roots.items():
        pending = list(initial)
        seen = set()
        while pending:
            fn = pending.pop()
            if fn in seen:
                continue
            seen.add(fn)
            mod, _, node = functions[fn]
            ranges[mod.path].add((start(node), node.end_lineno, *field))
            pending.extend(edges[fn] - seen)
    return {path: sorted(spans) for path, spans in sorted(ranges.items())}


if __name__ == '__main__':
    result = build(Path(sys.argv[1]))
    raw = json.dumps(result, separators=(',', ':'), sort_keys=True).encode()
    Path(sys.argv[2]).write_bytes(gzip.compress(raw, mtime=0))

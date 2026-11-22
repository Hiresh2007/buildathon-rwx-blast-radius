"""Select tests from observed configuration reads and class-construction dependencies."""

import ast
from collections import Counter
import enum
import gzip
import hashlib
import importlib
import importlib.machinery
import inspect
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import textwrap
import tempfile
import sysconfig
import time
from types import CodeType, FunctionType


def _module(path):
    return path[:-3].replace("/", ".").removesuffix(".__init__")


def _signature(value):
    if isinstance(value, ast.AST):
        return ast.dump(value)
    if isinstance(value, list):
        return tuple(_signature(x) for x in value)
    return value


def _safe_value(value, existing):
    if _signature(value) in existing:
        return True
    if isinstance(value, ast.Constant):
        return type(value.value) in (int, bool, str, float, type(None))
    if isinstance(value, ast.Name):
        return not value.id.startswith("__")
    if isinstance(value, ast.Attribute):
        return not value.attr.startswith("__") and _safe_value(value.value, existing)
    if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
        return all(_safe_value(item, existing) for item in value.elts)
    if isinstance(value, ast.Dict):
        return all(k is None or _safe_value(k, existing) for k in value.keys) and all(
            _safe_value(v, existing) for v in value.values
        )
    if isinstance(value, ast.UnaryOp) and isinstance(value.op, (ast.Not, ast.UAdd, ast.USub)):
        return isinstance(value.operand, ast.Constant)
    return False


def _factory_form(node):
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None
    args = [(None, value) for value in node.args] + [(kw.arg, kw.value) for kw in node.keywords]
    if (
        not args
        or any(kw.arg is None for kw in node.keywords)
        or any(
            not isinstance(v, ast.Constant) or type(v.value) not in (str, int, float, bool, type(None))
            for _, v in args
        )
    ):
        return None
    return node.func.id, tuple((key, type(value.value).__name__) for key, value in args)


def _changed_factories(a, b):
    old = {n: _signature(n) for n in ast.walk(a) if isinstance(n, (ast.Call, ast.Lambda))}
    new = {n: _signature(n) for n in ast.walk(b) if isinstance(n, (ast.Call, ast.Lambda))}
    if Counter(old.values()) == Counter(new.values()):
        return [], []
    # Keep all unchanged executable expressions intact. Only same-callee calls
    # with the original argument names and primitive types can differ.
    keep = Counter(old.values()) & Counter(new.values())

    def remaining(d):
        left = keep.copy()
        out = []
        for n, key in d.items():
            if left[key]:
                left[key] -= 1
            else:
                out.append(n)
        return out

    x, y = remaining(old), remaining(new)
    fx = [_factory_form(n) for n in x]
    fy = [_factory_form(n) for n in y]
    if None in fx or None in fy or Counter(fx) != Counter(fy):
        return None
    return x, y


def _approved_factory(fn):
    if not isinstance(fn, FunctionType) or not fn.__module__.startswith("sqlglot"):
        return False
    try:
        node = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
    except (SyntaxError, OSError, TypeError):
        return False
    if not isinstance(node, ast.FunctionDef) or node.decorator_list:
        return False
    formals = set(a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs)

    def literal_default(expr):
        return (
            expr is None
            or isinstance(expr, ast.Constant)
            or isinstance(expr, ast.Name)
            and expr.id in formals
        )

    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body.pop(0)
    if not body or not isinstance(body[-1], ast.Return):
        return False
    nested_names = set()
    for nested in body[:-1]:
        if (
            not isinstance(nested, ast.FunctionDef)
            or nested.decorator_list
            or not all(literal_default(x) for x in nested.args.defaults + nested.args.kw_defaults)
        ):
            return False
        # Some Python versions evaluate annotations while constructing the nested
        # function. An annotation that calls code is outside this factory proof.
        annotations = [
            a.annotation for a in nested.args.posonlyargs + nested.args.args + nested.args.kwonlyargs
        ]
        annotations.append(nested.returns)
        if any(
            isinstance(
                n,
                (
                    ast.Call,
                    ast.NamedExpr,
                    ast.Lambda,
                    ast.ListComp,
                    ast.DictComp,
                    ast.SetComp,
                    ast.GeneratorExp,
                ),
            )
            for ann in annotations
            if ann
            for n in ast.walk(ann)
        ):
            return False
        nested_names.add(nested.name)
    out = body[-1].value
    if isinstance(out, ast.Lambda):
        return all(literal_default(x) for x in out.args.defaults + out.args.kw_defaults)
    return isinstance(out, ast.Name) and out.id in nested_names


def declarations(path, old_tree, new_tree, known, factories=None):
    changed = []

    def visit(before, after, scope):
        if len(before) != len(after):
            return False
        for a, b in zip(before, after):
            if _signature(a) == _signature(b):
                continue
            if type(a) is not type(b):
                return False
            if isinstance(a, ast.ClassDef):
                if any(
                    _signature(getattr(a, k)) != _signature(getattr(b, k))
                    for k in a._fields
                    if k != "body"
                ):
                    return False
                if not visit(a.body, b.body, scope + a.name + "."):
                    return False
                continue
            if not isinstance(a, (ast.Assign, ast.AnnAssign)):
                return False
            if any(
                _signature(getattr(a, k)) != _signature(getattr(b, k)) for k in a._fields if k != "value"
            ):
                return False
            ts = a.targets if isinstance(a, ast.Assign) else [a.target]
            if len(ts) != 1 or not isinstance(ts[0], ast.Name) or a.value is None or b.value is None:
                return False
            symbol = f"{_module(path)}:{scope}{ts[0].id}"
            if symbol not in known:
                return False
            # A new callable/executable initializer is never run by the import probe.
            # Existing callable entries in a dispatch table can stay in place.
            ao = {_signature(n) for n in ast.walk(a.value)}
            bo = {_signature(n) for n in ast.walk(b.value)}
            different = _changed_factories(a.value, b.value)
            if different is None:
                return False
            old_calls, new_calls = different
            if old_calls:
                if (
                    factories is None
                    or not isinstance(a.value, ast.Dict)
                    or not isinstance(b.value, ast.Dict)
                ):
                    return False
                for n in old_calls:
                    factories.add((symbol, n.func.id))
                ao.update(_signature(n) for n in new_calls)
                bo.update(_signature(n) for n in old_calls)
            if not _safe_value(b.value, ao) or not _safe_value(a.value, bo):
                return False
            changed.append(symbol)
        return True

    return changed if visit(old_tree.body, new_tree.body, "") else None


def _type_name(cls):
    return str(cls.__module__) + ":" + str(cls.__qualname__)


def _key(value):
    if isinstance(value, type):
        return "class:" + _type_name(value)
    if isinstance(value, enum.Enum):
        return "enum:" + _type_name(type(value)) + "." + value.name
    if type(value) in (str, int, bool, float) or value is None:
        return type(value).__name__ + ":" + str(value)
    return None


class _Fingerprints:
    """Short stable fingerprints shared by all fields in a single imported interpreter."""

    def __init__(self):
        self.cache = {}
        self.visiting = set()

    def value(self, obj):
        simple = _key(obj)
        if simple is not None:
            return simple
        objid = id(obj)
        if objid in self.cache:
            return self.cache[objid]
        if objid in self.visiting:
            return "cycle:" + _type_name(type(obj))
        self.visiting.add(objid)
        try:
            if isinstance(obj, dict):
                entries = [(self.value(k), self.value(v)) for k, v in obj.items()]
                parts = ["dict", sorted(entries)]
            elif isinstance(obj, (list, tuple, set, frozenset)):
                items = [self.value(v) for v in obj]
                parts = [
                    type(obj).__name__,
                    sorted(items) if isinstance(obj, (set, frozenset)) else items,
                ]
            elif isinstance(obj, FunctionType):
                cells = [self.value(c.cell_contents) for c in obj.__closure__ or ()]
                parts = [
                    "function",
                    obj.__module__,
                    obj.__qualname__,
                    self.value(obj.__code__),
                    self.value(obj.__defaults__),
                    self.value(obj.__kwdefaults__),
                    cells,
                ]
            elif isinstance(obj, CodeType):
                parts = ["code", obj.co_code.hex(), list(obj.co_names), self.value(obj.co_consts)]
            elif isinstance(obj, re.Pattern):
                parts = ["regex", obj.pattern, obj.flags]
            elif isinstance(obj, bytes):
                parts = ["bytes", obj.hex()]
            else:
                raise TypeError(_type_name(type(obj)))
            data = json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()
            digest = hashlib.blake2b(data, digest_size=12).hexdigest()
            self.cache[objid] = digest
            return digest
        finally:
            self.visiting.remove(objid)

    def field(self, obj):
        if isinstance(obj, dict):
            entries = {}
            for k, v in obj.items():
                name = _key(k)
                if name is None or name in entries:
                    return ["value", self.value(obj)]
                try:
                    entries[name] = self.value(v)
                except (TypeError, ValueError, RecursionError):
                    entries[name] = None
            return ["dict", entries]
        return ["value", self.value(obj)]


def _import_all(root, target=None, replacement=None):
    original = importlib.machinery.SourceFileLoader.get_code

    def get_code(loader, fullname):
        if replacement is not None and fullname == _module(target):
            return compile(replacement, loader.path, "exec", dont_inherit=True)
        return original(loader, fullname)

    if replacement is not None:
        importlib.machinery.SourceFileLoader.get_code = get_code
    sys.path.insert(0, str(root))
    for path in sorted((root / "sqlglot").rglob("*.py")):
        if path.name != "__main__.py":
            importlib.import_module(_module(path.relative_to(root).as_posix()))


def _factory_bound_to(field, name):
    module, qualified = field.split(":", 1)
    module_obj = sys.modules.get(module)
    owner = module_obj
    try:
        for part in qualified.split(".")[:-1]:
            owner = vars(owner)[part]
        return _approved_factory(vars(owner).get(name, vars(module_obj).get(name)))
    except (TypeError, KeyError):
        return False


def _snapshot(fields):
    fingerprints = _Fingerprints()
    out = {}
    for symbol in fields:
        module, name = symbol.split(":", 1)
        obj = sys.modules.get(module)
        try:
            for key in name.split("."):
                owner, obj = obj, vars(obj)[key]
            if isinstance(obj, enum.Enum) and isinstance(owner, type) and type(obj) is owner:
                out[symbol] = ["enum_member"]
            else:
                out[symbol] = fingerprints.field(obj)
        except (KeyError, TypeError, AttributeError, ValueError, RecursionError):
            out[symbol] = None
    return out


def _probe_main(root):
    request = json.load(sys.stdin)
    _import_all(root, request["path"], request.get("source"))
    if not all(_factory_bound_to(field, name) for field, name in request.get("factories", ())):
        raise ValueError("unverified transform factory")
    json.dump(_snapshot(request["fields"]), sys.stdout, separators=(",", ":"))


def _stamp(root):
    digest = hashlib.sha256()
    for path in sorted((root / "sqlglot").rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode() + b"\x00")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return json.dumps(
        [
            1,
            sys.implementation.cache_tag,
            list(sys.version_info[:3]),
            sysconfig.get_platform(),
            digest.hexdigest(),
        ]
    )


def _factory_candidates(tree, module, declared):
    def scan(body, scope):
        for node in body:
            if isinstance(node, ast.ClassDef):
                yield from scan(node.body, scope + node.name + ".")
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(node.value, ast.Dict):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                continue
            symbol = f"{module}:{scope}{targets[0].id}"
            if symbol in declared:
                for child in ast.walk(node.value):
                    if _factory_form(child) is not None:
                        yield symbol, child.func.id

    return set(scan(tree.body, ""))


def build_baseline(root, map_path, output):
    """Write target-Python fingerprints atomically into a portable read-only image layer."""
    root, output = root.resolve(), output.resolve()
    with gzip.open(map_path, "rt", encoding="utf-8") as f:
        atlas = json.load(f)
    declared = set(atlas["declared"])
    fields = declared | set(atlas["derived"])
    for names in atlas["derived"].values():
        fields.update(names)
    _import_all(root)
    factories = set()
    for path in sorted((root / "sqlglot").rglob("*.py")):
        module = _module(path.relative_to(root).as_posix())
        for field, name in _factory_candidates(ast.parse(path.read_bytes()), module, declared):
            if _factory_bound_to(field, name):
                factories.add((field, name))
    states = _snapshot(sorted(fields))
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".symbols-", delete=False) as f:
        tmp = Path(f.name)
    db = sqlite3.connect(tmp)
    try:
        with db:
            db.execute("CREATE TABLE metadata (stamp TEXT)")
            db.execute("INSERT INTO metadata VALUES (?)", (_stamp(root),))
            db.execute(
                "CREATE TABLE fields (symbol TEXT PRIMARY KEY, fingerprint TEXT NOT NULL) WITHOUT ROWID"
            )
            db.executemany(
                "INSERT INTO fields VALUES (?, ?)",
                ((k, json.dumps(v, separators=(",", ":"))) for k, v in states.items()),
            )
            db.execute(
                "CREATE TABLE factories (symbol TEXT, name TEXT, PRIMARY KEY(symbol,name)) WITHOUT ROWID"
            )
            db.executemany("INSERT INTO factories VALUES (?, ?)", sorted(factories))
        db.close()
        tmp.chmod(0o644)
        tmp.replace(output)
    finally:
        db.close()
        tmp.unlink(missing_ok=True)
    print(
        f"Wrote {len(states)} pristine field fingerprints and {len(factories)} verified factory bindings."
    )


class SymbolIndex:
    """Masks are indexed by the supplied IDs, matching ImpactIndex.numbers."""

    def __init__(self, data, test_ids, source_root=None, baseline_path=None):
        if list(test_ids) != data["ids"]:
            raise ValueError("symbol and coverage test IDs differ")
        pool = [int(v, 16) for v in data["sets"]]
        self.incomplete = pool[data["incomplete"]]
        self.declared = set(data["declared"])
        self.raw = {k: pool[v] for k, v in data["reads"].items()}
        self.keys = {k: {name: pool[v] for name, v in d.items()} for k, d in data["keys"].items()}
        self.classes = {k: pool[v] for k, v in data["classes"].items()}
        self.modules = {k: pool[v] for k, v in data["modules"].items()}
        self.derived = data["derived"]
        self.initialized = data["initialized"]
        self.initialized_keys = data.get("initialized_keys", {})
        self.copies = {k: pool[v] for k, v in data.get("copies", {}).items()}
        self.root = Path(source_root or os.environ.get("SQLGLOT_ROOT", "/opt/sqlglot")).resolve()
        self.worker_seconds = 40.0
        self.baseline = {}
        self.approved_factories = set()
        self.snapshot_path = (
            Path(baseline_path) if baseline_path else Path(__file__).with_name("symbol-baseline.sqlite3")
        )
        self.snapshot_checked = False
        self.snapshot_db = None

    def _read_baseline(self, fields, factories):
        # A missing, stale, incompatible or damaged image cache uses the live pristine probe.
        try:
            if not self.snapshot_checked:
                self.snapshot_checked = True
                if self.snapshot_path.is_file():
                    uri = self.snapshot_path.resolve().as_uri() + "?mode=ro&immutable=1"
                    db = sqlite3.connect(uri, uri=True)
                    try:
                        if db.execute("SELECT stamp FROM metadata").fetchone() == (_stamp(self.root),):
                            self.snapshot_db = db
                    finally:
                        if self.snapshot_db is None:
                            db.close()
            if self.snapshot_db is None:
                return
            for key in fields:
                row = self.snapshot_db.execute(
                    "SELECT fingerprint FROM fields WHERE symbol=?", (key,)
                ).fetchone()
                if row is not None:
                    value = json.loads(row[0])
                    valid = (
                        value is None
                        or value == ["enum_member"]
                        or (
                            isinstance(value, list)
                            and len(value) == 2
                            and (
                                value[0] == "value"
                                and isinstance(value[1], str)
                                or value[0] == "dict"
                                and isinstance(value[1], dict)
                            )
                        )
                    )
                    if not valid:
                        raise ValueError("invalid cached symbol fingerprint")
                    self.baseline[key] = value
            for binding in factories:
                if self.snapshot_db.execute(
                    "SELECT 1 FROM factories WHERE symbol=? AND name=?", binding
                ).fetchone():
                    self.approved_factories.add(binding)
        except (sqlite3.Error, OSError, TypeError, ValueError):
            if self.snapshot_db is not None:
                self.snapshot_db.close()
                self.snapshot_db = None

    def probe(self, path, source, fields, factories=()):
        payload = json.dumps(
            {"path": path, "source": source, "fields": sorted(fields), "factories": sorted(factories)}
        ).encode()
        if self.worker_seconds < 0.1:
            return None
        started = time.monotonic()
        try:
            p = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-S",
                    str(Path(__file__).resolve()),
                    "--probe",
                    str(self.root),
                ],
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=min(10, self.worker_seconds),
                check=True,
            )
            return json.loads(p.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        finally:
            self.worker_seconds -= time.monotonic() - started

    def select(self, path, old_tree, new_tree, old_source=None, new_source=None):
        if not path.startswith("sqlglot/") or new_source is None:
            return None
        factories = set()
        roots = declarations(path, old_tree, new_tree, self.declared, factories)
        if not roots or len(roots) != 1:
            return None
        closure = set(roots)
        pending = list(roots)
        while pending:
            for symbol in self.derived.get(pending.pop(), ()):
                if symbol not in closure:
                    closure.add(symbol)
                    pending.append(symbol)
        self._read_baseline(closure - self.baseline.keys(), factories - self.approved_factories)
        missing = closure - self.baseline.keys()
        unverified = factories - self.approved_factories
        if missing or unverified:
            initial = self.probe(path, None, missing, unverified)
            if initial is None:
                return None
            self.baseline.update(initial)
            self.approved_factories.update(unverified)
        final = self.probe(path, new_source, closure)
        if final is None:
            return None
        bits = self.incomplete
        accounted = False
        for symbol in closure:
            before, after = self.baseline.get(symbol), final.get(symbol)
            if before == ["enum_member"] or after == ["enum_member"]:
                return None
            opaque_dict = before and before[0] == "dict" and None in before[1].values()
            if before is not None and before == after and not opaque_dict:
                continue
            raw = self.raw.get(symbol, 0)
            keys = self.keys.get(symbol, {})
            initialized = set(self.initialized.get(symbol, ()))
            init_keys = self.initialized_keys.get(symbol, {})
            copies = self.copies.get(symbol, 0)
            if before and after and before[0] == after[0] == "dict":
                a, b = before[1], after[1]
                delta = [
                    k
                    for k in (a.keys() | b.keys())
                    if a.get(k) != b.get(k) or a.get(k) is None or b.get(k) is None
                ]
                bits |= raw
                if a.keys() != b.keys():
                    bits |= copies
                for key in delta:
                    bits |= keys.get(key, 0)
                    initialized.update(init_keys.get(key, ()))
            else:
                bits |= raw | copies
                for modules in init_keys.values():
                    initialized.update(modules)
                for mask in keys.values():
                    bits |= mask
            for mod in initialized:
                if mod not in self.modules:
                    return None
                bits |= self.modules[mod]
            accounted |= bool(raw or keys or initialized or init_keys or copies)
            if before is None or after is None:
                # Dynamic class factories can produce classes without a stable module binding.
                module, field = symbol.split(":", 1)
                owner = field.rpartition(".")[0]
                bits |= self.classes.get(f"{module}:{owner}", 0) or self.modules.get(module, 0)
        if not accounted:
            # Imports succeeded but a published configuration value might only be used
            # through copied/native data. Keep tests using its owner or module.
            for symbol in roots:
                mod, field = symbol.split(":", 1)
                owner = field.rpartition(".")[0]
                extra = self.classes.get(f"{mod}:{owner}", 0) or self.modules.get(mod, 0)
                if not extra:
                    return None
                bits |= extra
        return bits


def verify_declarations(root):
    payload = json.load(sys.stdin)
    files = {_module(path): (path, source) for path, source in payload["files"].items()}
    loaded = set()
    original = importlib.machinery.SourceFileLoader.get_code
    def load(loader, name):
        if name in files:
            path, source = files[name]
            if not Path(loader.path).samefile(root / path):
                raise ImportError(name)
            loaded.add(name)
            return compile(source, loader.path, "exec", dont_inherit=True)
        return original(loader, name)
    importlib.machinery.SourceFileLoader.get_code = load
    _import_all(root)
    if loaded != files.keys():
        return
    for path, names in payload["classes"].items():
        module = sys.modules[_module(path)]
        if not Path(module.__file__).samefile(root / path):
            return
        for name in names:
            if not name:
                continue
            cls = vars(module).get(name)
            if type(cls) is not type or any("__init_subclass__" in base.__dict__ for base in cls.__mro__[1:-1]):
                return
    print("verified")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--verify-declarations":
        verify_declarations(Path(sys.argv[2]))
    elif len(sys.argv) == 3 and sys.argv[1] == "--probe":
        _probe_main(Path(sys.argv[2]))
    elif len(sys.argv) == 5 and sys.argv[1] == "--build-baseline":
        build_baseline(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
    else:
        raise SystemExit("Usage: symbols.py --build-baseline SQLGLOT_ROOT SYMBOL_MAP OUTPUT_DB")

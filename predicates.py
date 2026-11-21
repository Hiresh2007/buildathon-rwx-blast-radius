"""Baseline operand domains for identity, equality and ordering comparisons."""
import ast
import enum
import gzip
import json
import operator
from functools import lru_cache

OPS = {
    "Eq": operator.eq, "NotEq": operator.ne, "Lt": operator.lt, "LtE": operator.le,
    "Gt": operator.gt, "GtE": operator.ge, "Is": operator.is_, "IsNot": operator.is_not,
}
UNKNOWN = ("?",)


@lru_cache(maxsize=512)
def immutable_comparisons(cls):
    # Use the exact implementation inherited for every operator. An enum can
    # inherit string semantics or plain object identity, or override either.
    def implementation(owner, name):
        for parent in type.__getattribute__(owner, "__mro__"):
            namespace = type.__getattribute__(parent, "__dict__")
            if name in namespace:
                return namespace[name]
    for base in (str, object):
        names = ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__")
        if issubclass(cls, base) and all(implementation(cls, n) is implementation(base, n) for n in names):
            return base
    return None


def encode(value, depth=0):
    cls = type(value)
    if value is None:
        return ("n",)
    if cls is bool:
        return ("b", value)
    if cls is int and value.bit_length() <= 64:
        return ("i", value)
    if cls is str and len(value) <= 48:
        return ("s", value)
    if cls is tuple and len(value) <= 5 and depth < 2:
        elements = tuple(encode(item, depth + 1) for item in value)
        return UNKNOWN if UNKNOWN in elements else ("t", *elements)
    if type(cls) is type or type(cls) is enum.EnumMeta:
        base = immutable_comparisons(cls)
        if depth == 0 and type(cls) is enum.EnumMeta and base in (str, object):
            # Bypass user attribute hooks. Enum aliases share a canonical name and
            # object identity; a string-mixin still compares by its underlying text.
            fields = enum.Enum.__dict__["__dict__"].__get__(value)
            name = dict.get(fields, "_name_")
            module = type.__getattribute__(cls, "__module__")
            qualname = type.__getattribute__(cls, "__qualname__")
            if all(type(part) is str and len(part) <= 128 for part in (name, module, qualname)):
                member_value = encode(dict.get(fields, "_value_"), depth + 1)
                if base is str:
                    if str.__len__(value) > 48:
                        return UNKNOWN
                    member_value = ("s", str.__str__(value))
                return ("e", module, qualname, name,
                        "s" if base is str else "o", member_value)
        if base is str and str.__len__(value) <= 48:
            return ("s", str.__str__(value))
        if base is object and depth == 0:
            return ("o",)
    return UNKNOWN


def semantic_value(key):
    if key[0] == "e":
        return key[5] if key[4] == "s" else ("o",)
    return key


def decode(key):
    if key[0] == "n":
        return None
    if key[0] == "t":
        return tuple(decode(item) for item in key[1:])
    if key[0] in {"b", "i", "s"}:
        return key[1]
    raise ValueError("unsupported runtime value")


def literal(node):
    if not isinstance(node, (ast.Constant, ast.Tuple)):
        return UNKNOWN
    try:
        return encode(ast.literal_eval(node))
    except (ValueError, TypeError):
        return UNKNOWN


def edits(old, new):
    if len(old.ops) != 1 or len(new.ops) != 1:
        return None
    op = type(new.ops[0]).__name__
    if type(old.ops[0]).__name__ not in OPS or op not in OPS:
        return None
    changes = []
    for previous, current in zip((old.left, old.comparators[0]), (new.left, new.comparators[0])):
        if ast.dump(previous) == ast.dump(current):
            changes.append(None)
        else:
            before, after = literal(previous), literal(current)
            if before == UNKNOWN or after == UNKNOWN:
                return None
            changes.append(after)
    return op, changes


def boolean_names(node):
    if not isinstance(node, ast.BoolOp) or not (2 <= len(node.values) <= 5):
        return None
    names = []
    for item in node.values:
        negated = isinstance(item, ast.UnaryOp) and isinstance(item.op, ast.Not)
        ref = item.operand if negated else item
        if not isinstance(ref, ast.Name):
            return None
        names.append((ref.id, negated))
    return tuple(names)


def changed_predicates(old, new, out):
    """Match the whole module; every difference must be inside an eligible comparison."""
    if type(old) is not type(new):
        return False
    if isinstance(old, ast.BoolOp) and type(old.op) != type(new.op):
        names = boolean_names(old)
        if names is not None and names == boolean_names(new):
            out.append((old, type(new.op).__name__, []))
            return True
    if isinstance(old, ast.Compare):
        if ast.dump(old) == ast.dump(new):
            return True
        edit = edits(old, new)
        if edit is not None:
            out.append((old, *edit))
            return True
    if isinstance(old, ast.AST):
        return all(changed_predicates(getattr(old, name), getattr(new, name), out) for name in old._fields)
    if isinstance(old, list):
        return len(old) == len(new) and all(changed_predicates(a, b, out) for a, b in zip(old, new))
    return old == new


def stable(observation, op, changes):
    if op in {"And", "Or"}:
        if observation[0] != "truth":
            return False
        _, values, previous = observation
        return (all(values) if op == "And" else any(values)) == previous
    left, right, identical, old_result = observation
    if old_result is None:
        return False
    try:
        keys = [semantic_value(change if change is not None else value) for value, change in zip((left, right), changes)]
        if op in {"Is", "IsNot"}:
            if any(change is not None for change in changes):
                if all(key[0] not in {"b", "n"} for key in keys):
                    return False
                # None and bool are language-level singletons.
                identical = tuple(keys[0]) == tuple(keys[1])
            return (identical if op == "Is" else not identical) == old_result
        if any(key[0] == "o" for key in keys):
            if op not in {"Eq", "NotEq"} or any(key[0] not in {"n", "o"} for key in keys):
                return False
            equal = identical if all(key[0] == "o" for key in keys) else False
            return (equal if op == "Eq" else not equal) == old_result
        a, b = map(decode, keys)
        return OPS[op](a, b) == old_result
    except (TypeError, ValueError):
        return False


def load_domains(path, names):
    if not path.exists():
        return None
    with gzip.open(path, "rt") as source:
        data = json.load(source)
    if data.pop("ids") != names:
        return None
    data["complete"] = int(data["complete"], 16)
    data["lines"] = {}
    for filename, sites in data["files"].items():
        data["lines"][filename] = {int(key.split(":")[-2]) for key in sites}
        for key, records in sites.items():
            sites[key] = [(state, int(mask, 16)) for state, mask in records]
    return data


def exemptions(path, old, candidate, data, replaced, inserted):
    if not data or inserted or not replaced or not (replaced & data["lines"].get(path, set())):
        return 0
    changed = []
    if not changed_predicates(old, candidate, changed) or not changed:
        return 0
    exempt = data["complete"]
    for node, op, changes in changed:
        prefix = "B:" if isinstance(node, ast.BoolOp) else ""
        site = data["files"][path].get(f"{prefix}{node.lineno}:{node.col_offset}")
        if site is None:
            return 0
        observed = 0
        for entry, bits in site:
            observed |= bits
            if not stable(entry, op, changes):
                exempt &= ~bits
        exempt &= observed
    return exempt

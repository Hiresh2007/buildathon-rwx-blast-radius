"""Test-specific equivalence of equality and identity comparisons with None."""
import ast
import gzip
import json

from ast_syntax import same_syntax


def singleton_operand(node):
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return None
    if not isinstance(node.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)):
        return None
    right = node.comparators[0]
    if isinstance(right, ast.Constant) and right.value is None:
        return node.left
    if isinstance(node.left, ast.Constant) and node.left.value is None:
        return right
    return None


def enclosing_nodes(tree, first, last):
    """Walk only syntax that could enclose the edited source lines."""
    stack = [tree]
    while stack:
        node = stack.pop()
        if hasattr(node, "lineno") and (node.lineno > first or node.end_lineno < last):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


class ObservedSites:
    def __init__(self, path, ids):
        with gzip.open(path, "rt") as stream:
            data = json.load(stream)
        if data["ids"] != ids:
            raise ValueError("operand map uses a different inventory")
        self.complete = int(data["complete"], 16)
        self.files = {}
        self.lines = {}
        for path, sites in data["files"].items():
            locations = self.files[path] = {}
            lines = self.lines[path] = set()
            for key, (end, *masks) in sites.items():
                start, column = map(int, key.split(":"))
                lines.update(range(start, end + 1))
                locations[start, column] = tuple(int(mask, 16) for mask in masks)


class NoneComparisons(ObservedSites):
    def exemptions(self, path, source, candidate, replaced, inserted, *, fully_passive=False):
        if inserted or not replaced or not replaced.issubset(self.lines.get(path, ())):
            return 0
        first, last = min(replaced), max(replaced)
        previous = {
            (node.lineno, node.col_offset): node
            for node in enclosing_nodes(source.tree, first, last)
            if singleton_operand(node) is not None and node.lineno <= first and last <= node.end_lineno
        }
        for node in enclosing_nodes(candidate, first, last):
            if singleton_operand(node) is None:
                continue
            site = node.lineno, node.col_offset
            old = previous.get(site)
            row = self.files[path].get(site)
            if old is None or row is None:
                continue
            seen, unsafe_eq, unsafe_ne = row
            operators = {type(old.ops[0]), type(node.ops[0])}
            if operators == {ast.Is, ast.Eq}:
                unsafe = unsafe_eq
            elif operators == {ast.IsNot, ast.NotEq}:
                unsafe = unsafe_ne
            else:
                continue
            edited_ops = node.ops
            node.ops = old.ops
            try:
                if same_syntax(candidate, source.tree):
                    if fully_passive and unsafe:
                        return 0
                    return self.complete & seen & ~unsafe
            finally:
                node.ops = edited_ops
        return 0

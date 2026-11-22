"""Compare parsed syntax independently of source locations and formatting."""
import ast


def same_syntax(left, right):
    if left is right:
        return True
    kind = type(left)
    if kind is not type(right):
        return False
    if isinstance(left, ast.AST):
        for field in left._fields:
            if not same_syntax(getattr(left, field), getattr(right, field)):
                return False
        return True
    if kind in (list, tuple):
        if len(left) != len(right):
            return False
        for a, b in zip(left, right):
            if not same_syntax(a, b):
                return False
        return True
    if kind in (float, complex):
        return repr(left) == repr(right)
    return left == right


def same_module_syntax(original, candidate, changed_line):
    """Check the edited statement first, then prove equality for the complete module."""
    left, right = original, candidate
    while isinstance(getattr(left, "body", None), list):
        body = getattr(right, "body", None)
        if not isinstance(body, list) or len(left.body) != len(body):
            return False
        index = next((i for i, stmt in enumerate(left.body)
                      if isinstance(stmt, ast.stmt) and stmt.lineno <= changed_line <= stmt.end_lineno), None)
        if index is None:
            break
        left, right = left.body[index], body[index]
        if type(left) is not type(right):
            return False
    if left is not original and not same_syntax(left, right):
        return False
    return same_syntax(original, candidate)


class SyntaxIndex:
    """Intern source syntax within one comparison, ignoring source locations."""

    def __init__(self):
        self.nodes = {}
        self.forms = {}

    def key(self, value):
        kind = type(value)
        if isinstance(value, ast.AST):
            known = self.nodes.get(value)
            if known is not None:
                return known
            form = (kind, *(self.key(getattr(value, field)) for field in value._fields))
            index = self.forms.setdefault(form, len(self.forms))
            self.nodes[value] = index
            return index
        if kind in (list, tuple):
            form = (kind, *(self.key(item) for item in value))
        elif kind in (float, complex):
            form = (kind, repr(value))
        else:
            form = (kind, value)
        return self.forms.setdefault(form, len(self.forms))

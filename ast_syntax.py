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

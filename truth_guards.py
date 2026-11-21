"""Observe a conditional's already evaluated operand before checking its truth."""
import ast
from none_comparisons import ObservedSites, enclosing_nodes
from ast_syntax import same_syntax


GUARDS = (ast.If, ast.While, ast.IfExp)
OPERANDS = (ast.Name, ast.Attribute, ast.Call, ast.Subscript)
# State order is false non-bool, true non-bool, None, False, True.
RESULTS = {
    "truth": (False, True, False, False, True),
    "not": (True, False, True, True, False),
    "is_none": (False, False, True, False, False),
    "not_none": (True, True, False, True, True),
    "is_false": (False, False, False, True, False),
    "not_false": (True, True, True, False, True),
    "is_true": (False, False, False, False, True),
    "not_true": (True, True, True, True, False),
}


def guard_form(test):
    if isinstance(test, OPERANDS):
        return "truth", test
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and isinstance(test.operand, OPERANDS):
        return "not", test.operand
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], (ast.Is, ast.IsNot)):
        return None
    value, constant = test.left, test.comparators[0]
    if isinstance(value, ast.Constant):
        value, constant = constant, value
    if not isinstance(value, OPERANDS) or not isinstance(constant, ast.Constant):
        return None
    if constant.value is None:
        name = "none"
    elif type(constant.value) is bool:
        name = "true" if constant.value else "false"
    else:
        return None
    return ("is_" if isinstance(test.ops[0], ast.Is) else "not_") + name, value


class TruthGuards(ObservedSites):
    def exemptions(self, path, source, candidate, replaced, inserted):
        if inserted or not replaced or not replaced.issubset(self.lines.get(path, ())):
            return 0
        first, last = min(replaced), max(replaced)
        previous = {
            (node.test.lineno, node.test.col_offset): node.test
            for node in enclosing_nodes(source.tree, first, last)
            if isinstance(node, GUARDS) and node.test.lineno <= first and last <= node.test.end_lineno
        }
        for node in enclosing_nodes(candidate, first, last):
            if not isinstance(node, GUARDS):
                continue
            test = node.test
            site = test.lineno, test.col_offset
            original = previous.get(site)
            record = self.files[path].get(site)
            if original is None or record is None:
                continue
            before, after = guard_form(original), guard_form(test)
            if before is None or after is None or before[0] == after[0] or not same_syntax(before[1], after[1]):
                continue
            node.test = original
            try:
                if not same_syntax(candidate, source.tree):
                    continue
            finally:
                node.test = test
            seen, unknown, *states = record
            safe = self.complete & seen & ~unknown
            for left, right, mask in zip(RESULTS[before[0]], RESULTS[after[0]], states):
                if left != right:
                    safe &= ~mask
            return safe
        return 0

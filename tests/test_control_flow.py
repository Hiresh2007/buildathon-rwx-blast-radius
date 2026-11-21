import ast
import unittest
from types import CodeType

from execution import changes, first_changed_blocks, guarded_returns_only, instructions


def scope(text):
    tree = ast.parse(text)
    code = compile(tree, 'sqlglot/render.py', 'exec', dont_inherit=True)
    return tree, next(obj for obj in code.co_consts if isinstance(obj, CodeType))


def touched_lines(before, after):
    _, left = scope(before)
    _, right = scope(after)
    old_ops, a = instructions(left)
    new_ops, b = instructions(right)
    touched = first_changed_blocks(left, right, old_ops, a, new_ops, b)
    if touched is None:
        return None
    return {op.positions.lineno for op in old_ops if op.offset in touched}


class ControlFlowTests(unittest.TestCase):
    def test_insertion_in_one_arm_preserves_other_arm(self):
        old = 'def render(value, flag):\n    if flag:\n        return value\n    return 0\n'
        new = old.replace('        return value', '        if value is None:\n            return 0\n        return value')
        self.assertEqual(touched_lines(old, new), {3})
        tree, code = scope(old)
        new_tree, new_code = scope(new)
        self.assertTrue(guarded_returns_only(tree, new_tree, code, new_code))
        self.assertTrue(guarded_returns_only(new_tree, tree, new_code, code))

    def test_new_call_cannot_qualify_as_a_passive_guard(self):
        old = 'def render(value, flag):\n    if flag:\n        return value\n    return 0\n'
        new = old.replace('        return value', '        if value is None:\n            return value.read()\n        return value')
        tree, code = scope(old)
        new_tree, new_code = scope(new)
        self.assertFalse(guarded_returns_only(tree, new_tree, code, new_code))
        self.assertEqual(touched_lines(old, new), {3})

    def test_class_origin_can_move_without_exempting_its_body(self):
        source = "def choose(value):\n    return value\nclass C:\n    label = 41\n"
        shifted = source.replace("    return value", "    if value is None:\n        return 0\n    return value")
        original = compile(source, "module.py", "exec")
        changed = compile(shifted, "module.py", "exec")
        with self.assertRaises(ValueError):
            changes(original, changed)
        deferred = changes(original, changed, class_origins={"C"})
        self.assertEqual([old.co_qualname for old, _ in deferred], ["choose"])
        with self.assertRaises(ValueError):
            changes(original, compile(shifted.replace("label = 41", "label = 42"), "module.py", "exec"), class_origins={"C"})

    def test_shared_target_after_loop_is_compared(self):
        old = 'def render(numbers):\n    for x in numbers:\n        if x:\n            break\n    return 1\n'
        new = old.replace('    return 1', '    return len(numbers)')
        self.assertEqual(touched_lines(old, new), {2, 4})

    def test_exception_handler_preserves_coverage_of_the_body(self):
        old = 'def render(x):\n    try:\n        return x()\n    except ValueError:\n        return 1\n'
        new = old.replace('return x()', 'return x(4)')
        self.assertEqual(touched_lines(old, new), {3})


if __name__ == '__main__':
    unittest.main()

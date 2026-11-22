import ast
import unittest

from deferred_calls import changed_functions
from tools.build_code_readers import inspects_code


def changed(before, after):
    a, b = ast.parse(before), ast.parse(after)
    return changed_functions(a, b, compile(a, "example.py", "exec"), compile(b, "example.py", "exec"))


class DeferredCallsTests(unittest.TestCase):
    def test_nested_function_layout_can_change_inside_uncalled_ordinary_body(self):
        first = "class C:\n    def compute(self, n):\n        return n + 1\n"
        second = "class C:\n    def compute(self, n):\n        def transform(x): return x + 3\n        return transform(n)\n"
        self.assertEqual([c.co_qualname for c in changed(first, second)], ["C.compute"])

    def test_eager_namespace_or_function_metadata_requires_module_floor(self):
        a = "class C:\n    def f(self): return 1\n"
        for b in (
            "class C:\n    def f(self): return super().f()\n",
            "class C:\n    def f(self, x=None): return x\n",
            'class C:\n    def f(self):\n        "doc"\n        return 2\n',
            "class C:\n    def f(self): yield 3\n",
        ):
            with self.subTest(b=b):
                self.assertIsNone(changed(a, b))

    def test_changes_to_global_and_external_state_keep_the_module_floor(self):
        original = "def f(x): return x + 1\n"
        for revised in (
            "def f(x):\n    shared.update(x)\n    return x\n",
            "def f(x):\n    library.publish(x)\n    return x\n",
            "def f(x):\n    global value\n    value = x\n    return x\n",
            'def f(x):\n    open("result", "w")\n    return x\n',
        ):
            with self.subTest(revised=revised):
                self.assertIsNone(changed(original, revised))

    def test_compiled_observers_include_strings_and_ignore_comments(self):
        self.assertTrue(inspects_code('def inspect(f): return getattr(f, "__code__")', "example.py"))
        self.assertTrue(inspects_code("def inspect(f): return f.__closure__", "example.py"))
        self.assertFalse(inspects_code("# __code__ is not read here\ndef f(): return 2", "example.py"))

    def test_duplicate_member_names_do_not_alias_in_compiled_scopes(self):
        a = "class C:\n    def f(self): return 1\n    def f(self): return 2\n"
        self.assertIsNone(changed(a, a.replace("return 1", "return 3")))

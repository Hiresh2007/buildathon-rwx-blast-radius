import ast
import enum
from types import ModuleType
import unittest

from module_impact import declarations, dispatch_change
from symbols import verified_operand


class RegistryBindingTests(unittest.TestCase):
    path = "sqlglot/parsers/example.py"

    def test_added_and_removed_inherited_token_sets_and_local_aliases(self):
        first = "class C(Base):\n"
        method = "    def transform(self): return self.TOKENS\n"
        fields = "    TOKENS = owner.Base.TOKENS | {Token.END}\n    ALIASES = TOKENS\n"
        checks = set()
        a, b = ast.parse(first + fields + method), ast.parse(first + method)
        self.assertEqual(declarations(self.path, a, b, set(), checks), {"C"})
        self.assertEqual(declarations(self.path, b, a, set(), set()), {"C"})
        self.assertIn(("C", "TOKENS", "set", "owner.Base.TOKENS"), checks)
        self.assertIn(("C", "ALIASES", "inherited-set", "ALIASES"), checks)
        self.assertIsNone(declarations(self.path, ast.parse(first + fields + "    current = repr(TOKENS)\n" + method),
                                       ast.parse(first + "    current = repr(TOKENS)\n" + method), set(), set()))
        self.assertIsNone(declarations(self.path, ast.parse(first + fields.replace("owner.Base.TOKENS", "get_tokens()") + method),
                                       b, set(), set()))

    def test_normal_function_bindings_have_passive_lookup(self):
        m = ModuleType("sqlglot.parsers.example")
        def render(value):
            return str(value)
        m.C = type("C", (), {})
        m.render = render
        old = ast.parse("TABLE = {'X': lambda v: str(v)}").body[0]
        new = ast.parse("TABLE = {'X': render}").body[0]
        planned = set()
        self.assertTrue(dispatch_change(old, new, planned))
        self.assertEqual(planned, {("reference", "render")})
        self.assertTrue(verified_operand(m, "C", "TABLE", "reference", "render", {}))
        m.C.render = staticmethod(render)
        self.assertFalse(verified_operand(m, "C", "TABLE", "reference", "render", {}))
        old_source = "class C(Base):\n    render = object()\n    TABLE = {'X': lambda v: str(v)}\n    del render\n"
        new_source = old_source.replace("lambda v: str(v)", "render")
        self.assertIsNone(declarations(self.path, ast.parse(old_source), ast.parse(new_source), set(), set()))

    def test_local_factory_uses_the_compiled_patched_declaration(self):
        m = ModuleType("sqlglot.parsers.example")
        code = ("from __future__ import annotations\n"
                "def make(name):\n    def format(value): return name + str(value)\n    return format\n"
                "class C: TABLE = {}\n")
        exec(compile(code, "example.py", "exec", dont_inherit=True), vars(m))
        node = ast.parse(code).body[1]
        self.assertTrue(verified_operand(m, "C", "TABLE", "factory", "make", {m.__name__: code}, {m.make.__code__: node}))
        self.assertFalse(verified_operand(m, "C", "TABLE", "factory", "make", {m.__name__: code}, {}))
        changed = ast.parse(code.replace("return format", "observe(name)\n    return format")).body[1]
        self.assertFalse(verified_operand(m, "C", "TABLE", "factory", "make", {m.__name__: code},
                                          {m.make.__code__: changed}))

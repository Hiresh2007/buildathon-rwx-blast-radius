import ast
import enum
from types import ModuleType
import unittest

from ast_syntax import SyntaxIndex

from module_impact import dispatch_change
from symbols import verified_operand


class Token(enum.IntEnum):
    START = 1
    END = 2


class Generator:
    TOKENS = {Token.START}


class Expression:
    @classmethod
    def from_arg_list(cls, args):
        return cls()


class RegistryOperandTests(unittest.TestCase):
    def setUp(self):
        self.module = ModuleType("sqlglot.generators.example")
        self.module.Registry = type("Registry", (), {})
        self.module.types = ModuleType("sqlglot.example_types")
        self.module.types.Expression = Expression
        self.module.types.Generator = Generator
        self.module.Token = Token

    def plan(self, old, new):
        checks = set()
        match = dispatch_change(ast.parse(old).body[0], ast.parse(new).body[0], checks)
        return match, checks

    def validate(self, checks):
        return all(verified_operand(self.module, "Registry", "TABLE", kind, name, {})
                   for kind, name in checks)

    def test_structural_keys_preserve_literal_types_and_expression_shape(self):
        index = SyntaxIndex()
        trees = [ast.parse(text, mode="eval") for text in
                 ("True", "1", "1.0", "0.0", "-0.0", "0j", "b'x'", "'x'", "x", "[x]", "(x,)",
                  "lambda: x", "lambda: y", "f(x)", "f(y)", "{'x': f(x)}")]
        self.assertEqual(len({index.key(tree) for tree in trees}), len(trees))
        for tree in trees:
            self.assertEqual(index.key(tree), index.key(ast.parse(ast.unparse(tree), mode="eval")))
        self.assertNotEqual(index.key(ast.Constant(value=0.0)), index.key(ast.Constant(value=-0.0)))

    def test_class_to_lambda_registrations_and_classmethod_references(self):
        old = "TABLE = {**Base.TABLE, 'NOW': lambda args: args}"
        new = "TABLE = {**Base.TABLE, 'NOW': lambda args: args, 'X': types.Expression.from_arg_list}"
        match, checks = self.plan(old, new)
        self.assertTrue(match)
        self.assertEqual(checks, {("reference", "types.Expression.from_arg_list")})
        self.assertTrue(self.validate(checks))
        match, checks = self.plan(old, old.replace("'NOW'", "types.Expression"))
        self.assertTrue(match)
        self.assertTrue(self.validate(checks))
        self.module.types.Expression = ModuleType("placeholder")
        self.assertFalse(self.validate(checks))

    def test_builtin_enum_sets_are_checked_before_removing_tests(self):
        old = "TABLE = (types.Generator.TOKENS - {Token.START}) | {Token.START}"
        new = old.replace("| {Token.START}", "| {Token.START, Token.END}")
        match, checks = self.plan(old, new)
        self.assertTrue(match)
        self.assertTrue(self.validate(checks))
        self.module.Registry.Token = {}
        self.assertFalse(self.validate(checks))
        del self.module.Registry.Token
        self.module.types.Generator = type("Generator", (), {"TOKENS": [Token.START]})
        self.assertFalse(self.validate(checks))

    def test_registry_calls_keep_their_existing_shape_and_order(self):
        old = "TABLE = {**Base.T, 'A': rename('A'), **Other.T}"
        new = old[:-1] + ", 'B': rename('B')}"
        match, checks = self.plan(old, new)
        self.assertTrue(match)
        self.assertEqual(checks, {("factory", "rename")})
        self.assertFalse(self.plan(old, new.replace("rename('B')", "other('B')"))[0])
        self.assertFalse(self.plan(old, "{**Other.T, **Base.T}")[0])
        self.assertFalse(self.plan(old, "TABLE = {**Other.T, 'A': rename('A'), **Base.T}")[0])


if __name__ == "__main__":
    unittest.main()

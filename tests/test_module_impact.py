import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from module_impact import declarations, digest, module_tests, only_added_tests, test_sources


class ModuleBoundaryTests(unittest.TestCase):
    path = "sqlglot/parsers/example.py"
    prefix = "from __future__ import annotations\nclass Parser(Base):\n"
    first = "    def first(self):\n        return 1\n"
    second = "    def second(self):\n        return 2\n"

    def declarations(self, old, new, lines=(), path=None):
        return declarations(path or self.path, ast.parse(old), ast.parse(new), set(lines))

    def test_deferred_body_changes_have_no_import_probe(self):
        old = self.prefix + self.first + self.second
        new = old.replace("return 1", "return self.second()")
        self.assertEqual(self.declarations(old, new), set())
        self.assertIsNone(self.declarations(old, new, [4]))
        for changed in (old.replace("return 1", "yield 1"), old.replace("first(self)", "first(self, limit=1)"),
                        old.replace("return 1", '"""updated"""\n        return 1')):
            self.assertIsNone(self.declarations(old, changed))

    def test_class_method_additions_and_deletions_validate_the_owner(self):
        old = self.prefix + self.first
        revised = self.prefix + self.second + self.first
        self.assertEqual(self.declarations(old, revised), {"Parser"})
        self.assertEqual(self.declarations(revised, old), {"Parser"})
        self.assertIsNone(self.declarations(old, revised, path="sqlglot/other.py"))
        self.assertIsNone(self.declarations(revised, self.prefix + self.first + self.second))
        for suffix in ("    probe = locals()\n", "    @register\n    def final(self): pass\n",
                       "    def final(self, value=build()): pass\n", "select(Parser)\n"):
            self.assertIsNone(self.declarations(old + suffix, revised + suffix))

    def test_dispatch_edits_require_literal_strings_and_deferred_functions(self):
        old = self.prefix + "    TABLE = {**Base.TABLE, 'X': lambda self: self.first()}\n" + self.first
        self.assertEqual(self.declarations(old, old.replace("'X'", "'Y'")), {"Parser"})
        for replacement in ("factory()", "descriptor.value", "lambda self, arg=build(): arg"):
            self.assertIsNone(self.declarations(old, old.replace("lambda self: self.first()", replacement)))
        self.assertIsNone(self.declarations(old, old.replace("**Base.TABLE", "**Base.OTHER")))

    def test_module_statements_and_new_module_bindings_keep_the_full_schedule(self):
        old = "active = 4\ndef f():\n    return active\n"
        self.assertIsNone(self.declarations(old, old.replace("4", "7")))
        self.assertIsNone(self.declarations(old, old + "def _helper(): return 3\n"))

    def test_only_plain_new_tests_in_known_existing_classes_are_eligible(self):
        old = "class TestParser(Validator):\n    def test_original(self): pass\n"
        new = old + "    def test_added(self):\n        self.assertEqual(2, 2)\n"
        eligible = lambda text: only_added_tests(ast.parse(old), ast.parse(text), {"TestParser"})
        self.assertTrue(eligible(new))
        for text in (old, new.replace("test_original", "test_changed"), new.replace("test_added", "helper"),
                     new.replace("def test_added", "@pytest.mark.usefixtures('shared')\n    def test_added"),
                     new.replace("test_added(self)", "test_added(self, value=factory())"),
                     new.replace("def test_original(self): pass", "def test_original(self): raise RuntimeError"),
                     new + "    def test_added(self): pass\n"):
            self.assertFalse(eligible(text))
        self.assertFalse(only_added_tests(ast.parse(old), ast.parse(new), {"Other"}))

    def test_file_mask_includes_cold_uncertainty_and_transitive_symbols(self):
        class Index(SimpleNamespace):
            __hash__ = object.__hash__
        path = "sqlglot/example.py"
        original = "sqlglot.example:Example.TABLE"
        derived = "sqlglot.other:Other.TABLE"
        bits = lambda *places: sum(1 << n for n in places)
        symbols = SimpleNamespace(incomplete=bits(4), modules={"sqlglot.consumer": bits(11)},
                                  classes={}, declared={original}, raw={derived: bits(9)}, copies={},
                                  keys={}, initialized={}, initialized_keys={derived: {"k": ["sqlglot.consumer"]}},
                                  derived={original: [derived]})
        execution = SimpleNamespace(complete=True, untraced=bits(3), child_reads={path: bits(6)})
        index = Index(execution_ids=["a"], all_bits=(1 << 13) - 1, unknown=bits(1), incomplete=bits(2),
                      isolated={path: bits(0)}, reads={path: bits(5)}, files={path: {1: bits(7)}},
                      symbols=symbols, execution=execution)
        data = dict(ids=["a"], reads={path: "1"}, files={path: {"fn": ["key", "2", "4", []]}},
                    groups=[hex(bits(8)), hex(bits(10)), hex(bits(12))])
        with patch("module_impact.profile", return_value=data):
            self.assertEqual(module_tests(index, path), index.all_bits)
            self.assertEqual(module_tests(index, "sqlglot/missing.py"), index.all_bits)

    def test_packaged_test_sources_match_the_pinned_checkout(self):
        import os
        root = Path(os.environ.get("SQLGLOT_ROOT", "/opt/sqlglot"))
        if not root.is_dir():
            self.skipTest("SQLGlot source checkout unavailable")
        manifest = test_sources()
        self.assertTrue(manifest)
        for name, expected in manifest.items():
            self.assertEqual(digest((root / name).read_text()), expected, name)


if __name__ == "__main__":
    unittest.main()

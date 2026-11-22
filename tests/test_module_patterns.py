import ast
from pathlib import Path
import re
import tempfile
from types import ModuleType
import unittest

from module_patterns import changed_pattern, private_users
from symbols import verified_operand


class ModulePatternTests(unittest.TestCase):
    def change(self, prefix="import re\n", suffix="def parse(s): return PATTERN.search(s)\n"):
        old = ast.parse(prefix + 'PATTERN = re.compile("a+")\n' + suffix)
        new = ast.parse(prefix + 'PATTERN = re.compile("a*")\n' + suffix)
        return changed_pattern(old, new, old.body[-len(ast.parse(suffix).body)-1], new.body[-len(ast.parse(suffix).body)-1])

    def test_private_deferred_regex_only(self):
        self.assertEqual(self.change(), ("PATTERN", "a*"))
        for suffix in ("VALUE = PATTERN.search('a')\n", "def parse(s=PATTERN.search('x')): pass\n",
                       "def get(): return PATTERN\n", "def parse(): return vars()\n",
                       "from example import re\ndef parse(s): return PATTERN.search(s)\n"):
            with self.subTest(suffix=suffix):
                self.assertIsNone(self.change(suffix=suffix))

    def test_external_pattern_consumers_and_cache_observers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sqlglot").mkdir()
            (root / "tests").mkdir()
            (root / "sqlglot/owner.py").write_text("PATTERN = None\n")
            for text in ("from sqlglot.owner import PATTERN\n", "cache = re._cache\n"):
                (root / "tests/test_other.py").write_text(text)
                private_users.cache_clear()
                self.assertFalse(private_users(root, "sqlglot/owner.py", "PATTERN"))

    def test_import_verifier_requires_standard_pattern_and_binding(self):
        module = ModuleType("example")
        module.re = re
        module.PATTERN = re.compile("abc")
        self.assertTrue(verified_operand(module, "", "PATTERN", "module-pattern", "abc", {}))
        self.assertFalse(verified_operand(module, "", "PATTERN", "module-pattern", "other", {}))
        module.re = object()
        self.assertFalse(verified_operand(module, "", "PATTERN", "module-pattern", "abc", {}))

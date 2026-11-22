import ast
import difflib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cold_coverage import digest
from impact import Source
from module_impact import boundary_selection


class ModuleFixtureTests(unittest.TestCase):
    def test_coedited_fixture_keeps_each_reader_and_requires_a_matching_hunk(self):
        production = "sqlglot/optimizer/example.py"
        fixture = "tests/fixtures/optimizer/example.sql"
        original = "def transform(x):\n    return x\n"
        revised = original.replace("return x", "return x + 1")

        def change(name, before, after):
            return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                "a/" + name, "b/" + name))

        with TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / fixture
            data.parent.mkdir(parents=True)
            data.write_text("SELECT 1;\n")
            original_diff = change(production, original, revised)
            fixture_diff = change(fixture, "SELECT 1;\n", "SELECT 2;\n")
            index = SimpleNamespace(root=root, execution_ids=list("abcdefgh"), unknown=32, incomplete=0,
                                    execution=SimpleNamespace(untraced=0, child_reads={fixture: 8}),
                                    isolated={fixture: 4}, reads={fixture: 2}, test_files={}, import_lines={},
                                    cache_consumers=SimpleNamespace(floor=lambda *args: 0),
                                    tests_from_bits=lambda bits: {i for i in range(8) if bits & 1 << i})
            profile = {"ids": index.execution_ids, "hashes": {production: digest(original)},
                       "groups": ["10"], "reads": {fixture: "1"}}
            prepared = {production: (Source(original, production), revised, ast.parse(revised), {2}, set())}
            selected = set(range(8))
            def result(text):
                return boundary_selection(text, index, prepared, selected, lambda tests: 3000 * len(tests), {6})
            with patch("module_impact.profile", return_value=profile), patch("module_impact.module_tests", return_value=1):
                self.assertEqual(result(original_diff + fixture_diff), set(range(7)))
                self.assertEqual(result(original_diff + fixture_diff.replace("-SELECT 1;", "-SELECT 7;")), selected)
                del index.isolated[fixture]
                self.assertEqual(result(original_diff + fixture_diff), selected)

import ast
import difflib
import gzip
import json
import os
from pathlib import Path
import unittest

from execution import ScopeChange, changes
from impact import ImpactIndex, choose_tests
from selector import fill_slack, schedule


class SchedulerTests(unittest.TestCase):
    def test_padding_preserves_worker_cost_and_candidate_tests(self):
        durations = {0: 10000, 1: 4500, 2: 4300, 3: 75, 4: 1800, 5: 1000}
        workers = schedule({0, 1, 2}, durations)
        self.assertEqual(len(workers), 2)
        def cost():
            return max(sum(durations[n] for n in worker) for worker in workers)
        original = cost()
        fill_slack(workers, [{1, 3}, {3, 4, 5}], durations)
        scheduled = [n for worker in workers for n in worker]
        self.assertEqual(cost(), original)
        self.assertCountEqual(scheduled, {0, 1, 2, 3, 5})

    def test_constants_compiled_from_ast_and_text_match(self):
        old = 'class Expressions:\n    entries = {"SQL_ALIAS": lambda row: row[1], "TABLE": None}\n'
        new = old.replace('row[1]', 'row[2]')
        observed = changes(compile(old, 'sample.py', 'exec'),
                           compile(ast.parse(new), 'sample.py', 'exec'))
        self.assertEqual([before.co_qualname for before, _ in observed], ['Expressions.<lambda>'])

    def test_generator_conversion_changes_all_calls(self):
        old = 'def transform(value):\n    if value:\n        return value\n    return None\n'
        new = old.replace('return None', 'yield None')
        with self.assertRaises(ScopeChange):
            changes(compile(old, 'sample.py', 'exec'), compile(new, 'sample.py', 'exec'))


def patch(path, old, new):
    return ''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                       'a/' + path, 'b/' + path))


class PinnedSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo = Path(__file__).resolve().parents[1]
        source = Path(os.environ.get('SQLGLOT_ROOT', repo / '.cache/sqlglot'))
        if not (source / 'sqlglot/tokens.py').is_file():
            raise unittest.SkipTest('set SQLGLOT_ROOT to the pinned checkout')
        with gzip.open(repo / 'coverage.json.gz') as stream:
            data = json.load(stream)
        # Remap IDs to exercise selections against an inventory in a different order.
        tests = [dict(id=name, number=i) for i, name in enumerate(reversed(data['ids']))]
        tests.append(dict(id='tests/extra.py::new_test', number=len(tests)))
        cls.index = ImpactIndex(data, tests, source)
        cls.unknown = tests[-1]['number']

    def test_unknown_live_tests_and_readers_survive_text_edits(self):
        name = 'sqlglot/tokens.py'
        old = self.index.source(name).text
        selected = choose_tests(patch(name, old, '# comment\n' + old), self.index)
        self.assertIn(self.unknown, selected)
        self.assertNotEqual(selected, self.index.all)

    def test_invalid_source_encoding(self):
        name = 'sqlglot/tokens.py'
        old = self.index.source(name).text
        self.assertEqual(choose_tests(patch(name, old, '# coding: ascii\n' + old),
                                      self.index), self.index.all)

    def test_new_import_outside_replaced_function(self):
        name = 'sqlglot/helper.py'
        old = self.index.source(name).text
        new = old.replace('return int(number)', 'return int(number) + 0\n\nimport unavailable_example', 1)
        self.assertNotEqual(new, old)
        self.assertEqual(choose_tests(patch(name, old, new), self.index), self.index.all)

    def test_multiple_semantic_files(self):
        change = ''
        for name in ['sqlglot/helper.py', 'sqlglot/tokens.py']:
            old = self.index.source(name).text
            change += patch(name, old, old + '\nADDED_VALUE = 3\n')
        self.assertEqual(choose_tests(change, self.index), self.index.all)


if __name__ == '__main__':
    unittest.main()

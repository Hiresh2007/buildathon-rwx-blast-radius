import ast
import difflib
import gzip
import json
import os
import sys
from pathlib import Path
import unittest

from ast_syntax import same_syntax
from execution import ExecutionIndex, ScopeChange, changes, compiled_candidate, only_local_conditions, scalar_replacements
from impact import ImpactIndex, Source, _apply, _parse_diff, _separate_declarations, _statements, choose_tests
from selector import fill_slack, schedule


class SchedulerTests(unittest.TestCase):
    def test_separates_class_declaration_and_method_body_without_mutating_source(self):
        path = "sqlglot/example.py"
        old = 'class Model:\n    CONFIG = {"x": 1}\n\n    def value(self):\n        return 2\n'
        updated = old.replace('"x": 1', '"x": 3').replace("return 2", "return 7")
        change = _parse_diff(patch(path, old, updated))[0]
        source = Source(old, path)
        text, replaced, inserted = _apply(old, change.hunks)
        tree = ast.parse(text)
        parts = _separate_declarations(change, (source, text, tree, replaced, inserted))
        self.assertEqual([p[3] for p in parts], [{2}, {5}])
        self.assertEqual([ast.dump(p[2]) for p in parts],
                         [ast.dump(ast.parse(old.replace('"x": 1', '"x": 3'))),
                          ast.dump(ast.parse(old.replace('return 2', 'return 7')))])
        self.assertEqual(ast.dump(source.tree), source.syntax)
        self.assertEqual(ast.dump(tree), ast.dump(ast.parse(updated)))
        original_code = compiled_candidate(tree, path)
        self.assertIs(original_code, compiled_candidate(tree, path))
        for part in parts:
            self.assertIsNot(original_code, compiled_candidate(part[2], path))
            self.assertEqual(compiled_candidate(part[2], path), compile(part[2], path, "exec", dont_inherit=True))

    def test_inserted_statements_require_an_existing_conditional_line_entry(self):
        from probe import inserted_branch_entry

        path = "sqlglot/example.py"
        old = "def value(x):\n    if x:\n        return pow(x, 2)\n    return 0\n"
        source = Source(old, path)
        addition = "        if x is None:\n            raise ValueError()\n"
        updated = old.replace("        return pow", addition + "        return pow")
        tree = ast.parse(updated)
        before = ast.dump(tree)
        self.assertEqual(inserted_branch_entry(source, tree, path, 3, 2, ExecutionIndex), "1:value")
        self.assertEqual(ast.dump(tree), before)
        changed_elsewhere = ast.parse(updated.replace("return 0", "return 1"))
        self.assertIsNone(inserted_branch_entry(source, changed_elsewhere, path, 3, 2, ExecutionIndex))
        new_local = ast.parse(old.replace("        return pow", "        surprise = 1\n        return pow"))
        self.assertIsNone(inserted_branch_entry(source, new_local, path, 3, 1, ExecutionIndex))
        self.assertIsNone(inserted_branch_entry(source, tree, path, 4, 2, ExecutionIndex))

    def test_executor_entry_requires_cold_and_warm_misses_and_a_single_changed_file(self):
        from types import SimpleNamespace
        from unittest.mock import patch as mock
        from cold_coverage import digest
        from probe import GROUPS, QueryProbe

        filename = "sqlglot/example.py"
        old = "def value(x):\n    if x:\n        return abs(x)\n    return 0\n"
        new = old.replace("        return abs", "        if x is None:\n            return 3\n        return abs")
        delta = patch(filename, old, new)
        text, replaced, inserted = _apply(old, _parse_diff(delta)[0].hunks)
        prepared = {filename: (Source(old, filename), text, ast.parse(text), replaced, inserted)}
        probe = QueryProbe.__new__(QueryProbe)
        probe.numbers = {GROUPS[0]: 7}
        probe.cases = [(GROUPS[0], 0, {}, {})]
        probe.lines = {}
        probe.setup_bit = 16
        execution = SimpleNamespace(child_reads={}, untraced=0, baseline=ExecutionIndex.baseline)
        index = SimpleNamespace(import_lines={}, reads={}, files={}, execution=execution, incomplete=0, unknown=0,
                                cache_consumers=SimpleNamespace(floor=lambda *args: 0),
                                tests_from_bits=lambda bits: {7} if bits else set())
        selected = {7, 12}
        model = {filename: [digest(old), {"1:value|3": 1}]}
        with mock("probe.recorded_entries", return_value=model):
            self.assertEqual(probe.exclude_unentered_branches(delta, index, selected, prepared), {12})
            mixed = delta + patch("fixture.txt", "one\n", "two\n")
            self.assertEqual(probe.exclude_unentered_branches(mixed, index, selected, prepared), selected)
            probe.lines[filename] = {3: 1}
            self.assertEqual(probe.exclude_unentered_branches(delta, index, selected, prepared), selected)
            probe.lines.clear()
            index.files[filename] = {3: 1}
            self.assertEqual(probe.exclude_unentered_branches(delta, index, selected, prepared), selected)
            index.files.clear()
            model[filename][1].clear()
            self.assertEqual(probe.exclude_unentered_branches(delta, index, selected, prepared), selected)

    def test_default_and_body_of_same_function_are_disjoint(self):
        path = 'sqlglot/example.py'
        old = 'def f(enabled=False):\n    return "old" if enabled else "same"\n'
        new = old.replace('False', 'True').replace('"old"', '"new"')
        change = _parse_diff(patch(path, old, new))[0]
        text, lines, insertions = _apply(old, change.hunks)
        parts = _separate_declarations(change, (Source(old, path), text, ast.parse(text), lines, insertions))
        self.assertCountEqual([ast.dump(p[2]) for p in parts], [
            ast.dump(ast.parse(old.replace('False', 'True'))),
            ast.dump(ast.parse(old.replace('"old"', '"new"'))),
        ])

    def test_zero_duration_tests_fit_existing_single_worker(self):
        durations = {0: 5, 1: 0, 2: 0, 3: 1}
        workers = schedule({0, 1}, durations)
        fill_slack(workers, [{1, 2}], durations)
        self.assertEqual(workers, [[0, 1, 2]])

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

    def test_combined_changes_require_existing_scalar_operands(self):
        def code(body):
            return compile("def f(value):\n    " + body, "case.py", "exec").co_consts[0]
        self.assertTrue(scalar_replacements(code("return value + 1"), code("return value + 2")))
        self.assertFalse(scalar_replacements(code("return value + 1"), code("return len(value)")))
        self.assertFalse(scalar_replacements(code("return value + 1"), code("return value + 'x'")))

    def test_statement_index_includes_handlers_and_match_cases(self):
        source = """def example(value):
    try:
        match value:
            case True:
                return [x for x in (1, 2)]
            case _:
                pass
    except ValueError:
        raise
    finally:
        value = None
"""
        tree = ast.parse(source)
        self.assertEqual(list(_statements(tree)), [node for node in ast.walk(tree) if isinstance(node, ast.stmt)])

    def test_ast_comparison_preserves_constant_types_and_ignores_locations(self):
        self.assertTrue(same_syntax(ast.parse('def f(x): return x + 1'),
                                    ast.parse('\n\ndef f(x):\n    return x + 1\n')))
        self.assertFalse(same_syntax(ast.Constant(True), ast.Constant(1)))
        self.assertFalse(same_syntax(ast.Constant(-0.0), ast.Constant(0.0)))
        self.assertFalse(same_syntax(ast.parse('def f(x): return x'), ast.parse('def f(y): return y')))
        self.assertFalse(same_syntax(ast.parse('x = 1'), ast.parse('x = 1; x = 2')))

    def test_membership_operand_can_change_length(self):
        def code(expression):
            return compile('def f(x):\n    return ' + expression, 'example.py', 'exec').co_consts[0]
        pair = '("a", "b")', '("a", "b", "c")'
        self.assertTrue(scalar_replacements(*(code('x in ' + keys) for keys in pair)))
        self.assertFalse(scalar_replacements(*(code(keys) for keys in pair)))
        self.assertFalse(scalar_replacements(code('x in ("a", "b")'), code('x in ("a", "b", 3)')))

    def test_only_guards_on_local_values_may_change(self):
        original = 'def f(x, y):\n    if x:\n        return y\n    return None\n'
        def matches(candidate):
            old, new = [compile(text, 'sample.py', 'exec').co_consts[0] for text in (original, candidate)]
            return only_local_conditions(ast.parse(original), ast.parse(candidate), old, new)
        self.assertTrue(matches(original.replace('if x:', 'if x is not None and not y:')))
        for condition in ('x.value', 'bool(x)', 'GLOBAL', 'x > 2'):
            self.assertFalse(matches(original.replace('if x:', 'if ' + condition + ':')))
        changed_body = original.replace('if x:', 'if x is not None:').replace('return y', 'return x')
        self.assertFalse(matches(changed_body))

    def test_child_only_function_is_preserved(self):
        path = 'sqlglot/example.py'
        ids = ['parent', 'child']
        tag = sys.implementation.cache_tag
        parent = dict(ids=ids, python=tag, complete=True, files={path: {}}, sets=['2'], untraced=0)
        children = dict(ids=ids, python=tag, platform=sys.platform, complete='2',
                        scopes={path + '|1:active': '2'}, reads={})
        baseline = 'def active():\n    return 1\n\ndef unused():\n    return 7\n'
        index = ExecutionIndex(parent, ids, children)
        self.assertEqual(index.select(path, baseline, ast.parse(baseline.replace('return 1', 'return 2'))), 2)
        self.assertEqual(index.select(path, baseline, ast.parse(baseline.replace('return 7', 'return 8'))), 0)

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

    def test_passive_component_keeps_the_original_callers_of_the_second_edit(self):
        generator = "sqlglot/generator.py"
        parser = "sqlglot/parsers/duckdb.py"
        original_guard = self.index.source(generator).text
        original_parser = self.index.source(parser).text
        guard = patch(generator, original_guard, original_guard.replace(
            "if visible is not None:", "if visible != None:", 1))
        call = patch(parser, original_parser, original_parser.replace(
            "self._parse_alias(self._parse_primary_or_var(), explicit=True)",
            "self._parse_alias(self._parse_primary_or_var(), explicit=False)", 1))
        self.assertTrue(guard and call)
        affected = choose_tests(call, self.index)
        combined = choose_tests(guard + call, self.index)
        self.assertTrue(affected.issubset(combined))
        self.assertIn(self.unknown, combined)
        self.assertLess(len(combined), len(self.index.all))

    def test_multiple_semantic_files(self):
        change = ''
        for name in ['sqlglot/helper.py', 'sqlglot/tokens.py']:
            old = self.index.source(name).text
            change += patch(name, old, old + '\nADDED_VALUE = 3\n')
        self.assertEqual(choose_tests(change, self.index), self.index.all)

    def test_existing_test_body_keeps_module_and_source_readers(self):
        file = 'tests/dialects/test_oracle.py'
        old = self.index.source(file).text
        new = old.replace('self.assertEqual(create.kind, "TABLE")', 'self.assertEqual(create.kind, "VIEW")', 1)
        self.assertNotEqual(old, new)
        selected = choose_tests(patch(file, old, new), self.index)
        self.assertTrue(self.index.tests_from_bits(self.index.test_files[file]).issubset(selected))
        self.assertNotEqual(selected, self.index.all)
        eager = 'raise RuntimeError("collection")\n' + new
        self.assertEqual(choose_tests(patch(file, old, eager), self.index), self.index.all)


if __name__ == '__main__':
    unittest.main()

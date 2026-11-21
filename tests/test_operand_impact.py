import ast
import gzip
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from default_impact import DefaultIndex
from impact import Source
from lookup_domains import LookupDomains, lookup, site
from table_values import TableValues
from tools.build_default_impact import Defaults


class OperandTests(unittest.TestCase):
    def test_baseline_records_default_value_and_reflection_by_code_location(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = "sqlglot/example.py"
            src = "def keep(value='example'):\n    return value\n"
            tests = [dict(id=i, number=n) for n, i in enumerate("abcd")]
            trace = Defaults(root, tests)
            tree = trace.instrument(ast.parse(src), path)
            namespace = {"_rwx_default_missing": object(), "_rwx_default_use": trace.use}
            exec(compile(tree, str(root / path), "exec"), namespace)
            keep = namespace["keep"]
            trace.collecting = False
            with trace.context(1):
                self.assertEqual(keep(), "example")
            with trace.context(2):
                self.assertEqual(keep("different"), "different")
            with trace.context(4):
                keep.__qualname__ = "renamed"
                trace.access("object.__getattr__", (keep, "__defaults__"))
            with trace.context(8):
                self.assertEqual(keep("example"), "example")
            trace.passed = 15
            output = root / "defaults.gz"
            trace.save(output)
            index = DefaultIndex(output, list("abcd"))
            self.assertEqual(index.files[path]["keep|value"], [1 | 4, 0, 0, 0, 0, 1])

    def test_ternary_keeps_default_used_in_untaken_branch(self):
        with TemporaryDirectory() as directory:
            path = "sqlglot/sample.py"
            trace = Defaults(Path(directory), [dict(id="one", number=0)])
            source = "def select(value=False):\n    return value if value else False\n"
            tree = trace.instrument(ast.parse(source), path)
            namespace = {"_rwx_default_missing": object(), "_rwx_default_use": trace.use}
            exec(compile(tree, path, "exec"), namespace)
            trace.collecting = False
            with trace.context(1):
                self.assertFalse(namespace["select"]())
            self.assertEqual(trace.records[0][0][3][1], 1)

    def load(self, cls, records):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "observations.gz"
            path.write_bytes(gzip.compress(json.dumps(records).encode()))
            return cls(path, records["ids"])

    def test_literal_membership_keeps_changed_and_unobserved_needles(self):
        path = 'sqlglot/sample.py'
        src = Source('def f(x):\n    return x in ("a", "b", "d")\n', path)
        expression = src.tree.body[0].body[0].value
        _, operand = lookup(expression)
        tag = site(expression, operand)
        data = {'ids': list('abcde'), 'complete': '1f', 'files': {path: {
            tag: [2, '0f', '08', [[['unrelated'], '1'], [['b'], '2'], [['c'], '4']]]}}}
        domains = self.load(LookupDomains, data)
        edit = src.text.replace('("a", "b", "d")', '("b", "c")')
        self.assertEqual(domains.exemptions(path, src, ast.parse(edit), {2}, set()), 3)
        self.assertEqual(domains.exemptions(path, src, ast.parse(edit.replace('x in', 'x not in')), {2}, set()), 0)
        self.assertEqual(domains.exemptions(path, src, ast.parse(edit.replace('return x', 'return x.value')), {2}, set()), 0)

    def test_absent_dictionary_key_requires_all_observations(self):
        src = Source('def f(d):\n    return d.get("old", 7)\n', "sqlglot/sample.py")
        node = next(n for n in ast.walk(src.tree) if lookup(n))
        tag = site(node, node.args[0])
        # The first caller sees neither key. The second sees the new key on a
        # later call; the third uses a custom mapping with unmodelled behavior.
        data = {
            "ids": ["a", "b", "c"],
            "complete": "7",
            "files": {"sqlglot/sample.py": {tag: [2, "7", "4", [[[], "3"], [["new"], "2"]]]}},
        }
        domain = self.load(LookupDomains, data)
        candidate = ast.parse(src.text.replace('"old"', '"new"'))
        self.assertEqual(domain.exemptions("sqlglot/sample.py", src, candidate, {2}, set()), 1)
        other = ast.parse(src.text.replace('"old"', '"new"').replace("7", "9"))
        self.assertEqual(domain.exemptions("sqlglot/sample.py", src, other, {2}, set()), 0)

    def test_defaults_include_reflecting_and_unmeasured_readers(self):
        path = "sqlglot/sample.py"
        src = Source("def f(x=False):\n    return x\n", path)
        data = {"ids": list("abcde"), "complete": "f", "files": {path: {"f|x": [1, "3"]}}}
        index = SimpleNamespace(
            all_bits=31,
            isolated={path: 16},
            unknown=0,
            incomplete=0,
            reads={path: 4},
            execution=SimpleNamespace(child_reads={}, child_calls={}, untraced=0, child_complete=0),
        )
        domain = self.load(DefaultIndex, data)
        self.assertEqual(
            domain.select(path, src, ast.parse(src.text.replace("False", "True")), {1}, set(), index), 23
        )
        other = ast.parse(src.text.replace("False", "True").replace("return x", "return not x"))
        self.assertIsNone(domain.select(path, src, other, {1, 2}, set(), index))

    def test_combined_changes_retain_every_omitted_call(self):
        path = "sqlglot/sample.py"
        src = Source("def f(x=False):\n    return not x\n", path)
        data = {"ids": list("abc"), "complete": "7", "files": {
            path: {"f|x": [1, ["4", "1", "0", "0", "0", "3"]]}}}
        domain = self.load(DefaultIndex, data)
        index = SimpleNamespace(all_bits=7, isolated={}, unknown=0, incomplete=0, reads={},
                                execution=SimpleNamespace(child_reads={}, child_calls={}, untraced=0, child_complete=0))
        candidate = ast.parse(src.text.replace("False", "True"))
        self.assertEqual(domain.select(path, src, candidate, {1}, set(), index), 5)
        self.assertEqual(domain.select(path, src, candidate, {1}, set(), index, combined=True), 7)

    def test_inserted_table_key_keeps_misses_membership_and_shape_readers(self):
        path = "sqlglot/sample.py"
        src = Source('ITEMS = {\n    "existing": 1,\n}\n', path)
        data = {"ids":list("abcdef"), "complete":"3f", "files":{path:{"ITEMS":[
            "1", {"existing":"2", "new":"4"}, "8", {"new":"10", "unrelated":"20"}]}}}
        domain = self.load(TableValues, data)
        index = SimpleNamespace(all_bits=63, isolated={}, unknown=0, incomplete=0, reads={},
                                execution=SimpleNamespace(child_reads={}))
        addition = ast.parse(src.text.replace('}', '    "new": 2,\n}'))
        self.assertEqual(domain.select(path, src, addition, set(), {2}, index, allow_shape=True), 1 | 4 | 8 | 16)
        self.assertEqual(domain.select(path, src, ast.parse(src.text.replace(': 1', ': 2')), {2}, set(), index), 3)
        self.assertIsNone(domain.select(path, src, addition, set(), {3}, index))

    def test_table_presence_and_shape_observations_are_distinct_from_get(self):
        from tools.build_table_values import Rewrite, Trace
        trace = Trace.__new__(Trace)
        path = 'sqlglot/sample.py'
        trace.names = {'ITEMS'}
        trace.definitions = {(path, 'ITEMS')}
        trace.forward, trace.tables, trace.raw_masks = set(), {}, {}
        trace.keys, trace.shapes, trace.members = {}, {}, {}
        trace.active, trace.everyone, trace.importing, trace.collecting = 1, 15, 0, False
        code = "ITEMS = {'a': 1}\nfound = 'new' not in ITEMS\ncount = len(ITEMS)\nkeys = ITEMS.keys()\nvalue = ITEMS.get('a')\n"
        # Construction and visitation share the same AST nodes used by the parent map.
        parsed = ast.parse(code)
        tree = Rewrite(trace, path, parsed).visit(parsed)
        ns = {'_rwx_table_' + n: getattr(trace, n) for n in ('define','contains','shape','get','raw')}
        exec(compile(ast.fix_missing_locations(tree), path, 'exec'), ns)
        self.assertEqual((ns['found'], ns['count'], list(ns['keys']), ns['value']), (True, 1, ['a'], 1))
        site = path, 'ITEMS'
        self.assertEqual(trace.keys[site], {'a': 1})
        self.assertEqual(trace.members[site], {'new': 1})
        self.assertEqual(trace.shapes[site], 1)

    def test_table_values_include_every_changed_key_and_whole_table_readers(self):
        path = "sqlglot/sample.py"
        src = Source('class Config:\n    ITEMS = {"a": (0.0,), "b": (2,), "c": (3,)}\n', path)
        data = {"ids": list("abcde"), "complete": "1f", "files": {
            path: {"Config.ITEMS": ["8", {"a": "1", "b": "2", "c": "4"}]}}}
        domain = self.load(TableValues, data)
        index = SimpleNamespace(all_bits=31, isolated={}, unknown=0, incomplete=0,
                                reads={}, execution=SimpleNamespace(child_reads={}))
        changed = ast.parse(src.text.replace("0.0", "-0.0").replace("2,", "12,"))
        self.assertEqual(domain.select(path, src, changed, {2}, set(), index), 1 | 2 | 8)
        changed = ast.parse(src.text.replace("0.0", "False").replace("2,", "12,"))
        self.assertIsNone(domain.select(path, src, changed, {2}, set(), index))

    def test_child_calls_use_original_definition_location(self):
        path = "sqlglot/sample.py"
        src = Source("# comment\ndef f(x=False):\n    return x\n", path)
        data = {"ids": ["child"], "complete": "1", "files": {path: {"f|x": [2, "0"]}}}
        domain = self.load(DefaultIndex, data)
        index = SimpleNamespace(
            all_bits=1,
            isolated={},
            unknown=0,
            incomplete=0,
            reads={},
            execution=SimpleNamespace(
                child_reads={}, child_calls={f"{path}|2:f": 1}, untraced=1, child_complete=1
            ),
        )
        candidate = ast.parse("def f(x=True):\n    return x\n")
        self.assertEqual(domain.select(path, src, candidate, {1, 2}, set(), index), 1)


if __name__ == "__main__":
    unittest.main()

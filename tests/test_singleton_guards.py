import ast
import gzip
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from impact import Source
from none_comparisons import NoneComparisons
from tools.build_none_comparisons import safe_types
from tools.build_truth_guards import Rewriter, state
from truth_guards import TruthGuards


class GuardTests(unittest.TestCase):
    def observations(self, cls, path, record):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "profile.gz"
            data = {"ids": list("abcdef"), "complete": "1f", "files": {path: {"2:7": record}}}
            target.write_bytes(gzip.compress(json.dumps(data).encode()))
            return cls(target, data["ids"])

    def test_mixed_component_requires_passive_equality_for_every_observed_type(self):
        path = "sqlglot/example.py"
        code = "def f(value):\n    if value is None:\n        return 1\n"
        source = Source(code, path)
        candidate = ast.parse(code.replace("is None", "== None"))
        some_custom = self.observations(NoneComparisons, path, [2, "2f", "04", "00"])
        self.assertEqual(some_custom.exemptions(path, source, candidate, {2}, set(), fully_passive=True), 0)
        all_passive = self.observations(NoneComparisons, path, [2, "2f", "00", "04"])
        self.assertEqual(all_passive.exemptions(path, source, candidate, {2}, set(), fully_passive=True),
                         1 | 2 | 4 | 8)
        with_second_edit = ast.parse(code.replace("is None", "== None").replace("return 1", "return 2"))
        self.assertEqual(all_passive.exemptions(path, source, with_second_edit, {2}, set(), fully_passive=True), 0)

    def test_none_comparison_keeps_unknown_unseen_and_incomplete_cases(self):
        path = "sqlglot/example.py"
        code = "def f(value):\n    if value is None:\n        return 1\n"
        src = Source(code, path)
        index = self.observations(NoneComparisons, path, [2, "2f", "04", "08"])
        candidate = ast.parse(code.replace("is None", "== None"))
        self.assertEqual(index.exemptions(path, src, candidate, {2}, set()), 1 | 2 | 8)
        candidate.body[0].body[0].body[0].value.value = 5
        self.assertEqual(index.exemptions(path, src, candidate, {2, 3}, set()), 0)
        self.assertEqual(index.exemptions(path, src, ast.parse(code.replace("is None", "!= None")), {2}, set()), 0)

    def test_comparison_slots_are_compatible_current_and_side_effect_free(self):
        class Guarded:
            def __eq__(self, other):
                return self is other or (type(self) is type(other) and self.expensive(other))

        class Custom:
            def __eq__(self, other):
                raise AssertionError("classifier invoked an application comparison")

        class Borrowed:
            __eq__ = list.__eq__

        class Changing:
            pass

        self.assertEqual(safe_types(Guarded), (True, True))
        self.assertEqual(safe_types(Custom), (False, False))
        self.assertEqual(safe_types(Borrowed), (False, False))
        self.assertEqual(safe_types(Changing), (True, True))
        Changing.__eq__ = Custom.__eq__
        self.assertEqual(safe_types(Changing), (False, False))
        self.assertEqual(safe_types(list), (True, True))

    def test_truth_switch_keeps_changed_falsy_values_and_custom_hooks(self):
        path = "sqlglot/example.py"
        code = "def f(value):\n    if value:\n        return 1\n"
        src = Source(code, path)
        # Inputs by test are empty text, nonempty text, None, False, and an
        # unknown custom truth implementation. The sixth trace is incomplete.
        index = self.observations(TruthGuards, path, [2, "3f", "10", "01", "02", "04", "08", "20"])
        candidate = ast.parse(code.replace("if value:", "if value is not None:"))
        self.assertEqual(index.exemptions(path, src, candidate, {2}, set()), 2 | 4)
        other = ast.parse(code.replace("if value:", "if value.next is not None:"))
        self.assertEqual(index.exemptions(path, src, other, {2}, set()), 0)

    def test_instrumentation_evaluates_the_original_operand_once(self):
        class Owner:
            counter = 0

            @property
            def value(self):
                self.counter += 1
                return self

            def __bool__(self):
                self.counter += 1
                return True

        owner = Owner()
        self.assertIsNone(state(owner))
        trace = SimpleNamespace(registry={}, records=[])
        code = "def f(owner):\n    if owner.value:\n        return 3\n"
        tree = Rewriter(trace, "sample.py").visit(ast.parse(code))
        observed = []

        def capture(site, result):
            observed.append(state(result))
            return result

        namespace = {"_rwx_guard_operand": capture}
        exec(compile(ast.fix_missing_locations(tree), "sample.py", "exec"), namespace)
        self.assertEqual(namespace["f"](owner), 3)
        self.assertEqual(observed, [None])
        self.assertEqual(owner.counter, 2)


if __name__ == "__main__":
    unittest.main()

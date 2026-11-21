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
            namespace = {"_rwx_observe_defaults": trace.at_entry}
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
            self.assertEqual(index.files[path]["keep|value"], 1 | 4 | 8)

    def load(self, cls, records):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "observations.gz"
            path.write_bytes(gzip.compress(json.dumps(records).encode()))
            return cls(path, records["ids"])

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

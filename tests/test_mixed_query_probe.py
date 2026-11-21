import difflib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from impact import Source
from probe import QueryProbe


class MixedQueryProbeTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.original = "def convert(x):\n    return x + 1\n"
        self.paths = ["sqlglot/expression.py", "sqlglot/generator.py"]
        self.fixture = "tests/fixtures/example.sql"
        (self.root / self.fixture).parent.mkdir(parents=True)
        (self.root / self.fixture).write_text("SELECT 1\n")
        self.index = SimpleNamespace(
            root=self.root, reads={self.fixture: 0}, isolated={}, unknown=0, incomplete=0,
            execution=SimpleNamespace(child_reads={}), import_lines={},
            source=lambda path: Source(self.original, path),
            tests_from_bits=lambda bits: {i for i in range(3) if bits & (1 << i)},
        )
        self.probe = QueryProbe.__new__(QueryProbe)
        self.probe.numbers = {"test_execute_tpch": 0, "test_execute_tpcds": 1}
        self.probe.cases = [("test_execute_tpch", 0, {"parse": ["fingerprint"]}, {"parse": 0.01})]
        self.probe.setup_bit = 1 << 4
        self.probe.lines = {}
        self.probe.budget = 10
        self.probe.deadline = time.monotonic() + 60

    def changes(self):
        def diff(path, old, new):
            return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                fromfile=f"a/{path}", tofile=f"b/{path}"))
        combined = [diff(path, self.original, self.original.replace("1", "2")) for path in self.paths]
        combined.append(diff(self.fixture, "SELECT 1\n", "SELECT 2\n"))
        return "".join(combined)

    def test_fixture_reader_stays_even_when_its_production_queries_are_untouched(self):
        self.index.reads[self.fixture] = 1 << 1
        selected = self.probe.select(self.changes(), self.index, {0, 1, 2}, {}, lambda *args: [])
        self.assertEqual(selected, {1, 2})

    def test_replay_receives_all_candidate_sources_and_fixture_content(self):
        self.probe.lines = {self.paths[0]: {2: 2}}
        with patch("probe.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="test_execute_tpch\n")) as run:
            selected = self.probe.select(self.changes(), self.index, {0, 1}, {0: 15000, 1: 500},
                                         lambda numbers, _costs: [list(numbers)] if numbers else [])
        self.assertEqual(selected, set())
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(set(request["files"]), set(self.paths))
        self.assertEqual(request["fixtures"], {self.fixture: "SELECT 2\n"})
        self.assertEqual(request["checks"], {"test_execute_tpch": {"0": ["parse"]}})

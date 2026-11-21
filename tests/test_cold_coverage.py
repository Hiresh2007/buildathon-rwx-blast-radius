import difflib
import gzip
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cold_coverage import changed_offsets, digest, unaffected
from execution import ExecutionIndex, bytecode_key, scope_name
from impact import Source
from tools.pack_cold_coverage import pack
from tools.profile_cold_test import source_manifest


class ColdCoverageTests(unittest.TestCase):
    path = 'sqlglot/example.py'
    old = 'def update(value):\n    while value and value.key is not None:\n        value = value.parent\n    return value\n'

    def delta(self, updated):
        return ''.join(difflib.unified_diff(self.old.splitlines(True), updated.splitlines(True),
                                            'a/' + self.path, 'b/' + self.path))

    def fixture(self):
        ids = ['test_a', 'test_b', 'test_c']
        data = dict(ids=ids, groups=['1', '2', '4'], hashes={self.path: digest(self.old)}, reads={}, files={})
        execution = SimpleNamespace(untraced=0, child_calls={}, child_reads={}, files={}, masks=[0, 1, 2, 4],
                                    baseline=ExecutionIndex.baseline)
        index = SimpleNamespace(execution_ids=ids, all_bits=7, unknown=0, reads={}, execution=execution,
                                unmeasured={99}, source=lambda path: Source(self.old, path),
                                cache_consumers=SimpleNamespace(floor=lambda *args: 0),
                                tests_from_bits=lambda bits: {i for i in range(3) if bits & (1 << i)} | {99})
        return index, data

    def test_changed_entry_block_matches_instructions_after_resume(self):
        updated = self.old.replace('and value.key is not None', 'or value.key is not None')
        old = compile(self.old, self.path, 'exec').co_consts[0]
        new = compile(updated, self.path, 'exec').co_consts[0]
        leaders, offsets = changed_offsets(old, new)
        self.assertIn(0, leaders)
        self.assertTrue(offsets & 1)
        self.assertGreater(offsets.bit_count(), len(leaders))

        index, data = self.fixture()
        # The monitor can report the body of a block without reporting its header.
        visited = offsets & ~1
        data['files'][self.path] = {scope_name(old): [bytecode_key(old), '1', '0', [['1', format(visited, 'x')]]]}
        index.execution.files[self.path] = {scope_name(old): [0, 2, bytecode_key(old), [[next(iter(leaders)), 2]]]}
        with patch('cold_coverage.profile', return_value=data):
            self.assertEqual(unaffected(self.delta(updated), index), {2})

    def test_raw_reads_caches_children_and_unmeasured_tests_are_retained(self):
        index, data = self.fixture()
        updated = self.old.replace('is not None', 'is None')
        data['reads'][self.path] = '1'
        index.cache_consumers.floor = lambda *args: 2
        with patch('cold_coverage.profile', return_value=data):
            self.assertEqual(unaffected(self.delta(updated), index), {2})
            index.execution.child_reads[self.path] = 4
            self.assertEqual(unaffected(self.delta(updated), index), set())

    def test_mixed_sql_fixture_retains_both_cold_and_warm_fixture_readers(self):
        index, data = self.fixture()
        filename = 'tests/fixtures/optimizer/example.sql'
        before = 'SELECT 1;\n'
        after = 'SELECT 2;\n'
        fixture_patch = ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                     'a/' + filename, 'b/' + filename))
        text = self.delta(self.old.replace('is not None', 'is None')) + fixture_patch
        data['reads'][filename] = '1'
        index.reads[filename] = 2
        with TemporaryDirectory() as folder:
            index.root = Path(folder)
            fixture = index.root / filename
            fixture.parent.mkdir(parents=True)
            fixture.write_text(before)
            hashes = {filename: hashlib.sha256(fixture.read_bytes()).hexdigest()}
            with patch('cold_coverage.profile', return_value=data), patch('cold_coverage.fixture_sources', return_value=hashes):
                self.assertEqual(unaffected(text, index), {2})
                fixture.write_text('SELECT 3;\n')
                self.assertEqual(unaffected(text, index), set())

    def test_eager_changes_and_unmatched_sources_keep_the_entire_selection(self):
        index, data = self.fixture()
        with patch('cold_coverage.profile', return_value=data):
            self.assertEqual(unaffected(self.delta('enabled = True\n' + self.old), index), set())
            data['hashes'][self.path] = 'unrelated'
            self.assertEqual(unaffected(self.delta(self.old.replace('is not None', 'is None')), index), set())


class ColdMapBuildTests(unittest.TestCase):
    def test_packing_requires_the_complete_baseline_and_matching_source(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'tests').mkdir()
            (root / 'sqlglot').mkdir()
            (root / 'sqlglot/example.py').write_text('answer = 1\n')
            test = root / 'tests/test_example.py'
            test.write_text('def test_one(): pass\ndef test_two(): pass\n')
            inventory = root / 'inventory.gz'
            ids = ['tests/test_example.py::test_one', 'tests/test_example.py::test_two']
            inventory.write_bytes(gzip.compress(json.dumps({'ids': ids}).encode()))
            sample = root / 'sample.gz'
            info = dict(python='cpython-313', version=[3, 13, 18], platform='linux', machine='x86_64',
                        tree=source_manifest(root)[1], test_hash=hashlib.sha256(test.read_bytes()).hexdigest(),
                        target='tests/test_example.py', ids=ids, unknown=[], reads=['sqlglot/example.py'],
                        files={'sqlglot/example.py': {'1:example': ['key', [2, 8]]}})
            sample.write_bytes(gzip.compress(json.dumps(info).encode()))
            result = pack(root, inventory, [sample])
            self.assertEqual(result['groups'], ['3'])
            self.assertEqual(result['reads'], {'sqlglot/example.py': '1'})
            self.assertEqual(result['files']['sqlglot/example.py']['1:example'], ['key', '1', '0', [['1', '12']]])
            info['ids'] = ids[:1]
            sample.write_bytes(gzip.compress(json.dumps(info).encode()))
            with self.assertRaisesRegex(ValueError, 'incomplete baseline'):
                pack(root, inventory, [sample])
            test.write_text('def test_changed(): pass\n')
            with self.assertRaisesRegex(ValueError, 'source does not match'):
                pack(root, inventory, [sample])

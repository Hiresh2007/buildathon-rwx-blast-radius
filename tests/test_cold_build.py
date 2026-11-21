import gzip
import json
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.build_cold_profiles import build, combine
from tools.profile_cold_test import source_manifest


class ColdBuildTests(unittest.TestCase):
    def observation(self, label, fingerprint):
        return dict(python='cpython-313', version=[3, 13, 18], platform='linux', machine='x86_64',
                    ids=['one', 'two'], hashes={'f.py': 'a'}, groups=[label], reads={'f.py': '1'},
                    files={'f.py': {'4:work': [fingerprint, '1', '0', [['1', '40']]]}})

    def test_same_scope_offsets_keep_their_independent_group_bits(self):
        result = combine(self.observation('1', 'key'), self.observation('2', 'key'))
        self.assertEqual(result['groups'], ['1', '2'])
        self.assertEqual(result['reads'], {'f.py': '3'})
        self.assertEqual(result['files']['f.py']['4:work'], ['key', '3', '0', [['1', '40'], ['2', '40']]])

    def test_unmatched_bytecode_keeps_all_new_callers(self):
        result = combine(self.observation('1', 'first'), self.observation('2', 'second'))
        self.assertEqual(result['files']['f.py']['4:work'], ['first', '3', '2', [['1', '40']]])

    def test_existing_complete_group_is_not_collected_twice(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'sqlglot').mkdir()
            inventory = root / 'inventory.gz'
            ids = ['tests/test_example.py::test_one']
            inventory.write_bytes(gzip.compress(json.dumps({'ids': ids}).encode()))
            destination = root / 'cold.gz'
            current = self.observation('1', 'key')
            current.update(ids=ids, hashes=source_manifest(root)[0], python=sys.implementation.cache_tag,
                           version=list(sys.version_info[:3]), platform=sys.platform, machine=platform.machine())
            archive = gzip.compress(json.dumps(current).encode())
            destination.write_bytes(archive)
            with patch('tools.build_cold_profiles.resource.setrlimit'), patch('tools.build_cold_profiles.collect') as collect:
                build(root, inventory, destination, ['tests/test_example.py'], 10)
            collect.assert_not_called()
            self.assertEqual(destination.read_bytes(), archive)

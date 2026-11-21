from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from cache_impact import CacheConsumers
from tools.build_cache_consumers import build


class CacheProducerTests(unittest.TestCase):
    def test_cross_module_producer_and_inherited_table(self):
        builder = ('from functools import cache as memo\n'
                   'def factor(x):\n    return x * 2\n'
                   'def materialize(x):\n    return factor(x)\n'
                   '@memo\ndef classify(x):\n    return x % 3\n')
        consumer = ('from .builder import materialize\n'
                    'class Base:\n    entries = {}\n'
                    'class Child(Base):\n'
                    '    @classmethod\n    def select(cls, key):\n'
                    '        if key not in cls.entries:\n'
                    '            item = materialize(key)\n'
                    '            cls.entries[key] = item\n'
                    '        return cls.entries[key]\n'
                    'def reset(key):\n    Base.entries[key] = 0\n'
                    'def unrelated(x):\n    return x - 1\n')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / 'sqlglot').mkdir()
            (source / 'sqlglot/builder.py').write_text(builder)
            (source / 'sqlglot/consumer.py').write_text(consumer)
            ranges = build(source)

        def fields(path, line):
            return {field for first, last, field, _ in ranges[path] if first <= line <= last}

        field = {'sqlglot.consumer:Base.entries'}
        self.assertEqual(fields('sqlglot/builder.py', 3), field)
        self.assertEqual(fields('sqlglot/builder.py', 5), field)
        self.assertEqual(fields('sqlglot/builder.py', 8), {''})
        for line in (7, 8, 9, 12):
            self.assertEqual(fields('sqlglot/consumer.py', line), field)
        self.assertFalse(fields('sqlglot/consumer.py', 14))

    def test_unmeasured_wrapper_retains_warm_and_isolated_consumers(self):
        symbols = SimpleNamespace(raw={}, copies={}, keys={}, initialized={},
                                  initialized_keys={}, derived={}, incomplete=0)
        index = SimpleNamespace(symbols=symbols, isolated={'sqlglot/use.py': 2},
                                unknown=4, incomplete=0, all_bits=31, reads={},
                                files={'sqlglot/use.py': {10: 8}})
        cache = CacheConsumers({'sqlglot/data.py': [(4, 8, '', 'sqlglot/use.py')]}, index)
        self.assertEqual(cache.floor('sqlglot/data.py', {5}, set()), 14)
        self.assertEqual(cache.floor('sqlglot/data.py', set(), {4}), 14)
        self.assertEqual(cache.floor('sqlglot/data.py', {9}, set()), 0)

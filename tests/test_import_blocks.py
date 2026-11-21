import ast
import bisect
import platform
import sys
import unittest

from execution import ExecutionIndex, block_starts, bytecode_key, instructions, scope_name


class ImportBlocksTests(unittest.TestCase):
    def test_only_the_original_changed_block_controls_the_import_fallback(self):
        path = "sqlglot/example.py"
        source = "def pick(flag):\n    return 1 if flag else 2\n"
        candidate = ast.parse(source.replace("else 2", "else 3"))
        code = compile(source, path, "exec", dont_inherit=True).co_consts[0]
        starts = block_starts(code)
        leaders = {
            op.argval: starts[bisect.bisect_right(starts, op.offset) - 1]
            for op in instructions(code)[0]
            if op.opname == "LOAD_CONST"
        }
        scope = scope_name(code)
        fingerprint = bytecode_key(code)
        ids = ["true", "false", "untraced"]
        records = {
            "ids": ids, "python": sys.implementation.cache_tag, "complete": True,
            "sets": ["0", "1", "2", "3", "4"], "untraced": 4,
            "files": {path: {scope: [True, 3, fingerprint, [[leaders[1], 1], [leaders[2], 2]]]}}
        }
        children = {"ids": ids, "python": sys.implementation.cache_tag, "platform": sys.platform,
                    "complete": "0", "scopes": {}, "reads": {}}
        early = {"python": sys.implementation.cache_tag, "platform": sys.platform, "machine": platform.machine(),
                 "files": {path: {scope: [fingerprint, [leaders[1]]]}}}
        chosen = ExecutionIndex(records, ids, children, early)
        self.assertEqual(chosen.select(path, source, candidate), 2 | 4)
        self.assertIsNone(ExecutionIndex(records, ids, children).select(path, source, candidate))
        early["files"][path][scope][1].append(leaders[2])
        self.assertIsNone(chosen.select(path, source, candidate))
        early["files"][path][scope] = ["different bytecode", []]
        self.assertIsNone(chosen.select(path, source, candidate))


if __name__ == "__main__":
    unittest.main()

"""Measure basic-block coverage under each official test on the pinned checkout."""

import argparse
import bisect
import gzip
import importlib.machinery
import json
import os
from collections import defaultdict
from concurrent import futures
from functools import lru_cache
from pathlib import Path
import sys
from unittest.mock import patch

import pytest
from build_map import TestTrace, TracedPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from execution import block_starts, bytecode_key, scope_name


class BlockTrace(TestTrace):
    def __init__(self, root, tests):
        super().__init__(root, tests)
        self.scopes_hit = {}
        self.loading = 0

    @lru_cache(maxsize=None)
    def record(self, code):
        path = self.source_path(code.co_filename)
        if not path or not path.startswith("sqlglot/"):
            return None
        key = (path, scope_name(code))
        if key not in self.scopes_hit:
            self.scopes_hit[key] = [False, 0, bytecode_key(code), defaultdict(int)]
        record = self.scopes_hit[key]
        if record[2] != bytecode_key(code):
            # Multiple generated functions at the same source location.
            record[0] = True
        return record

    def enter(self, code, _offset):
        record = self.record(code)
        if record is not None:
            if self.loading or self.collecting:
                record[0] = True
            if self.active and self.trace_lines:
                record[1] |= self.active
                self.monitor.set_local_events(self.tool, code, self.monitor.events.INSTRUCTION)
        return self.monitor.DISABLE

    @lru_cache(maxsize=None)
    def leaders(self, code):
        starts = block_starts(code)
        # Mark every instruction so first entry via an exception, resume or generator
        # yield still counts the containing block.
        return {i: starts[bisect.bisect_right(starts, i) - 1] for i in range(0, len(code.co_code), 2)}

    def instruction(self, code, offset):
        record = self.record(code)
        if self.loading or self.collecting:
            record[0] = True
        if self.active and self.trace_lines:
            leader = self.leaders(code)[offset]
            record[3][leader] |= self.active
        return self.monitor.DISABLE

    def collect(self):
        m, tool = self.monitor, self.tool
        m.use_tool_id(tool, "SQLGlot block trace")
        m.register_callback(tool, m.events.PY_START, self.enter)
        m.register_callback(tool, m.events.INSTRUCTION, self.instruction)
        m.set_events(tool, m.events.PY_START)
        sys.addaudithook(self.audit)
        loader = importlib.machinery.SourceFileLoader
        original = loader.exec_module

        def execute(source_loader, module):
            # A lazy import can initialize shared state for other tests too.
            if not self.source_path(source_loader.path):
                return original(source_loader, module)
            self.loading += 1
            if self.loading == 1:
                m.restart_events()
            try:
                return original(source_loader, module)
            finally:
                self.loading -= 1
                if not self.loading:
                    m.restart_events()

        try:
            with patch.object(futures, "ProcessPoolExecutor", TracedPool), patch.object(loader, "exec_module", execute):
                return pytest.main(["tests", "-q", "--assert=plain", "-p", "no:cacheprovider"], plugins=[self])
        finally:
            self.active = 0
            m.set_events(tool, 0)
            m.free_tool_id(tool)

    def write_blocks(self, target):
        masks = {}
        def intern(mask):
            if mask not in masks:
                masks[mask] = len(masks)
            return masks[mask]

        files = defaultdict(dict)
        for (path, name), (early, calls, fingerprint, blocks) in sorted(self.scopes_hit.items()):
            files[path][name] = [early, intern(calls), fingerprint,
                                 [[offset, intern(mask)] for offset, mask in sorted(blocks.items())]]
        all_tests = (1 << len(self.ids)) - 1
        data = {"python": sys.implementation.cache_tag, "complete": True, "ids": self.ids, "files": files,
                "untraced": intern((all_tests & ~self.passed) | self.failed | self.outside_process),
                "sets": [format(mask, "x") for mask in masks]}
        target.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
        print(f"Measured {len(self.scopes_hit)} code objects in {len(files)} files", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    trace = BlockTrace(root, json.loads(args.manifest.read_bytes())["tests"])
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.update(SKIP_INTEGRATION="0", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    if trace.collect() == pytest.ExitCode.OK:
        trace.write_blocks(output)
    else:
        raise SystemExit("baseline tests must pass for an execution map")


if __name__ == "__main__":
    main()

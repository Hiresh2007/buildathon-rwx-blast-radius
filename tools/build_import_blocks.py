"""Measure the basic blocks actually reached by test collection and lazy module imports."""
import argparse
import concurrent.futures
import gzip
import importlib.machinery
import json
import os
import platform
import shutil
import signal
import subprocess
import tempfile
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_execution import BlockTrace
from build_map import TracedPool
from execution import bytecode_key, scope_name


class EarlyBlocks(BlockTrace):
    def __init__(self, root, tests):
        super().__init__(root, tests)
        self.observed = {}
        self.variants = {}
        self.armed = set()

    def set_context(self, mask, trace_lines):
        self.active, self.trace_lines = mask, trace_lines

    def enter(self, code, offset):
        path = self.source_path(code.co_filename)
        if path and path.startswith("sqlglot/"):
            tag = (path, scope_name(code))
            fingerprint = bytecode_key(code)
            if self.variants.setdefault(tag, fingerprint) != fingerprint:
                self.variants[tag] = None
            if self.collecting or self.loading:
                record = self.observed.setdefault(tag, [fingerprint, set()])
                self.armed.add(code)
                self.monitor.set_local_events(self.tool, code, self.monitor.events.INSTRUCTION)
        return self.monitor.DISABLE

    def instruction(self, code, offset):
        path = self.source_path(code.co_filename)
        self.observed[path, scope_name(code)][1].add(self.leaders(code)[offset])
        return self.monitor.DISABLE

    def enable(self):
        self.monitor.set_events(self.tool, self.monitor.events.PY_START)
        self.monitor.restart_events()

    def disable(self):
        for code in self.armed:
            self.monitor.set_local_events(self.tool, code, 0)
        self.armed.clear()

    def pytest_collection_finish(self):
        super().pytest_collection_finish()
        self.disable()

    def collect(self):
        monitor = self.monitor
        monitor.use_tool_id(self.tool, "SQLGlot import basic blocks")
        monitor.register_callback(self.tool, monitor.events.PY_START, self.enter)
        monitor.register_callback(self.tool, monitor.events.INSTRUCTION, self.instruction)
        loader = importlib.machinery.SourceFileLoader
        original = loader.exec_module

        def execute(loader, module):
            if self.collecting or not self.source_path(loader.path):
                return original(loader, module)
            self.loading += 1
            if self.loading == 1:
                self.enable()
            try:
                return original(loader, module)
            finally:
                self.loading -= 1
                if not self.loading:
                    self.disable()
        self.enable()
        try:
            with patch.object(concurrent.futures, "ProcessPoolExecutor", TracedPool), patch.object(loader, "exec_module", execute):
                return pytest.main(["tests", "-q", "--assert=plain", "-p", "no:cacheprovider"], plugins=[self])
        finally:
            self.disable()
            monitor.set_events(self.tool, 0)
            monitor.free_tool_id(self.tool)

    def save(self, output):
        files = {}
        for (path, name), (fingerprint, blocks) in self.observed.items():
            if self.variants.get((path, name)) == fingerprint and blocks:
                files.setdefault(path, {})[name] = [fingerprint, sorted(blocks)]
        data = {"python": sys.implementation.cache_tag, "platform": sys.platform, "machine": platform.machine(), "files": files}
        output.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
        print(sum(map(len, files.values())), "early scopes", sum(len(rec[1]) for group in files.values() for rec in group.values()), "early blocks")


def prepare(source, tests, output, budget):
    # The image keeps a pristine checkout. Profiling runs in a disposable copy.
    with tempfile.TemporaryDirectory(prefix="import-coverage-") as temporary:
        directory = Path(temporary)
        checkout = directory / "checkout"
        shutil.copytree(source, checkout, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
        result = directory / "observed.json.gz"
        command = [sys.executable, str(Path(__file__).resolve()), str(checkout), str(tests), str(result)]
        proc = subprocess.Popen(command, start_new_session=True)
        try:
            status = proc.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            status = 1
        if status == 0 and result.is_file():
            shutil.copyfile(result, output)
            return
    fallback = {"python": sys.implementation.cache_tag, "platform": sys.platform, "machine": platform.machine(), "files": {}}
    output.write_bytes(gzip.compress(json.dumps(fallback).encode(), mtime=0))
    print("Incomplete import profile. Using unrefined import-time scope coverage.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("source", "tests", "output"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--build-budget", type=int)
    args = parser.parse_args()
    root, tests, output = args.source.resolve(), args.tests.resolve(), args.output.resolve()
    if args.build_budget is not None:
        prepare(root, tests, output, args.build_budget)
        raise SystemExit(0)
    if tests.suffix == ".gz":
        with gzip.open(tests, "rt") as reader:
            ids = json.load(reader)["ids"]
        items = [{"id": name, "number": number} for number, name in enumerate(ids)]
    else:
        items = json.loads(tests.read_bytes())["tests"]
    trace = EarlyBlocks(root, items)
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.update(SKIP_INTEGRATION="0", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                      PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    status = trace.collect()
    if status != pytest.ExitCode.OK or trace.passed.bit_count() != len(trace.ids):
        raise ValueError("import coverage requires a passing complete baseline")
    trace.save(output)

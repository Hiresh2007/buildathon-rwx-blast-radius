"""Measure synchronous generator execution and cold import blocks in a passing test suite."""
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
import sys
import tempfile
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_execution import BlockTrace
from build_map import TracedPool
from execution import bytecode_key, scope_name


class LifecycleTrace(BlockTrace):
    def __init__(self, root, tests):
        super().__init__(root, tests)
        self.early = defaultdict(set)
        self.variants = {}
        self.armed_non_generators = set()

    @lru_cache(maxsize=None)
    def identity(self, code):
        path = self.source_path(code.co_filename)
        if not path or not path.startswith('sqlglot/'):
            return None
        key = path, scope_name(code)
        fingerprint = bytecode_key(code)
        if self.variants.setdefault(key, fingerprint) != fingerprint:
            self.variants[key] = None
        return key

    def enter(self, code, offset):
        key = self.identity(code)
        if key is not None:
            early = self.loading or self.collecting
            generator = bool(code.co_flags & 0x20)
            if early or generator and self.active and self.trace_lines:
                self.monitor.set_local_events(self.tool, code, self.monitor.events.INSTRUCTION)
                if not generator:
                    self.armed_non_generators.add(code)
            if generator:
                record = self.record(code)
                if early:
                    record[0] = True
                if self.active and self.trace_lines:
                    record[1] |= self.active
        return self.monitor.DISABLE

    def thrown(self, code, offset, exc):
        self.enter(code, offset)

    def instruction(self, code, offset):
        key = self.identity(code)
        if key is not None:
            leader = self.leaders(code)[offset]
            if self.collecting or self.loading:
                self.early[key].add(leader)
            if code.co_flags & 0x20 and self.active and self.trace_lines:
                record = self.record(code)
                record[1] |= self.active
                record[3][leader] |= self.active
        return self.monitor.DISABLE

    def finish_imports(self):
        for code in self.armed_non_generators:
            self.monitor.set_local_events(self.tool, code, 0)
        self.armed_non_generators.clear()

    def pytest_collection_finish(self):
        super().pytest_collection_finish()
        self.finish_imports()

    def collect(self):
        mon, tool = self.monitor, self.tool
        mon.use_tool_id(tool, 'SQLGlot import and generator coverage')
        mon.register_callback(tool, mon.events.PY_START, self.enter)
        mon.register_callback(tool, mon.events.PY_RESUME, self.enter)
        mon.register_callback(tool, mon.events.PY_THROW, self.thrown)
        mon.register_callback(tool, mon.events.INSTRUCTION, self.instruction)
        mon.set_events(tool, mon.events.PY_START | mon.events.PY_RESUME | mon.events.PY_THROW)
        sys.addaudithook(self.audit)
        loader = importlib.machinery.SourceFileLoader
        original = loader.exec_module

        def execute(loader, module):
            if self.collecting or not self.source_path(loader.path):
                return original(loader, module)
            self.loading += 1
            if self.loading == 1:
                mon.restart_events()
            try:
                return original(loader, module)
            finally:
                self.loading -= 1
                if not self.loading:
                    self.finish_imports()
                    mon.restart_events()

        try:
            with (patch.object(concurrent.futures, 'ProcessPoolExecutor', TracedPool),
                  patch.object(loader, 'exec_module', execute)):
                return pytest.main(['tests', '-q', '--assert=plain', '-p', 'no:cacheprovider'], plugins=[self])
        finally:
            self.finish_imports()
            mon.set_events(tool, 0)
            mon.free_tool_id(tool)

    def write_profiles(self, imports, generators):
        files = {}
        for (path, name), blocks in self.early.items():
            fp = self.variants.get((path, name))
            if fp and blocks:
                files.setdefault(path, {})[name] = [fp, sorted(blocks)]
        meta = dict(python=sys.implementation.cache_tag, platform=sys.platform, machine=platform.machine())

        def save(path, data):
            path.write_bytes(gzip.compress(json.dumps(data, separators=(',', ':')).encode(), mtime=0))

        save(imports, {**meta, 'files': files})
        self.write_blocks(generators)
        with gzip.open(generators, 'rt') as stream:
            data = json.load(stream)
        for (path, name), fp in self.variants.items():
            found = data['files'].get(path, {}).get(name)
            if found is None:
                continue
            if fp is None:
                del data['files'][path][name]
            else:
                found.append(sorted(self.early.get((path, name), ())))
        save(generators, {**data, **meta})
        import_count = sum(map(len, files.values()))
        generator_count = sum(map(len, data["files"].values()))
        print(f"{import_count} import scopes and {generator_count} generator scopes", flush=True)


def prepare(source, tests, output, generators, budget):
    # The image keeps a pristine checkout. Profiling runs in a disposable copy.
    with tempfile.TemporaryDirectory(prefix="import-coverage-") as temporary:
        directory = Path(temporary)
        checkout = directory / "checkout"
        shutil.copytree(source, checkout, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
        result = directory / "observed.json.gz"
        resumed = directory / "generators.json.gz"
        command = [sys.executable, str(Path(__file__).resolve()), str(checkout), str(tests), str(result), str(resumed)]
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
        if status == 0 and result.is_file() and resumed.is_file():
            shutil.copyfile(result, output)
            shutil.copyfile(resumed, generators)
            return
    fallback = {"python": sys.implementation.cache_tag, "platform": sys.platform, "machine": platform.machine(), "files": {}}
    output.write_bytes(gzip.compress(json.dumps(fallback).encode(), mtime=0))
    generators.write_bytes(gzip.compress(json.dumps({**fallback, "complete": False}).encode(), mtime=0))
    print("Incomplete coverage profile. Using broader import and generator coverage.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("source", "tests", "output", "generators"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--build-budget", type=int)
    args = parser.parse_args()
    root, tests, output = args.source.resolve(), args.tests.resolve(), args.output.resolve()
    generators = args.generators.resolve()
    if args.build_budget is not None:
        prepare(root, tests, output, generators, args.build_budget)
        raise SystemExit(0)
    if tests.suffix == ".gz":
        with gzip.open(tests, "rt") as reader:
            ids = json.load(reader)["ids"]
        items = [{"id": name, "number": number} for number, name in enumerate(ids)]
    else:
        items = json.loads(tests.read_bytes())["tests"]
    trace = LifecycleTrace(root, items)
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.update(SKIP_INTEGRATION="0", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                      PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    status = trace.collect()
    if status != pytest.ExitCode.OK or trace.passed.bit_count() != len(trace.ids) or trace.failed:
        raise ValueError("import coverage requires a passing complete baseline")
    trace.write_profiles(output, generators)

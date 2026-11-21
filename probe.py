"""Bounded replay of affected independent queries in expensive SQLGlot executor tests."""

import ast
from ast_syntax import same_syntax
import gzip
import importlib.abc
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from impact import _apply, _parse_diff
from replay_phases import verify


TEST_FILE = "tests/test_executor.py::TestExecutor::"
GROUPS = ("test_execute_tpcds", "test_execute_tpch", "test_optimized_tpch")


class PatchedImports(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Load only the supplied candidate modules over a read-only pinned checkout."""

    def __init__(self, root, files):
        self.files = {}
        for path, contents in files.items():
            name = path[:-3].replace("/", ".").removesuffix(".__init__")
            self.files[name] = (str(root / path), contents)

    def find_spec(self, fullname, path, target=None):
        if fullname not in self.files:
            return None
        filename, _ = self.files[fullname]
        return importlib.util.spec_from_file_location(
            fullname, filename, loader=self,
            submodule_search_locations=[str(Path(filename).parent)] if filename.endswith("/__init__.py") else None,
        )

    def exec_module(self, module):
        filename, text = self.files[module.__name__]
        exec(compile(text, filename, "exec"), module.__dict__)


def load_checkout(root, files):
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    sys.meta_path.insert(0, PatchedImports(root, files))


def worker():
    import resource
    request = json.loads(sys.stdin.buffer.read())
    cap = request["budget"]
    resource.setrlimit(resource.RLIMIT_CPU, (cap, cap + 1))
    root = Path(request["root"])
    fixtures = request.get("fixtures", {})
    if fixtures:
        from pytest_replay import overlay
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "src"
            resources = {**fixtures, "tests/helpers.py": (root / "tests/helpers.py").read_text()}
            overlay(root, candidate, resources)
            load_checkout(candidate, request["files"])
            verify(candidate, request["checks"], request["records"])
    else:
        load_checkout(root, request["files"])
        verify(root, request["checks"], request["records"])


class QueryProbe:
    def __init__(self, tests):
        with gzip.open(Path(__file__).with_name("query-map.json.gz"), "rt") as stream:
            data = json.load(stream)
        self.numbers = {test["id"][len(TEST_FILE):]: test["number"] for test in tests
                        if test["id"].startswith(TEST_FILE) and test["id"][len(TEST_FILE):] in GROUPS}
        self.cases = data["cases"]
        bits = [int(mask, 16) for mask in data["masks"]]
        self.lines = {path: {int(n): bits[b] for n, b in lines.items()}
                      for path, lines in data["lines"].items()}
        self.setup_bit = 1 << (4 * len(self.cases))
        self.budget = 32  # total worker CPU cap for the batch
        self.deadline = time.monotonic() + 60

    def select(self, text, index, selected, costs, schedule):
        targets = {g: n for g, n in self.numbers.items() if n in selected}
        if not targets:
            return selected
        touched = 0
        files = {}
        fixtures = {}
        try:
            changes = _parse_diff(text)
            combined = len(changes) > 1
            for change in changes:
                if combined and change.path.startswith("tests/fixtures/") and change.path in index.reads:
                    fixtures[change.path] = _apply((index.root / change.path).read_text(encoding="utf-8"), change.hunks)[0]
                    readers = index.tests_from_bits(index.reads[change.path] | index.execution.child_reads.get(change.path, 0)
                                                    | index.isolated.get(change.path, 0) | index.unknown)
                    targets = {g: n for g, n in targets.items() if n not in readers}
                    if not targets:
                        return selected
                    continue
                if not change.path.startswith("sqlglot/") or not change.path.endswith(".py"):
                    return selected
                source = index.source(change.path)
                candidate, replaced, inserted = _apply(source.text, change.hunks)
                if inserted or not replaced:
                    return selected
                new_tree = ast.parse(candidate)
                if same_syntax(source.tree, new_tree):
                    continue
                lines = self.lines.get(change.path, {})
                functions = set()
                observed_in_imports = index.import_lines.get(change.path, set())
                explicit = index.tests_from_bits(index.reads.get(change.path, 0) | index.incomplete)
                if explicit.intersection(targets.values()):
                    return selected
                for line in replaced:
                    scope = source.runtime_scope(line)
                    statement = source.statement(line)
                    if scope is None or statement is None:
                        return selected
                    functions.add((scope.lineno, scope.name))
                    for line_number in range(statement.lineno, statement.end_lineno + 1):
                        if line_number in observed_in_imports:
                            return selected
                        touched |= lines.get(line_number, 0)
                # Outside the measured functions the candidate's complete AST must match.
                def strip(tree):
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (node.lineno, node.name) in functions:
                            node.body = [ast.Pass()]
                    return tree
                if not same_syntax(strip(new_tree), strip(ast.parse(source.text))):
                    return selected
                files[change.path] = candidate
        except (SyntaxError, ValueError, OSError, RecursionError):
            return selected
        if not files or touched & self.setup_bit:
            return selected

        groups = {g: {} for g in targets}
        cpu = dict.fromkeys(targets, 0.0)
        records = {g: {} for g in targets}
        unsafe = set()
        phases = ("other", "parse", "optimize", "transpile")
        for i, (group, ordinal, results, seconds) in enumerate(self.cases):
            if group not in groups:
                continue
            active = (touched >> (4 * i)) & 15
            if not active:
                continue
            required = [p for bit, p in enumerate(phases) if active & (1 << bit)]
            if "other" in required or any(p not in results for p in required):
                unsafe.add(group)
                continue
            groups[group][ordinal] = required
            records[group][ordinal] = {p: results[p] for p in required}
            cpu[group] += sum(seconds[p] for p in required)
        selected = selected.difference(targets[g] for g in groups if not groups[g] and g not in unsafe)
        if self.budget < 2 or time.monotonic() > self.deadline:
            return selected

        def cost(numbers):
            workers = schedule(numbers, costs)
            return 2 * len(workers) + max((sum(costs[n] for n in worker) for worker in workers), default=0) / 1000

        before = cost(selected)
        pending = {g: groups[g] for g in groups if g not in unsafe and groups[g]}
        if not pending:
            return selected
        potential = before - cost(selected.difference(targets[g] for g in pending))
        cap = min(7, self.budget, int(potential * 0.80))
        if cap < 2 or 0.15 + sum(cpu[g] for g in pending) * 0.8 > cap:
            return selected
        request = dict(root=str(index.root), files=files, fixtures=fixtures, checks=pending, records=records, budget=cap)
        self.budget -= cap
        try:
            reply = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--worker"],
                input=json.dumps(request), text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=min(cap * 2 + 1, max(0.1, self.deadline - time.monotonic())),
            )
        except (OSError, subprocess.TimeoutExpired):
            return selected
        if reply.returncode != 0:
            return selected
        return selected.difference(targets[g] for g in reply.stdout.splitlines() if g in pending)


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit(2)
    worker()

"""Refine small candidate schedules by running the real pinned pytest tests on an overlay."""
import ast
import gzip
import json
import os
import signal
import sys
import subprocess
import tempfile
import time
from pathlib import Path
from functools import lru_cache

from impact import _apply, _parse_diff


def overlay(source, dest, files):
    changed = {}
    for name, value in files.items():
        parts = Path(name).parts
        node = changed
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def walk(root, directory, edits):
        directory.mkdir()
        for path in root.iterdir():
            name = path.name
            if name in {".git", "__pycache__", ".pytest_cache"}:
                continue
            out = directory / name
            item = edits.get(name)
            if isinstance(item, dict):
                walk(path, out, item)
            elif item is not None:
                out.write_text(item)
            else:
                out.symlink_to(path, target_is_directory=path.is_dir())
    walk(source, dest, changed)


class Reports:
    def __init__(self, ids):
        self.seen = {name: set() for name in ids}
        self.blocked = set()
        self.collection_failed = False

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_failed = True

    def pytest_runtest_logreport(self, report):
        name = report.nodeid
        if name not in self.seen:
            return
        if not report.passed or hasattr(report, "wasxfail"):
            self.blocked.add(name)
        elif type(report).__name__ == "TestReport":
            self.seen[name].add(report.when)

    def passed(self):
        if self.collection_failed:
            return []
        return [name for name, events in self.seen.items() if name not in self.blocked
                and events == {"setup", "call", "teardown"}]


def trial(pytest, base, ids, files, output, cap):
    import resource
    sys.dont_write_bytecode = True
    os.setsid()
    resource.setrlimit(resource.RLIMIT_CPU, (cap, cap + 1))
    os.chdir(base)
    sys.path.insert(0, str(base))
    os.environ.update(PYTHONPATH=str(base), PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                      SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="2", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    with open(os.devnull, "w") as devnull:
        os.dup2(devnull.fileno(), 1)
        os.dup2(devnull.fileno(), 2)
    report = Reports(ids)
    result = pytest.main(["-q", "--tb=no", "--rootdir", str(base), "--import-mode=importlib",
                          "-p", "no:cacheprovider", *ids], plugins=[report])
    if result not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        return
    for changed in files:
        if changed.startswith("tests/fixtures/"):
            helpers = sys.modules.get("tests.helpers")
            if helpers is None:
                return
            expected = Path(helpers.FIXTURES_DIR) / changed.removeprefix("tests/fixtures/")
            if not (base / changed).samefile(expected):
                return
        if not changed.endswith(".py"):
            continue
        name = changed[:-3].replace("/", ".").removesuffix(".__init__")
        mod = sys.modules.get(name)
        if mod is not None and not (base / changed).samefile(mod.__file__):
            return
    output.write_text(json.dumps(report.passed()))


def supervise():
    import pytest
    job = json.load(sys.stdin)
    root = Path(job["root"]).resolve()
    start = time.monotonic()
    used_cpu = 0
    for n, (key, files, ids, wall) in enumerate(job["cases"]):
        if used_cpu > 9 or time.monotonic() - start > 15:
            break
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp).resolve()
            checkout = temp / "src"
            output = temp / "result"
            materialized = dict(files)
            test_paths = {name.split("::", 1)[0] for name in ids}
            if any(name.startswith("tests/fixtures/") for name in files):
                test_paths.add("tests/helpers.py")
            for path in test_paths:
                if path not in materialized:
                    materialized[path] = (root / path).read_text()
            overlay(root, checkout, materialized)
            pid = os.fork()
            if pid == 0:
                try:
                    trial(pytest, checkout, ids, files, output, 2)
                except BaseException:
                    os._exit(1)
                os._exit(0)
            until = min(start + 16, time.monotonic() + wall)
            while True:
                completed, status, usage = os.wait4(pid, os.WNOHANG)
                if completed:
                    used_cpu += usage.ru_utime + usage.ru_stime
                    break
                if time.monotonic() > until:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    _, status, usage = os.wait4(pid, 0)
                    used_cpu += usage.ru_utime + usage.ru_stime
                    break
                time.sleep(0.012)
            # A CPU-limited group leader can exit while process-pool workers live on.
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if os.waitstatus_to_exitcode(status) == 0 and output.exists():
                print(json.dumps([key, json.loads(output.read_text())]), flush=True)


@lru_cache(maxsize=64)
def setup_classes(root, file):
    tree = ast.parse((root / file).read_text())
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)
            and any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == "setUpClass" for item in node.body)}


def _pivot(numbers, lookup, times, milliseconds, schedule):
    """Pick the largest profitable marginal reduction under the live worker cost."""
    def price(selection):
        workers = schedule(selection, milliseconds)
        loads = [sum(milliseconds[n] for n in worker) for worker in workers]
        return 2 * len(workers) + max(loads, default=0) / 1000

    # A runtime measured on another host is an estimate. Demand a margin for
    # machine speed, collection and unsuccessful confirmations.
    contenders = []
    for number in numbers:
        cpu = times.get(lookup[number]["id"])
        if cpu is not None and cpu < 1.45 and milliseconds[number] / 1000 > 2.5 * cpu + 0.30:
            contenders.append(number)
    if not contenders:
        return None
    current = price(numbers)
    best = None
    for n in contenders:
        cpu = times[lookup[n]["id"]]
        gain = current - price(numbers - {n})
        if gain < 2.5 * cpu + 0.30:
            continue
        candidate = (gain - cpu, n, cpu)
        if best is None or candidate > best:
            best = candidate
    return best


def refine(patches, selections, index, tests, schedule=None):
    lookup = {t["number"]: t for t in tests}
    ids = {t["id"]: t["number"] for t in tests}
    jobs = []
    times = {}
    if schedule is not None:
        with gzip.open(Path(__file__).with_name("pivot-times.json.gz"), "rt") as stream:
            times = json.load(stream)
    milliseconds = {n: round(t["seconds"] * 1000) for n, t in lookup.items()}
    for key, numbers in selections.items():
        if not numbers or numbers.intersection(index.unmeasured):
            continue
        rows = [lookup[n] for n in sorted(numbers)]
        duration = sum(t["seconds"] for t in rows)
        pivot = None
        if len(numbers) > 12 or duration > 0.65:
            if not times or numbers == index.all:
                continue
            pivot = _pivot(numbers, lookup, times, milliseconds, schedule)
            if pivot is None:
                continue
            _, chosen, predicted = pivot
            rows = [lookup[chosen]]
        files = {}
        try:
            changes = _parse_diff(patches[key])
            for change in changes:
                original = (index.root / change.path).read_text()
                files[change.path] = _apply(original, change.hunks)[0]
        except (OSError, ValueError):
            continue
        if not files or sum(map(len, files.values())) > 2_000_000:
            continue
        # Full fixture consumers usually produce another two-second worker even when
        # they identify a single failure. Prioritize possible formatting-only changes.
        fixtures = [change for change in changes if change.path.startswith("tests/fixtures/")]
        cosmetic = False
        if fixtures and not pivot:
            def folded(sign):
                return "".join("".join(line[1:].split()).casefold() for change in fixtures
                               for _, hunk in change.hunks for line in hunk
                               if line.startswith(sign) and not line[1:].lstrip().startswith("--"))
            cosmetic = folded("-") == folded("+")
            if not cosmetic:
                continue
        try:
            class_setup = any(len(t["id"].split("::")) >= 3
                              and t["id"].split("::")[1] in setup_classes(index.root, t["file"])
                              for t in rows)
        except (OSError, ValueError, SyntaxError):
            continue
        if class_setup and not pivot and not (cosmetic and duration < 0.25):
            continue
        time_limit = min(2.6, (2.2 if class_setup else 1.6) + duration)
        if pivot:
            # Normal whole-group confirmations get the first share of the existing budget.
            jobs.append((2, -pivot[0], key, files, [t["id"] for t in rows],
                         min(2.6, 0.8 + 1.8 * predicted)))
        else:
            jobs.append((class_setup, duration, key, files, [t["id"] for t in rows], time_limit))
    if not jobs:
        return selections
    jobs.sort(key=lambda item: (item[0], item[1], item[2]))
    env = dict(os.environ, PYTHON_CPU_COUNT="2", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
               PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    # Set the process count before Python starts, so forked pytest workers and
    # spawned upstream process pools observe the same two-CPU limit.
    try:
        proc = subprocess.run([sys.executable, __file__, "--supervise"],
                              input=json.dumps({"root": str(index.root), "cases": [j[2:] for j in jobs]}),
                              text=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=21)
        confirmed = proc.stdout
    except subprocess.TimeoutExpired as timeout:
        confirmed = timeout.stdout or b""
        if isinstance(confirmed, bytes):
            confirmed = confirmed.decode(errors="ignore")
    except OSError:
        return selections
    for row in confirmed.splitlines():
        try:
            key, passed = json.loads(row)
            if key in selections:
                selections[key] = selections[key].difference(ids.get(name) for name in passed)
        except (ValueError, TypeError):
            continue
    return selections


if __name__ == "__main__":
    if sys.argv[1:] != ["--supervise"]:
        raise SystemExit(2)
    supervise()

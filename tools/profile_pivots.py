"""Measure isolated cold pytest CPU for the unchanged pinned checkout only."""
import argparse
import gzip
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pytest_replay import overlay, trial


def run_one(pytest, root, nodeid):
    with tempfile.TemporaryDirectory() as folder:
        tmp = Path(folder).resolve()
        checkout = tmp / "src"
        output = tmp / "passed.json"
        file = nodeid.split("::", 1)[0]
        overlay(root, checkout, {file: (root / file).read_text()})
        pid = os.fork()
        if pid == 0:
            try:
                trial(pytest, checkout, [nodeid], {}, output, 2)
            except BaseException:
                os._exit(1)
            os._exit(0)
        deadline = time.monotonic() + 3.25
        while True:
            done, status, usage = os.wait4(pid, os.WNOHANG)
            if done:
                break
            if time.monotonic() >= deadline:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    os.kill(pid, signal.SIGKILL)
                _, status, usage = os.wait4(pid, 0)
                break
            time.sleep(0.012)
        if (os.waitstatus_to_exitcode(status) == 0 and output.exists()
                and json.loads(output.read_text()) == [nodeid]):
            return usage.ru_utime + usage.ru_stime
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    # Python reads PYTHON_CPU_COUNT during startup. The timed worker forks from this process.
    if os.environ.get("PYTHON_CPU_COUNT") != "2":
        os.execve(sys.executable, [sys.executable, __file__, *sys.argv[1:]],
                  dict(os.environ, PYTHON_CPU_COUNT="2", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
                       PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"))
    import pytest
    times = {}
    for test in json.loads(args.manifest.read_text())["tests"]:
        if test["seconds"] < 0.35:
            continue
        nodeid = test["id"]
        first = run_one(pytest, args.source.resolve(), nodeid)
        if first is None or first >= 1.45:
            continue
        second = run_one(pytest, args.source.resolve(), nodeid)
        if second is not None and second < 1.45:
            times[nodeid] = round(max(first, second), 3)
    payload = json.dumps(times, sort_keys=True, separators=(",", ":")).encode()
    args.output.write_bytes(gzip.compress(payload, mtime=0))
    print(f"{len(times)} confirmed individual timing measurements")


if __name__ == "__main__":
    main()

"""Trace Python descendants of passing tests whose main-process coverage was incomplete."""
import argparse
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


# Python normally imports sitecustomize before running either -c or a module.
# A status record per process plus a Popen count detects children that missed it.
SITE = r'''
import json
import os
import sys
root = os.environ["BR_SOURCE"] + os.sep
parent = os.environ.setdefault("BR_FIRST_PID", str(os.getpid()))
descendant = str(os.getpid()) != parent
log = os.open(os.path.join(os.environ["BR_LOG"], str(os.getpid())), os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
seen = set()
def record(event, key="", once=True):
    item = (event, key)
    if not once or item not in seen:
        seen.add(item)
        os.write(log, json.dumps(item).encode() + b"\n")
record("child" if descendant else "root")

def auditing(event, args):
    if event == "subprocess.Popen":
        executable, argv, cwd, env = args
        record("launch", once=False)
        if (not isinstance(executable, str) or "python" not in os.path.basename(executable)
                or not isinstance(argv, (tuple, list)) or any(k in argv for k in ("-I", "-S", "-E"))
                or (env is not None and (env.get("BR_LOG") != os.environ["BR_LOG"]
                    or env.get("BR_FIRST_PID") != parent))):
            record("unknown")
    elif event in ("os.fork", "os.forkpty", "os.exec", "os.posix_spawn"):
        # A fork without a new Python interpreter can reuse the parent's log.
        record("unknown")
    elif descendant and event == "open" and isinstance(args[0], (bytes, str)):
        name = os.path.abspath(os.fsdecode(args[0]))
        if not name.startswith(root) or (args[2] & os.O_ACCMODE) == os.O_WRONLY:
            return
        frame = sys._getframe(1).f_code
        if frame.co_name == "get_data" and "importlib" in frame.co_filename:
            return
        record("read", name[len(root):])

if descendant:
    monitor = sys.monitoring
    tool = monitor.COVERAGE_ID
    def enter(code, _offset):
        name = code.co_filename
        if name.startswith(root):
            record("scope", name[len(root):] + "|" + str(code.co_firstlineno) + ":" + code.co_qualname)
        return monitor.DISABLE
    monitor.use_tool_id(tool, "child call coverage")
    monitor.register_callback(tool, monitor.events.PY_START, enter)
    monitor.set_events(tool, monitor.events.PY_START)
sys.addaudithook(auditing)
'''


def trace(root, name):
    with tempfile.TemporaryDirectory() as tmp:
        hooks = Path(tmp)
        logs = hooks / "logs"
        logs.mkdir()
        (hooks / "sitecustomize.py").write_text(SITE)
        env = dict(os.environ, BR_SOURCE=str(root), BR_LOG=str(logs), PYTHONPATH=os.pathsep.join([tmp, str(root)]),
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0",
                   OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", PYTHON_CPU_COUNT="2",
                   PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
        try:
            p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", name],
                               cwd=root, env=env, capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            return None
        if p.returncode or b"1 passed" not in p.stdout:
            return None
        records = [json.loads(line) for log in logs.iterdir() for line in log.read_bytes().splitlines()]
        tags = [kind for kind, _ in records]
        if "unknown" in tags or tags.count("root") != 1 or not tags.count("child") or tags.count("launch") != tags.count("child"):
            return None
        return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("source", "coverage", "output"):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    with gzip.open(args.coverage) as f:
        coverage = json.load(f)
    ids = coverage["ids"]
    incomplete = int(coverage["sets"][coverage["incomplete"]], 16)
    complete = 0
    scopes, reads = {}, {}
    for n, name in enumerate(ids):
        bit = 1 << n
        if not bit & incomplete:
            continue
        records = trace(args.source.resolve(), name)
        if records is None:
            continue
        complete |= bit
        for kind, key in records:
            if kind in ("scope", "read"):
                target = scopes if kind == "scope" else reads
                target[key] = target.get(key, 0) | bit
    def encode(values):
        return {k: format(v, "x") for k, v in sorted(values.items())}
    data = dict(ids=ids, python=sys.implementation.cache_tag, platform=sys.platform, complete=format(complete, "x"),
                scopes=encode(scopes), reads=encode(reads))
    args.output.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
    print(f"Traced {complete.bit_count()}/{incomplete.bit_count()} incomplete tests, {len(scopes)} child scopes.")


if __name__ == "__main__":
    main()

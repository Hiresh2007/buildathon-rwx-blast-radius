"""Record every pinned source/fixture opened by each test in a fresh interpreter."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HOOK = """
import json
import os
import sys
root = os.path.realpath(os.environ["RWX_DEP_SOURCE"]) + os.sep
log = os.open(os.path.join(os.environ["RWX_DEP_LOG"], str(os.getpid())), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
seen = set()
def witness(path):
    if path not in seen:
        seen.add(path)
        os.write(log, json.dumps(path).encode() + b"\\n")
def audit(event, args):
    if event == "subprocess.Popen":
        exe, argv, cwd, env = args
        if not isinstance(exe, str) or "python" not in os.path.basename(exe) or not isinstance(argv, (list, tuple)) or any(x in ("-S", "-E", "-I") for x in argv) or (env is not None and env.get("RWX_DEP_LOG") != os.environ["RWX_DEP_LOG"]):
            witness("!unknown-child")
    if event != "open" or not isinstance(args[0], (bytes, str)) or (args[2] & os.O_ACCMODE) == os.O_WRONLY:
        return
    name = os.path.abspath(os.fsdecode(args[0]))
    if not name.startswith(root):
        return
    name = name[len(root):]
    if name.endswith(".pyc") and "/__pycache__/" in name:
        import importlib.util
        name = importlib.util.source_from_cache(name)
    witness(name)
sys.addaudithook(audit)
"""


def measure(source, ids, hooks, jobs):
    def run_one(item):
        i, name = item
        with tempfile.TemporaryDirectory() as logs:
            env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(hooks), str(source)]),
                       PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", SKIP_INTEGRATION="0",
                       PYTHON_CPU_COUNT="2", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
                       RWX_DEP_SOURCE=str(source), RWX_DEP_LOG=logs)
            cmd = [sys.executable, "-m", "pytest", "-q", "--assert=plain", "-p", "no:cacheprovider", name]
            try:
                proc = subprocess.run(cmd, cwd=source, env=env, capture_output=True, timeout=180)
            except subprocess.TimeoutExpired:
                return i, None
            if proc.returncode != 0 or b"1 passed" not in proc.stdout:
                return i, None
            paths = {json.loads(line) for p in Path(logs).iterdir() for line in p.read_bytes().splitlines()}
            if "!unknown-child" in paths or name.split("::", 1)[0] not in paths:
                return i, None
            return i, paths
    masks = {}
    untraced = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for n, (i, paths) in enumerate(pool.map(run_one, enumerate(ids)), 1):
            if paths is None:
                untraced |= 1 << i
            else:
                for path in paths:
                    if not path.startswith("."):
                        masks[path] = masks.get(path, 0) | (1 << i)
            if n % 100 == 0:
                print(f"{n}/{len(ids)} tests", flush=True)
    return masks, untraced


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--jobs", type=int, default=3)
    args = parser.parse_args()
    source = args.source.resolve()
    tests = sorted(json.loads(args.manifest.read_bytes())["tests"], key=lambda t: t["number"])
    ids = [t["id"] for t in tests]
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    with tempfile.TemporaryDirectory() as tmp:
        hooks = Path(tmp)
        (hooks / "sitecustomize.py").write_text(HOOK)
        masks, untraced = measure(source, ids, hooks, args.jobs)
    data = {"ids": ids, "unknown": format(untraced, "x"),
            "files": {path: format(mask, "x") for path, mask in sorted(masks.items())}}
    args.output.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
    print(f"{len(ids) - untraced.bit_count()}/{len(ids)} complete isolated tests, {len(masks)} paths.", flush=True)


if __name__ == "__main__":
    main()

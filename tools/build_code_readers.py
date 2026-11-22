"""Index pinned Python sources which inspect function bytecode or closures."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
from types import CodeType


MARKERS = {"__code__", "__closure__", "getsource", "getsourcelines", "getclosurevars", "get_instructions",
           "findsource", "gi_code", "cr_code", "ag_code", "dis", "marshal"}


def inspects_code(contents, filename):
    pending = [compile(contents, filename, "exec", dont_inherit=True)]
    while pending:
        code = pending.pop()
        names = list(code.co_names)
        names.extend(v for v in code.co_consts if type(v) is str)
        if any(name in MARKERS or name.startswith("co_") for name in names):
            return True
        pending.extend(v for v in code.co_consts if type(v) is CodeType)
    return False


def build(root):
    libraries, readers = {}, {}
    for directory in ("sqlglot", "tests"):
        for file in (root / directory).rglob("*.py"):
            path = file.relative_to(root).as_posix()
            contents = file.read_bytes()
            key = hashlib.sha256(contents).hexdigest()
            if directory == "sqlglot":
                libraries[path] = key
            if inspects_code(contents, path):
                readers[path] = key
    tree = hashlib.sha256(json.dumps(libraries, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    return {"library": tree, "readers": readers}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    packed = build(args.root)
    args.output.write_bytes(gzip.compress(json.dumps(packed, separators=(",", ":"), sort_keys=True).encode(), mtime=0))
    print(f"Indexed {len(packed['readers'])} code readers")

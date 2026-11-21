"""Run a public benchmark from local temporary storage, as in the contest container."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path)
    source.add_argument("--archive", type=Path)
    args = parser.parse_args()
    entry = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="blast-radius-") as temp:
        target = Path(temp)
        for name in [*entry.glob("*.py"), *entry.glob("*.json.gz")]:
            shutil.copyfile(name, target / name.name)
        if args.source:
            checkout = args.source.resolve()
        else:
            archive = target / "baseline.tar.gz"
            shutil.copyfile(args.archive, archive)
            expected = "93b2445f15f395b81c20556777ece19a6c7507df950f13fa9fac4a3be739af0d"
            if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
                raise SystemExit("pinned source checksum mismatch")
            checkout = target / "sqlglot"
            checkout.mkdir()
            subprocess.run(["tar", "-xzf", archive, "--strip-components=1", "-C", checkout], check=True)
        # Keep network-backed working directories outside the selector's wall-time budget.
        local_data = target / "data"
        local_data.mkdir()
        shutil.copyfile(args.data / "tests.json", local_data / "tests.json")
        shutil.copytree(args.data / "commits", local_data / "commits")
        subprocess.run([sys.executable, "-I", "-B", "-S", target / "symbols.py", "--build-baseline",
                        checkout, target / "symbols.json.gz", target / "symbol-baseline.sqlite3"],
                       stdout=subprocess.DEVNULL, check=True)
        subprocess.run([sys.executable, target / "selector.py", local_data],
                       env=dict(os.environ, SQLGLOT_ROOT=str(checkout)), check=True)


if __name__ == "__main__":
    main()

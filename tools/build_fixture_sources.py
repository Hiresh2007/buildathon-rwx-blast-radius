"""Fingerprint pinned SQL optimizer fixtures used by coverage-based test selection."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path


def fingerprints(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (root / 'tests/fixtures/optimizer').rglob('*.sql') if path.is_file()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    values = fingerprints(args.source)
    args.output.write_bytes(gzip.compress(json.dumps(values, sort_keys=True, separators=(',', ':')).encode(), mtime=0))

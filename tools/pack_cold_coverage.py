"""Merge independently observed pytest selections into a platform-specific coverage map."""
import argparse
from collections import defaultdict
import gzip
import hashlib
import json
from pathlib import Path

if __package__:
    from .profile_cold_test import source_manifest
else:
    from profile_cold_test import source_manifest


def load(path):
    with gzip.open(path, 'rt') as stream:
        return json.load(stream)


def expected_members(ids, target):
    return {name for name in ids if target in (name, name.split("::")[0])}


def pack(source, inventory, samples):
    ids = load(inventory)['ids']
    hashes, tree = source_manifest(source)
    groups = []
    reads = defaultdict(int)
    files = {}
    runtime = None
    for path in samples:
        sample = load(path)
        profile_runtime = tuple(sample[f] for f in ('python', 'platform', 'machine', 'version'))
        if runtime is not None and runtime != profile_runtime:
            raise ValueError('cannot combine coverage from distinct runtimes')
        runtime = profile_runtime
        target = sample['target']
        test_hash = hashlib.sha256((source / target.split('::')[0]).read_bytes()).hexdigest()
        if sample['tree'] != tree or sample['test_hash'] != test_hash:
            raise ValueError(f'profile source does not match the checkout: {target}')
        expected = expected_members(ids, target)
        if not expected or sample['unknown'] or set(sample['ids']) != expected:
            raise ValueError(f'incomplete baseline pytest selection: {target}')
        bit = 1 << len(groups)
        members = sum(1 << i for i, name in enumerate(ids) if name in expected)
        groups.append(format(members, 'x'))
        for filename in sample['reads']:
            reads[filename] |= bit
        for filename, scopes in sample['files'].items():
            table = files.setdefault(filename, {})
            for name, (key, offsets) in scopes.items():
                record = table.setdefault(name, [key, 0, 0, defaultdict(int)])
                record[1] |= bit
                if not key or record[0] != key:
                    record[2] |= bit
                else:
                    for offset in offsets:
                        record[3][offset] |= bit
    for table in files.values():
        for name, (key, calls, uncertain, hits) in table.items():
            values = defaultdict(int)
            for offset, mask in hits.items():
                values[mask] |= 1 << (offset // 2)
            table[name] = [key, format(calls, 'x'), format(uncertain, 'x'),
                           [[format(mask, 'x'), format(value, 'x')] for mask, value in sorted(values.items())]]
    return dict(python=runtime[0], platform=runtime[1], machine=runtime[2], version=runtime[3], ids=ids, groups=groups,
                files=files, reads={f: format(value, 'x') for f, value in reads.items()}, hashes=hashes)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('inventory', type=Path)
    parser.add_argument('target', type=Path)
    parser.add_argument('profiles', type=Path, nargs='+')
    args = parser.parse_args()
    result = pack(args.source, args.inventory, args.profiles)
    args.target.write_bytes(gzip.compress(json.dumps(result, separators=(',', ':'), sort_keys=True).encode(), mtime=0))

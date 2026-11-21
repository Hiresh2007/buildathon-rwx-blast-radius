"""Add fully observed baseline pytest invocations to the current platform's cold map."""
import argparse
import gzip
import json
import os
from pathlib import Path
import platform
import resource
import signal
import subprocess
import sys
import tempfile
import time

if __package__:
    from .pack_cold_coverage import load, expected_members, pack
    from .profile_cold_test import source_manifest
else:
    from pack_cold_coverage import load, expected_members, pack
    from profile_cold_test import source_manifest


def combine(original, additional):
    """Append disjoint observation groups for one pinned runtime, preserving uncertain calls."""
    for key in ('python', 'version', 'platform', 'machine', 'ids', 'hashes'):
        if original[key] != additional[key]:
            raise ValueError(f'incompatible cold coverage: {key}')
    shift = len(original['groups'])
    original['groups'].extend(additional['groups'])
    for path, new_mask in additional['reads'].items():
        old = int(original['reads'].get(path, '0'), 16)
        original['reads'][path] = format(old | (int(new_mask, 16) << shift), 'x')
    for path, scopes in additional['files'].items():
        table = original['files'].setdefault(path, {})
        for name, (key, calls, uncertain, hits) in scopes.items():
            record = table.setdefault(name, [key, '0', '0', []])
            new_calls = int(calls, 16) << shift
            record[1] = format(int(record[1], 16) | new_calls, 'x')
            new_uncertain = int(uncertain, 16) << shift
            if key is None or record[0] != key:
                new_uncertain |= new_calls
            else:
                record[3].extend([format(int(mask, 16) << shift, 'x'), reached] for mask, reached in hits)
            record[2] = format(int(record[2], 16) | new_uncertain, 'x')
    return original


def collect(root, target, output, budget):
    command = [sys.executable, str(Path(__file__).with_name('profile_cold_test.py')),
               str(root), target, str(output), '--timeout', str(max(1, int(budget - 2)))]
    process = subprocess.Popen(command, start_new_session=True)
    try:
        return process.wait(budget) == 0 and output.is_file()
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        return False


def build(root, inventory, destination, targets, budget):
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    deadline = time.monotonic() + budget
    current = load(destination) if destination.is_file() else None
    ids = load(inventory)['ids']
    hashes, _ = source_manifest(root)
    identity = dict(ids=ids, hashes=hashes, python=sys.implementation.cache_tag, version=list(sys.version_info[:3]),
                    platform=sys.platform, machine=platform.machine())
    if current and any(current.get(key) != value for key, value in identity.items()):
        current = None
    recorded = set(current['groups']) if current else set()
    numbers = {name: i for i, name in enumerate(ids)}
    added = 0
    with tempfile.TemporaryDirectory() as folder:
        for index, target in enumerate(targets):
            expected = expected_members(ids, target)
            group = format(sum(1 << numbers[name] for name in expected), 'x')
            if not expected or group in recorded:
                continue
            remaining = deadline - time.monotonic()
            if remaining < 5:
                break
            output = Path(folder) / f'{index}.json.gz'
            if not collect(root, target, output, min(remaining, 150)):
                print(f'Incomplete baseline profile for {target}', flush=True)
                continue
            try:
                new = pack(root, inventory, [output])
                current = combine(current, new) if current is not None else new
            except (KeyError, ValueError) as exc:
                print(f'Unmatched baseline profile for {target}: {exc}', flush=True)
                continue
            recorded.add(group)
            added += 1
    if added:
        temporary = destination.with_suffix('.partial')
        temporary.write_bytes(gzip.compress(json.dumps(current, separators=(',', ':'), sort_keys=True).encode(), mtime=0))
        temporary.replace(destination)
        print(f'Added {added} complete groups to {destination}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('inventory', type=Path)
    parser.add_argument('targets', nargs='+')
    parser.add_argument('--budget', type=int, default=240)
    args = parser.parse_args()
    destination = args.inventory.parent / f'cold-{sys.platform}-{platform.machine()}.json.gz'
    build(args.source, args.inventory, destination, args.targets, args.budget)

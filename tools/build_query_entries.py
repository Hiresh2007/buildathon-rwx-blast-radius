"""Index original conditional entries absent from independently recorded cold executor tests."""
import argparse
import ast
import dis
import gzip
import hashlib
import json
from pathlib import Path
import sys
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cold_coverage import cold_hits
from execution import block_starts, bytecode_key, scope_name
from probe import GROUPS, TEST_FILE


def nested(code):
    yield code
    for child in code.co_consts:
        if isinstance(child, types.CodeType):
            yield from nested(child)


def build(root, archives, destination):
    profiles = [json.loads(gzip.decompress(path.read_bytes())) for path in archives]
    if len({(p["platform"], p["machine"]) for p in profiles}) < 2:
        raise ValueError("two independently observed runtimes are required")
    first = profiles[0]
    if any(p["ids"] != first["ids"] or p["hashes"] != first["hashes"]
           or p["version"] != list(sys.version_info[:3]) or p["python"] != sys.implementation.cache_tag
           for p in profiles):
        raise ValueError("incompatible test inventory, source or Python bytecode version")
    observations = []
    for profile in profiles:
        members = [int(bits, 16) for bits in profile["groups"]]
        targets = []
        for name in GROUPS:
            bit = 1 << profile["ids"].index(TEST_FILE + name)
            targets.append(sum(1 << i for i, group in enumerate(members) if group & bit))
        observations.append((profile, targets))
    files = {}
    for path, expected in first["hashes"].items():
        if not path.startswith("sqlglot/") or not path.endswith(".py"):
            continue
        source = (root / path).read_bytes()
        if hashlib.sha256(source).hexdigest() != expected:
            raise ValueError(f"source mismatch: {path}")
        tree = ast.parse(source, path)
        branches = {suite[0].lineno for node in ast.walk(tree) if isinstance(node, ast.If)
                    for suite in (node.body, node.orelse) if suite and node.lineno < suite[0].lineno}
        entries = {}
        for code in nested(compile(source, path, "exec", dont_inherit=True)):
            if not branches or not code.co_flags & 1:
                continue
            starts = set(block_starts(code))
            offsets = {}
            active = None
            for op in dis.get_instructions(code):
                if op.offset in starts:
                    line = getattr(op, "line_number", op.starts_line)
                    active = line if op.starts_line and line in branches else None
                if active is not None:
                    offsets[active] = offsets.get(active, 0) | (1 << (op.offset // 2))
            if not offsets:
                continue
            key, name = bytecode_key(code), scope_name(code)
            for line, bits in offsets.items():
                potential = (1 << len(GROUPS)) - 1
                for profile, targets in observations:
                    reached = int(profile["reads"].get(path, "0"), 16)
                    reached |= cold_hits(profile, path, name, key, bits, False)
                    for i, groups in enumerate(targets):
                        if not groups & ~reached:
                            potential &= ~(1 << i)
                if potential:
                    entries[f"{name}|{line}"] = potential
        if entries:
            files[path] = [expected, entries]
    data = dict(version=list(sys.version_info[:3]), python=sys.implementation.cache_tag, targets=GROUPS,
                inventory=hashlib.sha256("\n".join(first["ids"]).encode()).hexdigest(), files=files)
    destination.write_bytes(gzip.compress(json.dumps(data, sort_keys=True, separators=(",", ":")).encode(), mtime=0))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("profiles", nargs="+", type=Path)
    args = parser.parse_args()
    build(args.source, args.profiles, args.output)

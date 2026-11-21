"""Trace per-query outputs and line coverage on the pinned baseline; no patch inputs."""
import argparse
import gzip
import json
import os
import pickle
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from probe import GROUPS, load_checkout
from replay_phases import checkout_serializer, digest, parse_transfer


def prepare(root, files):
    load_checkout(root, files)
    from tests import test_executor as mod
    return mod


def query_cases(mod, group):
    return mod.TestExecutor.tpcds_sqls if group == "test_execute_tpcds" else mod.TestExecutor.tpch_sqls


def run_case(mod, group, ordinal, parse=parse_transfer):
    test = mod.TestExecutor(group)
    ds = group == "test_execute_tpcds"
    case = query_cases(mod, group)[ordinal]
    if group == "test_optimized_tpch":
        test.tpch_sqls = [case]
        test.test_optimized_tpch()
        return
    # Match the process-pool boundary of _mp_execute, including pickling arguments
    # and the return table, then run the same dataframe assertions as the test.
    schema = mod.TPCDS_SCHEMA if ds else mod.TPCH_SCHEMA
    tables = test.tpcds_tables if ds else test.tpch_tables
    mod.initializer(schema, tables)
    args, sql, _ = case
    expression = parse(mod.parse_one, sql)
    args = pickle.loads(pickle.dumps(args))
    table = pickle.loads(pickle.dumps(mod.mp_execute(expression, args)))
    if table is not None:
        test.subtestHelper(ordinal, table, tpch=not ds)


def build(root, target):
    lines = {}
    cases = []
    root = root.resolve()
    prefix = str(root) + os.sep
    context = -1
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    def visit(code, line):
        path = code.co_filename
        if context is not None and path.startswith(prefix):
            local = path[len(prefix):]
            if local.startswith("sqlglot/"):
                byline = lines.setdefault(local, {})
                byline.setdefault(line, set()).add(context)
        return mon.DISABLE
    @contextmanager
    def active(i):
        nonlocal context
        previous, context = context, i
        mon.restart_events()
        try:
            yield
        finally:
            context = previous
            mon.restart_events()
    mon.use_tool_id(tool, "query-context")
    mon.register_callback(tool, mon.events.LINE, visit)
    mon.set_events(tool, mon.events.LINE)
    mod = prepare(root, {})
    mod.TestExecutor.setUpClass()
    from sqlglot import executor
    serializer = checkout_serializer(root)
    def wrap(phase, bit, func, convert):
        def record(*args, **kwargs):
            start = time.process_time()
            with active(4 * len(cases) + bit):
                result = func(*args, **kwargs)
            with active(None):
                fingerprints[phase].append(digest(convert(result)))
                seconds[phase] += time.process_time() - start
            return result
        return record
    executor.optimize = wrap("optimize", 2, executor.optimize, serializer)
    mod.transpile = wrap("transpile", 3, mod.transpile, lambda sql: sql)
    parse = wrap("parse", 1, parse_transfer, serializer)
    for group in GROUPS:
        mod.TestExecutor.cache.clear()
        for ordinal in range(len(query_cases(mod, group))):
            fingerprints, seconds = defaultdict(list), defaultdict(float)
            with active(4 * len(cases)):
                run_case(mod, group, ordinal, parse=parse)
            cases.append((group, ordinal, fingerprints, {p:round(s, 5) for p,s in seconds.items()}))
    mon.set_events(tool, 0)
    mod.TestExecutor.tearDownClass()
    masks = {}
    def key(indices):
        bits = sum(1 << (len(cases) * 4 if i == -1 else i) for i in indices)
        return masks.setdefault(bits, len(masks))
    compact = {p: {n: key(ids) for n, ids in coverage.items()} for p, coverage in lines.items()}
    data = dict(cases=cases, lines=compact, masks=[format(mask, "x") for mask in masks])
    target.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
    print(f"{len(cases)} queries, {len(compact)} files, {target.stat().st_size} bytes")


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("root", type=Path)
    args.add_argument("target", type=Path)
    options = args.parse_args()
    build(options.root, options.target)

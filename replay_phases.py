"""Reproduce the pure parse, worker serialization, optimize and SQL generation phases."""
import hashlib
import importlib.util
import json
import pickle


def checkout_serializer(root):
    # Keep the comparison independent of edits to the candidate's serde implementation.
    spec = importlib.util.spec_from_file_location("baseline_serializer", root / "sqlglot/serde.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.dump


def digest(value):
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def parse_transfer(parse_one, sql):
    return pickle.loads(pickle.dumps(parse_one(sql)))


def verify(root, checks, records):
    from sqlglot import parse_one, transpile
    from sqlglot.executor import optimize
    from sqlglot.schema import ensure_schema
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    serializer = checkout_serializer(root)
    for group, cases in checks.items():
        family = "tpc-ds" if group.endswith("tpcds") else "tpc-h"
        rows = list(load_sql_fixture_pairs(f"optimizer/{family}/{family}.sql"))
        schema = TPCDS_SCHEMA if family == "tpc-ds" else TPCH_SCHEMA
        try:
            for key, phases in cases.items():
                ordinal = int(key)
                _, sql, optimized = rows[ordinal]
                expr = None
                for phase in phases:
                    if phase == "parse":
                        expr = parse_transfer(parse_one, sql)
                        value = [digest(serializer(expr))]
                    elif phase == "optimize":
                        if expr is None:
                            expr = parse_transfer(parse_one, sql)
                        value = [digest(serializer(optimize(expr, ensure_schema(schema),
                                                           leave_tables_isolated=True, dialect=None)))]
                    elif phase == "transpile":
                        sqls = (sql, optimized) if group == "test_optimized_tpch" else (sql,)
                        value = [digest(transpile(query, write="duckdb")) for query in sqls]
                    else:
                        raise ValueError("unmodelled phase")
                    if value != records[group][key][phase]:
                        raise ValueError("candidate result changed")
        except Exception:
            continue
        print(group, flush=True)

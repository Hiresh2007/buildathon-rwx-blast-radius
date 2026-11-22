"""Select affected SQLGlot tests and print one worker schedule per patch."""

import argparse
import gzip
import json
import math
import time
from functools import lru_cache
from pathlib import Path

from impact import ImpactIndex, choose_tests
from probe import QueryProbe
from pytest_replay import refine


def schedule(test_numbers, milliseconds):
    """Compare longest-first schedules using the scorer's integer cost model."""
    ordered = sorted(test_numbers, key=lambda n: (-milliseconds[n], n))
    if not ordered:
        return []
    total = sum(milliseconds[n] for n in ordered)
    longest = milliseconds[ordered[0]]
    best_workers = []
    best_cost = float("inf")
    for count in range(1, min(8, len(ordered)) + 1):
        overhead = count * 2000
        if max(longest, (total + count - 1) // count) + overhead >= best_cost:
            continue
        bins = [[] for _ in range(count)]
        loads = [0] * count
        for number in ordered:
            worker = loads.index(min(loads))
            bins[worker].append(number)
            loads[worker] += milliseconds[number]
        bins = [b for b in bins if b]
        cost = max(loads) + 2000 * len(bins)
        if cost < best_cost:
            best_workers, best_cost = bins, cost
    return best_workers


def fill_slack(workers, related, milliseconds):
    """Add related tests while keeping the existing worker count and makespan."""
    loads = [sum(milliseconds[n] for n in worker) for worker in workers]
    ceiling = max(loads)
    included = {n for worker in workers for n in worker}
    for candidates in related:
        for number in sorted(candidates - included, key=lambda n: (milliseconds[n], n)):
            worker = loads.index(min(loads))
            duration = milliseconds[number]
            if loads[worker] + duration > ceiling:
                break
            workers[worker].append(number)
            loads[worker] += duration
            included.add(number)


def worker_cost(workers, milliseconds):
    maximum = max((sum(milliseconds[n] for n in worker) for worker in workers), default=0)
    return 2000 * len(workers) + maximum


def schedule_cost(numbers, milliseconds):
    return worker_cost(schedule(numbers, milliseconds), milliseconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path, nargs="?", default=Path("/data"))
    args = parser.parse_args()
    started = time.monotonic()
    tests = json.loads((args.data / "tests.json").read_bytes())["tests"]
    with gzip.open(Path(__file__).with_name("coverage.json.gz"), "rt", encoding="utf-8") as stream:
        index = ImpactIndex(json.load(stream), tests)
    probe = QueryProbe(tests)
    milliseconds = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
    @lru_cache(maxsize=64)
    def plan(numbers):
        workers = schedule(numbers, milliseconds)
        return workers, worker_cost(workers, milliseconds)

    def planned_workers(numbers, _costs):
        return plan(frozenset(numbers))[0]

    def price(numbers):
        return plan(frozenset(numbers))[1]

    free_tests = {number for number, duration in milliseconds.items() if duration == 0}
    patches = {}
    selections = {}
    protected = {}
    cold_only = set()
    full_cost = None
    for path in sorted((args.data / "commits").glob("*.patch")):
        if time.monotonic() - started > 125:
            selections[path.stem] = index.all
            continue
        try:
            patch = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            selections[path.stem] = index.all
            continue
        patches[path.stem] = patch
        protected[path.stem] = set()
        prepared = {}
        selected = choose_tests(patch, index, tests, protected=protected[path.stem], prepared_changes=prepared)
        if selected == index.all:
            from cold_coverage import unaffected
            spared = unaffected(patch, index, prepared) - protected[path.stem]
            if spared:
                if full_cost is None:
                    full_cost = price(selected)
                revised = selected - spared
                if full_cost - price(revised) >= 1000:
                    selected = revised
                    cold_only.add(path.stem)
            from module_impact import boundary_selection
            narrowed = boundary_selection(patch, index, prepared, selected, price, protected[path.stem])
            if narrowed != selected:
                selected = narrowed
                cold_only.add(path.stem)
        elif not protected[path.stem]:
            selected = probe.select(patch, index, selected, milliseconds, planned_workers)
        selections[path.stem] = selected
    if time.monotonic() - started < 140:
        candidates = {key: selections[key] for key in patches if selections[key] != index.all and not protected[key] and key not in cold_only}
        selections.update(refine(patches, candidates, index, tests, planned_workers))
    for key, selected in selections.items():
        selected = selected | protected.get(key, set())
        workers = [worker.copy() for worker in planned_workers(selected, milliseconds)]
        if workers and selected != index.all:
            if len(workers) == 1:
                fill_slack(workers, [free_tests], milliseconds)
            elif key in patches:
                fill_slack(workers, [*index.related_tests(patches[key]), index.all], milliseconds)
        print(json.dumps({"commit": key, "workers": workers},
                         separators=(",", ":")))


if __name__ == "__main__":
    main()

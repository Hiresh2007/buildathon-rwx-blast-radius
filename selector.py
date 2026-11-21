"""Select affected SQLGlot tests and print one worker schedule per patch."""

import argparse
import gzip
import json
from pathlib import Path

from impact import ImpactIndex, choose_tests


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path, nargs="?", default=Path("/data"))
    args = parser.parse_args()
    tests = json.loads((args.data / "tests.json").read_bytes())["tests"]
    with gzip.open(Path(__file__).with_name("coverage.json.gz"), "rt", encoding="utf-8") as stream:
        index = ImpactIndex(json.load(stream), tests)
    milliseconds = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    for path in sorted((args.data / "commits").glob("*.patch")):
        selected = choose_tests(path.read_text(encoding="utf-8", errors="replace"), index, tests)
        print(json.dumps({"commit": path.stem, "workers": schedule(selected, milliseconds)},
                         separators=(",", ":")))


if __name__ == "__main__":
    main()

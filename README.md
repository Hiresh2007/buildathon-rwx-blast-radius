# Blast Radius

This entry builds a test schedule for each independent SQLGlot patch. It reads the live test IDs and durations, applies the patch to the pinned source in memory, and chooses the cheapest of one through eight worker schedules.

## Run the public benchmark

```sh
python3.13 -m venv .cache/trace-env
.cache/trace-env/bin/python -m pip install -r requirements.lock
make sample PYTHON=.cache/trace-env/bin/python
make sample-docker
```

`make sample` downloads the organizer's public kit and pinned source into `.cache/` and stages the selector and inputs in local temporary storage before comparing with the starter. `make sample-docker` builds and tests the linux/amd64 image under the contest's CPU, memory, filesystem and network limits. Both commands use the organizer's scorer. The leaderboard adds the container’s CPU time. [Track and scoring rules](https://www.joinbuilders.org/buildathon/tracks/blast-radius)

| Selector | Public simulated seconds | Missed patches |
| --- | ---: | ---: |
| Organizer starter | 1,210.470 | 0 |
| Previous entry | 340.483 | 0 |
| Current entry | 106.965 | 0 |

We also generated 14 independent changes across dialect settings, expression declarations, optimizer and generator code, and SQL fixtures. Each change was checked against the 1,620-test inventory. The selector covered all 1,140 failing test/patch pairs, including test files that failed to import, in 79.431 simulated seconds. The previous version took 251.008 seconds on the same changes.

A separate mutation to a shared generator dispatch table broke 1,339 tests and the final schedule included all of them. The public sample also passed in the pinned linux/amd64 image on an emulated x86 processor at 117.095 simulated seconds. Time-limited test runs can choose different schedules on slower hardware.

Two further independent batches exercised the multi-file and operand selection rules. We ran every patch against all 1,620 tests. Nine multi-file mutations produced 25 failing test/patch pairs and took 123.853 simulated seconds, compared with 331.353 for the earlier selector. Twelve single-file mutations of lookup keys, fallback values and function defaults produced 39 failing pairs. One matched run took 93.792 seconds including selector CPU, compared with 264.686. Both batches had zero missed patches. Three additional edits to defaults controlling expression order, quoting and dialect handling produced 121 failing test/patch pairs, all included in the schedules.

## How it chooses tests

- The baseline maps record test-specific source lines, bytecode blocks and files read. A second run executes each of the 1,620 tests in its own process tree to measure imports and fixture dependencies, including Python subprocesses.
- Function edits are checked against compiled scopes and measured blocks. For eligible configuration edits, the selector imports the candidate in an isolated process and compares values with the baseline, including derived class fields and reads of individual dictionary keys.
- For edits to shared cache builders, later readers of a cached value are retained even when another test originally filled the cache.
- For required and optional expression arguments, the selector traces the validator’s actual inputs and checks which tests observe a missing key. Tests that inspect the schema directly remain eligible.
- For simple comparisons and local boolean guards, the selector can rule out a test when every observed operand yields the same result before and after the edit. Unmeasured values keep the candidate.
- For a literal string lookup on a built-in collection, it records the key sets seen by each test. A test can be ruled out when the changed key or fallback value leaves every observed lookup equivalent. Custom mappings and observations above the recording limits stay eligible.
- For a single default-argument edit in an ordinary function or method, it records callers whose bound argument matched the old default. Tests that inspected or changed the function defaults, read the source, or ran unobserved subprocess code remain eligible.
- The longest executor tests contain independent TPC queries. When a function-body edit touches a recorded query phase, the selector can run that phase on the patched source and compare its output with the pinned result. The worker has a bounded CPU and wall-time allowance.
- For short schedules the selector can run the selected pytest cases on a private copy of the changed paths. For longer schedules it can confirm a single test when its measured runtime is sufficiently below the schedule time it could save. A test can be removed when its setup, body, subtests and teardown all pass. Timeouts keep the outstanding candidates.
- Explicit readers of edited source and fixtures remain eligible for evaluation. For scalar substitutions across multiple deferred functions, the selector retains every observed caller of each entire function. Lexical changes, eager execution and unmodeled interactions retain the broader schedule. IDs missing from the baseline inventory are always included.

Existing workers take additional zero-duration tests. Shorter workers take related tests first, then other tests that fit the same integer-millisecond makespan.

The input is `/data/tests.json` and `/data/commits/*.patch`. Standard output contains one JSON object per patch. All checked-in observations come from the pinned checkout. The external scorer reads the sample answers.

## Rebuild the baseline maps

The checked-in maps were collected with CPython 3.13.18 and the organizer's dependency lock on SQLGlot commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. After the native benchmark has downloaded the source and kit, run these commands from the repository root.

```sh
python3.13 -m venv .cache/trace-env
PY=.cache/trace-env/bin/python
SRC=.cache/sqlglot
TESTS=.cache/kit/sample/tests.json
"$PY" -m pip install -r .cache/kit/requirements.lock
export PYTHON_CPU_COUNT=2 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
"$PY" tools/build_map.py "$SRC" "$TESTS" coverage.json.gz
"$PY" tools/build_cache_consumers.py "$SRC" cache-consumers.json.gz
"$PY" tools/profile_pivots.py "$SRC" "$TESTS" pivot-times.json.gz
"$PY" tools/build_execution.py "$SRC" "$TESTS" execution.json.gz
"$PY" tools/build_children.py "$SRC" coverage.json.gz children.json.gz
"$PY" tools/build_arguments.py "$SRC" "$TESTS" arguments.json.gz
"$PY" tools/build_dependencies.py "$SRC" "$TESTS" dependencies.json.gz --jobs 3
"$PY" tools/build_symbols.py "$SRC" "$TESTS" coverage.json.gz symbols.json.gz
"$PY" tools/build_predicates.py "$SRC" "$TESTS" predicates.json.gz
"$PY" tools/build_lookup_domains.py "$SRC" "$TESTS" lookup-domains.json.gz
"$PY" tools/build_default_impact.py "$SRC" "$TESTS" default-impact.json.gz
"$PY" tools/build_query_map.py "$SRC" query-map.json.gz
"$PY" -I -B -S symbols.py --build-baseline "$SRC" symbols.json.gz symbol-baseline.sqlite3
```

The contest organizers supply the sample patches, test manifest, dependency lock and scorer. SQLGlot supplies the test suite and fixtures used to generate the maps. [Organizer's kit](https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz) · [Pinned SQLGlot source](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa)

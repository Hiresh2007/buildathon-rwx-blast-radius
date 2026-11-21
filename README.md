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
| Previous entry (`f402de9`), matched host run | 97.282 | 0 |
| Current entry, matched host run | 97.282 | 0 |

We also generated 14 independent changes across dialect settings, expression declarations, optimizer and generator code, and SQL fixtures. Each change was checked against the 1,620-test inventory. The selector covered all 1,140 failing test/patch pairs, including test files that failed to import, in 79.431 simulated seconds. The previous version took 251.008 seconds on the same changes.

A separate mutation to a shared generator dispatch table broke 1,339 tests and the schedule included all of them. Time-limited test runs can choose different schedules on slower hardware.

Two further independent batches exercised the multi-file and operand selection rules. We ran every patch against all 1,620 tests. Nine multi-file mutations produced 25 failing test/patch pairs and took 123.853 simulated seconds, compared with 331.353 for the earlier selector. Twelve single-file mutations of lookup keys, fallback values and function defaults produced 39 failing pairs. One matched run took 93.792 seconds including selector CPU, compared with 264.686. Both batches had zero missed patches. Three additional edits to defaults controlling expression order, quoting and dialect handling produced 121 failing test/patch pairs, all included in the schedules.

An earlier ten-patch holdout combined six changes to parser and generator functions with four changes to dialect escape tables. Each patch ran against all 1,620 tests. Both versions covered all 15 failing test/patch pairs. Version `f402de9` took 30.521 simulated seconds, down from 248.412. Including selector CPU, the cost fell from 254.883 to 38.779 seconds. Five further changes to PostgreSQL, BigQuery and Snowflake tables and week-start handling produced 12 failing test/patch pairs. Those schedules covered all 12 in 12.796 simulated seconds, down from 33.422.

Ten further patches combined edits to different source files or test modules. The full suite produced 81 failing test/patch pairs. The current schedules included all 81 in 63.611 simulated seconds, compared with 368.170 for `f402de9`. Including selector CPU, the matched totals were 69.474 and 370.842 seconds. A separate eight-patch batch changed declarations and function bodies within the same file. Its schedules cost 79.725 simulated seconds, compared with 294.536 for `f402de9`.

In an eight-patch holdout pairing local guard or membership edits with a second production change, all 18 failing test/patch pairs were selected. On x86 Linux the schedule fell from 294.536 simulated seconds at `bda301ad` to 43.133. The organizer score including container CPU fell from 311.493 to 113.879 seconds in this local emulation. Three additional edits to shared functions produced five further failing pairs, all included.

A subsequent syntax-comparison change produced identical candidate and protected test sets for 123 public and independently generated patches. Across twelve local runs, static analysis consumed 15.026 CPU seconds compared with 23.196 for the guard-aware selector.

On eight paired edits that changed dictionary keys and a separate production function, all 26 failing pairs were scheduled. They took 92.786 simulated seconds compared with 294.536 for the previous selector.

Four single-file changes at frequently executed membership comparisons ran against all 1,620 tests. The Linux schedules included all 13 failing pairs in 49.308 simulated seconds, down from 94.327 at `bda301ad`. Eight independent changes to ordinary membership lists produced eight further failing pairs, all included.

We selected 16 single-file changes to frequently executed or randomly sampled conditions, then ran each patch against all 1,620 tests. The changes produced 1,412 failing patch/test pairs. The current selector retained every pair in 142.194 simulated seconds across the two native batches, compared with 366.631 seconds at `62bf52a`. In a separate 30-patch batch with 16 independent source changes and 14 public fixture, comment or mixed changes, both entries retained all 2,972 failing pairs. The simulated time fell from 250.041 to 224.914 seconds.

The mixed-change selector now recognizes when the only change in one component is switching a comparison with `None` between equality and identity, and all observed operands have passive comparison semantics. It retains the tests that can reach every other changed component, as well as untraced tests and direct readers. Its isolated x86 Linux image selected every public failure in 106.777 simulated seconds.

## How it chooses tests

- The baseline maps record test-specific source lines, bytecode blocks and files read. A second run executes each of the 1,620 tests in its own process tree to measure imports and fixture dependencies, including Python subprocesses.
- Function edits are checked against compiled scopes and measured blocks. For eligible configuration edits, the selector imports the candidate in an isolated process and compares values with the baseline, including derived class fields and reads of individual dictionary keys.
- For edits to shared cache builders, later readers of a cached value are retained even when another test originally filled the cache.
- For required and optional expression arguments, the selector traces the validator’s actual inputs and checks which tests observe a missing key. Tests that inspect the schema directly remain eligible.
- For simple comparisons and local boolean guards, the selector can rule out a test when every observed operand yields the same result before and after the edit. Unmeasured values keep the candidate.
- For a literal string lookup on a built-in collection, it records the key sets seen by each test. A test can be ruled out when the changed key or fallback value leaves every observed lookup equivalent. Custom mappings and observations above the recording limits stay eligible.
- For comparisons with `None`, the selector records which tests used built-in equality or a Python method that returns immediately for operands of different types. For simple condition changes, it records the value after the original expression is evaluated. Tests remain eligible when a custom comparison or truth method could run, when a source file is read directly, or when observations are incomplete.
- For literal configuration dictionaries, the selector collects the keys read with subscripting and `get`. Whole-dictionary reads and unobserved processes stay eligible. The selector uses this map when an edit changes only the values of existing keys and preserves their built-in types.
- For an edit to a scalar default argument, the trace distinguishes calls that omit the argument from calls that explicitly pass it. It records how the function used the default, including truth tests and comparisons with `None`. Tests that inspected the defaults, read the source, or ran unobserved subprocess code remain eligible.
- For mixed patches, the selector combines readers of changed configuration values with callers that omitted changed defaults and tests that executed changed functions. It can analyze separate fields, defaults and function bodies in one file when restoring those nodes reproduces the complete original syntax tree. Any unsupported component uses the broader schedule.
- A literal or operator change inside an existing, undecorated test function retains all tests in its module, along with every other test whose isolated process imported or read that module. Changes to collection, fixtures and shared test helpers use the full suite.
- The longest executor tests contain independent TPC queries. When a function-body edit touches a recorded query phase, the selector can run that phase on the patched source and compare its output with the pinned result. The worker has a bounded CPU and wall-time allowance.
- For short schedules the selector can run the selected pytest cases on a private copy of the changed paths. For longer schedules it can confirm a single test when its measured runtime is sufficiently below the schedule time it could save. A test can be removed when its setup, body, subtests and teardown all pass. Timeouts keep the outstanding candidates.
- For a change to a literal membership list, the selector can compare the observed string inputs with the keys added or removed. It keeps tests that supplied an affected key or a value with unmeasured comparison behavior. For changed dictionary keys in paired edits it also preserves presence checks and consumers of the table’s key set.
- For paired edits to local conditions, the selector compares the surrounding function syntax and keeps every observed caller when the instruction layout changes. A constant membership list may gain or lose values when the element type and membership operation are preserved.
- Explicit readers of edited source and fixtures remain eligible. When a patch changes constants or operators in multiple functions, the selector identifies the first bytecode blocks where execution could differ. It combines their observed test sets across files and retains subprocess and cache consumers. Changes to lexical scope, eager execution or instruction structure use a broader schedule. IDs missing from the baseline inventory are always included.

Existing workers take additional zero-duration tests. Shorter workers take related tests first, then other tests that fit the same integer-millisecond makespan.

When the ordinary selector would schedule the full suite, it can intersect two observations of the unchanged upstream test suite. One records reachability separately for each test in a warm suite. The other records the full lifetime of independent pytest invocations, including imports, collection, execution, teardown and traced Python descendants. A test is eligible for removal only when both observations miss the changed original code and direct reads, caches and incomplete traces permit it. For suspended generators, any observed invocation retains its tests. The selector checks the source hash, interpreter version, OS, architecture and test inventory before using a cold map. It retains the full schedule unless the revised allocation saves at least one simulated second.

For source changes combined with edits to existing optimizer SQL fixtures, the selector checks each fixture against its pinned contents. It retains every observed warm or cold reader as well as the tests reached by any source edit.

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
"$PY" tools/build_fixture_sources.py "$SRC" fixture-sources.json.gz
"$PY" tools/build_cache_consumers.py "$SRC" cache-consumers.json.gz
"$PY" tools/profile_pivots.py "$SRC" "$TESTS" pivot-times.json.gz
"$PY" tools/build_execution.py "$SRC" "$TESTS" execution.json.gz
"$PY" tools/build_children.py "$SRC" coverage.json.gz children.json.gz
"$PY" tools/build_arguments.py "$SRC" "$TESTS" arguments.json.gz
"$PY" tools/build_dependencies.py "$SRC" "$TESTS" dependencies.json.gz --jobs 3
"$PY" tools/build_symbols.py "$SRC" "$TESTS" coverage.json.gz symbols.json.gz
"$PY" tools/build_predicates.py "$SRC" "$TESTS" predicates.json.gz
"$PY" tools/build_none_comparisons.py "$SRC" "$TESTS" none-comparisons.json.gz
"$PY" tools/build_truth_guards.py "$SRC" "$TESTS" truth-guards.json.gz
"$PY" tools/build_lookup_domains.py "$SRC" "$TESTS" lookup-domains.json.gz
"$PY" tools/build_lookup_domains.py "$SRC" "$TESTS" membership-values.json.gz --literal-members
"$PY" tools/build_default_impact.py "$SRC" "$TESTS" default-impact.json.gz
"$PY" tools/build_table_values.py "$SRC" "$TESTS" table-values.json.gz
"$PY" tools/build_query_map.py "$SRC" query-map.json.gz
"$PY" -I -B -S symbols.py --build-baseline "$SRC" symbols.json.gz symbol-baseline.sqlite3
```

The Docker build also profiles the pinned executor TPC-DS node and the optimizer test module on its own architecture. It adds a group only after pytest passes, the complete manifest matches and every spawned Python process supplies a receipt. The training step uses a four-minute wall-clock budget. A failed or timed-out collection keeps the previously recorded groups available.

Build cold coverage on the target architecture with the same dependency lock and pinned source. A selector can be a full pytest file or an individual node ID. Each collection must complete without failures, skips or missing child-process receipts. For example, these commands create a map for a parser test and a test module.

```sh
mkdir -p .cache/cold
"$PY" tools/profile_cold_test.py "$SRC" tests/test_parser.py::TestParser::test_max_nodes .cache/cold/parser.json.gz
"$PY" tools/profile_cold_test.py "$SRC" tests/test_serde.py .cache/cold/serde.json.gz
COLD_MAP=$("$PY" -c 'import platform, sys; print(f"cold-{sys.platform}-{platform.machine()}.json.gz")')
"$PY" tools/pack_cold_coverage.py "$SRC" coverage.json.gz "$COLD_MAP" .cache/cold/*.json.gz
```

The contest organizers supply the sample patches, test manifest, dependency lock and scorer. SQLGlot supplies the test suite and fixtures used to generate the maps. [Organizer's kit](https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz) · [Pinned SQLGlot source](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa)

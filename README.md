# Blast Radius

This entry chooses SQLGlot tests for a batch of independent patches. It uses test-specific line coverage and file reads measured on the pinned SQLGlot checkout. The selector reads the live test IDs and durations, reconstructs each change in memory and emits the test numbers to run. It estimates the cost of one through eight workers in milliseconds and keeps the cheapest schedule.

## Reproduce the public benchmark

```sh
make sample
```

This downloads the public kit and pinned source into `.cache/`, runs our selector and the official starter, and scores both with the organizer's scorer. The reported local totals exclude CPU time. For the Docker runtime check, run `make sample-docker` with a linux/amd64-capable Docker engine. [Track and scoring rules](https://www.joinbuilders.org/buildathon/tracks/blast-radius)

The image reads `/data/tests.json` and `/data/commits/*.patch` and writes one compact JSON object per patch. It includes the pinned baseline source so it can examine surrounding code. Unknown or unsupported changes select a broader set of tests. The runtime has no external package dependencies.

## Regenerate the map

The checked-in `coverage.json.gz` is derived from test execution and fixture reads on SQLGlot commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. To rebuild it after `make sample`, use an isolated Python 3.13 environment.

```sh
python3.13 -m venv .cache/trace-env
.cache/trace-env/bin/python -m pip install -r .cache/kit/requirements.lock
.cache/trace-env/bin/python tools/build_map.py .cache/sqlglot .cache/kit/sample/tests.json coverage.json.gz
make sample
```

The generator uses pytest and CPython monitoring to record each test's dependencies. Subprocess-only coverage is marked incomplete. The public answers are input to the external scorer only.

## Credits

The source under analysis is SQLGlot. The starter, test inventory, sample patches, dependency lock and scorer are from the organizers. Publish this directory as its own public repository with the Dockerfile at the root. [Pinned SQLGlot source](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa) · [Official kit](https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz)

PYTHON ?= python3
IMAGE ?= blast-radius-entry
KIT := .cache/kit
DATA := $(KIT)/sample
KIT_URL := https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz
KIT_SHA256 := 81efabbc69c3c4367d928f2dba0f33ce9dccccbd080b54721b2636e5d8afab9d
SQLGLOT_REV := eb11aec4be8cb2122afe713a4816662c3f0fb2fa
ifndef SQLGLOT_ROOT
SQLGLOT_ROOT := $(CURDIR)/.cache/sqlglot
NATIVE_SOURCE := .cache/sqlglot/.ready
endif

SCORE = $(PYTHON) "$(KIT)/score.py" "$(DATA)" "$(DATA)/answers.json"
# The starter hardcodes /data. Override that path when running it on the host.
STARTER = import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import selector; selector.DATA = Path(sys.argv[2]); selector.main()

.PHONY: sample sample-docker
sample: $(KIT)/.ready $(NATIVE_SOURCE)
	@SQLGLOT_ROOT="$(SQLGLOT_ROOT)" $(PYTHON) selector.py "$(DATA)" > .cache/selector.jsonl
	@$(PYTHON) -c '$(STARTER)' "$(KIT)" "$(DATA)" > .cache/starter.jsonl
	@echo 'Selector on public sample (CPU excluded)'
	@$(SCORE) .cache/selector.jsonl --cpu-seconds 0
	@echo 'Starter on public sample (CPU excluded)'
	@$(SCORE) .cache/starter.jsonl --cpu-seconds 0

$(KIT)/.ready:
	@mkdir -p "$(KIT)"
	@curl -fsSL --retry 3 "$(KIT_URL)" -o "$(KIT)/sample.tar.gz"
	@$(PYTHON) -c 'import hashlib, pathlib, sys; sys.exit(0 if hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest() == sys.argv[2] else "Sample kit checksum mismatch")' "$(KIT)/sample.tar.gz" "$(KIT_SHA256)"
	@tar -xzf "$(KIT)/sample.tar.gz" -C "$(KIT)"
	@touch "$@"

.cache/sqlglot/.ready:
	@mkdir -p .cache/sqlglot
	@curl -fsSL --retry 3 "https://codeload.github.com/tobymao/sqlglot/tar.gz/$(SQLGLOT_REV)" -o .cache/sqlglot.tar.gz
	@tar -xzf .cache/sqlglot.tar.gz -C .cache/sqlglot --strip-components=1
	@touch "$@"

sample-docker: $(KIT)/.ready
	@docker build --platform linux/amd64 --tag "$(IMAGE)" .
	@set -eu; cidfile=$$(mktemp -d)/cid; \
	trap 'if test -s "$$cidfile"; then docker rm -f "$$(cat "$$cidfile")" >/dev/null 2>&1 || true; fi; rm -f "$$cidfile"; rmdir "$${cidfile%/cid}"' EXIT; \
	$(PYTHON) -c 'import subprocess, sys; subprocess.run(sys.argv[1:], timeout=180, check=True)' \
		docker run --rm --cidfile "$$cidfile" --platform linux/amd64 --network none --cpus 2 \
		--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
		--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges \
		--user 65534:65534 --env HOME=/tmp \
		--volume "$(CURDIR)/$(DATA)/commits:/data/commits:ro" \
		--volume "$(CURDIR)/$(DATA)/tests.json:/data/tests.json:ro" \
		"$(IMAGE)" > .cache/docker.jsonl
	@$(PYTHON) -c 'from pathlib import Path; import sys; sys.exit("stdout exceeds 2 MB") if Path(".cache/docker.jsonl").stat().st_size > 2 * 1024 * 1024 else None'
	@$(SCORE) .cache/docker.jsonl --cpu-seconds 0

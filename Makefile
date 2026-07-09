UV ?= uv
STORE ?= stores/synth-100k

# keep the venv OUT of iCloud-synced ~/Documents: iCloud sets the macOS
# hidden flag on .pth files and Python 3.12+ silently skips them, which
# breaks the editable install (symptom: ModuleNotFoundError: tgms from
# console scripts). Same var is documented in README for interactive use.
export UV_PROJECT_ENVIRONMENT ?= $(HOME)/.venvs/tgms

.PHONY: setup test test-full ci lint bench-ops synth-100k synth-1m reproduce clean

setup:
	$(UV) sync --extra agent

test:
	$(UV) run pytest tests/

# M2 acceptance sweep: 500 randomized oracle cases per operator
test-full:
	TGMS_HYP_EXAMPLES=500 $(UV) run pytest tests/

ci: lint
	TGMS_HYP_EXAMPLES=50 $(UV) run pytest tests/ -q

lint:
	$(UV) run ruff check tgms/ tests/

synth-100k:
	$(UV) run tgms synth data_raw/synth-100k --nodes 5000 --events 100000 --seed 1
	$(UV) run tgms ingest data_raw/synth-100k/events.jsonl --store stores/synth-100k

synth-1m:
	$(UV) run tgms synth data_raw/synth-1m --nodes 20000 --events 1000000 --seed 1
	$(UV) run tgms ingest data_raw/synth-1m/events.jsonl --store stores/synth-1m

bench-ops:
	$(UV) run tgms bench ops --store $(STORE) --out docs/bench_ops.md

# full pipeline: ingest -> tests -> task-gen -> matrix -> tables (built out
# through M7; stages land with their milestones)
reproduce: test-full

clean:
	rm -rf stores data_raw .pytest_cache .hypothesis

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

ci: lint hygiene
	TGMS_HYP_EXAMPLES=50 $(UV) run pytest tests/ -q

# spec §8.1: no commit may mix tests//oracle.py with implementation changes
hygiene:
	$(UV) run python scripts/check_commit_hygiene.py

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

data-collegemsg:
	$(UV) run python -c "from tgms.data.loaders import ingest_dataset; \
	  print(ingest_dataset('collegemsg', 'data_raw', 'stores/collegemsg'))"

suite-collegemsg:
	mkdir -p stores/suite-collegemsg
	$(UV) run tgms tasks --store stores/collegemsg --dataset collegemsg \
	  --seed 0 --out stores/suite-collegemsg/suite.json
	$(UV) run tgms memory build --store stores/collegemsg

data-emaileu:
	$(UV) run python -c "from tgms.data.loaders import ingest_dataset; \
	  print(ingest_dataset('email-eu', 'data_raw', 'stores/emaileu'))"

suite-emaileu:
	mkdir -p stores/suite-emaileu
	$(UV) run tgms tasks --store stores/emaileu --dataset email-eu \
	  --seed 0 --out stores/suite-emaileu/suite.json
	$(UV) run tgms memory build --store stores/emaileu

# synthetic campaign store: planted rings/ping-pong/bursts give T2 tasks
# with construction-known gold (spec WP2.5)
synth-t2:
	$(UV) run tgms synth data_raw/synth-t2 --nodes 5000 --events 200000 \
	  --seed 7 --rings 10 --pingpong 6 --bursts 4
	$(UV) run tgms ingest data_raw/synth-t2/events.jsonl --store stores/synth-t2

suite-synth:
	mkdir -p stores/suite-synth
	$(UV) run tgms tasks --store stores/synth-t2 --dataset synth-t2 \
	  --seed 0 --manifest data_raw/synth-t2/manifest.json \
	  --out stores/suite-synth/suite.json
	$(UV) run tgms memory build --store stores/synth-t2

# full pipeline (spec §0): ingest -> operator tests -> task-suite generation
# -> matrix -> tables. The matrix stage needs provider API keys; without
# them, reproduce stops after deterministic stages with instructions.
reproduce: test-full data-collegemsg suite-collegemsg
	@if [ -n "$$ANTHROPIC_API_KEY$$OPENAI_API_KEY" ]; then \
	  $(UV) run tgms eval run --config configs/matrix-dev.yaml; \
	else \
	  echo "deterministic stages done. Set ANTHROPIC_API_KEY (or"; \
	  echo "OPENAI_API_KEY) and run: tgms eval run --config configs/matrix-dev.yaml"; \
	fi

clean:
	rm -rf stores data_raw runs .pytest_cache .hypothesis

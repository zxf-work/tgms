#!/bin/bash
# Resource-axis runs (§14.2 working set vs RAM, §14.3 thread scaling,
# §14.4 reader concurrency, §15 cold vs warm) — REPRODUCE.md launch
# pattern: a script file under nohup that prints RUN_STARTED commit=<sha>
# into its own log first.
# Usage: nohup scripts/run_resources.sh > runs/resources-$(date +%Y%m%d).log 2>&1 &
# Assumes the venv is already built for HEAD (uv sync --extra duckdb
# --reinstall-package tgms — the Rust extension must match the checkout).
set -e
TGMS_REPO="${TGMS_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
export PATH="$HOME/.local/bin:$PATH"
cd "$TGMS_REPO"

echo "RUN_STARTED commit=$(git rev-parse --short HEAD)"
date -u +"UTC %Y-%m-%dT%H:%M:%SZ"

R=benchmarks/results-v1

# not under set -e: a nonzero exit is a hash gate speaking, and the log
# must still record every phase plus RUN_FINISHED with the worst status
set +e
overall=0
run() {
    echo ""
    echo "=== eval_resources $* ==="
    date -u +"UTC %Y-%m-%dT%H:%M:%SZ"
    uv run python scripts/eval_resources.py "$@"
    s=$?
    echo "=== exit=$s ==="
    [ "$s" -ne 0 ] && overall=$s
}

run threads  --scale 1000000  --systems native,duckdb --json "$R/eval-resources-threads-1m.json"
run threads  --scale 10000000 --systems native,duckdb --json "$R/eval-resources-threads-10m.json"
run coldwarm --scale 1000000  --systems native,duckdb --json "$R/eval-resources-coldwarm-1m.json"
run coldwarm --scale 10000000 --systems native,duckdb --json "$R/eval-resources-coldwarm-10m.json"
run readers  --scale 1000000  --duration 30 --barrier-s 45  --json "$R/eval-resources-readers-1m.json"
run readers  --scale 10000000 --duration 60 --barrier-s 150 --json "$R/eval-resources-readers-10m.json"
run memcap   --scale 10000000 --caps 2g,4g,8g --json "$R/eval-resources-memcap-10m.json"

echo ""
echo "RUN_FINISHED exit=$overall"
date -u +"UTC %Y-%m-%dT%H:%M:%SZ"
exit "$overall"

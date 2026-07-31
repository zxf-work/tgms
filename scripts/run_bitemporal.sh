#!/bin/bash
# §13 current-vs-bi-temporal overhead run (REPRODUCE.md launch pattern:
# a script file under nohup that prints RUN_STARTED commit=<sha> into its
# own log first).
# Usage: nohup scripts/run_bitemporal.sh > runs/bitemporal-$(date +%Y%m%d).log 2>&1 &
# Env: TGMS_SCALE (default 1000000), TGMS_DENSITIES (default 0,0.01,0.1,1,5,20)
set -e
TGMS_REPO="${TGMS_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
export PATH="$HOME/.local/bin:$PATH"
cd "$TGMS_REPO"

echo "RUN_STARTED commit=$(git rev-parse --short HEAD)"
date -u +"UTC %Y-%m-%dT%H:%M:%SZ"

SCALE="${TGMS_SCALE:-1000000}"
DENSITIES="${TGMS_DENSITIES:-0,0.01,0.1,1,5,20}"
OUT="benchmarks/results-v1/eval-$(python3 -c "
s=int('$SCALE')
print('10m' if s==10_000_000 else '1m' if s==1_000_000 else '200k' if s==200_000 else s)")-bitemporal.json"

# not under set -e: a nonzero exit is the hash gate speaking, and the log
# must still record RUN_FINISHED with that status
set +e
uv run python scripts/eval_bitemporal.py \
    --scale "$SCALE" --densities "$DENSITIES" --json "$OUT"
status=$?

echo "RUN_FINISHED exit=$status out=$OUT"
date -u +"UTC %Y-%m-%dT%H:%M:%SZ"

#!/bin/bash
#SBATCH --job-name=trunc-probe
#SBATCH --partition=bigTiger
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=24:00:00
#SBATCH --output=/project/xzhang12/tgms/logs/trunc-probe-%j.out

# Established-interface truncation probe (D-142). Frozen configuration
# per external_workloads/probe/PROBE_FREEZE.md: Qwen2.5-14B-Instruct-AWQ
# on vLLM 0.11.0, temperature 0, seed 0, 1024 new tokens, k=10 rows per
# page, 10 tool calls per question. PROBE_FREEZE.md is committed before
# this job is submitted; setup_probe.py runs first and records its
# sha256 in the setup receipt, so the universe is fixed before the
# first token of inference.
#
# Legs: sqlite C0, C1, C2 over the full manifest, then the PostgreSQL
# engine-diversity leg (C0 only, the two smallest eligible databases,
# embedded pgserver), then the scorer.
#
# The cluster venv needs pgserver + psycopg2 for the pg leg.

set -u
export HF_HOME=/project/xzhang12/hf
export HF_HUB_OFFLINE=1

cd /project/xzhang12/tgms

PY=.venv/bin/python
PROBE=external_workloads/probe
DBROOT=/project/xzhang12/minidev_pkg/minidev/MINIDEV/dev_databases

# ---- universe: built and validated against the databases, no model ----
# The universe and both manifests are committed pre-inference
# (c3f33f5); node-side setup re-validates the sqlite endpoints only.
# The pg data lives on NODE-LOCAL storage: initdb fails on the
# /project parallel filesystem, and /run/user does not exist on
# compute nodes, so both the socket dir and pgdata go under /tmp.
export XDG_RUNTIME_DIR=/tmp/xdg_$SLURM_JOB_ID
PGDATA_LOCAL=/tmp/probe_pg_$SLURM_JOB_ID
mkdir -p $XDG_RUNTIME_DIR

$PY $PROBE/setup_probe.py \
  --frozen external_workloads/bird/bird_500_select_sqlite.jsonl \
  --audit external_workloads/bird/multiplicity_audit.jsonl \
  --errata external_workloads/bird/annotation_errata.jsonl \
  --db-root $DBROOT \
  --out $PROBE/probe_manifest.jsonl \
  --out-pg $PROBE/probe_manifest_pg.jsonl \
  --receipt benchmarks/results-v1/eval-trunc-probe-setup.json || exit 1

$PY $PROBE/run_probe.py --print-freeze

# -------------------------------- model server (same pattern as BIRD) --
# The frozen model configuration is unchanged (temperature 0, seed 0,
# 1024 new tokens). Only the SERVED context is longer than the BIRD
# arm's 16384: ten pages of records can be long. The setup receipt
# reports max_page0_bytes; at the pinned universe that is q356
# (card_games, 74 columns, ~23 kB per page), for which a deep
# paginator can still overrun 32768 — that shows up as terminal
# "api_error" in the transcript and in the scorer's `error` count, and
# is never silently folded into no_commitment.
/project/xzhang12/vllm-env/bin/vllm serve Qwen/Qwen2.5-14B-Instruct-AWQ \
  --port 8000 --max-model-len 32768 --gpu-memory-utilization 0.90 \
  > logs/trunc-probe-vllm-$SLURM_JOB_ID.log 2>&1 &
VLLM_PID=$!

for i in $(seq 1 180); do
  if curl -s http://127.0.0.1:8000/v1/models > /dev/null; then break; fi
  if ! kill -0 $VLLM_PID 2>/dev/null; then
    echo "vllm died during startup"; exit 1
  fi
  sleep 5
done
curl -s http://127.0.0.1:8000/v1/models || { echo "server never came up"; kill $VLLM_PID; exit 1; }
echo "server up after ${i}0-ish seconds"

RC=0

# ------------------------------------------- sqlite leg: C0, C1, C2 ----
for COND in C0 C1 C2; do
  $PY $PROBE/run_probe.py \
    --manifest $PROBE/probe_manifest.jsonl \
    --db-root $DBROOT \
    --condition $COND \
    --api http://127.0.0.1:8000/v1 \
    --runs-root $PROBE/runs || RC=1
done

# ------------------------------- engine-diversity leg: postgres, C0 ----
# non-fatal: an engine-diversity hiccup must not zero the sqlite legs
($PY $PROBE/setup_probe.py --pg-load-only \
    --manifest-pg $PROBE/probe_manifest_pg.jsonl \
    --db-root $DBROOT --pgdata $PGDATA_LOCAL \
 && $PY $PROBE/run_probe.py \
    --engine pg \
    --manifest $PROBE/probe_manifest_pg.jsonl \
    --pgdata $PGDATA_LOCAL \
    --condition C0 \
    --api http://127.0.0.1:8000/v1 \
    --runs-root $PROBE/runs) || echo "WARN: pg leg failed, continuing"
rm -rf $PGDATA_LOCAL $XDG_RUNTIME_DIR

kill $VLLM_PID 2>/dev/null

# ------------------------------------------------------------ scoring --
$PY $PROBE/score_probe.py \
  --manifest $PROBE/probe_manifest.jsonl \
  --manifest-pg $PROBE/probe_manifest_pg.jsonl \
  --db-root $DBROOT \
  --runs-root $PROBE/runs \
  --out benchmarks/results-v1/eval-trunc-probe.json || RC=1

$PY $PROBE/setup_probe.py --pg-stop --pgdata $PROBE/runs/pgdata

exit $RC

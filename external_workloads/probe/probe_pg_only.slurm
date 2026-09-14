#!/bin/bash
#SBATCH --job-name=trunc-probe-pg
#SBATCH --partition=bigTiger
#SBATCH --gres=gpu:rtx_6000:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=03:00:00
#SBATCH --output=/project/xzhang12/tgms/logs/trunc-probe-pg-%j.out

# Postgres engine-diversity leg only (D-142): the sqlite legs of job
# 203402 are complete; this reruns the pg leg with the locale fix
# (sbatch environments on the compute nodes carry no LANG/LC_*, and
# initdb refuses invalid locales) and then rescores everything.

set -u
export HF_HOME=/project/xzhang12/hf
export HF_HUB_OFFLINE=1
export LANG=C LC_ALL=C
export XDG_RUNTIME_DIR=/tmp/xdg_$SLURM_JOB_ID
PGDATA_LOCAL=/tmp/probe_pg_$SLURM_JOB_ID
mkdir -p $XDG_RUNTIME_DIR

cd /project/xzhang12/tgms
PY=.venv/bin/python
PROBE=external_workloads/probe
DBROOT=/project/xzhang12/minidev_pkg/minidev/MINIDEV/dev_databases

/project/xzhang12/vllm-env/bin/vllm serve Qwen/Qwen2.5-14B-Instruct-AWQ \
  --port 8000 --max-model-len 32768 --gpu-memory-utilization 0.90 \
  > logs/trunc-probe-pg-vllm-$SLURM_JOB_ID.log 2>&1 &
VLLM_PID=$!
for i in $(seq 1 180); do
  curl -s http://127.0.0.1:8000/v1/models > /dev/null && break
  kill -0 $VLLM_PID 2>/dev/null || { echo "vllm died"; exit 1; }
  sleep 5
done
curl -s http://127.0.0.1:8000/v1/models || { echo "server never came up"; kill -9 $VLLM_PID 2>/dev/null; exit 1; }

$PY $PROBE/setup_probe.py --pg-load-only \
  --manifest-pg $PROBE/probe_manifest_pg.jsonl \
  --db-root $DBROOT --pgdata $PGDATA_LOCAL || { kill -9 $VLLM_PID; exit 1; }
$PY $PROBE/run_probe.py \
  --engine pg \
  --manifest $PROBE/probe_manifest_pg.jsonl \
  --pgdata $PGDATA_LOCAL \
  --condition C0 \
  --api http://127.0.0.1:8000/v1 \
  --runs-root $PROBE/runs; RC=$?
kill $VLLM_PID 2>/dev/null; sleep 5; kill -9 $VLLM_PID 2>/dev/null
rm -rf $PGDATA_LOCAL $XDG_RUNTIME_DIR

$PY $PROBE/score_probe.py \
  --manifest $PROBE/probe_manifest.jsonl \
  --manifest-pg $PROBE/probe_manifest_pg.jsonl \
  --db-root $DBROOT \
  --runs-root $PROBE/runs \
  --out benchmarks/results-v1/eval-trunc-probe.json
exit $RC

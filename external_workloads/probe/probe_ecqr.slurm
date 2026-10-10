#!/bin/bash
#SBATCH --job-name=trunc-probe-ecqr
#SBATCH --partition=bigTiger
#SBATCH --gres=gpu:rtx_6000:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=24:00:00
#SBATCH --output=/project/xzhang12/tgms-lane-D/logs/trunc-probe-ecqr-%j.out

# Truncation probe under ECQR gating (D-170, lane D). Frozen protocol:
# external_workloads/probe/PROBE_ECQR_FREEZE.md (committed before this
# job is submitted). Same model configuration as the probe: Qwen2.5-
# 14B-Instruct-AWQ on vLLM 0.11.0, temperature 0, seed 0, 1024 new
# tokens, served context 32768, k=10, 10 tool calls per question.
#
# Stages: both oracles (no model; a mismatch stops the job before the
# server starts), then E, E-aware and C0-replay, then the scorer.
#
# Submit from the login node (git is absent on compute nodes):
#   cd /project/xzhang12/tgms-lane-D && git pull -q --ff-only && \
#   sbatch --export=ALL,LANE_COMMIT=$(git rev-parse HEAD) \
#     external_workloads/probe/probe_ecqr.slurm
#
# Python: the shared clone's venv no longer exists on iTiger; the
# tgms-arc5 checkout's venv runs this pure-Python code, with the
# probe's pinned sqlglot (30.15.0) in a --target dir beside the clone.

set -u
export HF_HOME=/project/xzhang12/hf
export HF_HUB_OFFLINE=1
export LANG=C LC_ALL=C
export TMPDIR=/tmp/xz_probe_ecqr_$SLURM_JOB_ID
mkdir -p $TMPDIR

LANE=/project/xzhang12/tgms-lane-D
cd $LANE
PY=/project/xzhang12/tgms-arc5/.venv/bin/python
export PYTHONPATH=$LANE:$LANE/.pydeps
PROBE=external_workloads/probe
DBROOT=/project/xzhang12/minidev_pkg/minidev/MINIDEV/dev_databases
RUNS=$PROBE/runs/ecqr
COMMIT=${LANE_COMMIT:-unknown}
echo "commit=$COMMIT node=$(hostname) job=$SLURM_JOB_ID"

$PY $PROBE/run_probe_ecqr.py --print-freeze || exit 1

# ------------------------------------------------ oracles, no model ----
for OR in page_only enumerating; do
  $PY $PROBE/run_probe_ecqr.py --oracle $OR --condition E \
      --db-root $DBROOT --runs-root $RUNS > logs/probe-ecqr-oracle-$OR-$SLURM_JOB_ID.log || exit 1
  $PY $PROBE/score_probe_ecqr.py --oracle-check $OR --db-root $DBROOT \
      --runs-root $RUNS --commit $COMMIT --node $(hostname) \
      --slurm-job $SLURM_JOB_ID \
      --out $TMPDIR/oracle-$OR.json || { echo "oracle $OR failed"; exit 1; }
done

# -------------------------------- model server (probe.slurm pattern) --
/project/xzhang12/vllm-env/bin/vllm serve Qwen/Qwen2.5-14B-Instruct-AWQ \
  --port 8000 --max-model-len 32768 --gpu-memory-utilization 0.90 \
  > logs/trunc-probe-ecqr-vllm-$SLURM_JOB_ID.log 2>&1 &
VLLM_PID=$!

for i in $(seq 1 180); do
  if curl -s http://127.0.0.1:8000/v1/models > /dev/null; then break; fi
  if ! kill -0 $VLLM_PID 2>/dev/null; then
    echo "vllm died during startup"; exit 1
  fi
  sleep 5
done
curl -s http://127.0.0.1:8000/v1/models || { echo "server never came up"; kill -9 $VLLM_PID 2>/dev/null; exit 1; }
echo "server up after ${i}0-ish seconds"

RC=0
for COND in E E-aware C0-replay; do
  $PY $PROBE/run_probe_ecqr.py \
    --db-root $DBROOT --condition $COND \
    --api http://127.0.0.1:8000/v1 --runs-root $RUNS || RC=1
done

kill $VLLM_PID 2>/dev/null; sleep 5; kill -9 $VLLM_PID 2>/dev/null

# ------------------------------------------------------------ scoring --
$PY $PROBE/score_probe_ecqr.py --db-root $DBROOT --runs-root $RUNS \
  --commit $COMMIT --node $(hostname) --slurm-job $SLURM_JOB_ID \
  --out benchmarks/results-v1/eval-trunc-probe-ecqr.json || RC=1

rm -rf $TMPDIR
exit $RC

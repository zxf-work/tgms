#!/bin/bash
#SBATCH --job-name=bird-ecqr
#SBATCH --partition=bigTiger
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=24:00:00
#SBATCH --output=/project/xzhang12/tgms/logs/bird-ecqr-%j.out

# BIRD Mini-Dev 500 agent arm (D-130). Frozen configuration per
# external_workloads/FREEZE.md: Qwen2.5-14B-Instruct-AWQ on vLLM
# 0.11.0, temperature 0, seed 0, <=3 repair rounds, 600 s sqlite
# ceiling. The FREEZE.md prompt hashes must be committed BEFORE this
# job is submitted.

set -u
export HF_HOME=/project/xzhang12/hf
export HF_HUB_OFFLINE=1

cd /project/xzhang12/tgms

/project/xzhang12/vllm-env/bin/vllm serve Qwen/Qwen2.5-14B-Instruct-AWQ \
  --port 8000 --max-model-len 16384 --gpu-memory-utilization 0.90 \
  > logs/bird-vllm-$SLURM_JOB_ID.log 2>&1 &
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

PYTHONPATH=. .venv/bin/python external_workloads/scripts/run_bird_agent.py \
  --frozen external_workloads/bird/bird_500_select_sqlite.jsonl \
  --annotation external_workloads/bird/claim_annotation.jsonl \
  --adjudication external_workloads/bird/adjudication_proposed.jsonl \
  --errata external_workloads/bird/annotation_errata.jsonl \
  --db-root /project/xzhang12/minidev_pkg/minidev/MINIDEV/dev_databases \
  --api http://127.0.0.1:8000/v1 \
  --out external_workloads/bird/agent_run
RC=$?

kill $VLLM_PID 2>/dev/null
exit $RC

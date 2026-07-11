#!/bin/bash
# Sequential OSS matrix on a single-GPU node: run the 7B config, swap the
# vLLM server to 14B-AWQ, run the 14B config. One GPU, one served model at
# a time. Parameterized for any deployment:
#   TGMS_REPO   — tgms checkout (default: repo containing this script)
#   VLLM_ENV    — venv with vllm installed (default: $TGMS_REPO/../vllm/env)
#   HF_HOME     — HuggingFace cache dir (default: ~/.cache/huggingface)
set -e
TGMS_REPO="${TGMS_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
VLLM_ENV="${VLLM_ENV:-$TGMS_REPO/../vllm/env}"
export HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}"
export PATH="$HOME/.local/bin:$PATH"

cd "$TGMS_REPO"
uv run tgms eval run --config configs/matrix-dev-oss.yaml
echo "7B_MATRIX_DONE"

pkill -f "[v]llm.serve" || true; sleep 8
nohup "$VLLM_ENV/bin/vllm" serve Qwen/Qwen2.5-14B-Instruct-AWQ --dtype half \
  --max-model-len 28672 --gpu-memory-utilization 0.92 --port 8000 \
  > "$TGMS_REPO/runs/vllm-14b.log" 2>&1 &
for i in $(seq 1 100); do
  curl -s -m 3 http://localhost:8000/v1/models 2>/dev/null | grep -q AWQ && break
  grep -q "Engine core initialization failed" "$TGMS_REPO/runs/vllm-14b.log" \
    2>/dev/null && { echo "FAILED_14B_SERVER"; exit 1; }
  sleep 15
done
curl -s -m 3 http://localhost:8000/v1/models | grep -q AWQ \
  || { echo "FAILED_14B_TIMEOUT"; exit 1; }
echo "14B_SERVER_UP"

uv run tgms eval run --config configs/matrix-dev-oss-14b.yaml
echo "ALL_DONE"

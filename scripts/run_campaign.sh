#!/bin/bash
# Frozen-test campaign driver (single-GPU node, one served model at a time).
# Usage: run_campaign.sh <phase>
#   guided-ab  — dev A/B for guided JSON decoding (14B)
#   main       — C1/C2: all systems x 3 seeds, CollegeMsg test split (14B)
#   datasets   — email-Eu + synth test splits (14B)
#   models     — C3 scaling: ours on each smaller model, CollegeMsg test
# Env: TGMS_REPO, VLLM_ENV, HF_HOME as in run_oss_matrix.sh.
set -e
TGMS_REPO="${TGMS_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
VLLM_ENV="${VLLM_ENV:-$TGMS_REPO/../vllm/env}"
export HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}"
export PATH="$HOME/.local/bin:$PATH"
cd "$TGMS_REPO"

serve() {  # serve <hf-model-id> <ready-pattern> [extra vllm args...]
  local model="$1" pat="$2"; shift 2
  curl -s -m 10 http://localhost:8000/v1/models 2>/dev/null | grep -q "$pat" \
    && { echo "SERVER_ALREADY_UP $pat"; return 0; }
  [ -x "$VLLM_ENV/bin/vllm" ] || { echo "VLLM_NOT_FOUND $VLLM_ENV"; exit 1; }
  # kill the API parent AND the EngineCore child — the child holds the GPU
  # memory and does not match the parent pattern
  pkill -f "[v]llm.serve" || true; pkill -f "VLLM::EngineCore" || true; sleep 8
  local log="$TGMS_REPO/runs/vllm-$(echo "$pat" | tr '/ ' '--').log"
  nohup "$VLLM_ENV/bin/vllm" serve "$model" --dtype half --port 8000 \
    --gpu-memory-utilization 0.92 "$@" > "$log" 2>&1 &
  for i in $(seq 1 100); do
    curl -s -m 10 http://localhost:8000/v1/models 2>/dev/null | grep -q "$pat" \
      && { echo "SERVER_UP $pat"; return 0; }
    grep -q "Engine core initialization failed" "$log" 2>/dev/null \
      && { echo "SERVER_FAILED $pat"; exit 1; }
    sleep 15
  done
  echo "SERVER_TIMEOUT $pat"; exit 1
}

case "$1" in
guided-ab)
  serve Qwen/Qwen2.5-14B-Instruct-AWQ 14B-Instruct-AWQ --max-model-len 28672
  uv run tgms eval run --config configs/campaign/dev-guided-ab.yaml
  echo "PHASE_DONE guided-ab" ;;
main)
  serve Qwen/Qwen2.5-14B-Instruct-AWQ 14B-Instruct-AWQ --max-model-len 28672
  uv run tgms eval run --config configs/campaign/test-collegemsg-main.yaml
  echo "PHASE_DONE main" ;;
datasets)
  serve Qwen/Qwen2.5-14B-Instruct-AWQ 14B-Instruct-AWQ --max-model-len 28672
  uv run tgms eval run --config configs/campaign/test-emaileu.yaml
  echo "EMAILEU_DONE"
  uv run tgms eval run --config configs/campaign/test-synth.yaml
  echo "PHASE_DONE datasets" ;;
models)
  serve Qwen/Qwen2.5-7B-Instruct Qwen2.5-7B-Instruct --max-model-len 16384
  uv run tgms eval run --config configs/campaign/test-collegemsg-models.yaml
  echo "PHASE_DONE models" ;;
*) echo "usage: $0 guided-ab|main|datasets|models"; exit 2 ;;
esac

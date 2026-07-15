#!/bin/bash
# Frozen-test campaign driver (single-GPU node, one served model at a time).
# Usage: run_campaign.sh <phase>
#   guided-ab  — dev A/B for guided JSON decoding (14B)
#   main       — C1/C2: all systems x 3 seeds, CollegeMsg test split (14B)
#   datasets   — email-Eu + synth test splits (14B)
#   models     — C3 scaling: ours on each smaller model, CollegeMsg test
# Env: TGMS_REPO, VLLM_ENV, HF_HOME as in run_oss_matrix.sh; TGMS_FORCE
# passes a logged --force reason through to `tgms eval run` (needed to
# heal infra-failure rows past the spec §8.3 frozen-test guard).
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
  # sm_75 serving triage: FlexAttention (default) is fast but hits torch's
  # dynamo recompile limit after hours of traffic; --enforce-eager is
  # stable at an unusable ~3 tok/s; XFORMERS crashes in its kernel; and
  # RAISING the recompile ceiling makes guard dispatch CPU-bound within
  # minutes (1024 cached variants evaluated per step -> 1.7 tok/s). So:
  # stock config — small guard table stays fast — with the watchdog
  # bouncing the engine before the hours-scale limit crash, and
  # run_heal() absorbing anything that still lands.
  nohup \
    "$VLLM_ENV/bin/vllm" serve "$model" --dtype half --port 8000 \
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

count_errors() {  # count_errors <out_dir>
  python3 -c "
import json, glob, sys
print(sum(1 for f in glob.glob('$1/results/*.json')
          if 'task_error' in json.load(open(f))))"
}

run_heal() {  # run_heal <config> <out_dir> <model> <pattern> [serve args...]
  # eval passes until no infrastructure-failure rows remain (max 3); the
  # per-row cache recomputes only rows carrying task_error, so each pass
  # costs just the failed remainder. Server restarts between passes.
  local cfg="$1" out="$2" model="$3" pat="$4" n=-1; shift 4
  for pass in 1 2 3; do
    uv run tgms eval run --config "$cfg" \
      ${TGMS_FORCE:+--force "$TGMS_FORCE"} || true
    n=$(count_errors "$out")
    echo "PASS $pass ERRORS $n ($cfg)"
    [ "$n" = "0" ] && return 0
    pkill -f "[v]llm.serve" || true; pkill -f "VLLM::EngineCore" || true
    sleep 8
    serve "$model" "$pat" "$@"
  done
  echo "HEAL_INCOMPLETE $cfg ($n errors remain)"
}

M14=Qwen/Qwen2.5-14B-Instruct-AWQ; P14=14B-Instruct-AWQ
M7=Qwen/Qwen2.5-7B-Instruct;       P7=Qwen2.5-7B-Instruct

case "$1" in
guided-ab)
  serve "$M14" "$P14" --max-model-len 28672
  run_heal configs/campaign/dev-guided-ab.yaml runs/dev-collegemsg-guided \
    "$M14" "$P14" --max-model-len 28672
  echo "PHASE_DONE guided-ab" ;;
main)
  serve "$M14" "$P14" --max-model-len 28672
  run_heal configs/campaign/test-collegemsg-main.yaml runs/test-collegemsg-main \
    "$M14" "$P14" --max-model-len 28672
  echo "PHASE_DONE main" ;;
datasets)
  serve "$M14" "$P14" --max-model-len 28672
  run_heal configs/campaign/test-emaileu.yaml runs/test-emaileu \
    "$M14" "$P14" --max-model-len 28672
  echo "EMAILEU_DONE"
  run_heal configs/campaign/test-synth.yaml runs/test-synth \
    "$M14" "$P14" --max-model-len 28672
  echo "PHASE_DONE datasets" ;;
models)
  serve "$M7" "$P7" --max-model-len 16384
  run_heal configs/campaign/test-collegemsg-models.yaml runs/test-collegemsg-models \
    "$M7" "$P7" --max-model-len 16384
  echo "PHASE_DONE models" ;;
*) echo "usage: $0 guided-ab|main|datasets|models"; exit 2 ;;
esac

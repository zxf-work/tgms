#!/bin/bash
# Bounce the campaign vLLM server every INTERVAL seconds. On sm_75 the
# compiled FlexAttention engine accumulates dynamo recompilations under
# sustained traffic until throughput collapses (observed: ~60 tok/s down
# to 1.1 tok/s over ~30 h). A periodic restart resets it; in-flight eval
# tasks during the bounce fail into task_error rows, which the next
# run_heal pass recomputes.
# Usage: vllm_watchdog.sh [interval-seconds]  (default 12600 = 3.5 h)
INTERVAL="${1:-12600}"
VLLM_ENV="${VLLM_ENV:-/mnt/project/xzhang/vllm/env}"
LOG_DIR="${LOG_DIR:-$(cd "$(dirname "$0")/.." && pwd)/runs}"
while true; do
  sleep "$INTERVAL"
  model=$(curl -s -m 10 http://localhost:8000/v1/models \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['data'][0]['id'])" \
    2>/dev/null) || continue
  [ -z "$model" ] && continue
  case "$model" in *14B*) len=28672 ;; *) len=16384 ;; esac
  echo "$(date -Is) bouncing $model" >> "$LOG_DIR/watchdog.log"
  pkill -f "[v]llm.serve" || true; pkill -f "VLLM::EngineCore" || true
  sleep 10
  nohup \
    "$VLLM_ENV/bin/vllm" serve "$model" --dtype half --port 8000 \
    --gpu-memory-utilization 0.92 --max-model-len "$len" \
    > "$LOG_DIR/vllm-watchdog.log" 2>&1 &
  sleep 240
done

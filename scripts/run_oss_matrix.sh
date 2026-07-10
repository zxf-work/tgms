#!/bin/bash
# Sequential OSS matrix: 7B config, then swap the vLLM server to 14B-AWQ and
# run the 14B config. One GPU, one served model at a time.
set -e
cd /mnt/project/xzhang/tgms/work/tgms
export PATH="$HOME/.local/bin:$PATH"
uv run tgms eval run --config configs/matrix-dev-oss.yaml
echo "7B_MATRIX_DONE"
pkill -f "[v]llm.serve" || true; sleep 8
cd /mnt/project/xzhang/vllm && export HF_HOME=/mnt/project/xzhang/hf
nohup env/bin/vllm serve Qwen/Qwen2.5-14B-Instruct-AWQ --dtype half \
  --max-model-len 16384 --gpu-memory-utilization 0.92 --port 8000 \
  > vllm-14b.log 2>&1 &
for i in $(seq 1 100); do
  curl -s -m 3 http://localhost:8000/v1/models 2>/dev/null | grep -q AWQ && break
  grep -q "Engine core initialization failed" vllm-14b.log 2>/dev/null && \
    { echo "FAILED_14B_SERVER"; exit 1; }
  sleep 15
done
curl -s -m 3 http://localhost:8000/v1/models | grep -q AWQ || { echo "FAILED_14B_TIMEOUT"; exit 1; }
echo "14B_SERVER_UP"
cd /mnt/project/xzhang/tgms/work/tgms
uv run tgms eval run --config configs/matrix-dev-oss-14b.yaml
echo "ALL_DONE"

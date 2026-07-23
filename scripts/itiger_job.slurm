#!/bin/bash
#SBATCH --job-name=tgms
#SBATCH --partition=bigTiger
#SBATCH --cpus-per-task=8
#SBATCH --mem=96G
#SBATCH --output=logs/%x_%j.out
#SBATCH --error=logs/%x_%j.err
# Generic serve+eval job. Pass via --export: MODEL (hf id for vllm),
# CFG (eval config), MAXLEN, TP, and optionally EXTRA_ARGS_FILE (a file whose
# contents are appended to `vllm serve` — used for JSON flags like
# --rope-scaling that cannot survive Slurm --export comma-splitting).
# Resources via sbatch flags (--gres/--time).
set -e
export PATH="$HOME/.local/bin:$PATH"
export UV_CACHE_DIR=/project/$USER/uv-cache
export HF_HOME=/project/$USER/hf
# weights are cached on /project; run offline so a compute node without
# internet (some cannot reach huggingface.co) still serves from cache
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
cd /project/$USER/tgms
mkdir -p logs
PORT=$((8000 + SLURM_JOB_ID % 2000))
PAT=$(echo "$MODEL" | sed 's#.*/##')
EXTRA=""
[ -n "${EXTRA_ARGS_FILE:-}" ] && [ -f "$EXTRA_ARGS_FILE" ] && EXTRA="$(cat "$EXTRA_ARGS_FILE")"
echo "JOB $SLURM_JOB_ID on $(hostname): MODEL=$MODEL PORT=$PORT MAXLEN=${MAXLEN:-16384} TP=${TP:-1} EXTRA=[$EXTRA]"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
source /project/$USER/vllm-env/bin/activate
nohup vllm serve "$MODEL" --port $PORT --gpu-memory-utilization 0.92 \
  --max-model-len ${MAXLEN:-16384} --tensor-parallel-size ${TP:-1} \
  $EXTRA > logs/vllm_${SLURM_JOB_ID}.log 2>&1 &
VPID=$!
deactivate
# big models download (tens of GB) + load shards + inductor-compile on first
# run; wait up to 90 min for the OpenAI endpoint to answer.
up=0
for i in $(seq 1 540); do
  curl -s -m 5 http://localhost:$PORT/v1/models 2>/dev/null | grep -q "$PAT" && { up=1; break; }
  kill -0 $VPID 2>/dev/null || { echo "VLLM_DIED"; break; }
  sleep 10
done
[ "$up" = 1 ] || { echo "SERVE_FAIL"; tail -25 logs/vllm_${SLURM_JOB_ID}.log; exit 1; }
echo "SERVER_UP $PAT"
# per-job private store copy: DuckDB takes an exclusive file lock, so parallel
# jobs cannot share one store. Copy is cheap (~tens of MB) and read-only use.
STORE_SRC=$(grep '^store_path:' "$CFG" | awk '{print $2}')
STORE_JOB="${STORE_SRC}-j${SLURM_JOB_ID}"
cp -r "$STORE_SRC" "$STORE_JOB"
CFGJOB=/tmp/cfg_${SLURM_JOB_ID}.yaml
sed -e "s|localhost:8000|localhost:$PORT|" \
    -e "s|${STORE_SRC}|${STORE_JOB}|g" "$CFG" > "$CFGJOB"
uv run tgms eval run --config "$CFGJOB" --force "iTiger $CFG" || true
n=$(python3 -c "import json,glob,yaml; d=yaml.safe_load(open('$CFGJOB')); print(sum(1 for f in glob.glob(d['out_dir']+'/results/*.json') if 'task_error' in json.load(open(f))))" 2>/dev/null || echo "?")
r=$(python3 -c "import glob,yaml; d=yaml.safe_load(open('$CFGJOB')); print(len(glob.glob(d['out_dir']+'/results/*.json')))" 2>/dev/null || echo "?")
kill $VPID 2>/dev/null || true
rm -rf "$STORE_JOB"
echo "TGMS_JOB_DONE $CFG rows=$r errors=$n"

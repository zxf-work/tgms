#!/bin/bash
#SBATCH --job-name=tgms
#SBATCH --partition=bigTiger
#SBATCH --cpus-per-task=8
#SBATCH --mem=96G
#SBATCH --output=logs/%x_%j.out
#SBATCH --error=logs/%x_%j.err
# Generic serve+eval job. Pass via --export: MODEL (hf id for vllm),
# CFG (eval config), MAXLEN, TP. Resources via sbatch flags (--gres/--time).
set -e
export PATH="$HOME/.local/bin:$PATH"
export UV_CACHE_DIR=/project/$USER/uv-cache
export HF_HOME=/project/$USER/hf
cd /project/$USER/tgms
mkdir -p logs
PORT=$((8000 + SLURM_JOB_ID % 2000))
PAT=$(echo "$MODEL" | sed 's#.*/##')
echo "JOB $SLURM_JOB_ID on $(hostname): MODEL=$MODEL PORT=$PORT MAXLEN=${MAXLEN:-16384} TP=${TP:-1}"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
source /project/$USER/vllm-env/bin/activate
nohup vllm serve "$MODEL" --port $PORT --gpu-memory-utilization 0.92 \
  --max-model-len ${MAXLEN:-16384} --tensor-parallel-size ${TP:-1} \
  ${EXTRA_ARGS:-} > logs/vllm_${SLURM_JOB_ID}.log 2>&1 &
VPID=$!
deactivate
up=0
for i in $(seq 1 180); do
  curl -s -m 5 http://localhost:$PORT/v1/models 2>/dev/null | grep -q "$PAT" && { up=1; break; }
  kill -0 $VPID 2>/dev/null || { echo "VLLM_DIED"; break; }
  sleep 10
done
[ "$up" = 1 ] || { echo "SERVE_FAIL"; tail -20 logs/vllm_${SLURM_JOB_ID}.log; exit 1; }
echo "SERVER_UP $PAT"
sed "s|localhost:8000|localhost:$PORT|" "$CFG" > /tmp/cfg_${SLURM_JOB_ID}.yaml
uv run tgms eval run --config /tmp/cfg_${SLURM_JOB_ID}.yaml --force "iTiger $CFG" || true
n=$(python3 -c "import json,glob,yaml; d=yaml.safe_load(open('/tmp/cfg_${SLURM_JOB_ID}.yaml')); print(sum(1 for f in glob.glob(d['out_dir']+'/results/*.json') if 'task_error' in json.load(open(f))))" 2>/dev/null || echo "?")
kill $VPID 2>/dev/null || true
echo "TGMS_JOB_DONE $CFG errors=$n"

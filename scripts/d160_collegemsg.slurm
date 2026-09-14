#!/bin/bash
#SBATCH --job-name=tgms-d160-collegemsg
#SBATCH --partition=bigTiger
#SBATCH --gres=gpu:rtx_5000:1
#SBATCH --exclude=itiger05
#SBATCH --cpus-per-task=8
#SBATCH --mem=128G
#SBATCH --time=24:00:00
#SBATCH --signal=B:TERM@600
#SBATCH --output=/project/xzhang12/d160-work/logs/%x-%j.out
#SBATCH --error=/project/xzhang12/d160-work/logs/%x-%j.err
#
# D-160 re-measurement (OSDI'27 lane "D-160 re-measurement"; docs/STABILITY.md
# section 9): re-measure the CollegeMsg agent-loop numbers under the
# production claim gate's new default (drops `unsupported` AND
# `unverifiable` -- tgms.eval.plan_faults.GATED_VERDICTS, unchanged by this
# job) across the four arms in configs/matrix-d160-collegemsg-4arm.yaml
# (`ours`, `b5`, `b6e`, `llm_direct`), three seeds, Qwen2.5-14B-AWQ served
# locally by vLLM. Single job, single GPU -- run_matrix's own seed loop
# covers all three seeds sequentially inside one process/store handle, so
# no array or per-seed resubmission is needed.
#
# Repro: sbatch scripts/d160_collegemsg.slurm (run from $TGMS_REPO below).
# Resubmit-after-failure: TGMS_FORCE="<reason>" sbatch ... re-passes
# --force to tgms eval run (the frozen-split guard, spec section 8.3,
# refuses a second run of the same config+split without it; the first run
# needs no --force). The per-task result cache
# (<out_dir>/results/*.json) makes a resubmit after a partial run pick up
# where it left off rather than re-paying every LLM call.
#
# 2026-09-14 postmortem (job 211581): failed after 1:13:54 with exit 1:0,
# no signal, nothing in .out/.err/harness log past SERVER_UP -- the
# signature of a hard cgroup OOM-kill (SIGKILL bypasses this script's own
# TERM trap and cleanup entirely, which is why no RECORDS_COPIED /
# TGMS_JOB_DONE line appears). `sacct`'s batch-step MaxRSS was
# 67,080,148K, ~99.96% of the 64G --mem request that job used -- not a
# disk-quota issue (independently checked: /project/xzhang12 sat at 188G,
# well under quota, both during and after the failure; no
# `*.duckdb.tmp` spill files existed). --mem raised to 128G (itiger07 had
# ~179G free at the time). 94/94 `ours` + 94/94 `b5` + 65/94 `b6e` rows
# for seed 0 were already cached under <out_dir>/results/ when it died;
# a resubmit against the same out_dir resumes from there via the cache
# check above rather than re-running them.
#
# Two deviations from a naive "run the harness on /project" script, both
# precedented by scripts/storm_campaign.slurm / scripts/fault_matrix_campaign.slurm:
#  1. iTiger compute nodes have no `git` binary -- a two-line shim answers
#     `git rev-parse HEAD` / `git status --porcelain` from $TGMS_COMMIT, so
#     the harness's own `_git()` receipt (tgms/eval/harness.py) carries the
#     real commit instead of falling back to "n/a".
#  2. TMPDIR is node-local (/tmp, not /project): NFS4 "sillyrename" breaks
#     tempfile cleanup under load, same reasoning as the storm/crash
#     campaigns' own TMPDIR deviation.
#
# SIGTERM handling: the batch script is the process Slurm signals
# (--signal=B:TERM@600 fires 10 min before the time limit), not the `tgms
# eval run` child directly. A plain `exec` of that child would forward
# signals correctly but would also skip this script's own cleanup (stop
# vLLM, copy partial records to $STAGE/records/ even on a timeout) -- so
# instead the harness runs backgrounded and a TERM trap forwards the
# signal to it and then falls through to the same cleanup path a clean
# exit takes, so a timeout still yields a partial, valid, cached record.

set -uo pipefail  # not -e: cleanup must still run after a non-zero step

JOB_ID="${SLURM_JOB_ID:?must run under sbatch}"

CHECKOUT="${TGMS_REPO:-/project/xzhang12/tgms-d160}"
STAGE="${TGMS_STAGE:-/project/xzhang12/d160-work}"
VLLM_ENV="${VLLM_ENV:-/project/xzhang12/vllm-env}"
VENV_PY="$CHECKOUT/.venv/bin/python"
CFG="${TGMS_CFG:-configs/matrix-d160-collegemsg-4arm.yaml}"
MODEL="${TGMS_MODEL:-Qwen/Qwen2.5-14B-Instruct-AWQ}"
export TGMS_COMMIT="${TGMS_COMMIT:-92c63b9e4fccc6fbffa6d98f0666efd08e483b7a}"
FORCE_FLAG=()
[ -n "${TGMS_FORCE:-}" ] && FORCE_FLAG=(--force "$TGMS_FORCE")

mkdir -p "$STAGE/logs" "$STAGE/records" "$STAGE/node_meta"

# --- git shim (deviation 1) -------------------------------------------- #
SHIM_DIR="/tmp/tgms-d160-shim-${JOB_ID}"
mkdir -p "$SHIM_DIR"
cat > "$SHIM_DIR/git" <<'SHIM'
#!/bin/bash
if [ "$1" = "rev-parse" ]; then
    echo "${TGMS_COMMIT:-unknown}"
    exit 0
fi
if [ "$1" = "status" ]; then
    exit 0
fi
echo "git-shim: unsupported invocation: $*" >&2
exit 1
SHIM
chmod +x "$SHIM_DIR/git"
export PATH="$SHIM_DIR:$PATH"

# --- node-local scratch (deviation 2) ----------------------------------- #
TMPDIR="/tmp/tgms-d160-${JOB_ID}"
mkdir -p "$TMPDIR"
export TMPDIR

export HF_HOME=/project/xzhang12/hf HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export UV_CACHE_DIR=/project/xzhang12/uv-cache

cd "$CHECKOUT"
hostname > "$STAGE/node_meta/job-${JOB_ID}.host"
{ uname -a; nproc; nvidia-smi --query-gpu=name,memory.total --format=csv,noheader; } \
    > "$STAGE/node_meta/job-${JOB_ID}.gpu" 2>&1

echo "RUN_STARTED commit=${TGMS_COMMIT} job=${JOB_ID} model=${MODEL} cfg=${CFG}"

# --- pre-flight: the gate this whole campaign is about must still be the
# code's default -- fail loudly here, not three hours into the run.
"$VENV_PY" -c "
from tgms.eval.plan_faults import GATED_VERDICTS
assert GATED_VERDICTS == ('unsupported', 'unverifiable'), GATED_VERDICTS
print('GATED_VERDICTS ok:', GATED_VERDICTS)
" || { echo "GATE_ASSERTION_FAILED"; exit 2; }

# --- store: must already exist, DuckDB-backed (b6e reads store.duckdb
# directly). Built once, out of band, per benchmarks/frozen-v1/README.md
# plus --backend duckdb (configs/matrix-d160-collegemsg-4arm.yaml's header
# comment has the exact command). Fail fast with a clear message rather
# than a deep KeyError three layers into build_systems.
STORE_PATH=$(grep '^store_path:' "$CFG" | awk '{print $2}')
if [ ! -f "$STORE_PATH/store.duckdb" ]; then
    echo "STORE_MISSING: $STORE_PATH/store.duckdb not found -- build it first"
    exit 2
fi

# b5's vanilla-kuzu cache: rebuild fresh. A predecessor killed mid-query
# leaves a dirty WAL whose recovery can hang the open (build is seconds) --
# same guard scripts/itiger_job.slurm uses.
OUT_DIR=$(grep '^out_dir:' "$CFG" | awk '{print $2}')
rm -rf "${OUT_DIR:?}"/vanilla-kuzu-* 2>/dev/null

# --- serve Qwen2.5-14B-AWQ ------------------------------------------------ #
PORT=8000
nohup "$VLLM_ENV/bin/vllm" serve "$MODEL" --dtype half \
    --max-model-len 28672 --gpu-memory-utilization 0.92 --port "$PORT" \
    > "$STAGE/logs/vllm-${JOB_ID}.log" 2>&1 &
VPID=$!

up=0
for i in $(seq 1 540); do
    curl -s -m 5 "http://localhost:${PORT}/v1/models" 2>/dev/null | grep -q AWQ \
        && { up=1; break; }
    kill -0 "$VPID" 2>/dev/null || { echo "VLLM_DIED"; break; }
    sleep 10
done
if [ "$up" != 1 ]; then
    echo "SERVE_FAIL"
    tail -40 "$STAGE/logs/vllm-${JOB_ID}.log"
    kill "$VPID" 2>/dev/null
    rm -rf "$SHIM_DIR" "$TMPDIR"
    exit 1
fi
echo "SERVER_UP $MODEL port=$PORT"

# bounce the server periodically (defends against the sm_75-style
# dynamo-recompilation throughput decay seen on xzgpu -- iTiger's Ada/
# Hopper GPUs are not known to hit it, but a multi-hour run costs little
# to insure against it). Fixed port 8000 matches vllm_watchdog.sh's own
# assumption.
LOG_DIR="$STAGE/logs" VLLM_ENV="$VLLM_ENV" \
    nohup bash scripts/vllm_watchdog.sh 12600 \
    > "$STAGE/logs/watchdog-${JOB_ID}.log" 2>&1 &
WPID=$!

# --- run the matrix (all three seeds, sequentially, one process) -------- #
cleanup() {
    kill "$WPID" 2>/dev/null
    pkill -f "[v]llm.serve" 2>/dev/null
    pkill -f "[V]LLM::EngineCore" 2>/dev/null
    if [ -d "$OUT_DIR" ]; then
        DEST="$STAGE/records/d160-collegemsg-4arm-${JOB_ID}"
        mkdir -p "$DEST"
        cp -r "$OUT_DIR"/. "$DEST"/
        echo "RECORDS_COPIED $DEST"
    fi
    rm -rf "$SHIM_DIR" "$TMPDIR"
}
HPID=""
term_handler() {
    echo "TERM_RECEIVED job=${JOB_ID}, forwarding to harness pid=${HPID}"
    [ -n "$HPID" ] && kill -TERM "$HPID" 2>/dev/null
}
trap term_handler TERM

"$VENV_PY" -m tgms.cli eval run --config "$CFG" "${FORCE_FLAG[@]}" \
    > "$STAGE/logs/harness-${JOB_ID}.log" 2>&1 &
HPID=$!
wait "$HPID"
EC=$?

N_ROWS=$("$VENV_PY" -c "
import glob, yaml
d = yaml.safe_load(open('$CFG'))
print(len(glob.glob(d['out_dir'] + '/results/*.json')))
" 2>/dev/null || echo "?")
N_ERR=$("$VENV_PY" -c "
import json, glob, yaml
d = yaml.safe_load(open('$CFG'))
print(sum(1 for f in glob.glob(d['out_dir'] + '/results/*.json')
          if 'task_error' in json.load(open(f))))
" 2>/dev/null || echo "?")

cleanup
echo "TGMS_JOB_DONE job=${JOB_ID} cfg=${CFG} rows=${N_ROWS} errors=${N_ERR} exit=${EC}"
exit "$EC"

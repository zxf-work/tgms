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
# Postmortem across three submissions (2026-09-14):
#  - 211581: failed after 1:13:54, exit 1:0, no signal, nothing past
#    SERVER_UP in any log -- a hard cgroup OOM-kill (SIGKILL bypasses this
#    script's own TERM trap, hence no RECORDS_COPIED/TGMS_JOB_DONE line).
#    `sacct`'s batch-step MaxRSS was 67,080,148K, ~99.96% of the 64G
#    --mem this job used.
#  - The coordinator separately found (verified independently: many
#    concurrent Slurm array tasks across other jobs failed within the
#    same second) that the SAME run also spilled ~13GB of DuckDB temp
#    storage onto /project (a pathological b6e statement can drive both
#    the RSS and the temp spill at once -- DuckDB's default
#    `temp_directory` sits next to the database file, which for this
#    campaign is on /project, a quota-limited shared filesystem). Fixed
#    at the source in `tgms/eval/baselines.py`'s `BiTemporalSQL`: every
#    subprocess-side DuckDB connection now binds `temp_directory` under
#    the process's own `TMPDIR` (node-local, see deviation 2 below),
#    `max_temp_directory_size='4096MB'`, and `memory_limit='96GB'` (under
#    this script's 128G --mem), via DuckDB's own `config=` connect-time
#    option -- so a runaway statement fails bounded and node-local
#    instead of spilling onto the shared quota. --mem itself stays 128G
#    (raised from 64G; itiger07 had ~179G free) as a second, independent
#    layer of protection.
#  - 211705 (the previously-permitted resubmit): failed in 0:02:23 before
#    doing any new work -- 211581's own `run_matrix` call had already
#    logged a `runs_log.jsonl` entry for this config+split before it
#    OOM'd, and 211705 omitted `--force`, tripping the frozen-split guard
#    (spec section 8.3) immediately. This submission passes `TGMS_FORCE`
#    for exactly that reason (below).
# 94/94 `ours` + 94/94 `b5` + 65/94 `b6e` rows for seed 0 (0 errors) were
# already cached under <out_dir>/results/ before 211581 died; a resubmit
# against the same out_dir resumes from there via the cache check in
# `run_matrix` rather than re-running them.
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
LOCAL_LOGS="$TMPDIR/logs"
mkdir -p "$TMPDIR" "$LOCAL_LOGS"
export TMPDIR

export HF_HOME=/project/xzhang12/hf HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
# uv-cache is a project-wide shared cache other lanes' `uv sync` calls
# also use, on /project by long-standing convention (see
# scripts/itiger_job.slurm) -- this job does not run `uv sync` at all
# (the .venv is already built), so it never writes there; unset rather
# than point it somewhere this job won't use.
unset UV_CACHE_DIR

# --- fail fast if the shared quota is already tight (2026-09-14 ruling:
# the account tripped the ~200G quota twice from unrelated jobs' spills;
# a job that starts anyway just adds another candidate for the next
# cascade). This is a check on shared account state, not a claim about
# what THIS job will use -- see the DuckDB temp-directory/memory-limit
# bounds below and in tgms/eval/baselines.py for the actual fix.
DU_START_MB=$(du -sm /project/xzhang12 2>/dev/null | awk '{print $1}')
echo "DU_MB_AT_START=${DU_START_MB:-unknown}"
if [ -n "$DU_START_MB" ] && [ "$DU_START_MB" -gt 185000 ]; then
    echo "QUOTA_HEADROOM_TOO_LOW: /project/xzhang12 at ${DU_START_MB}MB > 185000MB, refusing to start"
    exit 2
fi

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
# All bulk logs (vLLM's per-request lines over hours, the harness log)
# live on node-local $TMPDIR, never /project -- only a trimmed tail of
# each is copied out in cleanup(), alongside the actual records.
PORT=8000
nohup "$VLLM_ENV/bin/vllm" serve "$MODEL" --dtype half \
    --max-model-len 28672 --gpu-memory-utilization 0.92 --port "$PORT" \
    > "$LOCAL_LOGS/vllm-${JOB_ID}.log" 2>&1 &
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
    tail -40 "$LOCAL_LOGS/vllm-${JOB_ID}.log"
    kill "$VPID" 2>/dev/null
    mkdir -p "$STAGE/logs"
    cp "$LOCAL_LOGS/vllm-${JOB_ID}.log" "$STAGE/logs/vllm-${JOB_ID}-SERVE_FAIL.log" 2>/dev/null
    rm -rf "$SHIM_DIR" "$TMPDIR"
    exit 1
fi
echo "SERVER_UP $MODEL port=$PORT"

# bounce the server periodically (defends against the sm_75-style
# dynamo-recompilation throughput decay seen on xzgpu -- iTiger's Ada/
# Hopper GPUs are not known to hit it, but a multi-hour run costs little
# to insure against it). Fixed port 8000 matches vllm_watchdog.sh's own
# assumption.
LOG_DIR="$LOCAL_LOGS" VLLM_ENV="$VLLM_ENV" \
    nohup bash scripts/vllm_watchdog.sh 12600 \
    > "$LOCAL_LOGS/watchdog-${JOB_ID}.log" 2>&1 &
WPID=$!

# --- run the matrix (all three seeds, sequentially, one process) -------- #
cleanup() {
    kill "$WPID" 2>/dev/null
    pkill -f "[v]llm.serve" 2>/dev/null
    pkill -f "[V]LLM::EngineCore" 2>/dev/null
    mkdir -p "$STAGE/logs"
    # trimmed tails only -- the full logs stay node-local and die with
    # $TMPDIR; this is enough to diagnose a failure without putting
    # hours of vLLM request logging on the shared quota.
    for f in "$LOCAL_LOGS"/*.log; do
        [ -f "$f" ] || continue
        tail -c 200000 "$f" > "$STAGE/logs/$(basename "$f" .log).tail.log" 2>/dev/null
    done
    if [ -d "$OUT_DIR" ]; then
        DEST="$STAGE/records/d160-collegemsg-4arm-${JOB_ID}"
        mkdir -p "$DEST"
        cp -r "$OUT_DIR"/. "$DEST"/
        echo "RECORDS_COPIED $DEST"
    fi
    du -sm /project/xzhang12 2>/dev/null | awk '{print "DU_MB_AT_CLEANUP="$1}'
    rm -rf "$SHIM_DIR" "$TMPDIR"
}
HPID=""
term_handler() {
    echo "TERM_RECEIVED job=${JOB_ID}, forwarding to harness pid=${HPID}"
    [ -n "$HPID" ] && kill -TERM "$HPID" 2>/dev/null
}
trap term_handler TERM

"$VENV_PY" -m tgms.cli eval run --config "$CFG" "${FORCE_FLAG[@]}" \
    > "$LOCAL_LOGS/harness-${JOB_ID}.log" 2>&1 &
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

#!/bin/bash
#SBATCH --job-name=tgms-overhead-ladder
#SBATCH --partition=bigTiger
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=02:00:00
#SBATCH --output=/project/xzhang12/overhead-ladder-work/logs/%j.out
#SBATCH --error=/project/xzhang12/overhead-ladder-work/logs/%j.err
#
# The five-rung overhead ladder (OSDI'27 lane D4), one store, one run:
# `scripts/bench_overhead_ladder.py --rungs 1,2,3,4,5`. Single task, single
# node -- there is no grid here (one store x one plan set x one seed per
# submission), so this does not need `storm_campaign.slurm`'s array-index
# decomposition; it borrows that script's node-local-TMPDIR and
# git-rev-parse-shim idioms verbatim, for the same reasons (see that
# file's header: NFS4 sillyrename breaks tempfile cleanup on /project, and
# iTiger compute nodes carry no `git` binary).
#
# The one deliberate deviation from `storm_campaign.slurm`'s launch style:
# the python invocation below is `exec`'d as the script's last command, so
# it replaces the shell as PID 1 of the job step and a SIGTERM from
# `scancel` or the 2-hour time limit reaches the interpreter directly
# instead of the wrapping bash (which would otherwise eat the signal and
# need its own trap to forward it). Nothing after `exec` runs, which is
# why cleanup is registered on EXIT before it, not after.
#
# Repro: sbatch scripts/overhead_ladder.slurm (run from the checkout named
# by $TGMS_REPO below; override via --export=ALL,VAR=val,...).
#
# Required:
#   TGMS_STORE   store name under $CHECKOUT/stores/, e.g. "bitcoinotc"
#   TGMS_PLANS   a plans directory or comma-separated plan file list,
#                relative to $CHECKOUT unless absolute
# Optional (defaults shown):
#   TGMS_RUNGS       1,2,3,4,5
#   TGMS_REPS        10
#   TGMS_SEED        0
#   TGMS_TOKENIZER   auto
#   TGMS_COMMIT      4af2181   (stamped as the measured commit; iTiger
#                               compute nodes have no `git` binary, so this
#                               cannot be read from the checkout at run time)

set -euo pipefail

JOB_ID="${SLURM_JOB_ID:?must run under sbatch}"
CHECKOUT="${TGMS_REPO:-/project/xzhang12/tgms-overhead-ladder-v1}"
STAGE="${TGMS_STAGE:-/project/xzhang12/overhead-ladder-work}"
export TGMS_COMMIT="${TGMS_COMMIT:-4af2181}"

STORE="${TGMS_STORE:?set TGMS_STORE (a store name under stores/)}"
PLANS="${TGMS_PLANS:?set TGMS_PLANS (a plans dir or comma-separated file list)}"
RUNGS="${TGMS_RUNGS:-1,2,3,4,5}"
REPS="${TGMS_REPS:-10}"
SEED="${TGMS_SEED:-0}"
TOKENIZER="${TGMS_TOKENIZER:-auto}"

OUT_DIR="$STAGE/runs/overhead-ladder/${JOB_ID}"

# Bounded retry for a transient shared-quota EDQUOT on /project (see
# tgms/tools/retry_io.py's docstring for the same rationale on the Python
# side, which now protects bench_overhead_ladder.py's own --out writes):
# 12 attempts, 10s apart, loud failure after the last rather than the task
# dying at its very first mkdir.
retry_mkdir() {
    local i
    for i in $(seq 1 12); do
        mkdir -p "$@" && return 0
        if [ "$i" -eq 12 ]; then
            echo "FATAL: mkdir -p $* failed after 12 attempts" >&2
            exit 1
        fi
        echo "RETRY_EDQUOT $i"
        sleep 10
    done
}

retry_mkdir "$OUT_DIR" "$STAGE/logs"

# git shim: answers only what bench_overhead_ladder.py's `_sha()` needs
# (`git rev-parse --short=12 HEAD`, `git status --porcelain`) --
# scripts/crash_campaign.slurm's / scripts/storm_campaign.slurm's own idiom.
SHIM_DIR="/tmp/tgms-overhead-ladder-shim-${JOB_ID}"
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

# node-local scratch, never /project (NFS4 sillyrename breaks tempfile
# cleanup -- crash-v1/storm-v1's own finding, carried over verbatim).
#
# No `trap ... EXIT` here, on purpose: the whole point of the `exec` below
# is that this shell's process image *becomes* python rather than forking
# and waiting on it, so bash never reaches its own EXIT to run a trap --
# an EXIT trap registered before an `exec` that succeeds simply never
# fires. Cleanup of $TMPDIR/$SHIM_DIR is therefore left to the node
# (job-scoped /tmp on a correctly configured iTiger prolog/epilog) rather
# than to this script; if iTiger's /tmp is not job-scoped, stale
# `tgms-overhead-ladder-*` directories under /tmp are the visible symptom
# and a periodic sweep, not a trap here, is the fix -- a signal-catching
# wrapper would defeat the reason `exec` was used at all.
TMPDIR="/tmp/tgms-overhead-ladder-${JOB_ID}"
mkdir -p "$TMPDIR"
export TMPDIR

echo "RUN_STARTED commit=${TGMS_COMMIT} job=${JOB_ID} store=${STORE} " \
     "plans=${PLANS} rungs=${RUNGS} reps=${REPS} seed=${SEED}"
hostname > "$OUT_DIR/node.host"
{ uname -a; nproc; } > "$OUT_DIR/node.uname" 2>&1

cd "$CHECKOUT"
VENV_PY="${TGMS_VENV_PY:-/project/xzhang12/tgms/.venv/bin/python}"

# exec: replaces this shell so SIGTERM (scancel, or the 2h wall) reaches
# the interpreter directly. Nothing below this line runs.
exec "$VENV_PY" -u scripts/bench_overhead_ladder.py \
    --store "stores/${STORE}" \
    --plans "$PLANS" \
    --rungs "$RUNGS" \
    --reps "$REPS" \
    --seed "$SEED" \
    --tokenizer "$TOKENIZER" \
    --out "$OUT_DIR/overhead-ladder-${STORE}.json"

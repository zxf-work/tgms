#!/bin/bash
#SBATCH --job-name=tgms-diskfull
#SBATCH --partition=bigTiger
#SBATCH --array=0-19%6
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=02:00:00
#SBATCH --output=/project/xzhang12/diskfull-v1-work/logs/task-%a.out
#SBATCH --error=/project/xzhang12/diskfull-v1-work/logs/task-%a.err
#
# Lane A task A5 — the ENOSPC/short-write injection sweep, >=2,000 trials.
# 20 array tasks x 100 trials = 2,000 trials of scripts/eval_diskfull.py,
# each task a distinct seed (BASE_SEED + SLURM_ARRAY_TASK_ID). Every trial's
# own child subprocess is itself short-lived (a tiny store, at most a few
# dozen writes) — the array width here is about spreading 2,000
# subprocess-per-trial spawns across nodes, not about any one trial being
# expensive.
#
# Repro: sbatch scripts/diskfull_campaign.slurm (run from the checkout named
# by $TGMS_REPO below; override CHECKOUT/STAGE/BASE_SEED via --export if
# regenerating elsewhere).
#
# Same two infrastructure fixes as scripts/crash_campaign.slurm and
# scripts/corruption_campaign.slurm (both documented in
# benchmarks/crash-v1/README.md): node-local TMPDIR (NFS4 sillyrename breaks
# tempfile cleanup under /project) and a `git` shim answering $TGMS_COMMIT
# (no git binary on iTiger compute nodes). A third, specific to this
# harness: TGMS_DISKFULL_AT/TGMS_SHORT_WRITE_AT are the injector's OWN env
# vars, armed only inside each trial's short-lived `--child` subprocess by
# scripts/eval_diskfull.py itself (never by this script) — this script must
# NOT export either one, or every child in the array would arm at the same
# fixed point instead of the harness's own seeded-random one per trial.

set -euo pipefail

TASK_ID="${SLURM_ARRAY_TASK_ID:?must run under sbatch --array}"
JOB_ID="${SLURM_JOB_ID:?must run under sbatch}"

CHECKOUT="${TGMS_REPO:-/project/xzhang12/tgms-diskfull-v1}"
STAGE="${TGMS_STAGE:-/project/xzhang12/diskfull-v1-work}"
BASE_SEED="${TGMS_BASE_SEED:-1000}"
TRIALS_PER_TASK="${TGMS_TRIALS_PER_TASK:-100}"
export TGMS_COMMIT="${TGMS_COMMIT:?set TGMS_COMMIT to the checkout commit sha}"

# Defensive: this job step's own environment must never carry the
# injector's env vars into the parent process (scripts/eval_diskfull.py's
# run_trial explicitly clears and re-sets them per child anyway, but a
# stray inherited value here would still be wrong provenance).
unset TGMS_DISKFULL_AT TGMS_SHORT_WRITE_AT || true

SEED=$((BASE_SEED + TASK_ID))
mkdir -p "$STAGE/records" "$STAGE/logs" "$STAGE/node_meta"

# git shim: answers only what the harness needs (`git rev-parse HEAD`).
SHIM_DIR="/tmp/tgms-diskfull-shim-${JOB_ID}"
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

# node-local scratch — every trial's child spawns under here via
# tempfile.mkdtemp(); NFS4 sillyrename under /project broke cleanup for the
# crash campaign (benchmarks/crash-v1/README.md), same fix reused here.
TMPDIR="/tmp/tgms-diskfull-${JOB_ID}"
mkdir -p "$TMPDIR"
export TMPDIR
cleanup() { rm -rf "$TMPDIR" "$SHIM_DIR"; }
trap cleanup EXIT

echo "RUN_STARTED commit=${TGMS_COMMIT} task=${TASK_ID} seed=${SEED}"
hostname > "$STAGE/node_meta/task-${TASK_ID}.host"
{ uname -a; nproc; } > "$STAGE/node_meta/task-${TASK_ID}.uname" 2>&1

cd "$CHECKOUT"

VENV_PY="/project/xzhang12/tgms/.venv/bin/python"
# A nonzero exit means "at least one trial had problems" (a hang, an
# injected fault that did not surface as a clean typed error, or a failed
# Q1-Q4 gate) — eval_diskfull.py's own convention — an expected,
# campaign-critical outcome, not a script failure, so capture the code
# without `set -e` tearing down before TASK_DONE is logged.
ec=0
"$VENV_PY" scripts/eval_diskfull.py \
    --trials "$TRIALS_PER_TASK" \
    --seed "$SEED" \
    --json "$STAGE/records/task-${TASK_ID}.json" && ec=0 || ec=$?

echo "TASK_DONE task=${TASK_ID} seed=${SEED} exit=${ec}"
exit "$ec"

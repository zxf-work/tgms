#!/bin/bash
#SBATCH --job-name=tgms-corrupt
#SBATCH --partition=bigTiger
#SBATCH --array=0-39%6
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=04:00:00
#SBATCH --output=/project/xzhang12/corruption-v1-work/logs/task-%a.out
#SBATCH --error=/project/xzhang12/corruption-v1-work/logs/task-%a.err
#
# Lane A task A4 — the corruption-detection sweep, >=10,000 trials.
# --verify-mode full (task A9, once A3 landed verify_full/A8 landed the
# CURRENT-missing-open refusal): observation 2 is now the strongest oracle
# available (manifest chain + event log + TCSR + artifact registry, not just
# the engine's own file walk) — this is also eval_corruption.py's own
# default, made explicit here for the record.
# 40 array tasks x 250 trials = 10,000 trials of scripts/eval_corruption.py,
# each task a distinct seed (BASE_SEED + SLURM_ARRAY_TASK_ID) so every trial
# in the campaign is independently reproducible from its recorded (seed,
# trial) pair.
#
# Repro: sbatch scripts/corruption_campaign.slurm (run from the checkout
# named by $TGMS_REPO below; override CHECKOUT/STAGE/BASE_SEED via --export
# if regenerating elsewhere).
#
# Reuses, unmodified, the two infrastructure fixes
# scripts/crash_campaign.slurm's smoke testing already discovered and
# documented in benchmarks/crash-v1/README.md (same class of harness: a
# Python script under scripts/ that spawns short-lived subprocesses against
# tiny stores under a temp dir, and shells out to `git rev-parse HEAD` for
# --json's commit stamp):
#
#  1. TMPDIR is node-local (/tmp/tgms-corrupt-$SLURM_JOB_ID), NOT under
#     /project — /project's NFS4 sillyrename semantics broke
#     tempfile.TemporaryDirectory cleanup for the crash campaign, and
#     scripts/eval_corruption.py's run_trial uses the same
#     tempfile.mkdtemp()-under-TMPDIR pattern eval_durability.py does.
#  2. A two-line `git` shim ahead of it on PATH answers `git rev-parse HEAD`
#     with $TGMS_COMMIT — iTiger compute nodes have no git binary at all,
#     and eval_corruption.py's _git_commit() falls back to `git rev-parse
#     HEAD` only when TGMS_COMMIT is unset, exactly like eval_durability.py.

set -euo pipefail

TASK_ID="${SLURM_ARRAY_TASK_ID:?must run under sbatch --array}"
JOB_ID="${SLURM_JOB_ID:?must run under sbatch}"

CHECKOUT="${TGMS_REPO:-/project/xzhang12/tgms-corruption-v1}"
STAGE="${TGMS_STAGE:-/project/xzhang12/corruption-v1-work}"
BASE_SEED="${TGMS_BASE_SEED:-1000}"
TRIALS_PER_TASK="${TGMS_TRIALS_PER_TASK:-250}"
export TGMS_COMMIT="${TGMS_COMMIT:?set TGMS_COMMIT to the checkout's commit sha}"

SEED=$((BASE_SEED + TASK_ID))
mkdir -p "$STAGE/records" "$STAGE/logs" "$STAGE/node_meta"

# git shim: answers only what the harness needs (`git rev-parse HEAD`).
SHIM_DIR="/tmp/tgms-corrupt-shim-${JOB_ID}"
mkdir -p "$SHIM_DIR"
cat > "$SHIM_DIR/git" <<'SHIM'
#!/bin/bash
if [ "$1" = "rev-parse" ]; then
    echo "${TGMS_COMMIT:-unknown}"
    exit 0
fi
if [ "$1" = "status" ]; then
    exit 0   # "--porcelain" with no output: never report -dirty on a compute node
fi
echo "git-shim: unsupported invocation: $*" >&2
exit 1
SHIM
chmod +x "$SHIM_DIR/git"
export PATH="$SHIM_DIR:$PATH"

# node-local scratch — see deviation (1) above; never /project.
TMPDIR="/tmp/tgms-corrupt-${JOB_ID}"
mkdir -p "$TMPDIR"
export TMPDIR
cleanup() { rm -rf "$TMPDIR" "$SHIM_DIR"; }
trap cleanup EXIT

echo "RUN_STARTED commit=${TGMS_COMMIT} task=${TASK_ID} seed=${SEED}"
hostname > "$STAGE/node_meta/task-${TASK_ID}.host"
{ uname -a; nproc; } > "$STAGE/node_meta/task-${TASK_ID}.uname" 2>&1

cd "$CHECKOUT"

VENV_PY="/project/xzhang12/tgms/.venv/bin/python"
# A nonzero exit means "at least one SILENT trial" — eval_corruption.py's own
# convention (main() returns 1 iff summary.verdict_counts.SILENT > 0) — an
# expected, reportable (and campaign-critical) outcome, not a script
# failure, so capture the code without `set -e` tearing down before
# TASK_DONE is logged.
ec=0
"$VENV_PY" scripts/eval_corruption.py \
    --trials "$TRIALS_PER_TASK" \
    --seed "$SEED" \
    --verify-mode full \
    --json "$STAGE/records/task-${TASK_ID}.json" && ec=0 || ec=$?

echo "TASK_DONE task=${TASK_ID} seed=${SEED} exit=${ec}"
exit "$ec"

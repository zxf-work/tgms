#!/bin/bash
#SBATCH --job-name=tgms-crash
#SBATCH --partition=bigTiger
#SBATCH --array=0-19%6
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=04:00:00
#SBATCH --output=/project/xzhang12/crash-v1-work/logs/task-%a.out
#SBATCH --error=/project/xzhang12/crash-v1-work/logs/task-%a.err
#
# EXP-A1 — the >=10,000-trial seeded crash/recovery campaign (Lane A, task
# A7; OSDI plan). 20 array tasks x 50 trials x 10 boundaries = 10,000 trials
# of scripts/eval_durability.py (P0.7), each task a distinct seed
# (BASE_SEED + SLURM_ARRAY_TASK_ID) so every trial in the campaign is
# independently reproducible from its recorded (seed, boundary, trial).
#
# Repro: sbatch scripts/crash_campaign.slurm  (run from the checkout named
# by $TGMS_REPO below; override CHECKOUT/STAGE/BASE_SEED via --export if
# regenerating elsewhere).
#
# Two deliberate deviations from a naive "run the harness on /project"
# script, both discovered by smoke-testing this exact script before the
# real campaign (see benchmarks/crash-v1/README.md):
#
#  1. TMPDIR is node-local (/tmp/tgms-crash-$SLURM_JOB_ID), NOT under
#     /project. /project is NFS4 (itigercage-ibnet:/project); the harness's
#     native store closes files that are then removed as part of
#     tempfile.TemporaryDirectory cleanup (scripts/eval_durability.py's
#     clean_replay_digest), and NFS's close-after-delete "sillyrename"
#     (.nfsXXXX) semantics turned that into a hard
#     "OSError: Directory not empty" on the very first smoke trial. Every
#     compute node has a large node-local NVMe at /tmp (verified: xfs on
#     /dev/nvme0n1p1, ~14T, itiger07-11) — trial scratch goes there and is
#     rm -rf'd at the end of the task; only the final --json record (small:
#     one record per trial, ~500 trials/task) is written to /project.
#
#  2. Slurm compute nodes on iTiger have no `git` binary at all (confirmed:
#     `which git` fails on itiger07 under srun; login-node-only). The
#     harness (scripts/eval_durability.py, not a file this campaign may
#     modify) unconditionally shells out to `git rev-parse HEAD` when
#     writing --json, with no TGMS_COMMIT escape hatch (that pattern exists
#     in scripts/bench_m5.py's _git_sha, commit 41c4066, but was never
#     ported to eval_durability.py). Rather than patch the harness, this
#     script puts a two-line `git` shim ahead of it on PATH that answers
#     `git rev-parse HEAD` with $TGMS_COMMIT and nothing else — confirmed
#     sufficient by smoke test (the harness's only git invocation).

set -euo pipefail

TASK_ID="${SLURM_ARRAY_TASK_ID:?must run under sbatch --array}"
JOB_ID="${SLURM_JOB_ID:?must run under sbatch}"

CHECKOUT="${TGMS_REPO:-/project/xzhang12/tgms-crash-v1}"
STAGE="${TGMS_STAGE:-/project/xzhang12/crash-v1-work}"
BASE_SEED="${TGMS_BASE_SEED:-1000}"
TRIALS_PER_TASK="${TGMS_TRIALS_PER_TASK:-50}"
export TGMS_COMMIT="${TGMS_COMMIT:-cb0e6af}"
BOUNDARIES="py_torn_wal_append,py_after_wal_fsync,py_before_engine_commit,after_seal,after_close_runs,after_dict,after_manifest,after_current,compact_before_install,gc_mid_delete"

SEED=$((BASE_SEED + TASK_ID))

# Bounded retry for a transient shared-quota EDQUOT on /project (see
# tgms/tools/retry_io.py's docstring for the same rationale on the Python
# side, which now protects eval_durability.py's own --json write): 12
# attempts, 10s apart, loud failure after the last rather than the task
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

retry_mkdir "$STAGE/records" "$STAGE/logs" "$STAGE/node_meta"

# git shim: answers only what the harness needs (`git rev-parse HEAD`).
SHIM_DIR="/tmp/tgms-crash-shim-${JOB_ID}"
mkdir -p "$SHIM_DIR"
cat > "$SHIM_DIR/git" <<'SHIM'
#!/bin/bash
if [ "$1" = "rev-parse" ]; then
    echo "${TGMS_COMMIT:-unknown}"
    exit 0
fi
echo "git-shim: unsupported invocation: $*" >&2
exit 1
SHIM
chmod +x "$SHIM_DIR/git"
export PATH="$SHIM_DIR:$PATH"

# node-local scratch — see deviation (1) above; never /project.
TMPDIR="/tmp/tgms-crash-${JOB_ID}"
mkdir -p "$TMPDIR"
export TMPDIR
cleanup() { rm -rf "$TMPDIR" "$SHIM_DIR"; }
trap cleanup EXIT

echo "RUN_STARTED commit=${TGMS_COMMIT} task=${TASK_ID} seed=${SEED}"
hostname > "$STAGE/node_meta/task-${TASK_ID}.host"
{ uname -a; nproc; } > "$STAGE/node_meta/task-${TASK_ID}.uname" 2>&1

cd "$CHECKOUT"
# No git binary on the compute node (see deviation (2) above) to run
# `git rev-parse HEAD`, so resolve HEAD by reading worktree/.git metadata
# directly: `.git` is a file ("gitdir: <path>") for a `git worktree add`
# checkout, and this worktree was created detached at cb0e6af, so its
# per-worktree HEAD file holds the raw SHA, not a symbolic ref.
if [ -f .git ]; then
    GITDIR=$(sed -n 's/^gitdir: //p' .git)
else
    GITDIR=.git
fi
ACTUAL_HEAD=$(cat "$GITDIR/HEAD" 2>/dev/null || echo unknown)
echo "HEAD=${ACTUAL_HEAD} want=${TGMS_COMMIT} (resolved without git; node has none)"

VENV_PY="/project/xzhang12/tgms/.venv/bin/python"
# A nonzero exit here means "at least one trial had problems" (the
# harness's own convention, scripts/eval_durability.py:main) — an expected,
# reportable outcome, not a script failure; the --json record is written
# unconditionally before that return, so capture the code without `set -e`
# tearing down before TASK_DONE is logged.
ec=0
"$VENV_PY" scripts/eval_durability.py \
    --trials "$TRIALS_PER_TASK" \
    --seed "$SEED" \
    --boundaries "$BOUNDARIES" \
    --json "$STAGE/records/task-${TASK_ID}.json" && ec=0 || ec=$?

echo "TASK_DONE task=${TASK_ID} seed=${SEED} exit=${ec}"
exit "$ec"

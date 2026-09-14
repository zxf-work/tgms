#!/bin/bash
#SBATCH --job-name=tgms-faultmatrix
#SBATCH --partition=bigTiger
#SBATCH --array=0-22%6
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=00:30:00
#SBATCH --output=/project/xzhang12/faults-v1-work/logs/%x-%A_%a.out
#SBATCH --error=/project/xzhang12/faults-v1-work/logs/%x-%A_%a.err
#
# Lane E, task E-campaign — the pre-registered trust-boundary fault matrix
# (docs/design/TRUST_BOUNDARY_FAULT_MATRIX_DESIGN_2026-09-13.md, FROZEN),
# run as a Slurm array of scripts/eval_trust_boundary_matrix.py invocations.
# One array task = one (cell, surface) pair.
#
# PRIMARY ARM ONLY (Lane E task E3, D-160 re-run, 2026-09-15): this table
# holds only the primary-gate rows of the original 40-task layout
# (scripts/eval_trust_boundary_matrix.py's --strict-gate secondary arm is
# NOT run here). D-160 (coordinator ruling; docs/STABILITY.md §9) made
# --strict-gate a no-op alias of the default gate in
# tgms.eval.plan_faults.gate_answer -- both now drop `unsupported` AND
# `unverifiable` -- so re-running the 17 former strict-arm task slots would
# reproduce the primary arm's own output byte-for-byte and waste array
# slots. Cell identities and per-cell N are UNCHANGED from the original
# campaign (docs/design/... §5, coordinator Addendum 1's eight
# "existing-machinery" cells at N=300, everything else at N=100); only the
# now-redundant strict-arm rows are dropped and the array renumbered:
#
# 23 tasks total:
#   - 19 surface-A cells, suite collegemsg                          (idx  0-18)
#   - 4 surface-B cells (F1-1, F1-3a, F1-3b, F1-5), the 29 runnable
#     TGIR-v1 plans on the LDBC-shaped fixture                      (idx 19-22)
#
# Same base seed (0) as the original campaign, so this re-run's mutation/
# task-selection sequence is identical to fault-matrix-campaign-2026-09-13's
# primary arm and the two records differ only in what the new gate lets
# through -- the controlled before/after comparison the README's "Re-run
# under D-160" section reports.
#
# email-eu is OUT OF SCOPE (unchanged from the original campaign): see
# benchmarks/faults-v1/README.md.
#
# Parameterization (this is the only change from the original script beyond
# dropping the strict-arm rows -- cells/N are not touched):
#   TGMS_REPO   checkout to run against. Points at the SEPARATE D-160
#               worktree (/project/xzhang12/tgms-d160, `git worktree add`
#               from 4af2181 with only tgms/eval/harness.py,
#               tgms/agent/reporter.py and tgms/eval/plan_faults.py
#               overwritten by scp from the local D-160 commit -- see
#               benchmarks/faults-v1/README.md's "Re-run under D-160" for
#               the sha256 receipts), NOT the original /project/xzhang12/tgms
#               checkout the 2026-09-13 campaign ran against.
#   TGMS_STAGE  staging directory for records/logs/node_meta. Point this at
#               a fresh directory (e.g. /project/xzhang12/faults-v1-work-d160)
#               so this re-run's raw output never collides with or
#               overwrites the original campaign's staged files.
#   TGMS_TAG    optional free-text run tag, logged on the RUN_STARTED line
#               of each task's driver log (default: "d160") for
#               at-a-glance provenance. NOT folded into the raw record
#               filename: scripts/fault_matrix_campaign_merge.py's
#               parse_records_dir() recovers each record's effective
#               (suite, surface) by stripping a literal "-strict" suffix
#               off the filename's "<cell>-<suite>[-strict]" remainder and
#               comparing what's left to "ldbc-fixture" -- an extra
#               "-<tag>" suffix there would make every surface-B record's
#               suite_part read "ldbc-fixture-<tag>" instead, silently
#               misclassifying it as surface A. Output-directory
#               separation is TGMS_STAGE's job, not the filename's; the
#               merge step renames "-d160" back out of any record where a
#               tagged filename slipped through before merging, and cells
#               kept in benchmarks/faults-v1/ under a separate "d160-raw/"
#               subdirectory (never overwriting the original 2026-09-13
#               raw files, which share these same base names) so the
#               two campaigns' records never collide on disk.
#   TGMS_COMMIT the commit sha stamped into every raw record via the git
#               shim (deviation 2 below) -- the LOCAL commit that made the
#               D-160 gate change (tgms/eval/harness.py,
#               tgms/eval/plan_faults.py), NOT 4af2181, since the worktree's
#               checked-out tree does not match 4af2181 verbatim after the
#               scp overwrite. Set this explicitly at submission time; the
#               script's own default (4af2181) is deliberately wrong for
#               this re-run and exists only so the original invocation
#               convention still works unmodified for anyone re-running the
#               pre-D160 campaign.
#   TGMS_BASE_SEED, TGMS_VENV_PY  unchanged from the original script.
# Submit like:
#   sbatch --job-name=tgms-faultmatrix-d160 \
#     --export=ALL,TGMS_REPO=/project/xzhang12/tgms-d160,\
#TGMS_STAGE=/project/xzhang12/faults-v1-work-d160,TGMS_TAG=d160,\
#TGMS_COMMIT=<local D-160 commit sha> \
#     scripts/fault_matrix_campaign.slurm
#
# Two deviations from a naive "run the harness on /project" script, both
# mirroring scripts/crash_campaign.slurm's own precedent and confirmed by
# smoke-testing this exact script before the original campaign (unchanged
# from the 2026-09-13 run):
#
#  1. TMPDIR and the store this task reads are both node-local
#     (/tmp/tgms-faultmatrix-$SLURM_JOB_ID), NOT under /project. Two reasons:
#     first, /project is NFS4 and this driver's own tempfile.TemporaryDirectory
#     scratch inherits the same NFS "sillyrename" risk crash-v1 hit; second,
#     and specific to this campaign, tgms.store.Store (opened by this
#     driver's resolve_store(), never read_only) takes an OS-level
#     fcntl.flock single-writer lock on <store>/writer.lock for the whole
#     process lifetime (tgms/store.py:29-34,271-310) even though every trial
#     here only reads. With up to 6 array tasks running concurrently
#     (--array=...%6) against one shared $TGMS_REPO/stores/collegemsg or
#     stores/ldbc-fixture, 5 of 6 would hit WriterLockedError immediately.
#     Each task instead cp's the small frozen store (~18 MB collegemsg, ~tens
#     of KB ldbc-fixture) to its own node-local scratch and points --store
#     there, so every task holds its own writer lock on its own private
#     copy. Nothing is written back; the copy is rm -rf'd on exit.
#
#  2. iTiger compute nodes have no `git` binary (confirmed: `which git`
#     fails under srun on itiger07; login-node-only, same finding as
#     crash-v1). scripts/eval_trust_boundary_matrix.py's own `_git()` helper
#     already catches that and falls back to "n/a" for git_commit rather
#     than crashing (unlike eval_durability.py, which crash-v1 had to shim
#     around) -- but "n/a" would throw away real provenance this campaign
#     does have. This script puts the same two-line `git` shim crash-v1
#     uses ahead of it on PATH, answering only `git rev-parse HEAD` from
#     $TGMS_COMMIT, so every raw record still carries the real commit.

set -euo pipefail

TASK_ID="${SLURM_ARRAY_TASK_ID:?must run under sbatch --array}"
JOB_ID="${SLURM_JOB_ID:?must run under sbatch}"

CHECKOUT="${TGMS_REPO:-/project/xzhang12/tgms}"
STAGE="${TGMS_STAGE:-/project/xzhang12/faults-v1-work}"
BASE_SEED="${TGMS_BASE_SEED:-0}"
export TGMS_COMMIT="${TGMS_COMMIT:-4af2181}"
VENV_PY="${TGMS_VENV_PY:-/project/xzhang12/tgms/.venv/bin/python}"
TAG="${TGMS_TAG:-d160}"

# --- the 23-task table (primary arm only): cell, surface, store-name,
# out-suite-label, n. Identical cell identities and per-cell N to the
# original 40-task table's primary-arm rows (idx 0-18, 36-39) -- only the
# now-redundant --strict-gate rows (former idx 19-35) are dropped, per
# D-160 making --strict-gate a no-op alias of the default gate.
CELL=(
  F1-1 F1-2 F1-3a F1-3b F1-4 F1-5 F1-6 F1-6b F1-7 F1-8 F1-9 F1-10 F1-11 F2-1 F2-2 F2-3 F2-4 F2-5 F2-6
  F1-1 F1-3a F1-3b F1-5
)
SURFACE=(
  A A A A A A A A A A A A A A A A A A A
  B B B B
)
STORE_NAME=(
  collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg
  ldbc-fixture ldbc-fixture ldbc-fixture ldbc-fixture
)
# --suite argument passed to the driver. For surface B this is a required
# but semantically unused placeholder (run_cell's surface=="B" branch never
# calls oracle_tasks(suite) -- see the driver's own run_cell docstring); the
# output file is renamed below to the "ldbc-fixture" label the README
# actually uses, and this deviation is documented there, not hidden by
# rewriting the raw record's own "dataset" field.
SUITE_ARG=(
  collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg
  collegemsg collegemsg collegemsg collegemsg
)
OUT_SUITE_LABEL=(
  collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg
  ldbc-fixture ldbc-fixture ldbc-fixture ldbc-fixture
)
N=(
  100 100 100 100 100 100 100 100 300 300 300 300 300 300 100 100 300 100 300
  100 100 100 100
)

if [ "${#CELL[@]}" -ne 23 ] || [ "${#SURFACE[@]}" -ne 23 ] || [ "${#STORE_NAME[@]}" -ne 23 ] \
   || [ "${#SUITE_ARG[@]}" -ne 23 ] || [ "${#OUT_SUITE_LABEL[@]}" -ne 23 ] \
   || [ "${#N[@]}" -ne 23 ]; then
    echo "task table length mismatch -- refusing to run" >&2
    exit 1
fi

CELL_ID="${CELL[$TASK_ID]}"
SURF="${SURFACE[$TASK_ID]}"
STORE="${STORE_NAME[$TASK_ID]}"
SUITE="${SUITE_ARG[$TASK_ID]}"
OUT_SUITE="${OUT_SUITE_LABEL[$TASK_ID]}"
N_TRIALS="${N[$TASK_ID]}"
SEED="$BASE_SEED"

# Bounded retry for a transient shared-quota EDQUOT on /project (see
# tgms/tools/retry_io.py's docstring for the same rationale on the Python
# side): 12 attempts, 10s apart, loud failure after the last rather than
# the task dying at its very first mkdir or losing the final record copy.
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

retry_cp() {
    local i
    for i in $(seq 1 12); do
        cp "$@" && return 0
        if [ "$i" -eq 12 ]; then
            echo "FATAL: cp $* failed after 12 attempts" >&2
            exit 1
        fi
        echo "RETRY_EDQUOT $i"
        sleep 10
    done
}

retry_mkdir "$STAGE/records" "$STAGE/logs" "$STAGE/node_meta"

echo "RUN_STARTED commit=${TGMS_COMMIT} tag=${TAG} task=${TASK_ID} seed=${SEED}"
hostname > "$STAGE/node_meta/task-${TASK_ID}.host"
{ uname -a; nproc; } > "$STAGE/node_meta/task-${TASK_ID}.uname" 2>&1

# git shim -- see deviation (2) above.
SHIM_DIR="/tmp/tgms-faultmatrix-shim-${JOB_ID}-${TASK_ID}"
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

# node-local scratch + node-local store copy -- see deviation (1) above.
TMPDIR="/tmp/tgms-faultmatrix-${JOB_ID}-${TASK_ID}"
mkdir -p "$TMPDIR/stores"
export TMPDIR
cleanup() { rm -rf "$TMPDIR" "$SHIM_DIR"; }
trap cleanup EXIT

cp -r "$CHECKOUT/stores/$STORE" "$TMPDIR/stores/$STORE"
LOCAL_STORE="$TMPDIR/stores/$STORE"

cd "$CHECKOUT"

OUT_DIR="$TMPDIR/out"
mkdir -p "$OUT_DIR"

ec=0
"$VENV_PY" scripts/eval_trust_boundary_matrix.py \
    --suite "$SUITE" \
    --surface "$SURF" \
    --cells "$CELL_ID" \
    --n "$N_TRIALS" \
    --seed "$SEED" \
    --store "$LOCAL_STORE" \
    --out "$OUT_DIR" \
    > "$STAGE/logs/task-${TASK_ID}.driver.log" 2>&1 && ec=0 || ec=$?

cat "$STAGE/logs/task-${TASK_ID}.driver.log"

# The driver names its output "<cell>-<suite arg>.json"; rename to the
# "<cell>-<out-suite-label>.json" convention benchmarks/faults-v1/README.md
# documents (collegemsg for surface A, ldbc-fixture for surface B) and copy
# to the shared staging area. Deliberately NOT tagged: fault_matrix_
# campaign_merge.py's parse_records_dir() recovers each record's effective
# surface by comparing this suffix to the literal string "ldbc-fixture"
# (see the TGMS_TAG comment above) -- a "-d160"-suffixed filename here
# would silently misclassify every surface-B record as surface A at merge
# time. Output separation from the original campaign's identically-named
# records is TGMS_STAGE's job (a distinct staging directory per run), not
# the filename's.
SRC_NAME="${CELL_ID}-${SUITE}.json"
DST_NAME="${CELL_ID}-${OUT_SUITE}.json"
if [ -f "$OUT_DIR/$SRC_NAME" ]; then
    retry_cp "$OUT_DIR/$SRC_NAME" "$STAGE/records/task-${TASK_ID}--${DST_NAME}"
else
    echo "MISSING_OUTPUT expected $OUT_DIR/$SRC_NAME" >&2
    ec=1
fi

echo "TASK_DONE task=${TASK_ID} cell=${CELL_ID} surface=${SURF} n=${N_TRIALS} exit=${ec}"
exit "$ec"

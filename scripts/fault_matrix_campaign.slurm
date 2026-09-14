#!/bin/bash
#SBATCH --job-name=tgms-faultmatrix
#SBATCH --partition=bigTiger
#SBATCH --array=0-39%6
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=00:30:00
#SBATCH --output=/project/xzhang12/faults-v1-work/logs/task-%a.out
#SBATCH --error=/project/xzhang12/faults-v1-work/logs/task-%a.err
#
# Lane E, task E-campaign — the pre-registered trust-boundary fault matrix
# (docs/design/TRUST_BOUNDARY_FAULT_MATRIX_DESIGN_2026-09-13.md, FROZEN),
# model-free primary arm plus the coordinator's --strict-gate secondary arm
# (Addendum 1), run as a Slurm array of scripts/eval_trust_boundary_matrix.py
# invocations. One array task = one (cell, surface, gate) triple.
#
# 40 tasks total:
#   - 19 surface-A cells x primary gate, suite collegemsg          (idx  0-18)
#   - 17 surface-A cells x --strict-gate, suite collegemsg         (idx 19-35)
#     (F1-8 and F2-5 are excluded from the strict arm: the driver's own
#     docstring says --strict-gate is a no-op for "cells with no answer/gate
#     at all (F1-8, F2-5, surface B)" -- running them again would just
#     reproduce the primary-arm file byte-for-byte and waste an array slot.)
#   - 4 surface-B cells (F1-1, F1-3a, F1-3b, F1-5) x primary gate only,
#     against the 29 runnable TGIR-v1 plans on the LDBC-shaped fixture
#     (idx 36-39). --strict-gate is likewise a no-op here (same docstring),
#     so surface B is never duplicated into a strict variant.
#
# N per cell follows the design's §5 pre-registration: 300 for the eight
# "existing-machinery" cells {F1-7, F1-8, F1-9, F1-10, F1-11, F2-1, F2-4,
# F2-6} (coordinator Addendum 1 names these eight explicitly), 100 for every
# other cell. Same base seed (0) for every task: each invocation reseeds its
# own random.Random(seed), so the primary and strict arms of the same cell
# draw the identical mutation/task-selection sequence and differ only in
# which claims the gate drops before classification -- the controlled
# comparison Addendum 1 asks for.
#
# email-eu is OUT OF SCOPE for this campaign: benchmarks/frozen-v1/ has no
# raw event log for email-eu (only collegemsg.eventlog.jsonl is checked in;
# tgms/data/loaders.py's "email-eu" entry downloads from SNAP and a fresh
# `tgms ingest` reassigns transaction times per D-023, so it cannot
# reproduce the frozen suite's gold). See benchmarks/faults-v1/README.md.
#
# Two deviations from a naive "run the harness on /project" script, both
# mirroring scripts/crash_campaign.slurm's own precedent and confirmed by
# smoke-testing this exact script before the real campaign:
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
#     (--array=...%6) against one shared /project/xzhang12/tgms/stores/
#     collegemsg or stores/ldbc-fixture, 5 of 6 would hit WriterLockedError
#     immediately. Each task instead cp's the small frozen store (~18 MB
#     collegemsg, ~tens of KB ldbc-fixture) to its own node-local scratch
#     and points --store there, so every task holds its own writer lock on
#     its own private copy. Nothing is written back; the copy is rm -rf'd on
#     exit.
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

# --- the 40-task table: cell, surface, store-name, out-suite-label, strict, n
CELL=(
  F1-1 F1-2 F1-3a F1-3b F1-4 F1-5 F1-6 F1-6b F1-7 F1-8 F1-9 F1-10 F1-11 F2-1 F2-2 F2-3 F2-4 F2-5 F2-6
  F1-1 F1-2 F1-3a F1-3b F1-4 F1-5 F1-6 F1-6b F1-7 F1-9 F1-10 F1-11 F2-1 F2-2 F2-3 F2-4 F2-6
  F1-1 F1-3a F1-3b F1-5
)
SURFACE=(
  A A A A A A A A A A A A A A A A A A A
  A A A A A A A A A A A A A A A A A
  B B B B
)
STORE_NAME=(
  collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg
  collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg
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
  collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg
  collegemsg collegemsg collegemsg collegemsg
)
OUT_SUITE_LABEL=(
  collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg
  collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg collegemsg
  ldbc-fixture ldbc-fixture ldbc-fixture ldbc-fixture
)
STRICT=(
  0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
  1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1
  0 0 0 0
)
N=(
  100 100 100 100 100 100 100 100 300 300 300 300 300 300 100 100 300 100 300
  100 100 100 100 100 100 100 100 300 300 300 300 300 100 100 300 300
  100 100 100 100
)

if [ "${#CELL[@]}" -ne 40 ] || [ "${#SURFACE[@]}" -ne 40 ] || [ "${#STORE_NAME[@]}" -ne 40 ] \
   || [ "${#SUITE_ARG[@]}" -ne 40 ] || [ "${#OUT_SUITE_LABEL[@]}" -ne 40 ] \
   || [ "${#STRICT[@]}" -ne 40 ] || [ "${#N[@]}" -ne 40 ]; then
    echo "task table length mismatch -- refusing to run" >&2
    exit 1
fi

CELL_ID="${CELL[$TASK_ID]}"
SURF="${SURFACE[$TASK_ID]}"
STORE="${STORE_NAME[$TASK_ID]}"
SUITE="${SUITE_ARG[$TASK_ID]}"
OUT_SUITE="${OUT_SUITE_LABEL[$TASK_ID]}"
IS_STRICT="${STRICT[$TASK_ID]}"
N_TRIALS="${N[$TASK_ID]}"
SEED="$BASE_SEED"

mkdir -p "$STAGE/records" "$STAGE/logs" "$STAGE/node_meta"

echo "RUN_STARTED commit=${TGMS_COMMIT} task=${TASK_ID} seed=${SEED}"
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

STRICT_FLAG=()
if [ "$IS_STRICT" = "1" ]; then
    STRICT_FLAG=(--strict-gate)
fi

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
    "${STRICT_FLAG[@]}" \
    > "$STAGE/logs/task-${TASK_ID}.driver.log" 2>&1 && ec=0 || ec=$?

cat "$STAGE/logs/task-${TASK_ID}.driver.log"

# The driver names its output "<cell>-<suite arg>[-strict].json"; rename to
# the "<cell>-<out-suite-label>[-strict].json" convention
# benchmarks/faults-v1/README.md documents (collegemsg for surface A,
# ldbc-fixture for surface B) and copy to the shared staging area.
SUFFIX=""
if [ "$IS_STRICT" = "1" ]; then
    SUFFIX="-strict"
fi
SRC_NAME="${CELL_ID}-${SUITE}${SUFFIX}.json"
DST_NAME="${CELL_ID}-${OUT_SUITE}${SUFFIX}.json"
if [ -f "$OUT_DIR/$SRC_NAME" ]; then
    cp "$OUT_DIR/$SRC_NAME" "$STAGE/records/task-${TASK_ID}--${DST_NAME}"
else
    echo "MISSING_OUTPUT expected $OUT_DIR/$SRC_NAME" >&2
    ec=1
fi

echo "TASK_DONE task=${TASK_ID} cell=${CELL_ID} surface=${SURF} strict=${IS_STRICT} n=${N_TRIALS} exit=${ec}"
exit "$ec"

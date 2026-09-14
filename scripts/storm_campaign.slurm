#!/bin/bash
#SBATCH --job-name=tgms-storm
#SBATCH --partition=bigTiger
#SBATCH --array=0-399%6
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=04:00:00
#SBATCH --output=/project/xzhang12/storm-v1-work/logs/task-%a.out
#SBATCH --error=/project/xzhang12/storm-v1-work/logs/task-%a.err
#
# The Correction Storm campaign (Lane C, tasks C2/C5;
# docs/design/CORRECTION_STORM_DESIGN_2026-09-13.md §7-8). One array task is
# one (store x mix x age x n_artifacts x seed) cell, run end to end by
# `scripts/bench_correction_storm.py` -- every arm in `tgms.eval.storm.ARMS`
# scored in the SAME task (one oracle pass per batch already prices every
# arm at once, `storm.py`'s own module docstring; splitting arms across
# array tasks would just repeat that identical oracle work `len(arms)`
# times for nothing). This is this campaign's own deliberate reading of
# §7's "array over 6 arms x stores x 5 seeds" grid: "arm" is not an array
# dimension here, because it never needs to be one.
#
# Repro: sbatch scripts/storm_campaign.slurm (run from the checkout named by
# $TGMS_REPO below; override via --export, the same convention
# scripts/crash_campaign.slurm uses).
#
# Grid (env-overridable; changing any of these changes the total task
# count, so --array above must be recomputed by hand -- the same static-
# array constraint scripts/crash_campaign.slurm's own header documents):
#   TGMS_STORES            (default: synth-iv-60k,collegemsg)             2
#   TGMS_MIXES             (default: c1,c2,c3,c4)                         4
#   TGMS_AGES              (default: none,recent,hours,days,deep)         5
#   TGMS_N_ARTIFACTS_LIST  (default: 1000)                                1
#   TGMS_N_SEEDS           (default: 5, seeds 0..N-1)                     5
#   TGMS_MEASURE_TTF_LIST  (default: sum,end-to-end)                      2
#                                                total = 2*4*5*1*5*2 = 400
#
# storm-v1 C6 freeze note (benchmarks/storm-v1/campaign.yaml): the frozen
# cell grid scores "both TTF modes" per cell, which the original single-
# mode version of this script (hardcoded `--measure-ttf sum`) could not
# produce. TGMS_MEASURE_TTF_LIST is the additive fix -- an outermost index
# dimension, slower-varying than store, so a small --array slice still
# sweeps one ttf mode's full cell grid before the other begins. This is
# the SMALLEST change that makes the script produce the freeze's grid;
# `scripts/storm_campaign_merge.py` gained the matching `--ttf-modes` flag.
#
# "none" in TGMS_AGES means no --age flag at all (the plain --mix cell,
# C2's rate axis without the age axis layered on). 10^5 artifacts is
# deliberately NOT in the default TGMS_N_ARTIFACTS_LIST -- design memo §3's
# R-18 trip point -- add it only with TGMS_ALLOW_R18_TRIP=1, which passes
# --allow-r18-trip through, and expect those cells to actually trip R-18
# per the design's own prediction table (§3, §8).
#
# Store availability: only synth-iv-60k and collegemsg (interval-vt and
# event-stream continuity stores, §7) are assumed already built under
# stores/ in this checkout. sx-mathoverflow/sx-superuser/wiki-talk/
# synth-1m/synth-10m need to be built server-side first (design memo §7:
# "New stores are built server-side with a build receipt, never on a
# laptop") -- add them to TGMS_STORES only once they exist on $CHECKOUT.
#
# Two deviations carried over verbatim from scripts/crash_campaign.slurm,
# for the same reasons (see that file's own header): (1) TMPDIR is
# node-local, never /project (NFS4 sillyrename breaks tempfile cleanup);
# (2) no `git` binary on iTiger compute nodes, so a two-line shim answers
# `git rev-parse HEAD` from $TGMS_COMMIT.

set -euo pipefail

TASK_ID="${SLURM_ARRAY_TASK_ID:?must run under sbatch --array}"
JOB_ID="${SLURM_JOB_ID:?must run under sbatch}"

CHECKOUT="${TGMS_REPO:-/project/xzhang12/tgms-storm-v1}"
STAGE="${TGMS_STAGE:-/project/xzhang12/storm-v1-work}"
BASE_SEED="${TGMS_BASE_SEED:-0}"
export TGMS_COMMIT="${TGMS_COMMIT:-8962b78}"
# additive fix: the checkout under test has its own rebuilt engine/venv
# (storm-v1 freeze anchor commit differs from the shared /project/xzhang12/
# tgms checkout other lanes build against) -- default to $CHECKOUT's own
# .venv rather than a hardcoded sibling checkout's, still overridable.
VENV_PY="${TGMS_VENV_PY:-$CHECKOUT/.venv/bin/python}"

IFS=',' read -r -a STORES <<< "${TGMS_STORES:-synth-iv-60k,collegemsg}"
IFS=',' read -r -a MIXES <<< "${TGMS_MIXES:-c1,c2,c3,c4}"
IFS=',' read -r -a AGES <<< "${TGMS_AGES:-none,recent,hours,days,deep}"
IFS=',' read -r -a N_ARTIFACTS_LIST <<< "${TGMS_N_ARTIFACTS_LIST:-1000}"
N_SEEDS="${TGMS_N_SEEDS:-5}"
IFS=',' read -r -a TTF_MODES <<< "${TGMS_MEASURE_TTF_LIST:-sum,end-to-end}"
BATCHES_PER_TASK="${TGMS_BATCHES_PER_TASK:-2000}"
ALLOW_R18_TRIP_FLAG=""
[ "${TGMS_ALLOW_R18_TRIP:-0}" = "1" ] && ALLOW_R18_TRIP_FLAG="--allow-r18-trip"

n_stores=${#STORES[@]}
n_mixes=${#MIXES[@]}
n_ages=${#AGES[@]}
n_nlist=${#N_ARTIFACTS_LIST[@]}
n_ttf=${#TTF_MODES[@]}
total=$((n_stores * n_mixes * n_ages * n_nlist * N_SEEDS * n_ttf))
if [ "$TASK_ID" -ge "$total" ]; then
    echo "TASK_ID=$TASK_ID out of range for grid size $total " \
         "(stores=$n_stores mixes=$n_mixes ages=$n_ages n_artifacts=$n_nlist seeds=$N_SEEDS ttf_modes=$n_ttf)" >&2
    exit 2
fi

# in-script index decomposition (§7's own phrase), seed varying fastest so
# a small array slice (e.g. --array=0-4) sweeps one cell's seeds first;
# ttf mode is the new OUTERMOST (slowest-varying) axis, so --array=0-199
# sweeps the whole store x mix x age x n_artifacts x seed grid at "sum"
# before "end-to-end" begins at task 200.
rem=$TASK_ID
seed_idx=$((rem % N_SEEDS)); rem=$((rem / N_SEEDS))
n_idx=$((rem % n_nlist)); rem=$((rem / n_nlist))
age_idx=$((rem % n_ages)); rem=$((rem / n_ages))
mix_idx=$((rem % n_mixes)); rem=$((rem / n_mixes))
store_idx=$((rem % n_stores)); rem=$((rem / n_stores))
ttf_idx=$((rem % n_ttf))

STORE="${STORES[$store_idx]}"
MIX="${MIXES[$mix_idx]}"
AGE="${AGES[$age_idx]}"
N_ARTIFACTS="${N_ARTIFACTS_LIST[$n_idx]}"
SEED=$((BASE_SEED + seed_idx))
MEASURE_TTF="${TTF_MODES[$ttf_idx]}"

AGE_FLAG=""
[ "$AGE" != "none" ] && AGE_FLAG="--age $AGE"

mkdir -p "$STAGE/records/task-${TASK_ID}" "$STAGE/logs" "$STAGE/node_meta"

# git shim: answers only what bench_correction_storm.py needs
# (`git rev-parse HEAD`) -- scripts/crash_campaign.slurm's own idiom.
SHIM_DIR="/tmp/tgms-storm-shim-${JOB_ID}"
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

# node-local scratch -- see header deviation (1); never /project.
TMPDIR="/tmp/tgms-storm-${JOB_ID}"
mkdir -p "$TMPDIR"
export TMPDIR
cleanup() { rm -rf "$TMPDIR" "$SHIM_DIR"; }
trap cleanup EXIT

echo "RUN_STARTED commit=${TGMS_COMMIT} task=${TASK_ID} store=${STORE} mix=${MIX} " \
     "age=${AGE} n_artifacts=${N_ARTIFACTS} seed=${SEED} measure_ttf=${MEASURE_TTF}"
hostname > "$STAGE/node_meta/task-${TASK_ID}.host"
{ uname -a; nproc; } > "$STAGE/node_meta/task-${TASK_ID}.uname" 2>&1

cd "$CHECKOUT"
# addendum stamps (additive; empty by default -- storm-v1 addendum-1's own
# text requires every record to carry these in its own "config").
ADDENDUM_ID_FLAG=""
[ -n "${TGMS_ADDENDUM_ID:-}" ] && ADDENDUM_ID_FLAG="--addendum-id $TGMS_ADDENDUM_ID"
FREEZE_SHA_FLAG=""
[ -n "${TGMS_FREEZE_SHA256:-}" ] && FREEZE_SHA_FLAG="--freeze-sha256 $TGMS_FREEZE_SHA256"
ec=0
"$VENV_PY" scripts/bench_correction_storm.py \
    --store "$STORE" \
    --n-artifacts "$N_ARTIFACTS" \
    --batches "$BATCHES_PER_TASK" \
    --seed "$SEED" \
    --mix "$MIX" \
    $AGE_FLAG \
    --measure-ttf "$MEASURE_TTF" \
    $ALLOW_R18_TRIP_FLAG \
    $ADDENDUM_ID_FLAG \
    $FREEZE_SHA_FLAG \
    --out "$STAGE/records/task-${TASK_ID}" \
    && ec=0 || ec=$?

echo "TASK_DONE task=${TASK_ID} store=${STORE} mix=${MIX} age=${AGE} " \
     "n_artifacts=${N_ARTIFACTS} seed=${SEED} measure_ttf=${MEASURE_TTF} exit=${ec}"
exit "$ec"

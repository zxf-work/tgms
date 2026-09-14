#!/bin/bash
#SBATCH --job-name=tgms-storm-dag
#SBATCH --partition=bigTiger
#SBATCH --array=0-39%6
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=02:00:00
#SBATCH --output=/project/xzhang12/storm-v1-work/logs/dag-task-%a.out
#SBATCH --error=/project/xzhang12/storm-v1-work/logs/dag-task-%a.err
#
# The storm-v1 C6 freeze's dag_phase grid (benchmarks/storm-v1/campaign.yaml):
# 5 shapes x depth{4,16} x fanout{2,10} x seeds{0,1} = 40 cells, gate G-S2
# (false-safe == 0, every cell). This is a SEPARATE array script, additive
# to `scripts/storm_campaign.slurm`, not an edit to it: that script's own
# grid is (store x mix x age x n_artifacts x seed x ttf_mode) with no shape/
# depth/fanout axis at all, and the dag_phase cells are orthogonal to that
# grid (one store only, no mix/age/n_artifacts dimension) -- folding a
# second, differently-shaped grid into the same index decomposition would
# not be the smallest change. `scripts/bench_correction_storm.py`'s own
# `--dag-shape/--dag-depth/--dag-fanout/--dag-cascade-k` flags run the DAG/
# cascade phase "after the correction-storm batch loop" (that script's own
# docstring) over the *same* store/registry, reported under the manifest's
# own "dag" key, independent of "summary" -- so each task here still runs a
# small underlying storm batch loop, deliberately minimal (default
# --n-artifacts/--batches, no --mix/--age) since the freeze's dag_phase spec
# names only `store: synth-iv-60k`, not a mix/age/n_artifacts cell; that
# batch loop's own arm summary is recorded for provenance but is not what
# G-S2 gates (G-S2 reads dag.cascade.false_safe_count only, per
# `scripts/storm_campaign_merge.py`'s `per_cell_table`/`TGMS_ARMS`).
#
# cascade_k = depth (frozen: campaign.yaml's dag_phase.cascade_k), so this
# script always sets --dag-cascade-k to the same value as --dag-depth --
# never a truncated k, per the freeze's own choice to test quiescence, not
# a deliberately-incomplete cascade (storm_dag.py's own docstring explains
# the difference: a k at least as deep as the DAG's real depth from the
# corrected root should reach false-safe=0).
#
# Grid (env-overridable; changing any of these changes the total task
# count, so --array above must be recomputed by hand):
#   TGMS_DAG_STORE    (default: synth-iv-60k)                  1
#   TGMS_DAG_SHAPES   (default: chain,tree,diamond,layered,power-law)  5
#   TGMS_DAG_DEPTHS   (default: 4,16)                           2
#   TGMS_DAG_FANOUTS  (default: 2,10)                           2
#   TGMS_DAG_SEEDS    (default: 0,1)                            2
#                                                total = 5*2*2*2 = 40
#
# Same two deviations as scripts/storm_campaign.slurm / crash_campaign.slurm
# (see those files' own headers): (1) TMPDIR is node-local, never /project;
# (2) no `git` binary on iTiger compute nodes, so a two-line shim answers
# `git rev-parse HEAD` from $TGMS_COMMIT.

set -euo pipefail

TASK_ID="${SLURM_ARRAY_TASK_ID:?must run under sbatch --array}"
JOB_ID="${SLURM_JOB_ID:?must run under sbatch}"

CHECKOUT="${TGMS_REPO:-/project/xzhang12/tgms-storm}"
STAGE="${TGMS_STAGE:-/project/xzhang12/storm-v1-work}"
export TGMS_COMMIT="${TGMS_COMMIT:-8962b78}"
VENV_PY="${TGMS_VENV_PY:-$CHECKOUT/.venv/bin/python}"

DAG_STORE="${TGMS_DAG_STORE:-synth-iv-60k}"
IFS=',' read -r -a SHAPES <<< "${TGMS_DAG_SHAPES:-chain,tree,diamond,layered,power-law}"
IFS=',' read -r -a DEPTHS <<< "${TGMS_DAG_DEPTHS:-4,16}"
IFS=',' read -r -a FANOUTS <<< "${TGMS_DAG_FANOUTS:-2,10}"
IFS=',' read -r -a SEEDS <<< "${TGMS_DAG_SEEDS:-0,1}"
DAG_N_ARTIFACTS="${TGMS_DAG_N_ARTIFACTS:-100}"
DAG_BATCHES="${TGMS_DAG_BATCHES:-20}"

n_shapes=${#SHAPES[@]}
n_depths=${#DEPTHS[@]}
n_fanouts=${#FANOUTS[@]}
n_seeds=${#SEEDS[@]}
total=$((n_shapes * n_depths * n_fanouts * n_seeds))
if [ "$TASK_ID" -ge "$total" ]; then
    echo "TASK_ID=$TASK_ID out of range for grid size $total " \
         "(shapes=$n_shapes depths=$n_depths fanouts=$n_fanouts seeds=$n_seeds)" >&2
    exit 2
fi

# in-script index decomposition, seed varying fastest (same convention as
# storm_campaign.slurm) so --array=0-1 sweeps one (shape,depth,fanout)'s
# two seeds first.
rem=$TASK_ID
seed_idx=$((rem % n_seeds)); rem=$((rem / n_seeds))
fanout_idx=$((rem % n_fanouts)); rem=$((rem / n_fanouts))
depth_idx=$((rem % n_depths)); rem=$((rem / n_depths))
shape_idx=$((rem % n_shapes))

SHAPE="${SHAPES[$shape_idx]}"
DEPTH="${DEPTHS[$depth_idx]}"
FANOUT="${FANOUTS[$fanout_idx]}"
SEED="${SEEDS[$seed_idx]}"
CASCADE_K="$DEPTH"

mkdir -p "$STAGE/dag-records/task-${TASK_ID}" "$STAGE/logs" "$STAGE/node_meta"

# git shim: answers only what bench_correction_storm.py needs
# (`git rev-parse HEAD`) -- scripts/crash_campaign.slurm's own idiom.
SHIM_DIR="/tmp/tgms-storm-dag-shim-${JOB_ID}"
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

# node-local scratch -- never /project (NFS4 sillyrename breaks tempfile
# cleanup, scripts/crash_campaign.slurm's own finding).
TMPDIR="/tmp/tgms-storm-dag-${JOB_ID}"
mkdir -p "$TMPDIR"
export TMPDIR
cleanup() { rm -rf "$TMPDIR" "$SHIM_DIR"; }
trap cleanup EXIT

echo "RUN_STARTED commit=${TGMS_COMMIT} task=${TASK_ID} store=${DAG_STORE} " \
     "shape=${SHAPE} depth=${DEPTH} fanout=${FANOUT} seed=${SEED} cascade_k=${CASCADE_K}"
hostname > "$STAGE/node_meta/dag-task-${TASK_ID}.host"
{ uname -a; nproc; } > "$STAGE/node_meta/dag-task-${TASK_ID}.uname" 2>&1

cd "$CHECKOUT"
# addendum stamps (additive; empty by default) -- same convention as
# storm_campaign.slurm.
ADDENDUM_ID_FLAG=""
[ -n "${TGMS_ADDENDUM_ID:-}" ] && ADDENDUM_ID_FLAG="--addendum-id $TGMS_ADDENDUM_ID"
FREEZE_SHA_FLAG=""
[ -n "${TGMS_FREEZE_SHA256:-}" ] && FREEZE_SHA_FLAG="--freeze-sha256 $TGMS_FREEZE_SHA256"
# storm-v1 addendum-2 (additive; default off, byte-identical v1 walk when
# absent): seed the cascade from tgms.artifact.lookup.affected()'s
# footprint-based answer for the root correction batch, not just the root.
SEED_FROM_AFFECTED_FLAG=""
[ "${TGMS_DAG_SEED_FROM_AFFECTED:-0}" = "1" ] && SEED_FROM_AFFECTED_FLAG="--dag-seed-from-affected"
ec=0
"$VENV_PY" scripts/bench_correction_storm.py \
    --store "$DAG_STORE" \
    --n-artifacts "$DAG_N_ARTIFACTS" \
    --batches "$DAG_BATCHES" \
    --seed "$SEED" \
    --dag-shape "$SHAPE" \
    --dag-depth "$DEPTH" \
    $SEED_FROM_AFFECTED_FLAG \
    --dag-fanout "$FANOUT" \
    --dag-cascade-k "$CASCADE_K" \
    $ADDENDUM_ID_FLAG \
    $FREEZE_SHA_FLAG \
    --out "$STAGE/dag-records/task-${TASK_ID}" \
    && ec=0 || ec=$?

echo "TASK_DONE task=${TASK_ID} store=${DAG_STORE} shape=${SHAPE} depth=${DEPTH} " \
     "fanout=${FANOUT} seed=${SEED} cascade_k=${CASCADE_K} exit=${ec}"
exit "$ec"

#!/bin/bash
#SBATCH --job-name=tgms-storm-r18
#SBATCH --partition=bigTiger
#SBATCH --time=12:00:00
#SBATCH --signal=B:TERM@10800
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --output=/project/xzhang12/storm-v1-work/r18-work/logs/task-0.out
#SBATCH --error=/project/xzhang12/storm-v1-work/r18-work/logs/task-0.err
#
# storm-v1 addendum-1's single R-18 probe cell: synth-iv-60k, c1 only,
# N=10,000, seed 0, 5 batches, --allow-r18-trip. A dedicated single-task
# script, additive and separate from `scripts/storm_campaign.slurm`
# (which has no wall-cap wiring and is shared by the 36-task main grid),
# because this is the one cell in the whole campaign carrying its own
# wall_cap_h (12h, addendum-1's own field) with an explicit
# "keep the partial record if the cap hits" requirement.
#
# `scripts/bench_correction_storm.py --wall-cap-s` installs a SIGTERM
# handler and stops after the in-flight batch if the signal arrives before
# all requested batches complete, writing a partial manifest
# (config.wall_capped=true) instead of nothing -- `Storm.run` itself has no
# such hook, so the CLI inlines an equivalent loop only when this flag is
# passed (verified byte-identical to the uncapped path when the signal
# never fires; see that script's own comment at the flag's definition).
#
# Two mechanics this depends on, both verified by a dedicated smoke test
# before this script was written:
#   1. `exec` replaces this batch script's own bash process with python, so
#      Slurm's `--signal=B:...` (which targets only the batch step's own
#      process, not children it forks) reaches python directly. Without
#      `exec`, bash receives the signal, dies immediately (no default
#      SIGTERM trap), and Slurm tears down the whole cgroup -- including
#      the orphaned python child -- before it ever sees the signal, which
#      is exactly what a first attempt at this script did (no partial
#      record, job just "CANCELLED").
#   2. The signal is checked only *between* batches (never mid-batch): if
#      one in-flight batch's own real execution time exceeds the lead time
#      below (10800s = 3h) after the signal fires, that one batch's result
#      is lost when the hard --time limit kills the job outright. R-18's
#      whole premise is a possible super-linear blow-up at N=10,000, so
#      this is a real, accepted residual risk, not a hypothetical one --
#      report it if it happens (config.wall_capped will be absent/false and
#      no manifest will exist, rather than a silently-shortened one).
#
# Same two deviations as scripts/storm_campaign.slurm / crash_campaign.slurm
# (see those files' own headers): (1) TMPDIR is node-local, never /project;
# (2) no `git` binary on iTiger compute nodes, so a two-line shim answers
# `git rev-parse HEAD` from $TGMS_COMMIT.

set -uo pipefail  # not -e: the git-shim heredoc below must not abort early

JOB_ID="${SLURM_JOB_ID:?must run under sbatch}"

CHECKOUT="${TGMS_REPO:-/project/xzhang12/tgms-storm}"
STAGE="${TGMS_STAGE:-/project/xzhang12/storm-v1-work/r18-work}"
export TGMS_COMMIT="${TGMS_COMMIT:-8962b78}"
VENV_PY="${TGMS_VENV_PY:-$CHECKOUT/.venv/bin/python}"

STORE="${TGMS_R18_STORE:-synth-iv-60k}"
MIX="${TGMS_R18_MIX:-c1}"
N_ARTIFACTS="${TGMS_R18_N_ARTIFACTS:-10000}"
SEED="${TGMS_R18_SEED:-0}"
BATCHES="${TGMS_R18_BATCHES:-5}"
WALL_CAP_S="${TGMS_R18_WALL_CAP_S:-32400}"  # 9h -- signal fires at 3h before
                                            # the 12h hard limit (--signal
                                            # above); this is the value the
                                            # harness itself uses only to
                                            # record config.wall_cap_s, the
                                            # actual enforcement is Slurm's
                                            # own --signal/--time.
ADDENDUM_ID="${TGMS_ADDENDUM_ID:-storm-v1-addendum-1}"
FREEZE_SHA256="${TGMS_FREEZE_SHA256:?TGMS_FREEZE_SHA256 must be set (campaign.yaml sha256 after the addendum)}"

mkdir -p "$STAGE/records/task-0" "$STAGE/logs" "$STAGE/node_meta"

# git shim: answers only what bench_correction_storm.py needs
# (`git rev-parse HEAD`) -- scripts/crash_campaign.slurm's own idiom.
SHIM_DIR="/tmp/tgms-storm-r18-shim-${JOB_ID}"
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
export TMPDIR="/tmp/tgms-storm-r18-${JOB_ID}"
mkdir -p "$TMPDIR"

echo "RUN_STARTED commit=${TGMS_COMMIT} store=${STORE} mix=${MIX} " \
     "n_artifacts=${N_ARTIFACTS} seed=${SEED} batches=${BATCHES} " \
     "addendum_id=${ADDENDUM_ID}"
hostname > "$STAGE/node_meta/task-0.host"
{ uname -a; nproc; } > "$STAGE/node_meta/task-0.uname" 2>&1

cd "$CHECKOUT"
# `exec` -- see header mechanic (1): python must directly be the batch
# step's own process for `--signal=B:TERM@...` above to reach it.
exec "$VENV_PY" scripts/bench_correction_storm.py \
    --store "$STORE" \
    --n-artifacts "$N_ARTIFACTS" \
    --batches "$BATCHES" \
    --seed "$SEED" \
    --mix "$MIX" \
    --measure-ttf sum \
    --allow-r18-trip \
    --wall-cap-s "$WALL_CAP_S" \
    --addendum-id "$ADDENDUM_ID" \
    --freeze-sha256 "$FREEZE_SHA256" \
    --out "$STAGE/records/task-0"

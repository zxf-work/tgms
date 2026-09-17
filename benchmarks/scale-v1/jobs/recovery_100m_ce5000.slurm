#!/bin/bash
#SBATCH --job-name=b7-recovery-100m-ce5000
#SBATCH --partition=bigTiger
#SBATCH --exclude=itiger04,itiger05
#SBATCH --gres=gpu:rtx_5000:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=256G
#SBATCH --time=24:00:00
#SBATCH --output=/home/xzhang12/b7-work-h/logs/recovery-100m-ce5000-%j.out
#SBATCH --error=/home/xzhang12/b7-work-h/logs/recovery-100m-ce5000-%j.err

# SCALE_BUILD_FORECAST_2026-09-15 Addendum 6 (coordinator, 2026-09-16
# ~09:10 UTC), "Recovery cadence pre-registration": the frozen 100M
# recovery band (1.7-2.9h) assumed linear cost; under --compact-every 500
# the 30M result (job 213071: 14,618.6s = 4.06h vs band 29-46 min,
# REFUTED 5.3x) extrapolates to ~45h at 100M (800 compactions over up to
# 100M rows), which cannot fit the 2-day wall and would measure only
# D-164 (compact() materialises every row per compaction). Therefore the
# 100M recovery runs with --compact-every 5000 (~80 compactions);
# prediction 3-6h, digest equal; falsifier > 2x upper or digest unequal.
# This is the only 100M recovery job in the campaign -- there is no
# 500-cadence 100M run to reconcile against (unlike the 30M lane, which
# isolates the cadence effect against its own frozen-protocol run).

set -uo pipefail
export PATH=$HOME/.cargo/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH
WORKTREE=/project/xzhang12/tgms-b7
EXPECTED_HEAD="b159bd10aa53ad5de5f4e8ff7af1c87789d6afd0"
WORKH=/home/xzhang12/b7-work-h
cd "$WORKTREE" || { echo "ABORT: cannot cd to worktree"; exit 92; }

WRITE_TEST="$WORKH/logs/write-test-${SLURM_JOB_ID}.txt"
if ! ( echo "live write test $(date -u +%FT%TZ) job=${SLURM_JOB_ID}" > "$WRITE_TEST" ); then
    echo "ABORT: live write test to $WORKH failed"
    exit 90
fi

PROJECT_MB=$(du -sm /project/xzhang12 2>/dev/null | awk '{print $1}')
if [ -n "${PROJECT_MB}" ] && [ "${PROJECT_MB}" -gt 185000 ]; then
    echo "ABORT: /project/xzhang12 at ${PROJECT_MB} MB, over the 185000 MB fail-fast line"
    exit 93
fi
echo "PROJECT_DU_BEFORE_MB=${PROJECT_MB}"

export TGMS_SEGMENT_CACHE_BYTES=46500000000
ACTUAL_HEAD=$(cat "$WORKTREE/.HEAD_COMMIT" 2>/dev/null)
export TGMS_COMMIT="${ACTUAL_HEAD}"
BUILD_INFO_JSON=$(.venv/bin/python -c "from tgms.storage.native.adapter import NativeAdapter; import json; print(json.dumps(NativeAdapter.build_info()))" 2>&1)
NODE=$(hostname)
CPU_MODEL=$(lscpu | grep 'Model name:' | sed 's/Model name:[[:space:]]*//')

echo "RUN_STARTED commit=${ACTUAL_HEAD}"
echo "BUILD_INFO ${BUILD_INFO_JSON}"
echo "TGMS_SEGMENT_CACHE_BYTES=${TGMS_SEGMENT_CACHE_BYTES}"
echo "SLURM_JOB_ID=${SLURM_JOB_ID} SLURM_NODELIST=${SLURM_NODELIST} NODE=${NODE}"
echo "CPU_MODEL=${CPU_MODEL}"

if [ "${ACTUAL_HEAD}" != "${EXPECTED_HEAD}" ]; then
    echo "ABORT: commit stamp mismatch — expected ${EXPECTED_HEAD}, worktree HEAD is ${ACTUAL_HEAD}"
    exit 91
fi

mkdir -p "$WORKH/records"
SRC_STORE="$WORKTREE/stores/synth-100m-native"
# Job-id-suffixed so a rerun never needs to delete a prior replay copy
# (never-delete policy) -- each attempt gets its own directory. "ce5000"
# in the name distinguishes this cadence from the frozen-protocol one.
REPLAY_STORE="$WORKTREE/stores/synth-100m-native-replayed-ce5000-${SLURM_JOB_ID}"
COMPACT_EVERY=5000

T0=$(date +%s.%N)
REPLAY_OUT=$(.venv/bin/python -m tgms.cli replay "$SRC_STORE/eventlog.jsonl" \
    --store "$REPLAY_STORE" --backend native --compact-every "${COMPACT_EVERY}")
REPLAY_EXIT=$?
T1=$(date +%s.%N)
REPLAY_WALL_S=$(python3 -c "print(${T1}-${T0})")
echo "REPLAY_STDOUT ${REPLAY_OUT}"
echo "REPLAY_EXIT=${REPLAY_EXIT}"
echo "REPLAY_WALL_S=${REPLAY_WALL_S}"

# `tgms.cli replay` prints {"batches": n, "stats": ...}; compact_every
# (tgms/storage/eventlog.py::replay) fires adapter.compact()+gc() every
# compact_every-th *applied batch*, no trailing compaction after the loop,
# so the exact count is n // compact_every -- computed here rather than
# re-instrumenting the engine.
BATCHES=$(echo "${REPLAY_OUT}" | python3 -c "import json,sys; print(json.load(sys.stdin)['batches'])")
COMPACTIONS=$(( BATCHES / COMPACT_EVERY ))
echo "BATCHES=${BATCHES} COMPACT_EVERY=${COMPACT_EVERY} COMPACTIONS=${COMPACTIONS}"

DIGEST_JSON=$(.venv/bin/python -c "
import json, sys
sys.path.insert(0, '.')
import tgms
try:
    src = tgms.open('${SRC_STORE}', backend='native')
    src_digest = src.digest_streaming()
    src.close()
    rep = tgms.open('${REPLAY_STORE}', backend='native')
    rep_digest = rep.digest_streaming()
    rep.close()
    print(json.dumps({'src_digest': src_digest, 'replayed_digest': rep_digest,
                       'digest_equal': src_digest == rep_digest}))
except Exception as e:
    print(json.dumps({'error': f'{type(e).__name__}: {e}'}))
")
echo "DIGEST_COMPARE ${DIGEST_JSON}"

python3 -c "
import json
record = {
    'store': '${SRC_STORE}', 'replay_store': '${REPLAY_STORE}',
    'compact_every': ${COMPACT_EVERY}, 'batches': ${BATCHES},
    'compactions': ${COMPACTIONS}, 'replay_wall_s': ${REPLAY_WALL_S},
    'replay_exit': ${REPLAY_EXIT}, 'job_id': '${SLURM_JOB_ID}',
    'digest_compare': json.loads('''${DIGEST_JSON}'''),
}
with open('$WORKH/records/recovery-100m-ce5000-${SLURM_JOB_ID}.json', 'w') as f:
    json.dump(record, f, indent=2, sort_keys=True)
    f.write('\n')
"

PROJECT_MB_AFTER=$(du -sm /project/xzhang12 2>/dev/null | awk '{print $1}')
echo "PROJECT_DU_AFTER_MB=${PROJECT_MB_AFTER}"
echo "RUN_FINISHED commit=${ACTUAL_HEAD} replay_exit=${REPLAY_EXIT} wall_s=${REPLAY_WALL_S} compactions=${COMPACTIONS}"
exit "${REPLAY_EXIT}"

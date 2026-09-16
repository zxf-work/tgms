#!/bin/bash
#SBATCH --job-name=b7-recovery-30m
#SBATCH --partition=bigTiger
#SBATCH --exclude=itiger04,itiger05
#SBATCH --gres=gpu:rtx_5000:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=128G
#SBATCH --time=06:00:00
#SBATCH --output=/home/xzhang12/b7-work-h/logs/recovery-30m-%j.out
#SBATCH --error=/home/xzhang12/b7-work-h/logs/recovery-30m-%j.err

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
SRC_STORE="$WORKTREE/stores/synth-30m-native"
# Job-id-suffixed so a rerun never needs to delete a prior replay copy
# (never-delete policy) -- each attempt gets its own directory.
REPLAY_STORE="$WORKTREE/stores/synth-30m-native-replayed-${SLURM_JOB_ID}"

T0=$(date +%s.%N)
.venv/bin/python -m tgms.cli replay "$SRC_STORE/eventlog.jsonl" \
    --store "$REPLAY_STORE" --backend native --compact-every 500
REPLAY_EXIT=$?
T1=$(date +%s.%N)
REPLAY_WALL_S=$(python3 -c "print(${T1}-${T0})")
echo "REPLAY_EXIT=${REPLAY_EXIT}"
echo "REPLAY_WALL_S=${REPLAY_WALL_S}"

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
echo "${DIGEST_JSON}" > "$WORKH/records/recovery-30m-digest-${SLURM_JOB_ID}.json"
echo "{\"replay_wall_s\": ${REPLAY_WALL_S}, \"replay_exit\": ${REPLAY_EXIT}}" \
    > "$WORKH/records/recovery-30m-wall-${SLURM_JOB_ID}.json"

PROJECT_MB_AFTER=$(du -sm /project/xzhang12 2>/dev/null | awk '{print $1}')
echo "PROJECT_DU_AFTER_MB=${PROJECT_MB_AFTER}"
echo "RUN_FINISHED commit=${ACTUAL_HEAD} replay_exit=${REPLAY_EXIT} wall_s=${REPLAY_WALL_S}"
exit "${REPLAY_EXIT}"

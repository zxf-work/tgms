#!/bin/bash
#SBATCH --job-name=b7-build-30m
#SBATCH --partition=bigTiger
#SBATCH --exclude=itiger04,itiger05
#SBATCH --gres=gpu:rtx_5000:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=128G
#SBATCH --time=06:00:00
#SBATCH --output=/home/xzhang12/b7-work-h/logs/build-30m-%j.out
#SBATCH --error=/home/xzhang12/b7-work-h/logs/build-30m-%j.err

set -uo pipefail
export PATH=$HOME/.cargo/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH
WORKTREE=/project/xzhang12/tgms-b7
EXPECTED_HEAD="b159bd10aa53ad5de5f4e8ff7af1c87789d6afd0"
WORKH=/home/xzhang12/b7-work-h
cd "$WORKTREE" || { echo "ABORT: cannot cd to worktree"; exit 92; }

# --- live write test on /home, at job start ---
WRITE_TEST="$WORKH/logs/write-test-${SLURM_JOB_ID}.txt"
if ! ( echo "live write test $(date -u +%FT%TZ) job=${SLURM_JOB_ID}" > "$WRITE_TEST" ); then
    echo "ABORT: live write test to $WORKH failed"
    exit 90
fi

# --- /project quota fail-fast (cluster-memory standing rule) ---
PROJECT_MB=$(du -sm /project/xzhang12 2>/dev/null | awk '{print $1}')
if [ -n "${PROJECT_MB}" ] && [ "${PROJECT_MB}" -gt 185000 ]; then
    echo "ABORT: /project/xzhang12 at ${PROJECT_MB} MB, over the 185000 MB fail-fast line"
    exit 93
fi
echo "PROJECT_DU_BEFORE_MB=${PROJECT_MB}"

export TGMS_SEGMENT_CACHE_BYTES=46500000000
# git is not installed on iTiger compute nodes (Stage 0 finding, itiger11);
# HEAD was stamped into .HEAD_COMMIT on the login node right after clone and
# is read back here instead of shelling out to git.
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

OUT_DIR="$WORKTREE/stores/synth-30m-native"
RSS_LOG="$WORKH/records/rss-30m-${SLURM_JOB_ID}.jsonl"
mkdir -p "$WORKH/records"

.venv/bin/python scripts/build_synth_store.py \
    --n-entities 30000000 --batch 250 --compact-every 1000000 \
    --digest streaming --backend native --out "$OUT_DIR" &
BUILD_PID=$!
echo "BUILD_PID=${BUILD_PID}"

python3 "$WORKH/tools/rss_sampler.py" --pid "$BUILD_PID" --interval 30 --out "$RSS_LOG" &
SAMPLER_PID=$!

wait "$BUILD_PID"
BUILD_EXIT=$?
kill "$SAMPLER_PID" 2>/dev/null || true
wait "$SAMPLER_PID" 2>/dev/null || true

PROJECT_MB_AFTER=$(du -sm /project/xzhang12 2>/dev/null | awk '{print $1}')
echo "PROJECT_DU_AFTER_MB=${PROJECT_MB_AFTER}"
echo "BUILD_EXIT=${BUILD_EXIT}"
echo "RUN_FINISHED commit=${ACTUAL_HEAD} exit=${BUILD_EXIT}"
exit "${BUILD_EXIT}"

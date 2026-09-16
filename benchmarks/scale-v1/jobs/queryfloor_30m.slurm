#!/bin/bash
#SBATCH --job-name=b7-queryfloor-30m
#SBATCH --partition=bigTiger
#SBATCH --exclude=itiger04,itiger05
#SBATCH --gres=gpu:rtx_5000:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=128G
#SBATCH --time=06:00:00
#SBATCH --output=/home/xzhang12/b7-work-h/logs/queryfloor-30m-%j.out
#SBATCH --error=/home/xzhang12/b7-work-h/logs/queryfloor-30m-%j.err

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
STORE_DIR="$WORKTREE/stores/synth-30m-native"

# (a) query-ready floor: a FRESH process cold-opens the store and runs the
# 13-op registry once each; this process's own VmHWM is the §2b quantity,
# distinct from the build's peak RSS measured in the build job.
.venv/bin/python "$WORKH/tools/query_floor.py" \
    --worktree "$WORKTREE" --store "$STORE_DIR" \
    --log "$STORE_DIR/eventlog.jsonl" \
    --out "$WORKH/records/queryfloor-30m-${SLURM_JOB_ID}.json"
FLOOR_EXIT=$?
echo "QUERY_FLOOR_EXIT=${FLOOR_EXIT}"

echo "RUN_FINISHED commit=${ACTUAL_HEAD} queryfloor_exit=${FLOOR_EXIT}"
exit "${FLOOR_EXIT}"

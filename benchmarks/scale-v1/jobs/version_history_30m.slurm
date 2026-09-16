#!/bin/bash
#SBATCH --job-name=b7-version-history-30m
#SBATCH --partition=bigTiger
#SBATCH --exclude=itiger04,itiger05
#SBATCH --gres=gpu:rtx_5000:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=128G
#SBATCH --time=06:00:00
#SBATCH --output=/home/xzhang12/b7-work-h/logs/version-history-30m-%j.out
#SBATCH --error=/home/xzhang12/b7-work-h/logs/version-history-30m-%j.err

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
RAW="$WORKH/records/version-history-30m-raw-${SLURM_JOB_ID}.jsonl"
: > "$RAW"

# (d) version_history wall + RSS on the same store, same call shape as B2
# (benchmarks/results-v1/b2-version-history-ab-2026-09*): kind="edge",
# belief="all", skip_cost_check=True (the plain path is refused past a few
# million versions by design). 3 separate-process reps, B2-style median-of-3.
for i in 1 2 3; do
    .venv/bin/python "$WORKH/tools/version_history_probe.py" \
        --worktree "$WORKTREE" --store "$STORE_DIR" >> "$RAW"
    echo "VERSION_HISTORY_REP_${i}_EXIT=$?"
done

.venv/bin/python - "$RAW" "$WORKH/records/version-history-30m-${SLURM_JOB_ID}.json" <<'PYEOF'
import json, statistics, sys
raw_path, out_path = sys.argv[1], sys.argv[2]
recs = [json.loads(l) for l in open(raw_path) if l.strip()]
walls = [r["wall_ms"] for r in recs]
vmhwms = [r["vmhwm_kb"] for r in recs]
summary = {
    "reps": recs, "n": len(recs),
    "wall_ms_median": statistics.median(walls) if walls else None,
    "vmhwm_kb_median": statistics.median(vmhwms) if vmhwms else None,
    "rows_total": recs[0]["rows_total"] if recs else None,
}
with open(out_path, "w") as f:
    json.dump(summary, f, indent=2, sort_keys=True)
    f.write("\n")
print(json.dumps({"wall_ms_median": summary["wall_ms_median"],
                   "vmhwm_kb_median": summary["vmhwm_kb_median"],
                   "rows_total": summary["rows_total"]}))
PYEOF
SUMMARY_EXIT=$?
echo "VERSION_HISTORY_SUMMARY_EXIT=${SUMMARY_EXIT}"

echo "RUN_FINISHED commit=${ACTUAL_HEAD} summary_exit=${SUMMARY_EXIT}"
exit "${SUMMARY_EXIT}"

#!/bin/bash
#SBATCH --job-name=b7-scale-check-30m
#SBATCH --partition=bigTiger
#SBATCH --exclude=itiger04,itiger05
#SBATCH --gres=gpu:rtx_5000:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=128G
#SBATCH --time=06:00:00
#SBATCH --output=/home/xzhang12/b7-work-h/logs/scale-check-30m-%j.out
#SBATCH --error=/home/xzhang12/b7-work-h/logs/scale-check-30m-%j.err

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
echo "PRODUCING_SCRIPT=scripts/eval_harness.py commit=${ACTUAL_HEAD}"

if [ "${ACTUAL_HEAD}" != "${EXPECTED_HEAD}" ]; then
    echo "ABORT: commit stamp mismatch — expected ${EXPECTED_HEAD}, worktree HEAD is ${ACTUAL_HEAD}"
    exit 91
fi

mkdir -p "$WORKH/records"
STORE_DIR="$WORKTREE/stores/synth-30m-native"

# (b) scale-curve set: same 13-operator registry / protocol constants as
# eval-10m-agg.json and the Stage-0 calibration (warmup=5, reps 30/10, warm
# cache), TGMS side only (--systems native; no ClickHouse on iTiger).
.venv/bin/python scripts/eval_harness.py --scale 30000000 --systems native \
    --json "$WORKH/records/scale-curve-30m-raw-${SLURM_JOB_ID}.json"
QUERY_EXIT=$?
echo "EVAL_HARNESS_EXIT=${QUERY_EXIT}"

# (c) tgms check --full: exit + wall.
CHECK_T0=$(date +%s.%N)
.venv/bin/tgms check "$STORE_DIR" --json > "$WORKH/records/check-full-30m-${SLURM_JOB_ID}.json"
CHECK_EXIT=$?
CHECK_T1=$(date +%s.%N)
echo "CHECK_FULL_EXIT=${CHECK_EXIT}"
echo "CHECK_FULL_WALL_S=$(python3 -c "print(${CHECK_T1}-${CHECK_T0})")"

# reach.window admission estimate + outcome, read directly off the cost
# guardrail (Addendum 5: "the lane records the estimate at each scale";
# eval_harness's own error string drops the estimate dict on refusal, so
# this computes/enforces it explicitly instead of re-parsing that string).
.venv/bin/python - "$STORE_DIR" "$WORKH/records/reach-admission-30m-${SLURM_JOB_ID}.json" <<'PYEOF'
import json, sys
sys.path.insert(0, "scripts")
import tgms
from tgms.temporal.algebra import REGISTRY, ensure_all_registered, validate_args, call_operator
from tgms.temporal.guardrails import add_time_estimate, enforce_cost
from tgms.core.errors import CostError
import eval_harness

store_dir, out_path = sys.argv[1], sys.argv[2]
ensure_all_registered()
store = tgms.open(store_dir, backend="native")
adapter = store.adapter
stats = adapter.stats()

t0, t1 = 0, 30_000_000  # same [0, n_entities) extent build_synth_store.py uses
span = t1 - t0
args = {"src": "n1", "window": {"t_a": t0, "t_b": t0 + span // 10}}
filled = validate_args("temporal_reachability", args)
spec = REGISTRY["temporal_reachability"]
estimate = add_time_estimate("temporal_reachability", spec.cost_fn(filled, stats))

admitted = True
refusal = None
try:
    enforce_cost("temporal_reachability", estimate)
except CostError as e:
    admitted = False
    refusal = e.details

executed_ok = None
exec_ms = None
rows = None
if admitted:
    import time
    t0p = time.perf_counter()
    try:
        payload = call_operator(adapter, "temporal_reachability", dict(args))
        exec_ms = round((time.perf_counter() - t0p) * 1000, 3)
        executed_ok = True
        rows = eval_harness._answer_size(payload)
    except Exception as e:
        executed_ok = False
        exec_ms = round((time.perf_counter() - t0p) * 1000, 3)
        refusal = {"error": f"{type(e).__name__}: {e}"}
store.close()

record = {
    "op": "temporal_reachability", "id": "reach.window", "scale": 30_000_000,
    "args": args, "time_est_ms": estimate.get("time_est_ms"),
    "estimate": estimate, "admitted": admitted,
    "executed_ok": executed_ok, "exec_ms": exec_ms, "rows": rows,
    "refusal_details": refusal,
}
with open(out_path, "w") as f:
    json.dump(record, f, indent=2, sort_keys=True)
    f.write("\n")
print(json.dumps({"admitted": admitted, "time_est_ms": estimate.get("time_est_ms"),
                   "executed_ok": executed_ok, "exec_ms": exec_ms}))
PYEOF
REACH_EXIT=$?
echo "REACH_ADMISSION_PROBE_EXIT=${REACH_EXIT}"

echo "RUN_FINISHED commit=${ACTUAL_HEAD} eval_exit=${QUERY_EXIT} check_exit=${CHECK_EXIT} reach_probe_exit=${REACH_EXIT}"
if [ "${QUERY_EXIT}" -ne 0 ] || [ "${CHECK_EXIT}" -ne 0 ]; then
    exit 1
fi
exit 0

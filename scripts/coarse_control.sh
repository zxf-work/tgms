#!/usr/bin/env bash
# COARSE -- the R1C same-host TGMS control (scripts/r1c_control.sh) replayed
# with the first storm campaign's lineage (P-COARSE): the same loop, flags,
# cell order, equality check and store source, plus `--leaf-scopes storm-v1`
# on every cell (the ten later LEAF_SCOPES entries removed in-process before
# registration; entity_history, neighborhood_evolution and aggregate_events
# keep their derivations). Differences from r1c_control.sh, and only these:
#   1. code: the worktree work/tgms-xz-coarse (public main plus the
#      --leaf-scopes flag; crates/ unchanged since b6cdde0, so the engine .so,
#      sha256 d48ae71caca80d9f3ea9f360b2e7a2f242f3e9e03ee223d69c4f8aff08b7faca,
#      was copied in from work/tgms-xz-ed4a83d6 rather than rebuilt)
#   2. outputs under external-v1/coarse/, host log external-v1/HOST-COARSE.log,
#      fresh store copies under external-v1/coarse-stores/ (taken from
#      external-v1/t1-stores/, as R1c's were), equality helper
#      external-v1/coarse_equality_check.py (a byte copy of r1c's)
#   3. `--leaf-scopes storm-v1` on every cell
#   4. the N = 10^4 probe cell is dropped: 18 N = 10^3 cells
#   5. log and label strings read COARSE instead of R1C
# Memgraph is stopped for the window and restarted by the EXIT trap. The
# on-demand mode is OFF (no --refresh-on-read), as in R1c.
set -uo pipefail   # no -e: one cell's failure must be recorded, never abort the other 18

ROOT=/mnt/project/xzhang/tgms
WORKTREE="$ROOT/work/tgms-xz-coarse"
VENV_PY="$ROOT/venv/bin/python"
STORES_SRC="$ROOT/work/tgms/stores"
EXT="$ROOT/external-v1"
EXPORT="$EXT/export"
COARSE="$EXT/coarse"
COARSESTORES="$EXT/coarse-stores"
EQCHECK="$EXT/coarse_equality_check.py"   # copied alongside this script

ADDENDUM_ID="storm-v1-addendum-3"
FREEZE_SHA256="c85fb0c8bb3b17e0b9f02a92a5ee0d5273298f43574583206582e5e5aa34d309"

export PYTHONPATH="$WORKTREE"

mkdir -p "$COARSE" "$COARSESTORES"

HOSTLOG="$EXT/HOST-COARSE.log"
PROGRESS="$COARSE/PROGRESS.log"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$HOSTLOG"; }

snapshot() {
  echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) =="
  uptime
  free -g
  ps -eo user,pcpu,pmem,comm --sort=-pcpu | head -12
  ss -ltnp 2>/dev/null | grep -E "7687|7688|7474|7475|8123" || echo "(no match on 7687/7688/7474/7475/8123)"
}

# ---------------------------------------------------------------------
# 0. pre-flight: confirm the box is free of other TGMS/export/Neo4j work
# ---------------------------------------------------------------------
log "COARSE starting. pre-flight process check."
OTHER=$(ps -eo pid,cmd | grep -iE "bench_correction_storm|ext_export|export_storm_workload|ivm-dd|neo4j" | grep -v grep | grep -v "$$" || true)
if [ -n "$OTHER" ]; then
  log "ABORT: another TGMS/export/Neo4j process appears to be running; COARSE must not overlap it:"
  log "$OTHER"
  exit 1
fi
log "pre-flight ok: no other TGMS/export/Neo4j process found."
snapshot >> "$HOSTLOG"

# ---------------------------------------------------------------------
# 1. stop Memgraph for the timed window
# ---------------------------------------------------------------------
log "stopping Memgraph (docker stop memgraph) for the timed window"
docker stop memgraph >> "$HOSTLOG" 2>&1
log "Memgraph stop issued. restart command recorded: 'docker start memgraph'."
MEMGRAPH_RESTARTED=0
restart_memgraph_on_exit() {
  if [ "$MEMGRAPH_RESTARTED" -eq 0 ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] EXIT trap: restarting Memgraph (docker start memgraph)" >> "$HOSTLOG"
    docker start memgraph >> "$HOSTLOG" 2>&1
    MEMGRAPH_RESTARTED=1
  fi
}
trap restart_memgraph_on_exit EXIT
snapshot >> "$HOSTLOG"

# ---------------------------------------------------------------------
# 2. fresh scratch store copies (from T1's own t1-stores copies, per the
#    R1 instructions -- NOT from the shared stores/ source directly) +
#    manifest upgrade (idempotent; no-op if already format 3)
# ---------------------------------------------------------------------
for STORE in collegemsg synth-iv-60k; do
  if [ ! -d "$COARSESTORES/$STORE" ]; then
    log "copying T1's $STORE copy -> $COARSESTORES/$STORE"
    cp -a "$EXT/t1-stores/$STORE" "$COARSESTORES/$STORE"
  fi
  log "upgrade-manifests on $COARSESTORES/$STORE (idempotent; no-op if already format 3)"
  "$VENV_PY" -m tgms.cli store upgrade-manifests --store "$COARSESTORES/$STORE" 2>&1 | tee -a "$HOSTLOG"
done

# ---------------------------------------------------------------------
# 3. the 18-cell grid -- identical order/flags to T1, plus --check-cache chain
# ---------------------------------------------------------------------
CELLS=(
  "collegemsg|c1|none|1000|20|end-to-end|0|1"
  "collegemsg|c1|deep|1000|20|end-to-end|0|1"
  "collegemsg|c3|none|1000|20|end-to-end|0|1"
  "collegemsg|c3|deep|1000|20|end-to-end|0|1"
  "collegemsg|c4|none|1000|20|end-to-end|0|1"
  "collegemsg|c4|deep|1000|20|end-to-end|0|1"
  "synth-iv-60k|c1|none|1000|20|end-to-end|0|1"
  "synth-iv-60k|c1|deep|1000|20|end-to-end|0|1"
  "synth-iv-60k|c3|none|1000|20|end-to-end|0|1"
  "synth-iv-60k|c3|deep|1000|20|end-to-end|0|1"
  "synth-iv-60k|c4|none|1000|20|end-to-end|0|1"
  "synth-iv-60k|c4|deep|1000|20|end-to-end|0|1"
  "synth-iv-60k|c3|recent|1000|20|end-to-end|0|0"
  "synth-iv-60k|c3|hours|1000|20|end-to-end|0|0"
  "synth-iv-60k|c3|days|1000|20|end-to-end|0|0"
  "collegemsg|c3|recent|1000|20|end-to-end|0|0"
  "collegemsg|c3|hours|1000|20|end-to-end|0|0"
  "collegemsg|c3|days|1000|20|end-to-end|0|0"
)

SEED=0

for SPEC in "${CELLS[@]}"; do
  IFS='|' read -r STORE MIX AGE N BATCHES TTF ALLOW_R18 STAMP <<< "$SPEC"
  CELL_ID="${STORE}-${MIX}-${AGE}-n${N}-s${SEED}"
  OUTDIR="$COARSE/$CELL_ID"
  mkdir -p "$OUTDIR"

  log "cell $CELL_ID: starting ($BATCHES batches, measure_ttf=$TTF)"
  snapshot > "$OUTDIR/host-snapshot-before.txt"

  AGE_FLAG=()
  [ "$AGE" != "none" ] && AGE_FLAG=(--age "$AGE")
  R18_FLAG=()
  [ "$ALLOW_R18" = "1" ] && R18_FLAG=(--allow-r18-trip)
  STAMP_FLAGS=()
  [ "$STAMP" = "1" ] && STAMP_FLAGS=(--addendum-id "$ADDENDUM_ID" --freeze-sha256 "$FREEZE_SHA256")

  T0=$(date +%s)
  "$VENV_PY" "$WORKTREE/scripts/bench_correction_storm.py" \
      --store "$COARSESTORES/$STORE" \
      --n-artifacts "$N" \
      --batches "$BATCHES" \
      --seed "$SEED" \
      --mix "$MIX" \
      "${AGE_FLAG[@]}" \
      --measure-ttf "$TTF" \
      --backend native \
      --burst-size 10000 --burst-after 10 \
      --check-cache chain \
      --leaf-scopes storm-v1 \
      "${R18_FLAG[@]}" \
      "${STAMP_FLAGS[@]}" \
      --out "$OUTDIR" \
      > "$OUTDIR/run.log" 2>&1
  EC=$?
  WALL=$(( $(date +%s) - T0 ))
  snapshot > "$OUTDIR/host-snapshot-after.txt"

  if [ $EC -ne 0 ]; then
    log "cell $CELL_ID: FAILED (exit $EC, wall ${WALL}s) -- see $OUTDIR/run.log"
    echo "$CELL_ID wall=${WALL}s exit=${EC} FAILED" >> "$PROGRESS"
    continue
  fi

  BATCHES_REALIZED=$(find "$OUTDIR" -maxdepth 1 -name 'storm-*-rows.jsonl' -exec wc -l {} \; | awk '{print $1}' | head -1)

  EQ_VERDICT="unknown"
  if [ -d "$EXPORT/$CELL_ID" ]; then
    EQ_OUT=$("$VENV_PY" "$EQCHECK" --cell-id "$CELL_ID" --t1-dir "$OUTDIR" \
              --export-dir "$EXPORT/$CELL_ID" 2>&1 | tee -a "$OUTDIR/equality-check.log")
    EQ_VERDICT=$(echo "$EQ_OUT" | tail -1 | "$VENV_PY" -c "import json,sys; print(json.loads(sys.stdin.read()).get('verdict','unknown'))" 2>/dev/null || echo "check-failed")
  else
    log "cell $CELL_ID: no export bundle at $EXPORT/$CELL_ID -- equality not computed"
  fi

  log "cell $CELL_ID: done, wall=${WALL}s, batches_realized=${BATCHES_REALIZED}/${BATCHES}, equality=${EQ_VERDICT}"
  echo "$CELL_ID wall=${WALL}s batches_realized=${BATCHES_REALIZED}/${BATCHES} equality=${EQ_VERDICT}" >> "$PROGRESS"
done

# ---------------------------------------------------------------------
# 4. restart Memgraph, final snapshot
# ---------------------------------------------------------------------
log "restarting Memgraph (docker start memgraph)"
restart_memgraph_on_exit
sleep 3
snapshot >> "$HOSTLOG"
log "COARSE loop complete."

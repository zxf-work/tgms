#!/usr/bin/env bash
# T1 — same-host TGMS control for P-EXT1/P-EXT2 (Addendum EXT-A, reading
# notes A1/A2/A11). Runs on xzgpu, serially, one cell at a time, `nice`
# NOT used. See:
#   docs/design/OSDI27_AUDIT_AND_PLAN_2026-09-13.md §4.3b / §4.4 (lane T1)
#   docs/design/EXTERNAL_BASELINES_DESIGN_2026-10-02.md §2.7 (host protocol),
#   §4 (record layout)
# This is the exact loop the T1 lane ran on xzgpu on 2026-10-03; it is
# committed here for the record, not re-run from the laptop (the lane ran
# it directly on xzgpu over the committed `fdd393c` worktree).
#
# 19 cells: the 12 seed-0 main-grid cells (2 stores x {c1,c3,c4} x
# {none,deep}), the N=10,000 probe (synth-iv-60k/c1/none/seed0, "sum" TTF
# mode as committed), and the 6 A2 cells (2 stores x c3 x
# {recent,hours,days}, seed 0, 20 batches). Flags for the 12 main-grid
# cells and the probe reproduce storm-v2's own committed invocation
# (benchmarks/storm-v1/campaign.yaml `addendum_3`/`r18_probe`, confirmed
# against the committed records' own `config` blocks: addendum_id
# storm-v1-addendum-3, freeze_sha256 c85fb0c8...). The 6 A2 cells have no
# committed grid row (Addendum EXT-A item A2) and are run with the same
# harness and n_artifacts/batches convention, without an addendum stamp.
set -uo pipefail   # no -e: one cell's failure must be recorded, never abort the other 18

ROOT=/mnt/project/xzhang/tgms
WORKTREE="$ROOT/work/tgms-xz-fdd393c"
VENV_PY="$ROOT/venv/bin/python"
STORES_SRC="$ROOT/work/tgms/stores"
EXT="$ROOT/external-v1"
EXPORT="$EXT/export"
T1="$EXT/t1"
T1STORES="$EXT/t1-stores"
EQCHECK="$EXT/t1_equality_check.py"   # copied alongside this script

ADDENDUM_ID="storm-v1-addendum-3"
FREEZE_SHA256="c85fb0c8bb3b17e0b9f02a92a5ee0d5273298f43574583206582e5e5aa34d309"

export PYTHONPATH="$WORKTREE"

mkdir -p "$T1" "$T1STORES"

HOSTLOG="$T1/HOST-T1.log"
PROGRESS="$T1/PROGRESS.log"

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
log "T1 starting. pre-flight process check."
OTHER=$(ps -eo pid,cmd | grep -iE "bench_correction_storm|ext_export|export_storm_workload|ivm-dd|neo4j" | grep -v grep | grep -v "$$" || true)
if [ -n "$OTHER" ]; then
  log "ABORT: another TGMS/export/Neo4j process appears to be running; T1 must not overlap it:"
  log "$OTHER"
  exit 1
fi
log "pre-flight ok: no other TGMS/export/Neo4j process found."
snapshot >> "$HOSTLOG"

# ---------------------------------------------------------------------
# 1. stop Memgraph for the timed window (PI ruling 2026-10-02; found as
#    docker container 'memgraph', image memgraph/memgraph:latest,
#    127.0.0.1:7688->7687/tcp)
# ---------------------------------------------------------------------
log "stopping Memgraph (docker stop memgraph) for the timed window"
docker stop memgraph >> "$HOSTLOG" 2>&1
log "Memgraph stop issued. restart command recorded: 'docker start memgraph'."
snapshot >> "$HOSTLOG"

# ---------------------------------------------------------------------
# 2. scratch store copies + manifest upgrade (known trap: the shared
#    collegemsg copy is manifest format 1; the fdd393c engine opens it
#    read-only and every correction write raises, which the harness
#    reports as "mix starved" / 0 batches realized)
# ---------------------------------------------------------------------
for STORE in collegemsg synth-iv-60k; do
  if [ ! -d "$T1STORES/$STORE" ]; then
    log "copying $STORE -> $T1STORES/$STORE"
    cp -a "$STORES_SRC/$STORE" "$T1STORES/$STORE"
  fi
  log "upgrade-manifests on $T1STORES/$STORE (idempotent; no-op if already format 3)"
  "$VENV_PY" -m tgms.cli store upgrade-manifests --store "$T1STORES/$STORE" 2>&1 | tee -a "$HOSTLOG"
done

# ---------------------------------------------------------------------
# 3. the 19-cell grid (store|mix|age|n_artifacts|batches|measure_ttf|allow_r18|addendum)
#    collegemsg cells first so the manifest-upgrade fix is verified on the
#    one store that carries the known trap before the full run commits.
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
  "synth-iv-60k|c1|none|10000|5|sum|1|1"
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
  OUTDIR="$T1/$CELL_ID"
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
      --store "$T1STORES/$STORE" \
      --n-artifacts "$N" \
      --batches "$BATCHES" \
      --seed "$SEED" \
      --mix "$MIX" \
      "${AGE_FLAG[@]}" \
      --measure-ttf "$TTF" \
      --backend native \
      --burst-size 10000 --burst-after 10 \
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
docker start memgraph >> "$HOSTLOG" 2>&1
sleep 3
snapshot >> "$HOSTLOG"
log "T1 loop complete."

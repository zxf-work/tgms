#!/usr/bin/env bash
# N1 -- timed Neo4j 5.26 recompute grid (design memo
# EXTERNAL_BASELINES_DESIGN_2026-10-02.md secs 2.2/2.5/2.7, Addendum EXT-A
# run order X1 -> T1 -> N1 -> D1 -> C1). Runs on xzgpu, serially, one cell
# at a time, no `nice` (standing host rule for timed cells). Reads the 43
# cells of export/INDEX.json in that file's own key order, with the probe
# (synth-iv-60k-c1-none-n10000-s0) moved to run last. Never touches
# external-v1/t1/, external-v1/export/ or external-v1/t1-stores/ except to
# read them.
set -uo pipefail   # no -e: one cell's failure must be recorded, never abort the other cells

ROOT=/mnt/project/xzhang/tgms
EXT="$ROOT/external-v1"
EXPORT="$EXT/export"
N1="$EXT/n1"
PKG="$EXT/n1-work/neo4j-recompute"
PYRUN="$PKG/scripts/n1_run_cell.py"           # copied alongside this script
CHECK="$EXT/n1_external_check.py"             # copied alongside this script (scripts/external_check.py)
VENV_PY="$ROOT/venv/bin/python"
PYDEPS=/mnt/project/xzhang/neo4j/pydeps
PROBE="synth-iv-60k-c1-none-n10000-s0"
GIT_COMMIT="${N1_GIT_COMMIT:-unknown}"

mkdir -p "$N1"
HOSTLOG="$N1/HOST-N1.log"
PROGRESS="$N1/PROGRESS.log"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$HOSTLOG"; }

snapshot() {
  echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) =="
  uptime
  free -g
  ss -ltnp 2>/dev/null | grep -E "7474|7475|7687|7688" || echo "(no match on 7474/7475/7687/7688)"
  ps -eo user,pcpu,pmem,comm --sort=-pcpu | head -12
}

# ---------------------------------------------------------------------
# 0. pre-flight: confirm the box is free of other TGMS/export work
# ---------------------------------------------------------------------
log "N1 starting. pre-flight process check."
OTHER=$(ps -eo pid,cmd | grep -iE "bench_correction_storm|ivm-dd|export_storm_workload" | grep -v grep | grep -v "$$" || true)
if [ -n "$OTHER" ]; then
  log "ABORT: another TGMS/export process appears to be running; N1 must not overlap it:"
  log "$OTHER"
  exit 1
fi
log "pre-flight ok: no other TGMS/export process found."
LOAD1=$(awk '{print $1}' /proc/loadavg)
log "load average (1 min) = $LOAD1"
log "git sha (laptop checkout running from): $GIT_COMMIT"
log "git sha (xzgpu copy under $PKG): see HOST-N1.log entry below"
if [ -d "$PKG/.git" ]; then
  (cd "$PKG" && git rev-parse HEAD) 2>&1 | tee -a "$HOSTLOG"
else
  log "(xzgpu $PKG is a plain copy, not a git checkout -- sha256 of its .py files recorded instead)"
  find "$PKG" -name '*.py' -exec sha256sum {} \; | sort | tee -a "$HOSTLOG" > /dev/null
fi
java -version 2>&1 | tee -a "$HOSTLOG"
/mnt/project/xzhang/neo4j/neo4j-community-5.26.0/bin/neo4j --version 2>&1 | tee -a "$HOSTLOG"
log "APOC jar: $(ls -la /mnt/project/xzhang/neo4j/apoc-5.26.0-core.jar 2>&1)"
free -g | tee -a "$HOSTLOG"
snapshot >> "$HOSTLOG"

# ---------------------------------------------------------------------
# 1. stop Memgraph for the timed window (PI ruling 2026-10-02)
# ---------------------------------------------------------------------
log "stopping Memgraph (docker stop memgraph) for the timed window"
docker stop memgraph >> "$HOSTLOG" 2>&1
log "Memgraph stop issued. restart command recorded: 'docker start memgraph'."
docker ps >> "$HOSTLOG" 2>&1
snapshot >> "$HOSTLOG"

cleanup_restart_memgraph() {
  log "restarting Memgraph (docker start memgraph)"
  docker start memgraph >> "$HOSTLOG" 2>&1
  sleep 3
  snapshot >> "$HOSTLOG"
}
trap cleanup_restart_memgraph EXIT

# ---------------------------------------------------------------------
# 2. cell order: export/INDEX.json's own key order, probe moved to last
# ---------------------------------------------------------------------
CELL_ORDER=$("$VENV_PY" - "$EXPORT/INDEX.json" "$PROBE" << 'PYEOF'
import json, sys
idx = json.load(open(sys.argv[1]))
probe = sys.argv[2]
cells = [c for c in idx.keys() if c != probe]
cells.append(probe)
print("\n".join(cells))
PYEOF
)
NCELLS=$(echo "$CELL_ORDER" | wc -l | tr -d ' ')
log "cell order resolved: $NCELLS cells, probe ($PROBE) last"

# ---------------------------------------------------------------------
# 3. the grid
# ---------------------------------------------------------------------
i=0
while IFS= read -r CELL_ID; do
  i=$((i+1))
  CELL_EXPORT="$EXPORT/$CELL_ID"
  OUTDIR="$N1/$CELL_ID"
  mkdir -p "$OUTDIR"

  if [ ! -f "$CELL_EXPORT/artifacts.jsonl" ]; then
    log "cell $CELL_ID ($i/$NCELLS): no export bundle at $CELL_EXPORT -- SKIPPED"
    echo "$CELL_ID wall=0s exit=127 FAILED" >> "$PROGRESS"
    continue
  fi

  log "cell $CELL_ID ($i/$NCELLS): starting"
  snapshot > "$OUTDIR/host-snapshot-before.txt"

  DATA_DIR="$OUTDIR/neo4j-data"
  LOGS_DIR="$OUTDIR/neo4j-logs"
  CONF_DIR="$OUTDIR/neo4j-conf"
  CSV_DIR="$OUTDIR/csv"
  rm -rf "$DATA_DIR" "$LOGS_DIR" "$CONF_DIR" "$CSV_DIR"   # this cell's own dirs only, fresh per cell

  T0=$(date +%s)
  "$VENV_PY" "$PYRUN" "$CELL_EXPORT" \
      --out-dir "$OUTDIR" \
      --data-dir "$DATA_DIR" \
      --logs-dir "$LOGS_DIR" \
      --conf-dir "$CONF_DIR" \
      --csv-dir "$CSV_DIR" \
      --pkg-dir "$PKG" \
      --pydeps "$PYDEPS" \
      --git-commit "$GIT_COMMIT" \
      > "$OUTDIR/run-stdout.log" 2>&1
  EC=$?
  WALL=$(( $(date +%s) - T0 ))
  snapshot > "$OUTDIR/host-snapshot-after.txt"

  # belt-and-suspenders: never leave a server of ours running into the next cell
  NEO4J_CONF="$CONF_DIR" /mnt/project/xzhang/neo4j/neo4j-community-5.26.0/bin/neo4j stop >> "$OUTDIR/run-stdout.log" 2>&1 || true

  if [ $EC -ne 0 ]; then
    log "cell $CELL_ID: FAILED (exit $EC, wall ${WALL}s) -- see $OUTDIR/run.log and run-stdout.log"
    echo "$CELL_ID wall=${WALL}s exit=${EC} FAILED" >> "$PROGRESS"
    continue
  fi

  EQ_EXPORT=$("$VENV_PY" -c "
import json
idx = json.load(open('$EXPORT/INDEX.json'))
print(idx.get('$CELL_ID', {}).get('equality_level', 'unknown'))
")

  CHECK_OUT="$OUTDIR/check.json"
  "$VENV_PY" "$CHECK" "$CELL_EXPORT" "$OUTDIR/result.json" --out "$CHECK_OUT" \
      --config-kind neo4j > "$OUTDIR/check-stdout.log" 2>&1
  CHECK_EC=$?
  if [ $CHECK_EC -ne 0 ]; then
    log "cell $CELL_ID: external_check.py exited $CHECK_EC -- see $OUTDIR/check-stdout.log"
  fi

  BURSTS_N=$("$VENV_PY" -c "
import json
r = json.load(open('$OUTDIR/result.json'))
print(len(r['per_cell']['per_burst']) - 1)   # minus epoch 0
")
  AGREE=$("$VENV_PY" -c "
import json
r = json.load(open('$OUTDIR/result.json'))
print(r['gates']['oracle_agreement']['agree'])
")
  TOTAL=$("$VENV_PY" -c "
import json
r = json.load(open('$OUTDIR/result.json'))
g = r['gates']['oracle_agreement']
print(g['agree'] + g['disagree'] + g['not_answered'])
")

  log "cell $CELL_ID: done, wall=${WALL}s, bursts=${BURSTS_N}, oracle=${AGREE}/${TOTAL}, equality=${EQ_EXPORT}"
  echo "$CELL_ID wall=${WALL}s bursts=${BURSTS_N}/${BURSTS_N} oracle=${AGREE}/${TOTAL} equality=${EQ_EXPORT}" >> "$PROGRESS"
done <<< "$CELL_ORDER"

log "N1 loop complete."

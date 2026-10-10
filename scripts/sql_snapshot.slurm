#!/bin/bash
#SBATCH --job-name=sql-snapshot
#SBATCH --partition=bigTiger
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=01:30:00
#SBATCH --output=/project/xzhang12/tgms-lane-B/logs/sql-snapshot-%j.out

# SQL page/count snapshot stress test, protocol frozen in
# external_workloads/probe/SNAPSHOT_FREEZE.md. CPU only.
#   sbatch scripts/sql_snapshot.slurm
# Optional --export: PY (python with duckdb; pgserver for the postgres
# leg), REPO (checkout to run from).

set -u
REPO=${REPO:-/project/xzhang12/tgms-lane-B}
PY=${PY:-/project/xzhang12/tgms/.venv/bin/python}
[ -x "$PY" ] || PY=/project/xzhang12/tgms-arc5/.venv/bin/python
cd "$REPO" || exit 1

export LANG=C LC_ALL=C
export TMPDIR=/tmp/sqlsnap_$SLURM_JOB_ID
export XDG_RUNTIME_DIR=$TMPDIR/xdg
mkdir -p "$XDG_RUNTIME_DIR"
export PYTHONPATH=$REPO

echo "job $SLURM_JOB_ID on $(hostname) at $(date -u +%FT%TZ)"
echo "repo $(git -C "$REPO" rev-parse HEAD)  python $PY"

ENGINES=sqlite-wal,sqlite-rollback,duckdb
NOTRUN=()
if "$PY" -c "import pgserver" 2>/dev/null; then
  ENGINES=$ENGINES,postgres
  export TGMS_PG_TESTS=1
else
  NOTRUN=(--not-run "postgres=pgserver is not installed in $PY and no PostgreSQL server binaries exist on the run host")
fi

"$PY" -m pytest -q -p no:cacheprovider tests/test_sql_snapshot.py
echo "unit tests rc=$?"

"$PY" scripts/eval_sql_snapshot.py \
  --engines "$ENGINES" \
  --workdir "$TMPDIR/work" \
  --receipt benchmarks/results-v1/eval-sql-snapshot.json \
  "${NOTRUN[@]}"
RC=$?
rm -rf "$TMPDIR"
echo "done rc=$RC at $(date -u +%FT%TZ)"
exit $RC

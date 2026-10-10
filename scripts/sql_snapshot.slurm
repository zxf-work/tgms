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
#   sbatch --export=ALL,ENGINES=postgres,RECEIPT=benchmarks/results-v1/eval-sql-snapshot-pg.json scripts/sql_snapshot.slurm
# Optional --export: PY, REPO, ENGINES (default: every engine whose
# driver imports), RECEIPT, PYDEPS (a pip --target directory put on
# PYTHONPATH, with INSTALL_MANIFEST.json inside).

set -u
REPO=${REPO:-/project/xzhang12/tgms-lane-B}
PY=${PY:-/project/xzhang12/tgms-arc5/.venv/bin/python}
PYDEPS=${PYDEPS:-$REPO/.pydeps}
RECEIPT=${RECEIPT:-benchmarks/results-v1/eval-sql-snapshot.json}
cd "$REPO" || exit 1

export LANG=C LC_ALL=C
export TMPDIR=/tmp/sqlsnap_$SLURM_JOB_ID
export XDG_RUNTIME_DIR=$TMPDIR/xdg
mkdir -p "$XDG_RUNTIME_DIR"
export PYTHONPATH=$REPO
[ -d "$PYDEPS" ] && export PYTHONPATH=$REPO:$PYDEPS

echo "job $SLURM_JOB_ID on $(hostname) at $(date -u +%FT%TZ)"
echo "repo $(cat .git/HEAD)  python $PY  PYTHONPATH $PYTHONPATH"

EXTRA=()
if "$PY" -c "import pgserver" 2>/dev/null; then
  export TGMS_PG_TESTS=1
  DEFAULT_ENGINES=sqlite-wal,sqlite-rollback,duckdb,postgres
else
  DEFAULT_ENGINES=sqlite-wal,sqlite-rollback,duckdb
  EXTRA+=(--not-run "postgres=pgserver is not importable from $PY with PYTHONPATH $PYTHONPATH")
fi
ENGINES=${ENGINES:-$DEFAULT_ENGINES}
[ -f "$PYDEPS/INSTALL_MANIFEST.json" ] && EXTRA+=(--install-manifest "$PYDEPS/INSTALL_MANIFEST.json")

"$PY" -m pytest -q -rA -p no:cacheprovider --junitxml "$TMPDIR/junit.xml" \
  tests/test_sql_snapshot.py
echo "unit tests rc=$?"

"$PY" scripts/eval_sql_snapshot.py \
  --engines "$ENGINES" \
  --workdir "$TMPDIR/work" \
  --receipt "$RECEIPT" \
  --junit "$TMPDIR/junit.xml" \
  "${EXTRA[@]}"
RC=$?
rm -rf "$TMPDIR"
echo "done rc=$RC at $(date -u +%FT%TZ)"
exit $RC

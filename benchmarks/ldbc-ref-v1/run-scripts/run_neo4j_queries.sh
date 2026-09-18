#!/bin/bash
# RUNBOOK.md §5.3 — the vendored Cypher, verbatim, over the loaded reference.
#
# The runbook's own protocol is one execution per template (§9,
# `protocol.reps: 1`) because it is a *correctness* comparison. The paper also
# needs per-template wall times on both sides under the same protocol, so each
# template here runs **one warm-up execution followed by three timed
# executions**, back to back per template (not four passes over the whole set,
# which would let 23 other templates evict the page cache between a template's
# own reps). `ref-rows/` — the rows everything downstream compares against — is
# the FIRST TIMED execution; the warm-up and reps 2-3 land in their own
# directories and contribute wall times only.
#
# `ldbc_reference_run.py` executes each plan once per invocation, so the loop
# lives here rather than in the runner: the runner stays the verbatim-Cypher
# reference it is, and no flag of it was added for timing.
set -u
set -o pipefail

cd /mnt/project/xzhang/tgms/work/tgms
export TMPDIR=/mnt/project/xzhang/tgms/tmp
export PYTHONPATH=/mnt/project/xzhang/neo4j/pydeps
PY=/mnt/project/xzhang/tgms/venv/bin/python
OUT=benchmarks/ldbc-ref-v1

echo "RUN_STARTED ldbc-ref-v1-neo4j-queries commit=$(git rev-parse --short=12 HEAD) $(date -u +%FT%TZ)"

BI="BI3 BI4 BI6 BI7 BI9 BI10 BI11 BI12 BI17 BI18"
IV="IC2 IC5 IC6 IC8 IC9 IC11 IC12 IS1 IS2 IS3 IS4 IS5 IS6 IS7"

run_plan () {   # $1 = plan id, $2 = cypher dir
  local pid=$1 dir=$2
  for rep in warmup t1 t2 t3; do
    local out="$OUT/ref-rows-$rep"
    [ "$rep" = t1 ] && out="$OUT/ref-rows"
    echo "--- $pid $rep $(date -u +%FT%TZ)"
    $PY scripts/ldbc_reference_run.py \
        --params "$OUT/params.json" \
        --cypher-dir "$dir" \
        --out "$out" \
        --plan "$pid" 2>&1 | tail -3
  done
}

for pid in $BI; do
  run_plan "$pid" external_workloads/ldbc/bi/neo4j/queries
done
for pid in $IV; do
  run_plan "$pid" external_workloads/ldbc/interactive_v1/cypher/queries
done

# §6: the reference side only ever runs under the id BI6 (that is the only id
# resolving to bi-6.cypher). BI6.v2 is the same query, same parameters, same
# answer — make it available under the name ldbc_compare.py matches on.
for d in ref-rows ref-rows-warmup ref-rows-t2 ref-rows-t3; do
  [ -f "$OUT/$d/ref-BI6.json" ] && cp "$OUT/$d/ref-BI6.json" "$OUT/$d/ref-BI6.v2.json"
done

echo "NEO4J_QUERIES_DONE $(date -u +%FT%TZ)"

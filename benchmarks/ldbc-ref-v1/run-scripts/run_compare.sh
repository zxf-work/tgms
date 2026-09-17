#!/bin/bash
# RUNBOOK.md §8 — normalize, match, classify. BI6.v2 is the compared id for the
# BI6 template (§6); BI6.json's own record stays as the v1-defect evidence and
# is reported as a footnote, never as the template's row.
set -u
set -o pipefail

cd /mnt/project/xzhang/tgms/work/tgms
export TMPDIR=/mnt/project/xzhang/tgms/tmp
PY=/mnt/project/xzhang/tgms/venv/bin/python
OUT=benchmarks/ldbc-ref-v1
DATE=${1:-2026-09-17}

echo "RUN_STARTED ldbc-ref-v1-compare commit=$(git rev-parse --short=12 HEAD) $(date -u +%FT%TZ)"

echo "=== do the two sides agree on what they bound? (RUNBOOK §5.1) ==="
$PY /mnt/project/xzhang/neo4j/check_params_agree.py
echo "PARAMS_AGREE_RC=$?"

$PY scripts/ldbc_compare.py \
    --tgms-dir "$OUT/tgms-rows" \
    --ref-dir "$OUT/ref-rows" \
    --contracts tests/fixtures/ldbc_ref/contracts.json \
    --sort-keys "$OUT/sort_keys.yaml" \
    --plan BI3 --plan BI4 --plan BI6.v2 --plan BI7 --plan BI9 --plan BI10 \
    --plan BI11 --plan BI12 --plan BI17 --plan BI18 \
    --plan IC2 --plan IC5 --plan IC6 --plan IC8 --plan IC9 --plan IC11 --plan IC12 \
    --plan IS1 --plan IS2 --plan IS3 --plan IS4 --plan IS5 --plan IS6 --plan IS7 \
    --out "$OUT/compare-$DATE.json"

echo "COMPARE_RC=$? $(date -u +%FT%TZ)"

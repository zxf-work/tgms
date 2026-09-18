#!/bin/bash
# RUNBOOK.md §5.2 — the TGIR side, with --emit-rows. This invocation is also
# the pending P-SF1b re-run of record (the coordinator's brief), so its own
# record is kept, under a name that cannot collide with the existing one:
#   benchmarks/ldbc-ref-v1/tgms-campaign-ldbc-ref-v1.json
# whose manifest carries a `companion` field naming
# benchmarks/results-v1/ldbc-sf1-campaign-fmt3-interactive-2026-09.json
# (14/14 COMPLETED at 54dcab0). That record is NOT superseded or overwritten:
# it covers the interactive arm only, this one covers all 25 plans, and both
# used the same store totals, seed, csv_root and protocol.
#
# --params is a params ROOT DIRECTORY, not the exported params.json.
# ---------------------------------------------------------------------------
# RUNBOOK.md §5.2 says `--params benchmarks/ldbc-ref-v1/params.json`. That is
# wrong and it fails every plan: `tgir_ldbc_sf1.py` does not read params.json
# at all — run_one -> _load_bound -> ldbc_snb_params.bind(plan_id, params_root,
# ...) -> read_bi_first(params_root / "bi" / "ldbc-snb-bi-parameters-..." ), so
# a file path yields
#   NotADirectoryError: .../params.json/bi/ldbc-snb-bi-parameters-sf1-to-sf30000/parameters-sf1/bi-3.csv
# and BIND_FAILED for all 25 (observed 2026-09-17 18:44Z; each failure still
# cost 150-750 s because every plan runs in a child that opens the 3.7 GB store
# before binding). The two sides therefore share parameters by *determinism*,
# not by a shared file: same params root, same CAMPAIGN_SEED, same csv_root,
# same first-tuple rule. That identity is checked after the run rather than
# assumed -- see check_params_agree.py.
#
# --csv is kept for ALL plans, including the scored-BI ones. It cannot affect
# BI binding: ldbc_snb_params.bind() gates sampling on
#     if csv_root is not None and plan_id in IV_SOURCES:
# so for a BI plan the flag is inert, and the BI rows still bind to LDBC's own
# third-party parameters (which is what makes that arm scored). The BI
# BIND_FAILED rows above were the --params defect, not --csv.
set -u
set -o pipefail

cd /mnt/project/xzhang/tgms/work/tgms
export TMPDIR=/mnt/project/xzhang/tgms/tmp
PY=/mnt/project/xzhang/tgms/venv/bin/python
OUT=benchmarks/ldbc-ref-v1
PARAMS_ROOT=/mnt/project/xzhang/tgms/ldbc-sf1/params
CSV=/mnt/project/xzhang/tgms/ldbc-sf1/bi-sf1-composite-merged-fk/graphs/csv/bi/composite-merged-fk/initial_snapshot

echo "RUN_STARTED ldbc-ref-v1-tgms commit=$(git rev-parse --short=12 HEAD) $(date -u +%FT%TZ)"

$PY scripts/tgir_ldbc_sf1.py \
    --store stores/snb-sf1 \
    --params "$PARAMS_ROOT" \
    --sf sf1 --plan all \
    --csv "$CSV" \
    --emit-rows "$OUT/tgms-rows" \
    --out "$OUT/tgms-campaign-ldbc-ref-v1.json"

echo "TGMS_DONE exit=$? $(date -u +%FT%TZ)"

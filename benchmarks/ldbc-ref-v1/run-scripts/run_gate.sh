#!/bin/bash
cd /mnt/project/xzhang/tgms/work/tgms || exit 1
echo "RUN_STARTED same-data-gate commit=$(git rev-parse --short=12 HEAD) $(date -u +%FT%TZ)"
export TMPDIR=/mnt/project/xzhang/tgms/tmp
mkdir -p benchmarks/ldbc-ref-v1
/mnt/project/xzhang/tgms/venv/bin/python /mnt/project/xzhang/neo4j/same_data_gate.py \
  /mnt/project/xzhang/tgms/ldbc-sf1/bi-sf1-composite-merged-fk/graphs/csv/bi/composite-merged-fk/initial_snapshot \
  /mnt/project/xzhang/tgms/ldbc-sf1/bi-sf1-composite-projected-fk/bi-sf1-composite-projected-fk/graphs/csv/bi/composite-projected-fk/initial_snapshot \
  benchmarks/ldbc-ref-v1/same-data-gate.json
echo "GATE_EXIT=$? $(date -u +%FT%TZ)"

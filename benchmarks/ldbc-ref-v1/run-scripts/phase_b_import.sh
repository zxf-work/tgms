#!/bin/bash
cd /mnt/project/xzhang/neo4j || exit 1
echo "RUN_STARTED phase-b-import commit=$(cd /mnt/project/xzhang/tgms/work/tgms && git rev-parse --short=12 HEAD) $(date -u +%FT%TZ)"
t0=$(date +%s)
bash /mnt/project/xzhang/neo4j/run_import.sh 2>&1 | tee /mnt/project/xzhang/neo4j/import.log
rc=${PIPESTATUS[0]}
echo "IMPORT_RC=$rc IMPORT_WALL_S=$(( $(date +%s) - t0 )) $(date -u +%FT%TZ)"
sha256sum /mnt/project/xzhang/neo4j/import.log

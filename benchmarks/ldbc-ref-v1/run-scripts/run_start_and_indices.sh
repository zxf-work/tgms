#!/bin/bash
# RUNBOOK.md §4.3 (second half) — start the server, apply the vendored index
# DDL verbatim, wait for the indexes to come online, and record the loaded
# graph's own counts so the import can be checked against the same-data gate.
set -u
set -o pipefail

export JAVA_HOME=/mnt/project/xzhang/neo4j/jdk-21
export PATH=$JAVA_HOME/bin:$PATH
export TMPDIR=/mnt/project/xzhang/tgms/tmp
NEO4J_HOME=/mnt/project/xzhang/neo4j/neo4j-community-5.26.0
Q=/mnt/project/xzhang/tgms/work/tgms/external_workloads/ldbc/bi/neo4j/ddl/indices.cypher
SHELL_ARGS="-a bolt://127.0.0.1:7687 -u neo4j -p neo4j --format plain"

echo "RUN_STARTED ldbc-ref-v1-start-indices $(date -u +%FT%TZ)"
"$NEO4J_HOME"/bin/neo4j start

for i in $(seq 1 120); do
  if "$NEO4J_HOME"/bin/cypher-shell $SHELL_ARGS "RETURN 1;" > /dev/null 2>&1; then
    echo "BOLT_READY after ${i}s $(date -u +%FT%TZ)"; break
  fi
  sleep 1
done

echo "=== indices (vendored DDL, unmodified) ==="
t0=$(date +%s)
"$NEO4J_HOME"/bin/cypher-shell $SHELL_ARGS < "$Q"
echo "=== awaiting index population ==="
"$NEO4J_HOME"/bin/cypher-shell $SHELL_ARGS "CALL db.awaitIndexes(3600);"
echo "INDEX_WALL_S=$(( $(date +%s) - t0 ))"

echo "=== loaded graph counts (must match the same-data gate) ==="
"$NEO4J_HOME"/bin/cypher-shell $SHELL_ARGS \
  "MATCH (n) RETURN count(n) AS nodes;"
"$NEO4J_HOME"/bin/cypher-shell $SHELL_ARGS \
  "MATCH ()-[r]->() RETURN count(r) AS relationships;"
echo "=== per-label ==="
"$NEO4J_HOME"/bin/cypher-shell $SHELL_ARGS \
  "CALL db.labels() YIELD label CALL apoc.cypher.run('MATCH (:\`'+label+'\`) RETURN count(*) AS c',{}) YIELD value RETURN label, value.c AS count ORDER BY label;" \
  || echo "(apoc per-label count unavailable; not fatal)"
echo "=== per-relationship-type ==="
"$NEO4J_HOME"/bin/cypher-shell $SHELL_ARGS \
  "CALL db.relationshipTypes() YIELD relationshipType AS t CALL apoc.cypher.run('MATCH ()-[:\`'+t+'\`]->() RETURN count(*) AS c',{}) YIELD value RETURN t, value.c AS count ORDER BY t;" \
  || echo "(apoc per-type count unavailable; not fatal)"
echo "=== APOC present? (BI10 needs apoc.path.subgraphNodes) ==="
"$NEO4J_HOME"/bin/cypher-shell $SHELL_ARGS \
  "SHOW PROCEDURES YIELD name WHERE name = 'apoc.path.subgraphNodes' RETURN name;"

echo "START_INDICES_DONE $(date -u +%FT%TZ)"

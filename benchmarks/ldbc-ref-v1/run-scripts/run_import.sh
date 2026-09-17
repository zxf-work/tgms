#!/bin/bash
# RUNBOOK.md §4.3 — static-snapshot import of the SF1 BI composite-projected-fk
# CSVs into a fresh Neo4j 5.26.0, tarball-native (no Docker).
#
# Transcribed from external_workloads/ldbc/bi/neo4j/scripts/import.sh:67-97
# (same node/relationship list, same order, same flags) with the Docker path
# prefixes removed, plus two additions the runbook's sketch does not carry:
#
#   * <database> is OMITTED, exactly as import.sh omits it. Neo4j 5's usage
#     line shows it as a trailing positional, but picocli treats
#     `--relationships` as variadic `<files>...`, so a trailing `neo4j` is
#     parsed as one more CSV file of the LAST relationship group and the
#     import dies with "File 'neo4j' doesn't exist" (observed, 2026-09-17
#     18:28Z). The parameter defaults to `neo4j`, which is the database the
#     queries then run against.
#   * the CSV root is the HEADERLESS tree built by strip_headers.sh, not the
#     pre-audit tarball as extracted. The vendored README requires Datagen's
#     `header=false` output; the only SF1 composite-projected-fk artifact LDBC
#     publishes carries a header line in every part-file. See strip_headers.sh
#     for the full reasoning and for why `--auto-skip-subsequent-headers` does
#     not solve it. Row counts of the stripped tree were verified equal to the
#     same-data gate's per-type data-row counts before this import ran.
set -eu
set -o pipefail

export JAVA_HOME=/mnt/project/xzhang/neo4j/jdk-21
export PATH=$JAVA_HOME/bin:$PATH
export TMPDIR=/mnt/project/xzhang/tgms/tmp

NEO4J_HOME=/mnt/project/xzhang/neo4j/neo4j-community-5.26.0
H=/mnt/project/xzhang/tgms/work/tgms/external_workloads/ldbc/bi/neo4j/headers
C=/mnt/project/xzhang/tgms/ldbc-sf1/bi-sf1-composite-projected-fk-headerless/initial_snapshot

echo "RUN_STARTED ldbc-ref-v1-import $(date -u +%FT%TZ)"
echo "M8: only initial_snapshot/ is referenced; deletes/ and inserts/ are not touched."
echo "  (the source tarball's siblings, never read by this command nor copied"
echo "   into the headerless tree strip_headers.sh built:)"
ls /mnt/project/xzhang/tgms/ldbc-sf1/bi-sf1-composite-projected-fk/bi-sf1-composite-projected-fk/graphs/csv/bi/composite-projected-fk \
  | sed 's/^/    /'
echo "  (the headerless tree this import reads contains only:)"
ls "$C/.." | sed 's/^/    /'

# header file first, then every part file of that entity, comma-joined and
# sorted so the argument is byte-reproducible across runs.
g() {
  printf '%s/%s/%s.csv' "$H" "$1" "$2"
  find "$C/$1/$2" -type f -name 'part-*.csv*' | sort | sed 's/^/,/' | tr -d '\n'
}

"$NEO4J_HOME"/bin/neo4j-admin database import full \
    --id-type=INTEGER --ignore-empty-strings=true --bad-tolerance=0 \
    --delimiter '|' \
    --report-file=/mnt/project/xzhang/neo4j/import.report \
    --nodes=Place="$(g static Place)" \
    --nodes=Organisation="$(g static Organisation)" \
    --nodes=TagClass="$(g static TagClass)" \
    --nodes=Tag="$(g static Tag)" \
    --nodes=Forum="$(g dynamic Forum)" \
    --nodes=Person="$(g dynamic Person)" \
    --nodes=Message:Comment="$(g dynamic Comment)" \
    --nodes=Message:Post="$(g dynamic Post)" \
    --relationships=IS_PART_OF="$(g static Place_isPartOf_Place)" \
    --relationships=IS_SUBCLASS_OF="$(g static TagClass_isSubclassOf_TagClass)" \
    --relationships=IS_LOCATED_IN="$(g static Organisation_isLocatedIn_Place)" \
    --relationships=HAS_TYPE="$(g static Tag_hasType_TagClass)" \
    --relationships=HAS_CREATOR="$(g dynamic Comment_hasCreator_Person)" \
    --relationships=IS_LOCATED_IN="$(g dynamic Comment_isLocatedIn_Country)" \
    --relationships=REPLY_OF="$(g dynamic Comment_replyOf_Comment)" \
    --relationships=REPLY_OF="$(g dynamic Comment_replyOf_Post)" \
    --relationships=CONTAINER_OF="$(g dynamic Forum_containerOf_Post)" \
    --relationships=HAS_MEMBER="$(g dynamic Forum_hasMember_Person)" \
    --relationships=HAS_MODERATOR="$(g dynamic Forum_hasModerator_Person)" \
    --relationships=HAS_TAG="$(g dynamic Forum_hasTag_Tag)" \
    --relationships=HAS_INTEREST="$(g dynamic Person_hasInterest_Tag)" \
    --relationships=IS_LOCATED_IN="$(g dynamic Person_isLocatedIn_City)" \
    --relationships=KNOWS="$(g dynamic Person_knows_Person)" \
    --relationships=LIKES="$(g dynamic Person_likes_Comment)" \
    --relationships=LIKES="$(g dynamic Person_likes_Post)" \
    --relationships=HAS_CREATOR="$(g dynamic Post_hasCreator_Person)" \
    --relationships=HAS_TAG="$(g dynamic Comment_hasTag_Tag)" \
    --relationships=HAS_TAG="$(g dynamic Post_hasTag_Tag)" \
    --relationships=IS_LOCATED_IN="$(g dynamic Post_isLocatedIn_Country)" \
    --relationships=STUDY_AT="$(g dynamic Person_studyAt_University)" \
    --relationships=WORK_AT="$(g dynamic Person_workAt_Company)"

echo "IMPORT_DONE $(date -u +%FT%TZ)"

#!/bin/bash
# Produce the headerless `composite-projected-fk` layout the Neo4j BI
# implementation requires, from the pre-generated tarball, which is not.
#
# external_workloads/ldbc/bi/neo4j/README.md: "The Neo4j implementation expects
# the data to be in composite-projected-fk CSV layout, **without headers** and
# with quoted fields ... Files should not have headers as these are provided
# separately in the headers/ directory", produced by running Datagen with
# `--format-options header=false,quoteAll=true`. The only SF1
# composite-projected-fk artifact LDBC publishes (bi-pre-audit) carries a
# header line in every part-file, so it is the `header=true` flavour.
#
# `--auto-skip-subsequent-headers=true` does not rescue this: it skips a
# repeated copy of the *group's own* header, and the group's header is the
# typed one from headers/ (`creationDate:DATETIME|id:ID(Comment)|...`), which
# the part-file's untyped `creationDate|id|...` does not match. Observed:
#   InputException: ERROR in input ... line:0 ... in field creationDate:DATETIME:1
#   raw field value: creationDate ... Text cannot be parsed to a DateTime
#
# So the header line is dropped here, mechanically, producing exactly the bytes
# Datagen's `header=false` would have written. Nothing else is touched: no
# reordering, no re-typing, no re-encoding of any field, and the vendored
# headers/ files (which define every column's type) are the only description of
# the data the importer ever reads. Quoting is not reproduced and does not need
# to be: `--trim-strings` defaults to false on Neo4j 5.x (preserving the
# trailing spaces that were quoteAll's only stated purpose) and the node CSVs
# contain zero `"` characters.
set -eu
set -o pipefail

SRC=/mnt/project/xzhang/tgms/ldbc-sf1/bi-sf1-composite-projected-fk/bi-sf1-composite-projected-fk/graphs/csv/bi/composite-projected-fk/initial_snapshot
DST=/mnt/project/xzhang/tgms/ldbc-sf1/bi-sf1-composite-projected-fk-headerless/initial_snapshot

echo "RUN_STARTED strip-headers $(date -u +%FT%TZ)"
rm -rf "$DST"
mkdir -p "$DST"

cd "$SRC"
find . -type d -printf '%p\0' | xargs -0 -I{} mkdir -p "$DST/{}"

strip_one() {
  local rel=$1
  zcat "$SRC/$rel" | tail -n +2 | gzip -1 > "$DST/$rel"
}
export -f strip_one
export SRC DST

find . -type f -name 'part-*.csv.gz' -printf '%P\0' \
  | xargs -0 -P 8 -I{} bash -c 'strip_one "$@"' _ {}

echo "files_src=$(find "$SRC" -type f -name 'part-*.csv.gz' | wc -l)"
echo "files_dst=$(find "$DST" -type f -name 'part-*.csv.gz' | wc -l)"
echo "STRIP_DONE $(date -u +%FT%TZ)"

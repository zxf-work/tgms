# Vendored LDBC SNB query/schema/script trees

This directory vendors the query, schema, and shell-script text (no data,
no generated CSVs, no binaries) from two upstream LDBC Council
repositories, pinned exactly as recorded in `external_workloads/MANIFEST.yaml:59-72`:

| tree | upstream project | pinned commit |
|---|---|---|
| `bi/` | [`ldbc/ldbc_snb_bi`](https://github.com/ldbc/ldbc_snb_bi) | `47dd38b40844ecdb0e42e5a610c369535304786d` |
| `interactive_v1/` | [`ldbc/ldbc_snb_interactive_v1_impls`](https://github.com/ldbc/ldbc_snb_interactive_v1_impls) | `11db98cc2ba14c33492f6c0c34e68c8be7e22e5f` |

Only the subtrees `bi/neo4j/` and `interactive_v1/cypher/` (queries, DDL/DML,
CSV import headers, and the shell/Python driver scripts around them) are
vendored, each alongside its own project's top-level `LICENSE.txt` and
`NOTICE.txt`. Each upstream repository's `LICENSE.txt` states the **Apache
License, Version 2.0**, copyright Linked Data Benchmark Council; see
`bi/LICENSE.txt` / `bi/NOTICE.txt` and `interactive_v1/LICENSE.txt` /
`interactive_v1/NOTICE.txt` for the full text. No `.cypher` file has been
edited — `benchmarks/ldbc-ref-v1/RUNBOOK.md` §1.1's independence argument
(these queries must run unmodified against a reference Neo4j instance)
depends on that.

Excluded on purpose: `interactive_v1/cypher/test-data/` (generated CSV
fixtures and update streams — data, not query/schema/script text),
`interactive_v1/cypher/src/main/java/` and `pom.xml` (the compiled Java
driver harness, not needed by `scripts/ldbc_reference_run.py`, which talks
to Neo4j directly over the `neo4j` Python driver), and each tree's own
`.git/` directory (these were full upstream clones on disk; only their
working-tree text at the pinned commit is vendored here).

See `benchmarks/ldbc-ref-v1/RUNBOOK.md` for how this content is used (the
24-template LDBC reference-correctness campaign, OSDI'27 Claim C9) and
`external_workloads/MANIFEST.yaml` for the formal pin record.

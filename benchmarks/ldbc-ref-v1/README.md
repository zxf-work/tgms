# ldbc-ref-v1 — LDBC reference-correctness and performance run (executed)

**Run of record: 2026-09-17, host `xzgpu`.** This directory was prep-only until
this run; `RUNBOOK.md` is the how, `campaign.yaml` the pre-registered what, and
this file is what actually happened. The scoring against `campaign.yaml`'s
frozen predictions is appended there as `addendum_1`, never edited into the
frozen blocks.

> **This is not an LDBC Benchmark, this is not an implementation of an LDBC
> Benchmark, and nothing produced here is an LDBC Benchmark Result.**

It is a correctness comparison between TGIR's output and an independently
loaded, unmodified-query Neo4j reference, over the 24 templates TGIR can
express. LDBC material is used under CC-BY 4.0.

---

## Read this first: 7 of 24 templates have an invalid reference side

The Neo4j side of **BI4, BI9, BI11, BI12, IC2, IC5 and IC9** was run with
temporal parameters passed as ISO-8601 *strings* rather than as Cypher
temporal values. In Neo4j 5, `DATETIME > STRING` does not raise — it evaluates
to null, the predicate is never satisfied, and the query returns **zero rows,
silently**. Those seven templates' reference results are not evidence of
anything and are **excluded from every agreement figure below**, not scored as
disagreements.

The fix is written and tested (`scripts/ldbc_reference_run.py::driverize_params`,
reproducing LDBC's own `cast_parameter_to_driver_input`,
`external_workloads/ldbc/bi/neo4j/queries.py:45-47`), but it **could not be
executed**: the coordinator froze all compute on `xzgpu` at 2026-09-17 ~22:5xZ
for a 6-hour reader-storm soak, after the TGMS side finished and before the
reference side could be re-run. Re-running those seven templates is the single
outstanding item — see [Pending](#pending-needs-the-host).

---

## 1. Setup and versions

| component | version / value |
|---|---|
| Neo4j | Community **5.26.0** (tarball, no root, no Docker) |
| JDK | Temurin **21.0.12+8** |
| APOC | **5.26.0-core** (`apoc.path.subgraphNodes` confirmed present; BI10's only external dependency) |
| Neo4j Python driver | **5.28.6** (installed to an isolated `--target` dir, not into the shared venv) |
| Neo4j home | `/mnt/project/xzhang/neo4j/neo4j-community-5.26.0` |
| data / logs | `data-ldbc-ref/`, `logs-ldbc-ref/` (fresh; the existing eval instance was not touched) |
| bolt | `127.0.0.1:7687` (http moved to 7475; Memgraph holds 7688) |
| `neo4j.conf` sha256 | `b22fb1a80eac03284f4d046a7b6c3d4813e153bb2e7bde8e2c3c7b98583a6fac` |
| heap / pagecache / tx timeout | 8g / 16g / 600s (RUNBOOK §2.3, unmodified) |
| TGMS store | `stores/snb-sf1` (backend native, batch 250, compact-every 100000, write-path bulk) |
| repo commit | `0b7198dfdf7a` **+ uncommitted harness fixes** (§6) — stamped `-dirty` |
| host | 40 cores, 93 GB |

Reference dataset: `bi-sf1-composite-projected-fk` from
`https://datasets.ldbcouncil.org/bi-pre-audit/`. **md5: 1482/1482 files OK, 0
FAILED.** (The runbook's URL for the Neo4j tarball, `dist.neo4j.org/deb/…`,
returns `NoSuchKey`; the working path is `dist.neo4j.org/…` — byte-identical in
size to this host's already-validated 5.26.0 tarball.)

## 2. The same-data gate (RUNBOOK §4.2) — **PASSED**

Both serializations were proven to be the same generated SF1 dataset before any
query ran. Per node type: row count **and** the sha256 of the numerically
sorted id list. Per relationship type: row count, where the merged-fk side's
count for an FK-merged relationship is the number of non-empty values in the
node file's FK column.

```
nodes 2997352 / 2997352 (required 2997352)
edges 17196776 / 17196776 (required 17196776)
GATE PASSED
```

All 8 node types agree on count and id-digest; all 23 relationship types agree
on count. Record: `same-data-gate.json`, gate digest
`81b9b099f44c803e3b42c779e496107aa47a30be2cb4ce8590bd0051a18e7660`.

The runbook's own gate command does not exist — `scripts/build_snb_store.py`
has no `--csv-root` and no `--dry-run-fidelity` flag (its CSV flag is `--csv`
and it has no dry-run mode), and `tgms.data.snb_loader.fidelity()` reads a
*built store*'s label counts, with a header table pinned to the **merged-fk**
serialization, so it cannot read the projected-fk tree at all. The gate the
runbook *describes* was implemented directly against both CSV trees:
`run-scripts/same_data_gate.py`.

Cross-check: the loaded Neo4j graph's own per-label counts match the TGMS
store's `dataset_card.json` exactly (City 1343, Comment 1739438, Company 1575,
Continent 6, Country 111, Forum 100827, Organisation 7955, Person 10295,
Place 1460, Post 1121226, Tag 16080, TagClass 71, University 6380;
`Message` = 2860664 = Comment + Post, i.e. M6). Every relationship type matches
too, **except `KNOWS`: 173014 in Neo4j vs 346028 in the store** — the M7
doubling, which is the cause of the one genuine disagreement below (IS3).

## 3. Import (RUNBOOK §4.3)

**Wall: 42 s** (`IMPORT_WALL_S=42`; the importer's own figure, "IMPORT DONE in
36s 771ms"). Indices: **14 s**, 19 indexes, all `ONLINE`.

```
Imported:
  2997352 nodes
  17196776 relationships
  35076475 properties
Peak memory usage: 1.060GiB
```

Node and relationship totals match the same-data gate exactly.
Import log digest (RUNBOOK §9): sha256 of `logs/import.log` —
see `manifest-2026-09-17.json`'s `config.import_log_sha256`.

M8 holds: only `initial_snapshot/` was passed to `neo4j-admin`; `deletes/` and
`inserts/` were never referenced and were never copied into the tree the
importer read.

Two corrections to the runbook's §4.3 command were needed:

1. **The trailing `<database>` argument must be omitted.** Neo4j 5's usage line
   shows it as a trailing positional, but picocli treats `--relationships` as
   variadic `<files>...`, so `neo4j` is parsed as one more CSV file of the last
   relationship group:
   `java.lang.IllegalArgumentException: File 'neo4j' doesn't exist`.
   The parameter defaults to `neo4j`. `import.sh` omits it for the same reason.
2. **The CSV tree had to be made headerless.** The vendored README requires
   Datagen's `--format-options header=false,quoteAll=true` output; the only SF1
   `composite-projected-fk` artifact LDBC publishes carries a header line in
   every part-file. `--auto-skip-subsequent-headers=true` does *not* rescue it,
   because it skips a repeated copy of the *group's own* header, and the group
   header is the typed one from `headers/` (`creationDate:DATETIME|id:ID(Comment)|…`),
   which the part-file's untyped `creationDate|id|…` does not match:

   ```
   InputException: ERROR in input
     data source: .../initial_snapshot/dynamic/Comment/part-00000-...csv.gz, position:13, line:0
     in field: creationDate:DATETIME:1
     raw field value: creationDate
     original error: Text cannot be parsed to a DateTime
   ```

   `run-scripts/strip_headers.sh` drops the header line from each of the 66
   part-files into a parallel tree, producing exactly the bytes
   `header=false` would have written. **No LDBC file was modified**, and the
   stripped tree's per-type row counts were verified equal to the same-data
   gate's data-row counts before the import ran. Quoting was not reproduced and
   does not need to be: `--trim-strings` defaults to false on 5.x (preserving
   the trailing spaces that were `quoteAll`'s only stated purpose) and the node
   CSVs contain **zero** `"` characters.

## 4. Protocol

- **Neo4j**: each template ran **1 warm-up + 3 timed executions, back to back
  per template** (not four passes over the set, which would let 23 other
  templates evict the page cache between a template's own reps). `ref-rows/` —
  the rows everything downstream compares against — is the **first timed**
  execution; the warm-up and reps 2–3 are in `ref-rows-warmup/`, `ref-rows-t2/`,
  `ref-rows-t3/` and contribute wall times only. Each execution is its own
  `ldbc_reference_run.py` invocation, so `wall_s` includes connection setup but
  not process start.
- **TGIR**: `tgir_ldbc_sf1.py`'s own protocol — warmups 1, reps 3, and 1 rep
  when the guard is bypassed (which it is for the BI rows). `ms` is the
  reported figure, `ms_all` the per-rep list. Each plan runs in its own child
  process, so its campaign wall also includes a ~150–270 s store open that `ms`
  excludes.
- Ceiling: 600 s both sides (`db.transaction.timeout=600s`;
  `bypass_ceiling_s: 600` plus a 420 s store-open allowance on the TGIR side).

> The two sides' wall times are **not** comparable as a speed ratio: different
> rep counts, and the TGIR figure excludes a store open the Neo4j figure has no
> analogue for (the server was already up). They are reported per side and
> never divided.

Parameters: one draw, two consumers — seed `5724984519806421702`, first tuple
in LDBC's own file order, anchors for the characterization-interactive arm
sampled from the merged-fk corpus. The two runners do not share `params.json`
(the TGIR runner re-binds from the params root), so their agreement rests on
determinism; `run-scripts/check_params_agree.py` checks it rather than assuming
it.

## 5. Per-template results

`agree?` is the recorded verdict in `compare-2026-09-17.json`. `ms` is TGIR's
reported figure; `w/t1/t2/t3` are the Neo4j warm-up and three timed wall times
in **seconds**.

| template | TGIR rows | Neo4j rows | agree? | TGIR ms | N4J w | N4J t1 | N4J t2 | N4J t3 |
|---|---:|---:|:---|---:|---:|---:|---:|---:|
| BI3  | 20  | 20  | values identical¹ | 35310.1 | 1.111 | 1.052 | 1.111 | 1.063 |
| BI4  | 100 | 0   | **ref invalid**² | 77064.2 | 0.013 | 0.011 | 0.013 | 0.013 |
| BI6  | —   | 100 | **TIGR timeout**³ | — | 1.860 | 1.693 | 1.816 | 1.974 |
| BI7  | 100 | 100 | values identical¹ | 13052.2 | 0.061 | 0.051 | 0.067 | 0.067 |
| BI9  | 100 | 0   | **ref invalid**² | 105391.0 | 0.009 | 0.009 | 0.009 | 0.010 |
| BI10 | 100 | 100 | values identical¹ | 24465.1 | 0.313 | 0.293 | 0.245 | 0.196 |
| BI11 | 1   | 1   | **ref invalid**² | 38233.8 | 0.102 | 0.098 | 0.104 | 0.098 |
| BI12 | 166 | 1   | **ref invalid**² | 148135.0 | 0.028 | 0.027 | 0.025 | 0.025 |
| BI17 | 0   | 0   | yes (vacuous) | 71798.8 | 0.047 | 0.033 | 0.033 | 0.031 |
| BI18 | 20  | 20  | **yes** | 54327.4 | 0.529 | 0.502 | 0.485 | 0.507 |
| IC2  | 20  | 0   | **ref invalid**² | 6403.9 | 0.131 | 0.035 | 0.036 | 0.034 |
| IC5  | 0   | 0   | **ref invalid**² | 18871.5 | 0.350 | 0.185 | 0.178 | 0.180 |
| IC6  | 10  | 10  | **yes** | 59059.0 | 0.494 | 0.374 | 0.345 | 0.351 |
| IC8  | 11  | 11  | **yes** | 5500.2 | 0.079 | 0.014 | 0.014 | 0.014 |
| IC9  | 20  | 0   | **ref invalid**² | 47401.3 | 1.711 | 1.488 | 1.496 | 1.629 |
| IC11 | 0   | 0   | yes (vacuous) | 5043.8 | 0.089 | 0.007 | 0.008 | 0.009 |
| IC12 | 5   | 5   | yes, but⁴ | 20428.0 | 0.557 | 0.271 | 0.295 | 0.254 |
| IS1  | 1   | 1   | **yes** | 3588.3 | 0.054 | 0.010 | 0.012 | 0.012 |
| IS2  | 10  | 10  | values identical¹ | 6665.1 | 0.094 | 0.018 | 0.016 | 0.013 |
| IS3  | 48  | 24  | **NO — real** ⁵ | 687.7 | 0.065 | 0.047 | 0.026 | 0.011 |
| IS4  | 1   | 1   | **yes** | 120.8 | 0.041 | 0.007 | 0.009 | 0.010 |
| IS5  | 1   | 1   | **yes** | 4486.6 | 0.049 | 0.009 | 0.011 | 0.017 |
| IS6  | 1   | 1   | **yes** | 4271.6 | 0.092 | 0.010 | 0.011 | 0.009 |
| IS7  | 1   | 1   | **yes** | 8010.1 | 0.107 | 0.011 | 0.011 | 0.011 |

**Neo4j timed wall range: 0.007 s – 1.974 s.** (BI10, the template the design
memo flagged as most likely to approach the 600 s ceiling, ran in 0.29 s. No
reference-side timeout occurred anywhere.)
**TGIR range: 120.8 ms – 148135.0 ms**, over 23 completed plans.

¹ The two sides return **identical values** but under **different column
names**, so the recorded name-based verdict is `disagreeing`. See §5.1.
² Reference side invalid — temporal parameter passed as a string (see top).
³ BI6.v2 hit the 1020 s ceiling (600 s + 420 s store-open allowance); recorded
as `TIMEOUT`. BI6.json (the v1 artifact) `ERRORED`, as RUNBOOK §6 predicted it
still would — kept as evidence the v1 defect reproduces at SF1.
⁴ IC12's reference projects 5 columns and TGIR's plan projects 4 (`tagNames`
is missing). The comparator only compares the TGIR schema's columns, so the
extra reference column is invisible to it and the `agreeing` verdict is
over-generous. See §5.2.
⁵ The one genuine disagreement. See §5.3.

### 5.1 Column-name divergence (BI3, BI7, BI10, IS2 — and BI9, BI11, BI17, IC2, IC9, IS3)

`ldbc_compare.py`'s docstring says it compares "both sides by column *kind*,
never by column name", but `rows_match` looks columns up **by name on both
sides**. The reference side's names come from the vendored Cypher's own
`RETURN … AS` aliases; the TGIR side's from the plan artifact's projection.
They were never guaranteed to coincide, and for 10 templates they do not:

| template | TGIR columns | Neo4j columns |
|---|---|---|
| BI3  | `forumId, forumTitle, forumCreationDate, moderatorId, messageCount` | `forum.id, forum.title, forum.creationDate, person.id, messageCount` |
| BI7  | `relatedTagName, relatedTagCount` | `relatedTag.name, count` |
| BI10 | `candidateId, tagName, messageCount` | `expertCandidatePerson.id, tag.name, messageCount` |
| IS2  | `…, originalPostId, originalPostAuthorId, …` | `…, postId, personId, …` |

Every lookup on the reference side returns `None`, so every row faults. But the
**values are the same**. BI3's first row, side by side:

```
tgms: forumCreationDate 1278060831585000  forumId 274877932867  forumTitle "Wall of Abhishek Sharma"  moderatorId 4398046517450  messageCount 304
ref:  forum.creationDate  1278060831585   forum.id 274877932867  forum.title  "Wall of Abhishek Sharma"  person.id   4398046517450  messageCount 304
```

Pairing the two sides' declared column orders positionally and applying the
µs/ms rule, **BI3, BI7, BI10 and IS2 are byte-identical to the reference**
(`run-scripts/` triage, reproduced in the log). They are reported above as
"values identical" rather than as agreement, because making that the recorded
verdict needs a **column correspondence table** this lane did not invent:
positional pairing is *not* universally safe — IC8 has the same column names in
a **different order** (`commentId, commentCreationDate` vs
`commentCreationDate, commentId`), where name-based matching is right and
positional matching would be wrong. A correspondence table is a design decision
for the coordinator, not a mid-run improvisation (RUNBOOK's own rule against
explaining a row away, `campaign.yaml` falsifier b).

### 5.2 The µs/ms rule was not firing at all (fixed)

RUNBOOK §4.3 lists the µs vs ms divergence as acting at "compare time only",
via `ldbc_compare.py`'s `ts` column kind, which multiplies the reference side
by 1000. That kind exists and is tested — but nothing produced it.
`tgir_ldbc_sf1.py --emit-rows` writes each dump's `schema` from the plan
artifact's projection, where a property read carries no TGIR type, so every
temporal column arrived as `json?`, which `normalize_value` passes through
untouched.

IS1 is the clean demonstration: both sides returned the *same person* —
identical `firstName`, `lastName`, `gender`, `browserUsed`, `locationIP`,
`cityId` — differing only in `birthday` (383184000000000 vs 383184000000) and
`creationDate` (1273606356004000 vs 1273606356004), each exactly 1000×. The
five templates that agreed outright before this fix (BI18, IC6, IC12, IS5, IS6)
are precisely the five projecting no temporal column.

Fixed by generating the missing table mechanically rather than by hand — the
same discipline `scripts/ldbc_sort_keys.py` follows for `sort_keys.yaml`:
`scripts/ldbc_column_kinds.py` derives it from two frozen inputs that both
predate this run (each plan artifact's `Project` bindings, and
`tgms.data.snb_loader`'s `NODES`/`EDGES` property-kind tables). A column is
`ts` because the loader declares the property it reads to be a clock — **never
because its values happened to differ by 1000**. Output: `column_kinds.json`
(17 plans, 9 temporal columns). Applying it moved IC8, IS1, IS4 and IS7 from
"every row faults" to full agreement.

### 5.3 IS3 — the one genuine disagreement: `MAPPING-RULE` / M7 KNOWS doubling

TGIR returns **48** rows, Neo4j **24** — exactly 2×. The store holds
`KNOWS` 346028 = 2 × 173014, because `snb_loader` writes one CSV row as two
edge versions (`both_ways=True`, M7: "Cypher's KNOWS is undirected and the
pattern evaluator does not consult `directed`"). `interactive-short-3.cypher`
matches `-[:KNOWS]-` undirected, so each friendship is one reference row and
two TGIR rows.

RUNBOOK §4.3's M7 line pre-classifies this exactly: *"any KNOWS-count doubling
in a result is a defect to report, not a normalization to apply."* Reported
accordingly, and **not** normalized away. The prior P-SF1b record shows the
same 48 (`ldbc-sf1-campaign-fmt3-interactive-2026-09.json`), so this is a
standing property of the store's M7 encoding, not a regression from this run.

This is the one row-level finding of the campaign, and it is the reason the
pre-registered per-template prediction is scored **REFUTED** (see
`campaign.yaml` `addendum_1`).

## 6. Harness defects found by executing this runbook

Five, all in code paths no test covered. Each is fixed, linted and covered by a
new test; all are **uncommitted working-tree changes on the host checkout**
(hence the `-dirty` commit stamp) and are committed on the laptop worktree
alongside this record.

1. **`export_bindings` bound the two sides to different entities.** For the 14
   Interactive templates, `bind()` samples an anchor from the corpus while
   `_cypher_side` projected the *validation file's* id. Measured: IS1's
   validation id `32985348839299` matches no Person in the SF1 BI corpus; the
   sampled anchor `4398046520371` does. Uncorrected, every Interactive template
   would have asked Neo4j about a nonexistent person — zero reference rows and
   a guaranteed "disagreement" saying nothing about TGIR.
2. **`export_bindings` keyed the Cypher side by validation-file column names**
   (`personIdSQ1`, `messageIdContent`) while the vendored queries read
   `$personId`/`$messageId`. All 14 Interactive templates failed with
   `Neo.ClientError.Statement.ParameterMissing`. The rule differs by arm — a BI
   parameter CSV's columns *are* the query's `$names`, the validation file's are
   not — so the fix is per-arm, and is guarded by a new test that greps each of
   the 24 vendored queries for its own `$param` occurrences and requires the
   export to cover them.
3. **Temporal parameters reached the driver as strings** (the §top issue).
   Fixed in `ldbc_reference_run.py::driverize_params`; **not yet executed.**
4. **A ceiling hit aborted the whole campaign.** `TimeoutExpired.stdout` is
   bytes even when `subprocess.run` was given `text=True`, so the timeout
   handler raised `TypeError: startswith first arg must be bytes…`, escaped
   `run_child`, and killed the run 9 plans in with nothing written. A ceiling
   hit must be one recorded `TIMEOUT` row. `tests/test_tgir_ldbc_sf1.py` is new
   — this driver had no test file at all.
5. **The `ts` column kind never fired** (§5.2).

Plus one runbook correction that is not a code defect:

6. **RUNBOOK §5.2's `--params benchmarks/ldbc-ref-v1/params.json` is wrong.**
   `tgir_ldbc_sf1.py` never reads `params.json`; `--params` is a params **root
   directory** (`run_one` → `_load_bound` → `bind()` → `read_bi_first(params_root
   / "bi" / …)`). Given a file, every plan fails:

   ```
   NotADirectoryError: [Errno 20] Not a directory:
   'benchmarks/ldbc-ref-v1/params.json/bi/ldbc-snb-bi-parameters-sf1-to-sf30000/parameters-sf1/bi-3.csv'
   ```

   This cost one full run (all 25 `BIND_FAILED`, 150–750 s each because every
   plan opens the 3.7 GB store in a child before binding). `--csv` is **not**
   implicated: `bind()` gates sampling on `if csv_root is not None and plan_id
   in IV_SOURCES:`, so for a BI plan the flag is inert. Recommend correcting
   `RUNBOOK.md:408`.

## 7. Gates (`campaign.yaml`)

| gate | result |
|---|---|
| **G-R1** every attempted template produces a result or a recorded refusal, 0 silent skips | **PASS** — 24/24; BI6.v2's ceiling hit is a recorded `TIMEOUT` |
| **G-R2** manifest validates against `result_manifest.schema.json` | **PASS** — `check_result_manifest.py`: "conforms" |
| **G-R3** same-data gate passes before any query | **PASS** — §2 |

## Pending (needs the host)

Compute on `xzgpu` was frozen mid-run for a 6-hour reader-storm soak. These
remain:

1. **Re-run the Neo4j side for BI4, BI9, BI11, BI12, IC2, IC5, IC9** with
   `driverize_params` (fixed and tested, never executed). Until then those
   seven templates have no valid reference and are excluded from every
   agreement figure. `run-scripts/run_neo4j_queries.sh` re-runs all 24 in
   ~3 minutes with the server up.
2. **Re-run BI6.v2**, which timed out at 1020 s, or raise its allowance. Until
   then `campaign.yaml`'s `bi6_is_not_a_semantic_gap` prediction is vacuous,
   not confirmed.
3. **Ratify (or reject) a column correspondence table** for the 10 templates
   whose two sides use different column names (§5.1), then re-run
   `ldbc_compare.py`. Four templates (BI3, BI7, BI10, IS2) are byte-identical
   to the reference under positional pairing and would move to full agreement.
4. **Decide whether the comparator should fault a reference column the TGIR
   plan does not project** (IC12's `tagNames`, BI4's three missing columns).
   Today they are silently ignored.
5. **Re-run three engine-dependent tests on the host.** On the laptop worktree
   `tests/test_ldbc_compare.py` reports `105 passed, 3 failed`; all three
   failures are `ImportError: cannot import name '_engine' from 'tgms'` —
   the compiled extension is not built here and those three tests construct a
   fixture store (`test_rows_digest_stable_across_two_runs_against_a_tmp_fixture_store`,
   `test_rows_digest_against_a_tmp_fixture_store_changes_with_the_rows`,
   `test_emit_rows_output_is_directly_readable_by_ldbc_compare`). They are
   unrelated to the changes in §6 and passed on the host earlier in this run,
   but the `--column-kinds` change (§5.2) landed after the host was frozen and
   has only been exercised on the laptop. `ruff check tgms/ tests/ scripts/`
   passes clean.

Nothing in this run required the host after the freeze; no compute was started
on it after the coordinator's instruction, and the Neo4j server was stopped.

## Files

| file | what |
|---|---|
| `same-data-gate.json` | the §2 gate, per-type counts and id digests |
| `params.json` | the one binding, both sides, 25/25 plans |
| `column_kinds.json` | generated compare-time column kinds (§5.2) |
| `tgms-campaign-ldbc-ref-v1.json` | the TGIR side; **companion to** (never supersedes) `benchmarks/results-v1/ldbc-sf1-campaign-fmt3-interactive-2026-09.json` |
| `tgms-rows/`, `ref-rows/` | per-template row dumps (compared pair) |
| `ref-rows-warmup/`, `ref-rows-t2/`, `ref-rows-t3/` | the other three Neo4j executions (wall times only) |
| `compare-2026-09-17.json` / `.md` | the verdicts |
| `timings-2026-09-17.json` | per-template wall times, both sides |
| `manifest-2026-09-17.json` | the §9 manifest (validates) |
| `run-scripts/` | every script this run executed |
| `logs/` | import, gate, query, TGMS-run and header-strip logs |

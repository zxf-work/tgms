# ldbc-ref-v1 — LDBC reference-correctness and performance run (executed)

**Executed 2026-09-17/18 on host `xzgpu`.** This directory was prep-only until
this run; `RUNBOOK.md` is the how, `campaign.yaml` the pre-registered what, and
this file is what actually happened. The scoring against `campaign.yaml`'s
frozen predictions is appended there as `addendum_1` (interim),
`addendum_2` (column correspondence) and `addendum_3` (final) — appended, never
edited into the frozen blocks or into each other.

> **This is not an LDBC Benchmark, this is not an implementation of an LDBC
> Benchmark, and nothing produced here is an LDBC Benchmark Result.**

It is a correctness comparison between TGIR's output and an independently
loaded, unmodified-query Neo4j reference, over the 24 templates TGIR can
express. LDBC material is used under CC-BY 4.0.

---

## Revision of record: **2026-09-18**

`compare-2026-09-18.json` / `.md`, `timings-2026-09-18.json` and
`manifest-2026-09-18.json` are the revision of record. The `2026-09-17` files
are the **interim** record and are kept in place, not overwritten: they were
produced while 7 templates had an invalid reference side.

Those 7 (BI4, BI9, BI11, BI12, IC2, IC5, IC9) had their temporal parameters
reach Neo4j as ISO-8601 *strings*. In Neo4j 5, `DATETIME > STRING` does not
raise — it evaluates to null, the predicate is never satisfied, and the query
returns **zero rows, silently**. They were re-run on 2026-09-18 with
`ldbc_reference_run.driverize_params` (which reproduces LDBC's own
`cast_parameter_to_driver_input`), same protocol, same parameters, same TGIR
row dumps. The superseded reference dumps are kept under
`ref-rows-invalid-2026-09-17/`.

Four of the seven moved straight to full agreement (BI9 100/100, BI11 1/1,
BI12 166/166, IC9 20/20). Two more became comparable and are now reported on
their real content (BI4, IC5). **The seventh, IC2, surfaced a second instance
of the IS3 defect that had been invisible while its reference returned nothing**
— see §5.5.

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

## 5. Per-template results (revision of record, 2026-09-18)

Verdicts are `compare-2026-09-18.json`'s, with the ratified column
correspondence (§5.1) and column kinds (§5.2) applied. `ms` is TGIR's reported
figure; `w/t1/t2/t3` are the Neo4j warm-up and three timed wall times in
**seconds**.

**Counts (24 templates): 18 agree · 3 `reference-column-not-projected`
(BI4, IC5, IC12) · 2 disagree (IC2, IS3) · 1 timeout (BI6.v2) · 0 error ·
0 not-comparable.** Every template except BI6.v2 now has a valid reference.

**Row agreement over the 23 comparable templates: 678 / 736 = 0.921.**
Per group: **BI 606/607 = 0.998** (8 of 9 agreeing verdicts) ·
**IC 56/66 = 0.848** (4 of 7) · **IS 16/63 = 0.254** (6 of 7). The IS row ratio
is dragged down entirely by IS3's 47 duplicate rows; every other IS template
agrees exactly.

| template | TGIR rows | Neo4j rows | verdict | TGIR ms | N4J w | N4J t1 | N4J t2 | N4J t3 |
|---|---:|---:|:---|---:|---:|---:|---:|---:|
| BI3  | 20  | 20  | **agree** (positional) | 35310.1 | 1.111 | 1.052 | 1.111 | 1.063 |
| BI4  | 100 | 100 | **ref-column-not-projected**¹ | 77064.2 | 32.690 | 25.847 | 24.754 | 24.347 |
| BI6  | —   | 100 | **TGIR timeout**² | — | 1.860 | 1.693 | 1.816 | 1.974 |
| BI7  | 100 | 100 | **agree** (positional) | 13052.2 | 0.061 | 0.051 | 0.067 | 0.067 |
| BI9  | 100 | 100 | **agree** (positional) | 105391.0 | 16.797 | 2.120 | 2.230 | 2.340 |
| BI10 | 100 | 100 | **agree** (positional) | 24465.1 | 0.313 | 0.293 | 0.245 | 0.196 |
| BI11 | 1   | 1   | **agree** (positional) | 38233.8 | 1.356 | 0.678 | 0.655 | 0.663 |
| BI12 | 166 | 166 | **agree** (name) | 148135.0 | 13.330 | 8.958 | 8.793 | 8.595 |
| BI17 | 0   | 0   | agree (vacuous, 0/0) | 71798.8 | 0.047 | 0.033 | 0.033 | 0.031 |
| BI18 | 20  | 20  | **agree** (name) | 54327.4 | 0.529 | 0.502 | 0.485 | 0.507 |
| IC2  | 20  | 20  | **DISAGREE — MAPPING-RULE**³ | 6403.9 | 0.209 | 0.065 | 0.058 | 0.058 |
| IC5  | 0   | 0   | **ref-column-not-projected**¹ | 18871.5 | 0.351 | 0.164 | 0.165 | 0.158 |
| IC6  | 10  | 10  | **agree** (name) | 59059.0 | 0.494 | 0.374 | 0.345 | 0.351 |
| IC8  | 11  | 11  | **agree** (name)⁴ | 5500.2 | 0.079 | 0.014 | 0.014 | 0.014 |
| IC9  | 20  | 20  | **agree** (positional) | 47401.3 | 1.552 | 1.359 | 1.446 | 1.297 |
| IC11 | 0   | 0   | agree (vacuous, 0/0) | 5043.8 | 0.089 | 0.007 | 0.008 | 0.009 |
| IC12 | 5   | 5   | **ref-column-not-projected**¹ | 20428.0 | 0.557 | 0.271 | 0.295 | 0.254 |
| IS1  | 1   | 1   | **agree** (name) | 3588.3 | 0.054 | 0.010 | 0.012 | 0.012 |
| IS2  | 10  | 10  | **agree** (positional) | 6665.1 | 0.094 | 0.018 | 0.016 | 0.013 |
| IS3  | 48  | 24  | **DISAGREE — MAPPING-RULE**³ | 687.7 | 0.065 | 0.047 | 0.026 | 0.011 |
| IS4  | 1   | 1   | **agree** (name) | 120.8 | 0.041 | 0.007 | 0.009 | 0.010 |
| IS5  | 1   | 1   | **agree** (name) | 4486.6 | 0.049 | 0.009 | 0.011 | 0.017 |
| IS6  | 1   | 1   | **agree** (name) | 4271.6 | 0.092 | 0.010 | 0.011 | 0.009 |
| IS7  | 1   | 1   | **agree** (name) | 8010.1 | 0.107 | 0.011 | 0.011 | 0.011 |

**Neo4j timed wall range: 0.007 s – 25.847 s** (BI4's 25.8 s is the new
maximum: it does real work now that its `$date` binds as a datetime; BI10, the
template the design memo flagged as the likeliest to approach the 600 s ceiling,
still runs in 0.29 s). No reference-side timeout occurred anywhere.
**TGIR range: 120.8 ms – 148135.0 ms**, over 23 completed plans.

¹ §5.3. ² §5.6. ³ §5.4 (IS3) and §5.5 (IC2). ⁴ IC8 is the template that forbids
a blanket positional rule — §5.1.

### 5.0 BI4's one disagreeing row

BI4 is `reference-column-not-projected` (the TGIR plan projects 2 of the
reference's 5 columns), and on the 2 columns it does project **99 of its 100
rows agree exactly**. The single disagreeing pair is the *same person*
(`personId` 24189255819865) with a different count: TGIR **419**, reference
**435**. That is a genuine content difference, not a naming or unit artefact,
and it sits at the `LIMIT 100` boundary of a query whose shape is the
`REPLY_OF*0..` var-length carve the design memo §7 named as a residual risk.
It is left **UNTRIAGED** rather than assigned a cause: one row is too thin a
basis, and BI4 is not one of the risk-flagged templates, so a confident
classification here would be a guess. Worth a targeted follow-up.

### 5.1 The column correspondence (`column_map.yaml`)

The two sides name their columns differently: the reference's names are the
vendored Cypher's own `RETURN … AS` aliases (and Neo4j's default name for an
unaliased expression, e.g. `forum.id`), TGIR's are its plan's output schema.
`ldbc_compare.py` looked both up by the TGIR name, so every reference lookup
returned `None` and every row faulted — while the values were identical.

`scripts/ldbc_column_map.py` derives the correspondence from two frozen
inputs — each plan artifact's own `Plan.out_schema` (TGIR's static derivation,
substituted with the artifact's own frozen `params`, so it reads no run
output), and each vendored `.cypher`'s final top-level `RETURN` aliases in
order, parsed with `ldbc_sort_keys.py`'s own splitter and alias regex:

- **Rule A (by name)** where every reference column has a same-named TGIR
  column — 10 templates. This is the *only* correct rule for **IC8**, whose two
  sides carry the same six names in a **different order**
  (`…, commentId, commentCreationDate, …` against
  `…, commentCreationDate, commentId, …`): pairing positionally would compare a
  timestamp against an id.
- **Rule B (by position, kind-checked)** where names differ and the counts
  match — 11 templates, including BI3. A position is paired only if the two
  columns' derived kinds agree (`uid`/`ts`/`scalar`), so a mis-ordered pair
  cannot slip through.
- **Otherwise `unmatched`** — 3 templates (§5.3).

Applying it moved BI3, BI7, BI10 and IS2 from "every row faults" to full
agreement. `tests/test_ldbc_column_map.py` regenerates the table and asserts
byte equality, and pins IC8-by-name and BI3-by-position specifically.

### 5.2 The µs/ms rule was not firing at all

RUNBOOK §4.3 lists the µs vs ms divergence as acting at "compare time only",
via `ldbc_compare.py`'s `ts` column kind, which multiplies the reference side
by 1000. That kind exists and is tested — but nothing produced it.
`--emit-rows` writes each dump's `schema` from the plan, where a *property*
read carries no TGIR type, so every temporal column arrived as `json?`, which
`normalize_value` passes through untouched.

IS1 is the clean demonstration: both sides returned the *same person* —
identical `firstName`, `lastName`, `gender`, `browserUsed`, `locationIP`,
`cityId` — differing only in `birthday` (383184000000000 vs 383184000000) and
`creationDate` (1273606356004000 vs 1273606356004), each exactly 1000×.

`scripts/ldbc_column_kinds.py` generates the missing table from the plan
artifacts' `Project` bindings and `snb_loader`'s frozen property kinds. A
column is `ts` because the loader declares the property it reads to be a clock
— **never because its values happened to differ by 1000**.

### 5.3 `reference-column-not-projected` — BI4, IC5, IC12

The comparator only ever compares the columns the **TGIR** schema declares, so
a reference column outside it was invisible: the template could be scored
`agreeing` on a strict subset of the query's answer. That is now its own
verdict class rather than silent agreement:

| template | reference projects | TGIR does not project |
|---|---|---|
| IC12 | 5 columns | `tagNames` |
| BI4  | 5 columns | `personFirstName`, `personLastName`, `personCreationDate` |
| IC5  | 2 columns | `forumName` (TGIR projects `forumId`/`forumTitle` instead) |

IC12's five rows agree on the four columns that do correspond — that is
reported, not discarded — but the template is **not** recorded as agreeing.

### 5.4 IS3 — `MAPPING-RULE`, the M7 KNOWS double count

TGIR returns **48** rows, Neo4j **24** — exactly 2×, every friendship twice
with identical values (the TGMS dump's 48 rows carry only 24 distinct). The
store holds `KNOWS` 346028 = 2 × 173014, because `snb_loader` writes one CSV
row as two edge versions (`both_ways=True`, M7: "Cypher's KNOWS is undirected
and the pattern evaluator does not consult `directed`"). `IS3.json` then
expands `Expand(from=p, into=friend, dir="both", exact(1), rel_type=KNOWS)`
over that materialisation, so each friendship is traversed twice — once
forward, once as the already-materialised reverse edge read backwards.

Neither half is wrong alone; the composition double-counts. Filed as
`ops/failure_ledger.jsonl` **`D-090-is3-knows-both-ways-double-count`**, and
repaired the way BI6/BI6.v2 was: `IS3.json` is **not** edited — the frozen
artifact stays as the evidence — and `benchmarks/tgir-v1/plans/IS3.v2.json` is
added, identical in every respect but the expand's `dir`, which is `out`.
`IV_SOURCES` gains an `IS3.v2` row binding exactly as `IS3` does.

> **The repair depends on the store's encoding.** `dir="out"` is complete *iff*
> the store holds both directions. `scripts/build_ldbc_fixture.py` deliberately
> writes **one** edge per friendship ("Writing both directions would double
> every friend row and turn a fixture artefact into a plan defect"), so the
> existing LDBC fixture cannot reproduce this defect at all — `IS3.json` is
> already correct there, and `IS3.v2.json` would *under*-report. The regression
> test therefore builds a both-ways store of its own and pins **both** facts.
> The alternative repair — a `Distinct` on `(friendId, friendshipCreationDate)`
> above the expand — is correct under either encoding, at the cost of changing
> the plan's shape. If encoding-independence matters more than plan shape, that
> is the variant to take instead.

Classified `MAPPING-RULE` and **reported, not normalized away**, exactly as
RUNBOOK §4.3's M7 line requires. The prior P-SF1b record shows the same 48, so
it is a standing property of the encoding, not a regression from this run.

### 5.5 IC2 — a second instance of the same defect, newly visible

**This is new at 2026-09-18 and was invisible before.** While IC2's reference
returned 0 rows (the temporal-parameter defect), there was nothing to compare
against. With a valid reference, IC2 returns 20 rows on both sides — and
TGIR's 20 carry only **10 distinct `messageId`s**. The `LIMIT 20` hides the
doubling as a row count: TGIR's 10 real rows are duplicated to fill the limit,
so the reference's correct top-20 and TGIR's share exactly 10 rows.

Same root cause as IS3: `interactive-complex-2.cypher` matches
`(:Person {id: $personId})-[:KNOWS]-(friend)<-[:HAS_CREATOR]-(message)`, and
`IC2.json` expands `KNOWS` with `dir="both"` at `exact(1)` over the both-ways
materialisation, so every friend — and therefore every message — is reached
twice.

A survey of all 24 artifacts found **nine** expanding `KNOWS` with
`dir="both"` (BI10, IC2, IC5, IC6, IC9, IC11, IC12, IS3, IS7). Only the two
that project per-edge rows at `exact(1)` without an aggregate or `DISTINCT`
above them — **IS3 and IC2** — surface it as duplicate rows at SF1; the other
seven collapse the duplicates in their aggregation and agree exactly. So the
defect is latent in nine plans and observable in two.

**IC2 is not repaired here.** It needs its own `IC2.v2.json` plus the matching
binder row, which is the coordinator's call and out of this lane's ratified
scope. It is listed as pending and recorded in the ledger entry's note.

### 5.6 BI6.v2 — timeout at the pre-registered ceiling

BI6.v2 hit the 1020 s ceiling (600 s bypass + 420 s store-open allowance),
recorded as `TIMEOUT` and **not re-budgeted**. On the earlier `a6b3e94` build
the same artifact COMPLETED in 291,970.9 ms (~292 s measured, 638.4 s child
wall; `benchmarks/results-v1/ldbc-sf1-campaign-fmt3-2026-09.json`, store
`snb-sf1-xz-a6b3e94`), so this is a **build-to-build difference**: the same
plan over the same data went from ~292 s to over the ceiling. Worth a bisect;
out of scope for this lane. `BI6.json` (the v1 artifact) `ERRORED`, as
RUNBOOK §6 predicted — kept as evidence the v1 defect reproduces at SF1.

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

## Pending

1. ~~Re-run the Neo4j side for BI4, BI9, BI11, BI12, IC2, IC5, IC9~~ —
   **done 2026-09-18** with `driverize_params`, same protocol. All seven now
   have a valid reference; the superseded dumps are in
   `ref-rows-invalid-2026-09-17/`.
2. **BI6.v2's timeout** stands at the pre-registered ceiling, not re-budgeted
   (§5.6). Open question is the ~292 s → >600 s build-to-build regression,
   which wants a bisect.
3. ~~Ratify a column correspondence table~~ — **done** (§5.1).
4. ~~Decide whether the comparator should fault an unprojected reference
   column~~ — **done**: `reference-column-not-projected` (§5.3). The remaining
   question is TGIR-side: whether IC12's `tagNames`, BI4's three person columns
   and IC5's `forumName` should be added to those artifacts' projections.
5. **Repair IC2** — the second instance of `D-090` (§5.5). Needs an
   `IC2.v2.json` expanding one direction plus the matching `IV_SOURCES` row,
   mirroring `IS3.v2`. Not done here: out of this lane's ratified scope.
6. **Run `tests/test_ldbc_is3_v2.py`'s two executing tests on a checkout with
   the built engine.** On this laptop worktree `tgms._engine` is not built, so
   the four static tests pass and the two that build a store are **skipped and
   therefore unverified** — including the regression test named in the ledger
   entry. The same limitation hits three pre-existing tests in
   `tests/test_ldbc_compare.py`. `ruff check tgms/ tests/ scripts/` passes.
7. **Triage BI4's single disagreeing row** (§5.0), left UNTRIAGED on purpose.
8. **Repoint the paper macros to the 2026-09-18 revision.**
   `scripts/tgir_paper_macros.py:132-134` pins
   `{compare,timings,manifest}-2026-09-17.json` by name. Those files are
   deliberately kept, so that lane's tests still pass — but the macros are
   reading the **interim** record, in which 7 templates had an invalid
   reference and IC2's defect was invisible. Any paper table built from them
   today understates agreement and omits a real finding. Not changed here:
   `tgir_paper_macros.py` belongs to another lane.

### Provenance note — removed sync artefacts

15 untracked files whose names contained `" 2."` (iCloud sync duplicates:
8 under `logs/`, 7 under `ref-rows-t3/`) were removed on 2026-09-18 before
`SHA256SUMS.txt` was recomputed. Each was verified byte-identical to its
sibling (sha256) before deletion; none differed, and none was tracked by git.

## Files

| file | what |
|---|---|
| `same-data-gate.json` | the §2 gate, per-type counts and id digests |
| `params.json` | the one binding, both sides, 25/25 plans |
| `column_kinds.json` | generated compare-time column kinds (§5.2) |
| `column_map.yaml` | generated reference->TGIR column correspondence (§5.1) |
| `tgms-campaign-ldbc-ref-v1.json` | the TGIR side; **companion to** (never supersedes) `benchmarks/results-v1/ldbc-sf1-campaign-fmt3-interactive-2026-09.json` |
| `tgms-rows/`, `ref-rows/` | per-template row dumps (compared pair) |
| `ref-rows-warmup/`, `ref-rows-t2/`, `ref-rows-t3/` | the other three Neo4j executions (wall times only) |
| `compare-2026-09-18.json` / `.md` | **the verdicts (revision of record)** |
| `compare-2026-09-17.json` / `.md` | the interim verdicts, superseded, kept |
| `ref-rows-invalid-2026-09-17/` | the 7 superseded (string-parameter) reference dumps |
| `timings-2026-09-18.json` / `manifest-2026-09-18.json` | **revision of record** (both validate) |
| `timings-2026-09-17.json` / `manifest-2026-09-17.json` | interim, superseded, kept |
| `run-scripts/` | every script this run executed |
| `logs/` | import, gate, query, TGMS-run and header-strip logs |

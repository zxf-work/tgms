# External benchmarks: what LDBC SNB would cost, and what it would prove

**Status:** decision support, 2026-08-03. The decision it supports is D-050.
**Verdict:** do not run LDBC SNB as a benchmark. Publish this instead.

---

## 0. What this document is, and what its numbers are

Two kinds of statement live here and they are not interchangeable.

**Facts about LDBC** are taken from the published specification source
(`github.com/ldbc/ldbc_snb_docs`), the reference implementations, and
`ldbcouncil.org`. Every one is quoted verbatim with its URL. Where something
could not be established from public sources, §9 says so rather than filling
the gap.

**Costs** are estimates, and a cost estimate is a hypothesis, not a
measurement. Every one below carries its basis, using three labels:

| label | means |
|---|---|
| **measured** | a number this project recorded, with the record named |
| **derived** | arithmetic on a measured number, with the extrapolation stated |
| **judgement** | an opinion, anchored where possible on a comparable task this project actually did |

No cost in §6 is *measured* on LDBC data, because no LDBC data was loaded.
That is the honest boundary of this analysis: it prices a job nobody has
started, and the project's own history is that adaptation work finds
correctness defects before it finds costs (D-023, D-030, D-049). Treat the
session counts as lower bounds.

The classification in §4 is different: it is not an estimate. It is a
hand-audited table over the published query texts, produced by the same
instrument that produced the 110-question study's 24/110, and it is
committed as data (§10) so it can be disagreed with line by line.

---

## 1. The question

Should TGMS run an external, third-party-defined benchmark — and if so
which, at what cost, proving what?

The reason to want one is specific and real: **nothing this project has
built is defined by anyone outside it.** The thirteen-query registry is
ours. The datasets are ours or SNAP's. Even the 110-question study — the
closest thing here to an external yardstick — used questions written by
students the project recruited, over datasets the project chose, classified
by the project's own instrument. A reviewer may discount all of it as
self-serving, and would not be unreasonable.

LDBC SNB is the obvious candidate: it is the graph-database community's
standard, it is public, it has an audit programme, and `CAPABILITY_MATRIX.md`
currently reports a matrix in which no cell is `unsupported` — a claim whose
symmetric counterpart (what can the baselines express that we cannot?) has
never been measured.

---

## 2. What LDBC actually mandates

### 2.1 The workloads

SNB defines two workloads over one dataset.

**Interactive**, in two versions. From `interactive-v2.tex`:

> "There are 14 complex and 7 short read queries. Update operations include
> 8 inserts and, newly introduced in the interactive v2 workload, 8 deletes.
> The workload mix consists of approximately 8% complex read, 72% short
> read, 20% insert, and 0.2% delete operations."

> "The complex reads and the short reads are identical to the ones in
> interactive v1, except for query 14, which was replaced to cover the
> Cheapest path-finding choke point."

v1 is the only version an audit can currently be commissioned for:

> "As of January 2024, commissioning audits for this workload is not yet
> possible."

**Business Intelligence**: 20 read query templates in 28 variants, with
write batches over a 33-day period.

That is **41 distinct read templates** in total (IS1–IS7, IC1–IC14, BI1–BI20),
which is the population §4 classifies.

### 2.2 The schema, and its single clock

`data.tex`: *"Its graph schema has 11 concrete node types connected by 20
edge types."*

Timestamps are sparse. From `tables/table-relations.tex`, of the twenty
edge-type rows:

- **three** carry a `DateTime`: `knows`, `likes`, `hasMember`
  (`joinDate` in v1, renamed `creationDate` in v2);
- **two** carry a year-granularity integer: `studyAt.classYear`,
  `workAt.workFrom`;
- **fifteen** carry no attribute at all — `containerOf`, `hasCreator`,
  `hasInterest`, `hasModerator`, `hasTag` (×2), `hasType`, `isLocatedIn`
  (×4), `isPartOf` (×2), `isSubclassOf`, `replyOf`.

Nodes: `Person`, `Forum`, `Post` and `Comment` carry `creationDate`; `Tag`,
`TagClass`, `Place` and `Organisation` are static and carry no date at all.

There **is** a valid-time interval model in SNB — and it never reaches the
benchmark. `data.tex`, §Lifespan Management: *"For an entity x, creation
date denotes its creation date, while deletion date denotes its deletion
date."* That interval is serialized only in `raw` mode, and of `raw` mode the
specification says, verbatim:

> "This mode is not intended for use with any LDBC workload."

The serializers the workloads actually consume carry `creationDate` and no
`deletionDate`. The lifespan is compiled into a flat stream of inserts and
deletes; deletes are destructive and cascading. There is no tombstone, no
belief clock, no `as of`, and no correction. Grepping the specification
source for *bitemporal*, *valid time*, *transaction time*, *time travel* and
*as of* returns nothing in a temporal-semantic sense; the only "snapshot" in
the spec is snapshot **isolation**, which is concurrency control.

**This is the single most important fact in the analysis.** SNB's whole time
dimension is one creation timestamp per dynamic entity, on a graph that is
never corrected.

### 2.3 Datagen and scale

`ldbc_snb_datagen_spark` (Spark 3.2.x, Java 8/11, SBT; Docker images and EMR
scripts published). `data.tex`:

> "The currently available SFs are the following: 1, 3, 10, 30, 100, 300,
> 1000, 3000, 10000, 30000. Additionally, three small SFs, 0.003, 0.1, and
> 0.3 are provided to help initial testing and validation efforts."

Scale factors are the ASCII GiB size of the `csv-singular-merged-fk`
serialization. Pre-generated datasets are downloadable from
`datasets.ldbcouncil.org`, so nobody has to run Spark to get SF1.

Entity counts (`tables/table-number-of-entities-bi-initial.tex`), which drive
§6.2:

| | nodes | edges | **versions to load** |
|---|---:|---:|---:|
| SF1 | 2,997,352 | 17,196,776 | **20,194,128** |
| SF10 | 27,231,349 | 170,343,945 | **197,575,294** |
| SF30 | 78,244,709 | 505,722,361 | **583,967,070** |

For orientation: the largest store this project has ever built is 10M edge
versions.

### 2.4 What makes a result reportable

The fair-use policy (`ldbcouncil.org/benchmarks/fair-use-policies/`, "based
on our Byelaws") is unambiguous:

> "A result of a performance test can be fairly described as an 'LDBC
> Benchmark Result', if the test … **has been successfully audited by an
> LDBC-approved auditor**, and the result is reported as part of an LDBC
> Benchmark Results set, so it can be interpreted in context."

> "The same trademark is infringed by **any report or description of one or
> more performance test results which are not part of a set of LDBC
> Benchmark Results**, or in any other way states or implies that the
> results are endorsed by or originates from LDBC."

An audit requires a **complete implementation** (`auditing.tex`):

> "A benchmark result can be audited if it is a *complete implementation* of
> an LDBC benchmark workload."

with all operations (reads *and* updates), official data sets, and *"using
the official LDBC driver (if available)"*, whose *"parameter generation,
result recording, and workload scheduling parts … should not be changed."*
Validation is cross-implementation on SF10 (*"The scale factor 10 shall be
used as validation data set"*) and is a gate: the workflow does not proceed
unless results match. Audited runs use **SF30 or larger**. Interactive
requires the LDBC ACID test suite. Interactive runs need a 30–35 minute
warm-up, a measurement window of *"at least 2 hours … and at most 2 hours
and 15 minutes"*, and the **95% on-time requirement**. Audits *"can only be
commissioned by LDBC member companies by contracting any of the
GDC-certified auditors"*, with a **3,000 GBP** fee to GDC per audit.

**Subsets are not reportable results.** Nothing in the rules admits a
partial run: the audit definition requires the test to *"completely
exercise … all the mandatory requirements"*. What an unaudited or derived
run may do is stated explicitly, and it is generous:

> "If your work is derived from an LDBC Draft or standard Benchmark, **or is
> a partial implementation** … we would expect you to give attribution, in
> line with our Creative Commons CC-BY 4.0 licence."

> "We would also suggest that you make a statement … that includes one of
> these phrases **'This is not an LDBC Benchmark', 'This is not an
> implementation of an LDBC Benchmark' or 'These are not LDBC Benchmark
> Results'**."

So the honest ceiling on any unaudited work is a *derived* workload carrying
that disclaimer. This is also exactly what this project's own evaluation
plan already requires of itself, and it means the credibility on offer is
**"we applied a third-party-defined yardstick"**, never **"we posted an LDBC
score"**.

Two audit rules close the audited path structurally rather than
expensively:

1. **The ACID suite is not applicable to TGMS's architecture.** It tests ten
   isolation anomalies between concurrent transactions. TGMS has no
   transaction API: writes are batches under a single-writer contract,
   multi-*process* writers are undefined by design (D-028), and readers are
   `read_only=True` snapshots (D-049). There is no isolation level to test.
2. **BI forbids the only query surface TGMS has.** `auditing.tex`:
   *"General-purpose programming languages (eg C, C++, Java, Julia) are not
   allowed."* … *"Systems should use a domain-specific query language (eg
   Cypher, Gremlin, GQL, GSQL SQL/PGQ) for the implementation."*
   TGMS's surface is a typed Python operator API — the query façade that
   would have been a DSL is deferred indefinitely (D-038/D-043). Interactive
   is looser (*"may be implemented … as procedural code written in a
   general-purpose programming language (eg using the API of the
   database)"*), so the asymmetry is sharp: **the BI subset, the natural
   "cheap credibility" candidate, is the one whose rules exclude us.**

---

## 3. Is any standard benchmark actually temporal?

TGMS's subject is bi-temporality. If the standard benchmark has no notion of
a correction or a belief state, that governs everything else. It does not.

| benchmark | time model |
|---|---|
| LDBC SNB Interactive v1 | static graph + creation timestamps; insert-only |
| LDBC SNB Interactive v2 / BI | + destructive, cascading deletes |
| LDBC FinBench | event-time windows + **monotonic-timestamp path constraints** — the richest temporality in LDBC — still one clock, destructive deletes |
| LDBC LSQB | static. README: *"date/string operations … are out of scope for this benchmark"* |
| LDBC Graphalytics | static; six classical kernels |
| TGB / TGB 2.0 | one event timestamp per edge; a **machine-learning** benchmark (link/node property prediction, filtered MRR) — no SUT, no queries |
| SNAP temporal networks | datasets, not a benchmark; one timestamp per edge |
| **TPC-BiH** | **genuinely bi-temporal — and relational** |

TPC-BiH (Kaufmann et al., TPCTC 2013) is the only benchmark found that has
two clocks. Its abstract:

> "The cost of keeping and querying history with novel operations (such as
> time travel, temporal joins or temporal aggregations) is not adequately
> reflected in any existing benchmark."

It has a *Manipulate Order Data* update scenario that changes a value *"while
keeping the application times (i.e., trying to hide this change)"* — a
retroactive correction, the exact event TGMS exists for. It is TPC-H-shaped
and relational, it is a TPCTC **proposal** rather than a ratified TPC
standard, and no public, currently-downloadable generator or reference
implementation was found.

**The intersection of "graph benchmark" and "bi-temporal" is, publicly,
empty.** The strongest evidence is that the people who needed one built
their own: AeonG (PVLDB 17(7), the leading temporal graph database, and
transaction-time only) reports in §7.1.2 that *"T-LDBC derives from LDBC …
by incorporating 'FOR TT AS OF t' into the LDBC IS queries (IS1-IS7)"*. An
author hand-bolting an as-of clause onto seven Interactive Short queries is
direct evidence that LDBC ships no as-of query and that nothing off the
shelf was available. The bi-temporal property-graph *data model* was still
being proposed at ADBIS 2025; a benchmark is downstream of that and has not
appeared.

---

## 4. Fit, measured the way this project measures fit

### 4.1 Instrument

The same one that produced the 110-question study: classes 1 (directly
expressible), 2 (expressible by operator composition), 3 (requires an
unimplemented capability, with named `need` tags). Classes 4 (ambiguous) and
5 (not a computation) cannot arise — every LDBC template is well-posed and
ships a reference implementation.

The tag vocabulary is the study's (`G`, `AR`, `PROP`, `CAL`, `SET`, `NEG`,
`GLOB`, `SEQ`) plus two this workload needs and CollegeMsg/Bitcoin-OTC never
did:

- **`PAT`** — a labelled multi-way structural pattern, beyond the fixed
  five-shape motif catalogue and the untyped k-hop expansion of
  `snapshot_subgraph`. (`eval_semantics.md` §6: *"Fixed motif catalogue.
  Five shapes, not arbitrary pattern matching."*)
- **`SP`** — shortest-path length, all-shortest-paths, or a weighted
  cheapest path. `temporal_paths` caps at six hops and ranks by
  `(arrival, hops, edge sequence)`; `temporal_reachability` returns earliest
  arrival, not distance. Neither reports a hop count as its answer and
  neither accepts edge weights.

Query shapes were read from the official reference implementations
(`ldbc_snb_interactive_v1_impls/cypher/queries`,
`ldbc_snb_bi/neo4j/queries`). Interactive v1 is classified because it is the
auditable version; where v2 differs materially (IC14) the record says so.

**The mapping the verdicts assume**, since a fit verdict is meaningless
without one: node → node version with `uid = "<Type>:<id>"`, `label` = the
LDBC type, `props` = the remaining attributes, `vt_s` = `creationDate`;
edge → edge version with `rel_type` = the LDBC edge type. For the fifteen
edge-type rows with no attribute, valid time is **invented** by the adapter;
the classification charitably assumes the best available choice and never
penalises a query for it.

### 4.2 Result

**3 of 41 read templates are expressible: IS1, IS4, IS5.**

| workload | expressible |
|---|---|
| Interactive Short (IS1–IS7) | **3 / 7** |
| Interactive Complex (IC1–IC14, v1) | **0 / 14** |
| Business Intelligence (BI1–BI20) | **0 / 20** |

- **IS4** (content of a message) is class 1 — one `entity_history` call;
  `creationDate` is the version's `vt_s` and `content`/`imageFile` are its
  props.
- **IS1** (profile of a person) is class 2 —
  `entity_history(include_edges)` plus `compute filter(rel_type eq
  IS_LOCATED_IN)`; the returned `cityId` is the City uid.
- **IS5** (creator of a message) is class 2 — the incident `HAS_CREATOR`
  edge gives the creator uid, a `$ref` binds that scalar into a second
  `entity_history`.

All three are point lookups on an anchor.

Missing capabilities across the 38 class-3 templates (multi-tagged):

| tag | count | what it is |
|---|---:|---|
| `PAT` | 35 | labelled multi-way pattern matching |
| `PROP` | 33 | property predicates and arbitrary attribute projection |
| `AR` | 16 | arithmetic beyond count/sum/min/max/topk |
| `G` | 15 | grouping past two dimensions, or over a JSON property |
| `SET` | 13 | set operations and joins between result sets |
| `NEG` | 6 | absence conditions |
| `SP` | 6 | shortest / all-shortest / cheapest paths |
| `CAL` | 5 | calendar semantics |

`PAT` and `PROP` together gate **37 of the 38**. Two structural facts do most
of that work, and neither is a missing feature so much as a design position:

1. **The scan signature is closed** (`eval_semantics.md` §6: *"No general
   predicate pushdown."*). Node and edge properties are untyped JSON above
   the storage layer; no operator filters on them, and only
   `entity_history` — which takes a *single* uid — projects them at all.
   `snapshot_subgraph` returns nodes as `{uid, label, hop}`; `_edge_rows`
   returns no props.
2. **There is no iteration primitive.** The plan DAG's `$ref` binds scalars
   and list projections, but there is no map. So any template whose answer
   projects per-row attributes of a *set* of entities is blocked — which is
   why even IS3 (friends of a person, with their names) fails while IS5 (one
   creator, with their names) passes.

The three easiest interesting near-misses are worth naming, because they
mark where a small capability would pay:

- **BI11** (friend triangles). `M_triangle_cyclic` and
  `M_triangle_acyclic_1` between them cover both orientation classes of a
  triangle, and the date window is a valid-time window. It still fails: the
  country restriction is a typed two-hop pattern, `node_filter` caps at
  10,000 uids against 68,673 Persons at SF10, and the δ-motif contract
  counts *time-ordered* instances under a span bound rather than undirected
  triangles.
- **BI2** (tag evolution). Per-tag counts in one window *are* an
  `aggregate_events` grouping by `dst` endpoint. What is missing is the
  per-tag join across two windows, `abs()`, the `HAS_TYPE` restriction, and
  the tag-name projection.
- **IS3** (friends of a person). Everything but the per-friend names.

### 4.3 What the bi-temporal machinery would sit out

- **0 of 41** templates reference a second clock. `as_of_tt` would be
  `OPEN_END` in every call, permanently. The belief clock — the thing this
  system is *for* — is exercised zero times by the workload.
- **19 of 41** carry a predicate on a temporal attribute at all (generously
  counted: IC10's birthday window and IC7's latency arithmetic are
  included). The other 22 never look at time.
- In all 19, the attribute is a **creation timestamp of an entity that is
  never corrected**. There are no closed valid-time intervals in the
  benchmark-facing data: every dynamic entity maps to `[creationDate,
  OPEN_END)`. The interval machinery — half-open containment, overlap joins,
  interval-splitting corrections — degenerates. `deletionDate` exists only
  in `raw` mode, which the spec excludes from every workload (§2.2).
- The `all_current` fast path (D-028 #7) would therefore be live for the
  whole run, which is the configuration D-040 measured as **costing nothing**
  relative to a current-only store. LDBC would be measuring TGMS with its
  distinguishing feature switched off.

To exercise corrections at all, the adapter would have to *synthesize* a
correction history over LDBC's topology — which the project's evaluation
plan already anticipates. But then the third-party-definedness, the entire
reason to use LDBC, is gone for exactly the dimension the project is about.
Measuring our own generator over someone else's topology is a topology
result, not a benchmark result.

---

## 5. The priced adaptation

The unit is an **engineering session**: the unit of work that produced one
of this project's existing evaluation deliverables. The calibration anchor
is public and inspectable — one baseline system's loader plus its thirteen
registry query implementations, verified by canonical hash before being
timed, is `scripts/pg_baseline.py` + `scripts/pg_queries.py` (14 KB + 33 KB),
and the same shape again for ClickHouse, Neo4j and Memgraph. Call that
**one system ≈ one to two sessions** for a two-entity-type schema on an
existing harness.

| # | category | estimate | basis |
|---|---|---|---|
| A | Schema and data mapping | **2–3 sessions** | *judgement*, anchored on the four baseline loaders (1.9–14.4 KB each), each of which mapped **2** entity types; LDBC has **31** (11 node + 20 edge) |
| B | Loading, SF1 | **~8 min**; ~0.50 GB on disk | *derived* — 20,194,128 versions at the measured 42.4k ev/s (`eval_writes.md`, 1M, xzgpu) and 24.6 B/row (`eval_phase0.md`) |
| B′ | Loading, SF10 | **~78 min**; ~4.9 GB | *derived*, same rates, 19.8× beyond the largest store measured |
| B″ | Loading, SF30 (audit floor) | **~3.8 h**; ~14.4 GB | *derived*, 58× beyond it |
| C | Memory at SF10+ | **unknown — the main technical risk** | *measured* only to 10M rows: VmHWM 5.93 GB uncapped, 1.76 GB under a 2 GB budget after D-041 (`eval_resources.md` §14.2, §18). SF10 is 19.8× that row count. Nothing here supports a projection |
| D | Implementing the 3 expressible queries | **< 1 session** | *judgement*, anchored on 13 queries/system in the six-system matrix — and these three are point lookups |
| E | The 38 inexpressible ones — skip | **0 sessions** | the deliverable would be a table of 38 `unsupported` cells, which §4 already is |
| E′ | The 38 inexpressible ones — build | **the project's whole remaining direction** | *judgement*: `PAT` + `PROP` gate 37 of 38, and jointly they are general pattern matching plus predicate pushdown and arbitrary projection — i.e. the query façade deferred indefinitely by D-043, *plus* an optimizer it did not include |
| F | Validation against LDBC's expected results | **not available** | *fact*: validation is cross-implementation over the whole workload on SF10 and gates the workflow; there is no per-query validation path for a 3-of-41 implementation. Gold would have to be ours, which is the credibility we were buying |
| G | Harness and protocol (audited) | **structurally closed** | *fact*: complete implementation + official driver + ACID suite + SF30 + member-company sponsor + 3,000 GBP. TGMS has no transaction API for the ACID suite (§2.4) and no DSL for BI (§2.4) |
| G′ | Harness and protocol (derived, unaudited) | **1–2 sessions** | *judgement*: parameter curation, a `_queries.py` in the existing baseline shape, disclaimer and attribution copy |
| H | Ongoing maintenance | **recurring, and the target moves** | *observed*: v1→v2 replaced IC14 outright, changed IC2's date bound from `<=` to `<`, and renamed `hasMember.joinDate` to `creationDate`; Datagen exists in a legacy Hadoop and a current Spark line |

**Minimum viable LDBC-derived artifact** — SF1, three Interactive Short
queries, plus their free `as_of_tt` twins in the AeonG "T-LDBC" style —
is **A + B + D + G′ ≈ 4–6 sessions**, and it delivers three point-read
latencies. The dominant cost is A, the mapping, which buys the *dataset* and
is independent of which queries run on it.

---

## 6. What it would and would not prove

**Would prove.** That TGMS can ingest a third-party-defined graph at a
third-party-defined scale, and answer the queries it can answer, at a stated
latency, under a workload nobody here designed. That is a real and
currently-missing thing.

**Would also prove, whether or not we wanted it to.** That the operator
algebra expresses 3 of 41 templates from the graph community's standard read
workload. §4 already establishes that at zero further cost. Running it does
not strengthen the finding; it only adds latencies for three point lookups.

**Would not prove anything about the system's subject.** 0 of 41 templates
touch a second clock; 22 of 41 touch no clock at all; and in the 19 that do,
the timestamp belongs to an entity that is never corrected. A months-long
adaptation would measure an adapter on a workload that never exercises
corrections, belief states, or as-of queries — with the `all_current` fast
path live throughout, i.e. in precisely the configuration D-040 measured as
costing nothing.

**Would not be an LDBC result.** Not "not audited" — *not reportable*, by the
fair-use rules quoted in §2.4, and required to carry the phrase "This is not
an LDBC Benchmark".

### Subsets

- **Interactive Short only** — the cheapest slice and the one with the best
  fit (3/7), and it has an academic precedent in AeonG's T-LDBC. It is also
  seven point lookups; the resulting table would say TGMS is fast at point
  lookups, which `eval_phase0.md` already says at 0.1 ms against Neo4j's 9.7.
- **BI read queries only** — the workload whose *shape* (time-window
  grouping and aggregation) best matches where D-044/D-046/D-047 just did
  the work. Fit is **0/20**, and BI's audit rules forbid a general-purpose
  programming language for queries, which is TGMS's only surface. This is the
  subset that looks most attractive and is in fact the least available.

### Alternatives, ranked by credibility per unit of work

1. **Publish §4.** A third-party-defined yardstick, applied honestly,
   reporting a number that is bad for us. Marginal cost: this document.
2. **LDBC SNB SF1 as a *dataset*.** Buys the topology diversity the roadmap
   wants (11 node types, 20 edge types, heavy skew, real text) with no query
   claim and no fair-use exposure. Cost: A + B ≈ 2–3 sessions. Worth doing
   *only if* the ≥20M-row memory question (C) is being answered anyway,
   because that is what it actually tests.
3. **SNAP temporal streams at scale** — `sx-stackoverflow` (63M temporal
   edges), `wiki-talk-temporal`. `tgms/data/loaders.py` already reads this
   format; each new dataset is ~15 lines. Real skew, real event rates, real
   scale, zero adaptation. Cheapest scale-and-skew evidence available.
4. **A fresh independent-question cohort in a new domain.** Buys *more
   evidence about the thing the system is for* — whether people ask
   questions needing two clocks — which no external graph benchmark can
   supply, because none is bi-temporal (§3). It does **not** buy
   LDBC-flavoured external credibility, and this document should not pretend
   it does: the cohort is still recruited by us.
5. **TPC-BiH.** The only genuinely bi-temporal benchmark located. Relational,
   TPC-H-shaped, unratified, with no generator found. Not runnable, but it is
   the right *citation* for the claim that no graph benchmark covers this
   ground, and its "hide this change" update scenario is a well-formed
   external definition of the correction workload that could be transposed.

---

## 7. Recommendation

**Do not run LDBC SNB. Publish this analysis as the deliverable.**

Concretely:

1. **No** to an audited run: structurally closed, not merely expensive — no
   transaction API for the ACID suite, no DSL for BI, member-company
   sponsorship, SF30 (584M versions, 58× the largest store ever built here).
2. **No** to an unaudited full or subset run: 3/41 expressible, 0/41
   touching a second clock, and the result may not be called an LDBC result.
3. **Yes** to §4 as a public artifact, and to the honest sentence it
   licenses: *the operator algebra expresses 3 of the 41 LDBC SNB read
   templates; 35 of the 38 it cannot express need labelled pattern matching.*
4. **Conditionally yes** to SF1 *as a dataset*, folded into whatever session
   answers the ≥20M-row memory question — not as a benchmark, and with no
   query claim attached.
5. **Prefer** SNAP temporal streams for scale and skew, since the loader
   already exists.

### What would change this

Three specific conditions, in decreasing likelihood:

- **The façade is un-deferred.** `PAT` + `PROP` gate 37 of 38 templates. If
  D-038's openCypher subset ever gets an M1, re-run `scripts/ldbc_fit.py`
  *before* building anything: the fit number is the cheapest available test
  of whether the façade's shape grammar is wide enough to matter, and LDBC's
  41 templates are a better conformance corpus than the twelve
  hash-verified Cypher formulations D-038 proposed.
- **A bi-temporal graph benchmark appears.** The ADBIS 2025 bi-temporal
  property-graph model is the line to watch; AeonG's T-LDBC is the template
  for what one would look like. If one appears, this recommendation inverts
  immediately — it would be the first external artifact that measures what
  TGMS is for.
- **An external reviewer names LDBC as a condition.** Then the cheapest
  compliant answer is the derived Interactive Short slice with as-of twins
  (A + B + D + G′ ≈ 4–6 sessions) and the fair-use disclaimer — not a
  benchmark run.

---

## 8. The strongest case against this recommendation

Stated fairly enough to act on instead.

**1. The self-definition problem is the project's largest unaddressed
credibility risk, and this recommendation does not fix it.** Every number
this project publishes comes from a workload it designed. §4 is *also* our
instrument applied by us — the classes, the tags, and the charitable mapping
are all ours, and a reviewer can discount §4 by the same argument that
motivated wanting LDBC in the first place. A run, even a 3-of-41 run with 38
`unsupported` cells, produces one thing §4 cannot: latencies on data and
queries nobody here chose. `eval_semantics.md` already insists that a faster
number for an easier query is a mistake; the symmetric discipline — publish
the workload where we lose — is the one that makes an evaluation believable,
and 4–6 sessions is not a large price for it. **If the PI weights external
credibility above internal evidence, take option 2 in §6 and run the derived
Interactive Short slice.**

**2. Adaptation work here has historically found correctness defects before
it found costs, and this analysis skips that.** D-023 found that
independently-built stores diverge in every derived id. D-030's PostgreSQL
port found a guardrail false positive and a real disagreement between the
oracle and the Rust kernel in `resolve_entities` that the synthetic data
never surfaced. D-049 found two correctness defects the moment a genuinely
new configuration was exercised, in code that had been "measured" already.
LDBC SF1 is 20.2M versions across 31 types with heavy text properties, 15
edge types whose valid time we invent, and real degree skew — a
configuration this store has never been in. The most likely outcome of
loading it is not a benchmark result but a bug, and the argument "the
workload does not exercise our feature" is not an argument that the loading
would not teach us something. **This is the stronger of the two objections**,
and it is worth noting that it argues for §6 option 2 — SF1 as a dataset —
which the recommendation already conditionally accepts. The disagreement is
about priority, not direction.

---

## 9. What could not be established

- The minimum RAM/disk to generate SF1 with the Spark Datagen on a laptop.
  No public figure; every documented example uses SF0.003. (Moot: SF1 is
  downloadable pre-generated.)
- Whether Interactive **v2** audits have become commissionable since the
  specification's "As of January 2024" statement. No newer public statement
  was found, and every published Interactive result remains v1.
- A public, currently-downloadable TPC-BiH generator or reference
  implementation.
- Any graph benchmark, anywhere, that is bi-temporal. Absence of evidence
  here is strong but not proof; §3 states the search performed.

---

## 10. Where this contradicts existing project documents

Recorded because the project retires claims rather than quietly dropping
them.

1. **`docs/ROADMAP.md` §1 sizes "external datasets (LDBC SNB, FinBench,
   LSQB, TGB)" as a single **L** item for "topology diversity".** Three
   corrections. (a) It conflates *datasets* with *workloads*: SNB SF1 as a
   dataset is ~2–3 sessions; SNB as a workload is not available at any
   price (§2.4). (b) **LSQB cannot serve temporal topology diversity at
   all** — its README puts date and string operations out of scope, and no
   LSQB query reads a timestamp. (c) TGB is a machine-learning benchmark
   with no SUT and no queries; "running TGB" can only mean using its edge
   streams, which is the same thing as item 3 in §6 and much cheaper than
   an **L**.
2. **The evaluation plan's §5.2 scale ladder — "SF1 for debugging …, SF10
   for routine experiments, SF30 or SF100 for large single-machine
   evaluation" — is not supported by any measurement this project has.**
   SF10 is 197.6M versions against a largest-ever 10M whose measured memory
   floor was 5.93 GB uncapped; SF30 is 584M; SF100 is 1.96B. That ladder is
   a hypothesis and should be re-sized, or gated behind the memory
   measurement (§5 row C).
3. **The evaluation plan's §5.2 "SNB Temporalized Graph" is
   self-undermining for its stated purpose.** The plan is right that the
   synthetic correction history must not be presented as part of the
   official benchmark. The stronger point it does not make: once the
   corrections are ours, LDBC contributes only topology, and the
   third-party-definedness that justified the adaptation is absent from
   exactly the dimension being measured.
4. **`docs/eval/CAPABILITY_MATRIX.md` — "No cell in the matrix is
   `unsupported` … That is a finding, not a disclaimer" — is true and easy
   to over-read.** It holds *at the registry's scope*, and the registry is
   ours. At LDBC's scope the expressiveness gap is 38 of 41 and it runs the
   other way: the baselines express everything TGMS cannot. Nothing in that
   file is wrong; the symmetric measurement was simply never taken, and §4 is
   it. The matrix should be read alongside this document.

---

## 11. Evidence and reproduction

- Classification table, as data: `benchmarks/ldbc-fit-v1/classification.json`
  (41 rows; class, need tags, temporal-predicate flag, justification, and
  the reference-implementation URL per row).
- Regenerate: `uv run python scripts/ldbc_fit.py report`. The script carries
  the hand-audited table, the class and tag definitions, the assumed schema
  mapping, and assertions that no class-1/2 chain names a non-operator and
  no class-3 entry invents a tag.
- Operator surface classified against: `tgms/temporal/ops_*.py` and
  `tgms/temporal/algebra.py` (14 operators).
- Comparable-task cost anchors: `docs/eval_writes.md` (load rate),
  `docs/eval_phase0.md` (storage, six-system latencies),
  `docs/eval_resources.md` §14.2/§18 (memory ceiling),
  `scripts/pg_queries.py` / `ch_queries.py` / `neo4j_queries.py` (per-system
  query implementation size).
- Prior fit instrument: `scripts/independent_questions.py`,
  `benchmarks/independent-v1/classification.json`.
- LDBC sources: `github.com/ldbc/ldbc_snb_docs` (specification source, all
  quotes above), `ldbcouncil.org/benchmarks/fair-use-policies/`,
  `ldbcouncil.org/benchmarks/snb/`,
  `github.com/ldbc/ldbc_snb_interactive_v1_impls`,
  `github.com/ldbc/ldbc_snb_bi`,
  `github.com/ldbc/ldbc_snb_datagen_spark`, `github.com/ldbc/ldbc_acid`.

**This is not an LDBC Benchmark, this is not an implementation of an LDBC
Benchmark, and nothing in this document is an LDBC Benchmark Result.** LDBC
specification material is quoted under its CC-BY 4.0 licence.

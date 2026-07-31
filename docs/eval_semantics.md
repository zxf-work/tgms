# Cross-system semantics

Where TGMS's semantics diverge from what another system can express, recorded
rather than smoothed over (evaluation plan §18.4).

A comparison is only meaningful if "the same query" means the same thing on
both sides. Some of what follows is genuinely hard to express elsewhere, and
the honest response is to mark a query **unsupported** on that system rather
than quietly compare a weaker one. A slower number for an equivalent query is
a result; a faster number for an easier query is a mistake.

## Verdicts

Every (system, query) pair carries one:

| verdict | meaning |
|---|---|
| **equivalent** | same logical answer, canonical hash matches |
| **approximated** | answers a *related but weaker* question; the difference is stated, and the timing is not comparable |
| **unsupported** | cannot express it; reported as such, never as slow |
| **guardrailed** | TGMS declines by policy (`E_COST`) while another system would attempt it — see below |

`approximated` is the dangerous one. It should be rare, and each instance
needs a sentence saying exactly what was weakened.

---

## 1. Two clocks, not one

Every version carries **valid time** `[vt_s, vt_e)` — when the fact held in
the world — and **transaction time** `[tt_s, tt_e)` — when the database
believed it. Every operator takes `as_of_tt`, defaulting to "current
beliefs", and all valid-time reasoning happens *inside* that belief state.

Most systems model at most one of these. A relational baseline can carry both
as columns, so the semantics are expressible — but only if **every** query
threads the belief predicate through, including nested and recursive parts.
Dropping it produces answers that look right on uncorrected data and are
silently wrong the moment a correction exists.

Minimum bar for `equivalent`: a query must return different results before
and after a correction when asked at different `as_of_tt`, matching TGMS.
`hist.asof` in the registry exists to catch a system that ignores the clock.

**Correction is not update, and retraction is not deletion.** A corrected
version's row is retained with its `tt_e` closed; it remains the answer to
"what did we believe then?". A system that overwrites in place cannot answer
historical belief queries at all and should be marked `unsupported` on them,
not given a pass because current-state answers agree.

## 2. Half-open intervals and the open-end sentinel

Intervals are `[start, end)` everywhere, valid iff `start < end`. An
open-ended interval is stored as `vt_e = 2^62` (`OPEN_END`), not `NULL` and
not infinity.

Two consequences for a baseline:

- Containment is `start <= t < end`. A system defaulting to closed intervals
  will differ at exactly one microsecond per boundary — invisible in
  aggregate, decisive at instant queries.
- `OPEN_END` participates in arithmetic. TGMS's own statistics treat an
  open-ended interval as contributing `vt_s + 1` to the extent, and a
  baseline must match that convention or `dataset_extent` will diverge for
  reasons unrelated to storage.

## 3. Identity is derived, and is not comparable across systems

`eid = hash(src, dst, rel_type, disc)` and
`vid = hash(identity, tt_s, vt_s)`. Both are TGMS constructions. Another
system will not reproduce them, and should not be asked to.

**Comparisons must therefore be on logical content, not on identifiers.**
Where a registry query's answer includes a `vid`, the canonicalizer compares
it only between systems that derive it identically — today, the two TGMS
backends. For an external system the query is either restated to return
logical fields, or marked `approximated` with that noted.

This also means transaction times must be *replayed*, never regenerated: a
store rebuilt by re-ingesting the same data gets new `tt` values from the
clock, so every derived id changes. Load from the recorded event log.

## 4. Result ordering is part of the answer

Operators specify total orders — scans by `(vt_s, vid)`, motif instances by
the `(t, eid)` sequence of their edges, resolution by `(score, uid)`. The
canonical hash includes row order.

A baseline returning the same rows in a different order is **not**
`equivalent`. Either its query carries the matching `ORDER BY` — which is
fair, since TGMS pays for that ordering too — or the divergence is recorded.
Adding an `ORDER BY` that TGMS gets for free from its layout is a real cost
the baseline should be charged, not hidden.

One subtlety already settled internally: the CSR traversal tiebreak within a
`(src, vt_s)` group is by `vid`, not `eid`. The operator outputs are
invariant to this — verified against the oracle on fixtures that exercise the
distinction — so a baseline need not reproduce it.

## 5. Guardrails are a semantic difference, not a performance one

TGMS refuses queries whose estimated cost exceeds a ceiling, returning
`E_COST` with narrowing suggestions. Another system will simply attempt the
query and either finish or exhaust the machine.

This is not TGMS being slower or faster; it is a different contract. Record
these as **guardrailed**, and where a comparison is wanted, run the
*narrowed* query on both systems rather than removing the guardrail on one.
The registry's `motif.filtered` is deliberately node-filtered for this
reason.

Pagination interacts: results carry `limit`, `truncated`, and a `cursor`, and
a truncated answer is a different answer. Baselines must apply the same
`limit`, and the plan's rule that count and enumeration are separate queries
(§11.3) exists because they optimize differently.

## 6. Where TGMS itself is narrower than a general system

Recorded for symmetry — these are places a baseline may legitimately do more,
and the comparison should not claim TGMS's restriction as an advantage:

- **Name resolution is current-canonical and string-only.** Matching is over
  the latest believed name, and only when it is a JSON string (D-031);
  historical alias lookup is not offered.
- **Single writer.** Concurrency comparisons are read-side only until
  belief-state isolation exists.
- **Fixed motif catalogue.** Five shapes, not arbitrary pattern matching. A
  system with general subgraph matching is strictly more expressive here.
- **No general predicate pushdown.** The scan signature is closed: valid-time
  window, relation type, incidence. Property filters happen above the storage
  layer, which a system with a real query optimizer would push down.

## 7. Per-system notes

### TGMS native / TGMS DuckDB adapter

`equivalent` on the whole registry by construction: they share the semantics
layer and differ only in storage. Any hash mismatch between them is a bug,
which is what makes them the useful Phase 0 pair.

### ClickHouse

Schema, loader, and tuning: `scripts/ch_baseline.py`; registry SQL, added
slice by slice: `scripts/ch_queries.py` (D-035). Loading follows the same
replay rule as PostgreSQL (§3). **Verdict: equivalent on the whole
registry** — all twelve queries hash-identical to the operators, verified
before timed.

How the hard shapes were expressed: ClickHouse has no session temp tables
over stateless HTTP and recursion is not its native shape, so the three
iterative queries (BFS, Bellman-Ford reachability, bounded path search)
drive rounds through Memory-engine working tables — round control in
Python, every set operation in ClickHouse; bounded hops make iteration
exact, not approximate. `LIMIT 1 BY` stands in for `DISTINCT ON`, tuple
comparison carries the motif's `(t, eid)` ordering, and
`JSONExtractString` returning empty for non-string values is precisely
D-031's string-only name rule. The round-trip structure is also the honest
performance story: the iterative queries pay ~1 ms per HTTP round plus a
table build per stage, which is why reachability is ClickHouse's worst
number and aggregation its best.

Two residual caveats: `lowerUTF8` and Python's `str.lower()` can disagree
on exotic case mappings (same class of divergence as PostgreSQL's
`lower()`, unobservable on the current datasets); and `burst.zscore` keeps
its scalar tail in Python for the same rounded-threshold reason as the
PostgreSQL twin.

### Neo4j

Loader and model: `scripts/neo4j_baseline.py`; Cypher, slice by slice:
`scripts/neo4j_queries.py` (D-036). Verdicts so far: `hist.single`,
`hist.asof`, `series.count`, `burst.zscore` — **equivalent**,
hash-verified; the rest unwritten. Semantics notes: Neo4j stores no
property for null, so absent and null are the same fact (compatible with
the operators, which never distinguish them); string comparison is
codepoint-ordered, matching the contract natively. The traversal slice is
the one this baseline exists for and is written next.

### PostgreSQL

Schema, indexes, tuning, and loader live in `scripts/pg_baseline.py`; the
registry SQL in `scripts/pg_queries.py`.

**How the data gets in.** PostgreSQL is a baseline, not a backend: it never
implements the write semantics. TGMS produces the canonical version rows and
`COPY` loads them, so transaction times arrive exactly as recorded. Anything
else would regenerate `tt` from the clock and change every derived id (§3).

**Storing props as text, not JSONB.** `props` is TEXT holding canonical JSON.
JSONB normalizes key order and whitespace, so a round trip would not return
the stored bytes, and the digest is computed over exactly those bytes. An
earlier schema carried a generated JSONB column alongside for querying; it was
dropped after measuring, because an expression index over `props::jsonb`
serves the same predicates without storing every blob twice. The effect is
that both systems parse JSON above the storage layer, which is what TGMS
already does (§6).

**The belief predicate has two spellings, and they are not interchangeable to
the planner.** A partial index `WHERE tt_e = OPEN_END` is the relational
analogue of the engine's `all_current` flag, and current-belief queries reach
it — but only when written as that equality. The general as-of form
`tt_s <= T AND T < tt_e` falls back to the full index, measured. The planner
is right to refuse: the implication holds only because `tt_e` never exceeds
`OPEN_END` in our data, which is not something the schema states. So registry
SQL must branch on whether `as_of_tt` was supplied — the same branch the
engine makes. Writing every query in the general form would understate
PostgreSQL; writing every query as the equality would answer the wrong
question at `hist.asof`.

**Storage, recorded without a ratio.** At 1M edge versions the tuned server
holds a 182.2 MB heap plus 366.4 MB of indexes, or 548.6 B/row all in. TGMS
measures 65.2 B/row and projects 92.9 B/row with indexes persisted. **These
are not yet a like-for-like comparison** and no ratio should be quoted from
them: the PostgreSQL figure carries eight edge indexes chosen for the whole
registry, against two on the TGMS side, and the TGMS index figure is a
projection rather than a measurement. An earlier storage claim in this project
was wrong for exactly this kind of mismatched-baseline reason. A real
comparison needs both systems carrying only the indexes the registry uses.

**All measurement happens on xzgpu.** Every reported number comes from the
one Linux host (40 cores, 93 GB) that `bench_ops.md` already uses. The laptop
is for development only, and figures taken there are not comparable: macOS
pins `effective_io_concurrency` to 0 for want of `posix_fadvise`, so the
baseline cannot prefetch at all, and the two machines differ by 5× in cores
and 6× in RAM.

PostgreSQL 16.14 on xzgpu is a source build under
`/mnt/project/xzhang/tgms/pg` (no root on that host), listening on a unix
socket at `/mnt/project/xzhang/tgms/pgrun` port 5433. Point the harness at it
with `PGHOST`/`PGPORT`; `scripts/pg_baseline.py --tune-server` records the
settings. Two choices there are worth stating because they are favourable to
PostgreSQL and deliberate: the cluster is initialized `--locale=C`, which
matches TGMS's byte ordering natively *and* is faster than a locale collation,
and it is built `--without-icu` for the same reason.

**Verdicts.** All twelve registry queries are written in SQL
(`scripts/pg_queries.py`) and every one returns a canonical hash identical to
the TGMS operator's, over non-trivial answers, on data containing corrections.
So the whole registry is **equivalent** on PostgreSQL — nothing in it turned
out to be inexpressible, which is itself worth stating: the expressiveness gap
this baseline was expected to expose is not there at the registry's scope.

Three needed more than a translation:

- **`reach.window` / `paths.k`** — recursive CTEs over `(node, arrival)`
  states. The traversal rule is *non-decreasing*, not strictly increasing:
  `tau = max(arrival, vt_s)`, admissible iff `tau < vt_e` and `tau < t_b`, so
  many edges may be traversed at one instant. `UNION` rather than `UNION ALL`
  supplies the label dedup that makes the recursion terminate. Path ranking is
  `(arrival, hops, sequence of (vt_s, eid))`; the sequence rides along as one
  text column of fixed-width chunks, which orders identically to element-wise
  array comparison because equal `hops` is compared first.
- **`burst.zscore`** — the scan, bucketing, and windowed aggregation are SQL
  (`stddev_pop`, frame `w PRECEDING AND 1 PRECEDING`), but the final scalar
  arithmetic is Python. The reference thresholds on the *rounded* score, and
  Python rounds half-to-even on the binary double while PostgreSQL rounds
  half-away-from-zero on a decimal expansion — so rounding server-side would
  change which rows exist, not merely how they print. At most 2000 buckets
  cross that boundary; no scan work moves.
- **`motif.filtered`** — a three-way self-join with `ROW(t, eid)` strict
  ordering. The ordering is strict in the *composite* key, not in time alone,
  so three events sharing a `vt_s` can still form a motif with `eid` deciding
  their roles.

**`resolve_entities`: the two TGMS implementations disagree with each other.**
Porting it surfaced a divergence that has nothing to do with PostgreSQL. The
oracle and portable fallback update the per-uid canonical version for *every*
believed version before the match test, so `label` and `name` come from the
latest version overall; the Rust kernel reaches that update only for
*matching* versions, so they come from the latest matching one. The two also
break `vt_s` ties in opposite directions — the oracle keeps the earlier-scanned
version, the kernel the later — and neither is order-independent. The SQL
follows the oracle. The synthetic data does not currently distinguish them, so
nothing fails; a uid whose newest version does not itself match would.

Three details did the work in getting those six to match, and each would have
produced a wrong-but-plausible answer on its own:

- **`COLLATE "C"` on every string ordering.** The operators sort uids and vids
  as Python strings, by code point. Under a locale collation PostgreSQL sorts
  them differently — and both answers look correctly sorted.
- **`clamp_tt`.** `a = LEAST(as_of_tt, OPEN_END - 1)`. Without it the default
  `as_of_tt = OPEN_END` matches no row at all.
- **`rows_total` counts before `LIMIT`**, so each query is a count plus a page.

Comparisons here are on logical content *and* on vid, which §3 would normally
forbid. It is sound in this one case because PostgreSQL is loaded with the
canonical rows and carries the vids TGMS derived, rather than deriving any.

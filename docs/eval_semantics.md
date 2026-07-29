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

- **Name resolution is current-canonical only.** Matching is over the latest
  believed name; historical alias lookup is not offered.
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

### PostgreSQL

Schema, indexes, tuning, and loader live in `scripts/pg_baseline.py`. The
registry SQL is not written yet, so no query carries a verdict here.

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

**Host caveat.** These runs are on a 16 GB / 8-core macOS laptop, while the
operator benchmarks in `bench_ops.md` were taken on xzgpu (40 cores, 93 GB).
Numbers from the two hosts are not comparable, and macOS additionally pins
`effective_io_concurrency` to 0 — no posix_fadvise, so the baseline cannot
prefetch at all here. The comparison run belongs on one Linux host.

**Expected divergences, still to be confirmed against the hash oracle:**
time-respecting reachability needs a recursive CTE carrying a monotonic
arrival time; δ-motifs need a three-way self-join with the ordering and span
predicates written out; belief filtering must be threaded through both,
including the recursive part.

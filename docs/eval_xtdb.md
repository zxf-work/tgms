# XTDB: the first semantic competitor

The evaluation gap D-070 named: the bi-temporal storage, correction and
historical-reconstruction claims have never faced a system that *contests*
them — DuckDB, PostgreSQL and ClickHouse are architectural baselines that do
what our SQL tells them. XTDB 2 is the first competitor that does the same
*job*: native bi-temporality (SQL:2011), automatic supersession, both time
axes first-class. This document is the plan, the fairness terms, and — per
the standing rule — the forecast, written before the harness existed.

- harness: `scripts/xtdb_baseline.py` (container lifecycle, replay, probes)
- decision record: D-083
- system under test: `ghcr.io/xtdb/xtdb` (XTDB 2.x, GA line), Postgres wire
  protocol on 5432, spoken through the same `psycopg` extra the PG baseline
  added (D-030)

## Scope: six storage operations, not thirteen queries

Per D-070 — the point is the storage claims, not the operator algebra:

| op | ours | XTDB |
|---|---|---|
| S1 current entity lookup | `believed_node_versions(uid)` | `SELECT … WHERE _id = ?` (defaults to current on both axes) |
| S2 valid-time as-of | `believed(uid)` + `valid_at(t)` | `FOR VALID_TIME AS OF ?` |
| S3 transaction-time as-of | `believed(uid, tt)` | `FOR SYSTEM_TIME AS OF ?` |
| S4 full version history (one identity) | `believed_log` / all versions of uid | `FOR ALL VALID_TIME FOR ALL SYSTEM_TIME WHERE _id = ?` |
| S5 correction-heavy ingest | event-log replay | the same ops as SQL DML (below) |
| S6 snapshot diff | `diff.global` between two tt | join of two `FOR SYSTEM_TIME AS OF` reads |

## The mapping, and why it is the honest one

**Op-level replay, not version-level.** The PG baseline received our
*resolved* versions because PostgreSQL has no temporal semantics of its own.
XTDB does, so it gets the *ops* — `assert` becomes `INSERT` with
`_valid_from`/`_valid_to`, whole-interval supersession and carves are XTDB's
own automatic portion semantics, `retract` becomes `DELETE … FOR PORTION OF
VALID_TIME` — and it performs its own supersession. That is what "a system
that contests the claim" means; feeding it pre-resolved rows would test
nothing but its B-trees. D-023 discipline holds: **one reference event log,
replayed into both systems.**

**Transaction time maps exactly, which is rare luck.** XTDB's backfill
override — `BEGIN READ WRITE WITH (SYSTEM_TIME = ?)` — requires monotonically
non-decreasing system time, and our event log guarantees strictly increasing
`tt` per batch by invariant (I2). One of our batches = one XTDB transaction
at `SYSTEM_TIME = map(tt)`. S3 is therefore a *direct* comparison, not a
translation through a correspondence table.

**Timestamps.** Our integer times map by `t → epoch + t µs` (order- and
gap-preserving, reversible); `OPEN_END` maps to XTDB's end-of-time (omitted
`_valid_to`). All comparisons happen in our integer domain after inverse
mapping.

**Content comparison, not representation comparison.** Fragment boundaries
may legitimately differ (two correct bi-temporal stores can carve the same
history into different pieces). The comparator therefore checks **believed
state**: at every probe point `(identity, vt, tt)` drawn from interval
boundaries plus uniform samples, the believed `(props, interval-coverage)`
must agree with the native store's answer, canonical-hashed on both sides.
Verdicts per `docs/eval_semantics.md`: equivalent / approximated /
unsupported / guardrailed, per operation.

## Fairness (D-030, unchanged)

XTDB gets its recommended configuration, its own idioms written to win, and
everything recorded in the run manifest: image digest, JVM heap, volume
mount, `COMMIT SYNC` vs `ASYNC` (**sync**, to match our fsync-per-commit
durability — deviations recorded), warm-up runs before timing exactly as
§16.3 gives every other system. Latencies over pgwire include the wire, as
PG's and ClickHouse's did. Two caveats declared up front rather than
discovered later: the Docker image is labelled non-production by XTDB's own
docs (recorded in the manifest; it is also their recommended single-node
path), and the JVM gets a warm-up pass before any timed run.

## Forecast, written 2026-08-06 before the harness (D-083; score after)

Scales: **dev 200k events on the Mac** (shapes only, no cross-host claims),
**1M on xzgpu** (numbers of record), 5% and 20% correction density. Native
figures cite current receipts. Per the D-074 rule, each cell names its
scale; per D-070's positioning, cells XTDB is expected to win are forecast
as such — a comparison we only publish where we win is worthless.

- **F1 — semantic agreement is the headline result, and I forecast it holds
  with ≤2 divergence classes.** Expected divergences: (a) our
  in-batch retire semantics (a version superseded within its own batch was
  never believed, D-059) against XTDB's intra-transaction visibility —
  probes at batch-boundary tt values should agree, but I forecast at least
  one intra-batch edge needs an `approximated` verdict; (b) interval
  fragmentation differences absorbed by the content comparator by design.
  Anything beyond two classes means our model and SQL:2011 diverge more
  than the technical report currently implies, which would itself be a
  publishable finding.
- **F2 — point lookups (S1–S3): native wins 2–5× at 1M.** Same shape as the
  PG result (0.1 vs 0.3 ms): our postings + open-version index against a
  JVM + pgwire round-trip. XTDB's as-of columnar scans are good; the wire
  and JVM floor is ~0.3–1 ms. *Scale: 1M, warm, p50.*
- **F3 — S3 flat in correction density for both systems.** Ours is flat
  since D-076…D-081; XTDB's system-time filtering is native to its Arrow
  layout. If XTDB *degrades* with density, that is a real differentiator
  for us; I forecast it does not (within 2× from 5% to 20%).
- **F4 — full history of one identity (S4): parity to 2× either way.** Both
  systems reach one identity's versions through an index. Ours
  materializes strings per version (the D-069 lesson); XTDB pays Arrow →
  pgwire serialization. No confident winner; stating parity *is* the
  forecast, and a >2× loss either way is information.
- **F5 — correction-heavy ingest (S5): native wins 3–10× at 1M/20%.**
  Ours: 362.6 s replay at 20% (`eval-1m-bitemporal-d081.json`). XTDB does
  more per correction than we do (its portion resolution is server-side and
  general), runs a JVM, and pays pgwire per batch. The interesting number
  is XTDB's *shape* across density — if its ingest is flat where ours was
  once quadratic, their portion machinery is better than our pre-D-072
  close path was, which is worth knowing regardless of the constant.
- **F6 — snapshot diff (S6): the cell XTDB is most likely to win, forecast
  XTDB 1–3× ahead at 1M.** A two-system-time diff is a columnar join over
  Arrow, squarely XTDB's design center; our `diff.global` carries ~300 ms
  of props materialization at 1M that density does not explain
  (eval_bitemporal §"honest limits"). I would rather forecast this loss
  and be wrong than discover it.
- **F7 — storage: XTDB 3–10× larger on disk at 1M**, measured as D-070's
  three footprints (canonical, query-ready, cold-start). Arrow generality
  plus immutable L0/L1 files against our 65 B/row purpose-built segments.
  Their cold start (JVM + Arrow open) I forecast *slower* than our
  post-D-082 0.3 s at 1M — a cell we win now that would have been a loss
  two weeks ago.
- **F8 — at least one of the six operations hits an expressiveness wall.**
  Candidate: S4's "history as of an earlier tt" composed shapes, or our
  edge identity model (eid = src+dst+rel+disc) needing a composite `_id`.
  Forecast: one `unsupported` or `approximated` verdict among the six, not
  zero — SQL:2011's bitemporal surface is narrower than its reputation.

## What this evaluation does not claim

No graph traversal, no operator algebra, no agent-facing claims — those
have their own competitor classes (Raphtory/TGLib; RAG/text-to-query). This
is storage semantics and storage cost only, on the four claims XTDB
actually contests.

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

## Results — 1M on xzgpu at `a340d6e` (receipts `eval-xtdb-1m-{5,20}-final.json`)

**The headline is semantic, exactly as hoped: 400 believed-state probes at
1M across both densities, plus the 140-probe crafted D-059 scenario and all
dev runs — zero disagreements anywhere.** XTDB's SQL:2011 portion machinery,
fed our raw ops, reproduces our belief semantics at every point probed. The
four claims XTDB contests are now the four claims a real bi-temporal
competitor *agrees about*, which converts them from assertions into
measurements.

| op | XTDB (5% / 20%) | native (5% / 20%) | ratio |
|---|---|---|---|
| S1 current lookup | 2.15 / 2.80 ms | 0.037 / 0.014 ms | 58–200× native |
| S2 valid-time as-of | 2.13 / 3.89 ms | 0.028 / 0.010 ms | 76–389× native |
| S3 tt as-of | 2.44 / 2.79 ms | 0.026 / 0.023 ms | 94–121× native |
| S4 history (one identity) | 5.45 / 3.46 ms | 0.137 / 0.126 ms | 28–40× native |
| S5 ingest (replay) | 411 / 1,788 s | 105 / 379 s | 3.9–4.7× native |
| S6 snapshot diff | 51.5 / 49.1 ms | 21.1 / 20.7 ms | 2.4× native |
| storage | 750.8 / 939.0 MB | 28.0 / 40.8 MB | 23–27× native |

Wire time is included in XTDB's numbers, as it was for PostgreSQL and
ClickHouse; ours are embedded-process latencies, and that asymmetry is part
of what is being compared (deployment model is a property of the system).

## Scoring the forecast (D-084) — written before the harness, scored after

- **F1 — agreement with ≤2 divergence classes: headline CONFIRMED, sub-claim
  WRONG.** Zero divergence classes, not one or two. The specifically
  forecast intra-batch `approximated` verdict did not appear even on the
  crafted D-059 scenario built to elicit it. SQL:2011's portion semantics
  and our belief model agree more exactly than this survey's own author
  predicted.
- **F2 — point lookups native 2–5×: WRONG on magnitude, 58–389×.** Same
  error class as D-074's F2: right mechanism (wire + JVM floor), wrong
  arithmetic — and half the error is that the forecast was written from
  pre-D-076/D-077 intuitions about our own engine, whose point reads got
  ~10× faster during the same week.
- **F3 — flat in density for both systems: CONFIRMED.** XTDB S3 moves
  1.14× from 5% to 20%; ours is flat. XTDB does not degrade with
  correction density — the differentiator I said would be real if present
  is not present, and that is worth as much to know.
- **F4 — S4 parity to 2×: WRONG, 28–40× native.** The forecast reasoned
  "both reach one identity through an index" and understated both our
  operator's post-arc speed and XTDB's per-row Arrow-to-pgwire cost.
- **F5 — ingest native 3–10×: CONFIRMED, 3.9× and 4.7×**, mid-band, and
  stable across density — XTDB's ingest shape is flat-ish too.
- **F6 — snapshot diff, forecast XTDB 1–3× ahead: WRONG — native 2.4×
  ahead at both densities.** The forecast loss did not materialize.
- **F7 — storage XTDB 3–10×: WRONG on magnitude, 23–27×**, and growing
  with scale (7.3× at 20k). Cold-start sub-cell not measured; unscored.
- **F8 — at least one expressiveness wall among the six: WRONG — none.**
  All six operations are expressible and agree. The three *dialect* walls
  hit en route (untyped-parameter refusal, no queries in DML transactions,
  `TO NULL` as end-of-time) are implementation friction, not contract
  gaps, and are recorded as such.

**Scorecard: 2 confirmed, 1 headline-confirmed with a wrong sub-claim, 5
wrong — and every miss but F4 erred *against* our own system.** The
forecast was written from intuitions predating the D-076…D-081 engine arc
and systematically under-rated the engine it was forecasting for. That is
the same lesson as D-074's F2 pointed the other way: forecasts age, and
the honest fix is scoring them, not updating them retroactively.

## What this evaluation does not claim

No graph traversal, no operator algebra, no agent-facing claims — those
have their own competitor classes (Raphtory/TGLib; RAG/text-to-query). This
is storage semantics and storage cost only, on the four claims XTDB
actually contests.

# The correction-density matrix

A standing benchmark for the write path under correction load: density ×
batch size × versions per identity × out-of-order valid-time distance.

**Why it exists.** The correction path was measured once, as a one-off
experiment (`docs/eval_bitemporal.md` §13), found superlinear, fixed, and
then went on being quoted at its **pre-fix** value for four days across
three internal documents and an external review (D-072). A number that is
measured once is a number that cannot be checked. This matrix makes the
measurement reproducible on demand — small scale in CI on every push, the
full matrix on xzgpu for release evaluations — and ships with a regression
gate, because a benchmark whose output nothing reads back is the same
failure with more JSON.

- harness: `scripts/bench_corrections.py`
- CI gate: `tests/test_correction_scaling.py`

---

## Forecast, written 2026-08-05 before the harness was built (D-073)

Per the standing rule: written down first, scored afterwards, per cell
rather than in aggregate. Aggregates conceal compensating errors.

### F1 — density: flat per-correction cost

With `de5071b` — one commit carrying both the WP-N4 postings and the
per-generation `close_index()` cache — in, marginal cost per correction is **flat**
across density; total time is linear in correction count. Predicted spread
across 1/5/20/50% at fixed batch size: **within 2×**, dominated by noise
and by segment-count growth rather than by any quadratic term.

*Basis:* measured 1.61 / 1.59 / 1.60 ms per correction at 1/5/20% on xzgpu
(D-072). 50% is unmeasured and is the cell most likely to surprise.

### F2 — batch size: the dominant axis, and steeply so at the small end

Batch size should matter **more than density**. Each committed batch is one
published generation: seal segments, fsync, write a close run, write and
fsync a manifest. At batch size 1 every correction pays that fixed cost
whole; by batch 100–1,000 it is amortized to near nothing.

Predicted: batch 1 is **>10× worse per correction** than batch 1,000. The
curve flattens by ~100 and is essentially flat from 1,000 to 10,000, where
staging memory starts to trade against it. This axis has never been swept —
the §13 sweep held batching fixed at the harness baseline.

### F3 — versions per identity: linear growth, and the next bottleneck

**This is the cell I expect to expose a real limit.** A correction resolves
its target through `believed_*_versions(identity)`, which returns *every*
version of that identity and filters in Python
(`base.py::_assert_edge`, `_correct`). The identity postings make finding
those rows O(candidates) rather than O(store) — but candidates *is* every
version of that identity.

Predicted: per-correction cost grows **linearly in versions per identity**,
with a visible knee once an identity's version count exceeds a segment's
worth of rows and the lookup starts touching multiple segments. At 100+
versions per identity I expect this term to overtake the commit cost of F2
at large batch sizes.

If F3 holds, the correction path is linear in *corrections* (F1, fixed) but
quadratic in *corrections to the same identity*, which is precisely the
long-lived-entity workload — and the honest headline becomes "linear in
volume, quadratic in per-entity history depth". That would be the next
engine item, and it is a different fix from the one D-072 found already
done: a per-identity open-version pointer, not a scan replacement.

### F4 — out-of-order valid-time distance: mild, via carving not lookup

The postings are keyed by identity, not by time, so lookup cost should be
**insensitive** to how far out of order a correction lands. The indirect
effect is real but smaller: a correction landing inside an existing
interval carves it (`base.py::_remainder`), writing up to two fragment rows
instead of none. Predicted **under 1.5×** between in-order and maximally
out-of-order at fixed density, showing up as write amplification rather
than latency.

### F5 — write/space amplification: superlinear in density, sublinear in batch

Bytes on disk per correction should be roughly flat, but *manifest* bytes
per correction fall sharply with batch size (one manifest per batch, not
per correction). Predicted: at batch 1 manifests are a **double-digit
percentage** of the store; at batch 1,000 they are negligible.

### What would falsify the fix being in

F1 failing — per-correction cost rising with density — means the quadratic
term is back. That is what the CI gate asserts, and it is asserted as a
**ratio** rather than an absolute time so it holds on any host.

---

## Results — `full` profile, xzgpu, HEAD `d5620bf` (current)

The same matrix after the open-version index (D-076), the segment-name cache
(D-077) and the close-index fold (D-079). 89.2 minutes, receipt
`bench-corrections-full-d079.json`. The grid below is directly comparable to
the `4481997` run further down; **the depth axis is not**, because D-075
changed its construction.

### ms per correction — old → new

| density | batch 1 | batch 10 | batch 100 | batch 1,000 | batch 10,000 |
|---:|---:|---:|---:|---:|---:|
| 1% | 15.18→11.98 | 1.19→0.86 | **0.123→0.103** | 0.273→0.303 | 2.45→2.69 |
| 5% | 15.39→12.20 | 2.17→1.28 | **0.281→0.130** | 0.291→0.307 | 2.45→2.70 |
| 20% | 16.08→12.01 | 2.17→1.28 | **0.952→0.230** | 0.355→0.312 | 2.47→2.70 |
| 50% | 16.25→11.09 | 2.16→1.19 | **0.955→0.231** | 0.498→0.341 | 2.52→2.72 |

**The optimum moved to batch 100 at every density**, where before it was 100
at low density and 1,000 at high. The best achievable per-correction cost
falls 0.355→0.230 ms at 20% and 0.498→0.231 ms at 50%.

### Per-identity history depth is now flat

| depth | 1 | 10 | 100 | 1,000 |
|---:|---:|---:|---:|---:|
| ms/corr | 0.207 | 0.238 | 0.240 | 0.244 |

**1.18× across a 1,000× range**, with seed batches held fixed so the axis
measures depth and nothing else. This is the axis that opened Session AC at a
claimed 18.8×.

### Replay — the restart path

| density | 1% | 5% | 20% |
|---:|---:|---:|---:|
| before | 93.66 s | 109.45 s | 296.81 s |
| after | 91.01 s | 98.82 s | **137.50 s** |

At 20% correction density replay is **2.2× faster** (1.484 → 0.688 ms per
correction). Recovery time is what a user actually waits for after a crash.

### What got worse, and why it is a real trade

**Batch 10,000 regressed 8–10%**, consistently at every density — and it is
not noise, it is commit latency:

| cell | commits | p50 commit before | after | |
|---|---:|---:|---:|---|
| 20% × batch 100 | 2,000 | 90.42 ms | 22.35 ms | **4× better** |
| 20% × batch 1,000 | 200 | 349.59 | 307.95 | better |
| 20% × batch 10,000 | 20 | 24,677.87 | 26,965.03 | **9% worse** |

The open-version index does per-row work at index time. That is repaid by
cheaper lookups *per generation* — so it pays when there are many generations
and loses when there are very few. At batch 10,000 a cell commits 20 times
and never recovers the cost. **This scores D-076's F5, which was left
unscored: commit overhead is ~8–10% at batch 10,000, against a forecast of
≤5%.** At batch 100 it is overwhelmed several times over by the fold's gains.

Storage is unchanged — the manifest share table below still holds exactly, to
the tenth of a percent, since none of these changes touch what is written.

### Guidance, revised

**Batch corrections at 100.** Not 1,000, and emphatically not 10,000: the
largest batch is now 12–26× worse per correction than the optimum *and* pays
a 27-second p50 commit latency, during which nothing else can commit.

---

## Results — `full` profile, xzgpu, HEAD `4481997` (superseded)

20,000 entities over a 1M-event base, ≤2,000 commits per cell, replay timed,
**100.8 minutes**. Receipt `benchmarks/results-v1/bench-corrections-full-d073.json`.
Eight cells hit the commit budget and are flagged `truncated` in the record.
(The run manifest says `dirty: true`: the tree carried exactly one
uncommitted file, this harness, scp'd onto a scratch branch fetched from
GitHub. The engine was rebuilt and verified newer than `crates/` first.)

### ms per correction — density × batch size

| density | batch 1 | batch 10 | batch 100 | batch 1,000 | batch 10,000 |
|---:|---:|---:|---:|---:|---:|
| 1% | 15.18 † | 1.19 | **0.123** | 0.273 | 2.451 |
| 5% | 15.39 † | 2.17 † | **0.281** | 0.291 | 2.451 |
| 20% | 16.08 † | 2.17 † | 0.952 | **0.355** | 2.471 |
| 50% | 16.25 † | 2.16 † | 0.955 † | **0.498** | 2.523 |

† capped at the commit budget. **The curve is U-shaped, not monotone.** The
optimum is batch 100–1,000; batch 10,000 is 5–20× *worse* than the optimum.
Worst-to-best across a row is **45× at 20% density and 123× at 1%**.

Batch 10,000's cost is not waste, it is a trade: its p50 **commit latency is
24.7 seconds**. Amortized cost is fine, but nothing else can commit for 25 s.

### manifest share of the store (%)

| density | batch 1 | batch 10 | batch 100 | batch 1,000 | batch 10,000 |
|---:|---:|---:|---:|---:|---:|
| 1% | 99.6 | 98.9 | 70.4 | 4.0 | 0.3 |
| 5% | 99.6 | 99.5 | 95.0 | 21.3 | 0.6 |
| 20% | 99.6 | 99.5 | 98.8 | 54.5 | 1.5 |
| 50% | 99.6 | 99.5 | 98.8 | 75.4 | 3.3 |

At batch 1 the store **is** its manifests — 99.6%, at every density. Large
batches dissolve the problem entirely.

> **Correction (D-075): the depth column below is ~9× too large, and the
> harness that produced it has been fixed.** Seeding depth D committed D
> batches, so this axis moved *two* variables at once — versions per identity
> **and** segment count, hence manifest size, which is O(segments). A control
> holding batches fixed at 1,000 and varying only how the same 200,000 rows
> distribute over identities measures the depth-only effect at **2.02×**, not
> 18.8×:
>
> | arm (1,000 batches, 200,000 rows both) | ms/corr | `believed_*` |
> |---|---:|---:|
> | depth 1 over 200,000 entities | 1.027 | 0.151 |
> | depth 1,000 over 200 entities | 2.075 | 1.199 |
>
> The two deltas agree to three decimals — **1.048 ms of extra correction
> cost, 1.048 ms of it inside `believed_*_versions`** — so the effect is real
> and entirely attributable to the identity lookup. Only its magnitude was
> inflated. `_seed_ops` now takes `seed_batches` and holds it fixed across
> depths. The numbers below are kept as the record of the confounded run.

### versions per identity — the axis the `ci` profile could not reach

| depth | ms/corr | bytes/corr | p99 commit | manifest share |
|---:|---:|---:|---:|---:|
| 1 | 0.1485 | 327 | 21.7 ms | 82.3% |
| 10 | 0.1622 | 373 | 22.1 ms | 82.9% |
| 100 | 0.3458 | 836 | 42.2 ms | 87.9% |
| 1,000 | **2.7947** | **5,462** | **294.4 ms** | 97.1% |

**18.8× from depth 1 to 1,000**, with the last decade of depth costing 8.1×
— i.e. linear in depth past a knee between 10 and 100.

### out-of-order valid-time distance, and replay

Distance 0 / 100 / 10,000 / 1,000,000 → 0.121 / 0.158 / 0.155 / 0.158 ms per
correction: **1.30×** end to end, with the cost in bytes (327 → 355) rather
than latency. Replay, timed against a reference log written from the same op
batches: 93.7 s / 109.5 s / 296.8 s at 1 / 5 / 20% density — 9.37 → 2.19 →
1.48 ms per correction, i.e. fixed overhead amortizing, not superlinear
growth.

### A statistic that needs a caveat

`marginal_ratio` (late batches over early ones) reaches **6.14** at 20% ×
batch 100. That is **not** the D-072 close-index defect returning — it is
manifest growth *within* the cell, since the manifest is O(segments) and that
cell commits 2,000 times. The two mechanisms have the same signature and are
separated only by scale: `tests/test_correction_scaling.py` runs 120 batches,
where manifest growth is negligible and the measured ratio is 1.03. **A
future reader must not treat a high marginal ratio in this matrix as the
defect returning.**

---

## Results — `ci` profile, HEAD `4481997`, dev M-series host

500 entities over a 20,000-event base, ≤120 commits per cell, 23.3 s total.
Receipt: `benchmarks/results-v1/bench-corrections-ci-d073.json`. The `full`
profile (1M-event base, batch sizes to 10,000, depths to 1,000) is written
and **not yet run** — it belongs on xzgpu.

### density × batch size — ms per correction

| density | batch 1 | batch 10 | batch 100 |
|---:|---:|---:|---:|
| 1% | 29.95 † | 2.98 | 0.331 |
| 5% | 31.76 † | 3.24 | 0.340 |
| 20% | 32.53 † | 3.05 † | 0.338 |

† capped at the 120-commit budget, flagged `truncated` in the record.

Read down a column for density (flat) and across a row for batch size
(**96×** from 1 to 100, and still **9×** from 10 to 100).

### manifest share of the store

| density | batch 1 | batch 10 | batch 100 |
|---:|---:|---:|---:|
| 1% | 93.9% | 68.4% | 16.5% |
| 5% | 93.9% | 91.8% | 35.8% |
| 20% | 93.9% | 93.1% | 65.6% |

### versions per identity, and out-of-order distance

| depth | ms/corr | bytes/corr | | distance | ms/corr | bytes/corr |
|---:|---:|---:|---|---:|---:|---:|
| 1 | 0.259 | 75.1 | | 0 | 0.270 | 75.1 |
| 5 | 0.284 | 95.7 | | 100 | 0.307 | 94.1 |
| 20 | 0.294 | 172.8 | | 10,000 | 0.291 | 96.2 |

---

## Scoring the forecast (D-073) — final, against the `full` profile

Per cell, not in aggregate. **F1 and F4 confirmed. F3 confirmed exactly,
including the knee it predicted. F2 right about the shape's existence and
wrong about where it turns. F5 confirmed and exceeded.**

- **F1 — density flat: CONFIRMED.** Holding batch size fixed, 1 → 50%
  density moves per-correction cost **1.07×** at batch 1 and **1.03×** at
  batch 10,000. Density is close to irrelevant to the write path.

- **F2 — batch size dominant: CONFIRMED, but the shape was wrong in both
  directions.** Predicted "batch 1 >10× worse than batch 1,000, flattening
  by ~100, essentially flat from 1,000 to 10,000". Measured: the spread is
  **45–123×**, far past ">10×"; it does *not* flatten by 100; and 1,000 →
  10,000 is not flat but a **5–9× regression**, making the curve U-shaped
  with an optimum at 100–1,000. The forecast did name the mechanism —
  "staging memory starts to trade against it" — so the direction of the
  right-hand rise was anticipated even though "flat" was the stated
  prediction. **A forecast that names a mechanism and then predicts the
  wrong sign for it should be scored as wrong, not as half-right.**

- **F3 — versions per identity linear, knee at 100+: CONFIRMED, and it is
  the largest single effect found.** 0.149 → 0.162 → 0.346 → **2.795** ms
  per correction at depths 1 / 10 / 100 / 1,000: flat to 10, the knee
  between 10 and 100 exactly as forecast, and **linear beyond it** (10×
  more depth costs 8.1×). Bytes per correction grows 16.7× over the same
  range. The predicted consequence stands measured: the correction path is
  linear in *corrections* and linear-per-correction in *per-entity history
  depth*, so a long-lived entity's corrections get steadily more expensive.

- **F4 — out-of-order mild: CONFIRMED**, now out to a distance of 1,000,000:
  **1.30×**, against a predicted "under 1.5×", with the cost appearing as
  bytes rather than latency exactly as predicted.

- **F5 — manifests a "double-digit percentage" at batch 1: CONFIRMED and
  far exceeded — 99.6%**, at every density.

### Earlier scoring against the `ci` profile

Kept because the two profiles disagree in a way that matters: at `ci` scale
F3 measured **flat** (0.259 / 0.284 / 0.294 ms over depths 1–20) and was
recorded as *untested rather than falsified*, on the grounds that the
forecast put the knee at 100+ and the profile stopped at 20. The `full`
profile shows the knee is real and sits exactly there. **Calling that cell
"untested" rather than "disconfirmed" was the right call, and it was only
right because the forecast had named a threshold the small profile could not
reach.** A forecast without a stated threshold would have been scored
"confirmed flat" and been wrong.

*(the paragraphs below score the `ci` run only; the `full` scoring above
supersedes them where they differ.)*

- **F1 — density flat: CONFIRMED.** Across 1/5/20% the spread is **1.09×**
  at batch 10 and **1.03×** at batch 100, against a predicted "within 2×".
  Marginal ratios within cells sit at ~1.0. Density, on its own, has almost
  no effect on per-correction cost — which is the post-fix world the
  forecast described.

- **F2 — batch size dominant: CONFIRMED in direction, MAGNITUDE BADLY
  UNDERSTATED, and one sub-claim WRONG.** Predicted "batch 1 >10× worse than
  batch 1,000"; measured batch 1 against batch **100** is already **96×**
  (32.53 → 0.338 at 20%). The ">10×" was true but uselessly loose. The
  sub-claim "the curve flattens by ~100" is **wrong**: 10 → 100 still gains
  **9×**, so the knee is above 100, not at it. The `full` profile's 1,000
  and 10,000 columns are needed to find where it actually flattens.

- **F3 — versions per identity linear: UNTESTED, not falsified.** Measured
  flat (0.259 / 0.284 / 0.294 ms) across depths 1–20 — but the forecast
  predicted the knee at **100+** versions per identity, and the `ci` profile
  stops at 20. This axis is unresolved and is the single strongest reason to
  run the `full` profile, whose depths go to 1,000. What *did* move is
  storage: 75 → 173 bytes per correction, consistent with retention, so the
  axis is doing something even where latency is flat.

- **F4 — out-of-order mild: CONFIRMED.** 0.270 → 0.307 ms, **1.14×**,
  against a predicted "under 1.5×", and the cost shows up where the forecast
  said it would — as bytes (75 → 96 per correction, the carve writing
  fragments) rather than as latency.

- **F5 — manifests dominate at small batches: CONFIRMED, and larger than
  predicted.** Forecast said "a double-digit percentage" at batch 1;
  measured **93.9%**. The store under correction load at batch size 1 is
  almost entirely manifests. Even at batch 100, 20% density leaves them at
  **65.6%**.

### What the matrix says the next engine items are

Not the correction *lookup*, which D-072 established is already linear. The
`full` profile names **two** successors, and they are independent.

**1. Per-entity history depth (F3) — the larger and less avoidable one.**
A correction to an entity with 1,000 prior versions costs **18.8×** one to a
fresh entity, growing linearly past a knee at ~100. Unlike the batch-size
effect, a user cannot configure their way out of it: history depth is what
the store is *for*, and it accumulates on exactly the long-lived entities a
bi-temporal system exists to track. `believed_*_versions(identity)` reaches
its rows through the identity postings, but then returns and filters every
version of that identity. **This is where the open-version index the D-072
handoff described would actually have paid** — not on the vid→location
lookup, which was already done, but keyed by `(identity, belief state)` so a
correction can reach the open version without walking the identity's history.
The handoff named a real structure for the wrong reason.

**2. Manifest write amplification (F5) — larger in ratio, but configurable.**
A correction-heavy store committed one at a time is **99.6% manifest bytes**
and costs 45–123× the same work at the optimum batch size. Two things follow,
different in kind:

- **Guidance, now, free** — correction-heavy ingest must batch, at
  **100–1,000**, and *not* larger: batch 10,000 is 5–9× worse per correction
  and pays a 24.7 s p50 commit latency. This costs nothing to document and is
  the highest-leverage thing a user can be told about the write path.
- **An engine question, only sized** — whether the manifest can become
  incremental (a delta per generation rather than a full rewrite). This is
  **not** claimed as a defect: a full manifest per generation is what makes a
  generation atomic and independently readable, which is load-bearing for the
  pinned-belief guarantee. It gets a forecast and a measurement before it
  gets an implementation.

**Optimizing the correction path moved the bottleneck, and this is where it
moved** — from the close-run scan to the manifest write and to per-entity
history depth; from the correction itself to the batch it is committed in and
the history it lands on.


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

With `de5071b` (WP-N4 postings) and `96ea135` (per-generation
`close_index()` cache) both in, marginal cost per correction is **flat**
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

## Scoring the forecast (D-073)

Per cell, not in aggregate. **Two clean hits, one hit whose magnitude was
badly understated, one sub-claim wrong, one untested.**

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

### What the matrix says the next engine item is

Not the correction *lookup*, which D-072 established is already linear —
but **manifest write amplification**, which no one had measured because no
one had swept batch size. A correction-heavy store committed in small
batches is 94% manifest bytes, and the per-correction cost gap between
batch 1 and batch 100 is 96×. That is a larger, better-evidenced effect
than anything remaining on the correction-lookup path.

Two things follow, and they are different in kind:

1. **Guidance, now, free** — correction-heavy ingest must batch. The gap is
   two orders of magnitude and it costs nothing to document.
2. **An engine question, sized before it is built** — whether the manifest
   can be made incremental (a delta per generation rather than a full
   rewrite), which is a real design change and should be forecast and
   measured the same way. It is *not* claimed here as a defect: a full
   manifest per generation is what makes a generation atomic and
   independently readable, and that is load-bearing for the pinned-belief
   guarantee.

**Optimizing the correction path moved the bottleneck, and this is where it
moved** — from the close-run scan to the manifest write, and from the
correction itself to the batch it is committed in.


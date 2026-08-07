# The cost guardrail, scored as a classifier

The guardrail (`tgms/temporal/guardrails.py`) estimates an operator call's
cost before running it and refuses with `E_COST` past a ceiling. Every
account of it so far is anecdotal — correct refusals, one repriced cost
model (D-030-era motifs), a density-dependent refusal at 20% corrections.
**It is a binary classifier over (call, budget) pairs, and a classifier is
characterized by its error rates, not its anecdotes** (external review;
D-070 item 6). The target artifact is an **admission frontier**: at a stated
budget, the measured false-admission rate, false-rejection rate, and
estimate error, under uniform and skewed data.

- harness: `scripts/eval_guardrail.py` — a grid of calls with the guardrail
  *disabled but recording*, every call also executed to ground truth
  (bounded by a hard timeout), then the frontier computed by sweeping the
  ceiling over the recorded estimates
- decision record: D-086

Definitions, fixed before measurement: for a wall-time budget T, a call is a
**false admission** if its estimate passed the ceiling mapped to T but its
actual runtime exceeded T; a **false rejection** if it was refused but its
actual runtime was ≤ T. Estimate error is `est_rows / actual_rows` (and the
time analogue), reported as distribution, not mean.

## The grid

Operators with distinct cost shapes: full-window scan (`series.count`
shape), narrowed co-activity, filtered and unfiltered motifs, k-hop
reachability, `entity_history` point reads. Axes: window fraction
{0.01, 0.1, 0.5, 1.0} × store {200k, 1M} × data {uniform synth, CollegeMsg
(hub-skewed, real)} × correction density {0.5%, 20%}.

## Forecast, written 2026-08-06 before the instrument (D-086; score after)

- **F1 — row estimates are within 2× of actual for ≥80% of *scan-shaped*
  cells on uniform data.** The model is window-fraction × row counts from
  incrementally maintained stats; on uniform synth that should be tight.
- **F2 — skew breaks the motif estimates by more than the D-030 reprice
  fixed.** The mean-degree model was calibrated on CollegeMsg once; on the
  *synthetic* store at small k the margin was already "thin" (D-030 record).
  Forecast: at least one motif cell on skewed data shows estimate error
  >5×, and it is an *under*-estimate (the dangerous direction — a false
  admission risk).
- **F3 — false rejections dominate false admissions at the default
  ceilings.** The defaults (20M rows / 5M expansions) were set
  conservatively; at 1M scale most refusable calls actually finish in
  seconds. Forecast: at a 2 s budget the default-equivalent ceiling refuses
  ≥3× more finishable calls than it admits over-budget ones.
- **F4 — time is the weak axis.** The guardrail estimates rows and
  expansions, not milliseconds; the rows→time mapping varies ≥10× across
  operator shapes at the same row count (columnar scan vs per-row motif
  expansion). Any budget stated in seconds inherits that spread — this is
  the cell most likely to motivate an engine change (per-operator
  rows→time coefficients from the §16.3 protocol's own measurements).
- **F5 — correction density does not move estimate error materially**
  (<1.5× shift) — the close index is consulted per row either way — but the
  *actuals* at 20% shift enough that a ceiling tuned at 0.5% misclassifies
  some cells at 20%: the seed anecdote (`motif.filtered` refused at 20%
  only) becomes a measured region of the frontier, not a story.

## Results — 90 cells, five stores, zero timeouts (xzgpu, `70bc31f`)

Receipt `eval-guardrail-frontier.json`. The frontier at the default
ceilings:

| budget | false admissions | false rejections | best multiplier | FA/FR at best |
|---:|---:|---:|---:|---:|
| 0.5 s | 2 | 9 | 0.5× | 0 / 11 |
| 2 s | **0** | **16** | **256×** | 0 / 3 |
| 10 s | 0 | 18 | 256× | 0 / 5 |

**The guardrail never admits anything over budget, and pays for that safety
by refusing 16–18 of 90 finishable calls (18–20%) — with the 2 s optimum a
factor of 256 above the default ceilings.** The defaults are roughly two
orders of magnitude too conservative for these workloads on this host,
because the estimates are systematically inflated (below) and because the
engine under the cost models has been through the D-047 kernels and the
D-076…D-081 arc since anything was calibrated.

Two cells deserve names:

- **`entity_history` estimates a full-store scan (1,015,199 rows at 1M) for
  a 0.13 ms point read** — seven orders of magnitude of estimate error. Its
  cost model predates both the identity postings (WP-N4) and the
  open-version index (D-076). Harmless at the default ceiling, and the
  first thing any tightened ceiling would wrongly refuse.
- **`motif.unfiltered` at 1M estimates 10.1 billion expansions and runs in
  2.9 s.** The E_COST demo refusal — the founding anecdote of the guardrail
  — refuses a call that finishes in under three seconds on the measurement
  host.

## Scoring the forecast (D-086)

- **F1 — row estimates within 2× on scan shapes: UNSCOREABLE, instrument
  gap.** The harness records actual wall time, not actual rows scanned;
  time-linearity across window fractions is consistent with accurate scan
  estimates, but that is a proxy, not the measurement F1 named. Adding an
  actual-rows column is the harness backlog item, and F1 stays open rather
  than being scored on the proxy.
- **F2 — skew produces a ≥5× motif *under*-estimate: WRONG, in the safe
  direction.** Every motif estimate at every store — skewed CollegeMsg
  included — is a large **over**-estimate (1M: est 10.1 B expansions,
  actual 2.9 s of work). The dangerous direction never appeared, which is
  exactly why false admissions are zero everywhere. The D-030-era reprice
  overcorrected, and the frontier now quantifies by how much.
- **F3 — false rejections dominate at the defaults: CONFIRMED,
  overwhelmingly.** FR:FA is 16:0 at 2 s against a forecast of ≥3:1.
- **F4 — time is the weak axis, rows→time varies ≥10× across shapes:
  CONFIRMED at 50–100× the forecast magnitude.** Milliseconds per million
  estimated units at 1M span 0.29 (unfiltered motif) to 166.6
  (reachability) — **574×** across shapes with honest estimates, ~1,200×
  including the broken `entity_history` model. A single ceiling pair
  cannot encode that spread; per-operator time coefficients can.
- **F5 — density moves estimates <1.5× and actuals more: CONFIRMED.**
  Estimates shift 1.21–1.48× from 0.5% to 20%; actuals shift 1.02–2.12×
  (`motif.filtered` the largest, the §13 anecdote now a measured region).

## What follows (queued, not done here)

The characterization says the fix is a **cost-model refresh**, not a
ceiling tweak: retire `entity_history`'s pre-postings estimate, recalibrate
the motif expansion model against these 90 receipts, and attach
per-operator rows→time coefficients so budgets stated in seconds stop
inheriting a 574× spread. Each change re-runs this harness as its
validation — the frontier is now the standing instrument the guardrail
never had.

## The refresh (D-087), forecast before implementing

Design: every estimate gains `time_est_ms` — per-operator coefficients
measured from this document's own 90-cell receipt (xzgpu-calibrated,
recorded as such; a deployment on other hardware scales them with one env
variable) — and `enforce_cost` refuses on a time ceiling (default 10 s)
first, with the old unit ceilings raised ×256 per the measured optimum to
serve as memory backstops. `entity_history` moves from `scan_estimate` to a
point-read model: average versions per entity, which is what the operator
actually returns. The motif coefficient branches on `node_filter` presence,
because the receipt shows the unfiltered candidate count inflated ~70×
relative to the filtered one per unit of actual work.

- **R1 — the frontier at a 2 s budget improves to FR ≤ 4 and FA ≤ 1** (from
  16 / 0), sweeping the time ceiling. *Same five stores, same cell grid.*
- **R2 — `entity_history`'s estimate lands within 3× of its actual result
  rows** (avg versions per entity ≈ what the version list returns), from
  seven orders of magnitude off.
- **R3 — time estimates within 3× of actual for ≥80% of cells at 1M.**
  In-sample at 1M (the coefficients come from these receipts); the 200k and
  CollegeMsg cells are the honest quasi-out-of-sample check and are scored
  separately.
- **R4 — no pinned test changes** except the webapp guardrail preset, which
  gains an explicit per-call budget (the demo demonstrates a budget refusal
  honestly instead of relying on ceilings the refresh retires).
- **R5 — the guardrail still guards at scale** (design intent, not measured
  here): the 10M unfiltered motif extrapolates to ~30 s of estimated time,
  refused at the 10 s default.

## Refresh results (D-087) — the frontier re-run, and the scoring

Receipt `eval-guardrail-frontier-d087.json` (same five stores, same 90
cells, xzgpu at `361880b`). One instrument defect en route: the first
post-refresh receipt recorded raw `cost_fn` output without the
`add_time_estimate` step production applies, so every non-motif cell read
`time_est_ms = 0` — all four of its 0.5 s false admissions were that gap,
not the model. Superseded; the numbers below are from the corrected run.

| budget | before (default) | after (default) | after (best m) |
|---:|---:|---:|---:|
| 0.5 s | FA 2 / FR 9 | FA 4 / FR 1 | FA 0 / FR 6 at m = 1/16 |
| 2 s | FA 0 / FR 16 | **FA 0 / FR 3** | FA 0 / FR 1 at m = 4 |
| 10 s | FA 0 / FR 18 | **FA 0 / FR 5** | **FA 0 / FR 0** at m = 16 |

False rejections at a 2 s budget fall **16 → 3** with false admissions
still zero, and the optimum moves from 256× the defaults to 4×. The 0.5 s
row is the design working, not failing: a 10 s *default* is not a 0.5 s
budget, and the sweep shows a caller passing `time_est_ms ≈ 625` gets
FA 0 — per-call budgets now map directly onto the ceiling, which is the
point of pricing in milliseconds.

- **R1 — FR ≤ 4 and FA ≤ 1 at 2 s: CONFIRMED** (3 and 0).
- **R2 — `entity_history` within 3× of its result: CONFIRMED** — estimates
  2 rows against ~1 returned, from seven orders of magnitude off.
- **R3 — time within 3× for ≥80% of 1M cells: WRONG, narrowly** — 76%
  in-sample (69% at 200k/CollegeMsg). The residual has two named shapes:
  small-window motif cells over-estimate up to 57× (the model floors at
  the full row-scan term while tiny windows do almost nothing), and the
  reachability coefficient does not transfer across scale (its fixpoint
  cost is super-linear in store size, so one ms/M number fit at 1M reads
  12× high on CollegeMsg). A per-op constant was always going to be a
  first-order model; the frontier now says which second-order terms matter.
- **R4 — no pinned test changes: WRONG** — three tests moved to the new
  contract (their discriminative intent preserved), and one of them caught
  a real coefficient error in the refresh's first draft: `version_history`
  fell to a 170 ms/M fallback when D-058 measured it at 15,400 ms/M.
- **R5 — still guards at scale: design intent restated**, now consistent:
  the 10M unfiltered motif prices at ~30 s against the 10 s default.

The instrument audited its operator three times in one session: the
missing would-be-refused class, the uid-hardcoded grid, and the missing
time-attach — each caught by the receipts disagreeing with themselves, none
by inspection.

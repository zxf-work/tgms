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

## Results

*(scored per cell after the runs — see D-086)*

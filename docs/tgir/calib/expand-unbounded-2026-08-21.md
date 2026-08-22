# Calibration — unbounded `Expand`

**Measured 2026-08-21.** `TGIR_SPEC.md` §2.3 makes this an M3
obligation rather than an assumption: the unbounded form's `time_est_ms` is the
least-calibrated number in the guardrail, and a wrong value is invisible in both
directions — too high refuses rows the forecast predicts unlocked, too low
admits a fixpoint that runs until the runtime backstop fires.

## Provenance

| | |
|---|---|
| git SHA | `a64338b69956` |
| store | `collegemsg.eventlog.jsonl`, rebuilt by **replay** (never ingest) |
| store digest | `7efd7f4f0ec02cb8` |
| backend | native |
| entities / edge versions | 1,899 / 59,835 |
| machine | macOS-26.5.2-arm64-arm-64bit |
| repeats per shape | 3 (median reported) |
| unit | one *expansion* = one neighbour visited, the same unit `cost.py` estimates |

## The two shapes §7.3 fixes

| shape | seeds | rows out | expansions | median ms | ms/M |
|---|---|---|---|---|---|
| ten bound anchors | 10 | 18920 | 1,196,620 | 219.368 | 183.3 |
| hoisted over the whole population | 1899 | 3611834 | 228,434,774 | 35815.101 | 156.8 |

## Coefficient

```
TIME_COEFF_MS_PER_M["tgir_expand_unbounded"] = 183.3
```

The **larger** of the two shapes is taken, so the estimate over-refuses rather
than over-admits on the shape it was not measured against — which is §2.13's own
instruction for an uncalibrated operator, applied to a calibrated one at its
worst measured shape.

**What this does not establish.** One store, one machine, one backend. The
reachability coefficient is a super-linear fixpoint and
`EVIDENCE_MODEL` §9 records that it does not transfer across scale; this receipt
fixes the admission arithmetic at *this* scale and is the number the freeze's
secondary admission axis is reported against. A deployment on slower hardware
scales it with `TGMS_TIME_COEFF_SCALE`, exactly as the operator coefficients
scale.

# Dataset cards

## synth (200k / 1M / 10M events)

Generator: `scripts/eval_harness.py::build_dataset` — deterministic from
the scale alone (splitmix64 endpoints, no runtime randomness). Properties,
each added after its absence made a comparison vacuous (lessons §9a):
constant average degree (~10) via |V| and edge lifetime scaling together;
community structure (50-node blocks, 70% intra) so motifs exist; one
deliberate 10× burst; a second belief epoch (corrections + retractions,
including interval-splitting corrections so `props_changed` is exercised);
`tt_epoch1` captured at build so belief probes discriminate. Sizes: |V| =
scale/100, edge versions ≈ scale + scale/200 corrections.

## CollegeMsg (frozen replay)

`benchmarks/frozen-v1/collegemsg.eventlog.jsonl` — 59,835 instantaneous
messaging events, real timestamps (Apr–Oct 2004), 1,899 users, heavy
degree skew, no corrections. Replay digest is byte-frozen across backends.
Known dataset truths: instant snapshots and strict-overlap joins are
legitimately thin (microsecond intervals); the belief probe pins
mid-ingestion state; degree skew is what exposed the motif cost-model
false positive.

## sx-mathoverflow / sx-superuser (Stack-Exchange, typed edges)

SNAP Stack-Exchange interaction networks, three raw files per dataset
(one per edge type: `A2Q` answer-to-question, `C2Q` comment-to-question,
`C2A` comment-to-answer), streamed in that fixed order — so the recorded
event log is deterministic but valid time interleaves across types, a
real tt≠vt workload the single-file datasets don't produce. Verified at
load against SNAP's published stats:

- **sx-mathoverflow**: 506,550 events / 24,818 nodes / 2,350 days
  (A2Q 107,581 · C2Q 203,639 · C2A 195,330)
- **sx-superuser**: 1,443,339 events / 194,085 nodes / 2,773 days
  (A2Q 430,033 · C2Q 479,067 · C2A 534,239)

The typed edges make `rel_types`-filtered operators meaningful on real
data for the first time (CollegeMsg and wiki-talk are single-type).

## wiki-talk (temporal)

SNAP `wiki-talk-temporal`: 7,833,140 events / 1,140,149 nodes / 2,320
days, single edge type `TALK`, instantaneous. The 10M-class real graph
with extreme hub skew (admins and bots) — selected as the guardrail
stressor: the D-086 frontier's skew forecasts (F2) were written against
synthetic skew, and this is the real thing. Build the store on a server;
the raw file alone is 7.8M lines.

## Loading rule (all datasets, all systems)

One recorded event log per dataset; every system loads *that* (TGMS
backends by replay, baselines from the canonical rows a native store
produces). Independently built stores of the same data legitimately differ
in tt and every derived id — the first differential run failed exactly
this way (D-023).

## synth-iv-60k (interval-valid-time synth)

Deterministic interval-valid-time synth, adopted 2026-08-28
(M5_CARVE_POPULATION_PROPOSAL, DECISION 5 ratified by the owner) to
answer §13.10's carve-arm question, which bitcoinotc/collegemsg cannot:
both are instantaneous-event stores (every believed interval `[t, t+1)`),
so no outside-window correction can ever carve a version their windows
read — measured twice (M4 §10, M5 Addendum 5).
`scripts/build_synth_iv_store.py` reuses the synth community/degree
structure (600 nodes, avg degree ~10, one burst band) and replaces the
constant 5%-of-extent edge lifetime with a log-uniform interval-length
distribution spanning 0.5%–50% of the extent, drawn from an independent
splitmix64 stream per event. 60,000 events / 600 nodes, epoch-1 only.
Event-log op payloads are byte-identical across independent builds;
`store_identity` is not (HLC tt, D-023). The builder self-verifies a
fitness probe at build time: one seeded outside-window correction must
change an `aggregate_events` duration answer, printed in the card.
Scored population for the carve arm only, per the M5 campaign freeze's
Addendum 6.

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

## Loading rule (all datasets, all systems)

One recorded event log per dataset; every system loads *that* (TGMS
backends by replay, baselines from the canonical rows a native store
produces). Independently built stores of the same data legitimately differ
in tt and every derived id — the first differential run failed exactly
this way (D-023).

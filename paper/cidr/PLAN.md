# CIDR 2027 submission plan (due 2026-08-04 23:59 PT)

Thesis: **the database as the trusted computing base for LLM agents.**
Three principles: (1) contract-bearing algebra, not query generation;
(2) evidence support is multidimensional ⟨correctness, completeness,
belief time, approximation, provenance⟩; (3) answers need a belief
state, not a snapshot. Format: ACM sigconf, 6 pages incl. references,
single-blind. One running example threads the whole paper
(belief-state + reachability + motif + arithmetic + correction + tamper).

## Inventory — already have
- Frozen campaign (D-018): CollegeMsg/email-Eu/synth test = 290 tasks
  (≥100 requirement met); healing run in flight, ETA ~2026-07-17.
- Metrics per row: EM/F1, first-plan validity, execution success, UCR
  (gated + raw via B3), repair attempts (n_llm_calls), wall_s, tokens.
- Two model configs (14B primary + 7B models phase). Second FAMILY
  (Phi-4-mini) optional — decide 07-19 after campaign lands.
- Output-contract lesson with receipts (0.00→1.00, first_oss_matrix).
- Truncation-taint machinery + the 100-vs-343 live example.
- Correction probes + representational argument for latest-state
  impossibility (b2 evidence).
- C2 fault injection 500/500 (count±1, uid-swap) + unit-scale others.
- Scaling: bench_ops @1e5/1e6 done; **1e7 run launched 2026-07-16**
  (runs/scale10m.log on xzgpu, CPU-only, parallel with GPU campaign).
- Trust-boundary figure: adapt paper/fig_arch.tex (already color-codes
  LLM vs verified vs deterministic; add explicit boundary).
- Trace viewer for the tampering demo.

## To build (code, all main-branch, no oracle/test changes)
1. Ablation flags: (a) `disable_output_contracts` in static verifier,
   (b) `disable_truncation_taint` in executor/claim-verifier. Config-
   threaded like llm_guided. ~small.
2. New mutation classes in eval/faults.py: truncated-page count,
   wrong-tt value, entity-set ±1 member, reversed interval, shifted
   window, wrong-step citation, quarantined-summary claim, wrong
   bucket. 50–100 cases/class, per-class table. CPU-only.
3. LLM-vs-DB latency split per row (trace step latencies already
   recorded by executor — aggregate into row).

## Experiments queue (GPU, after heal completes)
- E1 no-output-contracts: dev split, 14B (+7B), ours only.
- E2 no-taint: page-limit-exceeding task subset; measure
  supported-but-incomplete rate.
- E3 latest-state-only: have from campaign (b2 probes) + analysis.
- E4 optional second family (Phi-4-mini, ours only, test split).

## Schedule
- 07-16..17 freeze argument (this plan) + skeleton + flags + mutators.
- 07-18..23 campaign lands → score; run E1/E2 (+E4?); mutation table;
  10M figure; auto-generate all tables from receipts.
- 07-24..27 full 6-page draft.
- 07-28..30 PI adversarial review (4 questions per brief).
- 07-31..08-02 tag release, receipts, trace visualization, number audit.
- 08-03..04 final check + PI submits (in-person Amsterdam requirement).

## Cut list (keep out of this paper)
Operator-by-operator semantics; Kùzu-vs-DuckDB details; HLC details;
CLI/GUI; prompt-security depth; memory beyond correction-invalidation;
motif implementation; long benchmark narratives.

## EXPERIMENTS COMPLETE — 2026-07-24
All runs closed, zero error rows except phi heal (27 rows, healing now).
Scale study (frozen CollegeMsg, canonical store, iTiger): ours 7B 0.138 /
14B 0.340 / 32B 0.628 (probes 1.000) / 72B-AWQ 0.511 (probes 0.31 — AWQ,
not scale, is the bottleneck); baselines flat <=0.28; UCR 0.000 in every
cell. Fair baseline (H100, native 32k, k=20 x 24-event chunks): b1 0.021
vs ours 0.362 — RAG got WORSE when un-hobbled, at 32.5k vs 6.5k
tokens/task. E1/E2 replicated exactly at equal budget. Cross-family:
Llama-8B 0.043, Phi-4-mini 0.015 — capability threshold real; safety
invariant. Writing sweep: receipts table -> repo v0.3.0 -> arXiv v3 ->
CIDR -> site/blog per the 2026-07-22 plan.

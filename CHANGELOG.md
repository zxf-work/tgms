# Changelog

## v0.2.0 — 2026-07-20

Frozen-test campaign release: the pre-registered evaluation is complete.

- Results (frozen splits, D-018; receipts in runs artifacts and
  docs/TECHNICAL_REPORT.md): CollegeMsg 94 tasks x 3 seeds — TGMS 0.408
  exact match vs 0.106 / 0.064 / 0.152 for vector-RAG, static-graph RAG,
  text-to-Cypher; all paired-bootstrap deltas significant. Correction
  probes 0.897 (CollegeMsg) and 0.846 (email-EU) vs zero for latest-state
  baselines. email-EU 0.309, synthetic 0.314. End-to-end verification:
  raw unsupported-claim rate 7.8% -> 0.000 gated, costing one EM point.
- Verifier hardening: claims whose `from` provenance pointer names an
  uncited step are unverifiable; malformed claims can never crash
  verification (verdict unverifiable instead).
- Extended fault-injection classes (eval/faults_ext.py): wrong belief
  state, truncated-page counts, entity add/drop, ordering swaps, unit
  confusion, wrong-step citation — per-class detection tables; documented
  known-negative for entity under-claiming.
- E1/E2 ablation flags (`ablate_output_contracts`,
  `ablate_truncation_taint`) and guided-decoding support (rejected at 14B
  after A/B: D-019).
- Campaign infrastructure: phase driver with self-healing passes
  (infra-failure rows are never treated as results), vLLM watchdog for
  long-run serving on sm_75, per-task token accounting, frozen suites
  vaulted under benchmarks/frozen-v1/.
- Decisions D-018..D-021; 10M-event operator benchmarks.

## v0.1.0 — 2026-07-10

First public release: Phase 1–2 research prototype.

- Bi-temporal substrate: valid-time x transaction-time version rows,
  append-only write-ahead event log (replay-identical digests across the
  Kùzu and DuckDB backends), hybrid logical clock, interval-carving
  assert/retract/correct semantics.
- Verified operator algebra O1–O13: typed, deterministic, bounded,
  cost-guarded, bi-temporal by default, output-contracted; 500 randomized
  oracle cases per operator; bi-temporal immutability metamorphic tests.
- Planner–Executor–Verifier agent layer: constrained plan IR with $ref
  binding, static validation (grounding rule, output-field contracts,
  temporal sanity, cost pre-check), deterministic executor with
  content-addressed traces, trace-grounded claim verifier with truncation
  taint (fault-injection acceptance: 500/500 detected, 0 false positives).
- Evolution memory with number-checked digests and staleness quarantine
  under corrections.
- Task-suite generation with program-computed gold (incl. bi-temporal
  correction probes), baselines (vector-RAG, static-graph RAG,
  text-to-Cypher), matrix harness with paired-bootstrap statistics and
  determinism receipts.
- Interfaces: Python library, MCP server, CLI, static trace viewer,
  interactive guided demo GUI.
- Dev-split results (CollegeMsg, Qwen2.5-7B/14B): see
  docs/TECHNICAL_REPORT.md.

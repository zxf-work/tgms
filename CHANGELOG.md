# Changelog

## v0.3.0 — 2026-07-24

Post-campaign studies release: model scale, fair baselines, portability.

- Results (frozen splits, canonical store; TECHNICAL_REPORT 8.2c): the
  operator-backed advantage grows with model capability (EM 0.138 -> 0.340
  -> 0.628 across Qwen2.5 7B/14B/32B fp16; probes saturate at 1.000 at
  32B); 72B-AWQ regression isolates quantization as the planning
  bottleneck; vector-RAG at its intended k=20 breadth scores 0.021 vs ours
  0.362 in the same run; unsupported-claim rate 0.000 in every measured
  cell (4 scales, 3 families, 2 quantizations, 2 clusters).
- `tgms replay`: rebuild byte-identical stores from a recorded event log
  (preserves transaction times); canonical CollegeMsg event log + memory
  vaulted under benchmarks/frozen-v1/ (D-023 — a fresh ingest does NOT
  reproduce the store the frozen gold was computed on).
- Robustness from live campaigns: Kùzu buffer pool bounded (physical-RAM
  default OOMs in cgroups); b5 Cypher attempts execute in killable child
  processes with a hard wall-clock bound (cooperative query timeouts
  cannot interrupt every generated query); b1 chunk size plumbed
  (b1_chunk_events); 60-min LLM request budget; HF offline serving mode.
- Slurm tooling for HPC clusters (scripts/itiger_job.slurm): serve+eval
  in one right-sized allocation, per-job ports and store copies, fresh
  per-job b5 DB rebuilds.
- Decisions D-022..D-024.

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

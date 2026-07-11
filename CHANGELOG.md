# Changelog

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

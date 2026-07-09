# TGMS — Agent-Native Temporal Graph Management System

A research prototype of a **bi-temporal property graph management layer** whose query
surface is a small algebra of **verified temporal operators exposed as tools to LLM
agents**, plus a Planner–Executor–Verifier agent layer that performs temporal mining
and reasoning against it.

- **Bi-temporal model**: every node/edge version carries valid time `[vt_s, vt_e)` and
  transaction time `[tt_s, tt_e)`, distinguishing *evolution* (the edge ended) from
  *correction* (we were wrong about the edge).
- **Operator algebra** (O1–O13): snapshots, diffs, time-respecting reachability/paths,
  δ-temporal motifs, metric time series, burst detection, interval joins — all typed,
  deterministic, bounded, and oracle-verified.
- **Agent layer**: constrained Plan-IR planner, deterministic DAG executor with
  content-addressed execution traces, and a trace-grounded claim verifier.

See `docs/` and the Phase 1–2 implementation spec for details.

## Quickstart

```bash
uv sync                 # install (Python 3.12, managed by uv)
make test               # property/oracle test suite
```

## Layout

```
tgms/core       clock, bi-temporal data model, error taxonomy
tgms/storage    StorageAdapter ABC, Kùzu + DuckDB backends, event log, TCSR index
tgms/temporal   operator algebra O1–O13 + brute-force oracle
tgms/tools      tool schemas + MCP server / in-process ToolRouter
tgms/agent      Plan IR, planner, executor, verifier, evolution memory
tgms/data       dataset loaders + synthetic generator with planted patterns
tgms/eval       task suites T1–T4, baselines, harness, metrics
```

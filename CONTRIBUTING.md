# Contributing

TGMS is a research prototype developed against a written specification with
process rules (spec §8) that are enforced mechanically. The two that affect
every PR:

1. **Test ownership.** `tests/` and `tgms/temporal/oracle.py` are
   ground truth. Never modify them in the same commit as implementation
   code; test-only commits are prefixed `[tests]` with a written
   justification. CI rejects mixed commits
   (`scripts/check_commit_hygiene.py`).
2. **No silent scope changes.** Operator semantics, guardrail ceilings,
   schema fields, IR grammar, and pre-registered evaluation thresholds may
   not be changed to make something pass. Propose changes as a dated entry
   in `docs/DECISIONS.md` (context → proposal → consequence) and wait for
   maintainer sign-off.

Practical notes:
- `make setup` (uv, Python 3.12), `make test`, `make lint`,
  `make test-full` (500-case oracle sweep).
- Hot-path rule: no per-edge/per-node Python loops in operator kernels —
  columnar NumPy/Arrow or engine pushdown only (Python loops are fine in
  the oracle and tests).
- Every operator change must keep 100% oracle agreement; new operators need
  an oracle implementation, property tests, output-field declarations, and
  a tool-manual entry.
- Raw dataset files are never committed; loaders + SHA-256 manifests only.

---

## Where to start

If you're new to TGMS and want to contribute, here are entry points that don't require understanding the full research architecture:

### Documentation improvements
Clarify tutorials, expand examples, improve docstrings, or fix inconsistencies. Start with `docs/tutorials/` (once created as part of the adoption sprint) and `docs/ROADMAP.md`. No code review needed for pure documentation — keep it readable for both researchers and users.

### Data loaders for new event formats
Add support for new temporal event data sources by implementing a loader following the pattern in [`tgms/data/loaders.py`](tgms/data/loaders.py). Each loader yields events with `{src, dst, rel_type, vt_s, vt_e?, props?}` and produces a dataset card. See the existing SNAP/CollegeMsg loaders as models — the pattern is compact and self-contained.

### Trace viewer improvements
The trace viewer (`tgms/tools/trace_viewer.py` and web UI in `tgms/tools/webapp.py`) renders query execution traces as interactive HTML — step-by-step operator cards, latency, row counts, and claim verification badges. Improvements might include better DAG visualization, richer operator detail, or new badge types. Changes here are immediately visible in the tool's output without touching core operators.

### Benchmark extensions
Add new query workloads or correctness test cases under `benchmarks/` and tie them into the evaluation harness (`scripts/bench_corrections.py`, `scripts/bench_sequences.py`). Each extension documents its query family and expected result, making it easy to spot regressions. See `docs/bench_corrections.md` and `docs/bench_ops.md` for the existing benchmark taxonomy.

### Filing and addressing good-first-issues
Watch the repo's issue tracker for issues labeled `good-first-issue` — typically small correctness gaps, missing edge cases, or operator enhancements that don't affect the core IR. These are sized to fit a single session and come with clear acceptance criteria.

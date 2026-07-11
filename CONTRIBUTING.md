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

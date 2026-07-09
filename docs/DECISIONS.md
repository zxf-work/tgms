# DECISIONS.md — dated decision log (spec §8.2)

Format per entry: **context → proposal → consequence**. Entries marked
*(awaiting sign-off)* need PI approval; everything else is documented for the
record. Spec §8 (process rules) was adopted on 2026-07-09; commits before the
adoption marker predate the test-ownership rule (§8.1) and mixed tests with
implementation — hygiene checking starts at the marker recorded in D-010.

---

## D-001 — 2026-07-09 — Version-id formula refined
- **Context:** Spec WP1.1 defines `vid = hash(eid, tt_s)`. One batch can split
  an existing version into two fragments (carve left + right remainder) at the
  same `tt_s`, which collides.
- **Proposal:** `vid = hash(identity, tt_s, vt_s)` — a strict refinement,
  unique because believed valid intervals of one identity are disjoint.
- **Consequence:** No observable behavior change beyond vid strings;
  implemented in `tgms/storage/base.py::_vid`.

## D-002 — 2026-07-09 — O4 `delta_max_wait` gets exact path semantics
- **Context:** Spec WP1.3 suggests one-pass earliest-arrival relaxation with a
  wait cap. With a wait cap, greedy single-label relaxation is
  *schedule-dependent* (a smaller arrival can disable a later edge), so its
  result is not a well-defined function of the store — it cannot be
  oracle-tested honestly.
- **Proposal:** Path-based exact semantics: reachable iff some time-respecting
  path satisfies all wait constraints. Engine: vectorized fixpoint when delta
  is absent (exact via prefix-optimality); exact multi-label (node, arrival)
  search when delta is set. Oracle implements the same definition
  independently. Rust rewrite candidate per §7.1 if profiling demands.
- **Consequence:** O4-with-delta is slower (bounded by MAX_EXPANSIONS guard)
  but exact and deterministic.

## D-003 — 2026-07-09 — `result_digest` covers the payload only; O1 censors tt_e
- **Context:** Bi-temporal immutability requires results pinned to a past
  `as_of_tt` to be byte-identical under later writes. `dataset_extent`
  reflects current beliefs, and raw `tt_e` on returned versions leaks
  post-as_of knowledge (a later correction stamps a close time onto rows
  believed at the pinned tt).
- **Proposal:** `result_digest = SHA-256(canonical payload)` excluding
  `op/args_echo/dataset_extent`; `entity_history` reports `tt_e = OPEN_END`
  for any `tt_e > as_of_tt`.
- **Consequence:** The operator-level immutability metamorphic test passes;
  verifiers pin evidence by payload content.

## D-004 — 2026-07-09 — Failed write batches: log-then-skip-deterministically
- **Context:** Write-ahead means the batch is logged before it is applied;
  application can fail (e.g., retract with no target).
- **Proposal:** Adapters get begin/commit/rollback; a failed batch is rolled
  back but stays in the log; replay re-applies deterministically, re-fails
  identically, and skips.
- **Consequence:** Log and store never diverge; replay digests match.

## D-005 — 2026-07-09 — Burst scores quantized before thresholding
- **Context:** Engine (numpy) and oracle (pure python) differ in float
  summation order; scores at the flag threshold could flip.
- **Proposal:** `score = round(score, 9)` before comparison, identically in
  both implementations (matches the global 9-decimal float canonicalization).
- **Consequence:** Deterministic flags; hypothesis sweep clean.

## D-006 — 2026-07-09 — Fault-injection "detected" includes `unverifiable`
- **Context:** A mutated timestamp/uid that exists nowhere in evidence comes
  back `unverifiable` (not grounded), not `unsupported` (contradicted).
- **Proposal:** Detection = claim no longer verifies as supported
  (`unsupported` or `unverifiable`); FP measured symmetrically.
- **Consequence:** Matches C2 intent ("catches injected false claims");
  unit-scale readout: 100% detection, 0% FP on count/entity/ordering.

## D-007 — 2026-07-09 — Project venv relocated out of iCloud
- **Context:** Repo lives in iCloud-synced ~/Documents; iCloud sets the macOS
  hidden flag on .pth files; CPython 3.12+ silently skips hidden .pth files,
  breaking the editable install repeatedly.
- **Proposal:** `UV_PROJECT_ENVIRONMENT=$HOME/.venvs/tgms` (Makefile export +
  README note).
- **Consequence:** Stable installs; recommend moving the repo out of iCloud.

## D-008 — 2026-07-09 — Dependencies and licenses (spec §8.6)
- kuzu (MIT), duckdb (MIT), numpy (BSD-3), pyarrow (Apache-2.0),
  networkx (BSD-3, oracle only), jsonschema (MIT), pydantic (MIT),
  pandas (BSD-3), pyyaml (MIT), litellm (MIT), fastmcp (Apache-2.0),
  hypothesis (MPL-2.0, dev only), pytest (MIT, dev only), pytest-cov (MIT,
  dev only), ruff (MIT, dev only); optional extras: faiss-cpu (MIT),
  sentence-transformers (Apache-2.0). No raw dataset files are committed;
  loaders + SHA-256 manifests only (ICEWS: downloader-only pending
  redistribution-terms check).

## D-009 — 2026-07-09 — Current-view cache deferred *(awaiting sign-off)*
- **Context:** WP1.1 asks for CurrentNode/CurrentEdge tables refreshed by the
  write path, to make non-temporal queries cheap.
- **Proposal:** Defer: after the M3 column-projection/pushdown optimizations,
  all operators meet the informal latency targets at 1M events without it
  (see docs/bench_ops.md); the cache adds write-path complexity and a new
  consistency invariant. Revisit at the 10^7-event committed scale point.
- **Consequence:** One WP1.1 sub-item consciously open; correctness testing
  target ("current-view ≡ snapshot(now, now)") moot until built.

## D-010 — 2026-07-09 — Spec v1.1 adopted; §8 hygiene enforcement begins
- **Context:** Spec updated (§8 process rules; provenance columns; memory
  invalidation; prompt-injection policy; B5 baseline; statistical treatment).
- **Proposal:** Hygiene rule §8.1 enforced by `scripts/check_commit_hygiene.py`
  from this commit forward (base marker = the commit introducing this file);
  earlier history mixed tests+implementation and is grandfathered.
- **Consequence:** Implementation and test changes land in separate commits;
  test-affecting commits are labeled `[tests]` with justification and await
  human approval per §8.1.

## D-011 — 2026-07-09 — Provenance columns (spec v1.1 WP1.1) added
- **Context:** v1.1 reserves `source` ('ingest' | 'agent') and
  `provenance_ref` on NodeVersion/EdgeVersion and in event-log records, so
  Phase 3 agent write-back needs no migration.
- **Proposal:** Add both fields end-to-end (model dataclasses, both adapters,
  op records) with defaults `source='ingest'`, `provenance_ref=NULL`; no write
  operator exposed to the planner.
- **Consequence:** Store digests change (fields are part of logical content);
  stores are regenerated from event logs / raw data — no benchmark or gold
  regeneration needed since none are frozen yet.

## D-012 — 2026-07-09 — B5 text-to-query baseline + statistical treatment (v1.1)
- **Context:** v1.1 adds B5 (direct Cypher against vanilla Kùzu, same repair
  budget, verifiability-rate contrast) and pre-registered statistics (paired
  bootstrap, 10k resamples, 95% CIs, power check; T4 target raised to
  n≈150–200).
- **Proposal:** Implement with WP2.6 baselines/harness (M6/M7, not yet built);
  T4 authoring targets the raised n.
- **Consequence:** Tracked in the M6/M7 task list; no code yet.

## D-013 — 2026-07-09 — Model matrix: open-source first, commercial deferred
- **Context:** Spec WP2.6 lists `claude-sonnet-4-6` + one OpenAI flagship as
  the frontier tier. PI direction (2026-07-09): use open-source models (Qwen
  family and peers that fit the 24GB Turing GPU) for now; commercial models
  move to a future phase.
- **Proposal:** Serve ≤14B open-source instruct/reasoner models via vLLM on
  xzgpu (Quadro RTX 6000, sm_75 → fp16 only, AWQ for 14B). Start:
  Qwen2.5-7B-Instruct; then Qwen2.5-14B-Instruct-AWQ, Phi-4-mini, a distilled
  reasoner. The "frontier vs small" C3 gap readout is re-scoped to
  "largest servable open model vs smaller open models" until commercial
  models are added; C1/C2 readouts are unaffected (system-vs-baseline under
  identical models).
- **Consequence:** configs/matrix-dev-oss.yaml is the active dev config;
  llm_api_base/llm_api_key plumb LiteLLM to the vLLM endpoint. Model version
  strings recorded per §8.4 receipts remain mandatory.

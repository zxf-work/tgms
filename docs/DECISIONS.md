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
  the lab GPU node (Quadro RTX 6000, sm_75 → fp16 only, AWQ for 14B). Start:
  Qwen2.5-7B-Instruct; then Qwen2.5-14B-Instruct-AWQ, Phi-4-mini, a distilled
  reasoner. The "frontier vs small" C3 gap readout is re-scoped to
  "largest servable open model vs smaller open models" until commercial
  models are added; C1/C2 readouts are unaffected (system-vs-baseline under
  identical models).
- **Consequence:** configs/matrix-dev-oss.yaml is the active dev config;
  llm_api_base/llm_api_key plumb LiteLLM to the vLLM endpoint. Model version
  strings recorded per §8.4 receipts remain mandatory.

## D-015 — 2026-07-10 — Interactive demo GUI (PI-directed scope extension)
- **Context:** Spec §7.3 scopes Phase 1–2 UI to the static trace viewer; an
  interactive shell was listed as a Phase-3 demo-track extension. PI request
  (2026-07-10): an interactive guided GUI showing how TGMS is used, with
  prepared test cases, served from the lab GPU node and visited from a local machine.
- **Proposal:** `tgms webapp` — a stdlib-only HTTP server (no new
  dependencies) embedding a single-page guided tour: dataset card → verified
  operator playground (preset calls) → ask-the-agent with curated suite
  tasks and expected-gold checks → live claim-tamper demo (the C2 mechanism
  on demand) → bi-temporal probe pair (deterministic, no LLM). Read-only
  store access behind a lock; binds 127.0.0.1; remote access via SSH port
  forwarding only (no firewall exposure).
- **Consequence:** Demo surface added ahead of Phase 3; static trace viewer
  remains the archival artifact (the GUI links to it per ask).

## D-016 — 2026-07-10 — Apache-2.0 license; public-release preparation
- **Context:** PI decision to publish on GitHub and maintain a technical
  blog. Spec §8.6 requires license hygiene; the repo contained user-local
  Claude settings and deployment-specific hostnames.
- **Proposal:** Apache-2.0 (patent grant suits infrastructure adoption);
  untrack `.claude/` (user-local); parameterize deployment scripts via env
  vars (TGMS_REPO/VLLM_ENV/HF_HOME/TGMS_*); genericize internal host
  references in docs; add GitHub Actions CI (lint + §8.1 hygiene + fast
  property profile, <15 min per spec §7.5); README/CITATION/CHANGELOG/
  CONTRIBUTING/SECURITY; tag v0.1.0.
- **Consequence:** First-ever full ruff pass enforced (32 findings fixed);
  measured coverage gate: tgms/temporal at 96% (spec target ≥90%). History
  is kept intact — it documents the process and contains no secrets.

## D-017 — 2026-07-10 — Visibility campaign artifacts
- **Context:** Repo is public; PI requested arXiv preprint, blog, MCP
  registry listing, and a project page.
- **Proposal:** paper/ (LaTeX system-description preprint, compiles with
  tectonic; PI submits to arXiv cs.DB, cross-list cs.AI); GitHub Pages from
  docs/ (hand-rolled static site: project page + blog, no build step, no
  external assets); blog post #1 "Why your agent needs two kinds of time";
  server.json for the official MCP registry (publication gated on a PyPI
  release of the tgms package — queued).
- **Consequence:** docs/ now serves double duty (repo docs + website);
  .nojekyll disables Jekyll processing; public claims in site/blog restate
  only receipt-carrying numbers from the technical report.

## D-018 — 2026-07-11 — Test splits frozen for the v1 evaluation campaign
- **Context:** Spec §7.6/§8 require freezing the test split before any
  test-split measurement; dev-split iteration is complete (D-012 treatment)
  and the campaign configs are committed (configs/campaign/).
- **Proposal:** Freeze three suites, generated deterministically on the
  canonical stores (xzgpu, seed 0). Canonical copies are committed at
  benchmarks/frozen-v1/ (byte-identical to the generating machine's).
  - suite-collegemsg: n_dev=22 n_test=94,
    test_split_sha cbdc36a0774e78cb5301c091131750ef403f95379f8e4b7d8a07334354a0142f
  - suite-emaileu: n_dev=22 n_test=94,
    test_split_sha c8b2dfd660df31aa3578fdfae47dc30d8e7d9651d881915cab6210077677c093
  - suite-synth (planted rings/ping-pong/bursts, T2): n_dev=24 n_test=102,
    test_split_sha 696d8e8b8bf9af06b862e99639635af5be11e923ffac0d8b7fc80edda8dbcb09
- **Consequence:** From this entry on, test-split runs are limited to the
  pre-specified campaign configs; any change to task generation or gold
  computation invalidates the freeze and requires a new dated entry. Dev
  splits remain free for iteration (e.g. the guided-decoding A/B).

## D-019 — 2026-07-11 — Guided JSON decoding rejected at 14B (R3 escalation A/B)
- **Context:** Spec R3 lists constrained decoding as the escalation for plan
  syntax failures. A/B on the CollegeMsg dev split, Qwen2.5-14B-AWQ, ours
  only, seed 0: runs/dev-collegemsg-guided (vLLM guided_json over PLAN_SCHEMA
  and ANSWER_SCHEMA) vs runs/dev-collegemsg-oss-14b (unguided).
- **Observation:** Guided is worse on every axis — EM 0.409→0.227,
  first-emission validity 0.50→0.32, execution success 0.59→0.36, repair
  calls 1.6→2.3/task, wall time 140→231 s/task (one task took ~18 min).
  Syntactic validity is not the 14B bottleneck; constraining the decoder
  degrades plan semantics (grammar masks distort the model's natural JSON
  emission and leave no room for drafting) while adding latency.
- **Consequence:** llm_guided stays false for all v1 campaign configs. The
  knob remains available; re-testing at 7B (where E_SCHEMA malformation is a
  larger failure share) is an open question for the models phase.

## D-020 — 2026-07-18 — Generation cap for the baseline heal (main config)
- **Context:** 12 unhealed CollegeMsg-main rows, all baselines, are
  generations that run to the 4096-token cap: ~25–50 min each at this
  card's 3–4 tok/s, ~2.5 h/row with bounce collisions and retries.
- **Proposal:** llm_max_tokens: 1024 in test-collegemsg-main.yaml only
  (its ours/ours-noverify blocks are complete; pending rows are b1/b2/b5).
  A valid AnswerObject fits comfortably in 1024 tokens; generations that
  exceed it fail JSON parsing and score 0 with or without the cap.
- **Consequence:** Outcome-neutral by construction, latency-bounded heal.
  Dataset-phase configs keep 4096 (their pending rows include `ours`);
  revisit per-config only if the same pathology appears there.

## D-021 — 2026-07-20 — Frozen-test campaign complete
- **Context:** Healing campaign (heal8) reached HEAL8_ALL_DONE; all frozen
  suites clean except one deterministic row.
- **Results (14B, temp 0):** CollegeMsg 94x3 ours 0.408 (dev 0.409, replicated)
  vs b1 0.106 / b2 0.064 / b5 0.152, all paired-bootstrap deltas SIG
  (+0.26..+0.34, 95% CIs exclude 0); probes 0.897 vs 0.154/0/0. email-EU
  ours 0.309 vs 0.053/0.106, probes 0.846 vs 0/0. synth ours 0.314 (non-T2
  0.340) vs 0.029/0.157. C2 end-to-end: raw UCR 0.078 -> gated 0.000 at
  one EM point. 7B second config: ours 0.129.
- **Honest findings:** (1) T2 planted-pattern mining 0/8 for ours — planner
  cannot compose the multi-operator plan, exhausts repairs, emits empty
  answer (fails safe, UCR 0). (2) One 7B row fails by deterministic
  ContextWindowExceededError when repair-loop prompt growth overflows the
  16k window — a real small-window-serving limitation, not infra; counted
  as a scored 0, HEAL_INCOMPLETE is expected and correct.
- **Consequence:** Primary CIDR results table populated. GPU freed for the
  E1/E2 ablations, receipted extended-mutation rerun, and Phi-4-mini
  cross-family run.

## D-022 — 2026-07-21 — Token-cap confound in post-campaign experiments; corrected
- **Context:** The E1/E2 ablation and cross-family configs were templated
  off test-collegemsg-main.yaml *after* D-020 added `llm_max_tokens: 1024`,
  so they inherited the 1024 cap — while their comparison baselines
  (dev-collegemsg-oss-14b/v3 ours, and the Qwen-7B models config) used the
  default 4096. Verbose non-Qwen models truncate: Phi-4-mini's tokens_out
  was uniformly 4096 (= 4 repair calls each maxing the 1024 cap without
  emitting a parseable plan). This inflated the apparent cross-family gap
  (both Llama-3.1-8B and Phi-4-mini hit 0.043 — the truncation floor, not a
  capability floor).
- **Proposal:** Remove the cap from all five post-campaign experiment
  configs (abl-e1-7b, abl-e1-14b, abl-e2-14b, test-phi4mini, test-llama8b)
  and re-run at 4096 so every comparison is at equal generation budget.
- **Consequence:** The frozen headline is unaffected — the ours/14B rows
  predate D-020 (4096) and were never re-healed; D-020's 1024 cap only ever
  touched late baseline-answer rows, which fit well under 1024. Only the
  ablation and portability numbers are re-measured.

## D-023 — 2026-07-22 — Canonical store vaulted; ingest is not reproducible
- **Context:** Running the frozen suites on the iTiger cluster gave wrong
  gold matches (probes 0.000, deflated analytical EM). Root cause: a fresh
  `tgms ingest` assigns transaction times from the clock at write time, so a
  re-ingested store differs from the one the frozen gold was computed on —
  its regenerated test_split_sha was 117f951... not the D-018 cbdc36a...
  The suites alone are insufficient for reproducibility; the *store* is a
  required artifact.
- **Proposal:** Vault the canonical CollegeMsg store's event log
  (benchmarks/frozen-v1/collegemsg.eventlog.jsonl) and evolution memory
  (collegemsg.memory.sqlite). Add a `tgms replay` command that rebuilds a
  byte-identical store from a recorded event log (preserving tt, unlike
  ingest). Verified: replay -> regenerate suite reproduces the D-018 sha
  exactly. Reproduction procedure in benchmarks/frozen-v1/README.md.
- **Consequence:** Any machine reproduces the exact frozen store via
  `tgms replay`; re-ingestion is never used for the frozen splits. email-EU
  and synth stores should be vaulted the same way if their campaigns are
  rerun off-host.

## D-024 — 2026-07-24 — Post-campaign studies complete (scale, fair-baseline, portability)
- **Context:** iTiger cluster access (Slurm, RTX 5000/6000 Ada, H100)
  enabled experiments impossible on the 24GB Turing host; frozen splits and
  the canonical store (D-018/D-023) were reused unchanged.
- **Results:** model-scale study 7B/14B/32B fp16 + 72B AWQ — ours 0.138 /
  0.340 / 0.628 / 0.511 EM, probes 0.38 / 0.77 / 1.000 / 0.31, UCR 0.000
  everywhere; baselines flat (<=0.277). Fair baseline: b1 at k=20 breadth
  in a native 32k window scores 0.021 vs ours 0.362 same-run (32.5k vs
  6.5k tokens/task). E1/E2 replicated exactly at equal budget (D-022).
  Cross-family (Llama-3.1-8B 0.043, Phi-4-mini 0.015): competence needs a
  capability threshold; safety does not.
- **Consequence:** Headline claims for arXiv v3 and CIDR: the advantage
  grows with model capability; quantization (not scale) degrades planning;
  un-hobbling retrieval baselines does not rescue them; verification value
  persists at every scale. Full section: TECHNICAL_REPORT 8.2c. Serving
  and storage robustness fixes from the campaign are in vllm watchdog /
  kuzu pool bound / subprocess-bounded b5 / itiger job script.

## D-025 — 2026-07-25 — CIDR revision experiments pre-registered (b6, Bitcoin-OTC)
- **Context:** CIDR 2027 strategy review (2026-07-24) requires (1) a
  same-information baseline isolating the operator interface from
  history preservation, and (2) evidence beyond communication-network
  domains. Both run on the frozen-campaign model (Qwen2.5-14B-AWQ).
- **b6 (bi-temporal text-to-SQL):** the model writes DuckDB SQL against
  the identical version store TGMS executes on (schema + vt/tt semantics
  documented in the prompt; b5 repair budget; read-only subprocess
  execution; shared answer contract). Frozen splits: CollegeMsg 94x3,
  email-EU 94, synth 102 (existing D-018 SHAs).
- **Bitcoin-OTC:** new financial-trust domain (SNAP soc-sign-bitcoinotc,
  5,881 nodes / 35,592 signed ratings; rating as an edge property).
  Canonical store built once on xzgpu (D-023 discipline; corrections
  injected at suite generation, before any gold). Frozen test split:
  94 tasks, seed 0, test_split_sha
  4e69bfbac23a053fadd1139d7e2513838a214b400ce64e77e828b34c7b8ff3be.
  Systems pre-registered: ours, b2, b5, b6; one seed.
- **Consequence:** queue on xzgpu is phi-heal -> b6 (3 corpora) ->
  bitcoinotc; no test-split reruns without a logged TGMS_FORCE reason.
  CIDR tables consume these runs via scripts/cidr_metrics.py.

## D-026 — 2026-07-26 — Independent-question study pre-registered
- **Context:** four students wrote 110 questions over CollegeMsg and
  Bitcoin-OTC seeing only a natural-language data description and the
  two-clock property (handout: contamination controls; examples from a
  held-out library-log domain). Raw questions + hand-audited
  classification: benchmarks/independent-v1/.
- **Coverage result (no GPU needed):** 10/110 expressible by operator
  composition; 98 require unimplemented capability — grouped/distinct
  aggregation 76, arithmetic beyond count/sum/min/max 27,
  property-filtered patterns 20, calendar semantics 18, global
  scan-select 10, set ops 7, absence conditions 3 (multi-tagged);
  1 clock conflation; 1 false presupposition. Behavioral findings:
  writers misjudge data extent; writers anchor transaction time to
  event years.
- **Accuracy slice:** 5 runnable questions (3 CollegeMsg, 2 Bitcoin-OTC),
  verbatim, double-keyed manual gold (SQL + independent Python, asserted
  equal), 3 seeds, systems ours/b2/b5/b6. Frozen suite SHAs:
  suite-indep-collegemsg 31f045b969e8f2f4..., suite-indep-bitcoinotc
  b8ad8354bf8dba10.... Queued as campaign phase `independent` behind
  the bitcoinotc phase.
- **Consequence:** the CIDR draft reports coverage (RQ5) separately from
  accuracy; the 76/27/20 gap counts become the priced agenda item
  ("coverage-driven algebra growth").

## D-027 — 2026-07-25 — Runtime repair extended after Bitcoin-OTC dev diagnosis
- **Context:** frozen Bitcoin-OTC test (D-025) measured ours 0.309 vs b6
  0.340 (paired bootstrap CI [-0.117,+0.053], indistinguishable) with 43/94
  ours runs failing at execution. Diagnosis on the untouched dev split
  (22 tasks; runs dev-bitcoinotc{,2,3,4} on iTiger).
- **Findings:** (1) only E_COST/E_NOT_FOUND runtime errors re-entered the
  repair loop; E_LIMIT / E_INVALID_ARG / E_SCHEMA died with no retry
  (14/15 dev failures carried actionable payloads). (2) Payload precision
  is measurable: told "increase stride", the 14B undershot the 2000-bucket
  cap 3/3 (2026-2344 after seeing ~30k); told "use stride >= X" (computed
  minimum), it repaired 3/3.
- **Fixes (general, committed):** e0b529a extends REPAIRABLE to
  {E_COST, E_NOT_FOUND, E_LIMIT, E_INVALID_ARG, E_SCHEMA};
  ea04746 makes bucket-cap payloads name the minimum admissible stride and
  empty-$ref payloads direct re-planning of the producing step.
- **Dev effect:** exec success 7/22 -> 12/22 (E_LIMIT class eliminated);
  EM unchanged 3/22 — recovered executions produce wrong plans, so the
  residual is planning quality, outside the contracts by design.
- **Consequence:** frozen test numbers are NOT rerun; the pre-registered
  0.309 stands in the CIDR draft with the fixes disclosed as post-diagnosis
  and dev-validated. The payload-precision observation feeds the paper's
  repair-payload design principle. Post-campaign heals (Phi tail on xzgpu)
  run pre-fix code via the xzgpu bare remote, keeping their campaign
  internally consistent.

## D-028 — 2026-07-28 — Phase-3 native storage engine: architecture
- **Context:** Kùzu is no longer available (acquired by Apple) and DuckDB is
  the only remaining third-party engine in the runtime path
  (`storage/duckdb_adapter.py` as the primary store; `temporal/ops_motifs.py`
  for motif joins). PI direction (2026-07-26): build the lower-level storage
  system ourselves and keep it minimal but extremely fast on the closed
  surface TGMS actually uses. Design went through three drafts and two
  external review rounds; artifacts: `ENGINE_BLUEPRINT.md` (v3),
  `TGMS_NATIVE_ENGINE_REVIEW.md`, `TGMS_NATIVE_ENGINE_V2_REVIEW.md`,
  `ENGINE_IMPLEMENTATION_SPEC.md` (binding implementation contract).
- **Proposal — 17 locked decisions** (blueprint §8, recorded here verbatim):
  1. All logical state is manifest-generation scoped; readers hold snapshot
     handles; close patches are immutable commit artifacts.
  2. Identity is derived, not stored, under the derivability invariant
     (`disc` always recoverable); 64-bit prefixes are accelerators; ties
     compare full derived ids. Stored 96-bit ids are the recorded fallback.
  3. One canonical derivation implementation in the engine core, with an
     ingest-time parity assertion against the Python semantics layer.
  4. Composite-key `(vt_s, vid64)` tie groups never split across segment
     boundaries; manifests record full 96-bit boundary keys.
  5. Logical partitions ≠ physical segments; segments are byte-targeted;
     blocks are the codec/pruning unit.
  6. Lane membership = interval intersects ≤ K(=2) adjacent partitions;
     lane assignment is physical, not logical identity.
  7. Visibility v0 = `all_current` flag + sparse sidecars; the dense-`tt_e`
     sidecar tag is reserved in the format but unimplemented.
  8. Compaction preserves closed rows; equivalence test = store digest +
     historical-query sample byte-identical.
  9. No physical GC in the first version; later GC is explicit
     (`tgms store gc`) and reader-marker guarded.
  10. Group-commit durability, one mode; the manifest event-log offset lands
      on a record boundary; the log prefix hash is chained/checkpointed.
  11. u32 entity ids and row ids with capacity checks; column widths declared
      in the format header, fixed (not adaptive) in v0.
  12. The internal API is a chunked scan cursor; `StorageAdapter` is a
      compatibility boundary that materializes; the PyO3 boundary is coarse
      (one crossing per batch, GIL released).
  13. **Rust-first persistent core** (`crates/tgms-engine-core`, no Python
      dependency) with a thin PyO3/maturin layer; Python keeps semantics,
      CLI, adapters, and tests; NumPy prototypes are never authoritative.
  14. Version-specific wheels first; no `abi3` until the extension API
      stabilizes.
  15. The node store is identity-clustered; name lookup is current-canonical
      only.
  16. Format v0 carries checksums, codec ids, declared widths, versions, and
      segment complete-markers.
  17. Gates: E3/WP-N3 = semantic completeness (linear-scan fallbacks legal);
      E4/WP-N4 = indexed performance; scan and join gates are
      output-sensitive and name their materialization level.
- **Consequence:** implementation proceeds as WP-N0..WP-N5
  (`ENGINE_IMPLEMENTATION_SPEC.md` §7). DuckDB stays the default backend and
  the A/B reference until WP-N5, then becomes an optional extra; TGMS must
  function fully without it. Nothing above `storage/base.py` changes — the
  500-case oracle, replay/digest, and metamorphic suites are the arbiters of
  correctness and stay human-owned (§8.1). Deferred by design to E6+:
  compression, dense visibility sidecars, kernel parallelism, trigram name
  index, window-CSR caching, GC reclamation.

## D-029 — 2026-07-28 — Native-engine dependencies, licenses, and build backend
- **Context:** D-028 introduces a compiled Rust extension into a previously
  pure-Python package. Spec §8.6 requires a dated entry with licenses for new
  dependencies.
- **Rust (runtime):** `pyo3` (Apache-2.0/MIT), `numpy` rust crate
  (BSD-2-Clause), `serde` + `serde_json` (MIT/Apache-2.0), `sha2`
  (MIT/Apache-2.0), `crc32c` (Apache-2.0/MIT), `memmap2` (MIT/Apache-2.0),
  `thiserror` (MIT/Apache-2.0). **Build:** `maturin` (MIT/Apache-2.0),
  `cibuildwheel` (BSD-2-Clause, CI only). All permissive and compatible with
  the project's Apache-2.0 license. Deferred to E6+ under their own entry:
  `rayon`, integer/compression codecs.
- **Build backend:** `hatchling` → `maturin` (mixed Rust/Python project;
  the compiled module is `tgms._engine`, a private submodule of the existing
  `tgms` package, so the public import surface is unchanged).
- **Consequence (operational, deliberate):** binary wheels cover the
  supported matrix (CPython 3.11–3.13 × manylinux-x86_64 + macOS-arm64), so
  `pip install tgms` remains a single command with no toolchain — this is
  gated by the WP-N0 packaging probe. **Installing from source (including
  `uv sync` in a git checkout) now requires a Rust toolchain**, which affects
  the xzgpu and iTiger checkouts; both need `rustup`/`brew install rust`
  (no root required) before their next `uv sync`. The frozen campaigns are
  complete, so no experiment depends on this transition.

## D-030 — 2026-07-29 — PostgreSQL baseline: dependency and fairness policy
- **Context:** the evaluation plan (§4.1) requires a relational baseline with
  genuinely equivalent bi-temporal semantics. PostgreSQL is the cheapest such
  system to stand up, and the first external system in the matrix.
- **Dependency:** `psycopg[binary] >= 3.2` (LGPL-3.0-or-later for psycopg3;
  the `binary` wheel bundles libpq, PostgreSQL licence). Added as the
  **`eval` optional extra**, not a runtime dependency — nothing in TGMS
  itself talks to PostgreSQL, and a user installing the package must not
  acquire a database driver. Server: PostgreSQL 16 as a local service, not a
  container, so the same procedure applies on xzgpu.
- **Fairness policy (evaluation plan §11.4 permits system-native tuning):**
  the baseline is tuned as a competent operator would tune it — covering
  indexes on the version tables, `work_mem` and `shared_buffers` raised from
  defaults, and `EXPLAIN` checked on every registry query to confirm the
  planner is not doing something the schema could fix. **Every setting and
  index is recorded in the run manifest**, because tuning is part of what is
  being measured, and an untuned baseline would flatter TGMS for a reason
  that has nothing to do with storage design.
- **Consequence:** a query PostgreSQL cannot express equivalently is marked
  `unsupported` in `docs/eval_semantics.md` rather than being weakened until
  it runs — a faster number for an easier question is worse than no number.

## D-031 — resolve_entities: one canonical-version rule, string-only name matching

- **Date:** 2026-07-30
- **Context:** porting `resolve_entities` to SQL for the PostgreSQL baseline
  exposed that the Rust kernel and the reference oracle disagreed, and the
  suite could not tell: the kernel took an entity's canonical `label`/`name`
  from the latest **matching** believed version, the oracle from the latest
  believed version **overall**; the two broke `vt_s` ties in opposite
  scan-order-dependent directions; and the oracle's `str()` coercion let
  non-string names match text the store never contained ("None" for a JSON
  null, "42" for a number) while the kernel's promoted name column indexes
  strings only.
- **Decision:**
  1. Canonical state comes from the latest believed version by
     `(vt_s, vid)`, whether or not that version matched — an entity found
     by a superseded name resolves to what it is now. (Oracle behavior;
     kernel fixed, and it now also reads staged rows so a batch sees its
     own writes.)
  2. Name matching participates only when `name` is a non-empty JSON
     **string**. (Kernel/typed-column behavior; oracle, portable fallback,
     and the PostgreSQL SQL narrowed. This is the one place the reference
     implementation changed: its coercion was an accident, not a
     semantics.)
  3. The `vid` tiebreak is unreachable for believed versions of one uid
     (disjoint valid intervals) but is stated so every implementation is
     order-independent by construction.
- **Consequence:** `tests/test_resolve_semantics.py` pins all three
  behaviors on both backends; the output `name` field keeps its raw JSON
  type either way. `docs/eval_semantics.md` §6's "current-canonical only"
  gains "and string-matched".

## D-032 — column compression: one codec, chosen by trial per column

- **Date:** 2026-07-30
- **Context:** C4 was deferred until the uncompressed baseline existed; the
  measured breakdown at 1M rows showed integer columns at 71% of segment
  bytes, dominated by values in narrow per-segment ranges (sorted vt_s/vt_e,
  constant or sequential string refs, dictionary-bounded ids) — and 12 B/row
  of sha256-derived vid that cannot compress.
- **Decision:** per-block frame-of-reference bit-packing (codec 1), applied
  by measurement: the writer trial-encodes every column and keeps the
  smaller representation, so incompressible columns stay raw without a
  special case. Codec ids were reserved from format v0, so old stores read
  unchanged and old builds fail cleanly on new stores. Compressed columns
  decode once at open behind a new store-level Arc segment cache — sound
  because segment files are immutable — and the hot path keeps serving
  plain slices.
- **Measured (xzgpu, 1M rows):** segments 65.3 → **31.4 B/row** (0.169× the
  DuckDB baseline's 186.3). vt_s+vt_e 16 → 2.95; four ref columns ~16 → 1.5;
  src/dst 8 → 3.46; vid stays 12 and is now 38% of segment bytes — the
  standing price of derived identity. Query latency *improved* across the
  board (e.g. co_active 37.3 → 23.5 ms median), but that gain is
  attributable to the segment cache and decode-once pair, not compression
  alone — no ablation was run separating them. All 12 registry queries
  still hash identically across the three systems.
- **Open:** the string heap (11.2 B/row, now 36% of segments) is untouched —
  compressing it needs a general codec and a dependency decision (§8.6).
  And the manifest directory (24 MB at 1M after ~250 commits, versus 31 MB
  of segments) now dominates store overhead: every commit writes a full
  manifest and nothing collects old generations. That is a retention
  problem, not a codec problem, and is filed separately.

## D-033 — string heap packing: FOR offsets + DEFLATE payload

- **Date:** 2026-07-30
- **Context:** after D-032 the heap was the second-largest block at 11.2
  B/row (36% of segments), in two structurally different halves: monotone
  offsets and text payload.
- **Decision:** pack them separately — offsets through the existing FOR
  codec (3.98 → 0.40 B/row, no new dependency), payload through DEFLATE
  (6.40 → 2.14, measured with zlib on a real heap *before* choosing the
  codec). The split beats one DEFLATE stream over the whole heap by 1.4×.
  Dependency: `miniz_oxide` — pure Rust, zero transitive deps, MIT OR Zlib
  OR Apache-2.0 (§8.6); inflate uses the `with_limit` form so corrupt input
  cannot balloon. Same trial rule as columns (pack only when smaller), same
  decode-once-at-open. A packed heap leads with `u32::MAX` where a raw heap
  keeps its count — a value the count can never take — so old builds fail
  bounds checks rather than misreading text.
- **Measured (xzgpu, 1M rows):** heap 11.17 → **4.35 B/row**; segments
  31.4 → **24.6 B/row, 0.132×** DuckDB's 186.3 (65.3 before C4 — 2.65×
  total). Query medians statistically unchanged from the D-032 run; all 12
  registry queries hash identically across the three systems. vid is now
  48.8% of segment bytes: the compression story ends at the identity
  design, and the remaining store overhead is the manifest retention
  problem, not the data.

## D-034 — generation collection: retention window plus reader pins

- **Date:** 2026-07-30
- **Context:** every commit publishes `manifests/<G>.json` naming every
  live segment, and nothing collected superseded generations — at 1M rows
  the 283 commits held 23.6 MB of manifests against 24.8 MB of segments
  (the "retention problem" D-032/D-033 filed separately). Compaction has
  the same question from the other side: superseded segments stay on disk,
  so its peak is 2× the store. The engine is single-writer with in-process
  readers; there is no cross-process reader registry.
- **Decision:** one gc pass, one rule — a file is removed exactly when no
  retained generation names it. Retained = the generation `CURRENT` names,
  the last K generations (`keep_last`, default 2), and any generation
  pinned by a live in-process reader: every open `NativeStore` registers
  its generation in a process-global pin table keyed by canonical store
  root, commits re-pin, drop unregisters. Cross-process readers are
  deliberately out of scope — they hold their manifest in memory and
  already-opened segments via mmap, so the one exposure is a lazy first
  open of a collected segment, which fails with a *detected* IO error
  naming the file, never silent wrong data. Crash order: superseded
  manifests are deleted first, the directory fsynced, and segment/close
  eligibility is then recomputed from the manifests actually on disk, so
  an interrupted pass can only under-collect; `CURRENT` and its files are
  categorically untouched. Exposed as `NativeStore::gc(keep_last)`,
  `NativeAdapter.gc`, and `tgms store gc --keep K` (the blueprint's
  deferred command; `store compact` gained a CLI alongside). The same pass
  collects segments a crashed batch orphaned.
- **Measured (xzgpu, 1M rows):** whole store 48.2 → **25.1 B/row** after
  gc alone — manifests 23.41 → 0.33 B/row (283 files → 2) — and **24.6**
  after compaction merges 283 segments into 3 and gc collects the
  superseded files plus all 262 folded close runs, reclaiming the 2×
  compaction transient (50.0 MB peak → 24.8 MB). A retained manifest still
  costs O(segments): 165 KB naming 283 segments, 1 KB naming 3.
- **Consequence:** gc is explicit, like compaction — no background
  schedule. The fault matrix gains four cases (CURRENT untouchable,
  interrupted pass, reader pins, orphan collection); `docs/eval_phase0.md`
  "Storage" now reports whole-store B/row with retention closed.

## D-035 — ClickHouse baseline (Phase 2, second system)

- **Date:** 2026-07-31
- **Context:** the plan's Phase 2 pairs PostgreSQL with a columnar
  aggregation engine. ClickHouse 26.8 runs as a user-space static binary on
  the measurement host (no root there, like the PostgreSQL source build),
  localhost-only, data on /mnt/project. Client: clickhouse-connect. Both
  Apache-2.0 (§8.6).
- **Decision:** same fairness contract as D-030 — a baseline, never a
  backend; canonical rows loaded so tt and derived ids arrive byte-for-byte
  (D-023); registry SQL added slice by slice, each verified against the
  canonical hash before it is ever timed; unwritten queries report "no SQL
  written yet", which is not a verdict. Schema: MergeTree with
  edge_versions ordered by (vt_s, vid) — the scan contract's total order —
  and node_versions by (uid, vt_s, vid). Bytewise String comparison is the
  operators' code-point order, so PostgreSQL's COLLATE pinning has no
  ClickHouse counterpart.
- **First slice (xzgpu, 200k, §16 protocol):** hist.single 9.2 ms /
  hist.asof 10.0 (HTTP roundtrip floor ~1 ms; not its shape),
  **series.count 14.2 and burst.zscore 16.6 — fastest of all four systems**,
  native at 27.6/30.0. All four hashes identical across native, DuckDB,
  PostgreSQL, and ClickHouse. The aggregation win is exactly what this
  baseline exists to measure; whether it survives the remaining eight
  queries and 10M scale is the open question, not a foregone one.

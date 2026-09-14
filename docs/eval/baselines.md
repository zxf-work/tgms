# WP2.6 baseline arms

`tgms/eval/baselines.py` implements the non-`ours` arms of the matrix
runner (`tgms/eval/harness.py::run_matrix`). Every arm shares one answer
contract (`ANSWER_CONTRACT` / `ANSWER_SCHEMA`) and one repair-budget/model/
seed surface, set by the harness config, not by the arm classes themselves.

There is no `--arm` CLI flag: the harness is config-driven. A run's config
YAML names the arms to compare in its `systems` list, using these codes
(`tgms/eval/harness.py::build_systems` is the registry):

| code | class | what it does |
|---|---|---|
| `b1` | `VectorRAG` | chunked event sentences, embedded, top-k retrieval |
| `b2` | `StaticGraphRAG` | latest-snapshot 2-hop edge list, no history |
| `b5` | `TextToCypher` | model-written Cypher over vanilla Kùzu (no bi-temporal layer) |
| `b6` | `BiTemporalSQL` | model-written SQL over the *same* bi-temporal DuckDB store `ours` executes on |
| `b6e` | `BiTemporalSQLEvidence` | `b6` + the generic evidence verifier (ECQR witness gating) |
| `llm_direct` | `LLMDirect` | raw serialized event text, budget-truncated — no store, index, or query language at all |

## `llm_direct` — the LLM-direct arm

The "just stuff the events in the prompt" control. Construction takes the
same `(store, llm_fn, model, seed)` surface as `VectorRAG`/`StaticGraphRAG`,
plus `context_budget_tokens` (default 8,000) and an injectable `tokenizer`
callable (default: a whitespace-count approximation, since no repo-wide
token-counting convention exists — every field it feeds is named
`..._approx` accordingly).

Event selection is deterministic given `(seed, task entities, event set)`:
filtered to the question's named entities (`input_uids`) when the task
carries them, else the full corpus; ordered most-recent-first with a stable
tie-break on `(src, dst, rel_type)`. Events are serialized `[eN] src REL dst
at <iso> (<epoch_us>)` in that order and greedily included until the token
budget is exhausted. Every call records `events_offered`, `events_included`,
`truncated`, and `prompt_tokens_approx` in `meta`.

Claims carry the same AnswerObject contract as every other arm. Because
this arm has no operator trace, index, or store handle to recompute a claim
against (that absence is the ablation), `verify_llm_direct_claims` grounds
only `entity` claims against the cited `[eN]` lines' `src`/`dst`; every
other claim type is `unverifiable` by construction, and a claim citing an
`[eN]` tag that was never offered (or was truncated away) is `unverifiable`
rather than crashing verification. The resulting report uses the exact
verdict vocabulary (`supported | weakly_supported | unsupported |
unverifiable`) `tgms.agent.verifier.ClaimVerifier.verify` produces, so it
passes through `tgms.eval.plan_faults.GATED_VERDICTS` — the same drop set
D-160 put behind the `ours` production gate
(`docs/design/TRUST_BOUNDARY_FAULT_MATRIX_DESIGN_2026-09-13.md`, Addendum 2)
— before the answer is delivered. `LLMDirect.answer()` applies that gate
itself; `meta["report"]` carries the pre-gate verdicts and
`meta["pre_gate_answer"]` the ungated object, for the same
before/after contrast `b6e`'s `pre_gate_answer` gives.

## Dry-run recipe: four-arm CollegeMsg comparison on iTiger

Not run from this lane (no ssh, no experiments — see the campaign's
launch-hygiene rules). This is the command a later lane runs once a vLLM
server is up on an iTiger allocation, following the same pattern as
`scripts/run_oss_matrix.sh` / `scripts/vllm_watchdog.sh` (which target the
xzgpu soak box) and `configs/matrix-dev-oss.yaml`.

```bash
# 1. On the iTiger GPU node/allocation: serve the model (OpenAI-compatible
#    endpoint, zero API cost — commercial frontier models deferred, D-013).
#    MODEL is a parameter; pick a build known to fit the allocated GPU's
#    memory the way scripts/run_oss_matrix.sh already sizes 7B vs 14B-AWQ.
MODEL="Qwen/Qwen2.5-7B-Instruct"   # <-- parameter; swap in the campaign's model
"$VLLM_ENV/bin/vllm" serve "$MODEL" --dtype half --max-model-len 16384 \
    --gpu-memory-utilization 0.92 --port 8000 &
# optional: scripts/vllm_watchdog.sh to bounce the server periodically if
# the allocation runs long enough to hit the sm_75-style throughput decay

# 2. Four-arm config (new file, e.g. configs/matrix-dev-collegemsg-4arm.yaml):
#      suite_path: stores/suite-collegemsg/suite.json
#      store_path: stores/collegemsg
#      out_dir: runs/dev-collegemsg-4arm
#      split: dev
#      systems: [ours, b5, b6e, llm_direct]
#      models: ["openai/$MODEL"]
#      seeds: [0]
#      max_repairs: 3
#      llm_direct_budget_tokens: 8000   # fairness rule WP2.6a: >= ours's
#                                       # measured per-task context use on dev
#      memory_db: stores/collegemsg/memory.sqlite
#      llm_api_base: "http://localhost:8000/v1"
#      llm_api_key: "EMPTY"

# 3. Run:
uv run tgms eval run --config configs/matrix-dev-collegemsg-4arm.yaml
```

`b5` is `TextToCypher` and `b6e` is `BiTemporalSQLEvidence` — the codes the
harness registry actually dispatches on (see the table above); there is no
separate `text_to_cypher` / `bitemporal_sql` alias in the `systems` list.
`llm_direct_budget_tokens` is `LLMDirect`'s only extra config key, read by
`build_systems` (`tgms/eval/harness.py`) the same way `b1_k` / `b2_max_edges`
already are.

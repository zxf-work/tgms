# d160-collegemsg-v1 — CollegeMsg agent-loop re-measurement under D-160

This directory holds the campaign `docs/STABILITY.md` section 9
("What is explicitly deferred, and why") names as the one that
re-measures the CollegeMsg agent-loop numbers under D-160's production
claim gate (`tgms.eval.plan_faults.GATED_VERDICTS ==
("unsupported", "unverifiable")` — the code's default since 2026-09-15,
**unchanged by this campaign**; this run only re-measures under a fresh
model/seed/arm matrix, it does not touch the gate).

**This directory never overwrites `docs/site_facts.json`'s numbers** (the
old-gate `ucr_gated` 0/199, coverage 0.706, conditional accuracy 0.548
facts stay exactly as published) and no paper file is touched. The two
records below stand beside those facts, not in place of them.

## Files

| file | what it is |
|---|---|
| `manifest-2026-09-14.json` | the 4-arm campaign's result manifest, conforms to `benchmarks/schema/result_manifest.schema.json` (`check_result_manifest.py` passes) |
| `rows-2026-09-14.json` | the 1,128 raw per-task-run rows the manifest's `record` field points to (canonical JSON, sha256 = the manifest's `result_digest`) |
| `manifest-llm-direct-fix-2026-09-14.json` | the one-arm `llm_direct` follow-up's manifest (same task set/model/seeds, `llm_direct` only, under the real-tokenizer budget fix; job 212231) |
| `rows-llm-direct-fix-2026-09-14.json` | that follow-up's 282 raw rows (canonical JSON, sha256 = the manifest's `result_digest`) |

## Task set, systems, model

- Task set: the frozen CollegeMsg **test** split (`benchmarks/frozen-v1/suite-collegemsg.json`,
  94 tasks) — the same split the old-gate numbers were measured on. `docs/site_facts.json`'s
  own "199 emitted, of 282 task runs" language is 94 tasks × 3 seeds pooled = 282 task-runs;
  199 of those were gated-emitted (claim-carrying) under the pre-D-160 gate.
- Systems: `ours`, `b5` (TextToCypher, no claim gate — an interface ablation, not a
  trust-boundary arm), `b6e` (BiTemporalSQLEvidence, its own ECQR-based gate), `llm_direct`
  (raw event dump, no store/index/query layer, gated by the same `GATED_VERDICTS`
  as `ours`).
- Model: Qwen2.5-14B-Instruct-AWQ, served locally by vLLM on iTiger (RTX 5000 Ada,
  `itiger07`), 3 seeds (0, 1, 2), `max_repairs: 3`.

## Metric definitions used below

- **coverage** (a.k.a. "claim-carrying rate"): fraction of task-runs whose *delivered*
  (already-gated, where a gate applies) answer carries at least one claim. This is the
  reading `docs/site_facts.json`'s own "199 emitted, of 282 task runs" and
  `scripts/m8_tables.py`'s "carrying" already use — **not** the per-row text-uncovered-
  assertions fraction `tgms.agent.verifier.ClaimVerifier.verify` also computes under the
  same field name (`row["coverage"]`); that finer per-row diagnostic is reported
  separately below as "verifier text-coverage" to avoid conflating the two.
- **conditional accuracy**: mean `em` (exact-match, per `tgms.eval.metrics.score_answer`)
  among claim-carrying rows only — `scripts/m8_tables.py`'s `em_given_claims`.
- **ucr** / **ucr_pre_gate**: `tgms.agent.verifier`'s unsupported-claim rate, post- and
  pre-gate respectively (only "unsupported" claims, never "unverifiable" — `ucr_pre_gate`'s
  meaning is unchanged by D-160 per `docs/STABILITY.md` section 9).

## Results — D-160 gate, per arm × seed

| system | seed | n | coverage | conditional accuracy | ucr (post-gate) | ucr_pre_gate |
|---|---:|---:|---:|---:|---:|---:|
| ours | 0 | 94 | 0.3936 | 0.5135 | 0.0000 | 0.2176 |
| ours | 1 | 94 | 0.4043 | 0.5000 | 0.0000 | 0.2011 |
| ours | 2 | 94 | 0.3936 | 0.5135 | 0.0000 | 0.2176 |
| **ours** | **pooled** | **282** | **0.3972** | **0.5089** | **0.0000** | **0.2121** |
| b6e | 0 | 94 | 0.8298 | 0.3333 | — | ucr_pre_gate_e 0.1385 |
| b6e | 1 | 94 | 0.8298 | 0.3333 | — | ucr_pre_gate_e 0.1385 |
| b6e | 2 | 94 | 0.8298 | 0.3333 | — | ucr_pre_gate_e 0.1385 |
| **b6e** | **pooled** | **282** | **0.8298** | **0.3333** | — | **0.1385** |
| llm_direct | 0 | 94 | 0.0000 | n/a (0 carrying) | — | — |
| llm_direct | 1 | 94 | 0.0000 | n/a (0 carrying) | — | — |
| llm_direct | 2 | 94 | 0.0000 | n/a (0 carrying) | — | — |
| **llm_direct** | **pooled** | **282** | **0.0000** | **n/a** | — | — |
| b5 (no gate) | 0/1/2 identical | 94 each | n/a (ungated) | em 0.1809 (raw) | — | — |

`b6e` uses its own ECQR-based drop set (`UNSUPPORTED_NO_WITNESS` /
`UNSUPPORTED_VALUE_MISMATCH` / `UNSUPPORTED_BASIS_MISMATCH`), not
`GATED_VERDICTS`, so its "ucr (post-gate)" column is not directly comparable to
`ours`'s; `ucr_pre_gate_e` (its own pre-gate unsupported-analogue rate) is
reported instead. `b5` carries no claim gate at all (the interface ablation is
the operator algebra itself, not verification) — its raw `em` (0.1809, identical
across seeds — temperature 0, deterministic) is reported without a coverage/UCR
reading, since neither concept applies to an ungated arm.

**Additional `ours` diagnostics** (not part of the coverage/accuracy table
above): mean per-row verifier text-coverage (`row["coverage"]`, the
uncovered-assertions fraction, distinct from claim-carrying coverage above)
was 0.2733 / 0.2957 / 0.2850 for seeds 0/1/2 (pooled 0.2847) — this is the
metric `docs/STABILITY.md` section 9 describes falling under D-160 (a
dropped `unverifiable` claim's covered number/uid becomes uncovered text).

## `llm_direct`: pre-gate vs post-gate, and a known limitation of this record

`llm_direct`'s claim-carrying rate is **0.0000** pooled — every claim this arm
proposed that survived the LLM's own emission was still dropped by the gate
(mean 0.223 claims proposed pre-gate per row, 0.0 per row post-gate, all 3
seeds identical). Its raw, ungated (pre-gate) accuracy — computed from
`meta["pre_gate_answer"]`, the answer before `GATED_VERDICTS` dropped
anything — was **em 0.1818** (n=66 non-error rows across all 3 seeds,
identical per seed).

**Known limitation, disclosed rather than silently fixed:** 216 of 282
`llm_direct` rows (72/94 per seed) in this record are `task_error` rows —
`litellm.ContextWindowExceededError`, not a scored answer at all. Diagnosis:
`llm_direct_budget_tokens: 8000` was enforced by a whitespace-count
approximation that undercounts this corpus's real (BPE) token count by
3-7× (sampled overflow rows: 24,910-54,320 real input tokens against the
served model's 28,672-token window; non-overflowing rows' approximate
counts ranged 7-3,199, no overlap with the overflow set — these are tasks
with no `input_uids`, which fall back to the full unfiltered event corpus).
**`prompt_tokens_approx` in this record's `llm_direct` rows is therefore not
a reliable token count for the 216 overflow rows** — it is the input the
harness's own greedy-inclusion loop saw, not what the model's real
tokenizer would have counted. **This record's `llm_direct` result stands as
first shipped, context-overflow rate and all** — it is not overwritten;
see the next section for the fix and its own, separate result.

### `llm_direct` re-measured under the token-budget fix (job 212231)

`tgms/eval/baselines.py`'s `LLMDirect` now prefers a real HF tokenizer
(`_try_load_hf_tokenizer`) and caps the effective budget at
`max_model_len - system_prompt_cost - answer_reserve - wrapper_reserve`
(commits `dab5c2a`/`8de040f`). Re-run under
`configs/matrix-d160-collegemsg-llm-direct.yaml` (same task set, model,
seeds; `llm_direct_max_model_len: 28672` matching the served
`--max-model-len`): **282/282 rows, 0 task_error** (vs 216/282 before).
Every row's `meta.tokenizer_kind == "hf_real"` and
`meta.budget_effective_tokens == 8000` — confirming the fix measured the
same nominal 8,000-token budget in real tokens rather than
whitespace-approximated ones.

| | pre-fix (this record) | post-fix (job 212231) |
|---|---:|---:|
| task_error rate | 216/282 (76.6%) | **0/282 (0%)** |
| coverage (claim-carrying rate) | 0.0000 (of 66 scored) | **0.0000 (of 282 scored)** |
| raw pre-gate em | 0.1818 (n=66, easy tasks only) | **0.0638 (n=282, all tasks)** |
| mean claims proposed pre-gate | 0.223 | 0.660 |
| mean events included | 62.7 (unbounded/error mix) | 124.5 |
| truncated rate | 0.0% (of the 66 that didn't error) | 79.8% |

The fix eliminates the crash but does **not** change the headline finding:
`llm_direct`'s claim-carrying rate is **still 0.0000** even with 0 errors and
full coverage of all 282 tasks — every claim this arm proposes (now more
of them, 0.660/row vs 0.223/row, since more tasks get a real attempt) is
still dropped by the gate. Raw pre-gate em fell from 0.1818 to 0.0638
because the pre-fix number was computed over only the 66 "easy" (small,
filtered-context) tasks that happened not to overflow; the post-fix number
is the honest, complete measurement over all 282 tasks, including the
previously-unanswerable large-context ones, which score far worse once
they are actually attempted (now truncated to fit rather than crashing).

## Old gate vs D-160 gate

| | old gate (`docs/site_facts.json`, `docs/STABILITY.md` §9) | D-160 gate (this record, `ours`, pooled) |
|---|---:|---:|
| coverage | 0.706 (199/282) | **0.397** |
| conditional accuracy | 0.548 | **0.509** |
| ucr_gated | 0/199 → 0.000 | **0.000** |

## Pre-registered predictions vs measured (no verdicts)

| prediction | measured |
|---|---|
| coverage of `ours` falls below 0.706 | 0.3972 pooled (0.3936 / 0.4043 / 0.3936 per seed) |
| conditional accuracy of `ours` ≥ 0.548 | 0.5089 pooled (0.5135 / 0.5000 / 0.5135 per seed) |
| ucr_gated of `ours` stays 0 on all seeds | 0.0000 on all 3 seeds |
| `llm_direct` emits fewer gated claims than `ours`, raw pre-gate accuracy reported beside the gated one | `llm_direct` coverage 0.0000 vs `ours` 0.3972 (pre-fix, n=66 scored, or post-fix, n=282 scored — coverage is 0.0000 either way); raw pre-gate em 0.1818 pre-fix (n=66, partial) / **0.0638 post-fix (n=282, complete)** |

## Provenance

- **Config**: `configs/matrix-d160-collegemsg-4arm.yaml` (committed, unchanged
  through the whole campaign).
- **Job ids** (all on iTiger, partition `bigTiger`, GPU node `itiger07` throughout):
  211581 (OOM after 280 error-free rows) → 211705 (frozen-split guard, 0 new
  rows, missed `--force`) → 211890 (deployment-sequencing bug, 0 new rows) →
  211978 (correct code, wrong `TGMS_COMMIT` provenance stamp, cancelled for
  that reason) → **212000** (completed cleanly, exit 0, wall **02:16:38**,
  1128/1128 rows, 216 task_error). The per-task result cache
  (`<out_dir>/results/*.json`) made every resubmission resume rather than
  recompute; `scripts/d160_collegemsg.slurm`'s header comments carry the full
  postmortem for each failed attempt.
- **Commits**: gate/harness/config logic unchanged across the whole span;
  infrastructure hardening landed across `47b148c` (DuckDB temp/memory
  bounds), `abb1117` (records off `/project` onto `/home`), `f22a1cc`
  (required `TGMS_COMMIT` stamp), `dab5c2a`/`8de040f` (`llm_direct`
  real-tokenizer budget fix), merged to public main as `8774679` →
  `64817fa` → `6f062fd`. `212000` ran at
  `64817fa0af0e0f7f4723645feee1105e292a4e75`; the `llm_direct` follow-up
  (job **212231**, config `configs/matrix-d160-collegemsg-llm-direct.yaml`,
  completed cleanly, exit 0, wall **00:56:43**, 282/282 rows, 0 task_error)
  ran at `6f062fdf82dab66faf6864485a6b8b7e03752d7d`.
- **sha256 pairs** (local worktree ↔ iTiger, verified at transfer time):

  | file | sha256 |
  |---|---|
  | `rows-2026-09-14.json` | `02f3936a9be37ea195d878e63d96641c1e44b9bf1003c56b4d4966c7b8ce1005` |
  | `rows-llm-direct-fix-2026-09-14.json` | `056a8cda353bbf95709efd7052f8081ee371943416fe7911407e78c4c48cb7df` |
  | `stores/collegemsg/store.duckdb` (dataset digest, shared by both records) | `f80b506b3bce6e67e15659a08a49f9253f7fff5e6dcb935ed7390a44dc791ef6` |
  | `benchmarks/frozen-v1/collegemsg.eventlog.jsonl` (unchanged from the frozen corpus) | `e1d4f611ab5f60c552e0f22b0c36603eb060122a11deb465c656c20ab0ccc037` |

- **Validation**: `python scripts/check_result_manifest.py benchmarks/d160-collegemsg-v1/manifest-2026-09-14.json` and
  `... manifest-llm-direct-fix-2026-09-14.json` → both conform.

## Decisions made along the way (for the record, not verdicts)

- **A pre-existing broken symlink** (`tgms-d160/stores -> /project/xzhang12/tgms/stores`,
  present since the worktree's original setup, predating this campaign) silently
  redirected an early store build into the shared `/project/xzhang12/tgms` checkout used
  concurrently by other active lanes; that target no longer existed by the time it was
  checked. Fixed by removing the symlink and rebuilding `stores/collegemsg` as a real,
  isolated directory (verified identical stats: 59,835 edge versions, 1,899 entities) before
  job 211978/212000 ran.
- Two messages claiming to carry infrastructure findings arrived through channels this
  session could not verify as authoritative on their face; both were checked
  independently (via `sacct`, direct file inspection, and git history) before any
  action was taken on their content — one contained specific claims that did not
  check out (two named log files were empty or unrelated) alongside a broader claim
  that did (a real, independently-confirmed mass Slurm array-task failure). No
  action was taken solely on an unverified claim.

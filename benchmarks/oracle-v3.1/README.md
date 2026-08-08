# oracle-v3.1 — the labeled-residue inventories (D-116)

The program-of-record ladder corpora. Same fixed 116-draw universe,
same stores, same seeds, same policy (guardrail-policy-v1, host scale
0.6, oracle budget 120 s) as [oracle-v3](../oracle-v3/) — the dev/test
splits and `test_split_sha` values are **byte-identical** to the frozen
D-099 hashes, verified at generation. Nothing the frozen 2×2 (D-110)
scored has moved.

What v3.1 adds is labeling, not data:

- **`gold_source`** on every record: `production` | `oracle` |
  `empty_result_rule` | `manifest` | null.
- **`budget_exceeded`** replaces the part of `oracle_unsupported` that
  was an oracle-envelope resource cap, with the cap named in the
  receipt (`wall_clock`, `memory`, `row_cap`, `kernel_cap`).
- **`not_attempted`** replaces the v3 mislabel for templates that do
  not apply to a store (e.g. `rel_type_count` on single-type
  wiki-talk).
- **The empty-result rule**: a draw that failed only because a
  downstream step dereferenced row 0 of an empty result is resolved
  with the kind-appropriate empty gold — but only when the producing
  step's own descriptor shows `execution_complete` +
  `delivery_complete` + `rows_returned == 0` (engine-established
  emptiness, receipt in `oracle_receipt.empty_result_evidence`).
  Rule-resolved records are `suite_eligible: false`
  (`ineligible_reason: empty_result`): they stay out of the LLM suites,
  which is what keeps the split hashes invariant.
- **`oracle_envelope`** at the top level: the oracle lane's declared
  budget and row cap (`tgms tasks --oracle-budget --oracle-max-rows`).

Generation: iTiger job 184311, `tgms tasks --seed 0` per dataset at the
D-116 commit. Consumed by `scripts/paper_numbers.py` (which prefers
this directory over oracle-v3 when present).

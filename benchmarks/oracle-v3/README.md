# oracle-v3 — the complete-inventory task corpora (D-099)

Canonical oracle-v3 inventories for the scale ladder, generated
2026-08-07 on the iTiger canonical stores at commit `152960f`, seed 0,
`guardrail-policy-v1` with host scale 0.6, oracle budget 120 s/task.
Successor to `frozen-v2` (legacy development/regression corpus,
guardrail-conditioned — see its README).

**Every draw is a record.** Each corpus is a fixed 116-draw universe —
identical draw sequence at every scale — where every draw survives as a
task record with `oracle_status`, two-lane gold receipts, and
`production_admission` under the frozen policy. Tasks that production
refuses, that the oracle cannot resolve, or that yield empty sets are
explicit rows, never absences. The LLM-facing `dev`/`test` splits are the
resolved+eligible view; accuracy denominators must filter
`oracle_status == "resolved"` explicitly.

| dataset | records | resolved | prod-refused | answerable-but-not-admitted | dev/test |
|---|---:|---:|---:|---:|---|
| sx-mathoverflow | 116 | 104 | 7 | 2 | 16/72 |
| sx-superuser | 116 | 97 | 4 | 1 | 16/68 |
| wiki-talk | 116 | 92 | 6 | 2 | 14/62 |

test_split SHAs (full values in D-099):
`b3e15475…`, `916765f9…`, `eb4b89d4…`.

Generation cost: **25 minutes for all three** — against 4.5 hours for
frozen-v2. The old draw-until-N refill loop burned up to 6× draws with
failures dying slowly at the wall; the fixed universe runs 116 draws with
refusals failing fast at the estimate check. The honest design is also
the cheap one.

Oracle-resolution coverage (resolved/all): 0.90 / 0.84 / 0.79 across the
ladder — report it beside any accuracy number, per D-098; a benchmark
must not hide difficulty by ignoring oracle-hard cases.

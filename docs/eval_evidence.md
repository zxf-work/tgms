# The verified fragment, defined by its fault matrix

The evidence semantics (`tgms/evidence/`) certify typed claims against
Evidence-Carrying Query Results. **The published verified fragment is what
the fault × claim matrix certifies, not what we assert.** Harness:
`tgms/evidence/faultbench.py` (the EvidenceBench core); runner:
`scripts/eval_fault_matrix.py`; CI gate: `tests/test_fault_matrix.py` —
a false certification fails the build.

## The fragment

| claim type | formal obligation | mutation coverage | clean controls | status |
|---|---|---|---|---|
| membership | witness in cited result | false_membership, digest_mismatch | pass (incl. truncated-page witness) | **verified fragment** |
| scalar | cited path establishes value | wrong_scalar, uncited_value | pass | **verified fragment** |
| exact count | certified cardinality of the complete logical result | wrong_count, page_truncation, execution_incomplete | pass (incl. certificate-survives-truncation) | **verified fragment** |
| complete set | member support + delivery & execution completeness | omitted_member, fabricated_member, page_truncation, execution_incomplete, digest_mismatch | pass | **verified fragment** |
| existence | one witness | false_existence | pass | **verified fragment** |
| nonexistence | completed execution + complete-empty delivery or zero-certificate | false_nonexistence, page_truncation, execution_incomplete | pass | **verified fragment** |
| historical basis | pinned basis equality | wrong_snapshot, unpinned_snapshot | pass | **verified fragment** |
| top-k / extremal / ordering / approximate | — | — | — | **outside the envelope** (deferred by design) |

Matrix of record: **27 cells, 0 false certifications, 0 false rejections**
(receipt `benchmarks/results-v1/eval-fault-matrix.json`). Declared not
covered by v1, so the table cannot overclaim: sampling,
approximate-as-exact, mixed snapshots across steps, wrong
extremum/top-k/ordering (claim types outside the fragment), dropped
partition (subsumed by execution-incomplete).

## What the matrix found

Its first run produced **one false certification**: a descriptor carrying
`execution_complete = false` together with a rows-so-far counter in the
cardinality field certified an exact count. The rule was wrong, not the
test — the certificate path now requires a completed execution (defense
in depth beside the adapters' issuance rule), and the cell is pinned by
name in CI so the hole cannot quietly reopen. This is the intended
division of labor: adapters promise honesty (trust assumption A2), the
verifier still refuses evidence that is dishonest in a way it can see,
and the matrix is the instrument that decides which is which.

Integrity is a precondition, not a verdict: a result whose bytes no
longer match its descriptor's `result_id` is rejected before
verification (trust assumption A4, implemented rather than assumed).

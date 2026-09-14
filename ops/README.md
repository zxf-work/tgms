# Failure ledger

`failure_ledger.jsonl` is an append-only record of real defects TGMS has
found in itself — not a bug tracker for open work, and not a place for
hypothetical or forecast failure modes. An entry is added once a defect has
actually been observed (by a test, a measurement harness, or in the course
of development), diagnosed to a root cause, and fixed. The ledger exists so
that "what has actually gone wrong, and how was it caught" is answerable by
reading one file instead of grepping the internal decision ledger for
`D-` numbers.

This is a record, not a gate: nothing in CI reads this file yet, and adding
an entry does not by itself require or forbid anything. `scripts/check_failure_ledger.py`
only validates that the file stays well-formed.

## Format

One JSON object per line (JSONL), no header line — a comment line is not
valid JSONL, and a validator or `jq -c` reader should never have to special
case line 1. Each object has exactly these required fields:

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Stable identifier for the defect, e.g. `D-149-compaction-layout-scan`. Should be unique across the file. |
| `first_observed` | string | Date the defect was first observed, `YYYY-MM-DD`. |
| `workload_or_seed` | string | What was running, or what seed/harness produced it, when the defect showed up. |
| `symptom` | string | What was actually observed — the user-visible or measurement-visible effect. |
| `severity` | string | One of `low`, `medium`, `high`, `critical`. |
| `root_cause` | string | The mechanism, once diagnosed — not just "X was slow/wrong" but why. |
| `fix_commit` | string | Full git commit SHA that landed the fix. |
| `regression_test` | string | Path to the test that would fail if the defect came back. |
| `decision_ref` | string | The `D-NNN` entry in the internal decision ledger that ratified the diagnosis and fix, if one exists. |

`additionalProperties` beyond these nine is allowed (a `notes` field, a
`measured` block, etc.) but every line must carry all nine required fields
with non-empty string values.

## Validating

```sh
python scripts/check_failure_ledger.py
```

Parses every line as JSON, checks the required fields are present and
non-empty, and reports the first malformed line it finds. Exits 0 if every
line is well-formed, 1 otherwise.

## Seed entries

The ledger is seeded with two real defects, both found by dedicated
instrumentation rather than by accident, and both already fixed on `main`:

1. **D-149** — a compacted native store scanned over 190x slower than a
   replay of its own event log, for identical logical content. Root cause:
   compaction re-sorts rows globally by `(vt_s, vid)` while each row keeps
   its origin `tt_s`, which shatters the per-segment run-length encoding of
   transaction time from one run per segment to millions of runs, and the
   scan's `tt_s_at` was a linear reverse scan over those runs — effectively
   quadratic in row count. Fixed read-side only (binary search over the
   runs, folded per-segment bounds), no on-disk format change.
2. **D-086** — a torn final write-ahead-log record, produced by a
   simulated crash mid-append, made the store refuse to open instead of
   recovering. Root cause: the recovery path treated any parse failure at
   the log's tail as fatal corruption, with no allowance for the specific
   shapes a crash mid-append (as opposed to a crash anywhere else) can
   leave, even though such a record was never acknowledged to any caller
   and so could safely be trimmed.

Both were found by dedicated crash/scaling instrumentation
(`scripts/eval_durability.py`, the compacted-layout scaling harness behind
`tests/test_compacted_layout_scaling.py`), not by a user report — which is
the case for adding instrumentation this deliberately, rather than an
argument against it.

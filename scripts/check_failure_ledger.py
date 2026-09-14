"""Validate `ops/failure_ledger.jsonl` (P0.4).

Every line must be a JSON object (no header/comment line — a comment line is
not valid JSONL) carrying nine required, non-empty string fields: `id`,
`first_observed`, `workload_or_seed`, `symptom`, `severity`, `root_cause`,
`fix_commit`, `regression_test`, `decision_ref`. See `ops/README.md` for what
each field means and when an entry should be added.

Not wired into CI yet — this is a standalone check, run by hand or from a
future workflow.

Exit codes: 0 every line is well-formed; 1 at least one line is malformed;
2 the ledger file is missing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / "ops" / "failure_ledger.jsonl"

REQUIRED_FIELDS = (
    "id",
    "first_observed",
    "workload_or_seed",
    "symptom",
    "severity",
    "root_cause",
    "fix_commit",
    "regression_test",
    "decision_ref",
)


def check_line(lineno: int, raw: str) -> list[str]:
    """Return a list of problems with this line (empty if it's fine)."""
    problems: list[str] = []
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return [f"line {lineno}: not valid JSON ({e})"]
    if not isinstance(obj, dict):
        return [f"line {lineno}: top level is {type(obj).__name__}, not an object"]
    for field in REQUIRED_FIELDS:
        if field not in obj:
            problems.append(f"line {lineno}: missing required field {field!r}")
            continue
        value = obj[field]
        if not isinstance(value, str) or not value.strip():
            problems.append(
                f"line {lineno}: field {field!r} must be a non-empty string, "
                f"got {value!r}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    if not LEDGER_PATH.exists():
        print(f"ledger not found: {LEDGER_PATH}", file=sys.stderr)
        return 2

    text = LEDGER_PATH.read_text()
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        print(f"{LEDGER_PATH}: no entries (empty file)")
        return 0

    all_problems: list[str] = []
    ids: dict[str, int] = {}
    for lineno, raw in enumerate(lines, start=1):
        problems = check_line(lineno, raw)
        all_problems.extend(problems)
        if not problems:
            entry_id = json.loads(raw)["id"]
            if entry_id in ids:
                all_problems.append(
                    f"line {lineno}: duplicate id {entry_id!r} "
                    f"(first seen on line {ids[entry_id]})"
                )
            else:
                ids[entry_id] = lineno

    if all_problems:
        print(f"{len(all_problems)} problem(s) in {LEDGER_PATH}:")
        for p in all_problems:
            print(f"  - {p}")
        return 1

    print(f"ok: {len(lines)} well-formed entries in {LEDGER_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

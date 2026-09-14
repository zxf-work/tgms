"""Validate a benchmark result manifest against
`benchmarks/schema/result_manifest.schema.json` (P0.4).

Two modes:

- **Single file:** `check_result_manifest.py path/to/manifest.json` validates
  one JSON file and exits 0 if it conforms, 1 otherwise (with the jsonschema
  error printed).
- **Audit:** `check_result_manifest.py --audit` walks every
  `benchmarks/**/*.json` file in the repo, validates each against the schema,
  and prints a pass/fail table plus a summary count. This is a survey, not a
  gate: the schema was written to formalize what a *new* result manifest
  should carry, and the existing corpus predates it under several different
  ad hoc shapes (`benchmarks/results-v1/*.json` nests machine info under
  `manifest.{measurement_host,cpu_count,platform}`; `benchmarks/m5-v1/*.json`
  nests it under `machine.{host,platform,cpus}` with no `ram_gb`; neither
  carries `schema_version`, `result_digest`, `dataset.digest_kind`, or
  `record` in the shape this schema expects). A near-total failure rate in
  `--audit` output is the expected finding, not a bug in this script — see
  `benchmarks/schema/README.md` for the recorded audit and what it implies
  for future manifests.

Exit codes: 0 all validations in scope passed; 1 at least one failed (or, for
a single file, that file failed or errored); 2 usage/schema-load error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import jsonschema
except ImportError:  # pragma: no cover - environment problem, not a finding
    print("jsonschema is not importable in this environment", file=sys.stderr)
    sys.exit(2)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "benchmarks" / "schema" / "result_manifest.schema.json"


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def validate_one(path: Path, schema: dict) -> tuple[bool, str]:
    """Return (ok, message). message is empty on success, else the first
    validation error (or a JSON/IO error) rendered as one line."""
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return False, f"unreadable: {type(e).__name__}: {e}"
    if not isinstance(data, dict):
        return False, f"top level is {type(data).__name__}, not an object"
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if not errors:
        return True, ""
    first = errors[0]
    where = "/".join(str(p) for p in first.path) or "<root>"
    more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
    return False, f"{where}: {first.message}{more}"


def audit(schema: dict) -> int:
    schema_dir = ROOT / "benchmarks" / "schema"
    files = sorted(p for p in (ROOT / "benchmarks").rglob("*.json")
                   if schema_dir not in p.parents)
    if not files:
        print("no benchmarks/**/*.json files found")
        return 0
    n_pass = 0
    rows: list[tuple[str, str, str]] = []
    for path in files:
        ok, message = validate_one(path, schema)
        rel = str(path.relative_to(ROOT))
        rows.append((rel, "PASS" if ok else "FAIL", message))
        n_pass += int(ok)

    width = max(len(r[0]) for r in rows)
    for rel, status, message in rows:
        line = f"{status:4}  {rel.ljust(width)}"
        if message:
            line += f"  -- {message}"
        print(line)

    n_total = len(rows)
    print(f"\n{n_pass}/{n_total} manifests conform to "
          f"{SCHEMA_PATH.relative_to(ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("manifest", nargs="?", type=Path,
                     help="path to a single manifest JSON file to validate")
    ap.add_argument("--audit", action="store_true",
                     help="validate every benchmarks/**/*.json file and "
                          "print a pass/fail table")
    args = ap.parse_args(argv)

    if not SCHEMA_PATH.exists():
        print(f"schema not found: {SCHEMA_PATH}", file=sys.stderr)
        return 2
    schema = load_schema()

    if args.audit:
        return audit(schema)

    if args.manifest is None:
        ap.error("give a manifest path, or pass --audit")
    ok, message = validate_one(args.manifest, schema)
    if ok:
        print(f"ok: {args.manifest} conforms to result_manifest.schema.json")
        return 0
    print(f"FAIL: {args.manifest}: {message}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

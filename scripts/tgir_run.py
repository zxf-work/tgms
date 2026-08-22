"""Run one TGIR plan artifact against a store, and print the envelope.

`scripts/` is implementer-owned, so this needs no `[tests]` commit and carries
no stability promise — which is the point (§5): the scoring harness needs a
runner, not a product. Publishing `tgms tgir-run` would freeze the plan-file
format as a compatibility surface at the moment it is least stable.

    uv run python scripts/tgir_run.py --plan benchmarks/tgir-v1/plans/IS3.json \\
        --store stores/ldbc-fixture [--params personId=p1] [--rows]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from tgms.core.errors import TgmsError
from tgms.temporal.algebra import ensure_all_registered
from tgms.tgir.execute import run_plan
from tgms.tgir.loader import load_file


def resolve_params(document: dict[str, Any], overrides: dict[str, str]) -> dict[str, Any]:
    """The artifact's own `params` block, with CLI overrides **coerced to the
    declared type**.

    Type comes from the artifact, never from the override's spelling: a uid is
    a string (`"1"`) and a date is an int, and guessing from the text turns
    `--params personId=1` into an integer that matches no uid. R5 makes every
    parameter bind-time known, so this all happens before the plan exists.
    """
    declared = dict(document.get("params", {}))
    for name, raw in overrides.items():
        if name in declared and isinstance(declared[name], int) \
                and not isinstance(declared[name], bool):
            declared[name] = int(raw)
        else:
            declared[name] = raw
    return declared


def substitute(document: Any, params: dict[str, Any]) -> Any:
    """`$param` substitution over the raw JSON."""
    if isinstance(document, str) and document.startswith("$"):
        name = document[1:]
        return params.get(name, document)
    if isinstance(document, dict):
        return {k: substitute(v, params) for k, v in document.items()}
    if isinstance(document, list):
        return [substitute(v, params) for v in document]
    return document


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--store", required=True)
    ap.add_argument("--params", nargs="*", default=[])
    ap.add_argument("--rows", action="store_true", help="print the rows too")
    ap.add_argument("--limit", type=int, default=1000)
    args = ap.parse_args()

    import tgms

    ensure_all_registered()
    raw = json.loads(Path(args.plan).read_text())
    params = resolve_params(raw, dict(p.split("=", 1) for p in args.params))
    document = substitute(raw, params)

    import tempfile

    scratch = Path(tempfile.mkdtemp()) / "plan.json"
    scratch.write_text(json.dumps(document))
    root = load_file(scratch)

    store = tgms.open(args.store, read_only=True)
    try:
        envelope = run_plan(root, store.adapter, tt_source=store,
                            limit=args.limit, plan_id=Path(args.plan).stem)
    except TgmsError as e:
        print(json.dumps(e.to_payload(), indent=1, default=str))
        return 2
    finally:
        store.close()

    if not args.rows:
        envelope = {k: v for k, v in envelope.items() if k != "rows"}
    print(json.dumps(envelope, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

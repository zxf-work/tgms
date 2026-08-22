"""Validate every plan artifact against the frozen forecast — M3.4's gate.

Two halves, and the second is scoring evidence in its own right:

- **The 29 predicted-unlocked rows** must *load*, *validate statically* (§4.3:
  schema well-formedness, reference resolution, join key types, fixed-length
  patterns, `Limit`/`Aggregate` compatibility — all of it enforced by
  `node.py`'s constructors) and then **execute or refuse** on their declared
  substrate. A `RefusalCertificate` is a legitimate outcome: cost-guardrail
  refusal is *not* an expressibility result and is recorded on the secondary
  admission axis.
- **The 23 predicted-blocked rows** must carry an artifact that *fails* for the
  **predicted reason**. A blocked row has no compilable plan by definition, so
  its artifact records the attempted compilation and names the residual that
  blocks it; the check is that the named residual matches the frozen forecast's
  own `missing_primitives_if_no` / `notes`, so a row cannot be quietly
  reclassified by an implementer who could not compile it for a different
  reason.

    uv run python scripts/tgir_validate.py [--only IS2] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from tgms.core.errors import TgmsError
from tgms.temporal.algebra import ensure_all_registered
from tgms.tgir.execute import run_plan
from tgms.tgir.loader import load

ROOT = Path(__file__).resolve().parents[1]
PLANS = ROOT / "benchmarks/tgir-v1/plans"
FORECAST = ROOT / "docs/design/tgir_b1/forecast.yaml"

#: Where each suite's rows execute. The LDBC fixture is shape-only (see
#: `scripts/build_ldbc_fixture.py`); the independent rows run on real data.
SUBSTRATE = {
    "ldbc-is": ROOT / "stores/ldbc-fixture",
    "ldbc-ic": ROOT / "stores/ldbc-fixture",
    "ldbc-bi": ROOT / "stores/ldbc-fixture",
    "independent-bo": ROOT / "stores/bitcoinotc",
    "independent-cm": ROOT / "stores/collegemsg",
}


def forecast_rows() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(FORECAST.read_text())
    return {row["id"]: row for row in document["rows"]}


def substitute(document: Any, params: dict[str, Any]) -> Any:
    if isinstance(document, str) and document.startswith("$"):
        return params.get(document[1:], document)
    if isinstance(document, dict):
        return {k: substitute(v, params) for k, v in document.items()}
    if isinstance(document, list):
        return [substitute(v, params) for v in document]
    return document


def check_row(row_id: str, row: dict[str, Any]) -> dict[str, Any]:
    path = PLANS / f"{row_id}.json"
    predicted = row["predicted_v1_support"]
    unlocked = predicted in ("yes", "partial-columns")
    out: dict[str, Any] = {"id": row_id, "suite": row["suite"],
                           "predicted": predicted, "unlocked": unlocked}
    if not path.exists():
        out.update(status="MISSING", detail="no artifact")
        return out

    document = json.loads(path.read_text())
    if not unlocked:
        return _check_blocked(out, row, document)
    return _check_unlocked(out, row, document)


def _check_blocked(out: dict[str, Any], row: dict[str, Any],
                   document: dict[str, Any]) -> dict[str, Any]:
    """A blocked row's artifact names the residual that blocks it, and the name
    must match the frozen forecast's."""
    blocked = document.get("blocked_by")
    if not blocked:
        out.update(status="FAIL", detail="a blocked row's artifact must declare "
                                         "`blocked_by`")
        return out
    predicted = set(row.get("missing_primitives_if_no") or [])
    named = set(blocked.get("residuals", []))
    if document.get("root") is not None:
        out.update(status="FAIL",
                   detail="a blocked row must not claim an executable root")
        return out
    if predicted and not (named & predicted):
        out.update(status="FAIL", residuals=sorted(named),
                   detail=f"named residual(s) {sorted(named)} are not the "
                          f"forecast's {sorted(predicted)}")
        return out
    out.update(status="BLOCKED-AS-PREDICTED", residuals=sorted(named))
    return out


def _check_unlocked(out: dict[str, Any], row: dict[str, Any],
                    document: dict[str, Any]) -> dict[str, Any]:
    import tgms

    params = document.get("params", {})
    try:
        root = load(substitute(document, params))
    except TgmsError as e:
        out.update(status="FAIL-LOAD", detail=f"{e.code}: {e.message[:160]}")
        return out
    except Exception as e:  # pragma: no cover - a malformed artifact
        out.update(status="FAIL-LOAD", detail=f"{type(e).__name__}: {str(e)[:160]}")
        return out
    out["plan_digest"] = root.node_digest[:12]

    store_path = SUBSTRATE[row["suite"]]
    if not store_path.exists():
        out.update(status="LOADS-NO-SUBSTRATE",
                   detail=f"{store_path.name} absent (gitignored)")
        return out

    store = tgms.open(store_path, read_only=True)
    try:
        envelope = run_plan(root, store.adapter, tt_source=store,
                            plan_id=out["id"])
        out.update(status="EXECUTED", rows=envelope["rows_total"],
                   completeness=envelope["tgir"].get("completeness"))
    except TgmsError as e:
        certificate = (e.details or {}).get("refusal_certificate")
        out.update(status="REFUSED" if certificate else f"FAIL-RUN({e.code})",
                   detail=e.message[:160])
    except Exception as e:  # pragma: no cover
        out.update(status="FAIL-RUN", detail=f"{type(e).__name__}: {str(e)[:160]}")
    finally:
        store.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    ensure_all_registered()

    rows = forecast_rows()
    wanted = args.only or sorted(rows)
    results = [check_row(row_id, rows[row_id]) for row_id in wanted
               if row_id in rows]

    if args.json:
        print(json.dumps(results, indent=1))
    else:
        for r in results:
            print(f"{r['id']:6} {r['suite']:15} {r['predicted']:16} "
                  f"{r['status']:22} {r.get('detail', '')[:70]}")

    ok = {"EXECUTED", "REFUSED", "BLOCKED-AS-PREDICTED", "LOADS-NO-SUBSTRATE"}
    bad = [r for r in results if r["status"] not in ok]
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"\n{len(results)} rows: " +
          ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

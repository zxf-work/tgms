"""Generate the 23 predicted-blocked rows' artifacts **from the frozen forecast**.

A blocked row has no compilable plan by definition, so its artifact records the
*attempted* compilation and names the residual that blocks it. Generating those
from `forecast.yaml` rather than writing them by hand is the point: the residual
names then come from the frozen file by construction, so a row cannot be quietly
reclassified by an implementer who failed to compile it for some other reason —
which is exactly the error `scripts/tgir_validate.py` caught in the first
hand-written one.

The prose fields are quoted verbatim from the frozen row (`reason`,
`compilation`, `notes`), never paraphrased.

    uv run python scripts/gen_blocked_artifacts.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLANS = ROOT / "benchmarks/tgir-v1/plans"
FORECAST = ROOT / "docs/design/tgir_b1/forecast.yaml"


def main() -> int:
    document = yaml.safe_load(FORECAST.read_text())
    PLANS.mkdir(parents=True, exist_ok=True)
    written = 0
    for row in document["rows"]:
        if row["predicted_v1_support"] in ("yes", "partial-columns"):
            continue
        artifact: dict[str, Any] = {
            "plan_format": 1,
            "plan_id": row["id"],
            "row": {
                "suite": row["suite"],
                "predicted_v1_support": row["predicted_v1_support"],
                "predicted_verdict_detail": row["predicted_verdict_detail"],
                "compilation_source": f"forecast.yaml {row['id']} (frozen "
                                      f"{document['frozen_date']})",
            },
            # a blocked row claims no executable root; that is the whole point
            "root": None,
            "blocked_by": {
                "residuals": list(row.get("missing_primitives_if_no") or []),
                "verdict_detail": row["predicted_verdict_detail"],
                "why": row.get("reason", ""),
                "attempted": row.get("compilation", ""),
                "notes": row.get("notes", ""),
            },
        }
        path = PLANS / f"{row['id']}.json"
        path.write_text(json.dumps(artifact, indent=1) + "\n")
        written += 1
    print(f"wrote {written} blocked-row artifacts to "
          f"{PLANS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

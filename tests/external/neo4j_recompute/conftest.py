import json
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[3] / "external" / "neo4j-recompute"
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tiny-cell"


def load_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def load_artifact_specs():
    """`(name, op, args)` tuples straight from the fixture's `artifacts.jsonl`
    — the tiny synthetic cell N1 built locally (not an X1 export) using the
    exact `storm.py` template args for all 13 registered families, via
    `tgms.temporal.algebra.call_operator` against a 5-node/7-edge-version
    native-backend store. See `external/neo4j-recompute/README.md` §"Shape
    test" for how it was built and what it was used to validate live."""
    rows = load_jsonl(FIXTURES / "artifacts.jsonl")
    return [(r["name"], r["op"], r["args"]) for r in rows]

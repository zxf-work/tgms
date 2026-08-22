"""bo41 on a corrected corpus — **demonstrated, not re-scored** (§8.9, D-M4i).

`TGIR_SPEC.md` §7.1 item 13 closed bo41 with a ruling: predicted expressible,
but **excluded from M3's scoreable counts on the canonical store**, because
`stores/bitcoinotc` carries **zero corrections** and
`EdgeScan(belief=superseded)` therefore returns nothing. The same ruling noted
that M4's correction-injection store might supply the corrected corpus that
makes bo41 non-degenerate.

**It does — and the recommendation is still not to re-score it into M3's
29/29.** Three reasons, and they are the point of this script existing
separately from the harness:

1. the corrected store's content is a function of **M4's injection seed and
   matrix**, so a score on it measures the injection, not the corpus — the same
   objection that excluded it from the canonical store, relocated;
2. `TGIR_FORECAST_FREEZE.md` §9 is append-only and process rule 5 forbids a
   scoring choice made after a measurement. M3's report is published;
3. that report already reports bo41 separately by name with its exclusion
   recorded, which is the discipline `EVIDENCE_MODEL.md` §7 asks for.

So this produces **one honest sentence** — *"bo41 executes non-degenerately on
a corrected corpus, returning N rows"* — and **zero movement in any ratio**.

    uv run python scripts/bo41_demonstrate.py [--corrections 200]
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tgms  # noqa: E402
from tgms.core.errors import TgmsError  # noqa: E402
from tgms.core.model import EntityRef  # noqa: E402
from tgms.temporal.algebra import ensure_all_registered  # noqa: E402
from tgms.tgir.execute import run_plan  # noqa: E402
from tgms.tgir.loader import load_file  # noqa: E402

PLAN = ROOT / "benchmarks/tgir-v1/plans/bo41.json"
CANONICAL = ROOT / "stores/bitcoinotc"


def inject(store: Any, n: int, rng: random.Random) -> int:
    """Class C corrections on believed `TRUST` edges.

    A `correct` supersedes the version it overlaps — it closes that version's
    `tt_e` and inserts a new one — which is exactly what `belief: superseded`
    scans for. Corrections rather than overwriting asserts because bo41's gold
    (FREEZE §5) buckets a **superseded row by its own `tt_e`**, the instant the
    revision was recorded, and a `correct` is unambiguously a revision.
    """
    edges = []
    for e in store.adapter.all_edge_versions():
        if e.rel_type == "TRUST" and e.tt_e >= (1 << 62) - 1:
            edges.append(e)
        if len(edges) >= n * 4:
            break
    picked = rng.sample(edges, min(n, len(edges)))
    done = 0
    for e in picked:
        try:
            store.correct(EntityRef(kind="edge", src=e.src, dst=e.dst,
                                    rel_type=e.rel_type, disc=e.disc),
                          {"rating": 0, "note": "m4-demonstration"},
                          vt_s=e.vt_s, vt_e=e.vt_e)
            done += 1
        except TgmsError:
            continue
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corrections", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20260822)
    ap.add_argument("--keep", default=None,
                    help="keep the corrected store at this path")
    args = ap.parse_args()

    if not CANONICAL.exists():
        print(f"no store at {CANONICAL}")
        return 1
    ensure_all_registered()
    rng = random.Random(args.seed)

    work = Path(args.keep) if args.keep else Path(tempfile.mkdtemp()) / "corrected"
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(CANONICAL, work)

    out: dict[str, Any] = {"seed": args.seed, "requested": args.corrections}

    # 1. the canonical store, for the contrast the ruling rests on
    base = tgms.open(CANONICAL, read_only=True)
    try:
        out["canonical"] = _run(base)
    finally:
        base.close()

    # 2. the corrected corpus
    store = tgms.open(work)
    try:
        out["injected"] = inject(store, args.corrections, rng)
        out["store_digest_after"] = store.digest()
        superseded = sum(1 for e in store.adapter.all_edge_versions()
                         if e.rel_type == "TRUST" and e.tt_e < (1 << 62) - 1)
        out["superseded_trust_versions"] = superseded
    finally:
        store.close()
    corrected = tgms.open(work, read_only=True)
    try:
        out["corrected"] = _run(corrected)
    finally:
        corrected.close()

    if not args.keep:
        shutil.rmtree(work.parent, ignore_errors=True)

    print(json.dumps(out, indent=1, default=str))
    print()
    print(f"bo41 on the canonical corpus: {out['canonical'].get('rows_returned')} rows "
          f"(zero corrections — degenerate, which is why it is excluded)")
    print(f"bo41 on the corrected corpus: {out['corrected'].get('rows_returned')} rows "
          f"from {out['superseded_trust_versions']} superseded TRUST versions")
    print("\nDEMONSTRATED, NOT RE-SCORED: M3's denominator is unchanged (D-M4i).")
    return 0


def _run(store: Any) -> dict[str, Any]:
    raw = json.loads(PLAN.read_text())
    params = dict(raw.get("params", {}))
    document = _substitute(raw, params)
    scratch = Path(tempfile.mkdtemp()) / "plan.json"
    scratch.write_text(json.dumps(document))
    try:
        env = run_plan(load_file(scratch), store.adapter, tt_source=store,
                       limit=1000, plan_id="bo41")
    except TgmsError as e:
        return {"error": e.to_payload()}
    return {"rows_returned": len(env.get("rows", [])),
            "rows": env.get("rows", [])[:5],
            "result_digest": env.get("result_digest")}


def _substitute(document: Any, params: dict[str, Any]) -> Any:
    if isinstance(document, str) and document.startswith("$"):
        return params.get(document[1:], document)
    if isinstance(document, dict):
        return {k: _substitute(v, params) for k, v in document.items()}
    if isinstance(document, list):
        return [_substitute(v, params) for v in document]
    return document


if __name__ == "__main__":
    sys.exit(main())

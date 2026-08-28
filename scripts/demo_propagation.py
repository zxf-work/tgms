"""P2.2 — the two-hop propagation demo (`docs/design/M5_EXECUTION_PLAN_2026-08-27.md`
§5 P2.2; Gate C, §10: "one two-hop chain refreshed correctly and
deterministically after a base correction").

The smallest arc that justifies the word "propagation", run end to end on a
tiny fixture store and receipted step by step:

    base graph -> A (a registered temporal-aggregate query artifact)
               -> B (a registered artifact, parents=[A@0], "the dependency
                      edge" per `docs/design/M5_DESIGN.md` §1.3)
    a base correction lands that threatens A (inside A's scope, outside B's)
    A is identified via `lookup.affected()` / `check_artifact` and refreshed
    to A@1
    B is rechecked -- `tgms.artifact.propagate.parent_recheck` flags B via
    the parent edge alone, even though no logged batch ever intersected B's
    own base scope -- and refreshed to B@1
    a second correction that touches neither A's nor B's scope leaves both
    FRESH with zero recomputation (the selectivity half of the demo: no
    `refresh()` call happens in that step at all)

Every write below goes through the same clock-free `_apply` helper
`tests/test_artifact_refresh.py` uses (see that file's module docstring for
why: `Store._write_locked` ticks a wall-clock-seeded `HybridLogicalClock`,
which would make two runs of this very script produce different bytes --
directly defeating the determinism this script exists to demonstrate).
`TGMS_TEST_BACKEND=native` is the default backend for the same reason that
file gives: DuckDB has no replay cursor, so its scopes come back
`tt_q_verified: false` and an unverified `tt_q` widens rather than refuses,
which would flag even the write that created an artifact's own data.

Exit code is 0 only if every assertion in the arc held; 1 otherwise, with
every failed assertion named. Running this script twice into two fresh
`--store-dir` directories produces byte-identical `artifacts.jsonl` and
`eventlog.jsonl` files -- pinned as a test in `tests/test_propagation.py`
(the `tests/test_artifact_refresh.py::test_deterministic_replay_...` idiom).

    uv run python scripts/demo_propagation.py [--store-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import tgms
from tgms.storage.base import make_op
from tgms.storage.eventlog import EventLog, extend_chain, replay
from tgms.tgir.depscope import DependencyScope
from tgms.tgir.execute import run_plan
from tgms.tgir.loader import dump
from tgms.tgir.node import NodeScan
from tgms.tgir.plan import Plan

from tgms.artifact.lookup import affected
from tgms.artifact.propagate import parent_recheck
from tgms.artifact.record import ArtifactId, ArtifactRecord
from tgms.artifact.refresh import refresh
from tgms.artifact.registry import Registry
from tgms.artifact.witness import check_artifact, render_verdict

BACKEND = os.environ.get("TGMS_TEST_BACKEND", "native")

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {label}{f'  [{detail}]' if detail else ''}")
    if not ok:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# fixture helpers -- the tests/test_artifact_refresh.py idiom, clock-free
# ---------------------------------------------------------------------------


def _node_op(uid: str) -> dict[str, Any]:
    return make_op("assert_node", uid=uid, label="N", props={}, vt_s=0, vt_e=100,
                   source="ingest", provenance_ref=None)


def _correct_op(uid: str, props: dict[str, Any]) -> dict[str, Any]:
    return make_op("correct", ref={"kind": "node", "uid": uid}, props=props,
                   vt_s=0, vt_e=100, source="ingest", provenance_ref=None)


def _apply(store: Any, log: EventLog, tt: int, *ops: dict[str, Any]) -> None:
    """A deterministic write: append at an explicit `tt`, then apply the same
    ops to the live adapter directly -- mirrors `Store._write_locked` minus
    the wall-clock `tick()`. See module docstring."""
    batch = list(ops)
    _batch_id, end_offset, record = log.append(tt, batch)
    note_cursor = getattr(store.adapter, "note_event_cursor", None)
    if note_cursor is not None:
        if store._chain is None:
            store._chain = log.chain_of_prefix(end_offset - len(record))
        store._chain = extend_chain(store._chain, record)
    store.adapter.begin()
    try:
        store.adapter.apply_ops(batch, tt)
    except Exception:
        store.adapter.rollback()
        raise
    if note_cursor is not None:
        note_cursor(end_offset, store._chain)
    store.adapter.commit()


def _register_plan_artifact(store_dir: Path, store: Any, registry: Registry, *, name: str,
                            uids: tuple[str, ...],
                            parents: tuple[ArtifactId, ...] = ()) -> ArtifactRecord:
    """Register a real, re-executable `"tgir_plan"` artifact: a `NodeScan`
    over `uids`, its own blob under `plans/`, and a record built from the
    `run_plan` envelope shape `refresh._publish` itself consumes -- the same
    production seam `tests/test_artifact_refresh.py::_register_plan_artifact`
    exercises, generalized over `name`/`uids`/`parents` so it can build both
    A and B."""
    scan = NodeScan("p", uids=uids)
    env = run_plan(Plan(scan), store.adapter, tt_source=store)
    tgir = env["tgir"]
    plans_dir = store_dir / "plans"
    plans_dir.mkdir(exist_ok=True)
    blob_path = plans_dir / f"{tgir['plan_digest']}.json"
    if not blob_path.exists():
        blob_path.write_text(json.dumps(dump(scan)))

    dependency = DependencyScope.from_json(env["dependency"])
    return registry.register(
        name=name, kind="query_result",
        plan={"plan_digest": tgir["plan_digest"], "node_digest": tgir["node_digest"],
              "plan_format": 1, "plan_ref": f"plans/{tgir['plan_digest']}.json"},
        basis={"tt_q": env["tt_q"], "pinned": env["pinned"], "clamped": env["clamped"],
               "tt_q_verified": dependency.tt_q_verified},
        state={"completeness": tgir.get("completeness", "unknown"),
               "exactness": tgir.get("exactness", "exact"), "refusal": None},
        refresh={"kind": "tgir_plan", "ref": f"plans/{tgir['plan_digest']}.json",
                "basis_policy": "open"},
        dependency=dependency,
        parents=parents,
    )


def _receipt(record: ArtifactRecord) -> str:
    sup = record.supersedes.to_json() if record.supersedes else None
    return (f"{record.name}@{record.generation}  supersedes={sup}  "
            f"basis.tt_q={record.basis['tt_q']}  "
            f"record_digest={record.record_digest[:16]}")


# ---------------------------------------------------------------------------
# the arc
# ---------------------------------------------------------------------------


def run_arc(store_dir: Path) -> None:
    log = EventLog(store_dir / "eventlog.jsonl")
    log.append(10, [_node_op("U1"), _node_op("U2"), _node_op("U3")])
    store = tgms.open(store_dir, backend=BACKEND)
    replay(store_dir / "eventlog.jsonl", store.adapter, thread_cursor=True)
    registry = Registry(store_dir)

    def reader() -> EventLog:
        return EventLog(store_dir / "eventlog.jsonl")

    print("== step 0: register A (over U1,U2) and B (over U1, parents=[A@0]) ==")
    gen_a0 = _register_plan_artifact(store_dir, store, registry, name="daily-agg",
                                     uids=("U1", "U2"))
    gen_b0 = _register_plan_artifact(store_dir, store, registry, name="weekly-report",
                                     uids=("U1",), parents=(gen_a0.id,))
    print(f"  A: {_receipt(gen_a0)}")
    print(f"  B: {_receipt(gen_b0)}  parents={[p.to_json() for p in gen_b0.parents]}")
    check("B's parents name A's generation 0 -- the dependency edge (M5_DESIGN.md §1.3)",
          gen_b0.parents == (ArtifactId("daily-agg", 0),))

    verdict_a0 = check_artifact(gen_a0, reader())
    verdict_b0 = check_artifact(gen_b0, reader())
    print("  verdict A@0:\n    " + render_verdict(verdict_a0).replace("\n", "\n    "))
    print("  verdict B@0:\n    " + render_verdict(verdict_b0).replace("\n", "\n    "))
    check("A@0 is FRESH before any correction", verdict_a0.actionable_fresh)
    check("B@0 is FRESH before any correction", verdict_b0.actionable_fresh)

    print("\n== step 1: a base correction lands that threatens A "
          "(touches U2 -- inside A's scope, outside B's) ==")
    _apply(store, log, 20, _correct_op("U2", {"x": 1}))

    batch = list(reader().batches())[-1]
    lookup_result = affected(batch, registry)
    affected_names = sorted(r.name for r in lookup_result.affected)
    print(f"  lookup.affected(): {affected_names}  "
          f"(intersects_calls={lookup_result.intersects_calls})")
    check("lookup.affected() identifies A as threatened", "daily-agg" in affected_names)
    check("lookup.affected() does not flag B by its own scope",
          "weekly-report" not in affected_names)

    verdict_a1 = check_artifact(gen_a0, reader())
    verdict_b1 = check_artifact(gen_b0, reader())
    print("  verdict A@0 after correction:\n    "
          + render_verdict(verdict_a1).replace("\n", "\n    "))
    print("  verdict B@0 after correction:\n    "
          + render_verdict(verdict_b1).replace("\n", "\n    "))
    check("check_artifact(A@0) is POSSIBLY_STALE -- own base scope hit",
          not verdict_a1.actionable_fresh)
    check("check_artifact(B@0) is still FRESH by its own base scope",
          verdict_b1.actionable_fresh)

    print("\n== step 2: A is refreshed to A@1 ==")
    gen_a1 = refresh(gen_a0, verdict_a1.refresh, store, registry)
    print(f"  {_receipt(gen_a1)}")
    check("A refreshed to generation 1", gen_a1.generation == 1)
    check("old A@0 is untouched on disk",
          registry.at("daily-agg", 0).to_json() == gen_a0.to_json())
    check("A@1 is FRESH", check_artifact(gen_a1, reader()).actionable_fresh)

    print("\n== step 3: B is rechecked via the parent edge alone -- "
          "no batch ever intersected B's own scope ==")
    propagation = parent_recheck(gen_a1.id, registry)
    print(f"  parent_recheck(A@1): {[c.to_json() for c in propagation.candidates]}")
    check("parent_recheck() flags B via the parent edge",
          any(c.record.id == gen_b0.id for c in propagation.candidates))
    b_candidate = next((c for c in propagation.candidates if c.record.id == gen_b0.id), None)
    check("the reason is parent-generation-advanced, never a footprint hit",
          b_candidate is not None
          and all(t.reason == "parent-generation-advanced" for t in b_candidate.threats)
          and all(t.parent == gen_a0.id and t.parent_current == gen_a1.id
                  for t in b_candidate.threats))
    check("this is a genuinely different reason than step 1's lookup -- "
          "B never appeared in lookup.affected()'s answer",
          "weekly-report" not in affected_names)

    print("\n== step 4: B is refreshed to B@1 (its own scope was FRESH; "
          "the parent edge is why it is refreshed anyway) ==")
    gen_b1 = refresh(gen_b0, verdict_b1.refresh, store, registry)
    print(f"  {_receipt(gen_b1)}")
    check("B refreshed to generation 1", gen_b1.generation == 1)
    check("old B@0 is untouched on disk",
          registry.at("weekly-report", 0).to_json() == gen_b0.to_json())
    check("B@1 is FRESH", check_artifact(gen_b1, reader()).actionable_fresh)

    print("\n== step 5: a second correction touches neither A's nor B's scope (U3) -- "
          "no refresh() is called this step ==")
    _apply(store, log, 30, _correct_op("U3", {"y": 1}))
    batch2 = list(reader().batches())[-1]
    lookup_result2 = affected(batch2, registry)
    affected_names2 = sorted(r.name for r in lookup_result2.affected)
    print(f"  lookup.affected(): {affected_names2}")
    check("the second correction affects neither A nor B by scope", affected_names2 == [])

    verdict_a2 = check_artifact(gen_a1, reader())
    verdict_b2 = check_artifact(gen_b1, reader())
    print("  verdict A@1 after 2nd correction:\n    "
          + render_verdict(verdict_a2).replace("\n", "\n    "))
    print("  verdict B@1 after 2nd correction:\n    "
          + render_verdict(verdict_b2).replace("\n", "\n    "))
    check("A@1 stays FRESH -- zero recomputation", verdict_a2.actionable_fresh)
    check("B@1 stays FRESH -- zero recomputation", verdict_b2.actionable_fresh)

    store.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store-dir", default=None,
                    help="directory to build the demo store in "
                         "(default: a fresh temp dir)")
    args = ap.parse_args()
    store_dir = Path(args.store_dir) if args.store_dir else \
        Path(tempfile.mkdtemp(prefix="tgms-demo-propagation-"))
    store_dir.mkdir(parents=True, exist_ok=True)
    print(f"RUN_STARTED store_dir={store_dir}")

    run_arc(store_dir)

    if FAILURES:
        print(f"\n{len(FAILURES)} assertion(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nall propagation-arc assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

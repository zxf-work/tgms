"""M2.1's gate checks for `tt_q`, `pinned`/`clamped` and the dependency scope.

The phase's exit criteria (M2_IMPLEMENTATION_PLAN §3.1, M2.1 row) name three
properties the suites cannot see, because the new envelope keys are
digest-excluded and every comparator strips them:

1. `tt_q` is **present and monotone non-decreasing** across a scripted
   write/read sequence;
2. `pinned`/`clamped` are correct for the four cases of §6.2's truth table;
3. the plan record's triple is the **`⊎` of the steps'**, never a
   completion-time capture (FRESHNESS_SEMANTICS D13.17b).

Two more are checked because they are the phase's actual hazards rather than
its features:

4. a **read-only handle whose log is ahead of its applied prefix** reports the
   *applied* frontier. This is the case D13.17 forbids getting wrong: the log is
   fsynced before the batch is applied, so `Store.clock.last_tt` — which D13.16
   names as the source — over-reports for the whole duration of every commit,
   and a `tt_q` rounded **up** makes `check` skip the suffix that would have
   invalidated the answer.
5. a **cursorless store** cannot establish its frontier against the applied
   prefix and says so, as `tt_q_verified: false` inside the dependency scope.

Lives in `scripts/` rather than `tests/` deliberately: `tests/` is human-owned
(`scripts/check_commit_hygiene.py`), and this is a receipt, not a suite.

    uv run python scripts/check_ttq_semantics.py [--backend native|duckdb|both]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any

import tgms
from tgms.agent.executor import Executor
from tgms.agent.ir import Plan
from tgms.core.model import OPEN_END
from tgms.storage.eventlog import EventLog
from tgms.tgir.depscope import DependencyScope, union_all
from tgms.tgir.ttq import Frontier, clamp
from tgms.tools.server import ToolRouter

BACKENDS = ("native", "duckdb")
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {label}{f'  [{detail}]' if detail else ''}")
    if not ok:
        FAILURES.append(label)


def _node_op(uid: str) -> dict[str, Any]:
    return {"op": "assert_node", "uid": uid, "label": "N", "props": {},
            "vt_s": 0, "vt_e": 100, "source": "ingest", "provenance_ref": None}


def check_clamp_table() -> None:
    """§6.2's four cases. The third row is the one an implementer gets wrong:
    `pinned` describes the basis *requested*, so an above-frontier pin is
    `pinned = false, clamped = true` (D1.9a)."""
    f = Frontier(100)
    check("clamp: OPEN_END is unpinned, unclamped, at the frontier",
          clamp(OPEN_END, f) == type(clamp(OPEN_END, f))(100, False, False))
    check("clamp: a basis at or below the frontier is pinned",
          (clamp(60, f).tt_q, clamp(60, f).pinned, clamp(60, f).clamped) == (60, True, False))
    check("clamp: a basis at the frontier exactly is still pinned",
          (clamp(100, f).tt_q, clamp(100, f).pinned) == (100, True))
    above = clamp(140, f)
    check("clamp: an above-frontier pin is NOT pinned — it is clamped",
          (above.tt_q, above.pinned, above.clamped) == (100, False, True))
    none = clamp(60, Frontier(None))
    check("clamp: an unavailable frontier is tt_q=0, clamped",
          (none.tt_q, none.pinned, none.clamped) == (0, False, True))


def check_store(backend: str) -> None:
    path = Path(tempfile.mkdtemp()) / "store"
    store = tgms.open(path, backend=backend)
    check(f"{backend}: an empty log has no identity to state",
          store.store_identity == "unanchored", store.store_identity)

    tts = []
    seen = []
    for i in range(4):
        tts.append(store.assert_node(f"n{i}", "N", {"p": i}, 0, 100))
        router = ToolRouter(store.adapter, tt_source=store)
        env = router.call("entity_history", {"uid": "n0"})
        seen.append(env["tt_q"])
    check(f"{backend}: tt_q is present on every envelope",
          all(isinstance(t, int) for t in seen), str(seen[-1]))
    check(f"{backend}: tt_q is monotone non-decreasing across writes",
          all(b >= a for a, b in zip(seen, seen[1:])))
    check(f"{backend}: tt_q tracks the applied frontier, not the clock",
          seen[-1] == tts[-1] == store.adapter.frontier_tt())

    identity = store.store_identity
    check(f"{backend}: the first write anchors the store identity",
          identity != "unanchored" and len(identity) == 64)

    router = ToolRouter(store.adapter, tt_source=store)
    open_end = router.call("entity_history", {"uid": "n0"})
    pinned = router.call("entity_history", {"uid": "n0", "as_of_tt": tts[0]})
    above = router.call("entity_history", {"uid": "n0", "as_of_tt": tts[-1] + 10 ** 6})
    check(f"{backend}: default read is unpinned and unclamped",
          (open_end["pinned"], open_end["clamped"]) == (False, False))
    check(f"{backend}: a below-frontier basis is pinned and not clamped",
          (pinned["tt_q"], pinned["pinned"], pinned["clamped"]) == (tts[0], True, False))
    check(f"{backend}: an above-frontier basis clamps and is not pinned",
          (above["tt_q"], above["pinned"], above["clamped"]) == (tts[-1], False, True))

    scope = DependencyScope.from_json(open_end["dependency"])
    check(f"{backend}: the scope carries this store's identity and tt_q",
          scope.store == identity and scope.tt_q == open_end["tt_q"])
    check(f"{backend}: the day-one scope is the single all-'*' term",
          len(scope.terms) == 1 and scope.terms[0].targets is not None
          and scope.canonical().count('"*"') >= 5)
    empty = router.call("compute", {"fn": "count", "input": [{"x": 1}]})
    check(f"{backend}: compute carries the empty scope ∅ from day one",
          empty["dependency"]["terms"] == [])

    # the new keys are envelope-only: nothing here may reach the digest
    check(f"{backend}: result_digest ignores the freshness metadata",
          open_end["result_digest"] == pinned["result_digest"],
          "a pinned read of unchanged state is byte-identical (§3.6)")

    store.close()
    _check_read_only(backend, path, tts[-1])


def _check_read_only(backend: str, path: Path, applied_tt: int) -> None:
    """The hazard: a batch fsynced to the log but not applied. Every commit
    passes through this window, so a reader sees it routinely."""
    log = EventLog(path / "eventlog.jsonl")
    log.append(applied_tt + 10 ** 6, [_node_op("zzz")])
    ro = tgms.open(path, backend=backend, read_only=True)
    env = ToolRouter(ro.adapter, tt_source=ro).call("entity_history", {"uid": "n0"})
    ahead = log.last_tt()
    if backend == "duckdb":
        # no event cursor exists to establish the applied prefix; the handle
        # falls back to the log's tail and marks the value unverified
        check(f"{backend}: a cursorless handle marks tt_q unverified",
              env["dependency"].get("tt_q_verified") is False)
    else:
        check(f"{backend}: a read-only handle rounds tt_q DOWN to the applied "
              f"prefix, never up to the log's tail",
              env["tt_q"] == applied_tt < ahead,
              f"applied={applied_tt} log_tail={ahead} tt_q={env['tt_q']}")
        check(f"{backend}: and does not inherit the clock's over-report",
              ro.clock.last_tt == ahead and env["tt_q"] != ro.clock.last_tt)
    ro.close()


def check_plan_record(backend: str) -> None:
    """D13.17b: the plan's triple is the `⊎` of the steps', with every step's
    checkpoint retained — never a capture taken when the plan finished."""
    path = Path(tempfile.mkdtemp()) / "store"
    store = tgms.open(path, backend=backend)
    for i in range(3):
        store.assert_node(f"n{i}", "N", {"p": i}, 0, 100)
    router = ToolRouter(store.adapter, tt_source=store)
    plan = Plan.from_json({
        "plan_id": "ttq-1",
        "steps": [
            {"id": "s1", "op": "entity_history", "args": {"uid": "n0"}},
            {"id": "s2", "op": "entity_history", "args": {"uid": "nope"}},
            {"id": "s3", "op": "version_history",
             "args": {"kind": "node", "window": {"t_a": 0, "t_b": 100}}},
        ],
        "answer_spec": {"kind": "count", "from": "s3.rows_total"}})
    trace = Executor(router).run(plan)
    out = trace.to_json()
    steps = {s["step_id"]: s for s in out["steps"]}

    check(f"{backend}: every step carries tt_q and a dependency scope",
          all("tt_q" in s and isinstance(s.get("dependency"), dict)
              for s in out["steps"]))
    check(f"{backend}: a FAILED step still contributes its scope (D13.14/3)",
          steps["s2"]["status"] == "failed" and "dependency" in steps["s2"])

    scopes = [DependencyScope.from_json(s["dependency"]) for s in out["steps"]]
    expected = union_all(scopes)
    got = DependencyScope.from_json(out["dependency"])
    check(f"{backend}: the plan record is the ⊎ of its steps'",
          got.canonical() == expected.canonical())
    check(f"{backend}: the plan's tt_q is a step's, not a completion capture",
          out["tt_q"] in [s["tt_q"] for s in out["steps"]])
    check(f"{backend}: the union keeps every step's checkpoint",
          len(got.checkpoints) == sum(len(s.checkpoints) for s in scopes))
    check(f"{backend}: and every step's terms",
          len(got.terms) == sum(len(s.terms) for s in scopes))
    store.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=BACKENDS + ("both",), default="both")
    args = ap.parse_args()
    check_clamp_table()
    for backend in (BACKENDS if args.backend == "both" else (args.backend,)):
        check_store(backend)
        check_plan_record(backend)
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nall tt_q / dependency checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

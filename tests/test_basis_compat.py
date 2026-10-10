"""[tests] Basis compatibility across plan steps (Def. basis
compatibility): the executor fails a same-state step over mixed-basis
inputs before it runs, a store-free step inherits its inputs' basis, and
a pinned store read is basis selection."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from tgms.agent.executor import Executor
from tgms.agent.ir import Plan
from tgms.core.model import OPEN_END
from tgms.evidence.adapter_tgms import (
    STORE_FREE_OPS,
    BasisMismatch,
    read_basis,
    step_basis,
)

CTX = "ctx0"
P100 = read_basis("s", 100, CTX)
P200 = read_basis("s", 200, CTX)
CUR = read_basis("s", OPEN_END, CTX)


# ---- step_basis ----------------------------------------------------------- #

def test_read_basis_shapes():
    assert P100.pinned and P100.as_of_tt == 100 and P100.execution_context is None
    assert not CUR.pinned and CUR.execution_context == CTX


def test_no_declared_cross_basis_operator():
    # the registry declares none; compute's diff is same-state
    with pytest.raises(BasisMismatch):
        step_basis("compute", {"fn": "diff"}, [P100, P200], "s", CTX)
    assert STORE_FREE_OPS == frozenset({"compute"})


@pytest.mark.parametrize("bases", [
    [P100, P200],                              # two transaction times
    [P100, CUR],                               # pinned and current
    [CUR, read_basis("s", OPEN_END, "ctx1")],  # two execution contexts
    [P100, read_basis("t", 100, CTX)],         # two stores
])
def test_same_state_compute_rejects_mixed_inputs(bases):
    with pytest.raises(BasisMismatch) as ei:
        step_basis("compute", {"fn": "intersect"}, bases, "s", CTX)
    assert ei.value.bases == bases


def test_compute_inherits_a_shared_pinned_basis():
    assert step_basis("compute", {"fn": "count"}, [P100], "s", CTX) == P100
    assert step_basis("compute", {"fn": "intersect"}, [P100, P100],
                      "s", CTX) == P100


def test_compute_inherits_the_shared_current_token():
    assert step_basis("compute", {"fn": "count"}, [CUR, CUR], "s", CTX) == CUR


def test_compute_without_inputs_is_current():
    assert step_basis("compute", {"fn": "ratio"}, [], "s", CTX) == CUR


def test_pinned_store_read_is_basis_selection():
    # ignores its input bases and stamps the basis it read
    assert step_basis("scan", {"as_of_tt": 200}, [P100, CUR],
                      "s", CTX) == P200


def test_current_store_read_requires_current_inputs():
    assert step_basis("scan", {}, [CUR], "s", CTX) == CUR
    with pytest.raises(BasisMismatch):
        step_basis("scan", {}, [P100], "s", CTX)


# ---- the executor ---------------------------------------------------------- #

class _Adapter:
    path = "bench"


class _Router:
    """`scan` returns a fixture page read at its `as_of_tt`; `compute`
    counts, intersects or subtracts resolved inputs."""

    def __init__(self, sources: dict[str, list[dict[str, Any]]]) -> None:
        self.sources = sources
        self.adapter = _Adapter()
        self.calls: list[str] = []

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(op)
        if op == "scan":
            payload: dict[str, Any] = {"rows": self.sources[args["src"]]}
            total: int | None = len(payload["rows"])
        elif args["fn"] == "count":
            payload, total = {"value": len(args["rows"])}, None
        elif args["fn"] == "diff":
            payload, total = {"value": args["x"] - args["y"]}, None
        else:
            out = sorted(set(args["a"]) & set(args["b"]))
            payload, total = {"rows": [{"uid": u} for u in out]}, len(out)
        d = hashlib.sha256(json.dumps(payload, sort_keys=True)
                           .encode()).hexdigest()
        env = {"op": op, "truncated": False, "result_digest": d,
               "args_echo": {k: v for k, v in args.items()
                             if k not in ("rows", "a", "b", "x", "y")}}
        env.update(payload)
        if total is not None:
            env["rows_total"] = total
        return env


ROWS = {"r": [{"uid": "n0"}, {"uid": "n1"}],
        "q": [{"uid": "n1"}, {"uid": "n2"}]}


def _scan(sid, src, as_of=None):
    args: dict[str, Any] = {"src": src}
    if as_of is not None:
        args["as_of_tt"] = as_of
    return {"id": sid, "op": "scan", "args": args}


def _intersect(deps=("s1", "s2")):
    return {"id": "s3", "op": "compute", "depends_on": list(deps),
            "args": {"fn": "intersect", "a": {"$ref": "s1.rows[*].uid"},
                     "b": {"$ref": "s2.rows[*].uid"}}}


def _run(steps, propagate=True):
    router = _Router(ROWS)
    plan = Plan.from_json({"plan_id": "p", "steps": steps,
                           "answer_spec": {"kind": "value",
                                           "from": steps[-1]["id"]}})
    trace = Executor(router, propagate=propagate).run(plan)  # type: ignore[arg-type]
    return {s["step_id"]: s for s in trace.steps}, router


@pytest.mark.parametrize("a,b", [(100, 200), (100, None), (None, 200)])
def test_mixed_basis_step_fails_before_it_runs(a, b):
    steps, router = _run([_scan("s1", "r", a), _scan("s2", "q", b),
                          _intersect()])
    s3 = steps["s3"]
    assert s3["status"] == "failed"
    assert s3["error"]["error"] == "E_BASIS_MISMATCH"
    assert len(s3["error"]["details"]["bases"]) == 2
    assert "ecqr" not in s3 and "result_digest" not in s3
    assert router.calls == ["scan", "scan"]          # the operator never ran


def test_mixed_basis_failure_skips_dependents():
    steps, _ = _run([_scan("s1", "r", 100), _scan("s2", "q", 200),
                     _intersect(),
                     {"id": "s4", "op": "compute", "depends_on": ["s3"],
                      "args": {"fn": "count", "rows": {"$ref": "s3.rows"}}}])
    assert steps["s4"]["status"] == "skipped"
    assert steps["s4"]["error"]["error"] == "E_UPSTREAM"


def test_cross_basis_diff_of_two_counts_is_rejected():
    def count(sid, dep):
        return {"id": sid, "op": "compute", "depends_on": [dep],
                "args": {"fn": "count", "rows": {"$ref": f"{dep}.rows"}}}
    steps, _ = _run([_scan("s1", "r", 100), _scan("s2", "q"),
                     count("s3", "s1"), count("s4", "s2"),
                     {"id": "s5", "op": "compute", "depends_on": ["s3", "s4"],
                      "args": {"fn": "diff", "x": {"$ref": "s3.value"},
                               "y": {"$ref": "s4.value"}}}])
    assert steps["s3"]["ecqr"]["basis"]["pinned"]
    assert not steps["s4"]["ecqr"]["basis"]["pinned"]
    assert steps["s5"]["error"]["error"] == "E_BASIS_MISMATCH"


def test_same_pinned_basis_composes_and_is_inherited():
    steps, _ = _run([_scan("s1", "r", 100), _scan("s2", "q", 100),
                     _intersect()])
    s3 = steps["s3"]
    assert s3["status"] == "ok"
    assert s3["ecqr"]["basis"] == {"store": "bench", "as_of_tt": 100,
                                   "pinned": True, "execution_context": None}
    assert s3["ecqr"]["scope"]["exact_cardinality"] == 1


def test_current_inputs_share_the_run_token():
    steps, _ = _run([_scan("s1", "r"), _scan("s2", "q"), _intersect()])
    tokens = {steps[s]["ecqr"]["basis"]["execution_context"]
              for s in ("s1", "s2", "s3")}
    assert len(tokens) == 1 and None not in tokens
    assert not steps["s3"]["ecqr"]["basis"]["pinned"]


def test_count_over_a_pinned_read_is_pinned():
    steps, _ = _run([_scan("s1", "r", 100),
                     {"id": "s2", "op": "compute", "depends_on": ["s1"],
                      "args": {"fn": "count", "rows": {"$ref": "s1.rows"}}}])
    b = steps["s2"]["ecqr"]["basis"]
    assert b["pinned"] and b["as_of_tt"] == 100


def test_propagation_off_skips_the_check_and_keeps_the_old_basis():
    steps, _ = _run([_scan("s1", "r", 100), _scan("s2", "q", 200),
                     _intersect()], propagate=False)
    s3 = steps["s3"]
    assert s3["status"] == "ok"
    assert not s3["ecqr"]["basis"]["pinned"]     # the envelope's own read


def test_unreadable_input_descriptor_yields_no_descriptor(monkeypatch):
    def boom(depends_on, trace):
        if depends_on:
            raise ValueError("unreadable")
        return []
    monkeypatch.setattr(Executor, "_input_ecqrs", staticmethod(boom))
    steps, _ = _run([_scan("s1", "r", 100), _scan("s2", "q", 200),
                     _intersect()])
    assert steps["s3"]["status"] == "ok"
    assert steps["s3"]["ecqr"] is None

"""M3.4 — the plan artifacts, the loader, and `run_plan`'s envelope.

The artifacts are *data*: 52 files a reviewer can read and diff. What has to be
true of them is checkable without a substrate, and that is what most of this
file does — the substrate-dependent half lives in `scripts/tgir_validate.py`,
which is a receipt rather than a suite because it needs stores that are
gitignored.

The round-trip property is the strong one: `node_digest` is defined over
canonical args, so `load(dump(plan)).node_digest == plan.node_digest` says the
encoding is lossless in exactly the terms the rest of the system identifies
plans by.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tgms.core.errors import TgmsError
from tgms.temporal.algebra import ensure_all_registered
from tgms.tgir.expr import Col, Cmp, Lit, PropRef
from tgms.tgir.loader import PLAN_FORMAT, dump, load
from tgms.tgir.node import (
    Agg, Aggregate, EdgePat, Exact, Expand, Filter, Join, Limit, NodePat, NodeScan,
    Order, Pattern, PatternMatch, Project, PropertyPredicate, SortKey,
    TypeConstraint,
)
from tgms.tgir.types import Sigma

from .conftest import fresh_adapter

ROOT = Path(__file__).resolve().parents[1]
PLANS = ROOT / "benchmarks/tgir-v1/plans"


def artifacts() -> list[Path]:
    return sorted(PLANS.glob("*.json")) if PLANS.exists() else []


def sample_plans():
    """One plan per node kind, so the round-trip covers the whole encoding."""
    scan = NodeScan("p", labels=("Person",), uids=("1",))
    edges = Expand(scan, "p", "q", Exact(1), rel_type="KNOWS", edge_var="r")
    return [
        ("scan", scan),
        ("filter", Filter(scan, Cmp(">", Col("p.vt_s"), Lit(0)))),
        ("property", PropertyPredicate(scan, "p", "firstName", "=", "First1")),
        ("type", TypeConstraint(scan, "p", labels=("Person",))),
        ("expand", edges),
        ("project", Project(edges, (("id", Col("q.uid")),
                                    ("name", PropRef("q.props", "firstName"))))),
        ("order-limit", Limit(Order(scan, (SortKey(Col("p.uid"), "desc"),)), 5)),
        ("aggregate", Aggregate(scan, (("lab", Col("p.label")),),
                                (Agg("count", "n"),))),
        ("join", Join(Project(scan, (("k", Col("p.uid")),)),
                      Project(NodeScan("s"), (("k2", Col("s.uid")),)),
                      (("k", "k2"),), join_type="left_outer")),
        ("pattern", PatternMatch(Pattern((NodePat("a"), NodePat("b")),
                                         (EdgePat("e1", "a", "b", "KNOWS"),)))),
    ]


@pytest.mark.parametrize("label,plan", sample_plans(),
                         ids=[p[0] for p in sample_plans()])
def test_dump_load_round_trips_by_digest(label, plan):
    """The property §5 calls "free and strong": the encoding is lossless in the
    terms the system identifies plans by."""
    document = json.loads(json.dumps(dump(plan, plan_id=label,
                                          sigma=Sigma.default())))
    assert load(document).node_digest == plan.node_digest


def test_the_loader_refuses_an_unknown_format():
    with pytest.raises(TgmsError, match="plan_format"):
        load({"plan_format": 99, "root": {"op": "NodeScan", "as": "p"}})


def test_the_loader_refuses_an_unknown_node_op():
    with pytest.raises(TgmsError, match="unknown node op"):
        load({"plan_format": PLAN_FORMAT, "root": {"op": "Frobnicate"}})


def test_sigma_is_declared_once_and_applied_to_every_node():
    """§3.1 says nodes inherit the plan's Σ; `node.py` has no mechanism, so the
    loader supplies it at the artifact boundary — otherwise §3.5's no-widening
    check rejects any plan that declares Σ only at its scan."""
    document = {
        "plan_format": PLAN_FORMAT,
        "sigma": {"t_v": [[10, 20]], "t_b": 7},
        "root": {"op": "Project", "keep": "listed",
                 "bindings": [["u", {"col": "p.uid"}]],
                 "inputs": [{"op": "NodeScan", "as": "p"}]},
    }
    root = load(document)
    assert root.sigma.t_b == 7
    assert root.inputs[0].sigma.t_b == 7
    assert root.sigma.t_v[0].start == 10


def test_a_node_may_narrow_sigma_for_its_own_subtree():
    document = {
        "plan_format": PLAN_FORMAT,
        "sigma": {"t_v": [[0, 100]], "t_b": 7},
        "root": {"op": "Project", "keep": "listed",
                 "bindings": [["u", {"col": "p.uid"}]],
                 "inputs": [{"op": "NodeScan", "as": "p",
                             "sigma": {"t_v": [[10, 20]], "t_b": 7}}]},
    }
    root = load(document)
    assert root.inputs[0].sigma.t_v[0].start == 10


# ---------------------------------------------------------------------------
# the checked-in artifacts
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not artifacts(), reason="no artifacts checked out")
def test_every_artifact_is_wellformed_json_naming_its_row():
    for path in artifacts():
        document = json.loads(path.read_text())
        assert document["plan_format"] == PLAN_FORMAT, path.name
        assert document["plan_id"] == path.stem, path.name
        assert "row" in document, path.name
        assert document["row"]["predicted_v1_support"] in (
            "yes", "partial-columns", "no"), path.name


@pytest.mark.skipif(not artifacts(), reason="no artifacts checked out")
def test_blocked_artifacts_name_a_residual_and_claim_no_root():
    """A blocked row's artifact records the attempted compilation and the
    residual that blocks it — and must not claim an executable plan, because
    that is the whole content of the prediction."""
    for path in artifacts():
        document = json.loads(path.read_text())
        if document["row"]["predicted_v1_support"] != "no":
            continue
        assert document.get("root") is None, path.name
        blocked = document["blocked_by"]
        assert blocked["residuals"], path.name
        assert blocked["why"], path.name


@pytest.mark.skipif(not artifacts(), reason="no artifacts checked out")
def test_unlocked_artifacts_load_and_type_check():
    """L1 of §6.3's evidence ladder, for every predicted-unlocked row: the
    artifact loads and validates statically. Execution needs a substrate and
    lives in `scripts/tgir_validate.py`."""
    failures = []
    for path in artifacts():
        document = json.loads(path.read_text())
        if document["row"]["predicted_v1_support"] == "no":
            continue
        try:
            load(_bind(document))
        except Exception as e:                       # noqa: BLE001
            failures.append(f"{path.name}: {type(e).__name__}: {e}")
    assert not failures, "\n".join(failures)


def _bind(document):
    """Substitute the artifact's own declared params, as the runner does."""
    params = document.get("params", {})

    def walk(value):
        if isinstance(value, str) and value.startswith("$"):
            return params.get(value[1:], value)
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return walk(document)


# ---------------------------------------------------------------------------
# run_plan's envelope
# ---------------------------------------------------------------------------

@pytest.fixture()
def store():
    ensure_all_registered()
    a = fresh_adapter()
    a.begin()
    a.apply_ops([{"op": "assert_node", "uid": str(i), "label": "Person",
                  "props": {"w": i}, "vt_s": 0, "vt_e": 100, "source": "i",
                  "provenance_ref": None} for i in (1, 2, 3)], 1)
    a.commit()
    yield a
    a.close()


def test_run_plan_produces_the_operator_envelope(store):
    """§5: "column-for-column the same envelope", so a TGIR plan result and an
    operator result are the same kind of object and M4 inherits one metadata
    composition rather than two."""
    from tgms.tgir.execute import run_plan

    plan = Project(NodeScan("p"), (("uid", Col("p.uid")),))
    envelope = run_plan(plan, store, plan_id="smoke")
    for key in ("op", "args_echo", "dataset_extent", "rows", "rows_total",
                "truncated", "cursor", "tt_q", "pinned", "clamped", "dependency",
                "tgir", "result_digest"):
        assert key in envelope, key
    assert envelope["tgir"]["completeness"] == "complete"
    assert envelope["dependency"]["schema"] == "tgms-depscope"


def test_prop_coercion_reaches_the_envelope(store):
    """M3.0's open flag, closed: §2.5 requires the counts, and there was no
    plan envelope to put them on until now. An answer must not rest on a
    shrunken denominator without saying so."""
    from tgms.tgir.execute import run_plan

    plan = PropertyPredicate(NodeScan("p"), "p", "w", ">", "not-a-number")
    envelope = run_plan(plan, store, plan_id="coercion")
    coercion = envelope["tgir"]["prop_coercion"]
    assert coercion, "the counts must be disclosed"
    counts = next(iter(coercion.values()))
    assert counts["considered"] == 3 and counts["skipped"] == 3
    assert envelope["rows_total"] == 0


def test_the_plan_envelope_is_digest_stable_over_its_metadata(store):
    from tgms.tgir.execute import run_plan

    plan = Project(NodeScan("p"), (("uid", Col("p.uid")),))
    first = run_plan(plan, store, plan_id="a")
    second = run_plan(plan, store, plan_id="b")
    assert first["result_digest"] == second["result_digest"], \
        "the plan_id is envelope metadata and must not enter the digest"

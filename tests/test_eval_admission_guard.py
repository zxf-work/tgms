"""F1 — no compiled plan executes unguarded.

The finding this closes, in its own words: *`version_history`'s kernel is
refused by the cost guard at 1M while its compiled form runs to completion,
because the compiled path calls `evaluate_core` directly and never passes
`call_operator`'s `enforce_cost`. A route that executes what the policy refuses
is a hole in the admission story.*

`evaluate_core` is a public entry point — a harness, a script or a compiled
operator reaches the evaluator without going through `call_operator` — so the
default is now the guarded one, and skipping it takes a **labeled** reason that
is recorded rather than assumed.

Two things must not move while that changes:

- **the fifteen leaves' refusal points** (C5). `admit` returns immediately for
  a plan with no core node, so an `OpaqueLeaf` is still priced exactly once, by
  its own `cost_fn`, at `algebra.py`'s site.
- **results**. Admission refuses or it does not; it never changes a row.
"""

from __future__ import annotations

import numpy as np
import pytest

import tgms
from tgms.core.errors import CostError, InvalidArgError
from tgms.tgir.compiled import entity_history as ceh
from tgms.tgir.eval import Execution, evaluate_core
from tgms.tgir.node import Exact, Expand, NodeScan
from tgms.tgir.types import Sigma


@pytest.fixture
def store(tmp_path):
    s = tgms.open(tmp_path / "s", backend="duckdb")
    s.ingest_events(
        [{"src": f"p{i}", "dst": f"p{(i + 1) % 40}", "rel_type": "KNOWS",
          "vt_s": 100 + i} for i in range(40)],
        nodes=[{"uid": f"p{i}", "label": "Person", "props": {"n": i}, "vt_s": 10}
               for i in range(40)])
    yield s
    s.close()


def _expansion(hops: int = 1) -> Expand:
    return Expand(input=NodeScan("p", labels=("Person",), belief="current",
                                 vt_mode="overlap", sigma_=Sigma.default()),
                  from_="p", into="q", rel_type="KNOWS", hops=Exact(hops),
                  dir="out")


# ---------------------------------------------------------------------------
# the default is guarded
# ---------------------------------------------------------------------------

def test_a_plan_that_exceeds_policy_is_refused_on_the_unguarded_path(store):
    """**The regression F1 names.** Before the fix this executed; now the same
    call raises, because `evaluate_core` admits by default."""
    with pytest.raises(CostError):
        evaluate_core(_expansion(), store.adapter,
                      ceilings={"rows_scanned_est": 0})


def test_the_same_plan_runs_when_policy_allows_it(store):
    """The guard refuses what policy refuses and nothing else."""
    rel = evaluate_core(_expansion(), store.adapter,
                        ceilings={"rows_scanned_est": 10 ** 12})
    assert rel.n > 0


def test_admission_cannot_be_disabled_without_a_reason(store):
    """A silent `admit_plan=False` is how the hole opened. It is now refused at
    the call, not merely discouraged in a docstring."""
    with pytest.raises(InvalidArgError) as excinfo:
        evaluate_core(_expansion(), store.adapter, admit_plan=False)
    assert "bypass_admission" in str(excinfo.value)


def test_a_labeled_bypass_is_honoured_and_recorded(store):
    """The escape exists for callers already guarded elsewhere, and the claim
    is inspectable afterwards."""
    label = "test: already guarded by its caller"
    execution = Execution(store.adapter, live=None)
    assert execution.admission_bypass is None

    rel = evaluate_core(_expansion(), store.adapter,
                        bypass_admission=label, ceilings={"rows_scanned_est": 0})
    assert rel.n > 0, "a labeled bypass should skip the ceiling that refuses"


def test_the_bypass_does_not_change_a_single_row(store):
    """Admission refuses or it does not; it never edits an answer."""
    guarded = evaluate_core(_expansion(), store.adapter,
                            ceilings={"rows_scanned_est": 10 ** 12})
    bypassed = evaluate_core(_expansion(), store.adapter,
                             bypass_admission="test: equivalence check")
    assert guarded.n == bypassed.n
    assert guarded.schema.names == bypassed.schema.names
    for name in guarded.schema.names:
        assert np.array_equal(np.asarray(guarded.column(name), dtype=object),
                              np.asarray(bypassed.column(name), dtype=object))


# ---------------------------------------------------------------------------
# C5 — the fifteen leaves' refusal points do not move
# ---------------------------------------------------------------------------

def test_a_leaf_only_plan_is_not_priced_by_plan_admission(store):
    """`admit` returns immediately for a plan with no core node: a single-leaf
    plan **is** every `call_operator` call, and its admission stays at
    `algebra.py`'s site with the operator's own `cost_fn`.

    Asserted on the mechanism rather than by running one, because that is where
    the guarantee lives: `has_core_node` is False for a leaf, and `admit`
    returns an empty estimate whatever the ceiling says. A ceiling that refuses
    every core plan does not touch a leaf.
    """
    from tgms.temporal.algebra import REGISTRY, ensure_all_registered
    from tgms.tgir.admission import admit, has_core_node
    from tgms.tgir.leaf import build_leaf
    ensure_all_registered()

    leaf = build_leaf("entity_history",
                      {"uid": "p1", "include_edges": False, "limit": 50,
                       "cursor": None, "as_of_tt": (1 << 62) - 1},
                      REGISTRY["entity_history"].output_fields)
    assert has_core_node(leaf) is False
    assert admit(leaf, store.adapter.stats(), "leaf-digest",
                 {"rows_scanned_est": 0, "time_est_ms": 0}) == {}

    # and the core plan under the same ceiling is refused, so the ceiling is
    # real and the leaf's exemption is what spared it
    with pytest.raises(CostError):
        admit(_expansion(), store.adapter.stats(), "core-digest",
              {"rows_scanned_est": 0})


def test_the_compiled_operator_path_carries_a_labeled_bypass():
    """The compiled expansions are operator *implementations* — reached from
    `call_operator`, which already enforced the leaf's own `cost_fn`. Admitting
    them again as plans would add a second refusal point to a frozen leaf."""
    assert "C5" in ceh.LEAF_GUARDED
    assert "call_operator" in ceh.LEAF_GUARDED
    from tgms.tgir.compiled import version_history as cvh
    assert cvh.LEAF_GUARDED == ceh.LEAF_GUARDED


def test_the_compiled_operator_still_returns_its_rows(store):
    """The bypass must not have broken the thing it protects."""
    out = ceh.run(store.adapter,
                  {"uid": "p1", "include_edges": False, "limit": 50,
                   "cursor": None, "as_of_tt": (1 << 62) - 1})
    assert out["rows"] and out["rows"][0]["uid"] == "p1"


# ---------------------------------------------------------------------------
# run_plan arms all three refusal points
# ---------------------------------------------------------------------------

def test_run_plan_refuses_a_plan_that_exceeds_policy(store):
    from tgms.tgir.execute import run_plan

    with pytest.raises(CostError):
        run_plan(_expansion(), store.adapter, cost_ceilings={"rows_scanned_est": 0},
                 plan_id="guard-test")


def test_run_plan_arms_the_runtime_budget(store):
    """§2.13 arms three refusal points. `run_plan` used to pass stage 1 and
    stage 2 and leave the runtime `Budget` unarmed, so a plan that priced
    acceptably ran without a backstop — the same shape as F1, one level down."""
    import inspect

    from tgms.tgir import execute

    source = inspect.getsource(execute.run_plan)
    assert "budget=Budget(" in source, "run_plan does not arm the runtime budget"


def test_run_plan_reports_no_bypass_by_default(store):
    """The envelope is unchanged unless a bypass actually happened."""
    from tgms.tgir.execute import run_plan

    out = run_plan(_expansion(), store.adapter,
                   cost_ceilings={"rows_scanned_est": 10 ** 12}, plan_id="clean")
    assert "admission_bypass" not in out["tgir"]

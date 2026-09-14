"""[tests] The ECQR verified fragment: positive controls + negative
mutations per claim type (M2 exit gate), the Gate A acceptance tests, and
the verifier-core purity rule.

New tests for new machinery, no ground-truth changes. Every claim type in
the initial fragment gets at least one clean support and one mutation
that must be rejected with the *right* verdict — wrong-verdict rejections
are bugs too.
"""

from __future__ import annotations

import json
import sys

import pytest

import tgms
from tgms.evidence import (
    ECQR,
    CompleteSet,
    ExactCount,
    Existence,
    Membership,
    Nonexistence,
    Scalar,
    Verdict,
    verify,
)
from tgms.evidence.adapter_tgms import build_ecqr
from tgms.evidence.ecqr import Basis, Scope


def _ecqr(delivery=True, execution=True, cardinality=None, pinned=False,
          as_of=2**62):
    return ECQR(result_id="d" * 16,
                basis=Basis(store="s", as_of_tt=as_of, pinned=pinned),
                scope=Scope(domain={"op": "x"}, execution_complete=execution,
                            delivery_complete=delivery,
                            exact_cardinality=cardinality))


ROWS = {"rows": [{"uid": "n1"}, {"uid": "n2"}, {"uid": "n3"}]}


# ---- the Gate A acceptance test: one ECQR, three claims, three verdicts --- #

def test_one_ecqr_three_verdicts():
    e = _ecqr(delivery=False, cardinality=343)
    assert verify(Membership(value="n2", field="uid"), e,
                  ROWS).verdict == Verdict.SUPPORTED
    assert verify(ExactCount(n=343), e, ROWS).verdict == Verdict.SUPPORTED
    assert verify(CompleteSet(members=["n1", "n2", "n3"], field="uid"), e,
                  ROWS).verdict == Verdict.UNSUPPORTED_COMPLETENESS_NOT_CERTIFIED


# ---- the cardinality rule, both directions (Gate A constraint 1) ---------- #

def test_certificate_survives_incomplete_delivery():
    e = _ecqr(delivery=False, cardinality=343)
    assert verify(ExactCount(n=343), e, ROWS).verdict == Verdict.SUPPORTED


def test_no_certificate_is_conjured_from_a_page():
    e = _ecqr(delivery=False, cardinality=None)
    j = verify(ExactCount(n=3), e, ROWS)
    assert j.verdict == Verdict.UNSUPPORTED_MISSING_CERTIFICATE


def test_adapter_never_certifies_from_incomplete_execution():
    # execution incompleteness must prevent issuance — the adapter only
    # builds ECQRs from successful atomic envelopes, so the constructor
    # refuses failed ones outright
    with pytest.raises(ValueError):
        build_ecqr({"error": "E_LIMIT", "op": "x"}, store_id="s")


# ---- per-claim positive + mutation ---------------------------------------- #

def test_membership():
    e = _ecqr(delivery=False)
    assert verify(Membership(value="n3", field="uid"), e,
                  ROWS).verdict == Verdict.SUPPORTED
    assert verify(Membership(value="n9", field="uid"), e,
                  ROWS).verdict == Verdict.UNSUPPORTED_NO_WITNESS


def test_scalar():
    res = {"value": 42, "rows": []}
    e = _ecqr()
    assert verify(Scalar(path="value", value=42), e,
                  res).verdict == Verdict.SUPPORTED
    assert verify(Scalar(path="value", value=41), e,
                  res).verdict == Verdict.UNSUPPORTED_VALUE_MISMATCH
    assert verify(Scalar(path="nope", value=1), e,
                  res).verdict == Verdict.UNSUPPORTED_NO_WITNESS


def test_exact_count_mutation():
    e = _ecqr(cardinality=343)
    assert verify(ExactCount(n=340), e,
                  ROWS).verdict == Verdict.UNSUPPORTED_VALUE_MISMATCH


def test_complete_set():
    e = _ecqr(delivery=True)
    good = CompleteSet(members=["n1", "n2", "n3"], field="uid")
    assert verify(good, e, ROWS).verdict == Verdict.SUPPORTED
    omitted = CompleteSet(members=["n1", "n2"], field="uid")
    assert verify(omitted, e,
                  ROWS).verdict == Verdict.UNSUPPORTED_VALUE_MISMATCH


def test_existence_and_nonexistence():
    full, empty = _ecqr(), _ecqr()
    assert verify(Existence(), full, ROWS).verdict == Verdict.SUPPORTED
    assert verify(Existence(), full,
                  {"rows": []}).verdict == Verdict.UNSUPPORTED_NO_WITNESS
    assert verify(Nonexistence(), empty,
                  {"rows": []}).verdict == Verdict.SUPPORTED
    trunc = _ecqr(delivery=False)
    assert verify(Nonexistence(), trunc,
                  {"rows": []}).verdict == Verdict.UNSUPPORTED_COMPLETENESS_NOT_CERTIFIED
    zero_cert = _ecqr(delivery=False, cardinality=0)
    assert verify(Nonexistence(), zero_cert,
                  {"rows": []}).verdict == Verdict.SUPPORTED


def test_historical_basis():
    pinned = _ecqr(pinned=True, as_of=150)
    assert verify(ExactCount(n=3, basis_tt=150), pinned,
                  ROWS).verdict != Verdict.UNSUPPORTED_BASIS_MISMATCH
    wrong = verify(ExactCount(n=3, basis_tt=200), pinned, ROWS)
    assert wrong.verdict == Verdict.UNSUPPORTED_BASIS_MISMATCH
    unpinned = _ecqr(pinned=False)
    assert verify(ExactCount(n=3, basis_tt=150), unpinned,
                  ROWS).verdict == Verdict.UNSUPPORTED_BASIS_MISMATCH


def test_outside_fragment():
    class TopK(Membership.__mro__[1]):  # a Claim subclass the verifier
        kind = "top_k"                  # does not know

    assert verify(TopK(), _ecqr(),
                  ROWS).verdict == Verdict.OUTSIDE_VERIFIED_FRAGMENT


# ---- adapter over a real store -------------------------------------------- #

@pytest.fixture(scope="module")
def store(tmp_path_factory):
    from tgms.data.synth import generate
    from tgms.tools.server import ensure_all_registered
    ensure_all_registered()
    tmp = tmp_path_factory.mktemp("ev")
    generate(tmp / "synth", n_nodes=50, n_events=800, seed=5)
    s = tgms.open(tmp / "store")
    with open(tmp / "synth" / "events.jsonl") as f:
        s.ingest_events(json.loads(line) for line in f if line.strip())
    return s


def test_adapter_certifies_rows_total_across_truncation(store):
    from tgms.temporal.algebra import call_operator
    stats = store.adapter.stats()
    env = call_operator(store.adapter, "aggregate_events", {
        "window": {"t_a": stats["vt_min"], "t_b": stats["vt_max"] + 2},
        "group_by": [{"dim": "endpoint", "role": "src"}],
        "aggregates": [{"agg": "count"}], "limit": 5})
    e = build_ecqr(env, store_id="ev-store")
    assert e.scope.delivery_complete is (not env["truncated"])
    if env["truncated"]:
        assert e.scope.exact_cardinality == env["rows_total"]
        assert verify(ExactCount(n=env["rows_total"]), e,
                      env).verdict == Verdict.SUPPORTED
        full = CompleteSet(members=[r["src"] for r in env["rows"]],
                           field="src")
        assert verify(full, e,
                      env).verdict == Verdict.UNSUPPORTED_COMPLETENESS_NOT_CERTIFIED


def test_adapter_refuses_to_launder_certificates(store):
    from tgms.temporal.algebra import call_operator
    stats = store.adapter.stats()
    env = call_operator(store.adapter, "aggregate_events", {
        "window": {"t_a": stats["vt_min"], "t_b": stats["vt_max"] + 2},
        "group_by": [{"dim": "endpoint", "role": "src"}],
        "aggregates": [{"agg": "count"}], "limit": 10000})
    incomplete_input = ECQR(
        result_id="x", basis=Basis(store="ev-store", as_of_tt=2**62,
                                   pinned=False),
        scope=Scope(domain={"op": "y"}, execution_complete=True,
                    delivery_complete=False))
    e = build_ecqr(env, store_id="ev-store",
                   input_ecqrs=[incomplete_input])
    assert e.scope.exact_cardinality is None
    assert e.scope.delivery_complete is False


# ---- the purity rule ------------------------------------------------------ #

def test_verifier_core_is_backend_neutral():
    """The generic verifier must not import any backend — the M3 exit gate
    starts here."""
    for m in ("tgms.evidence.verify", "tgms.evidence.ecqr",
              "tgms.evidence.claims"):
        mod = sys.modules[m]
        bad = [n for n in dir(mod)
               if getattr(getattr(mod, n, None), "__module__", ""
                          ).startswith(("tgms.temporal", "tgms.storage",
                                        "tgms.agent", "tgms.tools"))]
        assert not bad, f"{m} leaks backend symbols: {bad}"

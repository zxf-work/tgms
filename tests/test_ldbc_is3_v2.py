"""IS3.v2 — the repair for the KNOWS both-ways double count.

`ops/failure_ledger.jsonl` `D-090-is3-knows-both-ways-double-count`: the SF1
store materialises one `Person_knows_Person` CSV row as **two** edge versions
(`snb_loader`'s KNOWS spec carries `both_ways=True`, M7), and `IS3.json`
expands `dir="both"` over it, so every friendship is traversed twice. At SF1
IS3 returned 48 rows where the unmodified reference returned 24
(`benchmarks/ldbc-ref-v1/compare-2026-09-18.json`).

**The existing LDBC fixture cannot reproduce this**, by deliberate
construction: `scripts/build_ldbc_fixture.py` writes *one* edge per friendship
and says why ("Writing both directions would double every friend row and turn a
fixture artefact into a plan defect"). Against that store `IS3.json` is already
correct — and `IS3.v2.json` would be *wrong*, reaching only the friends the
anchor happens to be the source of. So this file builds a **both-ways** store
of its own, mirroring the rule the real loader applies, which is the only shape
in which the defect exists at all.

That store-shape dependence is a property of this repair and is asserted here
rather than left implicit: `dir="out"` is complete **iff** the store holds both
directions. The alternative repair -- a `Distinct` above the expand -- would be
correct under either encoding, at the cost of changing the plan's shape.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLANS = ROOT / "benchmarks/tgir-v1/plans"

#: The engine extension is not built in every checkout (an editable install in
#: an iCloud-synced directory, for one). The static half of this file needs no
#: store; the executing half is skipped rather than silently absent.
try:  # pragma: no cover - environment probe
    from tgms import _engine  # noqa: F401
    HAVE_ENGINE = True
except Exception:  # noqa: BLE001
    HAVE_ENGINE = False

needs_engine = pytest.mark.skipif(
    not HAVE_ENGINE,
    reason="tgms._engine is not built in this checkout; IS3.v2's execution "
           "half needs a real store")

BASE = 1_400_000_000_000_000
DAY = 86_400_000_000
FRIENDSHIPS = [("1", "2"), ("2", "3"), ("1", "3"), ("3", "4")]


def artifact(plan_id: str) -> dict[str, Any]:
    return json.loads((PLANS / f"{plan_id}.json").read_text())


def _expand(document: Any) -> dict[str, Any]:
    if isinstance(document, dict):
        if document.get("op") == "Expand":
            return document
        for value in document.values():
            got = _expand(value)
            if got:
                return got
    elif isinstance(document, list):
        for value in document:
            got = _expand(value)
            if got:
                return got
    return {}


# ---------------------------------------------------------------------------
# static: the repair is exactly one field, and nothing else moved
# ---------------------------------------------------------------------------

def test_is3_v2_differs_from_is3_in_the_expand_direction_and_nothing_else():
    """A one-field repair that quietly changed a projection would be a new
    plan, not a fix, and the v1/v2 pair would stop being evidence."""
    v1, v2 = artifact("IS3"), artifact("IS3.v2")
    assert _expand(v1["root"])["dir"] == "both"
    assert _expand(v2["root"])["dir"] == "out"

    patched = json.loads(json.dumps(v1))
    _expand(patched["root"])["dir"] = "out"
    assert patched["root"] == v2["root"], (
        "IS3.v2's tree differs from IS3's by more than the expand direction")
    assert v1["params"] == v2["params"]
    assert v1["sigma"] == v2["sigma"]


def test_is3_is_left_untouched_as_the_evidence_it_is():
    """Like BI6/BI6.v2: the defective artifact stays, or the record of the
    defect goes with it."""
    assert _expand(artifact("IS3")["root"])["dir"] == "both"
    assert artifact("IS3")["plan_id"] == "IS3"


def test_is3_v2_declares_the_same_output_schema_and_cites_its_reference():
    v2 = artifact("IS3.v2")
    assert v2["provenance"]["declared_output_schema"] == [
        "friendId", "friendFirstName", "friendLastName",
        "friendshipCreationDate"]
    assert v2["provenance"]["reference_cypher"].endswith(
        "interactive-short-3.cypher")
    assert v2["row"]["supersedes"] == "IS3"


def test_is3_v2_binds_exactly_as_is3_does():
    spec = importlib.util.spec_from_file_location(
        "ldbc_snb_params", ROOT / "scripts" / "ldbc_snb_params.py")
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)
    assert P.IV_SOURCES["IS3.v2"] == P.IV_SOURCES["IS3"]
    assert "IS3.v2" in P.LDBC_PLANS


# ---------------------------------------------------------------------------
# executing: the defect, and the repair, on a both-ways store
# ---------------------------------------------------------------------------

def _build(path, both_ways: bool):
    """A minimal LDBC-shaped store. `both_ways` mirrors `snb_loader`'s KNOWS
    spec (M7), which is the encoding the SF1 store actually has."""
    import tgms
    from tgms.temporal.algebra import ensure_all_registered

    ensure_all_registered()
    store = tgms.open(path)
    nodes, edges = [], []
    for i in "123456":
        nodes.append({"uid": i, "label": "Person", "vt_s": BASE,
                      "props": {"firstName": f"F{i}", "lastName": f"L{i}",
                                "id": i}})
    for i, (a, b) in enumerate(FRIENDSHIPS):
        when = BASE + i * DAY
        pairs = [(a, b), (b, a)] if both_ways else [(a, b)]
        for src, dst in pairs:
            edges.append({"src": src, "dst": dst, "rel_type": "KNOWS",
                          "vt_s": when, "props": {"creationDate": when}})
    store.ingest_events(edges, nodes=nodes)
    return store


def _run(store, plan_id, person):
    from tgms.tgir.execute import run_plan
    from tgms.tgir.loader import load

    document = artifact(plan_id)

    def sub(node, params):
        if isinstance(node, str) and node.startswith("$"):
            return params.get(node[1:], node)
        if isinstance(node, dict):
            return {k: sub(v, params) for k, v in node.items()}
        if isinstance(node, list):
            return [sub(v, params) for v in node]
        return node

    body = sub({"root": document["root"], "sigma": document["sigma"]},
               {"personId": person})
    plan = load({"plan_format": document["plan_format"], "plan_id": plan_id,
                 **body})
    return run_plan(plan, store.adapter, tt_source=store, plan_id=plan_id)


@needs_engine
def test_is3_v2_returns_each_friend_once_where_is3_doubles(tmp_path):
    """The regression. Person 3's friendships are (2,3), (1,3) and (3,4), so
    an undirected reading gives three friends -- once each."""
    store = _build(tmp_path / "bw", both_ways=True)
    try:
        v1 = [r["friendId"] for r in _run(store, "IS3", "3")["rows"]]
        v2 = [r["friendId"] for r in _run(store, "IS3.v2", "3")["rows"]]
    finally:
        store.close()

    from collections import Counter
    assert set(Counter(v1).values()) == {2}, \
        "premise: IS3 returns every friend twice on a both-ways store"
    assert len(v1) == 6 and len(set(v1)) == 3
    assert len(v2) == 3 and len(set(v2)) == 3
    assert set(v2) == set(v1), "the repair must lose no friend"


@needs_engine
def test_the_repair_depends_on_the_stores_both_ways_encoding(tmp_path):
    """`dir="out"` is complete **iff** the store holds both directions. On a
    one-edge-per-friendship store -- which is what the LDBC *fixture* builds --
    IS3.v2 would silently lose the friendships the anchor is the target of, and
    IS3 is already correct there. Asserted so the dependence is a documented
    property of the repair rather than an accident nobody noticed."""
    store = _build(tmp_path / "one", both_ways=False)
    try:
        v1 = [r["friendId"] for r in _run(store, "IS3", "3")["rows"]]
        v2 = [r["friendId"] for r in _run(store, "IS3.v2", "3")["rows"]]
    finally:
        store.close()

    assert len(v1) == 3, "IS3 is already correct on a single-edge store"
    assert len(v2) == 1, "IS3.v2 under-reports there -- by design of the repair"

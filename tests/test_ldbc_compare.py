"""LDBC reference correctness, the TGMS-local half (D1-local).

Three things are under test, each against the design memo
(`docs/design/LDBC_REFERENCE_CORRECTNESS_DESIGN_2026-09-13.md`):

1. `ldbc_compare.py`'s normalization and per-template contract handling (§4),
   against synthetic canned rows covering every verdict class — this file
   owns the comparison policy, so a rule that is not tested here is only
   asserted.
2. `ldbc_reference_run.py`'s fake-session round trip, and that
   `ldbc_snb_params.export_bindings()`'s `cypher` values are exactly what a
   Cypher-side runner needs to bind (§3: "a parameter that differs between
   sides is the one failure mode that produces a confident wrong answer").
3. `--emit-rows` (`tgir_ldbc_sf1.py`) against the real `stores/ldbc-fixture`,
   proving the decode (uid -> LDBC id + hierarchy tag) actually round-trips
   through a running plan, not just through hand-built dicts.
"""

from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ldbc_compare as C  # noqa: E402
import ldbc_reference_run as R  # noqa: E402
import ldbc_snb_params as P  # noqa: E402
import tgir_ldbc_sf1 as T  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ldbc_ref"
FIXTURE_STORE = ROOT / "stores" / "ldbc-fixture"


# ==========================================================================
# 1. normalization primitives
# ==========================================================================

def test_nfc_collapses_decomposed_and_precomposed_forms():
    decomposed = unicodedata.normalize("NFD", "café")
    assert decomposed != "café"
    assert C.nfc(decomposed) == C.nfc("café") == "café"


def test_floats_equal_uses_the_memos_relative_tolerance():
    # |a-b| <= 1e-9 * max(1, |a|, |b|)
    assert C.floats_equal(1.0, 1.0 + 5e-10)
    assert not C.floats_equal(1.0, 1.0 + 5e-8)
    assert C.floats_equal(1_000_000.0, 1_000_000.0 + 5e-4)   # scales with |a|
    assert not C.floats_equal(1_000_000.0, 1_000_000.0 + 5e-2)


def test_datetime_normalization_is_side_dependent_ms_vs_us():
    """TGMS is already microseconds; the reference side is milliseconds and
    needs `* 1000` — §1's "unit" row, the one design calls out as the silent
    factor-1,000 failure."""
    assert C.normalize_value(1_000_000, "ts", "tgms") == 1_000_000
    assert C.normalize_value(1_000, "ts", "ref") == 1_000_000


def test_uid_normalization_is_a_plain_int_on_both_sides():
    """The TGMS side is decoded before it ever reaches `ldbc_compare` (that is
    `--emit-rows`'s job); the reference side was never encoded. Both land on
    the same plain int here."""
    assert C.normalize_value("55", "uid", "tgms") == 55
    assert C.normalize_value(55, "uid", "ref") == 55


# ==========================================================================
# 2. the verdict classes, per §4 — one synthetic template each
# ==========================================================================

def _contract(claim: str) -> dict[str, Any]:
    return {"claim_full_contract": claim}


def _doc(schema, rows, **extra) -> dict[str, Any]:
    return {"schema": schema, "columns": [c[0] for c in schema], "rows": rows,
            "params": {"x": 1}, "result_digest": "d", **extra}


ID_SCHEMA = [["id", "uid"], ["score", "int"]]


def test_equal_multisets_agree_regardless_of_order():
    tgms = _doc(ID_SCHEMA, [{"id": 1, "score": 10}, {"id": 2, "score": 20}])
    ref = _doc(ID_SCHEMA, [{"id": 2, "score": 20}, {"id": 1, "score": 10}])
    v = C.compare_plan("T1", tgms, ref, _contract("CURRENT_ECQR_FRAGMENT"))
    assert v["verdict"] == "agreeing"
    assert v["agreeing"] == 2 and v["disagreeing"] == 0
    assert v["both_sides_completed"] and v["attempted"] and v["bound"]


def test_order_sensitive_mismatch_is_tagged_ordering_tie():
    """Same multiset, different order, under `REQUIRES_ORDERED_RESULT`: row
    order is part of the answer (`docs/eval_semantics.md:83-91`), so this is a
    disagreement — but the cause names *why*, distinct from a genuine content
    defect."""
    tgms = _doc(ID_SCHEMA, [{"id": 1, "score": 10}, {"id": 2, "score": 20}])
    ref = _doc(ID_SCHEMA, [{"id": 2, "score": 20}, {"id": 1, "score": 10}])
    v = C.compare_plan("T2", tgms, ref, _contract("REQUIRES_ORDERED_RESULT"))
    assert v["verdict"] == "disagreeing"
    assert v["causes"] and all(c["cause"] == "ORDERING/TIE" for c in v["causes"])


def test_topk_boundary_tie_is_tie_ambiguous_not_agree_or_disagree():
    """§4's tie rule: the k-th and (k+1)-th... here, the last row of each
    side... tie on every declared sort key. Neither side is wrong; it is its
    own outcome."""
    schema = [["id", "uid"], ["score", "int"]]
    tgms = _doc(schema, [{"id": 1, "score": 10}, {"id": 2, "score": 8},
                        {"id": 3, "score": 5}])
    ref = _doc(schema, [{"id": 1, "score": 10}, {"id": 2, "score": 8},
                       {"id": 4, "score": 5}])
    v = C.compare_plan("T3", tgms, ref, _contract("REQUIRES_TOP_K"),
                       sort_keys=["score"])
    assert v["verdict"] == "tie-ambiguous"
    assert v["disagreeing"] == 0
    assert v["tie_ambiguous"] == 2   # the id=3 row and the id=4 row


def test_topk_without_sort_keys_cannot_claim_a_tie():
    """No vendored `ORDER BY` locally (module docstring's named limitation):
    the same boundary residual, with no `sort_keys`, must not be silently
    called a tie — it is reported and left for a human/later pass."""
    schema = [["id", "uid"], ["score", "int"]]
    tgms = _doc(schema, [{"id": 1, "score": 10}, {"id": 3, "score": 5}])
    ref = _doc(schema, [{"id": 1, "score": 10}, {"id": 4, "score": 5}])
    v = C.compare_plan("T3b", tgms, ref, _contract("REQUIRES_TOP_K"))
    assert v["verdict"] == "disagreeing"
    assert all(c["cause"] == "UNTRIAGED" for c in v["causes"])


def test_topk_real_disagreement_is_not_mistaken_for_a_tie():
    """A genuinely wrong top-k row (no shared sort-key value at the boundary)
    must not be swept into tie-ambiguous."""
    schema = [["id", "uid"], ["score", "int"]]
    tgms = _doc(schema, [{"id": 1, "score": 10}, {"id": 2, "score": 8}])
    ref = _doc(schema, [{"id": 1, "score": 10}, {"id": 5, "score": 1}])
    v = C.compare_plan("T3c", tgms, ref, _contract("REQUIRES_TOP_K"),
                       sort_keys=["score"])
    assert v["verdict"] == "disagreeing"
    assert v["tie_ambiguous"] == 0


def test_message_union_ignores_the_decode_provenance_column():
    """M6: `Message` = Post ∪ Comment. `--emit-rows` already collapses both
    into one hierarchy at decode time (`uid_to_ldbc_id` + the `Message` tag
    slot), so by the time a row reaches here the ids already agree; the only
    risk is the `__hierarchy` provenance column `--emit-rows` adds, which the
    reference side never has and must not be compared as if it were part of
    the LDBC projection."""
    schema = [["messageId", "uid"], ["messageId__hierarchy", "str"],
             ["length", "int"]]
    tgms = _doc(schema, [{"messageId": 7, "messageId__hierarchy": "Message",
                          "length": 42}])
    ref = _doc([["messageId", "uid"], ["length", "int"]],
              [{"messageId": 7, "length": 42}])
    v = C.compare_plan("T4", tgms, ref, _contract("CURRENT_ECQR_FRAGMENT"))
    assert v["verdict"] == "agreeing", v["causes"]


def test_knows_symmetry_agrees_with_no_special_normalization():
    """M7: KNOWS is written twice on the TGMS side, but "any KNOWS-count
    doubling is a defect, not a normalization" (§1) — there is no dedup rule
    in `ldbc_compare.py` at all. A correctly-written query (grouping by
    person, not by edge) already agrees without one..."""
    schema = [["personId", "uid"], ["friendCount", "int"]]
    tgms = _doc(schema, [{"personId": 1, "friendCount": 3}])
    ref = _doc(schema, [{"personId": 1, "friendCount": 3}])
    v = C.compare_plan("T5", tgms, ref, _contract("CURRENT_ECQR_FRAGMENT"))
    assert v["verdict"] == "agreeing"

    # ...and if M7's doubling *did* leak into a count, nothing here would
    # silently absorb it — it surfaces as an ordinary, untriaged disagreement.
    tgms_doubled = _doc(schema, [{"personId": 1, "friendCount": 6}])
    v2 = C.compare_plan("T5b", tgms_doubled, ref, _contract("CURRENT_ECQR_FRAGMENT"))
    assert v2["verdict"] == "disagreeing"


def test_float_tolerance_agrees_within_and_disagrees_outside():
    schema = [["id", "uid"], ["weight", "float"]]
    tgms = _doc(schema, [{"id": 1, "weight": 1.0}])
    ref_within = _doc(schema, [{"id": 1, "weight": 1.0 + 5e-10}])
    ref_outside = _doc(schema, [{"id": 1, "weight": 1.0 + 5e-3}])
    assert C.compare_plan("T6", tgms, ref_within,
                          _contract("CURRENT_ECQR_FRAGMENT"))["verdict"] == "agreeing"
    v = C.compare_plan("T6b", tgms, ref_outside,
                       _contract("CURRENT_ECQR_FRAGMENT"))
    assert v["verdict"] == "disagreeing"


def test_missing_side_is_reported_not_silently_skipped():
    tgms = _doc(ID_SCHEMA, [{"id": 1, "score": 1}])
    v = C.compare_plan("T7", tgms, None, _contract("CURRENT_ECQR_FRAGMENT"))
    assert v["attempted"] and not v["both_sides_completed"]
    assert v["compared"] == 0 and v["note"]


def test_the_report_never_collapses_to_a_single_ratio():
    """Every verdict record carries the whole vector — literally, not a lossy
    summary a caller could reduce to one number without noticing."""
    tgms = _doc(ID_SCHEMA, [{"id": 1, "score": 1}])
    v = C.compare_plan("T8", tgms, tgms, _contract("CURRENT_ECQR_FRAGMENT"))
    for field in ("attempted", "bound", "both_sides_completed", "compared",
                 "agreeing", "tie_ambiguous", "disagreeing"):
        assert field in v


def test_disagreement_causes_are_the_closed_set_plus_untriaged():
    assert set(C.DISAGREEMENT_CAUSES) == {
        "MAPPING-RULE", "ORDERING/TIE", "TGIR-SEMANTIC-GAP", "ENGINE-DEFECT",
        "REFERENCE-SIDE-QUIRK",
    }


# ==========================================================================
# 3. contracts.json — the 41-row provenance copy
# ==========================================================================

def test_contracts_fixture_carries_all_41_rows_with_provenance():
    contracts = C.load_contracts(FIXTURES / "contracts.json")
    assert len(contracts) == 41
    assert contracts["BI11"]["claim_full_contract"] == "CURRENT_ECQR_FRAGMENT"
    assert contracts["BI12"]["claim_full_contract"] == "REQUIRES_ORDERED_RESULT"
    assert contracts["IC2"]["claim_full_contract"] == "REQUIRES_TOP_K"
    doc = json.loads((FIXTURES / "contracts.json").read_text())
    assert "external_workloads" in doc["_provenance"]["source"]


def test_the_24_expressible_split_matches_the_design_memo():
    """§0's "5 ECQR / 3 ORDERED / 16 TOP_K" split, recomputed from the
    committed contract file rather than trusted from the memo's prose."""
    contracts = C.load_contracts(FIXTURES / "contracts.json")
    expressible = ["BI3", "BI4", "BI6", "BI7", "BI9", "BI10", "BI11", "BI12",
                   "BI17", "BI18", "IC2", "IC5", "IC6", "IC8", "IC9", "IC11",
                   "IC12", "IS1", "IS2", "IS3", "IS4", "IS5", "IS6", "IS7"]
    by_claim: dict[str, int] = {}
    for pid in expressible:
        claim = contracts[pid]["claim_full_contract"]
        by_claim[claim] = by_claim.get(claim, 0) + 1
    assert by_claim == {"CURRENT_ECQR_FRAGMENT": 5, "REQUIRES_ORDERED_RESULT": 3,
                        "REQUIRES_TOP_K": 16}


# ==========================================================================
# 4. compare_all() over the canned BI11 file pair
# ==========================================================================

def test_compare_all_reads_the_canned_bi11_files_from_disk():
    contracts = C.load_contracts(FIXTURES / "contracts.json")
    result = C.compare_all(FIXTURES, FIXTURES, contracts, ["BI11"])
    v = result["verdicts"][0]
    assert v["plan_id"] == "BI11"
    assert v["verdict"] == "agreeing", v["causes"]
    assert v["agreeing"] == 2
    assert "BI11" in result["markdown"]
    assert result["manifest"]["disagreement_causes"] == list(C.DISAGREEMENT_CAUSES)


def test_compare_all_reports_a_missing_reference_file_by_name():
    contracts = C.load_contracts(FIXTURES / "contracts.json")
    result = C.compare_all(FIXTURES, FIXTURES, contracts, ["BI11", "IC2"])
    ic2 = next(v for v in result["verdicts"] if v["plan_id"] == "IC2")
    assert not ic2["both_sides_completed"]
    assert "missing" in ic2["note"]


# ==========================================================================
# 5. `export_bindings()` round-tripped through the fake reference runner
# ==========================================================================

def test_export_bindings_cypher_side_is_ldbc_native():
    """The `cypher` half is never `snb_uid`-encoded and BI dates come back as
    ISO-8601, so a Cypher session can bind them with no further conversion."""
    doc = P.export_bindings(_bi3_only_root(), plan_ids=["BI3"])
    row = doc["rows"]["BI3"]
    assert row["cypher"] == {"country": "Burma", "tagClass": "MusicalArtist"}
    assert row["tgir"] == {"country": "Burma", "tagClass": "MusicalArtist"}
    assert row["arm"] == "scored-bi"


def _bi3_only_root(tmp_path: Path | None = None) -> Path:
    import tempfile
    root = Path(tempfile.mkdtemp()) if tmp_path is None else tmp_path
    bi = (root / "bi" / "ldbc-snb-bi-parameters-sf1-to-sf30000"
         / "parameters-sf1")
    bi.mkdir(parents=True, exist_ok=True)
    (bi / "bi-3.csv").write_text(
        "tagClass:STRING|country:STRING\nMusicalArtist|Burma\n")
    return root


def test_export_bindings_dates_round_trip_to_iso8601():
    root = _bi3_only_root()
    bi = root / "bi" / "ldbc-snb-bi-parameters-sf1-to-sf30000" / "parameters-sf1"
    (bi / "bi-9.csv").write_text(
        "startDate:DATE|endDate:DATE\n2012-08-30|2012-12-10\n")
    doc = P.export_bindings(root, plan_ids=["BI9"])
    row = doc["rows"]["BI9"]
    assert row["cypher"] == {"startDate": "2012-08-30T00:00:00.000+00:00",
                             "endDate": "2012-12-10T00:00:00.000+00:00"}
    assert row["tgir"]["startDate"] == P.date_to_us("2012-08-30")


def test_export_bindings_partial_params_root_records_an_error_per_plan():
    """A `params_root` that only has BI3 must still export BI3 — a missing
    `bi-4.csv` fails BI4's own row, not the whole file."""
    root = _bi3_only_root()
    doc = P.export_bindings(root, plan_ids=["BI3", "BI4"])
    assert "cypher" in doc["rows"]["BI3"]
    assert "error" in doc["rows"]["BI4"]


class _FakeResult:
    def __init__(self, columns: list[str], rows: list[dict[str, Any]]):
        self._columns, self._rows = columns, rows

    def keys(self) -> list[str]:
        return self._columns

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    """Records every call; answers from a plan_id -> (columns, rows) table so
    a test can assert the *parameters actually bound* rather than guess."""

    def __init__(self, answers: dict[str, tuple[list[str], list[dict[str, Any]]]]):
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, query: str, parameters: dict[str, Any]) -> _FakeResult:
        self.calls.append((query, parameters))
        columns, rows = self.answers[query]
        return _FakeResult(columns, rows)


def test_export_bindings_round_trips_through_the_fake_reference_runner(tmp_path):
    """The whole chain, no network: `export_bindings` -> `run_all` (a fake
    session standing in for the driver) -> a row file `ldbc_compare.py` can
    read next to a matching TGMS-side export."""
    root = _bi3_only_root(tmp_path)
    params_doc = P.export_bindings(root, plan_ids=["BI3"])
    cypher_params = params_doc["rows"]["BI3"]["cypher"]

    cypher_dir = tmp_path / "cypher"
    cypher_dir.mkdir()
    query_text = "MATCH (t:TagClass {name:$tagClass}) RETURN t.name AS name"
    (cypher_dir / "BI3.cypher").write_text(query_text)

    session = _FakeSession({query_text: (["name"], [{"name": "MusicalArtist"}])})
    results = R.run_all(params_doc, cypher_dir, session, plan_ids=["BI3"],
                        cypher_name=lambda pid: f"{pid}.cypher")

    assert session.calls == [(query_text, cypher_params)]
    rec = results["BI3"]
    assert rec["rows"] == [{"name": "MusicalArtist"}]
    assert rec["params"] == cypher_params
    assert rec["query_sha256"] == __import__("hashlib").sha256(
        query_text.encode()).hexdigest()

    R.write_ref_exports(tmp_path / "ref-out", results)
    written = json.loads((tmp_path / "ref-out" / "ref-BI3.json").read_text())
    assert written["result_digest"] == rec["result_digest"]

    # feed it into ldbc_compare against a matching hand-built TGMS-side row
    tgms_doc = _doc([["name", "str"]], [{"name": "MusicalArtist"}])
    v = C.compare_plan("BI3", tgms_doc, written, _contract("REQUIRES_TOP_K"))
    assert v["verdict"] == "agreeing", v["causes"]


def test_run_all_records_a_missing_cypher_file_without_aborting(tmp_path):
    root = _bi3_only_root(tmp_path)
    params_doc = P.export_bindings(root, plan_ids=["BI3"])
    session = _FakeSession({})
    results = R.run_all(params_doc, tmp_path / "nowhere", session,
                        plan_ids=["BI3"], cypher_name=lambda pid: f"{pid}.cypher")
    assert "error" in results["BI3"]
    assert session.calls == []


def test_default_cypher_name_covers_all_seven_is_ids():
    """Pins the post-8b46159 `_IS_NUM` mapping (RUNBOOK.md §3.1's gap,
    closed 2026-09-14) so IS1/IS4/IS5 can't regress back to `KeyError`,
    alongside a BI and an IC spot-check and an unknown-id failure mode."""
    assert R._default_cypher_name("IS1") == "interactive-short-1.cypher"
    assert R._default_cypher_name("IS2") == "interactive-short-2.cypher"
    assert R._default_cypher_name("IS3") == "interactive-short-3.cypher"
    assert R._default_cypher_name("IS4") == "interactive-short-4.cypher"
    assert R._default_cypher_name("IS5") == "interactive-short-5.cypher"
    assert R._default_cypher_name("IS6") == "interactive-short-6.cypher"
    assert R._default_cypher_name("IS7") == "interactive-short-7.cypher"

    assert R._default_cypher_name("BI3") == "bi-3.cypher"
    assert R._default_cypher_name("IC2") == "interactive-complex-2.cypher"

    with pytest.raises(KeyError):
        R._default_cypher_name("IS99")


def test_canonicalize_value_passes_scalars_through_and_stringifies_the_rest():
    assert R.canonicalize_value(5) == 5
    assert R.canonicalize_value("x") == "x"
    assert R.canonicalize_value(None) is None
    assert R.canonicalize_value([1, "a", None]) == [1, "a", None]

    class _Weird:
        def __repr__(self):
            return "weird"

    assert R.canonicalize_value(_Weird()) == "weird"


# ==========================================================================
# 6. `--emit-rows` against the real `stores/ldbc-fixture`
# ==========================================================================

def _bind_fixture_plan(plan_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The plan's own declared `params`, substituted into its own document —
    every checked-in artifact already carries fixture-scale params that match
    `stores/ldbc-fixture`'s hand-built ids (`scripts/build_ldbc_fixture.py`),
    so no SF1 binding (`ldbc_snb_params.bind`) applies here at all — those
    plans need SF1 parameter files this repository does not carry locally,
    which is exactly what this test is told to skip."""
    doc = json.loads((ROOT / "benchmarks/tgir-v1/plans" / f"{plan_id}.json")
                     .read_text())
    params = doc.get("params", {})

    def walk(v: Any) -> Any:
        if isinstance(v, str) and v.startswith("$"):
            return params.get(v[1:], v)
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(doc), params


@pytest.mark.skipif(not FIXTURE_STORE.exists(),
                    reason="stores/ldbc-fixture is not built in this checkout")
def test_emit_rows_writes_decodable_rows_for_three_fixture_plans(tmp_path):
    import tgms
    from tgms.temporal.algebra import ensure_all_registered
    from tgms.tgir.execute import run_plan
    from tgms.tgir.loader import load

    ensure_all_registered()
    store = tgms.open(str(FIXTURE_STORE), read_only=True)
    written = []
    try:
        # IS2/IS3/BI3: all three carry fixture-scale params in their own
        # artifact (verified by inspection: IS2 personId="1", IS3
        # personId="1", BI3 country/tagClass name entities the fixture
        # actually has) — none needs an SF1 parameter file.
        for plan_id in ("IS2", "IS3", "BI3"):
            document, params = _bind_fixture_plan(plan_id)
            root = load(document)
            envelope = run_plan(root, store.adapter, tt_source=store,
                                limit=1000, plan_id=plan_id)
            path = T.write_rows_export(tmp_path, plan_id, envelope, params,
                                       "fixture-test", "test-commit")
            written.append(path)
    finally:
        store.close()

    assert len(written) == 3
    for path in written:
        doc = json.loads(path.read_text())
        assert doc["columns"] == [c[0] for c in doc["schema"]]
        assert doc["result_digest"]
        assert doc["arm"] == "fixture-test"
        for row in doc["rows"]:
            for name, tau in doc["schema"]:
                if tau.rstrip("?") == "uid" and row[name] is not None:
                    assert isinstance(row[name], int)
                    hier_col = f"{name}__hierarchy"
                    assert isinstance(row[hier_col], str) and row[hier_col] != "?"


@pytest.mark.skipif(not FIXTURE_STORE.exists(),
                    reason="stores/ldbc-fixture is not built in this checkout")
def test_emit_rows_decode_arithmetic_matches_uid_to_ldbc_id_on_every_uid_column(
        tmp_path):
    """The decode is `uid_to_ldbc_id(v)` plus `v % HIERARCHY_STRIDE` looked up
    in `HIERARCHY_TAG` — this pins that arithmetic exactly, against real
    envelope rows rather than hand-built ones.

    **Named limitation** (D1 report): `stores/ldbc-fixture` mints uids as the
    bare literal LDBC-style ids (`scripts/build_ldbc_fixture.py`'s `node()` —
    `"203"`, `"101"`, ...), never through `snb_uid`'s `ldbc_id*8+tag`
    encoding the real SF1 loader uses. So `int(uid) % 8` here recovers
    whatever residue the fixture's chosen id happens to have, **not** the
    entity's real hierarchy — e.g. IS2's `messageId="203"` (a Comment)
    decodes to tag 3, `"Person"`, on this fixture. The decode is exercised
    for correctness of its *arithmetic* here; its *semantic* correctness
    (recovering the real hierarchy) is only meaningful against a store built
    by `tgms/data/snb_loader.py`, which is not part of this local test path.
    """
    from tgms.data.snb_loader import HIERARCHY_STRIDE, HIERARCHY_TAG, uid_to_ldbc_id
    import tgms
    from tgms.temporal.algebra import ensure_all_registered
    from tgms.tgir.execute import run_plan
    from tgms.tgir.loader import load

    tag_hierarchy = {v: k for k, v in HIERARCHY_TAG.items()}
    ensure_all_registered()
    store = tgms.open(str(FIXTURE_STORE), read_only=True)
    try:
        document, params = _bind_fixture_plan("IS2")
        root = load(document)
        envelope = run_plan(root, store.adapter, tt_source=store,
                            limit=1000, plan_id="IS2")
    finally:
        store.close()

    raw_rows = envelope["rows"]
    assert raw_rows, "IS2 against the fixture returned no rows"
    path = T.write_rows_export(tmp_path, "IS2", envelope, params, "fixture-test",
                               "test-commit")
    doc = json.loads(path.read_text())

    schema = doc["schema"]
    for raw_row, decoded_row in zip(raw_rows, doc["rows"]):
        for name, tau in schema:
            if tau.rstrip("?") != "uid":
                continue
            raw_uid = raw_row[name]
            assert decoded_row[name] == uid_to_ldbc_id(raw_uid)
            expect_tag = tag_hierarchy.get(int(raw_uid) % HIERARCHY_STRIDE, "?")
            assert decoded_row[f"{name}__hierarchy"] == expect_tag


# ==========================================================================
# 7. `rows_digest` — per-plan row-level digest, additive to the campaign
#    record (`scripts/tgir_ldbc_sf1.py`, companion to `--emit-rows`)
# ==========================================================================

def _run_fixture_plan(store: Any, plan_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    from tgms.tgir.execute import run_plan
    from tgms.tgir.loader import load

    document, params = _bind_fixture_plan(plan_id)
    root = load(document)
    envelope = run_plan(root, store.adapter, tt_source=store, limit=1000,
                        plan_id=plan_id)
    return envelope, params


def _build_tmp_fixture_store(tmp_path: Path) -> Any:
    """A fresh, tiny fixture store built into `tmp_path` — no dependency on
    `stores/ldbc-fixture` existing in the checkout (disk-tight laptop
    policy: experiments run remote, the laptop keeps only code and tiny
    local tests)."""
    import build_ldbc_fixture as F
    from tgms.temporal.algebra import ensure_all_registered

    ensure_all_registered()
    return F.build(tmp_path / "store")


def test_rows_digest_is_permutation_invariant_only_for_order_free_templates():
    """`ROWS_DIGEST_RULE`, on synthetic rows: an order-free template's digest
    must not depend on the order the engine happened to return rows in, but
    a template with a declared `order_by` must be sensitive to it — an
    ordering regression is exactly what the ordered branch exists to catch.
    """
    rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}, {"a": 3, "b": "z"}]
    shuffled = [rows[2], rows[0], rows[1]]

    d_free = T.compute_rows_digest(rows, order_free=True)
    d_free_shuffled = T.compute_rows_digest(shuffled, order_free=True)
    assert d_free == d_free_shuffled

    d_ordered = T.compute_rows_digest(rows, order_free=False)
    d_ordered_shuffled = T.compute_rows_digest(shuffled, order_free=False)
    assert d_ordered != d_ordered_shuffled


def test_rows_digest_is_sensitive_to_a_one_row_change():
    base = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    changed = [{"a": 1, "b": "x"}, {"a": 2, "b": "z"}]
    for order_free in (True, False):
        assert (T.compute_rows_digest(base, order_free=order_free)
               != T.compute_rows_digest(changed, order_free=order_free))


def test_is_order_free_reads_sort_keys_yaml_and_resolves_v2_aliases():
    """Pinned against the real `sort_keys.yaml`, not a fixture copy — a
    silently-added `ORDER BY` on a previously order-free template (or vice
    versa) is exactly the kind of drift `rows_digest`'s rule depends on
    catching."""
    assert T._is_order_free("BI11") is True         # order_by: []
    assert T._is_order_free("IS1") is True           # order_by: []
    assert T._is_order_free("BI3") is False          # declared order_by
    assert T._is_order_free("BI6.v2") is False       # alias of BI6, ordered
    assert T._is_order_free("NOT-A-REAL-PLAN") is True  # unknown -> order-free


def test_rows_digest_stable_across_two_runs_against_a_tmp_fixture_store(tmp_path):
    """A tmp fixture store with two small plans — one ordered (IS2), one
    order-free (BI11) — run twice each. `rows_digest` must agree run over
    run, and must actually be a sha256 hex digest."""
    store = _build_tmp_fixture_store(tmp_path)
    try:
        for plan_id in ("IS2", "BI11"):
            order_free = T._is_order_free(plan_id)
            env1, _ = _run_fixture_plan(store, plan_id)
            env2, _ = _run_fixture_plan(store, plan_id)
            _, rows1 = T.decode_rows(env1)
            _, rows2 = T.decode_rows(env2)
            d1 = T.compute_rows_digest(rows1, order_free=order_free)
            d2 = T.compute_rows_digest(rows2, order_free=order_free)
            assert d1 == d2, plan_id
            assert len(d1) == 64 and all(c in "0123456789abcdef" for c in d1)
    finally:
        store.close()


def test_rows_digest_against_a_tmp_fixture_store_changes_with_the_rows(tmp_path):
    """Same store, same plan (IS2, ordered) — mutating one decoded row (as a
    stand-in for a genuine result change) must change the digest, exercised
    against a real run's decoded rows rather than only hand-built dicts."""
    store = _build_tmp_fixture_store(tmp_path)
    try:
        envelope, _ = _run_fixture_plan(store, "IS2")
    finally:
        store.close()
    _, rows = T.decode_rows(envelope)
    assert rows, "IS2 against the tmp fixture returned no rows"
    baseline = T.compute_rows_digest(rows, order_free=False)

    mutated = [dict(r) for r in rows]
    first_key = next(iter(mutated[0]))
    mutated[0][first_key] = "mutated-sentinel-value"
    assert T.compute_rows_digest(mutated, order_free=False) != baseline


def test_emit_rows_output_is_directly_readable_by_ldbc_compare(tmp_path):
    """`--emit-rows`'s file shape (`write_rows_export`) is what
    `ldbc_compare.py --sort-keys` reads on both the TGMS and the reference
    side (module docstring). Comparing the export against itself must be a
    clean, complete agreement — proof the file loads and the schema/rows
    shape round-trips through `compare_plan`, not just through `json.loads`.
    """
    store = _build_tmp_fixture_store(tmp_path)
    try:
        envelope, params = _run_fixture_plan(store, "IS2")
    finally:
        store.close()
    path = T.write_rows_export(tmp_path, "IS2", envelope, params,
                               "fixture-test", "test-commit")
    doc = json.loads(path.read_text())

    sort_keys = C.lookup_sort_keys(C.load_sort_keys(T.SORT_KEYS_PATH), "IS2")
    verdict = C.compare_plan("IS2", doc, doc, None, sort_keys)

    assert verdict["both_sides_completed"] is True
    assert verdict["compared"] == len(doc["rows"]) > 0
    assert verdict["disagreeing"] == 0
    assert verdict["verdict"] == "agreeing"


# --------------------------------------------------------------------------
# 6. temporal parameters reach the driver as temporal values
# --------------------------------------------------------------------------

def test_iso_parameters_are_handed_to_the_driver_as_datetimes():
    """`params.json` stores a temporal parameter as its ISO-8601 spelling
    because it is JSON. Cypher must receive a *temporal value*: comparing a
    DATETIME property against a STRING does not raise in Neo4j 5, it yields
    null, so the predicate is never true and the query returns zero rows with
    no error anywhere. Reproduces LDBC's own
    `cast_parameter_to_driver_input` (bi/neo4j/queries.py)."""
    import datetime

    got = R.driverize_params({
        "date": "2010-02-12T00:00:00.000+00:00",
        "country": "India",
        "tag": "Bob_Geldof",
        "languages": ["es", "ta"],
        "lengthThreshold": 115,
        "personId": 8796093025922,
    })
    assert got["date"] == datetime.datetime(
        2010, 2, 12, tzinfo=datetime.timezone.utc)
    # everything that is not an ISO-8601 UTC timestamp passes through untouched
    assert got["country"] == "India"
    assert got["tag"] == "Bob_Geldof"
    assert got["languages"] == ["es", "ta"]
    assert got["lengthThreshold"] == 115
    assert got["personId"] == 8796093025922


def test_a_string_that_merely_looks_datelike_is_not_converted():
    """The match is on the full `us_to_iso` spelling, not on 'contains digits
    and dashes' — a tag or country name must never become a timestamp."""
    got = R.driverize_params({"tag": "2010-02-12", "other": "2010-02-12T00:00:00"})
    assert got["tag"] == "2010-02-12"
    assert got["other"] == "2010-02-12T00:00:00"


def test_run_query_sends_the_converted_parameters(monkeypatch):
    """The conversion is at the driver boundary, so it applies to every query
    the runner issues, not only to the ones a test remembers to convert."""
    import datetime

    seen: dict = {}

    class _Result:
        def keys(self):
            return []

        def __iter__(self):
            return iter(())

    class _Session:
        def run(self, text, params):
            seen.update(params)
            return _Result()

    R.run_query(_Session(), "MATCH (n) WHERE n.d > $date RETURN n",
                {"date": "2010-02-12T00:00:00.000+00:00", "country": "India"})
    assert seen["date"] == datetime.datetime(
        2010, 2, 12, tzinfo=datetime.timezone.utc)
    assert seen["country"] == "India"


# --------------------------------------------------------------------------
# 7. the column correspondence, and the reference column nobody projects
# --------------------------------------------------------------------------

def _docs(tgms_cols, tgms_row, ref_row):
    return ({"plan_id": "X", "params": {"a": 1},
             "schema": [[c, "str"] for c in tgms_cols],
             "columns": tgms_cols, "rows": [tgms_row]},
            {"plan_id": "X", "columns": list(ref_row), "rows": [ref_row]})


def test_the_column_map_renames_the_reference_side_before_matching():
    """Identical values under different column names must agree once the
    correspondence is supplied — that is the whole point of the table."""
    tgms_doc, ref_doc = _docs(
        ["forumId", "forumTitle"],
        {"forumId": 7, "forumTitle": "Wall"},
        {"forum.id": 7, "forum.title": "Wall"})
    contract = {"claim_full_contract": "CURRENT_ECQR_FRAGMENT"}

    without = C.compare_plan("X", tgms_doc, ref_doc, contract)
    assert without["verdict"] == "disagreeing", "premise: names differ"

    with_map = C.compare_plan(
        "X", tgms_doc, ref_doc, contract,
        column_map={"rule": "positional", "unmatched": [],
                    "map": {"forum.id": "forumId", "forum.title": "forumTitle"}})
    assert with_map["verdict"] == "agreeing"
    assert with_map["agreeing"] == 1
    assert with_map["column_map_rule"] == "positional"


def test_a_reference_column_no_tgir_column_projects_is_its_own_verdict():
    """The comparator only compares the columns the TGIR schema declares, so a
    reference column outside it used to be invisible and the template could be
    scored `agreeing` on a strict subset of the answer (IC12's `tagNames`)."""
    tgms_doc, ref_doc = _docs(
        ["personId", "replyCount"],
        {"personId": 3, "replyCount": 9},
        {"personId": 3, "tagNames": ["a"], "replyCount": 9})

    rec = C.compare_plan(
        "IC12", tgms_doc, ref_doc,
        {"claim_full_contract": "CURRENT_ECQR_FRAGMENT"},
        column_map={"rule": "name-partial", "unmatched": ["tagNames"],
                    "map": {"personId": "personId",
                            "replyCount": "replyCount"}})

    assert rec["verdict"] == "reference-column-not-projected"
    assert rec["reference_columns_not_projected"] == ["tagNames"]
    # the rows themselves still agree on the columns that DO correspond, and
    # that is reported rather than thrown away
    assert rec["agreeing"] == 1
    assert any(c["cause"] == "REFERENCE-COLUMN-NOT-PROJECTED"
               for c in rec["causes"])


def test_lookup_column_map_resolves_bi6_v2_to_the_bi6_template():
    """The correspondence is a property of the query, and BI6.v2 answers BI6."""
    table = {"BI6": {"rule": "positional", "map": {"a": "b"}, "unmatched": []}}
    assert C.lookup_column_map(table, "BI6.v2") == table["BI6"]
    assert C.lookup_column_map(table, "BI6") == table["BI6"]
    assert C.lookup_column_map(table, "IS3") is None
    assert C.lookup_column_map(None, "BI6") is None

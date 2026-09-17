"""`benchmarks/ldbc-ref-v1/column_map.yaml` is generated, not hand-written.

The two sides of the LDBC comparison name their columns differently — the
reference's names are the vendored Cypher's `RETURN … AS` aliases, TGIR's are
its plan's own output schema — and `ldbc_compare.py` looked both up by the
TGIR name, so every reference lookup returned `None` and every row faulted
while the values were identical. The correspondence that fixes that is a
derivation from frozen inputs, so it is regenerated and compared here rather
than trusted.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

MAP_PATH = ROOT / "benchmarks" / "ldbc-ref-v1" / "column_map.yaml"

_spec = importlib.util.spec_from_file_location(
    "ldbc_column_map", ROOT / "scripts" / "ldbc_column_map.py")
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)


@pytest.fixture(scope="module")
def table() -> dict:
    return yaml.safe_load(MAP_PATH.read_text())["plans"]


def test_the_checked_in_table_is_what_the_generator_produces(tmp_path):
    """The table is a derivation. If it can drift from its own generator it is
    a hand-written table wearing a generator's name."""
    out = tmp_path / "column_map.yaml"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ldbc_column_map.py"),
         "--column-kinds",
         str(ROOT / "benchmarks/ldbc-ref-v1/column_kinds.json"),
         "--out", str(out)],
        cwd=ROOT, check=True, capture_output=True, text=True)
    assert out.read_text() == MAP_PATH.read_text(), (
        "column_map.yaml is stale — re-run scripts/ldbc_column_map.py")


def test_ic8_maps_by_name_because_its_columns_are_reordered(table):
    """IC8 is the case that forbids a blanket positional rule: both sides carry
    the same six names, but the reference returns `commentId` before
    `commentCreationDate` and TGIR returns them the other way round. Pairing by
    position would compare an id against a timestamp."""
    ic8 = table["IC8"]
    assert ic8["rule"] == "name"
    assert ic8["unmatched"] == []
    assert ic8["map"]["commentId"] == "commentId"
    assert ic8["map"]["commentCreationDate"] == "commentCreationDate"
    # the premise: same names, different order
    assert set(ic8["tgir_columns"]) == set(ic8["reference_columns"])
    assert ic8["tgir_columns"] != ic8["reference_columns"]


def test_bi3_maps_by_position_because_no_name_is_shared(table):
    """BI3's reference returns Neo4j's default names for unaliased expressions
    (`forum.id`), which share nothing with the plan's (`forumId`)."""
    bi3 = table["BI3"]
    assert bi3["rule"] == "positional"
    assert bi3["unmatched"] == []
    assert bi3["map"] == {
        "forum.id": "forumId",
        "forum.title": "forumTitle",
        "forum.creationDate": "forumCreationDate",
        "person.id": "moderatorId",
        "messageCount": "messageCount",
    }


def test_positional_pairing_only_pairs_columns_of_the_same_kind(table):
    """The kind check is what makes positional pairing safe to apply at all."""
    prop_kind = M._property_kinds()
    import json
    kinds_table = json.loads(
        (ROOT / "benchmarks/ldbc-ref-v1/column_kinds.json").read_text())["plans"]
    for tpl, rec in table.items():
        if rec["rule"] != "positional":
            continue
        pid = M.PLAN_ARTIFACT[tpl]
        tschema = dict(M.tgir_columns(M.PLANS_DIR / f"{pid}.json"))
        kinds = kinds_table.get(pid, {})
        for ref_col, tgir_col in rec["map"].items():
            assert (M.ref_kind(ref_col, prop_kind)
                    == M.tgir_kind(tgir_col, tschema[tgir_col], kinds)), (
                f"{tpl}: {ref_col} -> {tgir_col} pairs across kinds")


def test_the_three_templates_whose_reference_projects_more_are_flagged(table):
    """A reference column no TGIR column maps to must be recorded, not
    silently dropped — the comparator only ever compares the columns the TGIR
    schema declares, so these were invisible and scored as agreement."""
    assert table["IC12"]["unmatched"] == ["tagNames"]
    assert table["BI4"]["unmatched"] == [
        "personFirstName", "personLastName", "personCreationDate"]
    assert table["IC5"]["unmatched"] == ["forumName"]
    for tpl, rec in table.items():
        if tpl not in ("IC12", "BI4", "IC5"):
            assert rec["unmatched"] == [], f"{tpl} unexpectedly unmatched"


def test_every_template_is_present_and_names_its_two_frozen_inputs(table):
    assert len(table) == 24
    for tpl, rec in table.items():
        assert rec["plan_artifact"].endswith(".json")
        assert rec["cypher"].endswith(".cypher")
        assert rec["rule"] in ("name", "positional", "name-partial")


def test_the_reference_columns_are_read_from_the_vendored_query(table):
    """Parsed from each `.cypher`'s own final RETURN, not from any run output.
    BI6's entry reads bi-6.cypher even though BI6.v2.json answers it."""
    assert table["BI6"]["cypher"] == "bi-6.cypher"
    assert table["BI6"]["plan_artifact"] == "BI6.v2.json"
    assert table["IS1"]["cypher"] == "interactive-short-1.cypher"

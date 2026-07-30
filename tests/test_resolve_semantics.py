"""Fixtures that distinguish the resolve_entities implementations (D-031).

Before D-031 the Rust kernel and the reference oracle disagreed in two ways
no existing fixture exercised: the kernel took an entity's canonical label
and name from the latest *matching* version rather than the latest believed
version overall, and the oracle's ``str()`` coercion let non-string names
match text the store never contained. Both suites passed because nothing
renamed an entity or gave one a numeric name.
"""

from __future__ import annotations

import pytest

import tgms
from tgms.temporal.algebra import call_operator, ensure_all_registered


@pytest.fixture()
def store(tmp_path):
    ensure_all_registered()
    st = tgms.open(tmp_path / "s")
    # Renamed entity: found by its old name, reported by its current one.
    st.assert_node("u-ren", "Person", {"name": "old-handle"}, vt_s=0, vt_e=100)
    st.assert_node("u-ren", "Person", {"name": "new-handle"}, vt_s=100, vt_e=200)
    # Numeric name: must not participate in matching (D-031).
    st.assert_node("u-num", "Sensor", {"name": 42}, vt_s=0, vt_e=100)
    # Null name: the oracle's former str() coercion matched "None" here.
    st.assert_node("u-null", "Thing", {"name": None}, vt_s=0, vt_e=100)
    yield st
    st.close()


def resolve(store, query):
    return call_operator(store.adapter, "resolve_entities", {"query": query})


def test_match_on_old_name_reports_current_state(store):
    """An entity found via a superseded name resolves to what it is *now*."""
    rows = resolve(store, "old-handle")["rows"]
    assert [r["uid"] for r in rows] == ["u-ren"]
    r = rows[0]
    assert r["match"] == 2
    # canonical state comes from the latest believed version, which does
    # not itself match the query
    assert r["name"] == "new-handle"
    assert r["label"] == "Person"


def test_numeric_name_does_not_match_its_digits(store):
    assert resolve(store, "42")["rows"] == []


def test_null_name_does_not_match_the_word_none(store):
    assert resolve(store, "none")["rows"] == []


def test_numeric_name_entity_still_resolvable_by_uid(store):
    rows = resolve(store, "u-num")["rows"]
    assert [(r["uid"], r["match"]) for r in rows] == [("u-num", 0)]
    # the output name keeps its JSON type even though it cannot match
    assert rows[0]["name"] == 42

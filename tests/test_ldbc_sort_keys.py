"""`scripts/ldbc_sort_keys.py` (lane D1-fix, OSDI'27 Claim C9) — the mechanical
`ORDER BY` -> `sort_keys.yaml` generator, and `ldbc_compare.py`'s
`--sort-keys` consumption of its output.

Three things are under test:

1. The generator's own resolution logic against three hand-checked
   templates chosen specifically because their `ORDER BY` key text does
   *not* equal the output column name verbatim (the case a naive
   "just copy the ORDER BY tokens" script would get wrong): one BI
   (`BI4`, `person.id` -> alias `personId`), one IC (`IC9`, `message.id`
   -> alias `commentOrPostId`), one IS (`IS3`, `toInteger(personId)`
   unwrapped to the alias `personId`) — each checked by hand against the
   vendored `.cypher` file's own text, not against the generator's output.
2. `benchmarks/ldbc-ref-v1/sort_keys.yaml` (the checked-in, generated file)
   lists all 24 campaign templates.
3. `ldbc_compare.py` actually uses a loaded `sort_keys.yaml`-shaped table —
   a synthetic two-row comparison with a boundary tie, driven end to end
   through `load_sort_keys`/`lookup_sort_keys`/`compare_plan`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ldbc_compare as C  # noqa: E402
import ldbc_sort_keys as S  # noqa: E402

SORT_KEYS_PATH = ROOT / "benchmarks" / "ldbc-ref-v1" / "sort_keys.yaml"
LDBC_ROOT = ROOT / "external_workloads" / "ldbc"


# ==========================================================================
# 1. three hand-checked templates
# ==========================================================================

def test_bi4_resolves_the_pre_alias_order_by_key_to_its_return_alias():
    """`bi-4.cypher`'s own text (read by hand): `RETURN person.id AS personId,
    ..., sum(messageCount) AS messageCount ORDER BY messageCount DESC,
    person.id ASC`. The second sort key is written against the bound
    variable `person.id`, not the projected name `personId` -- the exact
    case a script that just lexically diffed `ORDER BY` tokens against
    output columns would get wrong."""
    text = (LDBC_ROOT / "bi" / "neo4j" / "queries" / "bi-4.cypher").read_text()
    assert "person.id AS personId" in text
    assert "ORDER BY\n  messageCount DESC,\n  person.id ASC" in text

    parsed = S.parse_order_by(text)
    assert parsed["limit"] == 100
    assert parsed["order_by"] == [
        {"column": "messageCount", "direction": "DESC"},
        {"column": "personId", "direction": "ASC"},
    ]


def test_ic9_resolves_the_pre_alias_order_by_key_to_its_return_alias():
    """`interactive-complex-9.cypher` (read by hand): `RETURN ..., message.id
    AS commentOrPostId, ... ORDER BY commentOrPostCreationDate DESC,
    message.id ASC` -- same pattern as BI4, on the Interactive side."""
    text = (LDBC_ROOT / "interactive_v1" / "cypher" / "queries"
           / "interactive-complex-9.cypher").read_text()
    assert "message.id AS commentOrPostId" in text
    assert "message.id ASC" in text

    parsed = S.parse_order_by(text)
    assert parsed["limit"] == 20
    assert parsed["order_by"] == [
        {"column": "commentOrPostCreationDate", "direction": "DESC"},
        {"column": "commentOrPostId", "direction": "ASC"},
    ]


def test_is3_unwraps_the_tointeger_cast_to_its_aliased_column():
    """`interactive-short-3.cypher` (read by hand): `RETURN friend.id AS
    personId, ... ORDER BY friendshipCreationDate DESC, toInteger(personId)
    ASC`, no `LIMIT` at all. `toInteger(...)` is LDBC's own workaround for
    sorting a numeric id stored as a string -- the cast wraps an identifier
    that is *already* the RETURN alias, so unwrapping it needs no further
    pre-alias lookup."""
    text = (LDBC_ROOT / "interactive_v1" / "cypher" / "queries"
           / "interactive-short-3.cypher").read_text()
    assert "friend.id AS personId" in text
    assert "toInteger(personId) ASC" in text

    parsed = S.parse_order_by(text)
    assert parsed["limit"] is None
    assert parsed["order_by"] == [
        {"column": "friendshipCreationDate", "direction": "DESC"},
        {"column": "personId", "direction": "ASC"},
    ]


def test_a_key_never_projected_at_all_is_flagged_unmapped_not_guessed():
    """`interactive-complex-5.cypher`: `ORDER BY postCount DESC, forum.id
    ASC` but `RETURN forum.title AS forumName, postCount` never projects
    `forum.id` at all -- a real gap (the two systems cannot be compared on
    this boundary column from output rows alone), named rather than
    silently mapped to something that happens to parse."""
    text = (LDBC_ROOT / "interactive_v1" / "cypher" / "queries"
           / "interactive-complex-5.cypher").read_text()
    parsed = S.parse_order_by(text)
    assert parsed["order_by"][0] == {"column": "postCount", "direction": "DESC"}
    assert parsed["order_by"][1]["column"] == "forum.id"
    assert parsed["order_by"][1]["unmapped"] is True


def test_no_order_by_at_all_yields_an_empty_table():
    """The 5 `CURRENT_ECQR_FRAGMENT` templates plus BI11 (`RETURN count(*) AS
    count`, a single scalar row) have no `ORDER BY` to parse."""
    text = (LDBC_ROOT / "interactive_v1" / "cypher" / "queries"
           / "interactive-short-4.cypher").read_text()
    assert S.parse_order_by(text) == {"order_by": [], "limit": None}


# ==========================================================================
# 2. the checked-in sort_keys.yaml
# ==========================================================================

def test_sort_keys_yaml_is_checked_in_and_lists_all_24_campaign_templates():
    assert SORT_KEYS_PATH.is_file()
    doc = yaml.safe_load(SORT_KEYS_PATH.read_text())
    campaign = yaml.safe_load((ROOT / "benchmarks" / "ldbc-ref-v1"
                               / "campaign.yaml").read_text())
    ids = {t["id"] for t in campaign["templates"]}
    assert len(ids) == 24
    assert set(doc["templates"]) == ids


def test_sort_keys_yaml_matches_a_fresh_regeneration():
    """The checked-in file is not hand-edited drift from the generator --
    regenerating from the same vendored `.cypher` files reproduces it
    exactly (`templates` block; timestamps/headers aside, this generator
    writes none)."""
    campaign = yaml.safe_load((ROOT / "benchmarks" / "ldbc-ref-v1"
                               / "campaign.yaml").read_text())
    fresh = S.build_sort_keys(campaign)
    committed = yaml.safe_load(SORT_KEYS_PATH.read_text())
    assert fresh["templates"] == committed["templates"]


def test_ic5s_unmapped_flag_survives_into_the_checked_in_file():
    doc = yaml.safe_load(SORT_KEYS_PATH.read_text())
    ic5_keys = doc["templates"]["IC5"]["order_by"]
    assert any(k.get("unmapped") for k in ic5_keys)


# ==========================================================================
# 3. ldbc_compare.py actually uses it
# ==========================================================================

def _doc(schema, rows, **extra) -> dict[str, Any]:
    return {"schema": schema, "columns": [c[0] for c in schema], "rows": rows,
            "params": {"x": 1}, "result_digest": "d", **extra}


def test_load_sort_keys_extracts_just_the_column_names_in_order():
    loaded = C.load_sort_keys(SORT_KEYS_PATH)
    assert loaded["BI4"] == ["messageCount", "personId"]
    assert loaded["IS4"] == []          # no ORDER BY at all


def test_lookup_sort_keys_strips_the_v2_suffix_for_bi6():
    """`sort_keys.yaml` is keyed by the LDBC template id (`BI6`); the plan
    actually compared is `BI6.v2` (RUNBOOK.md §6) -- the sort key is a
    property of the vendored query, answered by either plan artifact."""
    loaded = C.load_sort_keys(SORT_KEYS_PATH)
    assert C.lookup_sort_keys(loaded, "BI6.v2") == loaded["BI6"]
    assert C.lookup_sort_keys(loaded, "BI6") == loaded["BI6"]
    assert C.lookup_sort_keys(loaded, "NOPE") is None
    assert C.lookup_sort_keys(None, "BI6") is None


def test_compare_all_uses_a_loaded_sort_keys_table_for_a_boundary_tie(tmp_path):
    """Synthetic two-row top-k comparison, driven through `compare_all`
    exactly as `ldbc_compare.py --sort-keys ...` would: both sides agree on
    row 1, and tie at the boundary on row 2's sort key (`score`) while
    disagreeing on `id` -- `TIE-AMBIGUOUS`, not a disagreement, because
    `--sort-keys` supplied `["score"]` for this plan id."""
    schema = [["id", "uid"], ["score", "int"]]
    tgms_dir, ref_dir = tmp_path / "tgms", tmp_path / "ref"
    tgms_dir.mkdir()
    ref_dir.mkdir()
    tgms_doc = _doc(schema, [{"id": 1, "score": 10}, {"id": 3, "score": 5}])
    ref_doc = _doc(schema, [{"id": 1, "score": 10}, {"id": 4, "score": 5}])
    (tgms_dir / "tgms-T9.json").write_text(__import__("json").dumps(tgms_doc))
    (ref_dir / "ref-T9.json").write_text(__import__("json").dumps(ref_doc))

    contracts = {"T9": {"claim_full_contract": "REQUIRES_TOP_K"}}
    sort_keys_yaml = tmp_path / "sort_keys.yaml"
    sort_keys_yaml.write_text(yaml.safe_dump({
        "templates": {"T9": {"cypher": "x.cypher",
                             "order_by": [{"column": "score", "direction": "DESC"}],
                             "limit": 2}},
    }))
    loaded = C.load_sort_keys(sort_keys_yaml)

    without = C.compare_all(tgms_dir, ref_dir, contracts, ["T9"])
    assert without["verdicts"][0]["verdict"] == "disagreeing"
    assert all(c["cause"] == "UNTRIAGED" for c in without["verdicts"][0]["causes"])

    with_keys = C.compare_all(tgms_dir, ref_dir, contracts, ["T9"],
                              sort_keys=loaded)
    v = with_keys["verdicts"][0]
    assert v["verdict"] == "tie-ambiguous"
    assert v["disagreeing"] == 0
    assert v["tie_ambiguous"] == 2


@pytest.mark.parametrize("pid", ["BI3", "BI4", "BI6", "BI7", "BI9", "BI10",
                                 "BI11", "BI12", "BI17", "BI18", "IC2", "IC5",
                                 "IC6", "IC8", "IC9", "IC11", "IC12", "IS1",
                                 "IS2", "IS3", "IS4", "IS5", "IS6", "IS7"])
def test_every_campaign_template_has_a_sort_keys_row(pid):
    loaded = C.load_sort_keys(SORT_KEYS_PATH)
    assert pid in loaded

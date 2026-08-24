"""Binding the 21 frozen plans to LDBC's own SF1 parameters (E13).

Each test names the frozen rule it defends
(`docs/design/PAPER_A_EVIDENCE_FREEZE.md` §A4/§A9 + §E addenda 2-3). The binder
is the second place a judgement call could leak into a claim — the first was the
loader — so the same discipline applies: a rule that is not tested is a rule
that is only asserted.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from tgms.data.snb_loader import snb_uid

_spec = importlib.util.spec_from_file_location(
    "ldbc_snb_params",
    Path(__file__).resolve().parents[1] / "scripts" / "ldbc_snb_params.py")
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)


@pytest.fixture
def params_root(tmp_path: Path) -> Path:
    bi = (tmp_path / "bi" / "ldbc-snb-bi-parameters-sf1-to-sf30000"
          / "parameters-sf1")
    bi.mkdir(parents=True)
    (bi / "bi-3.csv").write_text("tagClass:STRING|country:STRING\nPresident|India\nOther|Peru\n")
    (bi / "bi-4.csv").write_text("date:DATE\n2010-02-12\n2011-01-01\n")
    (bi / "bi-6.csv").write_text("tag:STRING\nBob_Geldof\n")
    (bi / "bi-7.csv").write_text("tag:STRING\nSlovenia\n")
    (bi / "bi-9.csv").write_text("startDate:DATE|endDate:DATE\n2012-08-30|2012-12-10\n")
    (bi / "bi-10a.csv").write_text(
        "personId:ID|country:STRING|tagClass:STRING|minPathDistance:INT|maxPathDistance:INT\n"
        "6597069770479|Italy|Thing|3|4\n")
    (bi / "bi-11.csv").write_text(
        "country:STRING|startDate:DATE|endDate:DATE\nIndia|2012-10-04|2013-01-16\n")
    (bi / "bi-12.csv").write_text(
        "startDate:DATE|lengthThreshold:INT|languages:STRING[]\n2012-06-05|115|es;ta;pt\n")
    (bi / "bi-17.csv").write_text("tag:STRING|delta:INT\nCosmic_Egg|12\n")
    (bi / "bi-18.csv").write_text("tag:STRING\nFyodor_Dostoyevsky\n")

    iv = tmp_path / "iv"
    iv.mkdir()
    rows = [
        '{"forumId":1,"personId":2,"joinDate":3}|"-1"',          # an update, first
        '{"personIdQ2":17592186052613,"maxDate":1354060800000,"limit":20}|[]',
        '{"personIdQ5":17592186055119,"minDate":1348704000000,"limit":20}|[]',
        '{"personIdQ6":30786325583618,"tagName":"Angola","limit":10}|[]',
        '{"personIdQ8":24189255818757,"limit":20}|[]',
        '{"personIdQ9":13194139542834,"maxDate":1324080000000,"limit":20}|[]',
        '{"personIdQ11":30786325583618,"countryName":"Laos","workFromYear":2010,"limit":10}|[]',
        '{"personIdQ12":17592186052613,"tagClassName":"BasketballPlayer","limit":20}|[]',
        '{"personIdSQ2":32985348839299,"limit":10}|[]',
        '{"personIdSQ3":32985348839299}|[]',
        '{"messageForumId":2199024038763}|{}',
        '{"messageRepliesId":2199024038763}|[]',
        '{"personIdQ2":999,"maxDate":1,"limit":20}|[]',          # a second Q2, later
    ]
    (iv / "validation_params-sf1.csv").write_text("\n".join(rows) + "\n")
    return tmp_path


# --------------------------------------------------------------------------
# §A4 — identity
# --------------------------------------------------------------------------

def test_id_parameters_go_through_the_same_uid_transform_as_the_loader(params_root):
    """A parameter compared against an encoded uid without the encoding matches
    nothing, and matches nothing *silently*. One definition site, both sides."""
    assert P.bind("BI10", params_root)["params"]["personId"] == \
        snb_uid("Person", 6597069770479)
    assert P.bind("IS2", params_root)["params"]["personId"] == \
        snb_uid("Person", 32985348839299)
    assert P.bind("IS6", params_root)["params"]["messageId"] == \
        snb_uid("Message", 2199024038763)


# --------------------------------------------------------------------------
# units — the silent-failure class
# --------------------------------------------------------------------------

def test_validation_file_dates_are_milliseconds_and_become_microseconds(params_root):
    """The store keeps valid time in microseconds (SF1 `vt_max` is 1.354e15).
    The validation file carries raw epoch **millis**, so an unconverted bound
    value is 1,000x too small and every `creationDate < $maxDate` test simply
    passes — a plausible-looking superset, silently."""
    assert P.bind("IC2", params_root)["params"]["maxDate"] == 1354060800000 * 1000
    assert P.bind("IC5", params_root)["params"]["minDate"] == 1348704000000 * 1000
    assert P.bind("IC9", params_root)["params"]["maxDate"] == 1324080000000 * 1000


def test_a_calendar_year_is_not_a_clock(params_root):
    """§A3 M4: `workFromYear` is a scalar predicate, not valid time. Scaling it
    would be the same error in the other direction."""
    assert P.bind("IC11", params_root)["params"]["workFromYear"] == 2010


def test_bi_dates_are_typed_by_their_own_header(params_root):
    """The BI files declare `:DATE`, so the type is the contract."""
    assert P.bind("BI4", params_root)["params"]["date"] == P.date_to_us("2010-02-12")
    b9 = P.bind("BI9", params_root)["params"]
    assert b9["startDate"] == P.date_to_us("2012-08-30")
    assert b9["endDate"] == P.date_to_us("2012-12-10")


# --------------------------------------------------------------------------
# §A9 — derived parameters and the selection rule
# --------------------------------------------------------------------------

def test_the_two_derived_parameters_are_derived_not_bound(params_root):
    """LDBC supplies their inputs, not them (R5 bind-time derivations)."""
    assert P.bind("BI10", params_root)["params"]["nearMaxHops"] == 3 - 1
    assert P.bind("BI17", params_root)["params"]["deltaMicros"] == 12 * P.HOUR_US


def test_the_selection_rule_takes_the_first_tuple_in_ldbc_file_order(params_root):
    """No inspection of result size, no re-draw. The fixture carries a second
    Q2 row later in the file; taking it would be choosing."""
    assert P.bind("IC2", params_root)["params"]["personId"] == \
        snb_uid("Person", 17592186052613)
    assert P.bind("BI3", params_root)["params"] == {"tagClass": "President",
                                                    "country": "India"}


def test_every_frozen_plan_has_a_parameter_source(params_root):
    assert len(P.LDBC_PLANS) == 21
    for pid in P.LDBC_PLANS:
        b = P.bind(pid, params_root)
        assert b["params"], pid
        assert not b["unbound_frozen_params"], (pid, b["unbound_frozen_params"])


# --------------------------------------------------------------------------
# §E addendum 3 — the n-way OR expansion
# --------------------------------------------------------------------------

def test_a_string_list_parameter_expands_to_one_disjunct_per_value(params_root):
    """The artifact carries `$language1`/`$language2`; LDBC supplies three
    languages. The rule expands the OR; the artifact is never edited."""
    b = P.bind("BI12", params_root)
    assert b["or_expansion"] == {"param": "languages",
                                 "values": ["es", "ta", "pt"], "disjuncts": 3}
    body = json.dumps(b["root"])
    for lang in ("es", "ta", "pt"):
        assert f'"{lang}"' in body
    assert "$language1" not in body and "$language2" not in body
    assert "language1" not in b["params"]


def test_the_expansion_keeps_one_left_hand_side_and_nests_right(params_root):
    """`[a,b,c]` -> `a v (b v c)`: the two-value artifact's shape extended, not
    re-associated, and every disjunct tests the same expression."""
    lhs = {"prop": ["post.props", "language"]}
    node = {"bool": "or",
            "l": {"cmp": "=", "l": lhs, "r": {"lit": "$language1"}},
            "r": {"cmp": "=", "l": lhs, "r": {"lit": "$language2"}}}
    got = P.expand_or_list(node, "language", ["a", "b", "c"])
    assert got == {"bool": "or",
                   "l": {"cmp": "=", "l": lhs, "r": {"lit": "a"}},
                   "r": {"bool": "or",
                         "l": {"cmp": "=", "l": lhs, "r": {"lit": "b"}},
                         "r": {"cmp": "=", "l": lhs, "r": {"lit": "c"}}}}


def test_a_single_value_collapses_to_one_comparison(params_root):
    lhs = {"prop": ["post.props", "language"]}
    node = {"bool": "or",
            "l": {"cmp": "=", "l": lhs, "r": {"lit": "$language1"}},
            "r": {"cmp": "=", "l": lhs, "r": {"lit": "$language2"}}}
    assert P.expand_or_list(node, "language", ["only"]) == \
        {"cmp": "=", "l": lhs, "r": {"lit": "only"}}


def test_the_expansion_leaves_unrelated_or_nodes_alone(params_root):
    """It keys on the `$<prefix>` literals, so an OR over anything else is not
    a candidate — a rewrite that caught every OR would silently reshape plans."""
    other = {"bool": "or",
             "l": {"cmp": "=", "l": {"col": "a"}, "r": {"lit": "$country"}},
             "r": {"cmp": "=", "l": {"col": "a"}, "r": {"lit": "$tagClass"}}}
    assert P.expand_or_list(other, "language", ["x", "y"]) == other


# --------------------------------------------------------------------------
# phantom anchors — the defect that cost a whole campaign
# --------------------------------------------------------------------------

class _Adapter:
    """Stands in for a store: knows a fixed set of uids."""

    def __init__(self, known):
        self.known = set(known)

    def dense_ids(self, uids):
        for u in uids:
            if str(u) not in self.known:
                raise LookupError(f"no entity {u}")
        return [0] * len(uids)


def test_a_bound_id_that_names_no_entity_fails_loudly(params_root):
    """`NodeScan(uids=[...])` on an unknown uid is not an error — it is an empty
    domain, so the plan degrades to a full scan that returns nothing, slowly.
    The first SF1 campaign spent hours that way because the Interactive
    parameters came from a different LDBC dataset. A phantom anchor has to be a
    bind failure, naming the id."""
    with pytest.raises(P.PhantomAnchor) as e:
        P.bind("BI10", params_root, adapter=_Adapter([]))
    assert snb_uid("Person", 6597069770479) in str(e.value)
    assert "BI10" in str(e.value)


def test_a_bound_id_that_exists_binds_normally(params_root):
    ok = _Adapter([snb_uid("Person", 6597069770479)])
    b = P.bind("BI10", params_root, adapter=ok)
    assert b["params"]["personId"] == snb_uid("Person", 6597069770479)
    assert b["phantom_anchors"] == []


def test_plans_without_id_parameters_are_unaffected(params_root):
    """BI4 takes only a date; there is nothing to probe and nothing to fail."""
    b = P.bind("BI4", params_root, adapter=_Adapter([]))
    assert b["params"]["date"] == P.date_to_us("2010-02-12")


def test_no_adapter_means_no_probe(params_root):
    """Binding without a store still works — the probe is opt-in, so the binder
    stays usable for inspection and for tests that have no store."""
    assert P.bind("BI10", params_root)["params"]["personId"]


# --------------------------------------------------------------------------
# the artifacts themselves are not edited
# --------------------------------------------------------------------------

def test_binding_never_touches_the_checked_in_artifact(params_root):
    before = (P.PLANS_DIR / "BI12.json").read_bytes()
    P.bind("BI12", params_root)
    assert (P.PLANS_DIR / "BI12.json").read_bytes() == before


def test_sigma_is_carried_through_unchanged(params_root):
    """§A3 M11. Narrowing Sigma is the one lever that would turn refusals into
    admissions, so it is closed by rule rather than by care."""
    for pid in P.LDBC_PLANS:
        frozen = json.loads((P.PLANS_DIR / f"{pid}.json").read_text())
        assert P.bind(pid, params_root)["sigma"] == frozen.get("sigma"), pid

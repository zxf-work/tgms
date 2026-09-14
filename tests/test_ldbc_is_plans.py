"""Lane D2 — the three LDBC Interactive Short templates that never had a plan,
and the BI6 triage.

IS1/IS4/IS5 were expressible under the *legacy* 15-operator algebra, so B1 never
forecast them as TGIR rows and `benchmarks/tgir-v1/plans/` never carried an
artifact for them — even though `docs/eval/CAPABILITY_MATRIX.md` and
`tests/test_ldbc_compare.py::test_the_24_expressible_split_matches_the_design_memo`
have always counted all three inside the 24 expressible templates. This file is
the evidence for the artifacts that close that gap: each one loads, validates
statically, executes on the 22-entity fixture, and returns the **declared**
schema with rows derived by hand from `scripts/build_ldbc_fixture.py`'s own
data — every expected value below names the fixture entity it comes from.

The BI6 half is the triage of the SF1 `ERRORED` outcome
(`benchmarks/results-v1/ldbc-sf1-campaign.json`: "null join key 'likerId' on the
left side at 535 row(s)"). The verdict is **plan defect, not TGIR gap**: §2.8's
`left_outer` already expresses `OPTIONAL MATCH` and `BI6.json` already used it —
what it also did was feed one outer join's null-filled output into the *key*
position of the next, which §2.8 makes an error rather than a non-match. The
fixture reproduces that at 1 row, and `BI6.v2.json` answers where v1 raises
while agreeing with v1 wherever v1 runs. `docs/design/BI6_TRIAGE_2026-09-14.md`
is the memo.

Nothing here is a scale result: the fixture is LDBC-*shaped*, not LDBC data.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from tgms.core.errors import InvalidArgError
from tgms.temporal.algebra import ensure_all_registered
from tgms.tgir.execute import run_plan
from tgms.tgir.loader import load

ROOT = Path(__file__).resolve().parents[1]
PLANS = ROOT / "benchmarks/tgir-v1/plans"

#: `scripts/build_ldbc_fixture.py`'s own clock constants. Repeated rather than
#: imported *as the expectation*, so a change to the builder's epoch shows up as
#: a failing assertion instead of moving both sides together.
BASE = 1_400_000_000_000_000
DAY = 86_400_000_000

_spec = importlib.util.spec_from_file_location(
    "build_ldbc_fixture", ROOT / "scripts" / "build_ldbc_fixture.py")
BUILDER = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(BUILDER)


@pytest.fixture(scope="module")
def fixture_store(tmp_path_factory):
    """The 22-entity LDBC-shaped store, rebuilt from the builder.

    Built in a tmp dir rather than read from `stores/ldbc-fixture`: that
    directory is gitignored, so a checkout that has never run the builder would
    otherwise silently skip the only rows this file is about.
    """
    import tgms

    ensure_all_registered()
    out = tmp_path_factory.mktemp("ldbc") / "fixture"
    BUILDER.build(out).close()
    store = tgms.open(out, read_only=True)
    yield store
    store.close()


def artifact(plan_id: str) -> dict[str, Any]:
    return json.loads((PLANS / f"{plan_id}.json").read_text())


def _substitute(document: Any, params: dict[str, Any]) -> Any:
    """The same `$name` rule `tgir_run.py` and `tgir_validate.py` use."""
    if isinstance(document, str) and document.startswith("$"):
        return params.get(document[1:], document)
    if isinstance(document, dict):
        return {k: _substitute(v, params) for k, v in document.items()}
    if isinstance(document, list):
        return [_substitute(v, params) for v in document]
    return document


def compile_plan(plan_id: str, **overrides: Any):
    """Load + static-validate. `node.py`'s constructors *are* the static check
    (§4.3: schema well-formedness, reference resolution, join key types), so a
    plan that loads has validated."""
    document = artifact(plan_id)
    params = dict(document.get("params", {})) | overrides
    return load(_substitute(document, params))


def execute(store, plan_id: str, **overrides: Any) -> dict[str, Any]:
    return run_plan(compile_plan(plan_id, **overrides), store.adapter,
                    tt_source=store, plan_id=plan_id)


# ---------------------------------------------------------------------------
# the declared output schema is the template's RETURN list
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("plan_id", ["IS1", "IS4", "IS5", "BI6.v2"])
def test_the_declared_schema_is_what_the_plan_actually_produces(plan_id):
    """Each artifact declares its output schema in `provenance`, copied from the
    reference template's RETURN list. A declared schema nobody checks is a
    comment; this makes it a contract — including column *order*, which
    `docs/eval_semantics.md` §4 makes part of the answer."""
    document = artifact(plan_id)
    declared = document["provenance"]["declared_output_schema"]
    assert list(compile_plan(plan_id).out_schema.names) == declared


@pytest.mark.parametrize("plan_id", ["IS1", "IS4", "IS5", "BI6.v2"])
def test_the_artifact_cites_the_vendored_reference_cypher(plan_id):
    """A plan whose provenance does not name the file it was compiled from is
    an assertion, not evidence."""
    provenance = artifact(plan_id)["provenance"]
    assert provenance["reference_cypher"].startswith("external_workloads/ldbc/")
    assert provenance["reference_cypher"].endswith(".cypher")
    assert provenance["licence"].startswith("LDBC material used under CC-BY 4.0")


# ---------------------------------------------------------------------------
# IS1 — profile of a person
# ---------------------------------------------------------------------------

def test_is1_returns_the_profile_of_fixture_person_1(fixture_store):
    """`interactive-short-1.cypher`, anchored on the artifact's own frozen
    param `personId = "1"`.

    Hand-derived from `build_ldbc_fixture.py`: Person `"1"` is the `i = 1`
    person, so `firstName`/`lastName` are `First1`/`Last1`, `gender` is
    `female` (`i % 2`), `birthday` is `BASE - 1*365*DAY` and `creationDate` is
    `BASE + 1*DAY`. `cityId` is `"601"` — the builder writes exactly one
    `IS_LOCATED_IN` per person, all to City `"601"` (`Cityville`) — and it is
    the City's **uid**, not `city.props.id`.
    """
    envelope = execute(fixture_store, "IS1")
    assert envelope["rows_total"] == 1
    assert envelope["tgir"]["completeness"] == "complete"
    assert envelope["rows"] == [{
        "firstName": "First1", "lastName": "Last1",
        "birthday": BASE - 365 * DAY,
        "locationIP": "10.0.0.1", "browserUsed": "chrome",
        "cityId": "601", "gender": "female",
        "creationDate": BASE + 1 * DAY,
    }]


def test_is1_creation_date_is_the_property_not_the_versions_valid_time(fixture_store):
    """The legacy `entity_history` compilation of IS1 read `creationDate` off
    the version's `vt_s`. The fixture writes every node with `vt_s = 0` and a
    `creationDate` property in the 1.4e15 range, so the two are not
    interchangeable and a plan that substituted one would be caught here."""
    row = execute(fixture_store, "IS1")["rows"][0]
    assert row["creationDate"] == BASE + 1 * DAY != 0


def test_is1_on_a_person_the_store_does_not_have_is_empty_not_an_error(fixture_store):
    """§2.1: `NodeScan(uids=[...])` on an unknown uid is an empty domain. That
    is why `ldbc_snb_params.verify_anchors` exists — the plan is not wrong, it
    is answering about nothing, and only the binder can say so."""
    assert execute(fixture_store, "IS1", personId="9999")["rows_total"] == 0


# ---------------------------------------------------------------------------
# IS4 — content of a message
# ---------------------------------------------------------------------------

def test_is4_on_a_post_takes_the_content_arm_of_the_coalesce(fixture_store):
    """`interactive-short-4.cypher` on Post `"101"` (the artifact's frozen
    param). The builder's `i = 1` Post has `content = "post 1 about things"`
    and `creationDate = BASE + (10 + 1)*DAY`."""
    envelope = execute(fixture_store, "IS4")
    assert envelope["rows"] == [{
        "messageCreationDate": BASE + 11 * DAY,
        "messageContent": "post 1 about things",
    }]


def test_is4_on_a_comment_takes_the_content_arm_with_no_imagefile_key(fixture_store):
    """Comment `"201"` is the `i = 1` comment: `creationDate = BASE + 21*DAY`,
    `content = "comment 1"`, and **no `imageFile` key at all** — so the
    coalesce's second arm is null and the first must carry the row. This is the
    half of `Message` the `[Post, Comment]` labels union exists for (§8.17):
    one plan, one scan, both labels, no hierarchy."""
    envelope = execute(fixture_store, "IS4", messageId="201")
    assert envelope["rows"] == [{
        "messageCreationDate": BASE + 21 * DAY,
        "messageContent": "comment 1",
    }]


def test_is4_scans_both_message_labels_from_one_artifact(fixture_store):
    """The labels union is in the artifact, not supplied per call."""
    scan = compile_plan("IS4").inputs[0]
    assert scan.op == "NodeScan"
    assert set(scan.labels) == {"Post", "Comment"}


# ---------------------------------------------------------------------------
# IS5 — creator of a message
# ---------------------------------------------------------------------------

def test_is5_returns_the_creator_of_a_post(fixture_store):
    """`interactive-short-5.cypher` on Post `"101"`, whose single
    `HAS_CREATOR` edge points at Person `"1"` (`First1 Last1`)."""
    assert execute(fixture_store, "IS5")["rows"] == [
        {"personId": "1", "firstName": "First1", "lastName": "Last1"}]


def test_is5_returns_the_creator_of_a_comment(fixture_store):
    """Comment `"204"` is created by Person `"4"` — the builder's
    `("204", "4")` HAS_CREATOR row, and the one comment whose author is not
    also a message author under the Tag1 topic."""
    assert execute(fixture_store, "IS5", messageId="204")["rows"] == [
        {"personId": "4", "firstName": "First4", "lastName": "Last4"}]


def test_is5_needs_one_scan_where_the_legacy_algebra_needed_two(fixture_store):
    """The row's whole content: `entity_history + filter + entity_history` with
    a `$ref` threading the creator uid collapses to one scan and one k=1
    `Expand`, because §2.3's expansion already binds the neighbour's full node
    schema (§4.2)."""
    plan = compile_plan("IS5")
    scans = []

    def walk(node):
        if node.op == "NodeScan":
            scans.append(node)
        for child in node.inputs:
            walk(child)

    walk(plan)
    assert len(scans) == 1


# ---------------------------------------------------------------------------
# BI6 — the triage: a plan defect, reproduced and fixed by a new artifact
# ---------------------------------------------------------------------------
#
# Fixture topology behind the two tag anchors (`build_ldbc_fixture.py`):
#
#   HAS_TAG   101->401, 102->401, 103->402, 201->402      (401 = "Tag1", 402 = "Tag2")
#   HAS_CREATOR  101->1, 102->2, 103->3, 201->2, 202->3, 203->1, 204->4
#   LIKES     2->101, 3->101, 1->102, 4->103
#
# Tag1: message1 in {101, 102}, person1 in {1, 2}; every person1 has a liker,
#       so v1's second join never sees a null key and v1 runs.
# Tag2: message1 in {103, 201}, person1 in {3, 2}; Comment 201 has **no**
#       liker, so person1 = 2 arrives at the second join with likerId = null.

def test_bi6_v1_raises_the_exact_sf1_error_on_a_message_with_no_likers(fixture_store):
    """The SF1 `ERRORED` record, reproduced on 22 entities. Same operator, same
    key, same rule — SF1 saw it at 535 rows because SF1 has 535 such person1
    rows, not because scale changed anything."""
    with pytest.raises(InvalidArgError) as raised:
        execute(fixture_store, "BI6", tagName="Tag2")
    assert "null join key 'likerId' on the left side" in str(raised.value)
    assert "§2.8" in str(raised.value)


def test_bi6_v1_null_key_comes_from_the_first_outer_joins_own_fill(fixture_store):
    """The defect named precisely: the column v1 uses as the **left** key of its
    second join is a column its **first** join declares nullable, because
    `Join{left_outer}.out_schema` calls `.nullable()` on the right side. §2.8 —
    "Join keys must be non-null; a null key is an error, not a non-match" — then
    makes the fill an error by construction, not by accident of the data."""
    outer = compile_plan("BI6").inputs[0].inputs[0].inputs[0]
    assert outer.op == "Join" and outer.join_type == "left_outer"
    assert outer.on == (("likerId", "likedPersonId"),)
    inner_left = outer.left
    assert inner_left.op == "Join" and inner_left.join_type == "left_outer"
    assert inner_left.out_schema.tau_of("likerId").nullable, \
        "the left key of the upper join is the lower join's null-filled column"


def test_bi6_v2_answers_where_v1_raises_and_keeps_the_zero_bucket(fixture_store):
    """Hand-derived for `Tag2`. person1 = 3 (author of Post 103): its liker is
    Person 4, whose only message is Comment 204, which nothing LIKES -> score 0.
    person1 = 2 (author of Comment 201): no liker at all -> score 0. Both rows
    must survive: `count(DISTINCT like)` over an all-null group is 0 (§2.10),
    and it is the person1 rows themselves the outer join has to preserve — a
    plain inner join would delete this entire answer."""
    envelope = execute(fixture_store, "BI6.v2", tagName="Tag2")
    assert envelope["rows"] == [{"personId": "2", "authorityScore": 0},
                                {"personId": "3", "authorityScore": 0}]


def test_bi6_v2_agrees_with_v1_wherever_v1_runs(fixture_store):
    """`Tag1`, where no person1 is liker-less and v1 therefore executes. v2 is a
    re-association of the same query, so it has to return v1's answer
    row-for-row and in the same order — otherwise it is a different plan, not a
    repair.

    Hand-derived: person1 = 1 (Post 101, liked by 2 and 3) scores the distinct
    LIKES edges into messages authored by 2 or 3 — `1->102` and `4->103` -> 2.
    person1 = 2 (Post 102, liked by 1) scores the LIKES edges into messages
    authored by 1 — `2->101` and `3->101` -> 2.
    """
    v1 = execute(fixture_store, "BI6", tagName="Tag1")
    v2 = execute(fixture_store, "BI6.v2", tagName="Tag1")
    assert v1["rows"] == [{"personId": "1", "authorityScore": 2},
                          {"personId": "2", "authorityScore": 2}]
    assert v2["rows"] == v1["rows"]


def test_bi6_v2_has_no_nullable_join_key_anywhere(fixture_store):
    """The structural property that makes the repair a repair rather than a
    workaround that happens to pass on this fixture: **every** join key column
    in v2 is non-nullable in its own input's schema, so §2.8's rule cannot fire
    on any data."""
    offenders = []

    def walk(node):
        if node.op == "Join":
            for lcol, rcol in node.on:
                if node.left.out_schema.tau_of(lcol).nullable:
                    offenders.append(("left", lcol))
                if node.right.out_schema.tau_of(rcol).nullable:
                    offenders.append(("right", rcol))
        for child in node.inputs:
            walk(child)

    walk(compile_plan("BI6.v2"))
    assert offenders == []


def test_bi6_v2_keeps_the_top_k_completeness_and_the_limit(fixture_store):
    """The repair must not quietly change what the answer claims about itself.
    `Limit(100)` stays at the root, so the result is `top-k` exactly as v1's
    was — not `complete`, which would be a stronger claim than the plan earns.
    """
    root = compile_plan("BI6.v2")
    assert root.op == "Limit" and root.n == 100
    for tag in ("Tag1", "Tag2"):
        assert execute(fixture_store, "BI6.v2",
                       tagName=tag)["tgir"]["completeness"] == "top-k"


def test_bi6_v2_supersedes_v1_without_editing_it():
    """Append-only: the v1 artifact is evidence of what the SF1 campaign ran and
    must stay byte-for-byte what it was."""
    assert artifact("BI6.v2")["row"]["supersedes"] == "BI6"
    assert artifact("BI6")["plan_id"] == "BI6"
    assert "supersedes" not in artifact("BI6")["row"]

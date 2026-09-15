"""`benchmarks/ldbc-ref-v1` — the frozen LDBC reference-correctness campaign
(lane D1-prep/D1-fix, OSDI'27 Claim C9). tmp_path/in-memory only, seconds:
this file never opens a store and never touches a network. As of lane D1-fix
(2026-09-14) the vendored LDBC Cypher trees (`external_workloads/ldbc/bi/neo4j/`,
`external_workloads/ldbc/interactive_v1/cypher/`) are on `main`
(`external_workloads/ldbc/README.md`), so a handful of tests below exercise
`ldbc_reference_run.run_all()` against the real vendored `.cypher` files with
a fake, no-network `neo4j` session injected — still no driver install, no
Neo4j instance, no store. It checks the frozen campaign's own contract: the
yaml parses, its 24 template ids resolve to a plan file on disk and to the
vendored-cypher filename the reference runner's own naming convention would
produce, the parameter-binding mechanism knows every one of those ids, and
the documented result-manifest layout (RUNBOOK.md §9) validates against
`benchmarks/schema/result_manifest.schema.json`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_PATH = ROOT / "benchmarks" / "ldbc-ref-v1" / "campaign.yaml"
RUNBOOK_PATH = ROOT / "benchmarks" / "ldbc-ref-v1" / "RUNBOOK.md"
PLANS_DIR = ROOT / "benchmarks" / "tgir-v1" / "plans"
SCHEMA_PATH = ROOT / "benchmarks" / "schema" / "result_manifest.schema.json"
CONTRACTS_PATH = ROOT / "tests" / "fixtures" / "ldbc_ref" / "contracts.json"

BI_CYPHER_DIR = ROOT / "external_workloads" / "ldbc" / "bi" / "neo4j" / "queries"
IV_CYPHER_DIR = (ROOT / "external_workloads" / "ldbc" / "interactive_v1"
                / "cypher" / "queries")

SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ldbc_reference_run as R  # noqa: E402
import ldbc_snb_params as P  # noqa: E402


@pytest.fixture(scope="module")
def campaign() -> dict:
    return yaml.safe_load(CAMPAIGN_PATH.read_text())


# --------------------------------------------------------------------------- #
# the yaml parses and carries exactly the 24 expressible templates          #
# --------------------------------------------------------------------------- #

def test_campaign_yaml_parses_and_is_frozen(campaign):
    assert campaign["freeze_id"] == "ldbc-ref-v1-2026-09-14"
    assert "templates" in campaign
    assert "predictions" in campaign
    assert "falsifiers" in campaign
    assert "gates" in campaign


def test_runbook_exists_alongside_the_campaign_file():
    assert RUNBOOK_PATH.is_file()
    text = RUNBOOK_PATH.read_text()
    # the runbook must name the same freeze so a reader can't drift them apart
    assert "campaign.yaml" in text


def test_templates_block_has_exactly_24_entries_with_unique_ids(campaign):
    templates = campaign["templates"]
    assert len(templates) == 24
    ids = [t["id"] for t in templates]
    assert len(set(ids)) == 24


def test_contract_split_matches_the_design_memo_5_3_16(campaign):
    """§0's "5 ECQR / 3 ORDERED / 16 TOP_K" split, recomputed from the
    campaign file rather than trusted from its own prose."""
    by_contract: dict[str, int] = {}
    for t in campaign["templates"]:
        by_contract[t["contract"]] = by_contract.get(t["contract"], 0) + 1
    assert by_contract == {
        "CURRENT_ECQR_FRAGMENT": 5,
        "REQUIRES_ORDERED_RESULT": 3,
        "REQUIRES_TOP_K": 16,
    }


def test_contracts_agree_with_the_committed_ldbc_contract_fixture(campaign):
    """Cross-check against `tests/fixtures/ldbc_ref/contracts.json`, the
    41-row provenance copy `ldbc_compare.py` itself reads (§4). The
    campaign's `id` is the LDBC template id even where the *plan artifact*
    executed for it differs (BI6 -> BI6.v2.json) — the contract class is a
    property of the LDBC template, not of which TGIR plan answers it."""
    contracts = json.loads(CONTRACTS_PATH.read_text())["rows"]
    for t in campaign["templates"]:
        assert t["id"] in contracts, t["id"]
        assert contracts[t["id"]]["claim_full_contract"] == t["contract"], t["id"]


# --------------------------------------------------------------------------- #
# every template's plan artifact exists on disk                              #
# --------------------------------------------------------------------------- #

def test_every_template_plan_artifact_exists_on_disk(campaign):
    for t in campaign["templates"]:
        plan_path = PLANS_DIR / t["plan_artifact"]
        assert plan_path.is_file(), f"{t['id']}: missing {plan_path}"
        # must at least parse as JSON with the right plan_id inside
        doc = json.loads(plan_path.read_text())
        assert doc["plan_id"] in (t["id"], t["plan_artifact"].removesuffix(".json"))


def test_bi6_uses_the_v2_artifact_not_the_v1_evidence_file(campaign):
    bi6 = next(t for t in campaign["templates"] if t["id"] == "BI6")
    assert bi6["plan_artifact"] == "BI6.v2.json"
    # BI6.json (the v1 defect evidence) must still exist and still be distinct
    assert (PLANS_DIR / "BI6.json").is_file()
    assert (PLANS_DIR / "BI6.v2.json").is_file()


# --------------------------------------------------------------------------- #
# vendored-cypher filenames: campaign.yaml's `cypher` field must match the   #
# reference runner's own naming convention (checked without needing the     #
# vendored tree itself, which is not part of this checkout — RUNBOOK §1.1)  #
# --------------------------------------------------------------------------- #

#: `scripts/ldbc_reference_run.py`'s `_IS_NUM`/`_IC_NUM` tables. RUNBOOK.md
#: §3.1 documented a gap here -- IS1/IS4/IS5 were added to the parameter
#: binder (`ldbc_snb_params.IV_SOURCES`) but not to this filename table, so
#: `_default_cypher_name` raised `KeyError` for them. Lane D1-fix
#: (2026-09-14) closed that gap in `ldbc_reference_run.py`'s `_IS_NUM`; this
#: test now asserts the fix -- every one of the 24 campaign template ids
#: resolves through `_default_cypher_name` with no exception, consistently
#: with the plan artifacts' `provenance.reference_cypher` fields.
@pytest.mark.parametrize(
    "template_id",
    [t for t in yaml.safe_load(CAMPAIGN_PATH.read_text())["templates"]],
    ids=lambda t: t["id"],
)
def test_cypher_field_matches_the_reference_runners_naming_convention(template_id):
    t = template_id  # parametrize handed us the whole dict
    pid = t["id"]
    expected_basename = Path(t["cypher"]).name
    assert R._default_cypher_name(pid) == expected_basename, pid


def test_is1_is4_is5_no_longer_raise_keyerror():
    """The specific regression RUNBOOK.md §3.1 named: `run_all()` used to
    raise `KeyError: no known vendored filename convention` for these three
    ids. Pinned here by name so a future edit that re-narrows `_IS_NUM`
    fails loudly instead of silently reopening the gap."""
    assert R._default_cypher_name("IS1") == "interactive-short-1.cypher"
    assert R._default_cypher_name("IS4") == "interactive-short-4.cypher"
    assert R._default_cypher_name("IS5") == "interactive-short-5.cypher"


def test_bi10_is_the_only_template_flagged_as_needing_apoc(campaign):
    assert campaign["neo4j"]["plugins"] == ["apoc-5.26.0-core"]
    bi10 = next(t for t in campaign["templates"] if t["id"] == "BI10")
    assert "APOC" in bi10.get("risk", "")


# --------------------------------------------------------------------------- #
# the parameters source: the binder knows how to bind every one of the 24   #
# --------------------------------------------------------------------------- #

def test_parameters_source_module_exists(campaign):
    source = Path(campaign["parameters"]["source"])
    # params.json itself is generated by the run (not committed -- it does
    # not exist in a prep-only worktree); what must exist is the mechanism
    # that produces it at the declared relative path, under this campaign's
    # own directory.
    assert source == Path("benchmarks/ldbc-ref-v1/params.json")
    assert (ROOT / "scripts" / "ldbc_snb_params.py").is_file()


def test_every_templates_binding_key_is_known_to_the_params_module(campaign):
    """For each of the 24 templates, the *binding key* the campaign would
    hand to `ldbc_snb_params.bind()` (the LDBC id, except BI6 which binds
    under `BI6.v2` -- same source file, same column, same rule, per
    `docs/design/BI6_TRIAGE_2026-09-14.md`) is present in `BI_SOURCES` or
    `IV_SOURCES`. This is what "the parameters source exists" means for a
    binder invoked at run time rather than a file committed in advance."""
    known = set(P.BI_SOURCES) | set(P.IV_SOURCES)
    for t in campaign["templates"]:
        binding_key = "BI6.v2" if t["id"] == "BI6" else t["id"]
        assert binding_key in known, f"{t['id']} ({binding_key}) not bindable"


def test_seed_matches_the_d1_local_campaign_seed(campaign):
    assert campaign["parameters"]["seed"] == P.CAMPAIGN_SEED == 5724984519806421702


# --------------------------------------------------------------------------- #
# the documented result-manifest layout (RUNBOOK.md §9) validates           #
# --------------------------------------------------------------------------- #

def _synthetic_manifest(record_path: str) -> dict:
    """The exact layout RUNBOOK.md §9 documents, with placeholder values --
    this is what the xzgpu lane's real manifest must match the *shape* of,
    not a value pinned in advance (the real run has not happened)."""
    return {
        "schema_version": "1.0.0",
        "git_commit": "0" * 12,
        "timestamp_utc": "2026-10-20T00:00:00Z",
        "machine": {"host": "xzgpu",
                    "platform": "Linux-5.4.0-216-generic-x86_64-with-glibc2.31",
                    "cpus": 40, "ram_gb": 93},
        "config": {
            "neo4j_version": "5.26.0",
            "neo4j_conf_sha256": "a" * 64,
            "apoc_version": "5.26.0-core",
            "params_file": "benchmarks/ldbc-ref-v1/params.json",
            "contracts_file": "tests/fixtures/ldbc_ref/contracts.json",
        },
        "seed": {"value": 5724984519806421702, "reason": None},
        "dataset": {
            "name": "ldbc-sf1-bi-composite-projected-fk",
            "digest": "b" * 64,
            "digest_kind": "manifest",
        },
        "result_digest": "c" * 64,
        "protocol": {"warmups": 0, "reps": 1,
                     "ceilings": {"query_timeout_s": 600}},
        "record": record_path,
    }


def test_synthetic_manifest_in_the_documented_layout_validates(tmp_path):
    schema = json.loads(SCHEMA_PATH.read_text())
    manifest = _synthetic_manifest("benchmarks/ldbc-ref-v1/compare-2026-10-20.json")

    out = tmp_path / "manifest.json"
    out.write_text(json.dumps(manifest, indent=1))
    reloaded = json.loads(out.read_text())

    jsonschema.validate(reloaded, schema)  # raises on failure


def test_synthetic_manifest_null_seed_without_reason_is_rejected(tmp_path):
    """The schema's own conditional (`seed.value == null` requires a
    non-empty `reason`) is exercised here so a future edit to the documented
    layout that silently drops `reason` fails loudly."""
    schema = json.loads(SCHEMA_PATH.read_text())
    manifest = _synthetic_manifest("benchmarks/ldbc-ref-v1/compare-2026-10-20.json")
    manifest["seed"] = {"value": None, "reason": None}

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(manifest, schema)


def test_gates_reference_the_real_schema_path(campaign):
    assert campaign["gates"]["G-R2_record_validates"]["schema"] == \
        "benchmarks/schema/result_manifest.schema.json"
    assert SCHEMA_PATH.is_file()


# --------------------------------------------------------------------------- #
# every campaign template id resolves through the runner's own tables, end   #
# to end, against the real vendored .cypher files now on `main` -- no        #
# driver install, no Neo4j instance, no store: a fake session stands in for  #
# the real `neo4j` driver session (RUNBOOK.md's own module docstring: the    #
# small surface `run_query` uses is `.run(query, params) -> Result` with     #
# `.keys()` + iteration).                                                    #
# --------------------------------------------------------------------------- #

class _FakeResult:
    def keys(self):
        return []

    def __iter__(self):
        return iter(())


class _FakeSession:
    def run(self, cypher_text, params):  # noqa: ARG002 -- fixed fake surface
        assert isinstance(cypher_text, str) and cypher_text  # file was read
        return _FakeResult()


def test_every_campaign_template_id_resolves_through_run_all(campaign):
    """RUNBOOK.md §3.1's gap, closed: every one of the 24 ids must resolve to
    an existing vendored `.cypher` file and run with no `"error"` key in the
    result -- not just `_default_cypher_name` not raising (the parametrized
    test above), but the full `run_all()` path a real xzgpu invocation takes,
    split into the same two `--cypher-dir` invocations RUNBOOK.md §5.3
    documents."""
    ids = [t["id"] for t in campaign["templates"]]
    bi_ids = [pid for pid in ids if pid.startswith("BI")]
    iv_ids = [pid for pid in ids if not pid.startswith("BI")]
    assert bi_ids and iv_ids

    params_doc = {"rows": {pid: {"cypher": {}} for pid in ids}}
    session = _FakeSession()

    bi_results = R.run_all(params_doc, BI_CYPHER_DIR, session, plan_ids=bi_ids)
    iv_results = R.run_all(params_doc, IV_CYPHER_DIR, session, plan_ids=iv_ids)

    errors = {pid: r["error"] for pid, r in {**bi_results, **iv_results}.items()
             if "error" in r}
    assert errors == {}
    assert set(bi_results) == set(bi_ids)
    assert set(iv_results) == set(iv_ids)

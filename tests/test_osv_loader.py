"""`tgms/data/osv_loader.py` (Lane F3, P0.5) against the 20-advisory fixture
under `tests/fixtures/osv/` (see its `README.md` for provenance/license and
exactly what each record covers).

Backend: native only (`TGMS_TEST_BACKEND=native`, the loader test recipe's
own default) — the fixture is small enough that this is fast, and the
store-level assertions below (`verify()`, digest replay) want the real
crash-safe engine, not an oracle.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

import tgms
from tgms.core.model import OPEN_END
from tgms.storage.eventlog import replay

from tgms.data.osv_loader import (
    ALLOWED_ECOSYSTEMS,
    bootstrap_ops,
    canonical_digest,
    dedupe_assert_nodes,
    diff_to_ops,
    is_malicious,
    iter_bootstrap_records,
    iter_ecosystem_records,
    osv_uid,
    parse_osv_ts,
    record_to_ops,
    summarize_ops,
)

BACKEND = os.environ.get("TGMS_TEST_BACKEND", "native")

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "osv"
REVISIONS = FIXTURE_ROOT / "revisions"


def _load(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _bootstrap_record(advisory_id: str) -> dict[str, Any]:
    for eco in ALLOWED_ECOSYSTEMS:
        p = FIXTURE_ROOT / eco / f"{advisory_id}.json"
        if p.exists():
            return _load(p)
    raise FileNotFoundError(advisory_id)


def _revision(advisory_id: str, n: int) -> dict[str, Any]:
    return _load(REVISIONS / f"{advisory_id}.v{n}.json")


# ---------------------------------------------------------------------------
# 1 — identity: determinism and collision-freedom
# ---------------------------------------------------------------------------


def test_osv_uid_is_deterministic() -> None:
    assert osv_uid("A", "GHSA-xxxx-xxxx-xxxx") == osv_uid("A", "GHSA-xxxx-xxxx-xxxx")
    assert osv_uid("P", "PyPI", "django") == "P:PyPI\x1fdjango"
    assert osv_uid("V", "PyPI", "django", "4.2.1") == "V:PyPI\x1fdjango\x1f4.2.1"


def test_osv_uid_is_collision_free_across_ecosystems_and_kinds() -> None:
    # the \x1f separator cannot occur in a real ecosystem/name/id, so a
    # different tuple never spells the same string -- demonstrated on the
    # near-miss shapes that would collide under a naive "+".join(parts):
    a = osv_uid("P", "PyPI", "foo-bar")
    b = osv_uid("P", "PyPI-foo", "bar")
    assert a != b
    # different kinds never collide even over the same parts
    assert osv_uid("P", "PyPI", "x") != osv_uid("V", "PyPI", "x")


def test_uid_collision_freedom_over_the_whole_fixture() -> None:
    records = list(iter_bootstrap_records(FIXTURE_ROOT))
    ops = list(bootstrap_ops(records))
    uid_labels: dict[str, str] = {}
    for op in ops:
        if op["op"] != "assert_node":
            continue
        prior = uid_labels.get(op["uid"])
        assert prior in (None, op["label"]), (
            f"uid {op['uid']!r} asserted with two different labels: "
            f"{prior!r} and {op['label']!r}"
        )
        uid_labels[op["uid"]] = op["label"]


# ---------------------------------------------------------------------------
# 2 — MAL- filtering
# ---------------------------------------------------------------------------


def test_mal_prefixed_records_are_filtered() -> None:
    assert is_malicious("MAL-2022-7421")
    assert not is_malicious("GHSA-227r-w5j2-6243")
    mal = _load(FIXTURE_ROOT / "PyPI" / "MAL-2022-7421.json")
    assert record_to_ops(mal) == []


def test_mal_prefixed_records_never_reach_iter_ecosystem_records() -> None:
    ids = {r["id"] for r in iter_ecosystem_records(FIXTURE_ROOT, "PyPI")}
    assert "MAL-2022-7421" not in ids
    assert "GHSA-227r-w5j2-6243" in ids


# ---------------------------------------------------------------------------
# 3 — the fixture parses to the expected node/edge counts
# ---------------------------------------------------------------------------

#: Computed once from the fixture and pinned here as the fidelity gate
#: (`snb_loader.fidelity`'s discipline: exact or nothing). 20 files on disk,
#: 19 kept (one `MAL-` filtered).
EXPECTED_RECORDS = 19
EXPECTED_NODE_LABELS = {
    "Advisory": 46, "Reference": 86, "Ecosystem": 4, "Package": 19,
    "Range": 20, "PackageVersion": 26,
}
EXPECTED_EDGE_RELS = {
    "aliases": 54, "references": 86, "affects": 19, "range_of": 20,
    "fixed_by": 21, "introduced_in": 5,
}


def test_fixture_parses_to_expected_counts() -> None:
    records = list(iter_bootstrap_records(FIXTURE_ROOT))
    assert len(records) == EXPECTED_RECORDS

    ops = list(bootstrap_ops(records))
    node_labels: dict[str, int] = {}
    edge_rels: dict[str, int] = {}
    for op in ops:
        if op["op"] == "assert_node":
            node_labels[op["label"]] = node_labels.get(op["label"], 0) + 1
        elif op["op"] == "assert_edge":
            edge_rels[op["rel_type"]] = edge_rels.get(op["rel_type"], 0) + 1
    assert node_labels == EXPECTED_NODE_LABELS
    assert edge_rels == EXPECTED_EDGE_RELS

    summary = summarize_ops(ops)
    assert summary["assert_node"] == sum(EXPECTED_NODE_LABELS.values()) == 201
    assert summary["assert_edge"] == sum(EXPECTED_EDGE_RELS.values()) == 205
    assert "correct" not in summary and "retract" not in summary


def test_ranges_never_enumerate_versions() -> None:
    """GHSA-227r-w5j2-6243's real record carries ~140 enumerated
    `affected[].versions` entries; none of them may become a node."""
    record = _bootstrap_record("GHSA-227r-w5j2-6243")
    assert len(record["affected"][0]["versions"]) > 100
    ops = record_to_ops(record)
    version_uids = {op["uid"] for op in ops if op["op"] == "assert_node"
                    and op["label"] == "PackageVersion"}
    # only the range's own fixed/introduced events name a version: "0" (the
    # sentinel) is skipped, "5.3.0rc1" (fixed) is the only concrete one.
    assert version_uids == {osv_uid("V", "PyPI", "invokeai", "5.3.0rc1")}


# ---------------------------------------------------------------------------
# 4 — bootstrap: already-withdrawn, last_affected-only, git ranges
# ---------------------------------------------------------------------------


def test_already_withdrawn_at_bootstrap_bounds_affects_not_asserted_open() -> None:
    record = _bootstrap_record("GHSA-22mf-97vh-x8rw")
    assert record["withdrawn"]
    withdrawn_ts = parse_osv_ts(record["withdrawn"])
    ops = record_to_ops(record)
    affects = [op for op in ops if op["op"] == "assert_edge" and op["rel_type"] == "affects"]
    assert len(affects) == 1
    assert affects[0]["vt_e"] == withdrawn_ts
    adv = [op for op in ops if op["op"] == "assert_node" and op["label"] == "Advisory"][0]
    assert adv["props"]["withdrawn"] is True
    # last_affected without fixed still yields a fixed_by edge (schema
    # rule: last_affected also maps to "fixed_by")
    fixed_by = [op for op in ops if op["op"] == "assert_edge" and op["rel_type"] == "fixed_by"]
    assert len(fixed_by) == 1
    # introduced: "0" is the sentinel -- no introduced_in edge
    assert not [op for op in ops if op["op"] == "assert_edge" and op["rel_type"] == "introduced_in"]


def test_git_range_multi_event_all_concrete() -> None:
    record = _bootstrap_record("OSV-2021-1809")
    ops = record_to_ops(record)
    ranges = [op for op in ops if op["op"] == "assert_node" and op["label"] == "Range"]
    assert len(ranges) == 1
    assert ranges[0]["props"]["range_type"] == "GIT"
    assert len(ranges[0]["props"]["events"]) == 3  # 1 introduced + 2 fixed
    introduced_in = [op for op in ops if op["op"] == "assert_edge" and op["rel_type"] == "introduced_in"]
    fixed_by = [op for op in ops if op["op"] == "assert_edge" and op["rel_type"] == "fixed_by"]
    assert len(introduced_in) == 1  # the commit hash is concrete, not "0"
    assert len(fixed_by) == 2       # two distinct fixed commit hashes


# ---------------------------------------------------------------------------
# 5 — diff_to_ops: one correction/append/noop per §2 row, on real revisions
# ---------------------------------------------------------------------------


def test_range_edit_is_one_correction_plus_new_version() -> None:
    old = _bootstrap_record("GHSA-227r-w5j2-6243")
    new = _revision("GHSA-227r-w5j2-6243", 2)
    assert canonical_digest(old) != canonical_digest(new)
    ops = diff_to_ops(old, new)
    assert summarize_ops(ops) == {"correct": 1, "assert_node": 1, "assert_edge": 1}
    corrections = [op for op in ops if op["op"] == "correct"]
    assert corrections[0]["ref"]["kind"] == "node"
    assert corrections[0]["ref"]["uid"].startswith("R:GHSA-227r-w5j2-6243\x1f")
    assert corrections[0]["props"]["fixed"] == "5.3.0"
    new_version = [op for op in ops if op["op"] == "assert_node"][0]
    assert new_version["uid"] == osv_uid("V", "PyPI", "invokeai", "5.3.0")


def test_severity_change_is_one_advisory_correction() -> None:
    old = _revision("GHSA-227r-w5j2-6243", 2)
    new = _revision("GHSA-227r-w5j2-6243", 3)
    ops = diff_to_ops(old, new)
    assert summarize_ops(ops) == {"correct": 1}
    op = ops[0]
    assert op["ref"] == {"kind": "node", "uid": "A:GHSA-227r-w5j2-6243"}
    assert op["props"]["severity"] != old.get("severity")
    assert op["vt_e"] == OPEN_END  # advisory node's own vt never truncates


def test_reference_only_bump_is_append_at_modified() -> None:
    old = _bootstrap_record("GHSA-22cj-m4wf-fv2c")
    new = _revision("GHSA-22cj-m4wf-fv2c", 2)
    ops = diff_to_ops(old, new)
    assert summarize_ops(ops) == {"assert_node": 1, "assert_edge": 1}
    edge = [op for op in ops if op["op"] == "assert_edge"][0]
    assert edge["rel_type"] == "references"
    assert edge["vt_s"] == parse_osv_ts(new["modified"])  # acquired, not retroactive


def test_alias_gained_is_append_both_arms() -> None:
    old = _revision("GHSA-22cj-m4wf-fv2c", 2)
    new = _revision("GHSA-22cj-m4wf-fv2c", 3)
    ops = diff_to_ops(old, new)
    assert summarize_ops(ops) == {"assert_node": 1, "assert_edge": 2}
    edges = [op for op in ops if op["op"] == "assert_edge"]
    pairs = {(e["src"], e["dst"]) for e in edges}
    adv, alias = "A:GHSA-22cj-m4wf-fv2c", "A:PYSEC-2026-9999"
    assert pairs == {(adv, alias), (alias, adv)}
    for e in edges:
        assert e["vt_s"] == parse_osv_ts(new["published"])  # held since publication


def test_withdrawn_transition_corrects_and_retracts_affects() -> None:
    old = _bootstrap_record("GHSA-22fp-mf44-f2mq")
    new = _revision("GHSA-22fp-mf44-f2mq", 2)
    assert not old.get("withdrawn") and new.get("withdrawn")
    withdrawn_ts = parse_osv_ts(new["withdrawn"])
    ops = diff_to_ops(old, new)
    retracts = [op for op in ops if op["op"] == "retract"]
    corrects = [op for op in ops if op["op"] == "correct"]
    assert len(retracts) == 1
    assert retracts[0]["ref"] == {
        "kind": "edge", "src": "A:GHSA-22fp-mf44-f2mq",
        "dst": osv_uid("P", "PyPI", "youtube-dl"), "rel_type": "affects", "disc": "",
    }
    assert retracts[0]["t"] == withdrawn_ts
    assert len(corrects) == 1
    assert corrects[0]["props"]["withdrawn"] is True
    assert corrects[0]["vt_e"] == OPEN_END  # the advisory node itself never truncates
    # the record's own `related: [CVE-2024-38519]` becomes a best-effort
    # `withdraws` edge (documented as a design-memo gap, not spelled exactly)
    withdraws = [op for op in ops if op["op"] == "assert_edge" and op["rel_type"] == "withdraws"]
    assert len(withdraws) == 1
    assert withdraws[0]["src"] == osv_uid("A", "CVE-2024-38519")
    assert withdraws[0]["dst"] == "A:GHSA-22fp-mf44-f2mq"


def test_noop_revision_emits_nothing() -> None:
    old = _bootstrap_record("GHSA-22jm-p2vv-j2hc")
    new = _revision("GHSA-22jm-p2vv-j2hc", 2)
    assert old["modified"] != new["modified"]
    assert canonical_digest(old) == canonical_digest(new)
    assert diff_to_ops(old, new) == []


def test_diff_to_ops_rejects_mismatched_ids() -> None:
    a = _bootstrap_record("GHSA-22jm-p2vv-j2hc")
    b = _bootstrap_record("GHSA-22cc-w7xm-rfhx")
    with pytest.raises(ValueError):
        diff_to_ops(a, b)


# ---------------------------------------------------------------------------
# 6 — dedupe_assert_nodes: cross-record shared-node dedup
# ---------------------------------------------------------------------------


def test_dedupe_assert_nodes_keeps_first_and_drops_repeats() -> None:
    ops = [
        {"op": "assert_node", "uid": "P:PyPI\x1fdjango", "label": "Package"},
        {"op": "assert_edge", "src": "x", "dst": "y", "rel_type": "r"},
        {"op": "assert_node", "uid": "P:PyPI\x1fdjango", "label": "Package"},
    ]
    out = list(dedupe_assert_nodes(ops, lambda u: False))
    assert summarize_ops(out) == {"assert_node": 1, "assert_edge": 1}


def test_dedupe_assert_nodes_honours_already_exists() -> None:
    ops = [{"op": "assert_node", "uid": "P:PyPI\x1fdjango", "label": "Package"}]
    out = list(dedupe_assert_nodes(ops, lambda u: True))
    assert out == []


# ---------------------------------------------------------------------------
# 7 — end-to-end: write into a native store, verify, replay reproduces digest
# ---------------------------------------------------------------------------


def test_bootstrap_writes_and_verifies_clean() -> None:
    records = list(iter_bootstrap_records(FIXTURE_ROOT))
    ops = list(bootstrap_ops(records))
    store_dir = Path(tempfile.mkdtemp())
    store = tgms.open(store_dir, backend=BACKEND)
    try:
        store._write(ops)
        stats = store.stats()
        assert stats["n_node_versions"] == 201
        assert stats["n_edge_versions"] == 205
        health = store.adapter.verify()
        assert health["healthy"] is True
        assert health["problems"] == []
    finally:
        store.close()


def test_replay_reproduces_digest() -> None:
    """The loading rule (`docs/eval/DATASET_CARDS.md`): one recorded event
    log per dataset, and replay of it into a fresh store reproduces the
    same logical content (`Store.digest()`), independent of `tt`/uid
    reproducibility (D-023 is about a *second ingest*, not a replay)."""
    records = list(iter_bootstrap_records(FIXTURE_ROOT))
    ops = list(bootstrap_ops(records))

    original_dir = Path(tempfile.mkdtemp())
    original = tgms.open(original_dir, backend=BACKEND)
    original._write(ops)
    original_digest = original.digest()
    original.close()

    replay_dir = Path(tempfile.mkdtemp())
    replayed = tgms.open(replay_dir, backend=BACKEND)
    n = replay(original_dir / "eventlog.jsonl", replayed.adapter, thread_cursor=True)
    assert n == 1  # one batch, one event-log record -- the whole bootstrap
    assert replayed.digest() == original_digest
    replayed.close()


# ---------------------------------------------------------------------------
# 8 — end-to-end: bootstrap fixture -> poll --once (fake fetcher) -> artifact
#     staleness/refresh, via scripts/live_osv_poller.py's own functions
# ---------------------------------------------------------------------------


def test_poll_once_with_fake_fetcher_then_artifact_refresh() -> None:
    import sys as _sys

    scripts_dir = str(Path(__file__).parent.parent / "scripts")
    if scripts_dir not in _sys.path:
        _sys.path.insert(0, scripts_dir)
    import live_osv_poller as poller  # noqa: PLC0415 -- test-local import

    store_dir = Path(tempfile.mkdtemp())
    state_path = Path(tempfile.mkdtemp()) / "state.json"
    metrics_path = Path(tempfile.mkdtemp()) / "live_metrics.jsonl"
    ledger_path = Path(tempfile.mkdtemp()) / "failure_ledger.jsonl"

    state = poller.bootstrap(store_dir, FIXTURE_ROOT, state_path=state_path)
    assert state["ids"], "bootstrap must record at least one known advisory id"

    store = tgms.open(store_dir, backend=BACKEND, read_only=True)
    from tgms.artifact.registry import Registry
    from tgms.artifact.witness import check_artifact
    from tgms.storage.eventlog import EventLog

    registry = Registry(store_dir)
    pkg_uid = osv_uid("P", "PyPI", "invokeai")
    record = poller.register_exposure_artifact(store, registry, pkg_uid)
    log = EventLog(store_dir / "eventlog.jsonl")
    assert check_artifact(record, log).actionable_fresh
    store.close()

    revised = _revision("GHSA-227r-w5j2-6243", 2)

    def fake_fetch(advisory_id: str) -> dict[str, Any]:
        assert advisory_id == "GHSA-227r-w5j2-6243"
        return revised

    result = poller.poll_once(
        store_dir, state_path=state_path, metrics_path=metrics_path,
        ledger_path=ledger_path, fetch_ids=[("GHSA-227r-w5j2-6243", revised["modified"])],
        fetch_record=fake_fetch,
    )
    assert result["corrections_written"] >= 1
    assert result["noop_revisions"] == 0
    # §4: the correction batch's `affected()` walk must have flagged this
    # exact package's exposure artifact POSSIBLY_STALE and `refresh()`d it
    # within the same cycle -- that wiring, not a manual refresh call, is
    # what this test is proving.
    assert result["artifacts_stale"] >= 1
    assert result["artifacts_refreshed"] >= 1

    verify_store = tgms.open(store_dir, backend=BACKEND, read_only=True)
    assert verify_store.adapter.verify()["healthy"] is True
    verify_store.close()

    registry = Registry(store_dir)
    log = EventLog(store_dir / "eventlog.jsonl")
    name = f"osv-exposure:{pkg_uid}"
    current = registry.current(name)
    # generation 0 (registered before the correction) is untouched on disk
    # and now reads POSSIBLY_STALE (not actionable_fresh); generation 1 is
    # poll_once's own refresh, superseding it and reading FRESH.
    assert current.generation == 1
    assert current.supersedes == record.id
    assert check_artifact(current, log).actionable_fresh
    assert not check_artifact(registry.at(name, 0), log).actionable_fresh
    assert registry.at(name, 0).to_json() == record.to_json()  # byte-identical, untouched

    assert metrics_path.exists()
    lines = [json.loads(line) for line in metrics_path.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["corrections_written"] >= 1
    assert "restart_count" in lines[0]

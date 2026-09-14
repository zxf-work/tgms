"""The integrity checker's full mode (Lane A A3).

`tests/test_native_faults.py` asks the durability question — a corrupted
store must refuse or serve the previous generation, never silently serve
mixed data. This file asks the *checker's* question, which is narrower and
sharper: for each defect an operator can actually suffer, does
`verify(mode="full")` name it, with the right `layer` and `kind`?

That matters because A4's corruption sweep uses this as its oracle. A sweep
injects a defect and asks the checker what it sees; if the checker's answer
is prose, the sweep can only grep, and if the checker repairs anything the
sweep is measuring its own repair. So every case below asserts two things:
the finding's structured identity, and — where the defect is one something
else in the system knows how to fix — that nothing on disk moved.

One defect class is deliberately absent here and covered in Rust instead:
two believed versions of one identity overlapping in valid time. There is no
way to build it through the Python API. `apply_ops` carves overlaps apart by
construction, and a hand-edited segment fails its checksum long before the
row invariants are reached, so the fixture has to be a segment written by
the low-level writer — which is what
`crates/tgms-engine-core/src/integrity.rs`'s own tests do. Adding an engine
hook that could produce the shape would be a hole in the write path for a
test's benefit. The same is true of a dictionary code that points past the
committed prefix: truncating `dict.log` is refused at *open*, which is a
different (and correct) answer, asserted below.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tgms.core.model import canonical_json

pytest.importorskip("tgms._engine", reason="native engine extension not built")

from tgms.cli import main  # noqa: E402
from tgms.storage.native import NativeAdapter  # noqa: E402


# --- fixtures ------------------------------------------------------------ #
#
# The constructed-state pattern of `tests/test_native_faults.py`: build a
# small real store, close it, then damage exactly one file.


def build(root: Path, batches: int = 3) -> Path:
    """A store with several published generations, a correction, and a
    persisted TCSR index. Returns the store root, closed."""
    import tgms
    from tgms.core.model import EntityRef

    store = tgms.open(root, backend="native")
    for b in range(batches):
        store.assert_edge(f"n{b}", f"n{b + 1}", "R", {"w": b},
                          vt_s=b, vt_e=b + 10, disc=f"#{b}")
    store.assert_node("n0", "N", {"p": 1}, vt_s=0, vt_e=50)
    store.correct(EntityRef(kind="node", uid="n0"), {"p": 99}, vt_s=0, vt_e=50)
    store.adapter.tcsr()  # so the persisted permutation exists to be checked
    store.close()
    return root


def native_dir(root: Path) -> Path:
    return root / "native"


def verify(root: Path, mode: str = "full") -> dict[str, Any]:
    """Check the store the way the CLI does: the adapter directly, so a torn
    log tail is a finding rather than an open failure."""
    adapter = NativeAdapter(native_dir(root))
    try:
        return adapter.verify(mode=mode)
    finally:
        adapter.close()


def kinds(report: dict[str, Any], layer: str | None = None) -> set[tuple[str, str]]:
    return {(f["layer"], f["kind"]) for f in report["findings"]
            if layer is None or f["layer"] == layer}


def any_segment(root: Path) -> Path:
    segs = sorted((native_dir(root) / "seg").glob("*.tgs"))
    assert segs, "expected the fixture to have written segments"
    return segs[0]


def flip_byte(path: Path, offset: int) -> None:
    b = bytearray(path.read_bytes())
    b[offset] ^= 0xFF
    path.write_bytes(bytes(b))


def tree_digest(root: Path) -> str:
    """Every byte under `root`, hashed. The read-only proof."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


# --- a clean store is the baseline --------------------------------------- #


@pytest.mark.parametrize("mode", ["fast", "full"])
def test_a_clean_store_has_no_findings(tmp_path, mode):
    root = build(tmp_path / "s")
    report = verify(root, mode)
    assert report["findings"] == [], report["findings"]
    assert report["problems"] == []
    assert report["healthy"]
    assert report["mode"] == mode


def test_full_mode_actually_walks_the_rows(tmp_path):
    """A mode that checked nothing extra would also report no findings."""
    root = build(tmp_path / "s")
    fast, full = verify(root, "fast"), verify(root, "full")
    assert fast["rows_walked"] == 0
    assert full["rows_walked"] == full["rows"] > 0
    assert full["identities_checked"] > 0
    assert full["believed_rows"] < full["rows_walked"], \
        "the fixture's correction should leave one superseded row"


def test_full_mode_re_derives_the_published_digest_from_scratch(tmp_path):
    """The one check full mode gained with format 3 (V2 diagnosis §4(d)).

    `manifest_sha` is now maintained incrementally — appended to a Merkle
    spine as a commit pushes segments — so a bug in that update would be
    *self-consistent*: the store would seal with a wrong digest, publish it,
    and check it against itself forever. Full mode rebuilds the whole tree
    from the whole ordered segment set and compares against what `CURRENT`
    actually publishes, which no incremental path touched.

    The defect has to be injected under a live handle: a store whose
    `CURRENT` disagrees with its head manifest does not reopen at all, which
    is a different (and also correct) answer, asserted elsewhere.
    """
    import tgms
    root = build(tmp_path / "s")
    store = tgms.open(root, backend="native")
    assert store.adapter.verify(mode="full")["healthy"]

    current = root / "native" / "CURRENT"
    generation = int(current.read_text().split()[0])
    current.write_text(f"{generation} 0000000000000000\n")

    report = store.adapter.verify(mode="full")
    assert not report["healthy"]
    oracle = [f for f in report["findings"]
              if f["kind"] == "digest-oracle-mismatch"]
    assert len(oracle) == 1, report["findings"]
    assert oracle[0]["layer"] == "manifest"
    assert oracle[0]["severity"] == "error"
    assert oracle[0]["generation"] == generation
    assert "0000000000000000" in oracle[0]["detail"]

    # fast mode does not pay for it — it is the price of an incremental
    # digest, and it belongs in the mode nobody runs per commit
    assert store.adapter.verify(mode="fast")["healthy"]
    store.close()


def test_every_finding_carries_the_full_shape(tmp_path):
    """The contract A4 matches on."""
    root = build(tmp_path / "s")
    any_segment(root).unlink()
    for f in verify(root)["findings"]:
        assert set(f) == {"layer", "kind", "path", "generation", "detail", "severity"}
        assert isinstance(f["generation"], int)
        assert f["severity"] in ("error", "advisory")
        assert f["layer"] and f["kind"] and f["detail"]


# --- the event log ------------------------------------------------------- #


def test_a_torn_log_tail_is_a_finding_and_not_a_repair(tmp_path):
    root = build(tmp_path / "s")
    log = root / "eventlog.jsonl"
    with open(log, "ab") as f:
        f.write(b'{"batch_id":"deadbeefdeadbeef","tt":999,"op')  # no newline
    before = tree_digest(root)

    report = verify(root)
    assert ("eventlog", "torn-tail") in kinds(report)
    assert not report["healthy"]
    assert tree_digest(root) == before, \
        "verify must report a torn tail, never trim it"


def test_a_rewritten_middle_record_breaks_the_chain(tmp_path):
    """The tamper a per-record hash alone would let through if it were also
    recomputed: the record's own id *and* the rolling chain must disagree."""
    root = build(tmp_path / "s")
    log = root / "eventlog.jsonl"
    lines = log.read_bytes().splitlines(keepends=True)
    obj = json.loads(lines[2])
    obj["ops"][0]["props"]["w"] = 9  # same byte length, still valid JSON
    rewritten = (canonical_json(obj) + "\n").encode()
    assert len(rewritten) == len(lines[2]), "the rewrite must not move later offsets"
    lines[2] = rewritten
    log.write_bytes(b"".join(lines))

    report = verify(root)
    found = kinds(report, "eventlog")
    assert ("eventlog", "record-id-mismatch") in found, found
    assert ("eventlog", "chain-mismatch") in found, found


def test_fast_mode_never_looks_at_the_log(tmp_path):
    """The modes have to differ in what they cost, not only in what they say."""
    root = build(tmp_path / "s")
    with open(root / "eventlog.jsonl", "ab") as f:
        f.write(b'{"batch_id":"x","tt":1,"op')
    assert verify(root, "fast")["healthy"]
    assert not verify(root, "full")["healthy"]


# --- segments ------------------------------------------------------------ #


def test_a_truncated_segment_is_a_segment_finding(tmp_path):
    root = build(tmp_path / "s")
    seg = any_segment(root)
    seg.write_bytes(seg.read_bytes()[:-32])  # lose the completion marker

    report = verify(root)
    assert ("segment", "segment-unreadable") in kinds(report)
    assert any(f["path"] == f"seg/{seg.name}" for f in report["findings"])


def data_start(seg: Path) -> int:
    """Offset of the first column extent: magic(4) + format(4) + len(4) +
    header JSON, rounded up to the writer's 64-byte alignment."""
    header_len = int.from_bytes(seg.read_bytes()[8:12], "little")
    return -(-(12 + header_len) // 64) * 64


def test_a_flipped_segment_byte_is_a_segment_finding(tmp_path):
    root = build(tmp_path / "s")
    seg = any_segment(root)
    flip_byte(seg, data_start(seg) + 8)  # inside a column extent: the CRC path

    report = verify(root)
    assert ("segment", "segment-unreadable") in kinds(report)
    assert any("checksum" in f["detail"] for f in report["findings"])


def test_a_missing_segment_is_named_as_missing(tmp_path):
    root = build(tmp_path / "s")
    seg = any_segment(root)
    seg.unlink()

    report = verify(root)
    assert ("segment", "segment-missing") in kinds(report)
    assert any(f["path"] == f"seg/{seg.name}" for f in report["findings"])


# --- defects that make the store unopenable ------------------------------ #
#
# Exit 2, not exit 1: these are refusals the engine makes before any report
# exists. "Cannot be checked" and "checked, and here is what is wrong" are
# different answers, and the sweep has to tell them apart.


def test_a_flipped_manifest_byte_makes_the_store_unreadable(tmp_path, capsys):
    root = build(tmp_path / "s")
    gen = int((native_dir(root) / "CURRENT").read_text().split()[0])
    path = native_dir(root) / "manifests" / f"{gen:020}.json"
    doc = json.loads(path.read_text())
    doc["stats"]["n_edge_versions"] += 1  # plausible, but the sha will not match
    path.write_text(json.dumps(doc, indent=2))

    assert main(["check", str(root)]) == 2
    assert "UNREADABLE" in capsys.readouterr().out


def test_a_dangling_current_makes_the_store_unreadable(tmp_path):
    root = build(tmp_path / "s")
    gen = int((native_dir(root) / "CURRENT").read_text().split()[0])
    (native_dir(root) / "CURRENT").write_text(f"{gen + 5} deadbeefdeadbeef\n")

    assert main(["check", str(root)]) == 2


def test_a_dictionary_shorter_than_the_manifest_commits_is_unreadable(tmp_path):
    """The reachable shape of "a code with no record". Truncating `dict.log`
    is caught by `Dictionary::open`, which refuses before verify runs — so
    the honest answer is exit 2. The *other* shape, a row naming a code past
    a dictionary the manifest legitimately commits, is only constructible
    below the Python API and is covered in `integrity.rs`."""
    root = build(tmp_path / "s")
    d = native_dir(root) / "dict.log"
    d.write_bytes(d.read_bytes()[:-8])

    assert main(["check", str(root)]) == 2


# --- the persisted TCSR index -------------------------------------------- #


def test_a_stale_tcsr_stamp_is_reported_but_does_not_condemn_the_store(tmp_path):
    """A permutation the store has outgrown is ignored and rebuilt by
    `load_permutation`, so it must not fail the check — an oracle that fired
    on every store that has written since its last index build would be
    useless to the sweep."""
    import tgms

    root = build(tmp_path / "s")
    store = tgms.open(root, backend="native")
    store.assert_edge("x", "y", "R", {"w": 1}, vt_s=0, vt_e=3, disc="#x")
    store.close()

    report = verify(root)
    assert ("tcsr", "tcsr-stale") in kinds(report)
    assert report["healthy"], "a stale index is advisory, not corruption"
    assert all(f["severity"] == "advisory"
               for f in report["findings"] if f["layer"] == "tcsr")
    assert main(["check", str(root)]) == 0


def test_a_damaged_tcsr_file_is_an_error(tmp_path):
    root = build(tmp_path / "s")
    perm = native_dir(root) / "index" / "tcsr.npz"
    assert perm.exists(), "the fixture should have persisted a permutation"
    perm.write_bytes(b"not a npz file at all")

    report = verify(root)
    assert ("tcsr", "tcsr-unreadable") in kinds(report)
    assert not report["healthy"]


def test_a_rewritten_tcsr_permutation_fails_the_content_check(tmp_path):
    """Stamp and shape both intact, the permutation itself wrong — which only
    a rebuild-and-compare can see."""
    import numpy as np

    root = build(tmp_path / "s")
    perm = native_dir(root) / "index" / "tcsr.npz"
    with np.load(perm) as z:
        fields = {k: z[k] for k in z.files}
    assert len(fields["out_row"]) >= 2, "need two rows to permute"
    fields["out_row"] = fields["out_row"][::-1].copy()
    np.savez(perm, **fields)

    report = verify(root)
    assert ("tcsr", "tcsr-content-mismatch") in kinds(report)
    assert not report["healthy"]


# --- the artifact registry ----------------------------------------------- #


def register(root: Path, *, name: str = "wmc") -> None:
    """One artifact generation beside the store's event log."""
    from tgms.artifact.record import StepDependency
    from tgms.artifact.registry import Registry
    from tgms.storage.eventlog import EventLog
    from tgms.tgir.depscope import DependencyScope, ScopeTerm, Targets, store_identity

    log = EventLog(root / "eventlog.jsonl")
    identity = store_identity(log.header(), log.first_batch())
    scope = DependencyScope(store=identity, tt_q=10,
                            terms=(ScopeTerm(targets=Targets(nodes=("n0",))),))
    reg = Registry(root, log=log)
    reg.register(
        name=name, kind="query_result", store=identity,
        plan={"plan_digest": "pd", "plan_format": 1, "plan_ref": "plans/pd.json"},
        basis={"tt_q": 10, "pinned": False, "clamped": False, "tt_q_verified": True},
        state={"completeness": "complete", "exactness": "exact", "refusal": None},
        refresh={"kind": "tgir_plan", "ref": "plans/pd.json"},
        steps=[StepDependency("s1", scope)],
        payload={"result_digest": "rd", "result_ref": "results/rd.json"},
    )
    (root / "plans").mkdir(exist_ok=True)
    (root / "plans" / "pd.json").write_text(json.dumps({"plan_format": 1}))
    (root / "results").mkdir(exist_ok=True)
    (root / "results" / "rd.json").write_text(json.dumps({"result_digest": "rd"}))


def test_a_registry_with_its_blobs_in_place_is_clean(tmp_path):
    root = build(tmp_path / "s")
    register(root)
    assert verify(root)["healthy"], verify(root)["findings"]


def test_a_tampered_registry_record_fails_its_own_digest(tmp_path):
    root = build(tmp_path / "s")
    register(root)
    path = root / "artifacts.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    obj = json.loads(lines[1])
    obj["kind"] = "something_else"  # inside the digest, and not recomputed
    lines[1] = (canonical_json(obj) + "\n").encode()
    path.write_bytes(b"".join(lines))

    report = verify(root)
    assert ("registry", "record-digest-mismatch") in kinds(report)
    assert not report["healthy"]


def test_a_missing_artifact_blob_is_a_blob_finding(tmp_path):
    root = build(tmp_path / "s")
    register(root)
    (root / "results" / "rd.json").unlink()

    report = verify(root)
    assert ("blob", "blob-missing") in kinds(report)
    assert any("payload.result_ref" in f["detail"] for f in report["findings"])


def test_a_result_blob_that_hashes_to_something_else_is_a_blob_finding(tmp_path):
    root = build(tmp_path / "s")
    register(root)
    (root / "results" / "rd.json").write_text(json.dumps({"result_digest": "other"}))

    report = verify(root)
    assert ("blob", "blob-digest-mismatch") in kinds(report)


# --- task A10: a plan blob's own bytes, not just its existence ------------ #
#
# `register()` above writes `plans/pd.json` *after* `reg.register()`
# returns, so those records carry no `blob_sha256` (`Registry.
# _stamp_blob_sha256` only hashes a blob it can already see) and the tests
# above exercise the pre-A10 fallback path. The corruption campaign's own
# finding (benchmarks/corruption-v1/eval-corruption-campaign-2026-09-14.json,
# stats.detection_matrix["artifact_blob|append_garbage"]: 0/106 detected)
# needs the real, production-shaped ordering — blob on disk *before*
# `register()` — the same ordering `scripts/demo_propagation.py` and
# `scripts/eval_corruption.py::register_sample_artifacts` already use, so
# `blob_sha256` actually gets stamped and `verify(mode="full")` has
# something to compare against.


def register_with_real_plan_blob(root: Path, *, name: str = "wmc2",
                                 plan_bytes: bytes = b'{"plan_format": 1, "op": "NodeScan", '
                                                     b'"uids": ["A"]}') -> None:
    """Same shape as `register()` above, except the plan blob exists before
    `Registry.register()` runs, so it gets a `blob_sha256` stamped against
    its real bytes."""
    from tgms.artifact.record import StepDependency
    from tgms.artifact.registry import Registry
    from tgms.storage.eventlog import EventLog
    from tgms.tgir.depscope import DependencyScope, ScopeTerm, Targets, store_identity

    log = EventLog(root / "eventlog.jsonl")
    identity = store_identity(log.header(), log.first_batch())
    scope = DependencyScope(store=identity, tt_q=10,
                            terms=(ScopeTerm(targets=Targets(nodes=("n0",))),))
    (root / "plans").mkdir(exist_ok=True)
    (root / "plans" / f"{name}.json").write_bytes(plan_bytes)
    reg = Registry(root, log=log)
    reg.register(
        name=name, kind="query_result", store=identity,
        plan={"plan_digest": f"pd-{name}", "plan_format": 1, "plan_ref": f"plans/{name}.json"},
        basis={"tt_q": 10, "pinned": False, "clamped": False, "tt_q_verified": True},
        state={"completeness": "complete", "exactness": "exact", "refusal": None},
        refresh={"kind": "tgir_plan", "ref": f"plans/{name}.json"},
        steps=[StepDependency("s1", scope)],
    )


def test_a_real_plan_blob_matching_its_stamped_hash_is_clean(tmp_path):
    root = build(tmp_path / "s")
    register_with_real_plan_blob(root)
    report = verify(root)
    assert report["healthy"], report["findings"]


def test_appended_garbage_on_a_plan_blob_is_now_a_blob_finding(tmp_path):
    """The exact corruption class the campaign found undetected: garbage
    bytes appended after a complete, otherwise-untouched JSON document.
    `_parse_json_blob_strict`'s "nothing after the document" rule is what
    catches this — the file is still "valid JSON" up to where the real
    document ends, which is exactly what a lenient `json.loads` used to let
    through silently."""
    root = build(tmp_path / "s")
    register_with_real_plan_blob(root)
    with open(root / "plans" / "wmc2.json", "ab") as f:
        f.write(b"\x00\x01not-json-and-not-whitespace\xff")

    report = verify(root)
    assert ("blob", "blob-digest-mismatch") in kinds(report)
    assert not report["healthy"]


def test_a_flipped_byte_in_a_plan_blob_is_a_blob_finding(tmp_path):
    """A mutation that keeps the file syntactically valid JSON — the
    trailing-bytes rule alone would miss this; `blob_sha256` is what
    catches it."""
    root = build(tmp_path / "s")
    register_with_real_plan_blob(root)
    blob = root / "plans" / "wmc2.json"
    buf = bytearray(blob.read_bytes())
    buf[10] ^= 0xFF  # inside "op": "NodeScan" — the document stays valid JSON
    blob.write_bytes(bytes(buf))

    report = verify(root)
    assert ("blob", "blob-digest-mismatch") in kinds(report)
    assert not report["healthy"]


def test_a_deleted_registry_generation_breaks_the_generation_chain(tmp_path):
    """No per-record digest can see a whole record removed; the per-name
    generation sequence is what catches it."""
    root = build(tmp_path / "s")
    register(root)
    register(root)  # generation 1 of the same name
    path = root / "artifacts.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(lines[0] + lines[2])  # drop generation 0

    report = verify(root)
    assert ("registry", "generation-not-consecutive") in kinds(report)


# --- the CLI's contract -------------------------------------------------- #


def test_exit_codes(tmp_path, capsys):
    clean = build(tmp_path / "clean")
    assert main(["store", "verify", "--store", str(clean)]) == 0
    assert main(["store", "verify", "--store", str(clean), "--fast"]) == 0
    assert main(["store", "verify", "--store", str(clean), "--full"]) == 0
    assert main(["check", str(clean)]) == 0

    damaged = build(tmp_path / "damaged")
    any_segment(damaged).unlink()
    assert main(["store", "verify", "--store", str(damaged)]) == 1
    assert main(["check", str(damaged)]) == 1

    assert main(["check", str(tmp_path / "nowhere")]) == 2
    capsys.readouterr()


def test_the_json_report_is_machine_readable(tmp_path, capsys):
    root = build(tmp_path / "s")
    any_segment(root).unlink()
    assert main(["check", str(root), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "full"
    assert report["readable"] is True
    assert report["store"] == str(root)
    assert report["healthy"] is False
    assert any(f["kind"] == "segment-missing" for f in report["findings"])


def test_the_unreadable_report_is_machine_readable_too(tmp_path, capsys):
    root = build(tmp_path / "s")
    (native_dir(root) / "CURRENT").write_text("not-a-generation\n")
    assert main(["check", str(root), "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["readable"] is False
    assert report["reason"]


def test_fast_and_full_are_mutually_exclusive():
    from tgms.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["store", "verify", "--store", "x", "--fast", "--full"])


def test_verify_mode_is_validated(tmp_path):
    from tgms.core.errors import InvalidArgError

    root = build(tmp_path / "s")
    adapter = NativeAdapter(native_dir(root))
    try:
        with pytest.raises(InvalidArgError):
            adapter.verify(mode="thorough")
    finally:
        adapter.close()


# --- the read-only promise ------------------------------------------------ #


def test_a_full_check_of_a_damaged_store_changes_nothing_on_disk(tmp_path):
    """The property the whole design rests on. Several defects at once, so a
    repair anywhere in the composition would show."""
    root = build(tmp_path / "s")
    register(root)
    with open(root / "eventlog.jsonl", "ab") as f:
        f.write(b'{"batch_id":"deadbeefdeadbeef","tt":999,"op')
    (root / "results" / "rd.json").unlink()
    seg = any_segment(root)
    seg.write_bytes(seg.read_bytes()[:-16])
    before = tree_digest(root)
    before_names = sorted(str(p.relative_to(root)) for p in root.rglob("*"))

    report = verify(root)
    assert not report["healthy"]
    assert tree_digest(root) == before
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == before_names


def test_checking_a_store_twice_gives_the_same_answer(tmp_path):
    """Determinism, which a sweep that diffs two reports depends on."""
    root = build(tmp_path / "s")
    any_segment(root).unlink()
    first, second = verify(root), verify(root)
    assert first["findings"] == second["findings"]


def test_verify_does_not_create_a_registry_or_a_log(tmp_path):
    """`Registry(...)` and `EventLog(...)` both create their file when it is
    absent. Neither may be reached that way from a checker."""
    root = tmp_path / "bare"
    (root / "native").mkdir(parents=True)
    adapter = NativeAdapter(root / "native")  # genesis, no log, no registry
    try:
        report = adapter.verify(mode="full")
    finally:
        adapter.close()
    assert report["healthy"], report["findings"]
    assert not (root / "eventlog.jsonl").exists()
    assert not (root / "artifacts.jsonl").exists()


def test_the_engine_directory_is_never_created_by_a_check(tmp_path, capsys):
    missing = tmp_path / "nothing-here"
    assert main(["check", str(missing)]) == 2
    capsys.readouterr()
    assert not missing.exists()


# --- documentation of the boundary --------------------------------------- #


def test_the_overlap_invariant_is_covered_in_rust():
    """A signpost, not a check.

    I1 disjointness of believed versions is enforced by `apply_ops` and
    verified by `integrity.rs::check_rows`. It is unreachable from here: the
    write path carves overlaps apart, and editing a segment to create one
    fails the segment's checksum first, so the fixture has to be a segment
    written by the low-level Rust writer. See
    `integrity::tests::two_believed_versions_of_one_identity_may_not_overlap`
    and `..._a_row_naming_a_code_past_the_committed_dictionary_is_a_finding`.
    """
    source = Path(__file__).resolve().parents[1] / \
        "crates/tgms-engine-core/src/integrity.rs"
    if not source.exists():  # an installed wheel ships no crate sources
        pytest.skip("engine sources are not in this tree")
    text = source.read_text()
    assert "fn two_believed_versions_of_one_identity_may_not_overlap" in text
    assert "fn a_row_naming_a_code_past_the_committed_dictionary_is_a_finding" in text

#!/usr/bin/env python
"""Corruption-detection sweep (Lane A task A4; plan `docs/eval_durability.md`
EXP-A3; grounded in `tests/test_native_faults.py`, `tests/test_torn_wal.py`,
`tests/test_suffix_replay.py`, `tests/test_tcsr_persistence.py`,
`tests/test_artifact_registry.py`).

The durability objective those tests already pin one file at a time is the
engine's own promise: *never expose an undetected inconsistent generation.*
This harness restates that as a randomized sweep: build one small store with
every on-disk file family present (segments, close runs, a checkpoint and a
delta manifest, the dictionary, `CURRENT`, the TCSR index, two registered
artifacts and their plan blobs), corrupt exactly one file one way, then
observe — never repair — through four surfaces a real caller would use:

  1. `tgms.open(store, backend="native", read_only=True)` — a reader process
     reopening the store. This still runs the native engine's own
     construction-time checks (manifest_sha, `CURRENT` shape, missing
     manifest, a populated store with `CURRENT` itself gone — see
     `tests/test_native_faults.py`), and Python's
     `Store._seed_frontier` *unconditionally* walks the whole event log
     (even for a reader, to avoid seeding a false-fresh frontier —
     `tgms/store.py`'s own docstring) with no torn-tail tolerance of its
     own — so, empirically (see `tests/test_eval_corruption.py::
     test_torn_event_log_tail_is_detected_even_read_only`), even a torn
     *tail* the D-086 trim contract says a **writer**-mode open should
     silently recover from is DETECTED here, via a raised `StateError`,
     before `Store._recover`'s replay/trim ever gets a chance to run — the
     one part of WAL handling `read_only=True` actually skips. Only damage
     `_seed_frontier`'s walk never reaches at all (none observed among the
     13 classes below; every class this harness targets is walked by
     something) would pass this step for free.
  2. `store.adapter.verify(mode="full")` (`--verify-mode`, default `full`,
     added Lane A task A9 once A3 landed `verify_full`) — the strongest
     oracle available: the engine's own checksum walk *plus* the manifest
     parent chain, the event log (framing/monotonicity/chain), the
     persisted TCSR permutation, and the artifact registry
     (`tgms/storage/native/adapter.py::NativeStoreAdapter.verify`). `--verify-
     mode fast` reproduces the original engine-file-only walk (segments,
     close runs, the dictionary, manifests; never `eventlog.jsonl` or
     `artifacts.jsonl`) for A/B comparison against pre-A3 runs.
  3. two fixed read queries (`entity_history`, `snapshot_subgraph`) against
     two uids seeded into every trial's store, compared by `result_digest`
     against a pre-mutation baseline of the same queries.
  4. an artifact `check` outcome: `Registry(store)` (which chain-verifies
     `artifacts.jsonl` on load — `tests/test_artifact_registry.py`) plus
     `witness.check_artifact` against the event log for both registered
     artifacts — the one observation that actually reads `eventlog.jsonl`
     end to end, since (1)-(3) mostly do not.

Every trial classifies as exactly one of:

  DETECTED         open refused, verify reported findings, a query raised,
                    or the artifact check step raised.
  SILENT            everything above succeeded AND a query's result digest
                    changed from baseline — the unacceptable outcome; the
                    campaign gate is "this count is zero".
  BENIGN            everything succeeded and every digest matched baseline
                    (e.g. the mutation landed on a file compaction had
                    already superseded, or on the plan blob `check` never
                    reads — see `explain_benign`).
  TOLERATED-REBUILT the mutation targeted the persisted TCSR index, which
                    the engine is documented to silently rebuild rather
                    than trust or error on (`tests/test_tcsr_persistence.py`
                    ::test_a_damaged_file_degrades_to_rebuild).

    python scripts/eval_corruption.py --trials 20 --seed 1 --json out.json
    python scripts/eval_corruption.py --classes segment_body,current \\
        --mutations flip_byte,delete_file --trials 50
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.eval_durability import _apply_workload_op, _random_workload  # noqa: E402

SCHEMA_VERSION = "1.0.0"

#: The two uids every trial's store carries, and the two fixed read queries
#: run against them (task A4's "entity_history on a known uid, snapshot_
#: subgraph at a time"). Chosen once, up front, so the harness never has to
#: discover a "known uid" from a store the mutation may have half-destroyed.
UID_A, UID_B = "q0", "q1"
FIXED_QUERIES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("entity_history", {"uid": UID_A}),
    ("snapshot_subgraph", {"seeds": [UID_A], "hops": 1, "t_valid": 500_000}),
)
#: `temporal_paths` is not one of the two fixed queries — it is only run to
#: force `tcsr()` to (re)build the persisted index, so a `tcsr_file` trial
#: can tell whether the engine rebuilt it.
_TCSR_TOUCH_ARGS = {"src": UID_A, "dst": UID_B, "window": {"t_a": 0, "t_b": 1_000_000},
                     "k": 1, "max_hops": 2}

#: Env var read by the engine's manifest-chain code (`manifest_chain.rs`,
#: `CHECKPOINT_EVERY_ENV`). The fixture builder sets it LOW for the span of
#: its post-compaction writes only (see `_checkpoint_every`), so a handful of
#: generations reliably produces a delta manifest alongside the checkpoint
#: compaction always writes. It must never be set at import time: importing
#: this module from a test leaked the value into every later test in the
#: same process (CI at 0b461de: `test_manifest_format2` saw a checkpoint
#: where the K=512 default writes a delta).
CHECKPOINT_EVERY_FOR_FIXTURE = "2"


class _checkpoint_every:
    """Set `TGMS_MANIFEST_CHECKPOINT_EVERY` for a `with` block and restore it."""

    def __init__(self, value: str) -> None:
        self._value = value
        self._prior: str | None = None

    def __enter__(self) -> None:
        self._prior = os.environ.get("TGMS_MANIFEST_CHECKPOINT_EVERY")
        os.environ["TGMS_MANIFEST_CHECKPOINT_EVERY"] = self._value

    def __exit__(self, *exc: object) -> None:
        if self._prior is None:
            os.environ.pop("TGMS_MANIFEST_CHECKPOINT_EVERY", None)
        else:
            os.environ["TGMS_MANIFEST_CHECKPOINT_EVERY"] = self._prior


class _NullSink:
    """`_apply_workload_op` wants an object to `.write()` acknowledgment
    lines to (eval_durability.py's Q1 bookkeeping) — this harness has no use
    for that ledger, so writes go nowhere."""

    def write(self, _s: str) -> None:
        return None


# --------------------------------------------------------------------------- #
# building the trial's store
# --------------------------------------------------------------------------- #


def native_dir(root: Path) -> Path:
    return root / "native"


def register_sample_artifacts(root: Path) -> list[str]:
    """Register two `query_result` artifacts over `UID_A`/`UID_B`, each with
    a real plan blob on disk at `plans/<name>.json` (the "artifact blob"
    file class) — the minimal valid `Registry.register()` field set,
    following `tests/test_artifact_registry.py`'s `_fields` fixture."""
    from tgms.artifact.record import StepDependency
    from tgms.artifact.registry import Registry
    from tgms.storage.eventlog import EventLog
    from tgms.tgir.depscope import DependencyScope, ScopeTerm, Targets, store_identity

    log = EventLog(root / "eventlog.jsonl")
    identity = store_identity(log.header(), log.first_batch())
    (root / "plans").mkdir(exist_ok=True)
    reg = Registry(root)
    names = []
    for i, uid in enumerate((UID_A, UID_B)):
        name = f"corruption-fixture-{i}"
        plan_ref = f"plans/{name}.json"
        (root / plan_ref).write_text(json.dumps(
            {"plan_format": 1, "op": "entity_history", "args": {"uid": uid}}))
        scope = DependencyScope(store=identity, tt_q=1_000_000,
                                terms=(ScopeTerm(targets=Targets(nodes=(uid,))),))
        reg.register(
            name=name, kind="query_result", store=identity,
            plan={"plan_digest": f"pd-{name}", "node_digest": f"nd-{name}",
                  "plan_format": 1, "plan_ref": plan_ref},
            basis={"tt_q": scope.tt_q, "pinned": False, "clamped": False,
                  "tt_q_verified": True},
            state={"completeness": "complete", "exactness": "exact", "refusal": None},
            refresh={"kind": "tgir_plan", "ref": plan_ref, "basis_policy": "open"},
            steps=[StepDependency(f"s-{name}", scope)],
        )
        names.append(name)
    return names


def build_store(store_dir: Path, trial_seed: int) -> dict[str, Any]:
    """A small store with every file family this sweep targets present:
    multiple segments and generations, a close run *and* a checkpoint *and*
    a delta manifest, a populated dictionary, a persisted TCSR index, and
    two registered artifacts with on-disk plan blobs.

    Returns `build_meta`: `artifact_names`, and `orphan_files` — the
    relative paths of every segment/close-run file that existed
    immediately before the one `compact()` call, which supersedes all of
    them (`tests/test_native_faults.py::
    test_gc_after_compaction_reclaims_superseded_segments`) — so a mutation
    landing on one of these is a known, provable BENIGN case, not a guess.
    """
    import tgms
    from tgms.core.model import EntityRef
    from tgms.temporal.algebra import call_operator, ensure_all_registered

    ensure_all_registered()
    store = tgms.open(store_dir, backend="native")
    sink = _NullSink()

    store.assert_node(UID_A, "N", {"i": 0}, vt_s=0, vt_e=1_000_000)
    store.assert_node(UID_B, "N", {"i": 0}, vt_s=0, vt_e=1_000_000)
    store.assert_edge(UID_A, UID_B, "R", {"i": 0}, vt_s=0, vt_e=1_000_000, disc="#0")

    rng = random.Random(trial_seed)
    for op in _random_workload(rng):
        _apply_workload_op(store, sink, op, rng)
    store.correct(EntityRef(kind="node", uid=UID_A), {"i": 1}, vt_s=0, vt_e=1_000_000)

    orphan_files = {
        str(p.relative_to(store_dir))
        for p in list((native_dir(store_dir) / "seg").glob("*.tgs"))
        + list((native_dir(store_dir) / "close").glob("*.tgc"))
        # every manifest generation written so far, checkpoint and deltas
        # alike: compact() below writes a brand-new, self-contained
        # checkpoint, so backward parent-chain reconstruction from the new
        # CURRENT never revisits any of these lower generations again
        # (manifest_chain.rs's "walks parent links back to nearest
        # checkpoint" — the new checkpoint has no parent to walk past).
        + list((native_dir(store_dir) / "manifests").glob("*.json"))
    }
    store.adapter.compact()  # forces a checkpoint manifest; supersedes the above

    # Post-compaction writes: fresh close run(s) the compaction above never
    # saw, and — with TGMS_MANIFEST_CHECKPOINT_EVERY set low — at least one
    # delta manifest layered on the checkpoint compaction just wrote.
    have_delta = False
    with _checkpoint_every(CHECKPOINT_EVERY_FOR_FIXTURE):
        for i in range(8):
            store.correct(EntityRef(kind="node", uid=UID_A), {"i": 2 + i}, vt_s=0, vt_e=1_000_000)
            have_delta = any(
                json.loads(p.read_text()).get("kind") == "delta"
                for p in (native_dir(store_dir) / "manifests").glob("*.json")
            )
            if have_delta:
                break
    assert have_delta, (
        "fixture bug: no delta manifest appeared after 8 post-compaction "
        f"writes with TGMS_MANIFEST_CHECKPOINT_EVERY={CHECKPOINT_EVERY_FOR_FIXTURE}")

    call_operator(store.adapter, "temporal_paths", _TCSR_TOUCH_ARGS)  # builds index/tcsr.npz
    artifact_names = register_sample_artifacts(store_dir)
    store.close()

    return {"artifact_names": artifact_names, "orphan_files": orphan_files}


# --------------------------------------------------------------------------- #
# file classes
# --------------------------------------------------------------------------- #


def _find_event_log(root: Path) -> list[Path]:
    p = root / "eventlog.jsonl"
    return [p] if p.exists() else []


def _find_segments(root: Path) -> list[Path]:
    return sorted((native_dir(root) / "seg").glob("*.tgs"))


def _find_close_runs(root: Path) -> list[Path]:
    return sorted((native_dir(root) / "close").glob("*.tgc"))


def _find_dict(root: Path) -> list[Path]:
    p = native_dir(root) / "dict.log"
    return [p] if p.exists() else []


def _find_manifests_by_kind(root: Path, kind: str) -> list[Path]:
    out = []
    for p in sorted((native_dir(root) / "manifests").glob("*.json")):
        try:
            doc = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if doc.get("kind", "checkpoint") == kind:
            out.append(p)
    return out


def _find_current(root: Path) -> list[Path]:
    p = native_dir(root) / "CURRENT"
    return [p] if p.exists() else []


def _find_tcsr(root: Path) -> list[Path]:
    p = native_dir(root) / "index" / "tcsr.npz"
    return [p] if p.exists() else []


def _find_artifacts_jsonl(root: Path) -> list[Path]:
    p = root / "artifacts.jsonl"
    return [p] if p.exists() else []


def _find_artifact_blobs(root: Path) -> list[Path]:
    d = root / "plans"
    return sorted(d.glob("*.json")) if d.exists() else []


#: The 13 file classes task A4 names, each mapped to a finder over a built
#: store. `build_store` guarantees every one of these is non-empty.
FILE_CLASSES: dict[str, Callable[[Path], list[Path]]] = {
    "event_log_record": _find_event_log,
    "event_log_tail": _find_event_log,
    "segment_header": _find_segments,
    "segment_body": _find_segments,
    "segment_footer": _find_segments,
    "close_run": _find_close_runs,
    "dict_tail": _find_dict,
    "checkpoint_manifest": lambda r: _find_manifests_by_kind(r, "checkpoint"),
    "delta_manifest": lambda r: _find_manifests_by_kind(r, "delta"),
    "current": _find_current,
    "tcsr_file": _find_tcsr,
    "artifacts_jsonl": _find_artifacts_jsonl,
    "artifact_blob": _find_artifact_blobs,
}

MUTATIONS = ("flip_bit", "flip_byte", "zero_span", "truncate",
             "append_garbage", "swap_same_class", "delete_file")


# --------------------------------------------------------------------------- #
# mutation
# --------------------------------------------------------------------------- #


def _segment_data_start(seg: Path) -> int:
    """Offset of the first column extent (`tests/test_native_faults.py::
    data_start`): magic(4) + format(4) + len(4) + header JSON, 64-aligned."""
    header_len = int.from_bytes(seg.read_bytes()[8:12], "little")
    return -(-(12 + header_len) // 64) * 64


def _pick_offset(cls: str, path: Path, rng: random.Random, data: bytes) -> int:
    length = len(data)
    if length == 0:
        return 0
    if cls == "segment_header":
        ds = _segment_data_start(path)
        return rng.randrange(0, max(1, min(ds, length)))
    if cls == "segment_body":
        ds = _segment_data_start(path)
        lo = min(ds, max(0, length - 1))
        hi = max(lo + 1, length - 24)
        return rng.randrange(lo, min(hi, length)) if hi > lo else lo
    if cls == "segment_footer":
        span = min(16, length)
        return length - rng.randint(1, span)
    if cls == "event_log_tail":
        lines = data.splitlines(keepends=True)
        last = lines[-1] if lines else data
        start = length - len(last)
        return rng.randrange(start, length) if length > start else start
    if cls == "event_log_record":
        lines = data.splitlines(keepends=True)
        if len(lines) >= 3:  # header + >=2 records: mutate a non-tail record
            idx = rng.randrange(1, len(lines) - 1)
            start = sum(len(line) for line in lines[:idx])
            span = max(1, len(lines[idx]) - 1)
            return start + rng.randrange(0, span)
        return rng.randrange(0, length)
    if cls == "dict_tail":
        span = min(32, length)
        return length - rng.randint(1, span)
    return rng.randrange(0, length)


def apply_mutation(cls: str, mutation: str, candidates: list[Path],
                   rng: random.Random, root: Path) -> dict[str, Any]:
    """Apply one mutation to one (or, for `swap_same_class`, two) file(s) of
    `cls`. Returns the applied mutation's own record — `mutation` may differ
    from the request when a mutation is structurally inapplicable (a swap
    with only one candidate file, or any byte-level op on an empty file);
    the fallback and why are recorded in `note`, never silently absorbed."""
    info: dict[str, Any] = {"class": cls, "mutation": mutation, "note": None}

    if mutation == "swap_same_class":
        if len(candidates) < 2:
            info["mutation"] = mutation = "flip_byte"
            info["note"] = ("swap_same_class needs >=2 files of this class; "
                            f"only {len(candidates)} present, fell back to flip_byte")
        else:
            a, b = rng.sample(candidates, 2)
            ta, tb = a.read_bytes(), b.read_bytes()
            if ta == tb:
                info["note"] = "swapped files were byte-identical (a legitimate no-op)"
            a.write_bytes(tb)
            b.write_bytes(ta)
            info["file"] = str(a.relative_to(root))
            info["files"] = [str(a.relative_to(root)), str(b.relative_to(root))]
            info["offset"] = None
            return info

    path = rng.choice(candidates)
    info["file"] = str(path.relative_to(root))

    if mutation == "delete_file":
        path.unlink()
        info["offset"] = None
        return info

    data = path.read_bytes()
    if len(data) == 0 and mutation != "append_garbage":
        info["mutation"] = mutation = "append_garbage"
        info["note"] = "target file was empty; fell back to append_garbage"

    if mutation == "append_garbage":
        n = rng.randint(8, 64)
        garbage = bytes(rng.randrange(256) for _ in range(n))
        with open(path, "ab") as f:
            f.write(garbage)
        info["offset"] = len(data)
        info["n_bytes"] = n
        return info

    offset = min(_pick_offset(cls, path, rng, data), max(0, len(data) - 1))
    info["offset"] = offset
    buf = bytearray(data)
    if mutation == "flip_bit":
        buf[offset] ^= 1 << rng.randrange(8)
        path.write_bytes(bytes(buf))
    elif mutation == "flip_byte":
        buf[offset] ^= 0xFF
        path.write_bytes(bytes(buf))
    elif mutation == "zero_span":
        end = min(len(buf), offset + 64)
        for i in range(offset, end):
            buf[i] = 0
        path.write_bytes(bytes(buf))
        info["span"] = [offset, end]
    elif mutation == "truncate":
        n = rng.randint(1, max(1, min(len(data), 200)))
        newlen = max(0, len(data) - n)
        path.write_bytes(data[:newlen])
        info["n_truncated"] = len(data) - newlen
    else:
        raise ValueError(f"unknown mutation: {mutation!r}")
    return info


# --------------------------------------------------------------------------- #
# observation
# --------------------------------------------------------------------------- #


def baseline_digests(store_dir: Path) -> dict[str, str]:
    import tgms
    from tgms.temporal.algebra import call_operator, ensure_all_registered

    ensure_all_registered()
    store = tgms.open(store_dir, backend="native", read_only=True)
    try:
        return {name: call_operator(store.adapter, name, args)["result_digest"]
               for name, args in FIXED_QUERIES}
    finally:
        store.close()


def observe(store_dir: Path, artifact_names: list[str],
           verify_mode: str = "full") -> dict[str, Any]:
    """Run the four A4 observations against a (possibly just-corrupted)
    store, never attempting to fix anything found. Every field defaults to
    "could not even try" shapes so a caller can classify without special-
    casing which step first failed.

    `verify_mode` selects the strength of observation 2: `"full"` (the
    default, since A3/A8 landed) also chain-verifies the manifest parent
    chain, the event log, the persisted TCSR permutation and the artifact
    registry (`NativeStoreAdapter.verify(mode="full")`, `docs/eval_durability.md`
    EXP-A3) — the strongest oracle available. `"fast"` keeps the original
    engine-file-only walk, kept only for A/B comparison against pre-A3 runs."""
    obs: dict[str, Any] = {
        "open_ok": False, "open_error": None, "verify_problems": None,
        "query_errors": {}, "query_digests": {}, "artifact_errors": {},
    }
    import tgms

    try:
        store = tgms.open(store_dir, backend="native", read_only=True)
    except Exception as e:  # noqa: BLE001 - the exact class is the finding
        obs["open_error"] = f"{type(e).__name__}: {e}"
        return obs
    obs["open_ok"] = True

    try:
        v = store.adapter.verify(mode=verify_mode)
        obs["verify_problems"] = list(v.get("problems") or [])
    except Exception as e:  # noqa: BLE001
        obs["verify_problems"] = [f"verify() raised {type(e).__name__}: {e}"]

    from tgms.temporal.algebra import call_operator, ensure_all_registered
    ensure_all_registered()
    for name, args in FIXED_QUERIES:
        try:
            env = call_operator(store.adapter, name, args)
            obs["query_digests"][name] = env["result_digest"]
        except Exception as e:  # noqa: BLE001
            obs["query_errors"][name] = f"{type(e).__name__}: {e}"

    registry_error = None
    records = {}
    try:
        from tgms.artifact.registry import Registry
        reg = Registry(store_dir)
        for name in artifact_names:
            records[name] = reg.current(name)
    except Exception as e:  # noqa: BLE001
        registry_error = f"{type(e).__name__}: {e}"

    if registry_error is not None:
        for name in artifact_names:
            obs["artifact_errors"][name] = registry_error
    else:
        from tgms.artifact.witness import check_artifact
        from tgms.storage.eventlog import EventLog
        log = EventLog(store_dir / "eventlog.jsonl")
        for name, rec in records.items():
            if rec is None:
                obs["artifact_errors"][name] = "artifact vanished from the registry"
                continue
            try:
                check_artifact(rec, log)
            except Exception as e:  # noqa: BLE001
                obs["artifact_errors"][name] = f"{type(e).__name__}: {e}"

    try:
        store.close()
    except Exception:  # noqa: BLE001 - best-effort; already have our findings
        pass
    return obs


def check_tcsr_rebuild(store_dir: Path) -> dict[str, Any]:
    """`tcsr_file`-only fifth check: force a rebuild attempt (any traversal
    query touches `tcsr()`) and see whether the persisted index is now
    re-stamped for the *current* generation — the documented "corruption
    degrades to rebuild" contract (`tests/test_tcsr_persistence.py::
    test_a_damaged_file_degrades_to_rebuild`), not an error and not silence
    (the rebuilt index is correct, not a blend)."""
    import numpy as np
    import tgms
    from tgms.temporal.algebra import call_operator, ensure_all_registered

    ensure_all_registered()
    idx = native_dir(store_dir) / "index" / "tcsr.npz"
    try:
        store = tgms.open(store_dir, backend="native", read_only=True)
    except Exception as e:  # noqa: BLE001
        return {"rebuilt": False, "error": f"open failed: {type(e).__name__}: {e}"}
    try:
        call_operator(store.adapter, "temporal_paths", _TCSR_TOUCH_ARGS)
    except Exception as e:  # noqa: BLE001
        store.close()
        return {"rebuilt": False, "error": f"{type(e).__name__}: {e}"}
    rebuilt = False
    try:
        with np.load(idx) as z:
            rebuilt = int(z["generation"]) == store.adapter._store.generation()
    except Exception:  # noqa: BLE001 - absence/unreadable means "not rebuilt"
        rebuilt = False
    store.close()
    return {"rebuilt": rebuilt, "error": None}


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #


def explain_benign(cls: str, mut_info: dict[str, Any], build_meta: dict[str, Any]) -> str:
    if cls == "artifact_blob":
        # Task A10 (found here: this class's own `append_garbage` row was
        # 0/106 detected, verdict BENIGN, every digest unchanged — the
        # reasoning below used to be true unconditionally and is why).
        # `register_sample_artifacts` writes each plan blob before
        # registering it, so `Registry.register()` now stamps a
        # `blob_sha256` for it; under the default `verify_mode="full"`,
        # `NativeStoreAdapter.verify` walks the artifact registry
        # (`tgms.artifact.registry.verify` -> `_blob_defects`) and recomputes
        # that hash, so a byte-level mutation here is DETECTED at the
        # `obs["verify_problems"]` check in `classify()`, well before this
        # function is ever reached (`tests/test_eval_corruption.py::
        # test_artifact_blob_append_garbage_is_now_detected_via_full_verify`,
        # `::test_artifact_blob_flip_byte_is_now_detected_via_full_verify`).
        # This branch is live only under `--verify-mode fast`, which — per
        # its own A/B-comparison purpose — never calls
        # `artifact_registry.verify()` (or `check_artifact`, which by
        # design never opens a blob at all — `tgms/artifact/witness.py`'s
        # module docstring) at all, so a blob's own bytes still cannot
        # affect any observation that mode runs.
        return ("plan blobs are content-addressed by their own bytes since "
                "task A10 (`blob_sha256`) and a byte-level mutation here is "
                "DETECTED under the default verify_mode='full' before "
                "classify() ever reaches this function — reachable only "
                "under --verify-mode fast, which never calls "
                "artifact_registry.verify() at all")
    if cls == "event_log_tail" and mut_info["mutation"] in ("truncate", "append_garbage",
                                                            "flip_bit", "flip_byte"):
        return ("tail damage within the trim contract (D-086): the mutated "
                "record was never acknowledged, so recovery silently "
                "truncates it on the next writer open")
    file_rel = mut_info.get("file")
    if file_rel is not None and file_rel in build_meta.get("orphan_files", set()):
        # Reached only for a segment/close-run orphan under the default
        # verify_mode="full": an orphaned *manifest* generation is instead
        # DETECTED before classify() ever gets here — full mode's own
        # manifest-parent-chain check (added Lane A task A9) walks every
        # retained generation still on disk, not only the chain reachable
        # from CURRENT (tests/test_eval_corruption.py::
        # test_orphaned_manifest_generation_is_detected_under_full_verify).
        return ("the mutated file was superseded by this trial's own "
                "compact() before the mutation ran — no live manifest "
                "generation references it")
    if mut_info["mutation"] == "swap_same_class" and mut_info.get("note"):
        return mut_info["note"]
    return "no observation this harness runs reads the mutated byte range"


def classify(cls: str, mut_info: dict[str, Any], obs: dict[str, Any],
            baseline: dict[str, str], build_meta: dict[str, Any],
            tcsr: dict[str, Any] | None) -> tuple[str, str]:
    if not obs["open_ok"]:
        return "DETECTED", f"open refused: {obs['open_error']}"
    if obs["verify_problems"]:
        return "DETECTED", f"verify findings: {obs['verify_problems'][:3]}"
    if obs["query_errors"]:
        return "DETECTED", f"query refused: {obs['query_errors']}"
    if obs["artifact_errors"]:
        return "DETECTED", f"artifact check refused: {obs['artifact_errors']}"

    changed = [name for name, d in obs["query_digests"].items() if baseline.get(name) != d]
    if changed:
        return "SILENT", f"result digest changed for: {changed}"

    if cls == "tcsr_file" and tcsr is not None and tcsr["rebuilt"]:
        return "TOLERATED-REBUILT", "the persisted TCSR index was silently rebuilt"

    return "BENIGN", explain_benign(cls, mut_info, build_meta)


# --------------------------------------------------------------------------- #
# per-trial driver
# --------------------------------------------------------------------------- #


def derive_trial_seed(seed: int, trial: int) -> int:
    import hashlib
    digest = hashlib.sha256(f"corruption:{seed}:{trial}".encode()).hexdigest()
    return int(digest[:16], 16)


def run_trial(trial: int, seed: int, classes: list[str], mutations: list[str],
              verify_mode: str = "full") -> dict[str, Any]:
    trial_seed = derive_trial_seed(seed, trial)
    rng = random.Random(trial_seed)
    work = Path(tempfile.mkdtemp(prefix="tgms-corrupt-"))
    store_dir = work / "store"
    t0 = time.perf_counter()
    try:
        build_meta = build_store(store_dir, trial_seed)
        baseline = baseline_digests(store_dir)

        available = [c for c in classes if FILE_CLASSES[c](store_dir)]
        assert available, f"none of the requested classes have candidates: {classes}"
        cls = rng.choice(available)
        candidates = FILE_CLASSES[cls](store_dir)
        mutation = rng.choice(mutations)

        mut_info = apply_mutation(cls, mutation, candidates, rng, store_dir)
        obs = observe(store_dir, build_meta["artifact_names"], verify_mode=verify_mode)
        tcsr = check_tcsr_rebuild(store_dir) if cls == "tcsr_file" else None

        verdict, reason = classify(cls, mut_info, obs, baseline, build_meta, tcsr)

        return {
            "trial": trial, "seed": seed, "trial_seed": trial_seed,
            "class": cls, "mutation": mut_info["mutation"],
            "mutation_requested": mutation, "mutation_note": mut_info.get("note"),
            "file": mut_info.get("file"), "files": mut_info.get("files"),
            "offset": mut_info.get("offset"),
            "verdict": verdict, "reason": reason,
            "open_ok": obs["open_ok"], "open_error": obs["open_error"],
            "verify_problems": obs["verify_problems"],
            "query_errors": obs["query_errors"] or None,
            "query_digests_match": {n: (baseline.get(n) == d)
                                    for n, d in obs["query_digests"].items()},
            "artifact_errors": obs["artifact_errors"] or None,
            "tcsr_rebuilt": tcsr["rebuilt"] if tcsr else None,
            "wall_s": round(time.perf_counter() - t0, 2),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# manifest / CLI
# --------------------------------------------------------------------------- #


def _git_commit() -> str:
    env = os.environ.get("TGMS_COMMIT")
    if env:
        return env
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                                    capture_output=True, text=True,
                                    timeout=10).stdout.strip())
        return (sha + ("-dirty" if dirty else "")) if sha else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _machine_info() -> dict[str, Any]:
    ram_gb = 0.1
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
            if out:
                ram_gb = round(int(out) / (1024 ** 3), 1)
        else:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        ram_gb = round(int(line.split()[1]) / (1024 ** 2), 1)
                        break
    except Exception:  # noqa: BLE001
        pass
    return {"host": platform.node() or "unknown", "platform": platform.platform(),
            "cpus": os.cpu_count() or 1, "ram_gb": ram_gb}


def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    pair: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"trials": 0, "detected": 0})
    benign_by_class: dict[str, list[str]] = defaultdict(list)
    for r in results:
        key = (r["class"], r["mutation"])
        pair[key]["trials"] += 1
        if r["verdict"] == "DETECTED":
            pair[key]["detected"] += 1
        if r["verdict"] == "BENIGN":
            benign_by_class[r["class"]].append(r["reason"])
    matrix = {
        f"{c}|{m}": {"trials": v["trials"], "detected": v["detected"],
                     "detection_rate": round(v["detected"] / v["trials"], 4)}
        for (c, m), v in sorted(pair.items())
    }
    verdict_counts = {v: sum(1 for r in results if r["verdict"] == v)
                      for v in ("DETECTED", "SILENT", "BENIGN", "TOLERATED-REBUILT")}
    return {
        "n_trials": len(results),
        "verdict_counts": verdict_counts,
        "detection_matrix": matrix,
        "benign_reasons_by_class": {c: sorted(set(rs)) for c, rs in benign_by_class.items()},
        "silent_trials": [r for r in results if r["verdict"] == "SILENT"],
    }


def build_manifest(results: list[dict[str, Any]], summary: dict[str, Any], seed: int,
                   classes: list[str], mutations: list[str], record_path: str,
                   verify_mode: str = "full") -> dict[str, Any]:
    result_src = json.dumps(
        [{k: v for k, v in r.items() if k != "wall_s"} for r in results],
        sort_keys=True, separators=(",", ":")).encode()
    import hashlib
    return {
        "schema_version": SCHEMA_VERSION,
        "git_commit": _git_commit(),
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "machine": _machine_info(),
        "config": {"harness": "scripts/eval_corruption.py", "classes": classes,
                  "mutations": mutations, "verify_mode": verify_mode},
        "seed": {"value": seed},
        "dataset": {"name": "seeded synthetic workload (scripts/eval_durability.py "
                            "generator), one tiny store per trial",
                   "digest": hashlib.sha256(
                       json.dumps({"classes": classes, "mutations": mutations},
                                 sort_keys=True).encode()).hexdigest(),
                   "digest_kind": "manifest"},
        "result_digest": hashlib.sha256(result_src).hexdigest(),
        "protocol": {"warmups": 0, "reps": len(results), "ceilings": {}},
        "record": record_path,
        "summary": summary,
        "results": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--classes", default=",".join(FILE_CLASSES))
    ap.add_argument("--mutations", default=",".join(MUTATIONS))
    ap.add_argument("--verify-mode", choices=("fast", "full"), default="full",
                    help="strength of observation 2 (store.adapter.verify); "
                         "'full' (default) is the A3/A8 strongest-oracle mode, "
                         "'fast' reproduces the pre-A3 engine-file-only walk")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    classes = [c for c in args.classes.split(",") if c]
    mutations = [m for m in args.mutations.split(",") if m]
    unknown_c = [c for c in classes if c not in FILE_CLASSES]
    unknown_m = [m for m in mutations if m not in MUTATIONS]
    if unknown_c:
        ap.error(f"unknown class(es): {unknown_c}; known: {sorted(FILE_CLASSES)}")
    if unknown_m:
        ap.error(f"unknown mutation(s): {unknown_m}; known: {list(MUTATIONS)}")

    results = []
    for t in range(args.trials):
        r = run_trial(t, args.seed, classes, mutations, verify_mode=args.verify_mode)
        results.append(r)
        print(f"  trial {t:4d} {r['class']:20s} {r['mutation']:16s} -> "
              f"{r['verdict']:17s} {r['reason'][:90]}", flush=True)

    summary = build_summary(results)
    print(f"\n{summary['n_trials']} trials: " +
         " ".join(f"{k}={v}" for k, v in summary["verdict_counts"].items()))
    if summary["verdict_counts"]["SILENT"]:
        print("\nSILENT trials (must be zero) — full records:")
        for r in summary["silent_trials"]:
            print(f"  {json.dumps(r)}")

    if args.json:
        record_path = str(args.json)
        try:
            record_path = str(args.json.resolve().relative_to(ROOT))
        except ValueError:
            pass
        manifest = build_manifest(results, summary, args.seed, classes, mutations, record_path,
                                  verify_mode=args.verify_mode)
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(manifest, indent=1) + "\n")
        print(f"record → {args.json}")

    return 1 if summary["verdict_counts"]["SILENT"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

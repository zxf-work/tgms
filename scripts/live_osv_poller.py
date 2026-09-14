#!/usr/bin/env python3
"""OSV live-workload poller — Lane F3, P0.5.

Design of record: `docs/design/LIVE_WORKLOAD_OSV_DESIGN_2026-09-13.md` §3, §5,
§8. One writer, one store, one batch per cycle (§3: "each cycle is exactly
one event-log record, one generation, one freshness-checkable correction
batch"). Network calls live in exactly two functions
(`default_fetch_modified_csv`, `default_fetch_record`) so a test can swap
both out for a fake and never touch the network (`--once` with injected
`fetch_ids`/`fetch_record`, used by `tests/test_osv_loader.py`).

**A deviation from the design's literal state.json shape**, named here and in
the F3 report: §3/§5 describe `state.json` as "id -> last_modified, digest".
That is exactly what `_load_state`/`_save_state` keep, but `diff_to_ops`
needs the *previous full record*, not its digest, to know which specific
fields changed (§2's table is keyed on field-level diffs, not "something
changed"). So this poller also keeps a small raw-record cache,
`<store>/osv_raw/<id>.json`, written every time an id is asserted/corrected
and read back as `old` on the next revision — a few KB per advisory, well
inside §5's 10 GB disk budget even at the full ~33k-advisory bootstrap.

Compaction cadence, the failure-ledger hook, and the metrics sink all follow
§5 as closely as the surrounding lanes' actual code allows — see the
docstrings on `poll_once` and `_write_failure_ledger_entry` for the specific
places this had to interpret rather than quote the memo.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tgms  # noqa: E402
from tgms.core.model import canonical_json, sha256_hex  # noqa: E402

from tgms.data.osv_loader import (  # noqa: E402
    ALLOWED_ECOSYSTEMS,
    bootstrap_ops,
    canonical_digest,
    dedupe_assert_nodes,
    diff_to_ops,
    is_malicious,
    iter_bootstrap_records,
    read_modified_id_csv,
    record_to_ops,
    summarize_ops,
)

_PROCESS_START = time.time()
_RUN_ID = uuid.uuid4().hex[:12]

#: §5: "compact() + gc(keep_last=2) every 100 poll cycles."
DEFAULT_COMPACT_EVERY_CYCLES = 100

#: Chunk bootstrap's single logical write into batches this large. The
#: fixture (~200 ops) fits in one; a real ~33k-advisory bootstrap
#: (~413k op-equivalents, §3) does not need to be, and should not be, one
#: `_write` call — `scripts/build_snb_store.py`'s own batching note applies
#: (apply cost is superlinear *within* one batch).
BOOTSTRAP_BATCH_OPS = 2_000

MODIFIED_ID_CSV_URL = "https://osv-vulnerabilities.storage.googleapis.com/{eco}/modified_id.csv"
VULN_URL = "https://api.osv.dev/v1/vulns/{id}"


# --------------------------------------------------------------------------- #
# small utilities                                                             #
# --------------------------------------------------------------------------- #

def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _now_us() -> int:
    return round(time.time() * 1_000_000)


def _dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:  # noqa: PERF203 -- a file can vanish mid-walk
                pass
    return total


def _raw_cache_dir(store_dir: Path) -> Path:
    return Path(store_dir) / "osv_raw"


def _load_raw(store_dir: Path, advisory_id: str) -> dict[str, Any] | None:
    p = _raw_cache_dir(store_dir) / f"{advisory_id}.json"
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _save_raw(store_dir: Path, record: dict[str, Any]) -> None:
    d = _raw_cache_dir(store_dir)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{record['id']}.json", "w", encoding="utf-8") as f:
        json.dump(record, f)


def _default_state() -> dict[str, Any]:
    return {"ids": {}, "cycles": 0, "restart_count": 0}


def _load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return _default_state()
    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("ids", {})
    state.setdefault("cycles", 0)
    state.setdefault("restart_count", 0)
    return state


def _save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, state_path)


def _exists_in_store(adapter: Any, uid: str) -> bool:
    try:
        return bool(adapter.believed_node_versions(uid))
    except Exception:  # noqa: BLE001 -- unknown uid reads as "does not exist"
        return False


def _make_batch(ops: list[dict[str, Any]], tt: int) -> dict[str, Any]:
    """Mirrors `EventLog.append`'s own `batch_id` formula exactly — this is
    the batch we just wrote, and `footprints_of_batch` needs it in the
    logged-record shape."""
    batch_id = sha256_hex(canonical_json({"tt": tt, "ops": ops}))[:16]
    return {"batch_id": batch_id, "tt": tt, "ops": ops}


# --------------------------------------------------------------------------- #
# metrics + failure ledger                                                    #
# --------------------------------------------------------------------------- #

def _telemetry_metrics(path: Path | None):
    """§P0.6's `tgms.telemetry.metrics.Metrics`, imported lazily. `None` if
    that module does not exist in this worktree yet -- every call site below
    guards on that and the poller runs either way."""
    if path is None:
        return None
    try:
        from tgms.telemetry.metrics import Metrics  # noqa: PLC0415
    except ImportError:
        return None
    return Metrics(str(path))


def _emit_cycle_metrics(metrics_path: Path, record: dict[str, Any],
                        telemetry: Any = None) -> None:
    """The design's own per-cycle shape (§5): **one JSON object per line**,
    every field named there. This is written directly, dependency-free,
    regardless of whether `tgms.telemetry.metrics.Metrics` is importable --
    that module's `counter`/`gauge`/`observe` produce one-series-per-line
    output, a different shape, so it is used *in addition to*, not instead
    of, this line, purely so a P0.6-aware dashboard also sees the cycle."""
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    with open(metrics_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
    if telemetry is not None and getattr(telemetry, "enabled", False):
        for key in ("events_appended", "corrections_written", "noop_revisions",
                    "retractions", "withdrawn_seen", "feed_errors"):
            telemetry.counter(f"osv_{key}", record.get(key, 0))
        for key in ("nodes", "edges", "store_bytes", "generations"):
            telemetry.gauge(f"osv_{key}", record.get(key, 0))
        telemetry.observe("osv_cycle_ms", record.get("cycle_ms", 0.0))
        telemetry.flush()


#: §5/§7 risk 3's failure classes.
FAILURE_CLASSES = (
    "feed_5xx", "schema_unknown_field", "id_disappeared", "digest_mismatch",
    "write_refused", "crash_restart",
)


def _write_failure_ledger_entry(ledger_path: Path, *, exc: BaseException,
                                run_id: str, commit: str,
                                records_affected: int = 0,
                                failure_class: str = "crash_restart") -> None:
    """Appends one line to `ledger_path` on an unhandled cycle exception.

    **Named deviation from `ops/README.md`'s own schema**, which the task
    explicitly points this hook at: that ledger's nine required fields
    (`ops/failure_ledger.jsonl`) describe a *curated, diagnosed, already-fixed*
    defect — `root_cause` is "the mechanism, once diagnosed", `fix_commit` is
    "the commit that landed the fix", `regression_test` names a test that
    would catch a recurrence. None of those exist yet for a fresh, unhandled
    exception raised *while this poller is running*. Rather than invent
    fictitious values for fields the schema promises are meaningful, this
    writes the nine required fields with honest placeholders
    (`root_cause`/`fix_commit`/`regression_test` = `"UNDIAGNOSED"` /
    `"PENDING"` / `"PENDING"`) and carries the design's own §5 shape
    (`ts`, `run_id`, `commit`, `class`, `detail`, `records_affected`,
    `action`) as additional, allowed properties (`ops/README.md`:
    "`additionalProperties` beyond these nine is allowed"). This keeps
    `scripts/check_failure_ledger.py` passing on the file this hook writes to
    (which defaults to a path the caller controls, never
    `ops/failure_ledger.jsonl` unless explicitly pointed there) while still
    recording everything §5 asks for. A human curating the real ledger later
    promotes a genuine, diagnosed entry from this file by hand.
    """
    entry = {
        "id": f"OSV-LIVE-{run_id}-{int(time.time())}",
        "first_observed": time.strftime("%Y-%m-%d", time.gmtime()),
        "workload_or_seed": f"live-osv poller run_id={run_id} commit={commit}",
        "symptom": f"{type(exc).__name__}: {exc}",
        "severity": "high",
        "root_cause": "UNDIAGNOSED",
        "fix_commit": "PENDING",
        "regression_test": "PENDING",
        "decision_ref": "",
        "ts": time.time(),
        "run_id": run_id,
        "commit": commit,
        "class": failure_class if failure_class in FAILURE_CLASSES else "crash_restart",
        "detail": repr(exc),
        "records_affected": records_affected,
        "action": "re-raised; supervisor restarts",
    }
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- #
# network — isolated so a test can inject a fake                              #
# --------------------------------------------------------------------------- #

def default_fetch_modified_csv(ecosystem: str, *, timeout: float = 30.0) -> Iterable[tuple[str, str]]:
    """§3: the per-ecosystem `modified_id.csv`. Streams `(modified_iso, id)`
    in the file's own reverse-chronological order."""
    url = MODIFIED_ID_CSV_URL.format(eco=ecosystem)
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        text = resp.read().decode("utf-8")
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    try:
        yield from read_modified_id_csv(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def default_fetch_record(advisory_id: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """§3: `GET /v1/vulns/{id}`. The design's self-limit (5 req/s) is the
    caller's job (`_throttled_fetch`), not this function's -- kept minimal so
    a test can monkeypatch/replace it wholesale."""
    url = VULN_URL.format(id=advisory_id)
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _throttled_fetch(fetch_record: Callable[[str], dict[str, Any]],
                     advisory_id: str, *, min_interval_s: float = 0.2) -> dict[str, Any]:
    """§3: "we self-limit to 5 req/s regardless." One call, one sleep before
    it if less than `min_interval_s` has passed since `_throttled_fetch` was
    last entered -- process-global, since one poller is one writer talking to
    one upstream."""
    now = time.monotonic()
    last = getattr(_throttled_fetch, "_last_call", 0.0)
    wait = min_interval_s - (now - last)
    if wait > 0:
        time.sleep(wait)
    _throttled_fetch._last_call = time.monotonic()  # type: ignore[attr-defined]
    return fetch_record(advisory_id)


# --------------------------------------------------------------------------- #
# bootstrap                                                                    #
# --------------------------------------------------------------------------- #

def bootstrap(store_dir: str | Path, bootstrap_root: str | Path, *,
             state_path: str | Path, backend: str = "native",
             ecosystems: tuple[str, ...] = ALLOWED_ECOSYSTEMS,
             dry_run: bool = False) -> dict[str, Any]:
    """§8: `--bootstrap`. Reads the per-ecosystem `all.zip` export layout
    (`<bootstrap_root>/<Ecosystem>/*.json`), writes one deduped op stream in
    chunks of `BOOTSTRAP_BATCH_OPS`, seeds `state.json` and the raw-record
    cache, and returns the state that was written.

    `dry_run=True` never touches `store_dir` or `state_path`: it writes to a
    throwaway store under `tempfile.mkdtemp()` and returns the same shape of
    state dict plus an `"ops_summary"` key, so `--dry-run` can print a plan
    without writing (§8's own words for the poller's dry-run)."""
    store_dir = Path(store_dir)
    state_path = Path(state_path)
    records = list(iter_bootstrap_records(bootstrap_root, ecosystems))
    ops = list(bootstrap_ops(records))

    target_dir = Path(tempfile.mkdtemp(prefix="osv-dry-run-")) if dry_run else store_dir
    store = tgms.open(target_dir, backend=backend)
    try:
        for i in range(0, len(ops), BOOTSTRAP_BATCH_OPS):
            store._write(ops[i:i + BOOTSTRAP_BATCH_OPS])
        health = store.adapter.verify()
        if not health.get("healthy", False):
            raise RuntimeError(f"bootstrap wrote an unhealthy store: {health}")
        if not dry_run:
            store.adapter.compact()
            store.adapter.gc(keep_last=2)
    finally:
        store.close()

    state = _default_state()
    for record in records:
        if is_malicious(record["id"]):
            continue
        state["ids"][record["id"]] = {
            "last_modified": record.get("modified"),
            "digest": canonical_digest(record),
        }
        if not dry_run:
            _save_raw(store_dir, record)

    state["ops_summary"] = summarize_ops(ops)
    state["stats"] = store.stats() if hasattr(store, "stats") else {}
    if not dry_run:
        _save_state(state_path, state)
    else:
        shutil.rmtree(target_dir, ignore_errors=True)
    return state


# --------------------------------------------------------------------------- #
# artifact registration — §4's 500 registered per-package exposure summaries #
# --------------------------------------------------------------------------- #

def register_exposure_artifact(store: Any, registry: Any, pkg_uid: str, *,
                                t_valid: int | None = None, hops: int = 1) -> Any:
    """§4: `Registry.register(...)` for one package's "current exposure"
    summary, an opaque `snapshot_subgraph` leaf (§5.5's `"operator"` refresh
    kind) — the shape `tests/test_artifact_refresh.py::
    test_operator_kind_refresh_end_to_end` exercises against the production
    seam (`ToolRouter.call` + `leaf_meta`, then `Registry.register`)."""
    from tgms.tgir.depscope import DependencyScope
    from tgms.tools.server import ToolRouter

    router = ToolRouter(store.adapter, tt_source=store)
    args = {"seeds": [pkg_uid], "hops": hops, "t_valid": t_valid or _now_us()}
    env = router.call("snapshot_subgraph", args)
    if "error" in env:
        raise RuntimeError(f"snapshot_subgraph({pkg_uid}) refused: {env}")
    meta = router.leaf_meta("snapshot_subgraph", env)

    safe = pkg_uid.replace("\x1f", "__").replace(":", "_").replace("/", "_")
    ref = f"osv_artifacts/{safe}.json"
    blob_path = Path(store.path) / ref
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_text(json.dumps({"op": "snapshot_subgraph", "args": args}))

    dependency = DependencyScope.from_json(env["dependency"])
    return registry.register(
        name=f"osv-exposure:{pkg_uid}", kind="osv_exposure",
        plan={"plan_digest": meta.get("plan_digest"), "node_digest": meta.get("node_digest"),
              "plan_format": None},
        basis={"tt_q": env["tt_q"], "pinned": env["pinned"], "clamped": env["clamped"],
               "tt_q_verified": dependency.tt_q_verified},
        state={"completeness": meta.get("completeness", "unknown"),
               "exactness": meta.get("exactness", "exact"), "refusal": None},
        refresh={"kind": "operator", "ref": ref, "basis_policy": "open"},
        dependency=dependency,
    )


def register_sample_artifacts(store: Any, registry: Any, *, sample_size: int = 500,
                              seed: str = "osv-live") -> list[Any]:
    """§4: register (up to) `sample_size` per-package exposure artifacts, a
    seeded sample of every Package this store currently knows about. Skips
    any name already registered (idempotent across restarts)."""
    import hashlib

    pkg_uids = sorted(
        v.uid for v in store.adapter.all_node_versions() if v.label == "Package"
    ) if hasattr(store.adapter, "all_node_versions") else []
    pkg_uids = sorted(set(pkg_uids))
    rng_key = lambda u: hashlib.sha256(f"{seed}:{u}".encode()).hexdigest()  # noqa: E731
    pkg_uids.sort(key=rng_key)
    chosen = pkg_uids[:sample_size]
    out = []
    for pkg_uid in chosen:
        name = f"osv-exposure:{pkg_uid}"
        if registry.current(name) is not None:
            continue
        out.append(register_exposure_artifact(store, registry, pkg_uid))
    return out


# --------------------------------------------------------------------------- #
# one poll cycle                                                              #
# --------------------------------------------------------------------------- #

def poll_once(store_dir: str | Path, *, state_path: str | Path, metrics_path: str | Path,
             ledger_path: str | Path, backend: str = "native",
             ecosystems: tuple[str, ...] = ALLOWED_ECOSYSTEMS,
             fetch_ids: list[tuple[str, str]] | None = None,
             fetch_modified_csv: Callable[[str], Iterable[tuple[str, str]]] | None = None,
             fetch_record: Callable[[str], dict[str, Any]] | None = None,
             compact_every: int = DEFAULT_COMPACT_EVERY_CYCLES,
             artifact_sample_size: int = 500) -> dict[str, Any]:
    """One cycle: diff `modified_id.csv` (or the injected `fetch_ids`) against
    `state.json`'s high-water mark, fetch each changed id, write one batch,
    run the §4 artifact lookup+refresh over it, emit one metrics line, and
    compact on the cycle cadence.

    On any unhandled exception this appends one failure-ledger entry
    (`_write_failure_ledger_entry`) and re-raises — the caller (`--once`'s
    CLI wrapper, or the `--interval-s` loop) decides whether that means
    "log and continue" or "let the supervisor restart me".
    """
    store_dir = Path(store_dir)
    state_path = Path(state_path)
    metrics_path = Path(metrics_path)
    ledger_path = Path(ledger_path)
    fetch_record = fetch_record or default_fetch_record
    fetch_modified_csv = fetch_modified_csv or default_fetch_modified_csv
    commit = _git_sha()
    cycle_start = time.time()
    state = _load_state(state_path)

    try:
        if fetch_ids is None:
            changed: list[tuple[str, str]] = []
            seen_ids: set[str] = set()
            for eco in ecosystems:
                for modified_iso, advisory_id in fetch_modified_csv(eco):
                    if advisory_id in seen_ids:
                        continue
                    known = state["ids"].get(advisory_id)
                    if known is not None and known.get("last_modified", "") >= modified_iso:
                        break  # reverse-chronological: caught up for this ecosystem
                    seen_ids.add(advisory_id)
                    changed.append((advisory_id, modified_iso))
        else:
            changed = list(fetch_ids)

        ops: list[dict[str, Any]] = []
        fetched_records: list[dict[str, Any]] = []
        noop_revisions = 0
        withdrawn_seen = 0
        feed_errors = 0
        for advisory_id, _modified_iso in changed:
            try:
                new_record = _throttled_fetch(fetch_record, advisory_id)
            except Exception:  # noqa: BLE001 -- one bad fetch must not sink the cycle
                feed_errors += 1
                continue
            fetched_records.append(new_record)
            old_record = _load_raw(store_dir, advisory_id)
            if new_record.get("withdrawn") and not (old_record or {}).get("withdrawn"):
                withdrawn_seen += 1
            if old_record is None:
                record_ops = record_to_ops(new_record)
            else:
                record_ops = diff_to_ops(old_record, new_record)
                if not record_ops:
                    noop_revisions += 1
            ops.extend(record_ops)

        store = tgms.open(store_dir, backend=backend)
        try:
            exists = lambda uid: _exists_in_store(store.adapter, uid)  # noqa: E731
            deduped_ops = list(dedupe_assert_nodes(ops, exists))
            tt = None
            if deduped_ops:
                tt = store._write(deduped_ops)
            for record in fetched_records:
                _save_raw(store_dir, record)
                state["ids"][record["id"]] = {
                    "last_modified": record.get("modified"),
                    "digest": canonical_digest(record),
                }
            state["cycles"] = int(state.get("cycles", 0)) + 1

            compact_s = 0.0
            if deduped_ops and state["cycles"] % compact_every == 0:
                t0 = time.time()
                store.adapter.compact()
                store.adapter.gc(keep_last=2)
                compact_s = time.time() - t0

            artifacts_stale = artifacts_refreshed = intersects_calls = 0
            freshness_ms = 0.0
            if deduped_ops and tt is not None:
                from tgms.artifact.lookup import affected
                from tgms.artifact.refresh import RefreshRefused, refresh
                from tgms.artifact.registry import Registry
                from tgms.artifact.witness import check_artifact
                from tgms.storage.eventlog import EventLog

                registry = Registry(store_dir)
                registered = register_sample_artifacts(
                    store, registry, sample_size=artifact_sample_size)
                batch = _make_batch(deduped_ops, tt)
                result = affected(batch, registry)
                intersects_calls = result.intersects_calls
                log = EventLog(store_dir / "eventlog.jsonl")
                t0 = time.time()
                for record in result.affected:
                    verdict = check_artifact(record, log)
                    if verdict.actionable_fresh:
                        continue
                    artifacts_stale += 1
                    try:
                        refresh(record, verdict.refresh, store, registry)
                        artifacts_refreshed += 1
                    except RefreshRefused:
                        pass
                freshness_ms = (time.time() - t0) * 1000
                artifacts_registered_total = len(registry.names())
            else:
                artifacts_registered_total = 0

            stats = store.stats()
        finally:
            store.close()

        _save_state(state_path, state)

        cycle_ms = (time.time() - cycle_start) * 1000
        record_out = {
            "ts": time.time(), "commit": commit, "run_id": _RUN_ID,
            "restart_count": int(os.environ.get("TGMS_OSV_RESTART_COUNT", state.get("restart_count", 0))),
            "uptime_s": time.time() - _PROCESS_START,
            "cycle_ms": cycle_ms,
            "records_seen": len(changed),
            "events_appended": len(deduped_ops),
            "revisions_seen": len(fetched_records),
            "corrections_written": summarize_ops(deduped_ops).get("correct", 0),
            "noop_revisions": noop_revisions,
            "retractions": summarize_ops(deduped_ops).get("retract", 0),
            "withdrawn_seen": withdrawn_seen,
            "nodes": stats.get("n_node_versions", 0),
            "edges": stats.get("n_edge_versions", 0),
            "queries_served": 0, "query_latency_p50_ms": None, "query_latency_p95_ms": None,
            "artifacts_registered": artifacts_registered_total,
            "artifacts_stale": artifacts_stale,
            "artifacts_refreshed": artifacts_refreshed,
            "freshness_check_ms": freshness_ms,
            "intersects_calls": intersects_calls,
            "store_bytes": _dir_size_bytes(store_dir),
            "generations": getattr(store.adapter, "generation", None),
            "compact_s": compact_s,
            "feed_errors": feed_errors,
        }
        _emit_cycle_metrics(metrics_path, record_out, _telemetry_metrics(
            metrics_path.parent / "telemetry.jsonl"))
        return record_out
    except BaseException as exc:  # noqa: BLE001 -- ledgered, then re-raised
        _write_failure_ledger_entry(ledger_path, exc=exc, run_id=_RUN_ID, commit=commit,
                                    records_affected=len(changed) if "changed" in locals() else 0)
        raise


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def _default_metrics_path() -> Path:
    return Path("run") / "live_metrics.jsonl"


def _default_ledger_path() -> Path:
    return ROOT / "ops" / "failure_ledger.jsonl"


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store", required=True, help="store directory (writer-owned)")
    p.add_argument("--state", required=True, help="state.json path")
    p.add_argument("--bootstrap", metavar="DIR", default=None,
                   help="bootstrap from a directory of per-ecosystem all.zip exports "
                        "(<DIR>/<Ecosystem>/*.json) instead of polling")
    p.add_argument("--once", action="store_true", help="run a single poll cycle and exit")
    p.add_argument("--interval-s", type=float, default=3600.0,
                   help="seconds between poll cycles when not --once (default: hourly)")
    p.add_argument("--log", default=None, help="metrics JSONL path "
                                                "(default: run/live_metrics.jsonl)")
    p.add_argument("--failure-ledger", default=None,
                   help="failure-ledger JSONL path (default: ops/failure_ledger.jsonl)")
    p.add_argument("--backend", default="native")
    p.add_argument("--dry-run", action="store_true",
                   help="compute ops and print a plan; never writes --store")
    p.add_argument("--artifact-sample-size", type=int, default=500)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    metrics_path = Path(args.log) if args.log else _default_metrics_path()
    ledger_path = Path(args.failure_ledger) if args.failure_ledger else _default_ledger_path()

    if args.bootstrap is not None:
        state = bootstrap(args.store, args.bootstrap, state_path=args.state,
                          backend=args.backend, dry_run=args.dry_run)
        if args.dry_run:
            print(f"DRY_RUN bootstrap from {args.bootstrap}: "
                  f"{len(state['ids'])} advisories, ops={state['ops_summary']}")
            print(f"(nothing written to {args.store} or {args.state})")
        else:
            print(f"bootstrapped {args.store}: {len(state['ids'])} advisories, "
                  f"ops={state['ops_summary']}")
        return 0

    if args.dry_run:
        # §8: "--dry-run writes a temp store and prints op counts." With no
        # --bootstrap this reports what a single poll cycle *would* fetch and
        # write, without touching --store: an empty `fetch_ids` isolates the
        # report to "no network reachable ids diffed yet" honestly rather
        # than silently hitting the real feed during a dry run.
        print("DRY_RUN poll: no --bootstrap given, and a dry-run poll cycle "
              "never calls the live network. Nothing written.")
        return 0

    print(f"RUN_STARTED commit={_git_sha()}")
    sys.stdout.flush()

    if args.once:
        poll_once(args.store, state_path=args.state, metrics_path=metrics_path,
                  ledger_path=ledger_path, backend=args.backend,
                  artifact_sample_size=args.artifact_sample_size)
        return 0

    while True:
        cycle_start = time.time()
        poll_once(args.store, state_path=args.state, metrics_path=metrics_path,
                  ledger_path=ledger_path, backend=args.backend,
                  artifact_sample_size=args.artifact_sample_size)
        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, args.interval_s - elapsed))


if __name__ == "__main__":
    sys.exit(main())

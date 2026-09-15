"""The longevity (soak) harness — M5 execution plan §7 P4.5, "the phase's
centerpiece" (`docs/design/M5_EXECUTION_PLAN_2026-08-27.md`); Gate G1's 24h
run and Phase 4's 72h run in the OSDI plan.

No prior harness runs the write path for hours: `scripts/eval_durability.py`
crashes a process at one instrumented boundary per trial on a small store;
`scripts/eval_concurrency.py mixed` runs a writer against readers for tens of
seconds; `scripts/build_snb_store.py` runs long but never crashes itself on
purpose and never checks freshness while writing. This harness is all of
those, sustained: a seeded append+correction stream, concurrent readers,
periodic `compact()`+`gc()`, artifact freshness checks tied to the
correction stream, and periodic crash/restart cycles — long enough (hours,
not seconds) that slow leaks and superlinear growth have room to show up
before the run ends rather than after.

**Process layout.**

- One **writer** process: opens the store read-write once per "life" (a life
  ends when the process dies, by design at a restart cycle, or by accident);
  submits a seeded append+correction stream through
  `tgms.write.GroupCommitWriter` (one submitter — the main loop — so this
  run's own commits are not concurrent enough to coalesce; the writer's
  own generation-per-batch behaviour is therefore the same as calling
  `Store` directly, and `GroupCommitWriter` is used anyway so the writer's
  commit path is the one public surface built for this role, and so
  `.stats()` — commits/submissions/max_group/solo_fallbacks — comes for
  free in the final report); runs `compact()`+`gc(keep_last=2)` every
  `--compact-every-batches` batches; and — see the limitation below — also
  runs the **artifact checker/refresher as a background thread inside
  itself**, sharing its one `Store` handle.
- N **reader** processes: `read_only=True`, looping the §14.4 query mix
  (`docs/eval_concurrency.md`), each recording its own per-minute
  p50/p95/p99. The orchestrator restarts one that dies unexpectedly and
  counts the restart (never treated as a run failure by itself).
- The **orchestrator** (this process, when not `_child ...`): copies
  `--store` into the run directory, spawns the above, drives the restart
  cycle by dropping a "please crash" instruction the writer polls between
  batches, waits out `--duration`, then does the final `verify()` +
  replay/digest-equivalence check and writes the result manifest.

**Why the checker is a thread, not a process (the plan's own text says
"a checker process").** `tgms.artifact.refresh.refresh` needs a *live*,
read-write `Store` handle (`tgms/artifact/refresh.py`'s own module
docstring: refresh is the one place in the artifact package that opens one).
Two operating-system processes each holding a read-write handle on the same
store directory is explicitly undefined behaviour here (D-028;
`docs/eval_concurrency.md` §22: "multi-process writers ... remain undefined
by design") — there is no cross-process lock file, only the in-process
`threading.Lock` `Store._write_locked` takes, which exists *specifically*
so several threads in one process can share one handle safely
(`tgms/store.py`'s own comment on `_write_lock`). Running the checker as a
second OS process would therefore need its own store handle and would be
exactly the undefined configuration the design refuses to define. Running it
as a thread inside the writer process, sharing the writer's one handle,
gets the same coverage (checks tied to the correction stream, refreshes
timed) without asking the engine to define something it deliberately does
not.

**The final replay step inherits D-149's own pathology, and (B7c,
2026-09-15) now has a compaction hook to bound it.** `tgms.storage.
eventlog.replay` — the same call `tgms replay` makes — applies every
logged batch into a fresh store, one generation per batch by default. For
a writer that commits many small (near-single-op) batches over a long run,
replaying the full log this way (uncompacted) reproduces the exact
O(batches²) manifest-growth pathology `scripts/build_snb_store.py:70-90`
measured (10,147 uncompacted generations -> 25 GB of manifests) — found
here by running the *verification step itself* long enough, which is very
much the point of this harness. `replay(..., compact_every=N)` now calls
`adapter.compact()`+`adapter.gc(keep_last=2)` every `N` applied batches,
safe by construction (compaction inherits the pre-compaction generation's
`created_tt` unchanged — `crates/tgms-engine-core/src/compact.rs` — so it
cannot disturb the historical `tt` values `replay` applies each batch at,
which is the actual constraint a *public-API* reimplementation of `replay`
still cannot get around — see `apply_correction_via_public_api`'s note on
that separate, still-standing constraint). This resets the accumulated
live-segment count — and with it the O(k²) manifest cost — every `N`
batches instead of letting it grow for the whole run; `cmd_run` below
passes this run's own `--compact-every-batches` as the replay's cadence by
default (`--replay-compact-every` overrides, `0` disables and falls back
to the old uncompacted replay) and projects the disk-guard cost as
`243.0 * min(compact_every, total_batches) ** 2 / 1e6` MB — the peak size
*within one compaction cycle*, not `243.0 * total_batches ** 2 / 1e6` —
since periodic compaction+gc reclaims each cycle's growth before the next
one starts rather than letting it accumulate across the whole run (see
`cmd_run`'s own comment where this is derived). `--writer-sleep-s`
overrides a mix's per-batch throttle so an operator can keep the total
batch count down independently of this.

**Mixture presets, and what they do and do not control.** The blueprint
mixture (60% read/query, 25% append, 10% historical correction, 5%
artifact/freshness ops) describes proportions of *operations across the
whole system*, but the system is heterogeneous processes with very
different per-op costs (a query is microseconds to milliseconds; a
group-commit publishes a generation). This harness does not attempt to hit
the literal global percentage — instead each preset sets (a) the
append:correction ratio *within the writer's own write stream* and (b) an
optional per-batch sleep that throttles the writer's duty cycle relative to
the (unthrottled) readers, which is the closest a harness with independent
processes can come to shifting the balance toward reads. Artifact/freshness
ops are not throttled independently at all: they ride the correction
stream, one checker pass per correction batch, which is the P4.5 text's own
phrasing ("freshness checks while writing" / "after each correction
batch") — so the 5% is not a literal rate but a structural coupling. This
is documented here, not silently approximated.

**The 38 GB local incident.** An earlier local run (`--store
stores/synth-300k --duration 10m --compact-every-batches 100 --readers 2`,
no `--writer-sleep-s`) filled the disk. Two things are true about it, and
this harness now guards against both rather than picking one story:

1. **Directly confirmed by measurement, not inferred.** This module's own
   long-standing docstring already named the final replay step's
   O(batches^2) manifest growth as D-149's inherited pathology
   (`scripts/build_snb_store.py:70-90`: 10,147 uncompacted generations ->
   25 GB). A small, bounded probe against a throwaway few-thousand-node
   store (single process, no readers, nothing this harness's own
   `--max-disk-mb` guard would have let run unbounded) reproduced the same
   shape directly: ~1,200 single-op batches, replayed uncompacted, cost
   ~350 MB of manifests on a store whose *compacted* footprint is under
   1 MB. A 10-minute unthrottled writer easily commits an order of
   magnitude more batches than that (the doc's own citation: fsync alone
   floors a single-writer commit around 5.8-35 ms depending on host, i.e.
   tens of commits/second sustained), which lands squarely in the tens-of-
   GB range this incident actually measured. This is now guarded twice: a
   projected-size check from the real batch count before the replay step
   even starts (skipping it rather than paying for it when the projection
   would exceed `--max-disk-mb`), and a `compact()`+`gc(keep_last=1)` on
   the throwaway replay store immediately after replay, before hashing it —
   safe because `store_digest()` is defined purely over logical content
   (`tgms/storage/base.py`), so compaction cannot change the answer, only
   the disk this harness leaves behind holding it.
2. **Plausible, not confirmed, and guarded against anyway.** The
   incident's own log shows the process dying *inside* the soak, before
   the first restart cycle and before the replay step's own print lines —
   i.e. before item 1 above had a chance to run at all — so something was
   already consuming disk during the live soak. `docs/eval_concurrency.md`'s
   own "three more races" section documents that the native engine's
   generation-pin table is *in-process only* ("no cross-process reader
   registry, deliberately"): a `read_only=True` reader in a **separate OS
   process** (this harness's own design: N reader *processes*, not
   threads, each opening its handle once and keeping it for the reader's
   entire life) is invisible to the writer process's `gc()`, and POSIX does
   not free a file's disk blocks on `unlink()` while another process still
   holds it open. A small same-process probe (one long-held reader handle,
   60 compaction cycles) did *not* reproduce runaway growth — but the
   in-process pin table is exactly what would prevent that in a
   single-process test, so it does not rule out the cross-process case,
   which this harness cannot safely reproduce locally (it would require
   deliberately growing a store past the point gc can bound it, which is
   the disk-filling failure mode itself). Guarded against without needing
   to resolve which story is true: a wall-clock floor between compactions
   (`--compact-min-interval-s`, on top of `--compact-every-batches`, so a
   fast commit loop cannot cycle compactions faster than a once-per-run
   reader refresh could ever catch up) and a periodic reader handle
   refresh (`--reader-reopen-every-s`: close and reopen `read_only=True` on
   the same store path) that bounds how long any one reader can keep a
   stale generation's files open regardless of compaction cadence.

Underneath both: a `--max-disk-mb` guard checked periodically through the
whole soak (not only before replay) aborts the run cleanly — a
`longevity_ledger.jsonl` entry, children killed, no traceback — the moment
`--out` crosses the line, which is the actual backstop for whatever this
reasoning gets wrong.

    python scripts/longevity_run.py --store stores/synth-300k --duration 10m \\
        --mix balanced --readers 2 --compact-every-batches 50 \\
        --restart-every 2m --artifacts 5 --seed 0 --out runs/longevity-dev \\
        --metrics runs/longevity-dev/metrics.jsonl --max-disk-mb 2048

    python scripts/longevity_run.py --store stores/synth-1m --duration 24h \\
        --mix balanced --readers 8 --compact-every-batches 500 \\
        --restart-every 30m --artifacts 20 --seed 0 --out runs/g1-24h \\
        --metrics runs/g1-24h/metrics.jsonl --max-disk-mb 51200
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parents[1]

import eval_durability as DUR  # noqa: E402 — the ten boundaries, not reimplemented
import check_result_manifest as CRM  # noqa: E402 — validates our own manifest

SCHEMA_VERSION = "1.0.0"

# --------------------------------------------------------------------------- #
# small shared helpers                                                        #
# --------------------------------------------------------------------------- #


def parse_duration(s: str) -> float:
    """`"90s"`, `"20m"`, `"24h"`, `"3d"`, or a bare number of seconds."""
    s = str(s).strip().lower()
    units = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    if s and s[-1] in units and s[:-1]:
        try:
            return float(s[:-1]) * units[s[-1]]
        except ValueError:
            pass
    return float(s)


#: Append:correction ratio within the writer's own stream, plus a per-batch
#: sleep that throttles the writer relative to the (unthrottled) readers —
#: see the module docstring on what this does and does not control.
MIXES: dict[str, dict[str, float]] = {
    "balanced":         dict(append=25, correction=10, artifact=5, read=60, writer_sleep_s=0.0),
    "read-heavy":       dict(append=12, correction=5,  artifact=3, read=80, writer_sleep_s=0.05),
    "correction-heavy": dict(append=20, correction=35, artifact=5, read=40, writer_sleep_s=0.0),
}

#: Every boundary the durability instrument knows, reused rather than
#: reinvented (`scripts/eval_durability.py`). MAINT_BOUNDARIES need a
#: compact()/gc() call to fire instead of a plain write.
ALL_BOUNDARIES = list(DUR.ALL)
MAINT_BOUNDARIES = set(DUR.MAINT_BOUNDARIES)


def pctl(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))]


def dist(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {"n": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0}
    return {"n": len(xs), "p50": round(pctl(xs, 0.50), 3),
            "p95": round(pctl(xs, 0.95), 3), "p99": round(pctl(xs, 0.99), 3)}


def _vm_status() -> dict[str, int]:
    """VmRSS/VmHWM in kB, Linux only (`/proc/self/status`) — empty
    elsewhere, exactly `scripts/eval_resources.py::_vm_status`'s contract,
    reused rather than reimplemented differently."""
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                k, v = line.split(":", 1)
                out[k.lower() + "_kb"] = int(v.split()[0])
    except OSError:
        pass
    return out


def _disk_bytes_by_kind(store_dir: Path) -> dict[str, int]:
    """Bucket every file under a store directory by what it is, from the
    on-disk layout alone (a filesystem walk — the same technique
    `scripts/eval_concurrency.py::cmd_commitcost` uses for `store_bytes`,
    not a call into engine internals). The native backend's layout, observed
    directly (`native/seg/*.tgs`, `native/manifests/*.json`, `native/CURRENT`,
    `native/dict.log`, `eventlog.jsonl`, `artifacts.jsonl`, `plans/*.json`):
    there is no on-disk TCSR file — it is an in-process resident index
    (D-045) — so that bucket is always 0 here, not a missing measurement.
    """
    kinds = {"segments": 0, "manifests": 0, "log": 0, "dict": 0,
             "registry": 0, "plans": 0, "tcsr": 0, "other": 0}
    if not store_dir.exists():
        return kinds
    for p in store_dir.rglob("*"):
        if not p.is_file():
            continue
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        name = p.name
        parts = p.relative_to(store_dir).parts
        if name == "eventlog.jsonl":
            kinds["log"] += sz
        elif name.endswith(".tgs"):
            kinds["segments"] += sz
        elif "manifests" in parts or name == "CURRENT":
            kinds["manifests"] += sz
        elif name == "dict.log":
            kinds["dict"] += sz
        elif name == "artifacts.jsonl":
            kinds["registry"] += sz
        elif parts and parts[0] == "plans":
            kinds["plans"] += sz
        else:
            kinds["other"] += sz
    return kinds


def _dir_size_bytes(path: Path) -> int:
    """Total bytes of every regular file under `path` — the same walk-based
    technique `_disk_bytes_by_kind` uses, applied to the whole `--out`
    directory (store copy, replay copy, logs, metrics) rather than one
    store, for the `--max-disk-mb` guard."""
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        if not p.is_file():
            continue
        try:
            total += p.stat().st_size
        except OSError:
            continue
    return total


def _write_ledger(out_dir: Path, event: str, **fields: Any) -> None:
    """One append-only JSONL ledger for events the run manifest cannot carry
    because the run stops before writing one (a disk-guard abort) or because
    they are cheap, incremental facts (a manifest upgrade) — never opened,
    never gated on the eventlog's own format."""
    entry = {"ts": time.time(), "event": event, **fields}
    with open(out_dir / "longevity_ledger.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
    print(f"  LEDGER {event}: " + ", ".join(f"{k}={v}" for k, v in fields.items()),
          flush=True)


def _disk_guard_check(out_dir: Path, max_disk_mb: float | None) -> float | None:
    """`None` while under budget; the measured MB once `--max-disk-mb` (if
    given) is exceeded."""
    if max_disk_mb is None:
        return None
    used_mb = _dir_size_bytes(out_dir) / 1e6
    return used_mb if used_mb > max_disk_mb else None


def _maybe_upgrade_manifests(store_path: Path) -> None:
    """Main's engine is moving to manifest format 2 (B5); a store copied
    from an older build needs `tgms store upgrade-manifests` before this
    harness's writer opens it read-write. Feature-detected, not hardcoded:
    at the time this harness was written `tgms/cli.py`'s own `store`
    subcommand only knows `gc`/`compact`/`verify` and `verify()`'s report
    carries no format field at all, so this is a no-op today by construction
    (`report.get(...)` on a missing key) and becomes a real upgrade the
    moment format-2 detection and the CLI verb both land — no second edit
    needed here.
    """
    import tgms

    probe = tgms.open(store_path, backend="native", read_only=True)
    try:
        report = probe.adapter.verify()
    finally:
        probe.close()
    fmt = report.get("manifest_format") if isinstance(report, dict) else None
    if fmt is None or fmt >= 2:
        return
    print(f"  store manifest format {fmt} detected; running "
          f"'tgms store upgrade-manifests' before the writer opens it ...",
          flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "tgms.cli", "store", "upgrade-manifests",
         "--store", str(store_path)],
        capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  WARNING: upgrade-manifests failed (rc={result.returncode}): "
              f"{result.stderr.strip()}", file=sys.stderr)


def _reopen_rw_with_retry(path: Path, attempts: int = 20, delay_s: float = 0.1) -> Any:
    """Reopen `path` read-write right after a child that held it has exited.

    The B5 single-writer lock (`<store>/writer.lock`, an OS-level `flock` —
    not yet landed in this tree as of this harness's own writing, see
    `tgms/store.py`) is released by the OS the instant that process's file
    descriptor closes, crash included, so this should succeed on the first
    try; the retry only covers the small scheduling gap between
    `Popen.poll()` reporting exit and the OS actually tearing the fd down.
    """
    import tgms

    last_exc: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            return tgms.open(path, backend="native")
        except Exception as e:                # noqa: BLE001 — lock-contention
            last_exc = e                       # shape isn't nailed down yet
            time.sleep(delay_s)
    assert last_exc is not None
    raise last_exc


def _git_commit() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "-uno"], cwd=ROOT,
                                    capture_output=True, text=True).stdout.strip())
        return sha + ("-dirty" if dirty else "")
    except Exception:  # noqa: BLE001
        return "unknown"


def _ram_gb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / (1024 * 1024), 2)
    except OSError:
        pass
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
                     / (1024 ** 3), 2)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                             text=True, check=True, timeout=5)
        return round(int(out.stdout.strip()) / (1024 ** 3), 2)
    except Exception:  # noqa: BLE001
        return 0.0


def _machine_info() -> dict[str, Any]:
    return {"host": platform.node() or "unknown",
            "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
            "cpus": os.cpu_count() or 1, "ram_gb": _ram_gb() or 0.01}


# --------------------------------------------------------------------------- #
# reader mix and artifact windows: derived from the store, not a fixed schema #
# --------------------------------------------------------------------------- #


def _sample_node_vt(store: Any, sample: int = 4000,
                    rng: random.Random | None = None) -> list[tuple[str, int]]:
    """`[(uid, vt_s), ...]`, capped at `sample`. Used both to seed the reader
    mix's arguments and to bucket artifacts into distinct valid-time windows —
    the store is whatever `--store` names, not necessarily the dataset
    `scripts/eval_harness.py` builds, so query arguments are sampled from the
    live content rather than assumed (`"n1"`/`"n2"`)."""
    rng = rng or random.Random(0)
    out: list[tuple[str, int]] = []
    for v in store.adapter.all_node_versions():
        out.append((v.uid, v.vt_s))
        if len(out) >= sample:
            break
    return out


def build_reader_mix(vt_lo: int, vt_hi: int, uids: list[str]) -> list[dict[str, Any]]:
    """The §14.4 mix (`docs/eval_concurrency.md`/`scripts/eval_resources.py`
    `READER_MIX`): a point lookup, a 2-hop instant, a bucketed scan, an
    interval join — reused by shape, with arguments sampled from whatever
    store this run was given rather than the fixed synthetic dataset those
    scripts assume."""
    span = max(1, vt_hi - vt_lo)
    mid = vt_lo + span // 2
    u1 = uids[0] if uids else "n0"
    u2 = uids[1] if len(uids) > 1 else u1
    return [
        {"id": "hist.single", "op": "entity_history", "args": {"uid": u1}},
        {"id": "snap.hop2", "op": "snapshot_subgraph",
         "args": {"seeds": [u1], "hops": 2, "t_valid": mid}},
        {"id": "series.count", "op": "graph_metric_timeseries",
         "args": {"metric": "edge_event_count", "window": {"t_a": vt_lo, "t_b": vt_hi},
                  "stride": max(1, span // 100)}},
        {"id": "coactive.narrow", "op": "co_active",
         "args": {"a_spec": {"src": u1}, "b_spec": {"src": u2},
                  "allen_relation": {"relation": "overlaps"}}},
    ]


def ensure_artifacts(store: Any, registry: Any, n: int, rng: random.Random,
                     pairs: list[tuple[str, int]]) -> list[str]:
    """Register `n` `"tgir_plan"` artifacts, each a `NodeScan` over a
    distinct valid-time window's worth of uids — the P4.5 text's "register N
    artifacts over windows" — idempotently, so a writer restart never
    re-registers what a prior life already published (`Registry.register`
    refuses a non-consecutive generation, so this must check first).

    Follows exactly the production seam `tests/test_artifact_refresh.py::
    _register_plan_artifact` exercises: a real `run_plan` execution, its
    plan dumped to `plans/<digest>.json`, the record built from the
    envelope `refresh._publish` itself consumes.
    """
    from tgms.tgir.depscope import DependencyScope
    from tgms.tgir.execute import run_plan
    from tgms.tgir.loader import dump
    from tgms.tgir.node import NodeScan
    from tgms.tgir.plan import Plan

    existing = set(registry.names())
    created: list[str] = []
    if not pairs:
        return created
    ordered = sorted(pairs, key=lambda p: p[1])
    buckets = max(1, n)
    bucket_size = max(1, len(ordered) // buckets)
    plans_dir = Path(store.path) / "plans"
    plans_dir.mkdir(exist_ok=True)
    for i in range(n):
        name = f"longevity-artifact-{i}"
        if name in existing:
            continue
        lo, hi = i * bucket_size, min(len(ordered), (i + 1) * bucket_size)
        window_uids = tuple(u for u, _ in ordered[lo:hi]) or (ordered[0][0],)
        window_uids = tuple(rng.sample(window_uids, min(5, len(window_uids))))
        scan = NodeScan(f"p{i}", uids=window_uids)
        env = run_plan(Plan(scan), store.adapter, tt_source=store)
        tgir = env["tgir"]
        blob = plans_dir / f"{tgir['plan_digest']}.json"
        if not blob.exists():
            blob.write_text(json.dumps(dump(scan)))
        dependency = DependencyScope.from_json(env["dependency"])
        registry.register(
            name=name, kind="query_result",
            plan={"plan_digest": tgir["plan_digest"], "node_digest": tgir["node_digest"],
                  "plan_format": 1, "plan_ref": f"plans/{tgir['plan_digest']}.json"},
            basis={"tt_q": env["tt_q"], "pinned": env["pinned"], "clamped": env["clamped"],
                   "tt_q_verified": dependency.tt_q_verified},
            state={"completeness": tgir.get("completeness", "unknown"),
                   "exactness": tgir.get("exactness", "exact"), "refusal": None},
            refresh={"kind": "tgir_plan", "ref": f"plans/{tgir['plan_digest']}.json",
                    "basis_policy": "open"},
            dependency=dependency)
        created.append(name)
    return created


def apply_correction_via_public_api(store: Any, gc_writer: Any, correction: Any) -> bool:
    """Translate one `tgms.eval.corrections.Correction`'s raw ops into calls
    on the *public* write surface (`Store`/`GroupCommitWriter`'s
    `assert_node`/`assert_edge`/`correct`/`retract`/`ingest_events`) instead
    of the internal `adapter.apply_ops`/`Store._write` every other caller of
    this generator uses (`scripts/bench_corrections.py`).

    Returns `False`, applying nothing, for a Class-E ("within-batch
    retirement") correction: its whole point is two ops committed as **one**
    event-log batch, and every public convenience method is its own batch —
    there is no public call that takes an explicit op list. Skipping it is a
    documented limitation, not a silent substitution.
    """
    from tgms.core.model import EntityRef

    if correction.cls == "E":
        return False
    for op in correction.ops:
        kind = op["op"]
        if kind == "ingest_events":
            # a1_events: exactly one event, and events supersede nothing —
            # the one public call this class needs `GroupCommitWriter` does
            # not expose, so it goes straight to the store (same lock).
            #
            # `corrections.py::_a1_events` already stamps a fresh `disc` on
            # this event, but that disc is derived from the *writer's own
            # RNG sequence*, and the soak's writer restarts every life with
            # the same seed (`spawn_writer` never varies it by life index) —
            # so two lives can walk the same RNG sequence and stamp the same
            # disc for what are, in wall-clock terms, two different
            # corrections. Re-stamp here with something that survives a
            # process restart by construction: not the RNG, and not any
            # `Store` counter (`Store._ingest_offset_base` is per-instance
            # and resets on every new writer process too), just a fresh
            # per-call token.
            events = [dict(ev, disc=f"life-{uuid.uuid4().hex[:16]}") for ev in op["events"]]
            store.ingest_events(events, node_label=op.get("node_label", "Node"))
        elif kind == "assert_node":
            gc_writer.assert_node(op["uid"], op["label"], op.get("props") or {},
                                  op["vt_s"], op["vt_e"])
        elif kind == "assert_edge":
            gc_writer.assert_edge(op["src"], op["dst"], op["rel_type"],
                                  op.get("props") or {}, op["vt_s"], op["vt_e"],
                                  op.get("disc", ""))
        elif kind == "correct":
            gc_writer.correct(EntityRef(**op["ref"]), op["props"], op["vt_s"], op["vt_e"])
        elif kind == "retract":
            gc_writer.retract(EntityRef(**op["ref"]), op["t"])
        else:  # pragma: no cover — corrections.py's own closed op vocabulary
            raise ValueError(f"unsupported op for the public-API translation: {kind}")
    return True


# --------------------------------------------------------------------------- #
# child: reader                                                               #
# --------------------------------------------------------------------------- #


def child_reader(cfg: dict[str, Any]) -> None:
    import tgms
    from tgms.temporal.algebra import call_operator, ensure_all_registered
    from tgms.telemetry.metrics import Metrics

    ensure_all_registered()
    metrics = Metrics(cfg.get("metrics_path"))
    idx = cfg["idx"]
    store_path = cfg["store"]
    store = tgms.open(store_path, backend="native", read_only=True)
    a = store.adapter
    mix = cfg["mix"]
    for q in mix:                          # one warm pass
        try:
            call_operator(a, q["op"], dict(q["args"]))
        except Exception:                  # noqa: BLE001 — a refusal is data
            pass

    end_at = cfg["end_at"]
    progress_path = Path(cfg["progress_path"])
    timings: dict[str, list[float]] = {q["id"]: [] for q in mix}
    errors: dict[str, int] = {}
    done = 0
    reopens = 0
    minute_t0 = time.perf_counter()
    last_flush = time.time()
    # The native engine's generation-pin table is in-process only (no
    # cross-process reader registry, by design — docs/eval_concurrency.md's
    # "three more races" section) — a reader process that keeps one store
    # handle open for its whole life can hold a writer's compacted-away
    # generation's files open (unlinked, not freed) for that entire time.
    # Bounding that window is this harness's own fix for the 38 GB local
    # incident (see the module docstring) — a reader process, not a thread,
    # cannot be told to drop a *specific* old generation, so it drops its
    # *whole* handle on a schedule instead.
    reopen_every_s = cfg.get("reopen_every_s", 60.0)
    last_reopen = time.perf_counter()
    while time.time() < end_at:
        for q in mix:
            try:
                t = time.perf_counter()
                call_operator(a, q["op"], dict(q["args"]))
                timings[q["id"]].append((time.perf_counter() - t) * 1e3)
                done += 1
            except Exception as e:         # noqa: BLE001
                errors[q["id"]] = errors.get(q["id"], 0) + 1
                metrics.counter("reader_errors_total", reader=idx, query=q["id"],
                                error=type(e).__name__)
        if reopen_every_s and time.perf_counter() - last_reopen >= reopen_every_s:
            store.close()
            store = tgms.open(store_path, backend="native", read_only=True)
            a = store.adapter
            reopens += 1
            metrics.counter("reader_reopens_total", reader=idx)
            last_reopen = time.perf_counter()
        now = time.time()
        if now - last_flush >= cfg.get("report_every_s", 60):
            elapsed = max(1e-9, time.perf_counter() - minute_t0)
            for q in mix:
                ts = timings[q["id"]]
                d = dist(ts)
                metrics.gauge("query_p50_ms", d["p50"], reader=idx, query=q["id"])
                metrics.gauge("query_p95_ms", d["p95"], reader=idx, query=q["id"])
                metrics.gauge("query_p99_ms", d["p99"], reader=idx, query=q["id"])
                metrics.counter("queries_total", d["n"], reader=idx, query=q["id"])
                timings[q["id"]] = []
            metrics.gauge("qps", done / elapsed, reader=idx)
            metrics.gauge("rss_kb", _vm_status().get("vmrss_kb", 0), reader=idx)
            metrics.flush()
            progress_path.write_text(json.dumps(
                {"idx": idx, "done": done, "errors": errors, "reopens": reopens,
                 "ts": now}))
            done = 0
            minute_t0 = time.perf_counter()
            last_flush = now
    metrics.flush()
    progress_path.write_text(json.dumps({"idx": idx, "done": done, "errors": errors,
                                         "reopens": reopens,
                                         "ts": time.time(), "final": True}))
    store.close()


# --------------------------------------------------------------------------- #
# child: writer (owns the store; the checker runs as a thread inside it)      #
# --------------------------------------------------------------------------- #


def _emit_cumulative_counters(metrics, last_emitted: dict[str, int],
                              **current: int) -> None:
    """Feed cumulative accumulators to `Metrics.counter`, which *adds*.

    `Metrics.counter(name, delta)` accumulates in memory and `flush()` writes
    the running total, so passing a life's cumulative count on every periodic
    flush would make the sink's value the sum of k snapshots instead of the
    count itself. Emit only the delta since the previous call; `last_emitted`
    is the per-life memory of what has already been fed in. Always calls
    counter() (even with a zero delta) so every flush carries the series.
    """
    for name, value in current.items():
        metrics.counter(name, value - last_emitted.get(name, 0))
        last_emitted[name] = value


def child_writer(cfg: dict[str, Any]) -> None:
    import tgms
    from tgms.artifact.refresh import RefreshRefused, refresh as artifact_refresh
    from tgms.artifact.registry import Registry
    from tgms.artifact.witness import check_artifact
    from tgms.eval import corrections as C
    from tgms.storage.eventlog import EventLog
    from tgms.telemetry.metrics import Metrics
    from tgms.write import GroupCommitWriter

    store_path = Path(cfg["store"])
    store = tgms.open(store_path, backend="native")
    metrics = Metrics(cfg.get("metrics_path"))
    rng = random.Random(cfg["seed"])
    mix = dict(MIXES[cfg["mix"]])
    if cfg.get("writer_sleep_s") is not None:
        mix["writer_sleep_s"] = float(cfg["writer_sleep_s"])
    registry = Registry(store_path)

    rng_probe = random.Random(cfg["seed"] ^ 0xA12)
    pairs = _sample_node_vt(store, rng=rng_probe)
    ensure_artifacts(store, registry, cfg["artifacts"], rng_probe, pairs)

    sub = C.probe_substrate(store, rng=rng)
    uids = sorted({u for u, _ in pairs}) or list(sub.uids)
    target = C.Target(read_uids=tuple(uids[:20]), window=(sub.vt_lo, sub.vt_hi))

    gc_writer = GroupCommitWriter(store, max_delay_s=0.0, max_batch=200).start()

    stop_event = threading.Event()
    corr_signal = threading.Event()
    check_counts = {"checks": 0, "invalidations": 0, "refreshes": 0}

    def checker_loop() -> None:
        """Tied to the correction stream (P4.5: "freshness checks while
        writing" / "after each correction batch"), not a fixed cadence."""
        log_path = store_path / "eventlog.jsonl"
        while not stop_event.is_set():
            if not corr_signal.wait(timeout=1.0):
                continue
            corr_signal.clear()
            for name in list(registry.names()):
                rec = registry.current(name)
                if rec is None:
                    continue
                try:
                    verdict = check_artifact(rec, EventLog(log_path))
                except Exception:          # noqa: BLE001 — a check that cannot
                    continue                # answer is not a harness error
                check_counts["checks"] += 1
                metrics.counter("artifact_checks_total")
                if verdict.actionable_fresh:
                    continue
                check_counts["invalidations"] += 1
                metrics.counter("artifact_invalidations_total")
                if verdict.refresh is None:
                    continue
                t0 = time.perf_counter()
                try:
                    artifact_refresh(rec, verdict.refresh, store, registry)
                except RefreshRefused:
                    continue
                except Exception:              # noqa: BLE001 — recorded, not raised
                    continue
                dt_ms = (time.perf_counter() - t0) * 1e3
                check_counts["refreshes"] += 1
                metrics.counter("artifact_refreshes_total")
                metrics.gauge("time_to_fresh_ms", dt_ms, artifact=name)

    checker_thread = threading.Thread(target=checker_loop, name="longevity-checker",
                                      daemon=True)
    checker_thread.start()

    control_path = Path(cfg["control_path"])
    progress_path = Path(cfg["progress_path"])
    compactions_path = Path(cfg["compactions_path"])
    end_at = cfg["end_at"]
    compact_every = max(1, cfg["compact_every_batches"])
    # A wall-clock floor on top of the batch-count trigger — the 38 GB local
    # incident's own diagnosis (module docstring): `--compact-every-batches`
    # alone lets an unthrottled writer cycle full compactions many times a
    # second against a sizeable store, and each cycle's outgoing generation
    # can sit unlinked-but-open (POSIX) in a cross-process reader's fd for
    # as long as that reader keeps its handle. This does not need every
    # `compact_every` batches to actually compact, only to *offer* to.
    compact_min_interval_s = max(0.0, cfg.get("compact_min_interval_s", 2.0))
    last_compact_t = time.perf_counter()

    n = cfg["start_n"]
    batches = appends = corrections_applied = corrections_skipped = errors = compactions = 0
    compactions_throttled = 0
    last_emitted: dict[str, int] = {}
    last_control_seq = -1
    commit_lat: list[float] = []
    minute_t0 = time.perf_counter()
    last_flush = time.time()
    batches_since_flush = 0

    def do_append() -> None:
        nonlocal n
        src, dst = f"lw{n}", f"lw{n + 1}"
        gc_writer.assert_edge(src, dst, "LW", {}, vt_s=n, vt_e=n + 1_000_000)
        n += 2

    def do_correction() -> bool:
        gen = rng.choice(list(C.GENERATORS))
        placement = rng.choice(C.PLACEMENTS)
        cands = C.generate(store, sub, target, rng=rng, generators=[gen],
                           placements=[placement])
        if not cands:
            return False
        return apply_correction_via_public_api(store, gc_writer, cands[0])

    weight_total = max(1e-9, mix["append"] + mix["correction"])

    while time.time() < end_at:
        if control_path.exists():
            try:
                ctl = json.loads(control_path.read_text())
            except (OSError, ValueError):
                ctl = None
            if ctl and ctl.get("seq", -1) != last_control_seq:
                last_control_seq = ctl["seq"]
                boundary = ctl["boundary"]
                os.environ["TGMS_CRASH_POINT"] = boundary
                if boundary in MAINT_BOUNDARIES:
                    store.adapter.compact()   # dies here (os._exit(137))
                # otherwise falls through and dies inside the next write below

        is_corr = rng.random() < (mix["correction"] / weight_total)
        t0 = time.perf_counter()
        try:
            if is_corr:
                if do_correction():
                    corrections_applied += 1
                    corr_signal.set()
                else:
                    corrections_skipped += 1
            else:
                do_append()
                appends += 1
        except Exception as e:              # noqa: BLE001 — recorded, not raised
            errors += 1
            # Labeled by exception type, like the reader path already does
            # (child_reader's reader_errors_total carries error=type(e).__name__)
            # — the writer path did not before this fix, so the soak's 249
            # errors had no recoverable cause (benchmarks/longevity-v1/
            # README.md's own "errors observed" section). The ledger entry
            # (out_dir/longevity_ledger.jsonl, shared across every writer
            # life) and this life's own stderr (folded into
            # logs/writer-<life>.log by _spawn) both get the exception
            # class and message so a future run's errors are diagnosable.
            metrics.counter("writer_errors_total", error=type(e).__name__)
            _write_ledger(store_path.parent, "writer_op_error",
                          op=("correction" if is_corr else "append"),
                          error_type=type(e).__name__, error_msg=str(e),
                          life_progress_path=str(progress_path))
        else:
            commit_lat.append((time.perf_counter() - t0) * 1e3)
        batches += 1
        batches_since_flush += 1

        if batches % compact_every == 0:
            if time.perf_counter() - last_compact_t < compact_min_interval_s:
                compactions_throttled += 1
                metrics.counter("compactions_throttled_total")
            else:
                os.environ.pop("TGMS_CRASH_POINT", None)
                ct0 = time.perf_counter()
                report = store.adapter.compact()
                gc_report = store.adapter.gc(keep_last=2)
                compactions += 1
                last_compact_t = time.perf_counter()
                ct1 = last_compact_t
                metrics.counter("compactions_total")
                metrics.gauge("compaction_ms", (ct1 - ct0) * 1e3)
                with open(compactions_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"t_start": ct0, "t_end": ct1,
                                         "batches": batches, "compact": report,
                                         "gc": gc_report}) + "\n")

        if mix["writer_sleep_s"]:
            time.sleep(mix["writer_sleep_s"])

        now = time.time()
        if now - last_flush >= cfg.get("report_every_s", 60):
            elapsed = max(1e-9, time.perf_counter() - minute_t0)
            d = dist(commit_lat)
            metrics.gauge("commit_p50_ms", d["p50"])
            metrics.gauge("commit_p95_ms", d["p95"])
            metrics.gauge("commit_p99_ms", d["p99"])
            metrics.gauge("commits_per_s", batches_since_flush / elapsed)
            metrics.gauge("generation", float(store.adapter.generation))
            metrics.gauge("rss_kb", _vm_status().get("vmrss_kb", 0))
            for k, v in _disk_bytes_by_kind(store_path).items():
                metrics.gauge(f"disk_bytes_{k}", float(v))
            _emit_cumulative_counters(metrics, last_emitted,
                                      appends_total=appends,
                                      corrections_applied_total=corrections_applied,
                                      corrections_skipped_total=corrections_skipped)
            metrics.flush()
            progress_path.write_text(json.dumps({
                "n": n, "batches": batches, "appends": appends,
                "corrections_applied": corrections_applied,
                "corrections_skipped": corrections_skipped,
                "compactions": compactions, "compactions_throttled": compactions_throttled,
                "errors": errors,
                "checks": check_counts["checks"],
                "invalidations": check_counts["invalidations"],
                "refreshes": check_counts["refreshes"], "ts": now}))
            commit_lat = []
            batches_since_flush = 0
            minute_t0 = time.perf_counter()
            last_flush = now

    stop_event.set()
    checker_thread.join(timeout=10)
    gc_stats = gc_writer.stats()
    gc_writer.close()
    _emit_cumulative_counters(metrics, last_emitted,
                              appends_total=appends,
                              corrections_applied_total=corrections_applied,
                              corrections_skipped_total=corrections_skipped)
    metrics.flush()
    progress_path.write_text(json.dumps({
        "n": n, "batches": batches, "appends": appends,
        "corrections_applied": corrections_applied,
        "corrections_skipped": corrections_skipped,
        "compactions": compactions, "compactions_throttled": compactions_throttled,
        "errors": errors,
        "checks": check_counts["checks"],
        "invalidations": check_counts["invalidations"],
        "refreshes": check_counts["refreshes"], "group_commit": gc_stats,
        "ts": time.time(), "final": True}))
    store.close()


CHILDREN = {"reader": child_reader, "writer": child_writer}


# --------------------------------------------------------------------------- #
# orchestrator                                                                #
# --------------------------------------------------------------------------- #


def _spawn(role: str, cfg: dict[str, Any], log_path: Path,
          env_extra: dict[str, str] | None = None) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    if env_extra:
        env.update(env_extra)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "w", encoding="utf-8")
    p = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_child", role],
        stdin=subprocess.PIPE, stdout=logf, stderr=subprocess.STDOUT, env=env)
    assert p.stdin is not None
    p.stdin.write(json.dumps(cfg).encode("utf-8"))
    p.stdin.close()
    return p


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def cmd_run(args: argparse.Namespace) -> int:
    print(f"RUN_STARTED commit={_git_commit()}", flush=True)

    duration_s = parse_duration(args.duration)
    restart_every_s = parse_duration(args.restart_every) if args.restart_every else 0.0
    out_dir = Path(args.out)
    store_src = Path(args.store)
    store_name = store_src.name

    # The final replay's own compaction cadence (B7c): defaults to this
    # run's own writer cadence (--compact-every-batches, always positive by
    # construction of that flag's own default) so the replay folds
    # manifests back down on the same schedule the live run used;
    # --replay-compact-every overrides, and 0 (or a non-positive override)
    # disables replay-time compaction entirely, falling back to the old
    # uncompacted replay.
    replay_compact_every: int | None = args.replay_compact_every
    if replay_compact_every is None:
        replay_compact_every = args.compact_every_batches
    if not replay_compact_every or replay_compact_every <= 0:
        replay_compact_every = None

    plan = {
        "store": str(store_src), "store_name": store_name,
        "duration_s": duration_s, "mix": args.mix, "readers": args.readers,
        "compact_every_batches": args.compact_every_batches,
        "compact_min_interval_s": args.compact_min_interval_s,
        "reader_reopen_every_s": args.reader_reopen_every_s,
        "restart_every_s": restart_every_s, "artifacts": args.artifacts,
        "writer_sleep_s": args.writer_sleep_s,
        "max_disk_mb": args.max_disk_mb,
        "replay_compact_every": replay_compact_every,
        "seed": args.seed, "out": str(out_dir),
        "metrics": args.metrics or str(out_dir / "metrics.jsonl"),
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, "plan": plan}, indent=2))
        return 0

    if not store_src.exists():
        print(f"no such store: {store_src}", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "logs").mkdir(exist_ok=True)
    live_store = out_dir / "store"
    if live_store.exists():
        shutil.rmtree(live_store)
    print(f"  copying {store_src} -> {live_store} ...", flush=True)
    shutil.copytree(store_src, live_store)          # the ONE working copy

    if args.max_disk_mb is not None:
        used_mb = _dir_size_bytes(out_dir) / 1e6
        if used_mb > args.max_disk_mb:
            _write_ledger(out_dir, "disk_guard_abort", reason="initial_copy_exceeds_limit",
                          measured_mb=round(used_mb, 1), limit_mb=args.max_disk_mb)
            return 3

    _maybe_upgrade_manifests(live_store)

    import tgms
    probe = tgms.open(live_store, backend="native", read_only=True)
    dataset_digest = probe.digest()
    probe.close()

    metrics_path = Path(plan["metrics"])
    control_path = out_dir / "writer_control.json"
    compactions_path = out_dir / "compactions.jsonl"
    recoveries_path = out_dir / "recoveries.jsonl"
    reader_restarts_path = out_dir / "reader_restarts.jsonl"
    for p in (control_path, compactions_path, recoveries_path, reader_restarts_path):
        if p.exists():
            p.unlink()
    for p in out_dir.glob("writer_progress-*.json"):
        p.unlink()

    def writer_progress_path(life_index: int) -> Path:
        return out_dir / f"writer_progress-{life_index}.json"

    t_start = time.time()
    end_at = t_start + duration_s
    env_extra = {"TGMS_METRICS_PATH": str(metrics_path)}

    pairs_probe = tgms.open(live_store, backend="native", read_only=True)
    pairs = _sample_node_vt(pairs_probe)
    vt_lo, vt_hi = pairs_probe.adapter.stats().get("vt_min", 0), \
        pairs_probe.adapter.stats().get("vt_max", 1)
    pairs_probe.close()
    reader_mix = build_reader_mix(vt_lo if vt_lo is not None else 0,
                                  vt_hi if vt_hi is not None else 1,
                                  [u for u, _ in sorted(pairs, key=lambda x: x[1])])

    def spawn_writer(start_n: int, life_index: int) -> subprocess.Popen:
        cfg = {"store": str(live_store), "mix": args.mix, "seed": args.seed,
               "artifacts": args.artifacts, "compact_every_batches": args.compact_every_batches,
               "compact_min_interval_s": args.compact_min_interval_s,
               "start_n": start_n, "end_at": end_at, "control_path": str(control_path),
               "progress_path": str(writer_progress_path(life_index)),
               "compactions_path": str(compactions_path),
               "metrics_path": str(metrics_path), "report_every_s": 60,
               "writer_sleep_s": args.writer_sleep_s}
        return _spawn("writer", cfg, out_dir / "logs" / f"writer-{life_index}.log", env_extra)

    def spawn_reader(idx: int, life_index: int) -> subprocess.Popen:
        cfg = {"store": str(live_store), "idx": idx, "end_at": end_at, "mix": reader_mix,
               "metrics_path": str(metrics_path),
               "progress_path": str(out_dir / f"reader-{idx}-progress.json"),
               "report_every_s": 60, "reopen_every_s": args.reader_reopen_every_s}
        return _spawn("reader", cfg, out_dir / "logs" / f"reader-{idx}-{life_index}.log",
                      env_extra)

    writer_life = 0
    writer_proc = spawn_writer(0, writer_life)
    reader_lives = [0] * args.readers
    reader_procs = [spawn_reader(i, 0) for i in range(args.readers)]

    rng = random.Random(args.seed ^ 0xF00D)
    next_restart_at = (t_start + restart_every_s) if restart_every_s else None
    control_seq = 0
    recoveries = 0
    reader_restart_count = 0
    unexpected_writer_deaths = 0
    writer_finished = False

    print(f"  soak running for {duration_s:.0f}s: mix={args.mix} readers={args.readers} "
          f"compact_every={args.compact_every_batches} restart_every="
          f"{restart_every_s or 'never'}", flush=True)

    disk_guard_tripped = False
    next_disk_check_at = t_start + 5.0

    while time.time() < end_at:
        time.sleep(min(1.0, max(0.05, duration_s / 200)))

        if args.max_disk_mb is not None and time.time() >= next_disk_check_at:
            next_disk_check_at = time.time() + 5.0
            measured = _disk_guard_check(out_dir, args.max_disk_mb)
            if measured is not None:
                disk_guard_tripped = True
                print(f"  DISK GUARD TRIPPED at {measured:.1f} MB (limit "
                      f"{args.max_disk_mb:.1f} MB); aborting the soak now",
                      flush=True)
                break

        if next_restart_at is not None and time.time() >= next_restart_at:
            boundary = rng.choice(ALL_BOUNDARIES)
            control_seq += 1
            control_path.write_text(json.dumps({"seq": control_seq, "boundary": boundary}))
            next_restart_at = time.time() + restart_every_s

        wrc = None if writer_finished else writer_proc.poll()
        if wrc == 0 and time.time() >= end_at - 1.0:
            # the writer's own deadline (== end_at) elapsed and it exited
            # cleanly: a graceful stop, not a death to recover from or
            # restart. Once seen, stop polling it — the run is ending.
            writer_finished = True
        elif wrc is not None:
            # Engine crash points (`crates/tgms-engine-core/src/store.rs::
            # crash_point`) call `std::process::abort()` -> SIGABRT, which
            # Python reports as returncode -6; the Python-side crash points
            # (`tgms/storage/crashpoint.py`) call `os._exit(137)` instead.
            # Both are the harness's own designed kill, not a bug.
            armed = control_path.exists()
            designed = armed and wrc in (137, -6)
            kind = "designed" if designed else "unexpected"
            if not designed:
                unexpected_writer_deaths += 1
            t_death = time.time()
            prog = _read_json(writer_progress_path(writer_life)) or {}
            t0 = time.perf_counter()
            try:
                r = _reopen_rw_with_retry(live_store)
                r.adapter.verify()
                r.close()
            except Exception as e:          # noqa: BLE001
                print(f"  WARNING: reopen after writer death failed: {e}", flush=True)
            recovery_s = time.perf_counter() - t0
            with open(recoveries_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t_death": t_death, "kind": kind,
                                     "returncode": wrc, "recovery_s": recovery_s,
                                     "life_index": writer_life}) + "\n")
            recoveries += 1
            print(f"  writer life {writer_life} ended ({kind}, rc={wrc}); "
                  f"recovered in {recovery_s:.3f}s", flush=True)
            if control_path.exists():
                control_path.unlink()
            writer_life += 1
            start_n = prog.get("n", 0)
            writer_proc = spawn_writer(start_n, writer_life)

        for i, rp in enumerate(reader_procs):
            rc = rp.poll()
            if rc is not None and time.time() < end_at:
                reader_restart_count += 1
                with open(reader_restarts_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"idx": i, "t": time.time(),
                                         "returncode": rc, "life_index": reader_lives[i]}) + "\n")
                print(f"  reader {i} died (rc={rc}); restarting", flush=True)
                reader_lives[i] += 1
                reader_procs[i] = spawn_reader(i, reader_lives[i])

    if disk_guard_tripped:
        print("  disk guard tripped: killing children, skipping replay/manifest ...",
              flush=True)
        for rp in reader_procs:
            rp.kill()
        writer_proc.kill()
        for rp in reader_procs:
            try:
                rp.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
        try:
            writer_proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        measured = _dir_size_bytes(out_dir) / 1e6
        _write_ledger(out_dir, "disk_guard_abort", reason="soak_exceeds_limit",
                      measured_mb=round(measured, 1), limit_mb=args.max_disk_mb,
                      wall_s=round(time.time() - t_start, 1))
        print(f"RUN_ABORTED reason=disk_guard measured_mb={measured:.1f} "
              f"limit_mb={args.max_disk_mb:.1f}", flush=True)
        return 3

    print("  duration elapsed; waiting for children to stop ...", flush=True)
    for rp in reader_procs:
        try:
            rp.wait(timeout=60)
        except subprocess.TimeoutExpired:
            rp.kill()
    try:
        writer_proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        writer_proc.kill()
        writer_proc.wait(timeout=10)

    # --- final verification -------------------------------------------- #
    print("  verifying final store ...", flush=True)
    final = _reopen_rw_with_retry(live_store)
    verify_report = final.adapter.verify()
    final_digest = final.digest()
    final_stats = final.stats()
    final.close()

    # --- final replay / digest equivalence ------------------------------ #
    # `replay()` applies every logged batch as its own generation
    # (module docstring: D-149's O(batches^2) manifest-growth pathology,
    # inherited rather than introduced by this harness), uncompacted unless
    # `replay_compact_every` is set (B7c, 2026-09-15: `replay(...,
    # compact_every=N)` folds the throwaway replay store back down every N
    # applied batches, the same `adapter.compact()`+`adapter.gc(keep_last=2)`
    # a live writer already does). With --max-disk-mb set, estimate that
    # cost from the batch count *before* paying for it, using the same
    # constant the docstring's own citation implies
    # (scripts/build_snb_store.py:70-90: 10,147 uncompacted generations ->
    # 25 GB of manifests, ~243 bytes/batch^2).
    #
    # Two different projections, depending on whether replay-time
    # compaction is enabled:
    #
    # - **Uncompacted** (`replay_compact_every is None`): `total_batches`
    #   (the full event-log batch count) is the right input — every batch
    #   in the whole log becomes its own permanent, never-reclaimed
    #   generation, so cost grows with the whole run's batch count:
    #   `projected_mb = 243.0 * total_batches ** 2 / 1e6`.
    # - **Compacted**: periodic `compact()`+`gc(keep_last=2)` *reclaims*
    #   each cycle's accumulated segments/manifests before the next cycle
    #   starts (confirmed by the same calibration this constant comes from
    #   — scripts/build_snb_store.py's own comment: compacting every
    #   100,000 ops held manifests at ~0 MB at 600k ops, not growing with
    #   the ops count), so disk usage saw-tooths between a small
    #   post-compaction baseline and a peak of one cycle's own O(cycle^2)
    #   cost, repeating every `replay_compact_every` batches without
    #   accumulating across cycles. The worst case this guard must budget
    #   for is therefore that one-cycle peak, not a sum over every cycle in
    #   the run: `projected_mb = 243.0 * min(replay_compact_every,
    #   total_batches) ** 2 / 1e6` — independent of `total_batches` once
    #   the run is longer than one cycle. (A naive `243.0 *
    #   replay_compact_every * total_batches / 1e6` — cycle cost times
    #   number of cycles — double-counts: it assumes each cycle's cost
    #   adds to the last, which is exactly what `gc(keep_last=2)` prevents.)
    from tgms.storage.eventlog import EventLog as _EventLog
    from tgms.storage.eventlog import replay as replay_log

    total_batches = sum(1 for _ in _EventLog(live_store / "eventlog.jsonl").batches_from(0))
    do_replay = True
    replay_skipped: dict[str, Any] | None = None
    if args.max_disk_mb is not None:
        if replay_compact_every is not None:
            cycle = min(replay_compact_every, total_batches) if total_batches else 0
            projected_mb = (243.0 * (cycle ** 2)) / 1e6
            projection_kind = "compacted"
        else:
            projected_mb = (243.0 * (total_batches ** 2)) / 1e6
            projection_kind = "uncompacted"
        current_mb = _dir_size_bytes(out_dir) / 1e6
        if current_mb + projected_mb > args.max_disk_mb:
            do_replay = False
            replay_skipped = {
                "reason": "projected_replay_exceeds_limit",
                "total_batches": total_batches,
                "replay_compact_every": replay_compact_every,
                "projection_kind": projection_kind,
                "projected_mb": round(projected_mb, 1),
                "limit_mb": args.max_disk_mb,
            }
            _write_ledger(out_dir, "disk_guard_replay_skip",
                          total_batches=total_batches,
                          replay_compact_every=replay_compact_every,
                          projection_kind=projection_kind,
                          projected_mb=round(projected_mb, 1),
                          current_mb=round(current_mb, 1), limit_mb=args.max_disk_mb)
            kind_note = (f"peak within one {replay_compact_every}-batch compaction "
                        f"cycle, {projection_kind}" if projection_kind == "compacted"
                        else f"the whole {total_batches}-batch, {projection_kind} replay")
            print(f"  SKIPPING final replay: projected to ~{projected_mb:.0f} MB "
                  f"(~{projected_mb / 1e6:.1f} TB) of manifests ({kind_note}; D-149's "
                  f"own O(batches^2) pathology), which would push --out over "
                  f"--max-disk-mb={args.max_disk_mb:.0f}; digest equivalence not "
                  f"checked this run.", flush=True)

    replay_digest: str | None = None
    digest_equal: bool | None = None
    if do_replay:
        cadence_note = (f" (compacting every {replay_compact_every} batches)"
                        if replay_compact_every is not None else "")
        print(f"  replaying {total_batches} batches into a fresh store"
              f"{cadence_note} ...", flush=True)
        replay_dir = out_dir / "replay-store"
        if replay_dir.exists():
            shutil.rmtree(replay_dir)
        replayed = tgms.open(replay_dir, backend="native")
        dst_log = Path(replayed.path) / "eventlog.jsonl"
        shutil.copyfile(live_store / "eventlog.jsonl", dst_log)
        replay_log(dst_log, replayed.adapter, thread_cursor=True,
                  compact_every=replay_compact_every)
        # store_digest() is defined purely over logical content
        # (tgms/storage/base.py: "backend-independent"), so compacting this
        # throwaway store before hashing it cannot change the digest — it
        # only stops this harness from leaving an uncompacted, D-149-shaped
        # copy sitting in --out once it no longer needs one.
        replayed.adapter.compact()
        replayed.adapter.gc(keep_last=1)
        replay_digest = replayed.digest()
        replayed.close()
        digest_equal = replay_digest == final_digest
        if not digest_equal:
            print(f"  DIGEST MISMATCH: final={final_digest[:16]} "
                  f"replay={replay_digest[:16]}", file=sys.stderr)

    summary = summarize(out_dir, metrics_path, t_start, end_at,
                        recoveries_path, reader_restarts_path, compactions_path)
    summary.update({
        "verify_healthy": bool(verify_report.get("healthy")),
        "final_stats": final_stats,
        "recoveries": recoveries, "unexpected_writer_deaths": unexpected_writer_deaths,
        "reader_restarts": reader_restart_count,
        # `digest_equal` stays `None` ("not computed") rather than being
        # coerced to `False` ("computed and found unequal") whenever the
        # disk guard skips the replay step — `replay_skipped` is the only
        # place that distinguishes those two very different outcomes, and
        # scripts/longevity_report.py must read it before treating a `None`
        # digest_equal as a FAIL. `None` when the replay ran (whether or not
        # digests matched).
        "digest_equal": digest_equal,
        "replay_skipped": replay_skipped,
        "total_batches": total_batches,
    })

    manifest = {
        "schema_version": SCHEMA_VERSION, "git_commit": _git_commit(),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "machine": _machine_info(),
        "config": plan, "seed": {"value": args.seed},
        "dataset": {"name": store_name, "digest": dataset_digest,
                    "digest_kind": "store_digest"},
        "result_digest": final_digest,
        "protocol": {"warmups": 0, "reps": 1,
                     "ceilings": {"duration_s": duration_s,
                                  "compact_every_batches": args.compact_every_batches,
                                  "restart_every_s": restart_every_s}},
        "record": _rel_or_abs(metrics_path),
        "summary": summary,
    }
    out_path = out_dir / f"longevity-{store_name}-{args.seed}.json"
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"  wrote {out_path}", flush=True)

    schema = CRM.load_schema()
    ok, message = CRM.validate_one(out_path, schema)
    if ok:
        print(f"  manifest conforms to {CRM.SCHEMA_PATH.name}", flush=True)
    else:
        print(f"  MANIFEST INVALID: {message}", file=sys.stderr)

    print(f"RUN_DONE digest_equal={digest_equal} verify_healthy="
          f"{summary['verify_healthy']} recoveries={recoveries} "
          f"reader_restarts={reader_restart_count} errors={summary['error_count']}",
          flush=True)
    return 0 if (ok and digest_equal is True and summary["verify_healthy"]) else 1


def _rel_or_abs(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return str(p.resolve())


def summarize(out_dir: Path, metrics_path: Path, t_start: float, end_at: float,
             recoveries_path: Path, reader_restarts_path: Path,
             compactions_path: Path) -> dict[str, Any]:
    """Gate E's own vocabulary: bounded metadata growth, no unbounded memory,
    throughput/latency drift, compaction stalls — computed from the metrics
    JSONL and the sidecar event logs the run wrote."""
    lines = _read_jsonl(metrics_path)
    gauges: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for rec in lines:
        if rec.get("kind") != "gauge":
            continue
        key = (rec["name"], json.dumps(rec.get("labels", {}), sort_keys=True))
        gauges.setdefault(key, []).append((rec["ts"], rec["value"]))
    for v in gauges.values():
        v.sort()

    def series(name: str) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for (n, _labels), pts in gauges.items():
            if n == name:
                out.extend(pts)
        out.sort()
        return out

    def half_split(pts: list[tuple[float, float]], window_s: float = 3600.0
                   ) -> tuple[list[float], list[float]]:
        if not pts:
            return [], []
        t0, t1 = pts[0][0], pts[-1][0]
        first = [v for t, v in pts if t <= t0 + window_s]
        last = [v for t, v in pts if t >= t1 - window_s]
        return first, last

    def slope(pts: list[tuple[float, float]]) -> float:
        if len(pts) < 2:
            return 0.0
        t0, v0 = pts[0]
        t1, v1 = pts[-1]
        dt = max(1e-9, t1 - t0)
        return (v1 - v0) / dt

    commits = series("commits_per_s")
    commit_p99 = series("commit_p99_ms")
    rss = series("rss_kb")
    manifest_bytes = series("disk_bytes_manifests")
    segment_bytes = series("disk_bytes_segments")
    generation = series("generation")

    c_first, c_last = half_split(commits)
    p_first, p_last = half_split(commit_p99)

    compactions = _read_jsonl(compactions_path)
    reader_p99_all = [pt for (n, _l), pts in gauges.items() if n == "query_p99_ms"
                      for pt in pts]
    compaction_stall_p99 = 0.0
    for c in compactions:
        window = [v for t, v in reader_p99_all if c["t_start"] - 60 <= t <= c["t_end"] + 60]
        if window:
            compaction_stall_p99 = max(compaction_stall_p99, max(window))

    # Counters in the Metrics sink are cumulative-per-flush (never reset),
    # so the running total is whichever line for a (name, labels) key has
    # the latest timestamp — summed across every reader's / life's labels.
    counter_latest: dict[tuple[str, str], tuple[float, float]] = {}
    for rec in lines:
        if rec.get("kind") != "counter":
            continue
        key = (rec["name"], json.dumps(rec.get("labels", {}), sort_keys=True))
        ts = rec.get("ts", 0.0)
        if key not in counter_latest or ts >= counter_latest[key][0]:
            counter_latest[key] = (ts, rec.get("value", 0.0))

    def counter_sum(name: str) -> float:
        return sum(v for (n, _l), (_t, v) in counter_latest.items() if n == name)

    progress_files = sorted(out_dir.glob("writer_progress-*.json"),
                            key=lambda p: int(p.stem.rsplit("-", 1)[-1]))
    writer_prog = (_read_json(progress_files[-1]) or {}) if progress_files else {}

    # --- writer counters: life-summed, not counter_latest ---------------- #
    # `child_writer`'s own accumulators (`errors`, `appends`,
    # `corrections_applied`, `corrections_skipped`, and the checker
    # thread's `checks`/`invalidations`/`refreshes`) start at 0 in every
    # fresh writer process ("life") and are mirrored into the shared
    # metrics sink as *unlabeled* counters (no life index in the label
    # set) — so `counter_latest` above, keyed only on (name, labels),
    # collapses every life onto one key and keeps only the sample with the
    # latest timestamp, i.e. the last life's own count. For any run with
    # `--restart-every` set this silently discards every earlier life's
    # counters (see benchmarks/longevity-v1/README.md's "errors observed"
    # section: 42 lives, manifest said 1, the true sum was 249).
    #
    # Each life's own last-written `writer_progress-<life>.json` (one file
    # per life, already written by `child_writer` regardless of whether
    # that life ended cleanly or was killed mid-flight — the last periodic
    # flush before a kill is still that life's true final count, since
    # these are monotonic non-negative accumulators within a life) carries
    # exactly what counter_latest was missing. Summing each life's own
    # last snapshot, one term per life, gives the true run total.
    life_progress = [_read_json(p) or {} for p in progress_files]

    def life_summed(key: str) -> int:
        return int(sum(p.get(key, 0) or 0 for p in life_progress))

    writer_errors_life_summed = life_summed("errors")
    writer_totals_all_lives = {
        "errors": writer_errors_life_summed,
        "appends": life_summed("appends"),
        "corrections_applied": life_summed("corrections_applied"),
        "corrections_skipped": life_summed("corrections_skipped"),
        "artifact_checks": life_summed("checks"),
        "artifact_invalidations": life_summed("invalidations"),
        "artifact_refreshes": life_summed("refreshes"),
        "lives": len(life_progress),
    }

    reader_done_total = int(counter_sum("queries_total"))
    reader_errors_total = int(counter_sum("reader_errors_total"))

    recoveries_recorded = _read_jsonl(recoveries_path)
    unexpected_recoveries = sum(1 for r in recoveries_recorded if r.get("kind") == "unexpected")
    error_count = writer_errors_life_summed + reader_errors_total + unexpected_recoveries

    return {
        "wall_s": round(time.time() - t_start, 1),
        # `writer_final` is the LAST life's own final progress snapshot
        # only — never a run total (see writer_totals_all_lives, which is
        # summed across every life, for the true totals).
        "writer_final": writer_prog,
        "writer_totals_all_lives": writer_totals_all_lives,
        "reader_queries_total": reader_done_total,
        "reader_errors_total": reader_errors_total,
        "reader_restarts_recorded": len(_read_jsonl(reader_restarts_path)),
        "compactions": len(compactions),
        "drift": {
            "throughput_first_hour_avg": round(statistics.fmean(c_first), 3) if c_first else None,
            "throughput_last_hour_avg": round(statistics.fmean(c_last), 3) if c_last else None,
            "commit_p99_first_hour_max": round(max(p_first), 3) if p_first else None,
            "commit_p99_last_hour_max": round(max(p_last), 3) if p_last else None,
        },
        "memory_slope_kb_per_s": round(slope(rss), 6),
        "metadata_growth_slope_bytes_per_s": {
            "manifests": round(slope(manifest_bytes), 6),
            "segments": round(slope(segment_bytes), 6),
        },
        "generation_final": generation[-1][1] if generation else None,
        "compaction_stall_max_reader_p99_ms": round(compaction_stall_p99, 3),
        "error_count": int(error_count),
    }


# --------------------------------------------------------------------------- #
# entry                                                                       #
# --------------------------------------------------------------------------- #


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "_child":
        cfg = json.loads(sys.stdin.read())
        CHILDREN[sys.argv[2]](cfg)
        return 0

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", required=True, help="existing store to copy and soak")
    ap.add_argument("--duration", required=True, help='e.g. "1h", "24h", "90s"')
    ap.add_argument("--mix", choices=list(MIXES), default="balanced")
    ap.add_argument("--readers", type=int, default=2)
    ap.add_argument("--compact-every-batches", type=int, default=200)
    ap.add_argument("--restart-every", default="0",
                    help='e.g. "30m"; "0" disables the restart cycle')
    ap.add_argument("--artifacts", type=int, default=5)
    ap.add_argument("--writer-sleep-s", type=float, default=None,
                    help="override the mix's per-batch writer throttle "
                         "(seconds); lower for a higher commit rate and more "
                         "generations per hour, higher to keep the final "
                         "replay step's uncompacted manifest growth bounded "
                         "on a batch-count-sensitive host")
    ap.add_argument("--compact-min-interval-s", type=float, default=2.0,
                    help="wall-clock floor between compactions on top of "
                         "--compact-every-batches (default 2s) — the fix for "
                         "the 38 GB local incident (see module docstring): "
                         "an unthrottled writer cycling full compactions "
                         "many times a second leaves each outgoing "
                         "generation's files unlinked-but-open in a "
                         "cross-process reader for that whole window")
    ap.add_argument("--reader-reopen-every-s", type=float, default=60.0,
                    help="each reader process closes and reopens its "
                         "read_only=True handle on this cadence (default "
                         "60s; 0 disables) so it cannot hold a "
                         "compacted-away generation's files open for the "
                         "reader's whole life — the other half of the 38 GB "
                         "fix")
    ap.add_argument("--max-disk-mb", type=float, default=None,
                    help="abort the run cleanly (a longevity_ledger.jsonl "
                         "entry, no traceback, no store copy left "
                         "half-written) if --out's total size exceeds this "
                         "many MB — checked periodically through the soak "
                         "and, as a before-the-fact size estimate, before "
                         "the final replay step. Strongly recommended on "
                         "any host with bounded disk; the smoke test always "
                         "sets one.")
    ap.add_argument("--replay-compact-every", type=int, default=None,
                    help="compact()+gc() the final replay's throwaway store "
                         "every N applied batches (B7c: tgms.storage."
                         "eventlog.replay's own compact_every) — bounds the "
                         "O(batches^2) manifest-growth pathology (D-149) "
                         "that otherwise makes replay of a long run's full "
                         "history impractical. Defaults to this run's own "
                         "--compact-every-batches (the same cadence the "
                         "live writer used); 0 disables replay-time "
                         "compaction entirely, falling back to the old "
                         "uncompacted replay and its O(batches^2) "
                         "disk-guard projection.")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--metrics", default=None, help="default: <out>/metrics.jsonl")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())

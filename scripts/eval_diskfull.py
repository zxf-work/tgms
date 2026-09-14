#!/usr/bin/env python
"""ENOSPC / short-write injection (Lane A task A5; plan
`docs/eval_durability.md`).

Real ENOSPC needs either root (a loopback filesystem) or an actual full
disk, neither available for a local, no-experiments-yet harness (PI rule).
Instead this injects at the **Python call boundary**: a child process the
parent spawns (mirroring `scripts/eval_durability.py`'s parent/child split)
installs a counting monkeypatch over `os.write`, `os.fsync`, `os.rename`
and `os.replace` before opening a store, controlled by two env vars:

  TGMS_DISKFULL_AT=<n>       the n-th call (across all four, one shared
                              counter) raises `OSError(ENOSPC, ...)`.
  TGMS_SHORT_WRITE_AT=<n>    the n-th `os.write` call returns fewer bytes
                              than asked, once, instead of raising.

**What this can and cannot see — read before trusting a "clean" result.**
`os.write(fd, data)` is the raw syscall wrapper; a buffered Python file
object's own `.write()` (`open(path, "ab")`, used everywhere in this
codebase) calls the C library's write() directly, never `os.write`. Grepping
`tgms/` turns up no direct `os.write` or `os.rename` call at all — every
write goes through buffered `file.write()`, and the one atomic-replace site
in Python (`tgms/storage/tcsr.py::save_permutation`) calls `os.replace`, not
`os.rename`. So in the **current** codebase, on the plain write workload
this harness runs:

  - `TGMS_DISKFULL_AT`/`TGMS_SHORT_WRITE_AT` are reachable through exactly
    one call site: `os.fsync(f.fileno())` in `EventLog.append`
    (`tgms/storage/eventlog.py`) — this is the one this harness's end-to-end
    trials actually exercise, i.e. "ENOSPC while fsyncing the write-ahead
    log". `tgms/artifact/registry.py`'s `os.fsync` (artifact append) and
    `tgms/storage/tcsr.py`'s `os.replace` (persisted TCSR swap) are real,
    reachable sites too, but this harness's workload never registers an
    artifact or calls `tcsr()`, so a trial never reaches them.
  - `os.write` and `os.rename` are patched for completeness (a future write
    site, or product code on a different OS path, might call them) but are
    **dead code today** — `tests/test_eval_diskfull.py` proves the injector
    itself fires correctly by calling `os.write`/`os.rename` directly after
    installing it, independent of whether any product code path reaches
    them yet.
  - **Every Rust-side write is invisible to this injector entirely** —
    `NativeStore` never goes through Python's `os` module. The engine-side
    injection points a Rust-lane harness would need instead:
    `write_atomic` (`crates/tgms-engine-core/src/store.rs`, the manifest
    temp-file-then-rename that publishes a generation and the `CURRENT`
    flip), segment writes (`segment.rs`, `Segment::write`/finalize), close
    run writes (`visibility.rs`), the dictionary append
    (`dict.rs::Dictionary::open`/append path), and manifest-chain writes
    (`manifest_chain.rs`, checkpoint/delta serialization). None of those
    are implemented here — Lane A owns only the Python-boundary injector;
    an engine-side ENOSPC/short-write harness is the Rust lane's follow-up.

Per trial: a seeded workload (the same generator `scripts/eval_durability.py`
uses) runs in the injector-armed child at a random injection point; the
parent requires that the child's own exception was clean (a normal Python
exception, not a hang — enforced by a subprocess timeout) before reopening
*without* the injector and checking:

  Q1 every acknowledged write is present (ack is recorded only after the
     call returns, so this holds regardless of where injection landed);
  Q2 recovery is deterministic (recovered digest == clean replay of the
     recovered store's own event log — reuses `eval_durability.
     clean_replay_digest`);
  Q3 single-generation visibility (`verify()` clean);
  Q4 orphan reclamation after `compact()` + `gc()`.

    python scripts/eval_diskfull.py --trials 20 --seed 1 --json out.json
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import platform
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.eval_durability import (  # noqa: E402
    _apply_workload_op,
    _random_workload,
    clean_replay_digest,
)

SCHEMA_VERSION = "1.0.0"

ENV_DISKFULL_AT = "TGMS_DISKFULL_AT"
ENV_SHORT_WRITE_AT = "TGMS_SHORT_WRITE_AT"

#: Both trial modes this harness runs; each trial picks one uniformly.
MODES = ("diskfull", "short_write")

#: A batch of the seed write + `_random_workload`'s ops rarely exceeds this
#: many os.fsync calls at the tiny scale every trial runs at (each op is its
#: own commit, one `EventLog.append` fsync apiece) — wide enough that most
#: injection points land inside the workload, some past its end (a
#: legitimate "never fired" trial, not a harness bug).
INJECT_AT_RANGE = (1, 60)


# --------------------------------------------------------------------------- #
# the injector — installed only inside the child, only when armed
# --------------------------------------------------------------------------- #


def install_injector(status_path: Path) -> None:
    """Patch `os.write`/`os.fsync`/`os.rename`/`os.replace` with a shared
    call counter. Only called in the child process, only when
    `TGMS_DISKFULL_AT` or `TGMS_SHORT_WRITE_AT` is set. Writes
    `status_path` on every call past the trigger point so the parent can
    tell whether (and where, and how) the injector actually fired even if
    the child later dies in a way that never reaches its own top-level
    `finally` (belt and suspenders — the top-level `finally` in `child()`
    normally writes the authoritative copy).
    """
    diskfull_at = os.environ.get(ENV_DISKFULL_AT)
    short_write_at = os.environ.get(ENV_SHORT_WRITE_AT)
    diskfull_at = int(diskfull_at) if diskfull_at else None
    short_write_at = int(short_write_at) if short_write_at else None
    if diskfull_at is None and short_write_at is None:
        return

    state = {"n": 0, "fired_at": None, "fired_kind": None}
    orig_write, orig_fsync = os.write, os.fsync
    orig_rename, orig_replace = os.rename, os.replace

    def _tick() -> int:
        state["n"] += 1
        return state["n"]

    def _maybe_enospc(site: str) -> None:
        n = _tick()
        if diskfull_at is not None and n == diskfull_at:
            state["fired_at"], state["fired_kind"] = n, f"enospc@{site}"
            status_path.write_text(json.dumps(state))
            raise OSError(errno.ENOSPC, f"[injected] no space left on device ({site} call #{n})")

    def p_write(fd: int, data: bytes) -> int:
        n = _tick()
        if diskfull_at is not None and n == diskfull_at:
            state["fired_at"], state["fired_kind"] = n, "enospc@os.write"
            status_path.write_text(json.dumps(state))
            raise OSError(errno.ENOSPC, f"[injected] no space left on device (os.write call #{n})")
        if short_write_at is not None and n == short_write_at:
            state["fired_at"], state["fired_kind"] = n, "short_write@os.write"
            status_path.write_text(json.dumps(state))
            short_len = max(1, len(data) // 2)
            return orig_write(fd, data[:short_len])
        return orig_write(fd, data)

    def p_fsync(fd: int) -> None:
        _maybe_enospc("os.fsync")
        return orig_fsync(fd)

    def p_rename(src: Any, dst: Any) -> None:
        _maybe_enospc("os.rename")
        return orig_rename(src, dst)

    def p_replace(src: Any, dst: Any) -> None:
        _maybe_enospc("os.replace")
        return orig_replace(src, dst)

    os.write, os.fsync, os.rename, os.replace = p_write, p_fsync, p_rename, p_replace


# --------------------------------------------------------------------------- #
# child — runs the armed workload
# --------------------------------------------------------------------------- #


class _AckSink:
    def __init__(self, path: Path) -> None:
        self._f = open(path, "a", buffering=1)

    def write(self, s: str) -> None:
        self._f.write(s)


def child(store_path: str, trial_seed: str, acked_path: str, status_path: str) -> int:
    status = Path(status_path)
    install_injector(status)  # no-op unless TGMS_DISKFULL_AT/SHORT_WRITE_AT is set

    import tgms

    acked = _AckSink(Path(acked_path))
    rng = random.Random(int(trial_seed))
    error: str | None = None
    exit_code = 0
    try:
        store = tgms.open(store_path, backend="native")

        def write(uid: str, i: int) -> None:
            store.assert_node(uid, "N", {"i": i}, vt_s=0, vt_e=1_000_000)
            acked.write(json.dumps({"kind": "node", "key": uid, "i": i}) + "\n")

        write("a0", 0)
        for op in _random_workload(rng):
            _apply_workload_op(store, acked, op, rng)
        store.close()
    except OSError as e:
        error = f"OSError(errno={e.errno}, {errno.errorcode.get(e.errno, '?')}): {e}"
        exit_code = 2  # the expected shape: a clean, typed OS error
    except Exception as e:  # noqa: BLE001 - any other exception is itself a finding
        error = f"{type(e).__name__}: {e}"
        exit_code = 3
    finally:
        # Authoritative status write: whatever install_injector already
        # wrote (if the fault fired), overwritten here with the same
        # "n calls seen" figure plus the child's own outcome, so the parent
        # never has to reconcile two partial views.
        prior = json.loads(status.read_text()) if status.exists() else {"n": 0, "fired_at": None,
                                                                         "fired_kind": None}
        status.write_text(json.dumps({**prior, "error": error, "exit_code": exit_code}))
    return exit_code


# --------------------------------------------------------------------------- #
# parent — one trial
# --------------------------------------------------------------------------- #


def derive_trial_seed(seed: int, trial: int) -> int:
    digest = hashlib.sha256(f"diskfull:{seed}:{trial}".encode()).hexdigest()
    return int(digest[:16], 16)


def _q1_q3_q4(store_path: Path, acked_path: Path) -> tuple[dict[str, Any], list[str]]:
    """Simplified relative of `eval_durability.assess_q1_q3_q4`: no special
    "last write may be a visible blend" case is needed here, because the
    child stops at the *first* exception and only ever acks a write after
    the call returns — so every acked entry, without exception, must be
    exactly present; an unacked trailing write may or may not have been
    resurrected by write-ahead replay (bytes reached the OS before the
    injected fsync failure), and that is Q1's known-open direction, not a
    violation either way (D-086)."""
    import tgms
    from tgms.core.model import edge_eid

    problems: list[str] = []
    fields: dict[str, Any] = {}
    acked = [json.loads(line) for line in acked_path.read_text().splitlines() if line.strip()]

    store = tgms.open(store_path, backend="native")
    v = store.adapter.verify()
    if v.get("problems"):
        problems.append(f"verify: {v['problems'][:3]}")
    fields["q3_single_generation"] = not v.get("problems")

    latest: dict[tuple[str, Any], int | None] = {}
    for rec in acked:
        key = (rec["kind"], tuple(rec["key"]) if isinstance(rec["key"], list) else rec["key"])
        latest[key] = rec["i"]
    q1 = True
    for (kind, key), want in latest.items():
        if kind == "node":
            got = store.adapter.believed_node_versions(key)
        else:
            src, dst, rel, disc = key
            got = store.adapter.believed_edge_versions(edge_eid(src, dst, rel, disc), src=src, dst=dst)
        vals_here = [g.props.get("i") for g in got]
        if want is None:
            if got:
                q1 = False
                problems.append(f"acked {kind} {key} retracted but believed={vals_here}")
            continue
        if len(got) != 1 or got[0].props.get("i") != want:
            q1 = False
            problems.append(f"acked {kind} {key}={want} but believed={vals_here}")
    fields["q1_acked_survive"] = q1
    fields["digest"] = store.digest()
    store.close()

    store = tgms.open(store_path, backend="native")
    try:
        store.adapter.compact()
        store.adapter.gc(keep_last=0)
        v = store.adapter.verify()
        tmps = list((store_path / "native").rglob("*.tmp"))
        fields["q4_orphans_reclaimed"] = not v.get("problems") and not tmps
        if v.get("problems"):
            problems.append(f"post-gc verify: {v['problems'][:2]}")
        if tmps:
            problems.append(f"tmp files survive gc: {[t.name for t in tmps]}")
    finally:
        store.close()

    return fields, problems


def run_trial(trial: int, seed: int) -> dict[str, Any]:
    trial_seed = derive_trial_seed(seed, trial)
    rng = random.Random(trial_seed)
    mode = rng.choice(MODES)
    at = rng.randint(*INJECT_AT_RANGE)

    work = Path(tempfile.mkdtemp(prefix=f"tgms-diskfull-{mode}-"))
    store_path = work / "store"
    acked_path = work / "acked.txt"
    status_path = work / "status.json"
    acked_path.touch()
    t0 = time.perf_counter()

    env = {**os.environ}
    env.pop(ENV_DISKFULL_AT, None)
    env.pop(ENV_SHORT_WRITE_AT, None)
    env[ENV_DISKFULL_AT if mode == "diskfull" else ENV_SHORT_WRITE_AT] = str(at)

    rec: dict[str, Any] = {
        "trial": trial, "seed": seed, "trial_seed": trial_seed,
        "mode": mode, "inject_at": at,
    }
    try:
        proc = subprocess.run(
            [sys.executable, __file__, "--child", str(store_path), str(trial_seed),
             str(acked_path), str(status_path)],
            capture_output=True, text=True, timeout=60, env=env,
        )
        rec["hang"] = False
        rec["child_exit"] = proc.returncode
        rec["child_stderr_tail"] = proc.stderr[-300:] if proc.returncode not in (0, 2) else None
    except subprocess.TimeoutExpired:
        rec["hang"] = True
        rec["child_exit"] = None
        rec["problems"] = ["child did not terminate within 60s — a hang, the disallowed outcome"]
        rec["clean_error"] = False
        rec["wall_s"] = round(time.perf_counter() - t0, 2)
        return rec

    status = json.loads(status_path.read_text()) if status_path.exists() else {}
    rec["fault_fired"] = status.get("fired_at") is not None
    rec["fired_kind"] = status.get("fired_kind")
    rec["calls_seen"] = status.get("n")
    rec["child_error"] = status.get("error")
    # "clean" = terminated (no hang, already established) via a well-formed
    # Python exception when the fault fired — exit 0 (fault never reached
    # by the workload) is equally clean, just uninteresting for this trial.
    rec["clean_error"] = (not rec["fault_fired"]) or rec["child_exit"] in (2, 3)

    problems: list[str] = [] if rec["clean_error"] else [
        f"injected fault fired ({rec['fired_kind']}) but the child exited "
        f"{rec['child_exit']} without a clean typed error: {rec['child_error']}"]

    try:
        fields, q_problems = _q1_q3_q4(store_path, acked_path)
        rec.update(fields)
        problems.extend(q_problems)
        d1 = fields["digest"]
        d2 = clean_replay_digest(store_path / "eventlog.jsonl")
        rec["q2_deterministic"] = d1 == d2
        if d1 != d2:
            problems.append("recovered digest != clean replay digest")
    except Exception as e:  # noqa: BLE001 - an unopenable store is itself the finding
        rec["q1_acked_survive"] = False
        rec["q2_deterministic"] = False
        rec["q3_single_generation"] = False
        rec["q4_orphans_reclaimed"] = False
        problems.append(f"reopen after injected fault failed: {type(e).__name__}: {e}")

    rec["problems"] = problems
    rec["wall_s"] = round(time.perf_counter() - t0, 2)
    return rec


# --------------------------------------------------------------------------- #
# manifest / CLI
# --------------------------------------------------------------------------- #


def _git_commit() -> str:
    env = os.environ.get("TGMS_COMMIT")
    if env:
        return env
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                             text=True, timeout=10).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                                    capture_output=True, text=True, timeout=10).stdout.strip())
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


GATE_KEYS = ("q1_acked_survive", "q2_deterministic", "q3_single_generation",
            "q4_orphans_reclaimed")


def is_ok(r: dict[str, Any]) -> bool:
    return bool(r.get("clean_error")) and not r.get("hang") and all(
        r.get(k, False) for k in GATE_KEYS)


def build_manifest(results: list[dict[str, Any]], seed: int, record_path: str) -> dict[str, Any]:
    bad = [r for r in results if not is_ok(r)]
    result_src = json.dumps(
        [{k: v for k, v in r.items() if k != "wall_s"} for r in results],
        sort_keys=True, separators=(",", ":")).encode()
    fired = [r for r in results if r.get("fault_fired")]
    by_mode = {m: {"trials": sum(1 for r in results if r["mode"] == m),
                  "fired": sum(1 for r in results if r["mode"] == m and r.get("fault_fired"))}
              for m in MODES}
    return {
        "schema_version": SCHEMA_VERSION,
        "git_commit": _git_commit(),
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "machine": _machine_info(),
        "config": {"harness": "scripts/eval_diskfull.py", "modes": list(MODES),
                  "inject_at_range": list(INJECT_AT_RANGE)},
        "seed": {"value": seed},
        "dataset": {"name": "seeded synthetic workload (scripts/eval_durability.py "
                            "generator), one tiny store per trial",
                   "digest": hashlib.sha256(str(INJECT_AT_RANGE).encode()).hexdigest(),
                   "digest_kind": "manifest"},
        "result_digest": hashlib.sha256(result_src).hexdigest(),
        "protocol": {"warmups": 0, "reps": len(results),
                    "ceilings": {"child_timeout_s": 60}},
        "record": record_path,
        "summary": {
            "n_trials": len(results), "n_problems": len(bad),
            "n_fault_fired": len(fired), "n_hang": sum(1 for r in results if r.get("hang")),
            "by_mode": by_mode,
        },
        "results": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--child", nargs=4,
                    metavar=("STORE", "TRIAL_SEED", "ACKED", "STATUS"))
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    if args.child:
        return child(*args.child)

    results = []
    for t in range(args.trials):
        r = run_trial(t, args.seed)
        results.append(r)
        tag = "HANG" if r.get("hang") else ("OK" if is_ok(r) else "PROBLEM")
        print(f"  trial {t:4d} mode={r['mode']:11s} at={r['inject_at']:3d} "
              f"fired={str(r.get('fault_fired')):5s} -> {tag} "
              f"{'; '.join(r.get('problems', []))[:100]}", flush=True)

    bad = [r for r in results if not is_ok(r)]
    print(f"\n{len(results)} trials, {len(bad)} with problems "
         f"({sum(1 for r in results if r.get('fault_fired'))} fired the injected fault, "
         f"{sum(1 for r in results if r.get('hang'))} hung)")

    if args.json:
        record_path = str(args.json)
        try:
            record_path = str(args.json.resolve().relative_to(ROOT))
        except ValueError:
            pass
        manifest = build_manifest(results, args.seed, record_path)
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(manifest, indent=1) + "\n")
        print(f"record → {args.json}")

    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

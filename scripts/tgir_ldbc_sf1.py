"""The E13 campaign: the 21 frozen LDBC plans against the real SF1 instance.

**This is not an LDBC Benchmark, this is not an implementation of an LDBC
Benchmark, and nothing produced here is an LDBC Benchmark Result.**

What it does, per `docs/design/PAPER_A_EVIDENCE_FREEZE.md` §B and §C5:

1. **Bind** each plan to LDBC's own SF1 parameters (`ldbc_snb_params.py`). No
   plan is edited: `sigma` is carried through untouched (§A3 M11 — narrowing it
   is the one lever that would turn refusals into admissions, so it is closed by
   rule) and only `params` are rebound (§A3 M12).
2. **Re-derive** the admission estimate under the bound parameters. The frozen
   table was computed with the artifacts' fixture parameters; binding real ones
   changes the inputs, so any verdict flip is the forecast under test and is
   reported as a scored result, not repaired (§E addendum 3).
3. **Admit or refuse** at the frozen policy, recording the `RefusalCertificate`
   whenever it refuses.
4. **Execute** the admitted plans under the §C4 timing protocol.
5. **Re-run the refused plans with the guard bypassed but recording**, under a
   hard wall ceiling (§C5) — the D-086 method, which is what turns "10 of 21
   refuse" from an anecdote into a classifier score. A timeout is recorded as
   `TIMEOUT` and *is* a true rejection at a 10 s budget, whatever the plan would
   eventually have done.

Outcome classes are reported separately and never absorbed into one another.

    uv run python scripts/tgir_ldbc_sf1.py --store stores/snb-sf1 \
        --params /mnt/project/xzhang/tgms/ldbc-sf1/params --out benchmarks/results-v1/ldbc-sf1.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ldbc_snb_params import (  # noqa: E402
    LDBC_PLANS, PhantomAnchor, bind, substitute,
)
from tgms.core.errors import CostError, TgmsError  # noqa: E402
from tgms.temporal.algebra import ensure_all_registered  # noqa: E402
from tgms.temporal.guardrails import DEFAULT_CEILINGS  # noqa: E402
from tgms.tgir.admission import POLICY_VERSION, plan_estimate  # noqa: E402
from tgms.tgir.execute import run_plan  # noqa: E402
from tgms.tgir.loader import load  # noqa: E402

#: §C5. The ceiling `external_workloads/FREEZE.md` already fixed for BIRD gold
#: validation, adopted for continuity. No plan runs unbounded.
#:
#: **Enforced, not observed.** The first version of this runner compared elapsed
#: time against the ceiling *after* `run_plan` returned, which bounds nothing:
#: the first campaign spent 870 s inside BI10 without stopping, against a plan
#: whose estimate is 1.7e5 ms and against BI17 whose estimate is 1.3e9 ms — two
#: weeks. A ceiling that cannot interrupt is a label.
#:
#: Enforcement is a subprocess per bypassed plan, because the work happens
#: inside a Rust call that a Python-level signal cannot reliably interrupt;
#: only killing the process is certain.
BYPASS_CEILING_S = 600

#: Extra wall the child gets on top of the ceiling, for opening the store. The
#: SF1 warm-up is ~165 s (the in-process index build over 20.4M versions) and it
#: is not part of what the ceiling is meant to bound, so the child measures the
#: plan alone and the parent allows for the open.
CHILD_OPEN_ALLOWANCE_S = 420

#: §C4's protocol, reduced for plans that are seconds rather than milliseconds:
#: the campaign is about admission and completion, not about a p95 on a scan.
WARMUPS = 1
REPS = 3


def _sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:                                  # noqa: BLE001
        return "unknown"


def _load_bound(plan_id: str, params_root: Path, sf: str,
                adapter: Any = None) -> tuple[Any, dict[str, Any]]:
    b = bind(plan_id, params_root, sf, adapter)
    document = substitute({"root": b["root"], "sigma": b["sigma"]}, b["params"])
    # `plan_format` is the reader's version gate; the bound document is the
    # frozen artifact re-parameterised, so it carries the artifact's own.
    return load({"plan_format": b["plan_format"], "plan_id": plan_id,
                 **document}), b


def _time(fn: Any, reps: int) -> tuple[list[float], Any]:
    out = None
    for _ in range(WARMUPS):
        out = fn()
    times = []
    for _ in range(reps):
        t = time.time()
        out = fn()
        times.append((time.time() - t) * 1000.0)
    return times, out


def _last_json(text: str, phase: str | None) -> dict[str, Any]:
    """The last JSON line of a child, optionally restricted to one phase.

    `TimeoutExpired` carries whatever the child managed to write, which is why
    the child emits its identity and estimate up front.
    """
    out: dict[str, Any] = {}
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            got = json.loads(line)
        except json.JSONDecodeError:
            continue
        if phase is None and got.get("phase") == "pre":
            continue
        if phase is not None and got.get("phase") != phase:
            continue
        out = {k: v for k, v in got.items() if k != "phase"}
    return out


def run_child(plan_id: str, store_path: str, params_root: Path,
              sf: str) -> dict[str, Any]:
    """Run one plan in a child under a hard kill.

    **Every** plan goes through here, not only the refused ones. The first
    bounded campaign bounded the bypassed arm and left the admitted arm
    in-process on the reasoning that an admitted plan is predicted under 6 s —
    which is exactly the assumption the experiment exists to test. BI18 was
    admitted at an estimate of 5,918 ms and had run **49 minutes** when the
    campaign was killed, so the arm that needed no bound was the arm that ran
    away. A ceiling that only guards the cases you expect to be slow is not a
    ceiling.

    The child measures `run_plan` alone; the parent allows it
    `BYPASS_CEILING_S + CHILD_OPEN_ALLOWANCE_S` of wall so that the store open
    is not charged against the ceiling. A kill is recorded as `TIMEOUT`, which
    at a 10 s budget **is** a true rejection whatever the plan would eventually
    have done — the point of the arm is the classifier score, not the runtime.
    """
    cmd = [sys.executable, "-u", str(Path(__file__).resolve()),
           "--single", plan_id, "--store", store_path,
           "--params", str(params_root), "--sf", sf, "--out", os.devnull]
    t0 = time.time()
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=BYPASS_CEILING_S + CHILD_OPEN_ALLOWANCE_S,
                              cwd=ROOT)
    except subprocess.TimeoutExpired as e:
        pre = _last_json(e.stdout or "", phase="pre")
        return {**pre, "plan_id": plan_id, "outcome": "TIMEOUT",
                "wall_s": round(time.time() - t0, 1),
                "note": f"killed at the {BYPASS_CEILING_S}s ceiling "
                        f"(+{CHILD_OPEN_ALLOWANCE_S}s store-open allowance)"}
    final = _last_json(done.stdout, phase=None)
    if final:
        if final.get("ms", 0) > BYPASS_CEILING_S * 1000:
            final["outcome"] = "TIMEOUT"
        final.pop("phase", None)
        return final
    pre = _last_json(done.stdout, phase="pre")
    return {**pre, "plan_id": plan_id, "outcome": "ERRORED",
            "error": (done.stderr or done.stdout)[-300:]}


def run_one(plan_id: str, store: Any, params_root: Path, sf: str,
            bypass: bool, emit_pre: bool = False) -> dict[str, Any]:
    rec: dict[str, Any] = {"plan_id": plan_id}
    try:
        root, b = _load_bound(plan_id, params_root, sf, store.adapter)
    except PhantomAnchor as e:
        rec.update(outcome="BIND_FAILED", phantom_anchor=True, error=str(e))
        if emit_pre:
            print(json.dumps({"phase": "pre", **rec}, default=str), flush=True)
        return rec
    except Exception as e:                             # noqa: BLE001
        rec.update(outcome="BIND_FAILED", error=f"{type(e).__name__}: {e}")
        if emit_pre:
            print(json.dumps({"phase": "pre", **rec}, default=str), flush=True)
        return rec
    rec["params"] = b["params"]
    rec["param_source"] = b["source"]
    rec["or_expansion"] = b["or_expansion"]

    stats = store.stats()
    est = plan_estimate(root, stats)
    hits = [k for k, c in DEFAULT_CEILINGS.items() if est.get(k, 0) > c]
    rec["estimate"] = {k: est[k] for k in
                       ("time_est_ms", "rows_scanned_est", "expansions_est")}
    rec["ceilings_hit"] = hits
    rec["derived_admission"] = "refuse" if hits else "admit"
    rec["policy_version"] = POLICY_VERSION
    # Emitted before a single row is read, so that a child killed at the
    # ceiling still leaves an attributable record. The first bounded campaign
    # produced thirteen rows of {bypassed, note, outcome, wall_s} with no
    # plan_id and no estimate, because the parent stopped computing anything
    # when every plan moved into a child.
    if emit_pre:
        print(json.dumps({"phase": "pre", **rec}, default=str), flush=True)

    ceilings = None
    if hits:
        # capture the certificate the policy would issue, then decide whether
        # to run anyway with the guard bypassed-but-recorded (§C5)
        try:
            from tgms.tgir.admission import admit
            admit(root, stats, plan_id)
        except CostError as e:
            rec["refusal_certificate"] = e.details.get("refusal_certificate")
        if not bypass:
            rec["outcome"] = "REFUSED"
            return rec
        ceilings = {k: 1 << 62 for k in DEFAULT_CEILINGS}
        rec["bypassed"] = True

    t0 = time.time()
    try:
        def go() -> Any:
            return run_plan(root, store.adapter, tt_source=store,
                            limit=100_000, plan_id=plan_id, cost_ceilings=ceilings)

        reps = 1 if hits else REPS
        times, envelope = _time(go, reps)
        rec["outcome"] = "COMPLETED"
        rec["ms"] = sorted(times)[len(times) // 2]
        rec["ms_all"] = [round(x, 3) for x in times]
        rec["rows"] = len(envelope.get("rows", []))
        rec["completeness"] = envelope.get("completeness")
    except CostError as e:
        rec["outcome"] = "REFUSED_ON_RUN"
        rec["error"] = e.to_payload().get("code")
    except TgmsError as e:
        rec["outcome"] = "ERRORED"
        rec["error"] = e.to_payload().get("code")
        rec["error_detail"] = str(e)[:300]
    except Exception as e:                             # noqa: BLE001
        rec["outcome"] = "ERRORED"
        rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    rec["wall_s"] = round(time.time() - t0, 3)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--params", required=True)
    ap.add_argument("--sf", default="sf1")
    ap.add_argument("--out", required=True)
    ap.add_argument("--plan", default="all")
    ap.add_argument("--single", default="",
                    help="internal: run one plan, guard bypassed, print one "
                         "JSON record. Used by the parent to enforce the "
                         "ceiling with a kill.")
    ap.add_argument("--no-bypass", action="store_true",
                    help="skip the guard-bypassed re-run of refused plans")
    args = ap.parse_args()

    import tgms

    ensure_all_registered()

    if args.single:
        store = tgms.open(args.store, read_only=True)
        try:
            rec = run_one(args.single, store, Path(args.params), args.sf,
                          bypass=not args.no_bypass, emit_pre=True)
        finally:
            store.close()
        print(json.dumps({"phase": "final", **rec}, default=str))
        return 0

    sha = _sha()
    ids = LDBC_PLANS if args.plan == "all" else [args.plan]
    print(f"RUN_STARTED commit={sha} store={args.store} sf={args.sf} "
          f"plans={len(ids)} policy={POLICY_VERSION} host={platform.node()}",
          flush=True)

    t0 = time.time()
    records: list[dict[str, Any]] = []
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    def flush() -> None:
        """After every plan, not at the end. The first bounded campaign wrote
        its record only on completion, so killing it at 2 h discarded four
        finished measurements that had to be read back out of the log."""
        out.write_text(json.dumps({"manifest": manifest(), "records": records},
                                  indent=1, sort_keys=True, default=str))

    def manifest() -> dict[str, Any]:
        return {"commit": sha, "host": platform.node(),
                "platform": platform.platform(), "store": args.store,
                "sf": args.sf, "policy_version": POLICY_VERSION,
                "ceilings": dict(DEFAULT_CEILINGS),
                "bypass_ceiling_s": BYPASS_CEILING_S,
                "child_open_allowance_s": CHILD_OPEN_ALLOWANCE_S,
                "every_plan_in_a_child": True,
                "protocol": f"warmups {WARMUPS}, reps {REPS} (1 when bypassed)",
                "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "wall_s": round(time.time() - t0, 1),
                "complete": len(records) == len(ids)}

    for pid in ids:
        t = time.time()
        rec = run_child(pid, args.store, Path(args.params), args.sf)
        records.append(rec)
        flush()
        print(f"  {pid:6s} {rec.get('derived_admission', '—'):>6} -> "
              f"{rec.get('outcome', '?'):<14} "
              f"est {rec.get('estimate', {}).get('time_est_ms', '?'):>12} ms  "
              f"actual {rec.get('ms', '—')!s:>12}  "
              f"rows {rec.get('rows', '—')!s:>8}  "
              f"[{time.time() - t:.1f}s]", flush=True)

    flush()

    admitted = [r for r in records if r.get("derived_admission") == "admit"]
    refused = [r for r in records if r.get("derived_admission") == "refuse"]
    print(f"\nderived: {len(refused)} refuse / {len(admitted)} admit "
          f"of {len(records)}")
    for name in ("COMPLETED", "REFUSED", "REFUSED_ON_RUN", "TIMEOUT",
                 "ERRORED", "BIND_FAILED"):
        n = sum(1 for r in records if r.get("outcome") == name)
        if n:
            print(f"  {name:<16} {n}")
    print(f"record: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ldbc_snb_params import LDBC_PLANS, bind, substitute  # noqa: E402
from tgms.core.errors import CostError, TgmsError  # noqa: E402
from tgms.temporal.algebra import ensure_all_registered  # noqa: E402
from tgms.temporal.guardrails import DEFAULT_CEILINGS  # noqa: E402
from tgms.tgir.admission import POLICY_VERSION, plan_estimate  # noqa: E402
from tgms.tgir.execute import run_plan  # noqa: E402
from tgms.tgir.loader import load  # noqa: E402

#: §C5. The ceiling `external_workloads/FREEZE.md` already fixed for BIRD gold
#: validation, adopted for continuity. No plan runs unbounded.
BYPASS_CEILING_S = 600

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


def _load_bound(plan_id: str, params_root: Path, sf: str) -> tuple[Any, dict[str, Any]]:
    b = bind(plan_id, params_root, sf)
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


def run_one(plan_id: str, store: Any, params_root: Path, sf: str,
            bypass: bool) -> dict[str, Any]:
    rec: dict[str, Any] = {"plan_id": plan_id}
    try:
        root, b = _load_bound(plan_id, params_root, sf)
    except Exception as e:                             # noqa: BLE001
        rec.update(outcome="BIND_FAILED", error=f"{type(e).__name__}: {e}")
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
    if rec.get("wall_s", 0) > BYPASS_CEILING_S:
        rec["outcome"] = "TIMEOUT"
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--params", required=True)
    ap.add_argument("--sf", default="sf1")
    ap.add_argument("--out", required=True)
    ap.add_argument("--plan", default="all")
    ap.add_argument("--no-bypass", action="store_true",
                    help="skip the guard-bypassed re-run of refused plans")
    args = ap.parse_args()

    import tgms

    ensure_all_registered()
    sha = _sha()
    ids = LDBC_PLANS if args.plan == "all" else [args.plan]
    print(f"RUN_STARTED commit={sha} store={args.store} sf={args.sf} "
          f"plans={len(ids)} policy={POLICY_VERSION} host={platform.node()}",
          flush=True)

    store = tgms.open(args.store, read_only=True)
    t0 = time.time()
    records = []
    try:
        for pid in ids:
            t = time.time()
            rec = run_one(pid, store, Path(args.params), args.sf,
                          not args.no_bypass)
            records.append(rec)
            print(f"  {pid:6s} {rec.get('derived_admission', '—'):>6} -> "
                  f"{rec.get('outcome', '?'):<14} "
                  f"est {rec.get('estimate', {}).get('time_est_ms', '?'):>12} ms  "
                  f"actual {rec.get('ms', '—')!s:>12}  "
                  f"rows {rec.get('rows', '—')!s:>8}  "
                  f"[{time.time() - t:.1f}s]", flush=True)
    finally:
        store.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "manifest": {"commit": sha, "host": platform.node(),
                     "platform": platform.platform(), "store": args.store,
                     "sf": args.sf, "policy_version": POLICY_VERSION,
                     "ceilings": dict(DEFAULT_CEILINGS),
                     "bypass_ceiling_s": BYPASS_CEILING_S,
                     "protocol": f"warmups {WARMUPS}, reps {REPS} (1 when bypassed)",
                     "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "wall_s": round(time.time() - t0, 1)},
        "records": records,
    }, indent=1, sort_keys=True, default=str))

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

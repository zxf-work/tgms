#!/usr/bin/env python
"""Propagation ablation over the frozen m8 task-runs (D-170 lane A).

Question: on the recorded agent runs, how many claims that the ECQR
verifier marks UNSUPPORTED would become SUPPORTED if cross-step
propagation (Lemma 3.10: blocking of reducers over incomplete inputs,
rule (a); certificate stripping and delivery marking, rule (c)) were
off? A faithful replay re-verifies each claim against the descriptor
its cited step would carry with propagation off, which needs, per run,
the plan with its arguments, the per-step trace (status, truncation,
descriptor) and the cited result payloads.

This script audits which of those the frozen records carry and
computes exactly what they determine, with no approximation:

- operator arms (ours, ours-noverify): the records carry plan_ops (op
  names only), the reporter's claims, and run-level UCR from the legacy
  trace verifier. They carry no plan arguments, no per-step trace, no
  descriptors and no result payloads, so the per-claim flip count is
  NOT computable and is reported as null with the missing artifacts
  named. What they do determine: the runs whose first execution failure
  is rule (a) blocking (the executor's refusal to reduce a truncated
  input), i.e. runs where propagation changed execution itself. This
  is a lower bound: the record keeps only the first failed step.
- SQL enforced arm (b6e): each record carries the statement's ECQR and
  the per-claim ECQR verdicts. A SQL plan is one statement, so its
  descriptor has no input descriptors; with propagation off the adapter
  emits the identical descriptor and every verdict is unchanged. The
  script checks the premise (provenance.inputs empty) on every stored
  descriptor and reports flips by claim kind.

    python scripts/eval_propagation_ablation.py --runs runs \
        --manifest logs/frozen_runs.sha256 \
        --json benchmarks/results-v1/eval-propagation-ablation.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path

DS = ["sx-mathoverflow", "sx-superuser", "wiki-talk"]
OP_ARMS = ("ours", "ours-noverify")
RULE_A_MARK = "would reduce a truncated result to one number"
#: what a descriptor-level replay of one operator run needs
REPLAY_NEEDS = {
    "plan_with_args": ("plan", "plan_json"),
    "per_step_trace": ("trace", "steps"),
    "per_step_descriptors": ("ecqr",),
    "per_claim_ecqr_verdicts": ("claim_verdicts",),
}


def _records(runs: Path, ds: str, sub: str) -> list[dict]:
    out = []
    for f in sorted(glob.glob(str(runs / f"m8-{ds}-{sub}" / "results" /
                                  "*.json"))):
        out.append(json.loads(Path(f).read_text()))
    return out


def _has_key(obj, names) -> bool:
    if isinstance(obj, dict):
        return any(k in names for k in obj) or any(
            _has_key(v, names) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_key(v, names) for v in obj)
    return False


def operator_arm(runs: Path, ds: str) -> dict:
    rows = _records(runs, ds, "tgms")
    out: dict = {}
    for arm in OP_ARMS:
        rs = [r for r in rows if r.get("system") == arm]
        blocked = [r["task_id"] for r in rs
                   if RULE_A_MARK in json.dumps(r.get("exec_error") or {})]
        n_claims = sum(len((r.get("answer_object") or {}).get("claims")
                           or []) for r in rs)
        present = {need: sum(1 for r in rs if _has_key(r, keys))
                   for need, keys in REPLAY_NEEDS.items()}
        out[arm] = {
            "n_runs": len(rs),
            "n_claims_recorded": n_claims,
            "replay_artifacts_present_in_n_runs": present,
            "rule_a_blocked_runs_lower_bound": len(blocked),
            "rule_a_blocked_task_ids": sorted(blocked),
            "flips_unsupported_to_supported_by_kind": None,
        }
    return out


def sql_arm(runs: Path, ds: str) -> dict:
    rs = [r for r in _records(runs, ds, "sql") if r.get("system") == "b6e"]
    n_desc = with_inputs = 0
    verdicts: dict[str, int] = {}
    unsup_by_kind: dict[str, int] = {}
    for r in rs:
        m = r.get("meta") or {}
        e = m.get("ecqr")
        if e:
            n_desc += 1
            if (e.get("provenance") or {}).get("inputs"):
                with_inputs += 1
        kinds = {c.get("id"): c.get("type", "?") for c in
                 ((m.get("pre_gate_answer") or {}).get("claims") or [])}
        for cv in m.get("claim_verdicts") or []:
            v = cv.get("ecqr_verdict", "?")
            verdicts[v] = verdicts.get(v, 0) + 1
            if v != "SUPPORTED":
                k = kinds.get(cv.get("id"), "?")
                unsup_by_kind[k] = unsup_by_kind.get(k, 0) + 1
    unchanged = with_inputs == 0
    return {
        "n_runs": len(rs),
        "n_descriptors": n_desc,
        "descriptors_with_input_descriptors": with_inputs,
        "claim_verdicts": verdicts,
        "unsupported_claims_by_kind": unsup_by_kind,
        "flips_unsupported_to_supported_by_kind": (
            {k: 0 for k in unsup_by_kind} if unchanged else None),
        "flips_total": 0 if unchanged else None,
    }


def _verify_manifest(manifest: Path, root: Path) -> dict:
    ok = bad = 0
    for line in manifest.read_text().splitlines():
        if not line.strip():
            continue
        digest, path = line.split(None, 1)
        p = root / path.strip()
        h = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""
        ok += h == digest
        bad += h != digest
    return {"manifest": str(manifest), "files_verified": ok,
            "files_failed": bad,
            "manifest_sha256": hashlib.sha256(
                manifest.read_bytes()).hexdigest()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=Path, default=Path("runs"))
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--json", type=Path, required=True)
    args = ap.parse_args()
    if args.json.exists():
        raise SystemExit(f"{args.json} exists; receipts are never "
                         f"overwritten")

    out: dict = {
        "protocol": {
            "question": "claims ECQR marks UNSUPPORTED that become "
                        "SUPPORTED with Lemma 3.10 propagation off",
            "runs": "m8 14B frozen task-runs, 3 datasets x "
                    "{operators: ours, ours-noverify; sql: b6, b6e}",
            "propagation_off": "tgms.agent.executor.Executor("
                               "propagate=False): no reducer blocking, "
                               "descriptors built without input "
                               "descriptors",
            "rule_a_mark": RULE_A_MARK,
            "deterministic": True,
        },
    }
    if args.manifest:
        out["provenance"] = _verify_manifest(args.manifest, args.runs.parent)
    out["operators"] = {ds: operator_arm(args.runs, ds) for ds in DS}
    out["sql"] = {ds: sql_arm(args.runs, ds) for ds in DS}
    n_runs = sum(v[a]["n_runs"] for v in out["operators"].values()
                 for a in OP_ARMS) + sum(
        2 * v["n_runs"] for v in out["sql"].values())
    out["n_task_runs"] = n_runs
    out["operators_missing_for_replay"] = [
        "plan arguments (records keep plan_ops, the op names only)",
        "per-step execution trace (status, truncated, upstream_truncated)",
        "per-step ECQR descriptors",
        "cited result payloads (the content-addressed result store lived "
        "under the regenerable stores/ tree, which was deleted)",
        "per-claim ECQR verdicts (operator rows keep run-level legacy UCR "
        "only; the observational ecqr column was not persisted)",
    ]
    commit = os.environ.get("COMMIT") or subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True,
        text=True).stdout.strip()
    out["manifest"] = {
        "commit": commit, "host": platform.node(),
        "slurm_job": os.environ.get("SLURM_JOB_ID"),
        "date": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"),
        "python": platform.python_version()}
    args.json.write_text(json.dumps(out, indent=1) + "\n")
    for ds in DS:
        o, s = out["operators"][ds], out["sql"][ds]
        print(f"{ds}: rule(a) blocked runs ours="
              f"{o['ours']['rule_a_blocked_runs_lower_bound']} "
              f"noverify={o['ours-noverify']['rule_a_blocked_runs_lower_bound']}"
              f"  sql unsupported={sum(s['unsupported_claims_by_kind'].values())}"
              f" flips={s['flips_total']}")
    print(f"record -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

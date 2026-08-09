#!/usr/bin/env python
"""Verifier scaling and descriptor space, measured (D-126).

The submission-readiness review asks two questions the overhead receipt
(evidence-overhead-itiger.json) leaves open: how do canonicalization,
digesting, descriptor construction, and per-claim verification scale
with delivered-result size, and how many bytes does a serialized ECQR
actually cost. This script answers both.

Timing, per delivered-result size n in {10, 100, 1k, 10k, 100k} rows:
  canonicalize_ms    canonical-JSON serialization of the result payload
  digest_ms          SHA-256 over the already-canonical bytes
  build_ecqr_ms      descriptor construction (digest precomputed, as in
                     the executor path where call_operator digests the
                     payload while materializing the envelope)
  verify_membership_ms   worst case: witness is the last row
  verify_completeset_ms  claimed set == full projected column
  verify_count_cert_ms   certificate path (no row access)
  verify_nonexist_ms     zero-cardinality certificate path
  multiclaim_per_claim_ms  16 mixed claims citing one already-digested
                     descriptor: marginal per-claim verify cost

Space, serialized canonical-JSON bytes per descriptor, decomposed into
semantic core (result_id, basis, completeness, cardinality, exactness),
query-domain, and provenance+semantics bytes, for both adapters.

    python scripts/eval_verifier_scaling.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import time
from pathlib import Path

SIZES = [10, 100, 1_000, 10_000, 100_000]


def _t(fn, reps):
    xs = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t0) * 1e3)
    return statistics.median(xs)


def _reps(n: int) -> int:
    return {10: 400, 100: 400, 1_000: 100, 10_000: 20, 100_000: 5}[n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    from tgms.core.model import canonical_json, sha256_hex
    from tgms.evidence.adapter_sql import build_sql_ecqr
    from tgms.evidence.adapter_tgms import build_ecqr
    from tgms.evidence.claims import (CompleteSet, ExactCount, Existence,
                                      Membership, Nonexistence, Scalar)
    from tgms.evidence.verify import Verdict, verify

    out: dict = {"host": platform.node(), "sizes": SIZES, "timing": []}

    for n in SIZES:
        reps = _reps(n)
        rows = [{"uid": f"u{i:06d}", "value": i} for i in range(n)]
        payload = {"op": "scan_entities", "rows": rows, "truncated": False}
        canon = canonical_json(payload)
        env = {"op": "scan_entities",
               "args_echo": {"window": {"t_a": 0, "t_b": 1}, "limit": n},
               "rows": rows, "rows_total": n, "truncated": False,
               "result_digest": sha256_hex(canon)}
        e = build_ecqr(env, "bench-store")
        assert e.scope.exact_cardinality == n and e.scope.delivery_complete

        last = rows[-1]["uid"]
        members = [r["uid"] for r in rows]
        env_empty = dict(env, rows=[], rows_total=0)
        e_zero = build_ecqr(env_empty, "bench-store")

        row = {
            "rows": n,
            "canonical_bytes": len(canon.encode("utf-8")),
            "canonicalize_ms": _t(lambda: canonical_json(payload), reps),
            "digest_ms": _t(lambda: sha256_hex(canon), reps),
            "build_ecqr_ms": _t(lambda: build_ecqr(env, "bench-store"),
                                reps),
            "verify_membership_ms": _t(
                lambda: verify(Membership(value=last, field="uid"),
                               e, payload), reps),
            "verify_completeset_ms": _t(
                lambda: verify(CompleteSet(members=members, field="uid"),
                               e, payload), reps),
            "verify_count_cert_ms": _t(
                lambda: verify(ExactCount(n=n), e, payload), reps),
            "verify_nonexist_ms": _t(
                lambda: verify(Nonexistence(), e_zero, {"rows": []}), reps),
        }
        # sanity: every timed verdict is the expected one
        assert verify(Membership(value=last, field="uid"),
                      e, payload).verdict == Verdict.SUPPORTED
        assert verify(CompleteSet(members=members, field="uid"),
                      e, payload).verdict == Verdict.SUPPORTED
        assert verify(ExactCount(n=n), e, payload).verdict == \
            Verdict.SUPPORTED
        assert verify(Nonexistence(), e_zero,
                      {"rows": []}).verdict == Verdict.SUPPORTED

        # m mixed claims citing one already-digested descriptor: the
        # digest is bound once per ECQR/result pair; the marginal cost
        # of an additional claim is verify() dispatch only.
        claims = []
        for i in range(16):
            claims.append([Membership(value=rows[i % n]["uid"],
                                      field="uid"),
                           ExactCount(n=n), Existence(),
                           Scalar(path=f"rows[{i % n}].value",
                                  value=i % n)][i % 4])
        row["multiclaim_per_claim_ms"] = _t(
            lambda: [verify(c, e, payload) for c in claims],
            max(2, reps // 4)) / len(claims)
        out["timing"].append({k: (round(v, 5) if isinstance(v, float)
                                  else v) for k, v in row.items()})

    def _space(e) -> dict:
        d = e.to_json()
        total = len(canonical_json(d).encode("utf-8"))
        domain = len(canonical_json(d["scope"]["domain"]).encode("utf-8"))
        prov = len(canonical_json(
            {"provenance": d["provenance"],
             "semantics": d["semantics"]}).encode("utf-8"))
        return {"total_bytes": total, "query_domain_bytes": domain,
                "provenance_semantics_bytes": prov,
                "semantic_core_bytes": total - domain - prov}

    rows100 = [{"uid": f"u{i:06d}", "value": i} for i in range(100)]
    env100 = {"op": "scan_entities",
              "args_echo": {"window": {"t_a": 0, "t_b": 1}, "limit": 100},
              "rows": rows100, "rows_total": 100, "truncated": False,
              "result_digest": sha256_hex(canonical_json(rows100))}
    e_tgms = build_ecqr(env100, "bench-store")
    e_sql = build_sql_ecqr(
        rows=rows100,
        sql="SELECT DISTINCT src AS uid, 1 AS value FROM edge_versions "
            "WHERE tt_s <= ? AND ? < tt_e ORDER BY uid",
        params=[4611686018427387904, 4611686018427387903],
        store_id="bench-store", engine="duckdb", engine_version="1.1.3",
        total_count=100, limited=False)
    out["space"] = {"tgms_operator": _space(e_tgms),
                    "sql_adapter": _space(e_sql),
                    "result_bytes_100_rows": len(
                        canonical_json(rows100).encode("utf-8"))}

    print(json.dumps(out, indent=1))
    if args.json:
        commit = os.environ.get("COMMIT", "")
        if not commit:
            try:
                commit = subprocess.run(["git", "rev-parse", "HEAD"],
                                        capture_output=True,
                                        text=True).stdout.strip()
            except OSError:
                commit = "unknown"
        out["commit"] = commit
        args.json.write_text(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

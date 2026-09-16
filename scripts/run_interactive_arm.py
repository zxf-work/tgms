"""P-SF1b driver: run ONLY the characterization-interactive arm (the 11
original IC/IS plans + the post-freeze IS1/IS4/IS5) through the existing
`tgir_ldbc_sf1.py` machinery, with `--csv` correctly supplied this time.

Not a modification of `tgir_ldbc_sf1.py` -- reuses its own `run_child`,
`manifest`-equivalent constants, `_sha`, `CAMPAIGN_SEED`, `ROWS_DIGEST_RULE`
etc. directly, restricted to the interactive-arm plan ids only. It exists
because the tracked script's `--plan` flag only supports "all" (which would
also run the 10 BI plans of the scored-bi arm -- unwanted here and far more
expensive) or exactly one id (no arm-level filter) -- so this is the
"parent" `main()` loop from `tgir_ldbc_sf1.py`, trimmed to one arm, run from
outside that file so it itself needs no edit. Lives in scripts/ (alongside
`tgir_ldbc_sf1.py`) so `from ldbc_compare import ...` / `from
ldbc_snb_params import ...` resolve exactly as they do for that file.

`--resume`: loads `--out` if it already exists and skips any plan id
already recorded in it, running only what is missing. Added because the
first P-SF1b invocation was cut short after 11 of 14 plans by the *outer*
orchestration tool's own timeout (not a plan or ceiling failure -- each
plan reopens the store fresh in its own child subprocess, ~150-270s wall
each here, dominated by the SF1 index-build warm-up, so 14 plans can
outrun an unrelated wrapper's time budget). A second, `--resume`d
invocation under `setsid nohup` (so it survives its own controlling
session ending) completed the remaining IS5/IS6/IS7 without re-running the
11 already on disk.

Usage (from the repo root of the pinned worktree):
    venv/bin/python scripts/run_interactive_arm.py \
        --store stores/snb-sf1-xz-54dcab0 \
        --params /mnt/project/xzhang/tgms/ldbc-sf1/params \
        --csv /mnt/project/xzhang/tgms/ldbc-sf1/bi-sf1-composite-merged-fk/graphs/csv/bi/composite-merged-fk/initial_snapshot \
        --emit-rows /mnt/project/xzhang/tgms/tmp/sf1b-rows \
        --out benchmarks/results-v1/ldbc-sf1-campaign-fmt3-interactive-2026-09.json
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ itself

import tgir_ldbc_sf1 as T  # noqa: E402
from ldbc_snb_params import IV_SOURCES  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--params", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--sf", default="sf1")
    ap.add_argument("--out", required=True)
    ap.add_argument("--emit-rows", default="")
    ap.add_argument("--resume", action="store_true",
                    help="load --out if it exists and skip plan ids already "
                         "recorded in it (a prior invocation was cut short "
                         "by an outer timeout, not by the plan itself)")
    args = ap.parse_args()

    all_ids = sorted(IV_SOURCES)  # the 14-plan characterization-interactive arm
    assert len(all_ids) == 14, all_ids

    sha = T._sha()
    csv_root = Path(args.csv)
    out = Path(args.out)
    records: list[dict] = []
    if args.resume and out.exists():
        prev = json.loads(out.read_text())
        records = prev.get("records", [])
        done = {r.get("plan_id") for r in records}
        ids = [i for i in all_ids if i not in done]
        print(f"RESUMING: {len(done)} already recorded ({sorted(done)}), "
              f"{len(ids)} remaining", flush=True)
    else:
        ids = all_ids

    print(f"RUN_STARTED commit={sha} store={args.store} sf={args.sf} "
          f"plans={len(ids)} arm=characterization-interactive "
          f"policy={T.POLICY_VERSION} host={platform.node()}", flush=True)

    t0 = time.time()
    out.parent.mkdir(parents=True, exist_ok=True)

    def manifest() -> dict:
        return {
            "commit": sha, "host": platform.node(),
            "platform": platform.platform(), "store": args.store,
            "sf": args.sf, "policy_version": T.POLICY_VERSION,
            "ceilings": dict(T.DEFAULT_CEILINGS),
            "bypass_ceiling_s": T.BYPASS_CEILING_S,
            "child_open_allowance_s": T.CHILD_OPEN_ALLOWANCE_S,
            "every_plan_in_a_child": True,
            "campaign_seed": T.CAMPAIGN_SEED,
            "campaign_seed_source": T.CAMPAIGN_SEED_SOURCE,
            "csv_root": args.csv,
            "rows_digest_rule": T.ROWS_DIGEST_RULE,
            "sort_keys_sha256": T._sort_keys_sha256(),
            "arms": {
                "characterization-interactive":
                    "the 11 Interactive rows, whose LDBC parameters name "
                    "entities of the separately generated Interactive "
                    "dataset (§E addendum 4), plus the 3 post-freeze "
                    "IS1/IS4/IS5 rows. Anchors are seeded draws from this "
                    "corpus; execution-at-scale characterization only. "
                    "Never summed with the scored arm. THIS RECORD covers "
                    "ONLY this arm -- P-SF1b, a targeted rerun; see "
                    "manifest.supersedes.",
            },
            "protocol": f"warmups {T.WARMUPS}, reps {T.REPS} (1 when bypassed)",
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "wall_s": round(time.time() - t0, 1),
            "complete": len(records) == len(all_ids),
            "supersedes": {
                "record": "benchmarks/results-v1/"
                           "ldbc-sf1-campaign-fmt3-2026-09.json",
                "arm": "characterization-interactive",
                "reason": "that record's characterization-interactive arm "
                          "invoked tgir_ldbc_sf1.py without --csv, so all "
                          "14 plans bound raw validation_params-sf1.csv ids "
                          "against the Interactive dataset's own ids "
                          "instead of sample_anchor() draws from this "
                          "store's corpus, and recorded BIND_FAILED / "
                          "phantom_anchor for all of them (csv_root: '' is "
                          "the tell). Its scored-bi arm is unaffected and "
                          "stands as-is; this record does not touch it.",
            },
        }

    def flush() -> None:
        out.write_text(json.dumps({"manifest": manifest(), "records": records},
                                  indent=1, sort_keys=True, default=str))

    for pid in ids:
        t = time.time()
        rec = T.run_child(pid, args.store, Path(args.params), args.sf,
                          csv_root, Path(args.emit_rows) if args.emit_rows else None)
        records.append(rec)
        flush()
        print(f"  {pid:6s} {rec.get('derived_admission', '—'):>6} -> "
              f"{rec.get('outcome', '?'):<14} "
              f"actual {rec.get('ms', '—')!s:>12}  "
              f"rows {rec.get('rows', '—')!s:>8}  "
              f"[{time.time() - t:.1f}s]", flush=True)

    flush()
    print(f"\nrecord: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Print the Gate E table for one `scripts/longevity_run.py` output
directory, and write it as a markdown table alongside the run.

Gate E (`docs/design/M5_EXECUTION_PLAN_2026-08-27.md` §10, frozen
elsewhere — this script reports against it, it does not define it):
bounded metadata growth, deterministic final state, no unbounded memory,
acceptable throughput/latency drift. The manifest's own `summary` block
(written by `longevity_run.py::summarize`) already carries the numbers;
this script only formats them and applies the plan's qualitative reading
(a slope near zero is "bounded", not a hard byte/second threshold, since
that threshold is a §10 decision this script does not make).

    python scripts/longevity_report.py runs/longevity-dev
    python scripts/longevity_report.py runs/longevity-dev --md out.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _find_manifest(out_dir: Path) -> Path:
    candidates = sorted(out_dir.glob("longevity-*.json"))
    if not candidates:
        raise SystemExit(f"no longevity-*.json manifest found under {out_dir}")
    return candidates[-1]


def _fmt(x: Any, unit: str = "") -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:,.3f}{unit}"
    return f"{x}{unit}"


def _verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def build_table(manifest: dict[str, Any], *, label: str | None = None) -> tuple[str, str]:
    """Returns (text_report, markdown_table)."""
    s = manifest.get("summary", {})
    drift = s.get("drift", {})
    growth = s.get("metadata_growth_slope_bytes_per_s", {})

    digest_equal = bool(s.get("digest_equal"))
    verify_healthy = bool(s.get("verify_healthy"))
    mem_slope = s.get("memory_slope_kb_per_s")
    manifest_slope = growth.get("manifests")
    segment_slope = growth.get("segments")
    error_count = s.get("error_count", 0)

    #: Qualitative: a metadata slope that stays at or near zero once
    #: compaction is running periodically is "bounded"; the plan does not
    #: freeze a byte/second number here (§10), so this is a sign check, not
    #: a threshold.
    bounded_metadata = (manifest_slope is not None and manifest_slope <= 1.0)
    no_unbounded_memory = (mem_slope is not None and mem_slope <= 5.0)
    deterministic = digest_equal and verify_healthy
    drift_ok = True
    t_first, t_last = drift.get("throughput_first_hour_avg"), drift.get("throughput_last_hour_avg")
    if t_first and t_last and t_first > 0:
        drift_ok = (t_last / t_first) >= 0.5   # more than a 2x regression is a finding

    rows = [
        ("deterministic final state (verify() clean + replay digest equality)",
         _verdict(deterministic),
         f"verify_healthy={verify_healthy} digest_equal={digest_equal}"),
        ("bounded metadata growth (manifest bytes slope)",
         _verdict(bounded_metadata),
         f"manifests {_fmt(manifest_slope, ' B/s')}, segments {_fmt(segment_slope, ' B/s')}"),
        ("no unbounded memory (RSS slope)",
         _verdict(no_unbounded_memory),
         f"{_fmt(mem_slope, ' kB/s')}"),
        ("throughput/latency drift (first hour vs. last hour)",
         _verdict(drift_ok),
         f"throughput {_fmt(t_first)} -> {_fmt(t_last)} commits/s, "
         f"p99 {_fmt(drift.get('commit_p99_first_hour_max'), ' ms')} -> "
         f"{_fmt(drift.get('commit_p99_last_hour_max'), ' ms')}"),
        ("compaction stalls (max reader p99 during a compaction window)",
         "info", f"{_fmt(s.get('compaction_stall_max_reader_p99_ms'), ' ms')}"),
        ("errors observed", "info" if error_count == 0 else "FLAG", str(error_count)),
        ("recoveries / reader restarts", "info",
         f"{s.get('recoveries', 0)} / {s.get('reader_restarts', 0)}"),
    ]

    tag = f" ({label})" if label else ""
    lines = [f"Gate E — longevity harness{tag}", "=" * 60]
    for name, verdict, detail in rows:
        lines.append(f"  [{verdict:5}] {name}")
        lines.append(f"           {detail}")
    text = "\n".join(lines)

    md = ["| check | verdict | detail |", "|---|---|---|"]
    for name, verdict, detail in rows:
        safe_detail = detail.replace("|", "/")
        md.append(f"| {name} | {verdict} | {safe_detail} |")
    if label:
        md.insert(0, f"**{label}**")
        md.insert(1, "")
    markdown = "\n".join(md) + "\n"
    return text, markdown


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--md", type=Path, default=None,
                    help="markdown table output path (default: <out_dir>/gate_e_report.md)")
    ap.add_argument("--label", default=None,
                    help='e.g. "dev-host, not a reported number"')
    args = ap.parse_args(argv)

    manifest_path = _find_manifest(args.out_dir)
    manifest = json.loads(manifest_path.read_text())
    text, markdown = build_table(manifest, label=args.label)
    print(text)

    md_path = args.md or (args.out_dir / "gate_e_report.md")
    md_path.write_text(markdown)
    print(f"\nwrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

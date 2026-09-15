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

Two things this script does beyond formatting the manifest's `summary`
block directly, both added after `benchmarks/longevity-v1/`'s 24h soak
found the gaps:

- **Deterministic final state** distinguishes `digest_equal: null` ("the
  disk guard skipped the replay step, never checked") from `false`
  ("checked, found unequal") — the former row reads `NOT COMPUTED (replay
  skipped: ...)`, never `FAIL`, and says Gate E is inconclusive on that row.
- **No unbounded memory** additionally computes the median within-life RSS
  regression slope (`median_within_life_slope`, reading the run's own
  `metrics.jsonl`/`recoveries.jsonl` when they are available alongside the
  manifest) and uses *that* for the gate verdict — the naive first-vs-last
  slope over the whole run is dominated by the once-per-restart saw-tooth
  (every writer life is a fresh OS process), not by growth within one live
  process. Falls back to the first-vs-last slope, clearly labeled, when
  those side files are not present locally.

    python scripts/longevity_report.py runs/longevity-dev
    python scripts/longevity_report.py runs/longevity-dev --md out.md
"""

from __future__ import annotations

import argparse
import bisect
import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _find_manifest(out_dir: Path) -> Path:
    candidates = sorted(out_dir.glob("longevity-*.json"))
    if not candidates:
        raise SystemExit(f"no longevity-*.json manifest found under {out_dir}")
    return candidates[-1]


def _find_metrics(out_dir: Path, manifest: dict[str, Any]) -> Path | None:
    """Locate the raw metrics JSONL this manifest's `record` field names —
    needed for the within-life memory slope (median_within_life_slope
    below), which the manifest's own scalar `memory_slope_kb_per_s` cannot
    give us. Not always available locally: `benchmarks/longevity-v1/`'s own
    24h soak keeps its 33 MB `metrics.jsonl` on xzgpu per the PI ruling
    recorded in that directory's README, so this returns `None` there and
    callers must degrade gracefully, not crash."""
    candidates = [out_dir / "metrics.jsonl"]
    record = manifest.get("record")
    if record:
        candidates.append(ROOT / record)
    for c in candidates:
        if c.exists():
            return c
    return None


def _load_writer_rss_points(metrics_path: Path) -> list[tuple[float, float]]:
    """`(ts, value)` for every unlabeled `rss_kb` gauge sample — the
    writer's own (child_reader labels its rss_kb with `reader=<idx>`, so
    filtering on empty labels selects the writer series only)."""
    points: list[tuple[float, float]] = []
    for line in metrics_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("kind") == "gauge" and rec.get("name") == "rss_kb" \
                and not rec.get("labels"):
            points.append((rec["ts"], rec["value"]))
    points.sort()
    return points


def _load_restart_times(out_dir: Path) -> list[float]:
    """Every writer-restart-cycle `t_death` from `recoveries.jsonl` — the
    actual life boundaries, straight from the run's own record, rather than
    inferred by looking for a drop in the RSS series itself."""
    path = out_dir / "recoveries.jsonl"
    if not path.exists():
        return []
    out: list[float] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if "t_death" in rec:
            out.append(float(rec["t_death"]))
    return sorted(out)


def _linreg_slope(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope (value per second) over `points`; `None` when
    there are fewer than two points to fit one from."""
    n = len(points)
    if n < 2:
        return None
    t_bar = sum(t for t, _ in points) / n
    v_bar = sum(v for _, v in points) / n
    den = sum((t - t_bar) ** 2 for t, _ in points)
    if den == 0:
        return 0.0
    num = sum((t - t_bar) * (v - v_bar) for t, v in points)
    return num / den


def median_within_life_slope(points: list[tuple[float, float]],
                             restart_times: list[float]) -> float | None:
    """Median of the per-writer-life linear-regression RSS slope.

    `points` (unsorted `(ts, value)` samples, one writer gauge series) are
    split into segments at each timestamp in `restart_times` (a writer
    life's `t_death`, from `recoveries.jsonl`) so each segment holds one
    writer life's own samples — using the run's own recorded restart times
    rather than trying to detect a life boundary from the RSS series itself
    (which would be fooled by a run whose baseline keeps climbing across
    lives, e.g. a growing store's larger resident index per fresh process:
    a later life's samples can sit *above* an earlier life's, even though a
    restart happened in between).

    This is what "no unbounded memory" should measure: every life is a
    fresh OS process (`benchmarks/longevity-v1/README.md`'s own 24h soak:
    41 restarts, 41 RSS drops of >30%, one per restart — a saw-tooth), so
    the naive first-vs-last slope over the whole, uncorrected series is
    dominated by that saw-tooth, not by growth within a single live
    process. Returns `None` when there are no points, or no life has 2+
    samples to fit a slope from.
    """
    if not points:
        return None
    pts = sorted(points)
    bounds = sorted(restart_times)
    segments: list[list[tuple[float, float]]] = [[] for _ in range(len(bounds) + 1)]
    for t, v in pts:
        segments[bisect.bisect_right(bounds, t)].append((t, v))
    slopes = [sl for seg in segments for sl in [_linreg_slope(seg)] if sl is not None]
    if not slopes:
        return None
    return statistics.median(slopes)


def _fmt(x: Any, unit: str = "") -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:,.3f}{unit}"
    return f"{x}{unit}"


def _fmt_mb_and_tb(mb: Any) -> str:
    """`mb` formatted with both units spelled out, e.g. '280,791,798.0 MB
    (~280.8 TB)' — added so a projected-cost figure can never again be
    mislabeled by unit alone (see benchmarks/longevity-v1/README.md and
    docs/eval_concurrency.md's §24, both of which once mislabeled this same
    figure as "~268 PB")."""
    if mb is None:
        return "n/a"
    try:
        mb_f = float(mb)
    except (TypeError, ValueError):
        return str(mb)
    return f"{mb_f:,.1f} MB (~{mb_f / 1e6:.1f} TB)"


def _verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def build_table(manifest: dict[str, Any], *, label: str | None = None,
                rss_points: list[tuple[float, float]] | None = None,
                restart_times: list[float] | None = None) -> tuple[str, str]:
    """Returns (text_report, markdown_table).

    `rss_points`/`restart_times` are optional: when given (see
    `_load_writer_rss_points`/`_load_restart_times`, called from `main` when
    the run's `metrics.jsonl`/`recoveries.jsonl` are available locally), the
    memory row also reports the median within-life RSS slope and the gate
    verdict is based on it instead of the naive first-vs-last slope.
    """
    s = manifest.get("summary", {})
    drift = s.get("drift", {})
    growth = s.get("metadata_growth_slope_bytes_per_s", {})

    digest_equal_raw = s.get("digest_equal")
    verify_healthy = bool(s.get("verify_healthy"))
    replay_skipped = s.get("replay_skipped")
    mem_slope = s.get("memory_slope_kb_per_s")
    manifest_slope = growth.get("manifests")
    segment_slope = growth.get("segments")
    error_count = s.get("error_count", 0)

    #: Qualitative: a metadata slope that stays at or near zero once
    #: compaction is running periodically is "bounded"; the plan does not
    #: freeze a byte/second number here (§10), so this is a sign check, not
    #: a threshold.
    bounded_metadata = (manifest_slope is not None and manifest_slope <= 1.0)

    median_slope = median_within_life_slope(rss_points, restart_times) \
        if rss_points and restart_times is not None else None
    if median_slope is not None:
        no_unbounded_memory = median_slope <= 5.0
        mem_detail = (f"first-vs-last {_fmt(mem_slope, ' kB/s')}; "
                      f"within-life median {_fmt(median_slope, ' kB/s')} "
                      f"(gate uses the within-life figure)")
    else:
        no_unbounded_memory = (mem_slope is not None and mem_slope <= 5.0)
        mem_detail = (f"first-vs-last {_fmt(mem_slope, ' kB/s')}; "
                      f"within-life median n/a (metrics.jsonl/recoveries.jsonl not "
                      f"available locally — gate falls back to first-vs-last, which "
                      f"a restart saw-tooth can dominate; see "
                      f"benchmarks/longevity-v1/README.md's memory section)")

    # `digest_equal` is `None` ("never checked" — the disk-guard skipped the
    # replay) versus `False` ("checked, and found unequal"): collapsing
    # `None` to `False` (the old `bool(s.get("digest_equal"))`) reported a
    # skipped check as a FAIL indistinguishable from an actual mismatch.
    if digest_equal_raw is None:
        rs = replay_skipped or {}
        reason = rs.get("reason", "unknown")
        det_verdict = f"NOT COMPUTED (replay skipped: {reason})"
        # `projection_kind`/`replay_compact_every` (B7c, 2026-09-15): the
        # disk-guard projection is one of two different formulas depending
        # on whether the replay itself would have compacted periodically —
        # name which one applied rather than leaving it ambiguous.
        kind = rs.get("projection_kind")
        cadence = rs.get("replay_compact_every")
        if kind == "compacted" and cadence:
            kind_note = f"compacted projection, peak within one {cadence}-batch cycle"
        elif kind == "uncompacted":
            kind_note = "uncompacted projection, whole run"
        else:
            kind_note = "projection kind not recorded"
        det_detail = (
            f"Gate E inconclusive on this row — verify_healthy={verify_healthy}, "
            f"digest_equal=not computed; replay skipped because "
            f"total_batches={_fmt(rs.get('total_batches'))} projected "
            f"{_fmt_mb_and_tb(rs.get('projected_mb'))} of manifests ({kind_note}), "
            f"over the limit_mb={_fmt(rs.get('limit_mb'))} ceiling")
    else:
        deterministic = bool(digest_equal_raw) and verify_healthy
        det_verdict = _verdict(deterministic)
        det_detail = f"verify_healthy={verify_healthy} digest_equal={bool(digest_equal_raw)}"

    drift_ok = True
    t_first, t_last = drift.get("throughput_first_hour_avg"), drift.get("throughput_last_hour_avg")
    if t_first and t_last and t_first > 0:
        drift_ok = (t_last / t_first) >= 0.5   # more than a 2x regression is a finding

    rows = [
        ("deterministic final state (verify() clean + replay digest equality)",
         det_verdict, det_detail),
        ("bounded metadata growth (manifest bytes slope)",
         _verdict(bounded_metadata),
         f"manifests {_fmt(manifest_slope, ' B/s')}, segments {_fmt(segment_slope, ' B/s')}"),
        ("no unbounded memory (RSS slope)",
         _verdict(no_unbounded_memory), mem_detail),
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

    metrics_path = _find_metrics(args.out_dir, manifest)
    rss_points = _load_writer_rss_points(metrics_path) if metrics_path else None
    restart_times = _load_restart_times(args.out_dir)

    text, markdown = build_table(manifest, label=args.label,
                                 rss_points=rss_points, restart_times=restart_times)
    print(text)

    md_path = args.md or (args.out_dir / "gate_e_report.md")
    md_path.write_text(markdown)
    print(f"\nwrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

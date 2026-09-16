# live-osv-v1 — Lane F3, the live OSV advisory-feed workload (C10)

This directory holds the first **committed record snapshot** of the live
OSV workload described in
`docs/design/LIVE_WORKLOAD_OSV_DESIGN_2026-09-13.md`. The workload itself
(`scripts/live_osv_poller.py`, one writer, one store) runs continuously on
xzgpu under `scripts/live_supervise.sh`, against
`/mnt/project/xzhang/tgms/live-osv/store` — per the design's own PI ruling
and disk-budget note, that store, its 229 MB event log, and its raw-feed
tarballs stay on xzgpu's project storage, never this repository. What lands
here is a **snapshot manifest**: a schema-valid, digest-checked reading of
that live state at one moment, taken entirely with read-only commands
against the running service (a 24h soak, P-SOAK2, was live on the same host
at snapshot time and was at no point built, run, or opened for a write).

## Snapshot

- **file**: `snapshot-2026-09-16.json`
- **schema**: conforms to `benchmarks/schema/result_manifest.schema.json`
  (`scripts/check_result_manifest.py` — exit 0)
- **sha256** of `snapshot-2026-09-16.json`:
  `1978d6a692f9768dfa51b7260f11d00e13bce203b2765ff1b20de3f8109a3699`
- **snapshot taken**: 2026-09-16T01:09:42Z UTC

## The three numbers this unblocks

`scripts/osdi_paper_macros.py`'s `osdiLiveDays`, `osdiLiveAdvisories`, and
`osdiLiveCorrections` (C10) were PENDING because no committed record existed
for the live-osv workload; this snapshot is that record.

| macro | value | source field in the snapshot |
|---|---|---|
| `osdiLiveDays` | 1.89 days | `live_osv.operation.days_of_operation` |
| `osdiLiveAdvisories` | 32,827 (32,787 bootstrap + 40 live-ingested) | `live_osv.advisories.{bootstrap,total_at_snapshot,new_since_bootstrap}` |
| `osdiLiveCorrections` | 1 | `live_osv.corrections.corrections_written` |

**Days of operation** is computed as `(snapshot_epoch - first_record_epoch)
/ 86400`, where `first_record_epoch` is the `ts` field of line 1 of the
poller's own `metrics.jsonl` on xzgpu (the first cycle of the one and only
poller life this store has had — one `RUN_STARTED commit=4af218192d8c` line
in `run/supervisor.log`, `restart_count` staying `0` through all 38 recorded
cycles) — **not** the design memo's prose ("running by Sep 22" was a
forward-looking target, not a start-date record) and not the bootstrap
script's own `wall_start` line, which is ~2.5 minutes earlier than the first
successful bootstrap's completion. Exact epoch values, both ISO and raw, are
in `live_osv.operation`.

**Advisories ingested** splits the bootstrap count from the live-poll
delta, per the task's own request and the design's §3 accounting: the
bootstrap itself is documented as **32,787 advisories** in
`logs/bootstrap_run_20260914T034428Z.log` ("bootstrapped
.../store: 32787 advisories, ops={'assert_node': 247524, 'assert_edge':
484131}") — the actual run's own count, which differs slightly from the
design memo's 32,912 (a different day's `all.zip` export; MAL- filtering
and per-ecosystem dedup shift the kept-advisory count run to run, and the
memo's own number was explicitly "measured from today's exports" on
2026-09-13, one day before this bootstrap ran). The live total at snapshot
time, read from `state.json`'s own `len(ids)`, is **32,827** — 40 new
advisory ids appended since bootstrap, over 38 poll cycles.

**Corrections applied** is `corrections_written`, summed over all 38 lines
of `metrics.jsonl` — the design's §2 definition of a genuine belief-
changing revision (a `correct` op: severity/range/summary/withdrawn content
actually changed), as opposed to `noop_revisions` (byte-identical
re-serialization; **41 of the 95** revisions seen this run were noop, 43%,
in the ballpark of the design's "~a third of feed churn" estimate) or a
`retraction` (**1** this run, the same cycle that saw the run's one
`withdrawn_seen` advisory — consistent with §2's "withdrawn is deliberately
both": one `correct` + one `retract` from a single upstream event). The sum
across cycles is safe because each cycle only processes ids past its own
high-water mark in `state.json` — cycles are disjoint, so summing
double-counts nothing.

## Provenance and store identity

- **poller commit at bootstrap**: `4af218192d8c` (the one `RUN_STARTED`
  line in `run/supervisor.log`)
- **poller commit at snapshot**: `886805f600bcb447cb0bff16d432a796ba732339`
  — the pinned worktree's `HEAD` advanced from `4af218192d8c` partway
  through this run (`scripts/live_osv_poller.py`'s `_git_sha()` re-shells
  out every cycle rather than caching the commit at process start), but
  `restart_count` stayed `0` and there is only one `RUN_STARTED` line: this
  is one continuous poller life, not two, and the commit-field change in
  `metrics.jsonl` reflects the worktree's `HEAD` moving under it, not a
  restart.
- **store path**: `/mnt/project/xzhang/tgms/live-osv/store` (xzgpu, not
  committed)
- **event log digest** (`dataset.digest`, `digest_kind: eventlog_sha`):
  sha256 `d700bafa660747df68f7f1093b8b54c8ecd82cf4618066cf994d59d2115648cd`
  over `store/eventlog.jsonl` (228,938,169 bytes) — read twice, ~5.5
  minutes apart spanning cycles 37 and 38, identical both times: the
  store's content has been stable since generation 377 (the last cycle
  that actually wrote ops); cycles 37-38 were noop-revision-only and
  touched only `state.json`.
- **store stats at snapshot** (from the poller's own last `metrics.jsonl`
  line, cycle 38, cross-referencing generation 377's content): generation
  377, 247,845 nodes, 497,522 edges, 471,277,791 bytes.
- **failure ledger**: `ops/failure_ledger.jsonl` does not exist and `ops/`
  is empty on xzgpu — zero ledgered failures this run, consistent with
  `feed_errors: 0` and `restart_count: 0` on every one of the 38
  `metrics.jsonl` lines.

## Exact read-only commands used

All commands ran as `ssh -o BatchMode=yes xzhang@xzgpu.uom.memphis.edu
"<command>"`, single-purpose, against files the poller already writes for
itself — no store was opened for a write, no build or engine run was
started, and `/mnt/project/xzhang/tgms/longevity/` (P-SOAK2's own tree) was
never touched:

```
find /mnt/project/xzhang/tgms/live-osv -maxdepth 3
wc -l /mnt/project/xzhang/tgms/live-osv/metrics.jsonl
cat /mnt/project/xzhang/tgms/live-osv/metrics.jsonl
python3 -c "import json; s=json.load(open('/mnt/project/xzhang/tgms/live-osv/state.json')); print(len(s['ids']), s.get('cycles'), s.get('restart_count'))"
grep -o 'RUN_STARTED[^"]*' /mnt/project/xzhang/tgms/live-osv/run/supervisor.log | sort -u
cat /mnt/project/xzhang/tgms/live-osv/logs/bootstrap_run_20260914T034428Z.log
sha256sum /mnt/project/xzhang/tgms/live-osv/store/eventlog.jsonl /mnt/project/xzhang/tgms/live-osv/state.json
wc -c /mnt/project/xzhang/tgms/live-osv/store/eventlog.jsonl
git -C /mnt/project/xzhang/tgms/work/tgms rev-parse HEAD
date -u +%s.%N; date -u '+%Y-%m-%dT%H:%M:%SZ'
uname -srm; nproc; free -g | awk '/Mem:/{print $2}'; hostname
ls -la /mnt/project/xzhang/tgms/live-osv/ops/
ps -p $(cat /mnt/project/xzhang/tgms/live-osv/run/supervisor.pid) -o pid,etimes,cmd
```

The full list, verbatim, is also embedded in the manifest itself under
`live_osv.read_only_commands_used`, and the 38 raw per-cycle
`metrics.jsonl` lines this snapshot summarizes are embedded under
`live_osv.cycles_raw` (a named deviation from the schema's usual
"`record` points at a separate rows file" shape — the whole metrics
history here is 38 small JSON lines, not a large per-row artifact that
needs to live off-repo; see the manifest's own `live_osv.note`).

## What this snapshot is not

This is a snapshot of a workload still running — 1.89 days into what the
design projects as a ~60-day campaign (2026-09-22 through 2026-11-21). It
is not the final C10 record: `osdiLiveDays`/`osdiLiveAdvisories`/
`osdiLiveCorrections` will need to be recomputed from a later snapshot
before the campaign closes, the same way other claims in this repo carry a
non-overwrite discipline across re-measurements. This file exists so those
three macros stop being PENDING now, from real data, rather than staying
blocked until the full campaign window closes.

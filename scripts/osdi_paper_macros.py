#!/usr/bin/env python
"""Generate the OSDI paper's number macros from committed receipts.

House rule (copied verbatim from ``scripts/tgir_paper_macros.py`` and
``scripts/paper_macros.py``): **assert, do not trust.** Every macro this
script emits is recomputed from the row-level fields of the record that
owns it, cross-checked against whatever aggregate the record states for
itself, and then checked against a frozen expected value in
``tests/test_osdi_paper_macros.py``. A disagreement anywhere in that chain
is a hard failure -- the script refuses to write output, never silently
adjusts a number, and never falls back to a record's own summary field
without first recomputing it from the rows underneath.

This is Lane W (task W2)'s receipts machinery for the OSDI paper. The
claims -> evidence map that assigns every macro name to a claim and a
record is ``docs/design/OSDI27_PAPER_SKELETON_2026-09-15.md`` S3 (gitignored,
internal; not shipped with this script, but every macro below documents
its own record path and field so the mapping is reconstructable without
it). Implemented here: every claim whose record has LANDED per that
skeleton --

  C1  benchmarks/crash-v1/eval-crash-campaign-2026-09-13.json
  C3  benchmarks/results-v1/b1-manifest-ab-2026-09{,-raw}.json (v1 B1 A/B) and
      benchmarks/results-v1/b1-manifest-v2-ab-2026-09{,-raw}.json (Lane W2f's
      B1-v2, manifest format 3 -- the reader torn-tail fix -- vs. the pinned
      format-2 soak engine; ``osdiB1v2*`` macros stand beside the v1-era
      ``osdiManifest*`` ones without overwriting them; no verdict macro --
      scoring is the coordinator's, per the internal freeze doc) and
      benchmarks/results-v1/b1-manifest-co7-chain-open-2026-09{,-raw}.json
      (Lane P-CO7's chain-open re-measurement on a confirmed-quiet host,
      treatment ``ebe1dc2``; ``osdiB1co7*`` macros stand beside the v2e-era
      ``osdiB1v2e*`` ones without overwriting them; the two
      ``osdiB1WorstPhaseOpen*`` macros are arithmetic on the co7 per-delta
      costs projected to K-1=511 deltas, labelled as such, not a
      measurement; no verdict macro here either)
  C4  benchmarks/results-v1/b2-version-history-ab-2026-09{,-raw}.json
  C5  benchmarks/results-v1/eval-readers-10m-2026-09.json
  C6  benchmarks/freshness-v1/trials-{full,fixture}.json (M4 record of
      account) + benchmarks/m5-v1/topup-{carve2,propagation2-*}.json,
      the campaign's closing round per
      docs/design/M5_CAMPAIGN_FREEZE_2026-08-27.md Addendum 8 (never read
      as a number source here -- the addendum is prose-only provenance in
      this file's comments; every number below is recomputed from the
      m5-v1 JSON files' own rows)
  C8  benchmarks/faults-v1/fault-matrix-campaign-2026-09-{13,15-d160}.json
  D160-collegemsg (Lane W2c, not in the OSDI27 skeleton's C-numbering --
      the coordinator's D-160 ruling deliverable) --
      benchmarks/d160-collegemsg-v1/{manifest,rows}-2026-09-14.json, the
      CollegeMsg coverage/conditional-accuracy/UCR re-measurement under the
      production claim gate that also drops `unverifiable` claims (see
      docs/STABILITY.md section 9). Its pre-D-160-gate counterparts
      (osdiOldGate*) are parsed out of docs/site_facts.json's
      `unsupported_claims` fact and cross-checked against that same
      STABILITY.md section, never hard-coded independently of both. The
      `llm_direct` follow-up re-run under the real-tokenizer budget fix
      (`manifest-llm-direct-fix-2026-09-14.json` /
      `rows-llm-direct-fix-2026-09-14.json`, job 212231) has now landed --
      `osdiD160LlmDirectCoverageFixed` and its siblings
      (`osdiD160LlmDirectErrorsFixed`/`RawEmFixed`/`TokenizerFixed`/
      `BudgetFixed`) are computed by `compute_d160_llm_direct_fix` below.
      The pre-fix record's own numbers (`osdiD160LlmDirectCarrying`,
      `osdiD160LlmDirectOverflowErrors`) are unchanged and still stand
      beside them, per the campaign's non-overwrite discipline.
  C7  (partial) benchmarks/storm-v1/storm-campaign-dag-{,v2-,v3-}2026-09.json
      (+ each one's -rows.jsonl) -- the DAG-phase v1/v2/v3 grids, all 40/40
      cells each -- and benchmarks/storm-v1/storm-r18-probe-2026-09.json
      (+ -rows.jsonl) -- the N=10,000 c1 seed-0 R-18 characterization probe,
      5/5 batches, not wall-capped. An earlier attempt at the main
      correction-storm cell grid (addendum-1, ``storm-campaign-2026-09.json``,
      12/36 cells complete, 24 blocked on an iTiger disk-quota incident) was
      never committed and stays a dead end -- its own quantities
      (``osdiStormCells``/``osdiStormFalseFresh``/``osdiTtfSpeedup``/
      ``osdiStormSpeedupN1k``/``osdiStormAvoidedN1k``) were retired (no
      longer emitted, PENDING or otherwise) since no record under that
      name ever landed and no paper text references those legacy names.
      Addendum-1's grid itself since **has** landed under
      a different name and seam (Lane W2m; ``storm-v1-main-grid-2026-09-15
      .json`` + its -rows.jsonl, five submissions across the same quota
      incident, 36/36 cells, commit ``8962b78``, pre-D-161-rollout) -- its
      macros are ``osdiStormV1*`` (``compute_c7_storm_v1`` below), which
      resolves the previously-PENDING ``osdiStormV1SpeedupN1kSeed0`` stub
      with a value read from this record instead of from
      benchmarks/storm-v1/README.md's prose. The v1-scoring quantities the
      C6 pre-registration template asked for -- row-touch/entity-touch/
      window-overlap false-fresh rates, the P4 (wall-avoided <
      decision-avoided) check -- are per-cell aggregates (``Sum(false_fresh)
      / Sum(changed_count)`` over each cell's own batches, then median
      across cells) read from ``storm-v1-records-36-tasks.tar.gz``'s
      per-batch rows, sha256-checked against README.md's own quoted value,
      the same in-memory ``tarfile`` discipline as Lane W2l's v2 tarball
      read below. A *different* 36-cell grid -- the same (store x mix x age
      x seed) recipe, rerun post-D-161-rollout under commit ``fdd393c``
      (addendum-3, ``storm-v2-main-grid-2026-09-15.json`` + its -rows.jsonl,
      job 212294) -- **has** also landed; its macros are ``osdiStormV2*``
      (``compute_c7_storm_v2`` below) and stand beside ``osdiStormV1*``
      without overwriting or resolving them -- a v1/v2 speedup comparison
      is prose-only (README.md's own two main-grid sections), never a
      macro. The survivor-fraction/precision pair
      (``osdiStormV2SurvivorFractionC1Median``/``osdiStormV2PrecisionC1Median``)
      is a per-batch quantity (``candidate_survivors``/``changed_count``)
      that only the per-task ``-rows.jsonl`` sidecars carry, and those
      sidecars stay on iTiger's stage directory by design
      (``storm_campaign_merge.py``'s own module note) -- never committed
      individually, unlike the per-cell summaries embedded in
      ``storm-v2-main-grid-2026-09-15-rows.jsonl`` itself. Lane W2l landed
      this pair anyway: ``benchmarks/storm-v1/storm-v2-records-36-tasks.tar.gz``
      (sha256-checked against ``benchmarks/storm-v1/README.md``'s own
      quoted value) packs all 36 tasks' per-batch ``-rows.jsonl`` files as
      transferred from the cluster, so the 12 c1-mix cells' 240 batches
      (``osdiStormV2C1Batches``) are read from it in memory (``tarfile``,
      nothing extracted to the repo) and matched to their cell via each
      merged-grid row's own ``record`` field. The overall and per-store
      medians (``osdiStormV2{SurvivorFraction,Precision}{Synth,CollegeMsg}
      C1Median``) are computed in ``compute_c7_storm_v2`` below alongside
      the rest of the c1-mix quantities.

  W2m (storm-v2 R-18 probe, job 212295, addendum-3) --
      benchmarks/storm-v1/storm-v2-r18-probe-2026-09-15.json (+
      -rows.jsonl), the same cell as the v1 R-18 probe above
      (synth-iv-60k/c1/N=10,000/seed=0/5 batches) rerun post-D-161-rollout
      under commit ``fdd393c``. ``osdiStormV2Probe*``
      (``compute_c7_storm_v2_probe`` below) land beside the still-untouched
      ``osdiR18*`` (v1) macros and beside ``osdiStormV2*`` (the different
      36-cell main-grid record) -- no v1-vs-v2 or probe-vs-grid comparison
      macro here, only prose (README.md's own "R-18 probe v2 (addendum-3)"
      table); ``osdiStormSpeedupShrinksWithN`` in particular is a verdict,
      not a macro, and is deliberately never added.

  W2g (24h longevity soak, Lane B task B7a, Gate G1/Gate E) --
      benchmarks/longevity-v1/longevity-synth-1m-native-0.json (manifest) +
      recoveries.jsonl, reader_restarts.jsonl, longevity_ledger.jsonl,
      orchestrator.log (whole-file sha256-checked against README.md's own
      Files-here table), and writer_error_counts_by_life.json (a locally
      derived, un-hashed side-file whose per-life sum this script recomputes
      rather than trusting). No verdict macro -- Gate E's own PASS/FAIL/FLAG
      table is gate_e_report.md, not this script.

  W2j (same soak, 2026-09-15 re-derived Gate E report + the aborted
      post-hoc replay check) -- benchmarks/longevity-v1/
      summary_rederived_2026-09-15.json (whole-file sha256-checked, frozen
      here; its own ``derived_from.original_manifest_sha256`` field is
      cross-checked against W2g's LONGEVITY_MANIFEST_SHA256 above -- proof
      the re-derivation ran against the *same* soak, not a different one)
      + gate_e_report_rederived_2026-09-15.md (whole-file sha256-checked,
      frozen here) for the within-life writer/reader RSS-slope macros, and
      replay-check-2026-09-15.json (whole-file sha256-checked against
      README.md's own quoted value in its "Post-hoc replay check" section)
      for the aborted-OOM replay record. Both are read-only
      re-measurements/re-derivations against the W2g soak's own preserved
      raw inputs -- no new soak, no new replay attempt. No verdict macro.

  W2p (P-SOAK2, post-fix second soak, OSDI'27 plan Sec 4.3b) --
      benchmarks/longevity-v1/longevity-synth-1m-native-1.json (manifest,
      commit ``eed91c0``, 4 writer lives at ``--restart-every 6h``) +
      rss_slopes-2.json, reader_error_counts_by_class-2.json,
      writer_error_counts_by_life-2.json, gate_e_report-2.md,
      verify-full-soak2-2026-09-17.txt, and recoveries-2.jsonl, every one
      whole-file sha256-checked against README.md's "Soak 2 (post-fix)"
      section's own "Files added here" table. Unlike W2g, the end-of-run
      replay/digest step actually ran this time (``digest_equal: True``)
      and the dedicated full-mode ``tgms check`` cleared the
      believed-versions-overlap class entirely (0 findings, vs. W2g's
      13,714) -- both are the record's strongest positive signals. The
      new, unpredicted finding is a reader-side ``OSError``/``StateError``
      failure storm (150,476,512 reader errors, 0 in W2g) that this script
      also verifies down to the by-class totals. ``compute_longevity_soak_two``
      below computes every ``osdiSoak*Two`` macro; the Gate E table's own
      verdict column is prose (gate_e_report-2.md), not emitted here.
      Added later (2026-09-18, lane W2v): compactions-2.jsonl (the writer's
      raw per-compaction log, copied from xzgpu) and reader_onset_rows-2.json
      (the edge-row count at the reader-error storm's earliest/latest
      onset, reconciling compactions-2.jsonl's ``time.perf_counter()``
      clock against reader_error_counts_by_class-2.json's wall-clock onset
      via metrics.jsonl's ``compactions_total`` counter -- method and
      error bound in the side-file itself) round out the same
      ``compute_longevity_soak_two`` function's ``osdiSoakReaderOnset*Two``
      macros.

  W2u (P-STORM-HUNT, a 6h observation-only run, commit ``57952fa``) --
      benchmarks/longevity-v1/stormhunt-2026-09-17.json (manifest, single
      writer life, no restarts) + rss_slopes-stormhunt.json,
      writer_error_counts_by_class-stormhunt.json, host_load-stormhunt.log,
      reader_op_error-stormhunt.jsonl (empty) + its README.txt,
      gate_e_report-stormhunt.md, and build_info-stormhunt.log, every one
      whole-file sha256-checked against README.md's "P-STORM-HUNT (6 h
      observation run, 2026-09-17/18)" section's own "Files added here"
      table. Purpose: try to capture the P-SOAK2 reader-side
      ``OSError``/``StateError`` storm recurring, under the D-088 bounded
      reader-error-message capture merged at ``c3a5592``; it did not recur
      (0 reader errors, 0 ``reader_op_error`` events) -- all 175 errors are
      the same writer-side ``NotFoundError`` correction-race class seen in
      both soaks. Both reader RSS slopes (11.4-12.5 kB/s) exceed the 10
      kB/s frozen bound every P-SOAK2 reader passed under.
      ``compute_longevity_soak_hunt`` below computes every ``osdiSoak*Hunt``
      macro; ``osdiSoakHuntPatternReproduced`` states the pre-registration's
      own clause (e) non-conclusion (a negative 6h result is not evidence
      the pattern is gone) as its provenance, not as a number.

  W-lane (the original soak's full-mode verify + REPLAY-2) --
      benchmarks/longevity-v1/verify-full-2026-09-15.txt (two `tgms check`
      entries concatenated: the pre-replay check of the original store,
      then the post-hoc full verify of REPLAY-2's own replayed store) and
      replay-check-2-2026-09.json (REPLAY-2 itself, the post-D-087-fix
      replay that completed, unlike W2j's OOM-killed attempts 1/2 above).
      Both whole-file sha256-checked (frozen here, same discipline as
      W2j's pair). ``compute_longevity_verify_and_replay2`` below computes
      every ``osdiSoakVerifyFull*``/``osdiSoakReplay2*`` macro; no verdict
      macro (``CORRUPT`` is the verifier's own text, not scored here).

  W-lane (P-OV1, the xzgpu-calibrated overload sweep, EXP-B4) --
      benchmarks/overload-v1/{overload-2026-09-15,overload-2026-09-15-rep2}.json
      (2 reps, `--clients 1 2 4 8 16 32 64 --max-concurrent 8`,
      `entity_history` under load) + hwm-checkpoints-v3.json (the "Pinned"
      VmHWM localization). ``compute_overload`` below computes every
      ``osdiOverload*`` macro, sha256-checked by hash membership against
      benchmarks/overload-v1/SHA256SUMS; no verdict macro (the per-clause
      PASS/REFUTED scoring is README.md's own prose).

C10 (live OSV workload) -- benchmarks/live-osv-v1/snapshot-2026-09-16.json,
Lane C10-snap's first committed record snapshot of the live-osv poller
running on xzgpu (docs/design/LIVE_WORKLOAD_OSV_DESIGN_2026-09-13.md).
``osdiLiveDays``/``osdiLiveAdvisories``/``osdiLiveCorrections`` are computed
by ``compute_c10_live_osv`` below, every number recomputed from the
snapshot's own embedded per-cycle rows (``live_osv.cycles_raw``) and
cross-checked against its pre-aggregated fields, plus a whole-file sha256
tamper check against README.md's own quoted value.

B7 (scale campaign) -- ``docs/design/SCALE_BUILD_FORECAST_2026-09-15.md``
(gitignored, internal; not shipped with this script, but every field below
is read from a committed record). Stage 0 (iTiger calibration,
``benchmarks/scale-v1/itiger-calib-{1m,10m}.json``) supplies k_build/
k_recover and the 10M scale-curve anchor; Stage 1 30M (``build-30m.json``
+ eight sidecars, scored in the pre-registration's Addendum 7) and Stage 1
100M (``build-100m.json`` + seven sidecars, merge of ``fe52997``) are both
fully landed. ``compute_b7_scale`` below computes every ``osdiB7*{30M,100M}``
macro, the Stage-0 anchors, and the (scale-independent) ``osdiB7300MGate``,
whole-file sha256-checking every record it reads against the sha256 table
in its own README (``benchmarks/scale-v1/README.md`` for Stage 1,
``itiger-calib-2026-09.README.md`` for Stage 0) before trusting anything
inside it. At 100M, 7 of the 13 scale-curve operators are refused by the
cost guardrail: ``reach.window`` (anticipated by Addendum 5, carries a
numeric ``time_est_ms`` estimate) stays a PENDING
``osdiB7ScaleCurveP50ReachWindow100M`` naming that estimate as the reason;
the other six (unanticipated, no numeric estimate in the record) land as
``osdiB7Refused<Op>100M = true`` instead of a fabricated p50.
``osdiB7Recovery100M`` aliases ``osdiB7RecoveryCe5000At100M`` since no
cadence-500 100M run exists or was ever planned (Addendum 6 pre-judged it
infeasible). Nothing here is PENDING for lack of a landed record anymore;
only the one guardrail-refused p50 remains.

Claim C9, independent-validation axis (Lane W2t, ldbc-ref-v1) --
``benchmarks/ldbc-ref-v1/compare-2026-09-18.json`` (revision of record,
manifest + per-template verdicts), cross-checked against
``README.md``'s verdict table and ``campaign.yaml``'s addendum_3 (the
frozen gate/scoring addendum; addenda 1/2 are superseded readings kept
for provenance and never a number source here), and against the
superseded interim ``compare-2026-09-17.json`` for the two
``osdiLdbcInterim*`` macros that let the paper cite the reference-side
fix. This resolves the three stubs that used to stand here
(``osdiLdbcExpressible``/``osdiLdbcExecuted``/``osdiLdbcValidated``) and
lands the rest of the scorecard beside them --
``compute_ldbc_ref_v1`` below computes every ``osdiLdbc*`` macro, every
whole-file sha256 checked against the run's own ``SHA256SUMS.txt`` and
every count recomputed from the compare record's row-level
``attempted``/``compared``/``agreeing``/``disagreeing`` fields, never
taken from its own aggregate or from prose. This lane's scope is the
independent-validation axis only; the broader four-axis LDBC generality
claim's other axes are outside it and are not addressed here. The
still-unlanded slice of C7 above and the one guardrail-refused B7 p50
are emitted as PENDING stubs (see ``Macros.add_pending``) that raise a
real LaTeX error (``\\errmessage``) if the paper ever expands one,
rather than silently emitting a placeholder number.

Every macro is emitted as
``\\expandafter\\newcommand\\csname osdi<Name>\\endcsname{<value>}`` rather
than a bare ``\\newcommand{\\osdi<Name>}``: roughly two-thirds of the ~400
names carry digit tokens (e.g. ``osdiB7BuildWall30M``), and a LaTeX
control-sequence name may not contain a digit outside ``\\csname``. The
file also defines, once at its top, ``\\providecommand{\\osdi}[1]{\\csname
osdi#1\\endcsname}`` so manuscript prose can write ``\\osdi{B7BuildWall30M}``
instead of the raw ``\\csname`` form. Digit-free names still work under
their bare ``\\osdiFoo`` spelling unmodified, since ``\\csname
osdiFoo\\endcsname`` denotes that same control sequence.

Usage:  $HOME/.venvs/tgms/bin/python scripts/osdi_paper_macros.py [--check | --check-only]

Every mode first recomputes and verifies all macros (assert, do not trust,
per the house rule above); a verification failure exits 1 regardless of
flags. With no flag, the script writes
``paper/osdi/generated/osdi-macros.tex`` unconditionally (creating
``paper/osdi/generated/`` if needed). ``--check`` additionally regenerates
that file when it is stale or missing -- e.g. a fresh worktree, or after a
merge, whose gitignored ``paper/`` tree lags the committed records it is
derived from -- and re-verifies the write, printing "regenerated" or "up to
date"; it exits 0 once the file matches, never failing merely because the
file was stale. ``--check-only`` (alias ``--no-write``) is the old strict
contract: it never writes, and exits 1 with "stale generated file: ..." if
the file would differ -- use this where a write is undesired (e.g. a CI
gate). Nothing under ``paper/`` is committed (``paper/`` is gitignored
publicly); this is the local convention this script and
``scripts/osdi_paper_figures.py`` share for that directory. As of 2026-09
no CI workflow invokes this script at all (nothing under ``.github/workflows/``
references it) -- there is no committed generated file for CI to check
staleness against, so CI's actual paper-side gate is just that this script
and its test suite (``tests/test_osdi_paper_macros.py``) pass; ``--check-only``
is provided for if/when a workflow starts calling it directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import tarfile
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "paper" / "osdi" / "generated"

CRASH_V1 = ROOT / "benchmarks" / "crash-v1" / "eval-crash-campaign-2026-09-13.json"

B1_RAW = ROOT / "benchmarks" / "results-v1" / "b1-manifest-ab-2026-09-raw.json"

B1_V2_MANIFEST = ROOT / "benchmarks" / "results-v1" / "b1-manifest-v2-ab-2026-09.json"
B1_V2_RAW = ROOT / "benchmarks" / "results-v1" / "b1-manifest-v2-ab-2026-09-raw.json"

B1_V2E_MANIFEST = ROOT / "benchmarks" / "results-v1" / "b1-manifest-v2e-remeasure-2026-09.json"
B1_V2E_RAW = ROOT / "benchmarks" / "results-v1" / "b1-manifest-v2e-remeasure-2026-09-raw.json"

B1_CO7_MANIFEST = ROOT / "benchmarks" / "results-v1" / "b1-manifest-co7-chain-open-2026-09.json"
B1_CO7_RAW = ROOT / "benchmarks" / "results-v1" / "b1-manifest-co7-chain-open-2026-09-raw.json"

B2_SUMMARY = ROOT / "benchmarks" / "results-v1" / "b2-version-history-ab-2026-09.json"
B2_RAW = ROOT / "benchmarks" / "results-v1" / "b2-version-history-ab-2026-09-raw.json"
VH_FORECAST = ROOT / "docs" / "design" / "BOUNDED_VERSION_HISTORY_FORECAST_2026-09-13.md"

READERS_10M = ROOT / "benchmarks" / "results-v1" / "eval-readers-10m-2026-09.json"

FRESH_FULL = ROOT / "benchmarks" / "freshness-v1" / "trials-full.json"
FRESH_FIXTURE = ROOT / "benchmarks" / "freshness-v1" / "trials-fixture.json"
M5_CARVE_TWO = ROOT / "benchmarks" / "m5-v1" / "topup-carve2-synth-iv-60k.json"
M5_PROP_TWO = [
    ROOT / "benchmarks" / "m5-v1" / f"topup-propagation2-{store}.json"
    for store in ("bitcoinotc", "collegemsg", "sx-mathoverflow")
]

FAULTS_PRE = ROOT / "benchmarks" / "faults-v1" / "fault-matrix-campaign-2026-09-13.json"
FAULTS_POST = ROOT / "benchmarks" / "faults-v1" / "fault-matrix-campaign-2026-09-15-d160.json"

STORM_V1 = ROOT / "benchmarks" / "storm-v1"
DAG_V1 = STORM_V1 / "storm-campaign-dag-2026-09.json"
DAG_V1_ROWS = STORM_V1 / "storm-campaign-dag-2026-09-rows.jsonl"
DAG_V2 = STORM_V1 / "storm-campaign-dag-v2-2026-09.json"
DAG_V2_ROWS = STORM_V1 / "storm-campaign-dag-v2-2026-09-rows.jsonl"
DAG_V3 = STORM_V1 / "storm-campaign-dag-v3-2026-09.json"
DAG_V3_ROWS = STORM_V1 / "storm-campaign-dag-v3-2026-09-rows.jsonl"
R18_PROBE = STORM_V1 / "storm-r18-probe-2026-09.json"
R18_PROBE_ROWS = STORM_V1 / "storm-r18-probe-2026-09-rows.jsonl"
STORM_V2_MAIN_GRID = STORM_V1 / "storm-v2-main-grid-2026-09-15.json"
STORM_V2_MAIN_GRID_ROWS = STORM_V1 / "storm-v2-main-grid-2026-09-15-rows.jsonl"
# Lane W2m: the v2 R-18 probe (job 212295, addendum-3, post-D-161-rollout,
# same cell as R18_PROBE above -- synth-iv-60k/c1/N=10,000/seed=0/5
# batches). Both files committed directly (no records tarball needed, per
# README.md's "R-18 probe v2 (addendum-3)" section).
STORM_V2_R18_PROBE = STORM_V1 / "storm-v2-r18-probe-2026-09-15.json"
STORM_V2_R18_PROBE_ROWS = STORM_V1 / "storm-v2-r18-probe-2026-09-15-rows.jsonl"
STORM_V1_README = STORM_V1 / "README.md"
STORM_V2_RECORDS_TARBALL = STORM_V1 / "storm-v2-records-36-tasks.tar.gz"
# Lane W2l: benchmarks/storm-v1/README.md's own "Per-batch rows" section
# quotes this sha256 for the tarball; cross-checked against that quoted
# text in compute_c7_storm_v2 below, not only frozen from a first read.
STORM_V2_RECORDS_TARBALL_SHA256 = "f1acac9ed96a3fca8ea76aeba8657c6bced8d726244204a899ee6210a714ed0d"
# Lane W2m: addendum-1's own 36-cell main grid (pre-D-161-rollout, commit
# 8962b78), landed under this name -- a different file from the never-
# committed storm-campaign-2026-09.json partial attempt add_pending_stubs
# below still references.
STORM_V1_MAIN_GRID = STORM_V1 / "storm-v1-main-grid-2026-09-15.json"
STORM_V1_MAIN_GRID_ROWS = STORM_V1 / "storm-v1-main-grid-2026-09-15-rows.jsonl"
STORM_V1_RECORDS_TARBALL = STORM_V1 / "storm-v1-records-36-tasks.tar.gz"
# Lane W2m: benchmarks/storm-v1/README.md's own "storm-v1 main grid" section
# quotes this sha256 for the tarball; cross-checked against that quoted
# text in compute_c7_storm_v1 below, not only frozen from a first read.
STORM_V1_RECORDS_TARBALL_SHA256 = "a5396d6ffba3b13bb57e2f2ed004a67ae2fdcd80a639e69efe7849f1d667ee63"

D160_DIR = ROOT / "benchmarks" / "d160-collegemsg-v1"
D160_MANIFEST = D160_DIR / "manifest-2026-09-14.json"
D160_ROWS = D160_DIR / "rows-2026-09-14.json"
D160_MANIFEST_FIX = D160_DIR / "manifest-llm-direct-fix-2026-09-14.json"
D160_ROWS_FIX = D160_DIR / "rows-llm-direct-fix-2026-09-14.json"
D160_SUITE = ROOT / "benchmarks" / "frozen-v1" / "suite-collegemsg.json"
SITE_FACTS = ROOT / "docs" / "site_facts.json"
STABILITY_MD = ROOT / "docs" / "STABILITY.md"

CORRUPTION_PRE = ROOT / "benchmarks" / "corruption-v1" / "eval-corruption-campaign-2026-09-14.json"
CORRUPTION_POST = (ROOT / "benchmarks" / "corruption-v1"
                    / "eval-corruption-campaign-2026-09-14-post-a10.json")

LADDER_DIR = ROOT / "benchmarks" / "ladder-v1"
LADDER_MERGED = LADDER_DIR / "ladder-2026-09-14.json"
LADDER_RAW = [
    LADDER_DIR / "raw" / "overhead-ladder-bitcoinotc-seed0-job212303.json",
    LADDER_DIR / "raw" / "overhead-ladder-bitcoinotc-seed1-job212304.json",
    LADDER_DIR / "raw" / "overhead-ladder-bitcoinotc-seed2-job212305.json",
]

LONGEVITY_DIR = ROOT / "benchmarks" / "longevity-v1"
LONGEVITY_MANIFEST = LONGEVITY_DIR / "longevity-synth-1m-native-0.json"
LONGEVITY_RECOVERIES = LONGEVITY_DIR / "recoveries.jsonl"
LONGEVITY_READER_RESTARTS = LONGEVITY_DIR / "reader_restarts.jsonl"
LONGEVITY_LEDGER = LONGEVITY_DIR / "longevity_ledger.jsonl"
LONGEVITY_ORCHESTRATOR_LOG = LONGEVITY_DIR / "orchestrator.log"
LONGEVITY_WRITER_ERRORS_BY_LIFE = LONGEVITY_DIR / "writer_error_counts_by_life.json"
LONGEVITY_GATE_E_REPORT = LONGEVITY_DIR / "gate_e_report.md"
# benchmarks/longevity-v1/README.md's own "Files here" table -- the five
# record files verified byte-identical (sha256) between xzgpu and this
# committed copy before commit. writer_error_counts_by_life.json and
# gate_e_report.md are the README's own "derived locally" pair and are
# deliberately absent from this table (and from the digest check below);
# their numbers are recomputed from the hash-checked files instead.
LONGEVITY_MANIFEST_SHA256 = "a94a0c2c3d343d84161c911f04c6a29b586a4b3ddfc113bffb38549d988b3500"
LONGEVITY_RECOVERIES_SHA256 = "a3ef427f47a4801ffcb4eab03bd05fd4d979b6d3ce507318b01c89783b3081da"
LONGEVITY_READER_RESTARTS_SHA256 = "ff7375c22a6c660ab565641d8ecce6a82de2de7a0628e20d50b7eda6f50170fe"
LONGEVITY_LEDGER_SHA256 = "edc13c40f50b849ee4fde1adfdad1ebbbed0e7be24e97a853bea4e0e414d7db4"
LONGEVITY_ORCHESTRATOR_LOG_SHA256 = "3c66d62554a1d19051a166510ec4f003af0f9b4e3ed36c4ceca7da5dce80f7a0"

# Lane W2j -- the 2026-09-15 re-derived Gate E report (post-fix harness,
# same soak) and the same-day post-hoc replay check, both read-only
# re-measurements against the W2g soak's own preserved raw inputs; see
# benchmarks/longevity-v1/README.md's "Re-derived Gate E report" and
# "Post-hoc replay check" sections. Neither file appears in that README's
# "Files here" table (that table is the *original* soak's five files
# only), so their whole-file sha256 is frozen here instead, from this
# lane's own first read of the committed copies -- a tamper test, not a
# cross-check against a second copy of the number recorded elsewhere.
LONGEVITY_SUMMARY_REDERIVED = LONGEVITY_DIR / "summary_rederived_2026-09-15.json"
LONGEVITY_GATE_E_REPORT_REDERIVED = LONGEVITY_DIR / "gate_e_report_rederived_2026-09-15.md"
LONGEVITY_REPLAY_CHECK = LONGEVITY_DIR / "replay-check-2026-09-15.json"
LONGEVITY_SUMMARY_REDERIVED_SHA256 = "b2b873eec314c9646f88b892434518c61fb3c6702ff0af9842d9da9b653e14d1"
LONGEVITY_GATE_E_REPORT_REDERIVED_SHA256 = "8ad5cf1fd22306d41255ab1560d72e50b5256569f88fccbb1b82b362d0c7c7b8"
# This one *is* independently recorded elsewhere: README.md's "Post-hoc
# replay check" section quotes it verbatim ("Files: replay-check-2026-09-15.json
# (sha256 ...)"), so this constant is cross-checked against that quoted
# text below, not only frozen from a first read.
LONGEVITY_REPLAY_CHECK_SHA256 = "a5c7a93c79af6a97160f7262fd16c98ff99cb982f22d282e896f3805ab7e4d9b"

# Lane W2p -- P-SOAK2, the post-fix second soak (commit eed91c0). Every
# path below is whole-file sha256-checked against
# benchmarks/longevity-v1/README.md's "Soak 2 (post-fix)" section's own
# "Files added here" table, exactly as W2g's five-file table is checked
# above -- not an independently-frozen first-read digest like W2j's pair.
LONGEVITY_MANIFEST_TWO = LONGEVITY_DIR / "longevity-synth-1m-native-1.json"
LONGEVITY_RECOVERIES_TWO = LONGEVITY_DIR / "recoveries-2.jsonl"
LONGEVITY_GATE_E_REPORT_TWO = LONGEVITY_DIR / "gate_e_report-2.md"
LONGEVITY_WRITER_ERRORS_BY_LIFE_TWO = LONGEVITY_DIR / "writer_error_counts_by_life-2.json"
LONGEVITY_READER_ERRORS_BY_CLASS_TWO = LONGEVITY_DIR / "reader_error_counts_by_class-2.json"
LONGEVITY_RSS_SLOPES_TWO = LONGEVITY_DIR / "rss_slopes-2.json"
LONGEVITY_VERIFY_FULL_TWO = LONGEVITY_DIR / "verify-full-soak2-2026-09-17.txt"
# Added later (2026-09-18, lane W2v): the raw per-compaction log copied
# from xzgpu, and the side-file that reconciles its perf_counter clock
# against reader_error_counts_by_class-2.json's wall-clock onset times to
# answer "what edge-row count was the store at when the reader
# OSError/StateError storm's onset began" -- see both files' own sha256
# rows appended (append-only) to README.md's "Files added here" table.
LONGEVITY_COMPACTIONS_TWO = LONGEVITY_DIR / "compactions-2.jsonl"
LONGEVITY_READER_ONSET_ROWS_TWO = LONGEVITY_DIR / "reader_onset_rows-2.json"
LONGEVITY_MANIFEST_TWO_SHA256 = "19b597db12c6830859f5317ec8bdb630e4599c2fed8924ed26d41edc1cc23f68"
LONGEVITY_RECOVERIES_TWO_SHA256 = "23a70f0e38c89ab1478a13389c93e86112aceabc2b07b91fc18ca669ad34827b"
LONGEVITY_GATE_E_REPORT_TWO_SHA256 = "4a02fb42f092be22c39dfc2eed21170310e9c5dfd88836b42d459a1bb9da9814"
LONGEVITY_WRITER_ERRORS_BY_LIFE_TWO_SHA256 = "d46fa237e5b02c6420c0735f7375454e82051a6c666ec6b3bb32183d13ef6126"
LONGEVITY_READER_ERRORS_BY_CLASS_TWO_SHA256 = "e6d8624fc410c26289541557b17de132aeb08597b4c9bf83327c3cf22de34f41"
LONGEVITY_RSS_SLOPES_TWO_SHA256 = "8a690ece6a9579b2ccd5d8a4473cb7f7c6b07ada390657e6a7ea5dee188c3054"
LONGEVITY_VERIFY_FULL_TWO_SHA256 = "f18892a01759422854d3d09e4bfb04a6b4cc65b18d2e60aee55f5c8c711bdb59"
LONGEVITY_COMPACTIONS_TWO_SHA256 = "f17788eb7a1ca197dafefe8a627ea7441641e5a3f6786752f0638a2aa2e0673b"
LONGEVITY_READER_ONSET_ROWS_TWO_SHA256 = "48597e90f8146318afa60ad668403492b3464cd5c29933a329804341a9c6c0e7"

FAILURE_LEDGER = ROOT / "ops" / "failure_ledger.jsonl"

LIVE_OSV_DIR = ROOT / "benchmarks" / "live-osv-v1"
LIVE_OSV_SNAPSHOT = LIVE_OSV_DIR / "snapshot-2026-09-16.json"
# README.md's own "Snapshot" section quotes this sha256 for the manifest
# file; cross-checked against that quoted text in compute_c10_live_osv
# below, not only frozen from a first read.
LIVE_OSV_SNAPSHOT_SHA256 = "1978d6a692f9768dfa51b7260f11d00e13bce203b2765ff1b20de3f8109a3699"

# Lane B7 (scale campaign) -- benchmarks/scale-v1/. Stage 0 (iTiger
# calibration) and Stage 1 (30M) are both landed; 100M is a separate,
# concurrently-running lane whose records are not here yet (see
# add_pending_stubs). Every sha256 this lane cares about is checked
# against the table in the record's own README (B7_README for Stage 1,
# B7_ITIGER_CALIB_README for Stage 0) by _readme_sha256_table below, not
# hard-coded here -- there is no separate frozen-constant copy to drift.
B7_SCALE_DIR = ROOT / "benchmarks" / "scale-v1"
B7_README = B7_SCALE_DIR / "README.md"
B7_ITIGER_CALIB_README = B7_SCALE_DIR / "itiger-calib-2026-09.README.md"
B7_ITIGER_CALIB_1M = B7_SCALE_DIR / "itiger-calib-1m.json"
B7_ITIGER_CALIB_10M = B7_SCALE_DIR / "itiger-calib-10m.json"
B7_BUILD_30M = B7_SCALE_DIR / "build-30m.json"
B7_SCALE_CURVE_30M = B7_SCALE_DIR / "scale-curve-30m.json"
B7_SCALE_CURVE_30M_RAW = B7_SCALE_DIR / "scale-curve-30m-raw.json"
B7_CHECK_FULL_30M = B7_SCALE_DIR / "check-full-30m.json"
B7_RECOVERY_30M = B7_SCALE_DIR / "recovery-30m.json"
B7_RECOVERY_30M_CE5000 = B7_SCALE_DIR / "recovery-30m-ce5000.json"
B7_VERSION_HISTORY_30M = B7_SCALE_DIR / "version-history-30m.json"
B7_QUERYFLOOR_30M = B7_SCALE_DIR / "queryfloor-30m.json"

B7_BUILD_100M = B7_SCALE_DIR / "build-100m.json"
B7_BUILD_COMPACTION_WALLS_100M = B7_SCALE_DIR / "build-100m-compaction-walls.json"
B7_SCALE_CURVE_100M = B7_SCALE_DIR / "scale-curve-100m.json"
B7_SCALE_CURVE_100M_RAW = B7_SCALE_DIR / "scale-curve-100m-raw.json"
B7_CHECK_FULL_100M = B7_SCALE_DIR / "check-full-100m.json"
B7_RECOVERY_100M_CE5000 = B7_SCALE_DIR / "recovery-100m-ce5000.json"
B7_VERSION_HISTORY_100M = B7_SCALE_DIR / "version-history-100m.json"
B7_QUERYFLOOR_100M = B7_SCALE_DIR / "queryfloor-100m.json"

# The 13-operator scale-curve registry (query id -> CamelCase macro
# fragment), same ids as scale-curve-30m.json/queryfloor-30m.json's
# per_operator_p50_ms / queries[*].id.
B7_SCALE_CURVE_OPS = {
    "hist.single": "HistSingle",
    "hist.asof": "HistAsof",
    "snap.hop2": "SnapHop2",
    "diff.global": "DiffGlobal",
    "reach.window": "ReachWindow",
    "paths.k": "PathsK",
    "series.count": "SeriesCount",
    "burst.zscore": "BurstZscore",
    "nbr.evolution": "NbrEvolution",
    "coactive.narrow": "CoactiveNarrow",
    "resolve.substr": "ResolveSubstr",
    "agg.rel_bucket": "AggRelBucket",
    "motif.filtered": "MotifFiltered",
}

# At 100M, 7 of the 13 operators are refused by the cost guardrail
# (CostError). reach.window's refusal carries a numeric time_est_ms
# estimate (a dedicated admission probe, same as 30M's); the other six
# carry only a "CostError: ... exceeds ceilings" string, no estimate --
# per the coordinator's ruling, those six get a landed osdiB7Refused<Op>
# 100M = true macro instead of a PENDING osdiB7ScaleCurveP50<Op>100M (no
# invented p50, and no fabricated estimate for a refusal that has none).
B7_SCALE_CURVE_100M_REFUSED_NO_ESTIMATE = (
    "snap.hop2", "diff.global", "nbr.evolution", "coactive.narrow",
    "resolve.substr", "agg.rel_bucket",
)

# The 30M/100M stores' content digests (store_digest, streaming) -- shared
# by each scale's build/scale-curve/check-full/recovery/version-history
# records alike, since none of them modify the store; compute_b7_scale
# cross-checks every one of those records' own digest fields against
# these frozen values.
B7_30M_STORE_DIGEST = "239118cae2044e5928b68d82423e0e996b6bc96654cbfd34266747486aa7e6d8"
B7_100M_STORE_DIGEST = "48bcb256874e6ad7ed8712ebff668fb25f81c6eafd78d7274efa245efa8a2d97"

# Lane W-lane -- P-OV1, the xzgpu-calibrated overload sweep (EXP-B4):
# benchmarks/overload-v1/{overload-2026-09-15,overload-2026-09-15-rep2}.json
# (2 reps, --clients 1 2 4 8 16 32 64 --max-concurrent 8) and
# hwm-checkpoints-v3.json (the "Pinned" VmHWM localization: the service
# surface's own high-water mark stays ~227 MB through the 64-client and
# recovery steps; the ~2 GB lifetime peak belongs to the harness's own
# end-of-sweep store.digest() call, not to ToolRouter/ConcurrencyGate under
# load -- see README.md's "Pinned" section). Every sha256 below is checked
# against benchmarks/overload-v1/SHA256SUMS (note: that file's own entry for
# rep1 is misnamed "overload-2026-09-15-rep1.json" even though the
# committed file is "overload-2026-09-15.json" -- a naming quirk in the
# sums file itself, not a hash mismatch; checked by hash membership below,
# not by filename).
OVERLOAD_DIR = ROOT / "benchmarks" / "overload-v1"
OVERLOAD_REP1 = OVERLOAD_DIR / "overload-2026-09-15.json"
OVERLOAD_REP2 = OVERLOAD_DIR / "overload-2026-09-15-rep2.json"
OVERLOAD_REP1_RECORDS = OVERLOAD_DIR / "overload-2026-09-15.records.json"
OVERLOAD_HWM_CHECKPOINTS_V3 = OVERLOAD_DIR / "hwm-checkpoints-v3.json"
OVERLOAD_SHA256SUMS = OVERLOAD_DIR / "SHA256SUMS"

# Lane W-lane -- the 2026-09-15 full-mode verify of the original soak store
# (benchmarks/longevity-v1/verify-full-2026-09-15.txt, two `tgms check`
# entries concatenated: the pre-replay check of the original store, then
# the post-hoc full verify of REPLAY-2's replayed store, appended after it
# unedited -- see README.md's "Post-hoc replay check -- attempt 3" section)
# and REPLAY-2 itself (replay-check-2-2026-09.json, the post-D-087-fix
# replay that actually completed, unlike the OOM-killed attempts 1/2 this
# script already reads as LONGEVITY_REPLAY_CHECK above). Neither file
# appears in README.md's original "Files here" table (that soak predates
# both), so their whole-file sha256 is frozen here from this lane's own
# first read of the committed copies, the same discipline W2j's pair uses
# above.
LONGEVITY_VERIFY_FULL = LONGEVITY_DIR / "verify-full-2026-09-15.txt"
LONGEVITY_REPLAY_CHECK_2 = LONGEVITY_DIR / "replay-check-2-2026-09.json"
LONGEVITY_VERIFY_FULL_SHA256 = "4a84460725df097cb6df81eec8d55a7c8b28b2d906c2cb709712f3a0c1be6a68"
LONGEVITY_REPLAY_CHECK_2_SHA256 = "45c6b2561b3a63f4164874a88c97f42c5c4bf1f11888d075340659aca41b2c66"

# Lane W2u -- P-STORM-HUNT, a 6h observation-only run (commit 57952fa,
# single writer life, --restart-every unset/never) whose only job was to
# see whether P-SOAK2's reader-side OSError/StateError failure storm would
# recur under the D-088 bounded reader-error-message capture (merged
# c3a5592) -- see README.md's "P-STORM-HUNT (6 h observation run,
# 2026-09-17/18)" section. Every whole-file sha256 below is checked
# against that section's own "Files added here" table, parsed out of
# README.md itself via _readme_sha256_table -- scoped to just this
# section (via _readme_section) because the table shares field labels
# like **launched**: with P-SOAK2's section above, and a whole-README
# regex search for those labels would risk matching the wrong lane's row.
# Same discipline as B7's README-table checks above -- no separate
# frozen-constant copy to drift.
LONGEVITY_README = LONGEVITY_DIR / "README.md"
LONGEVITY_MANIFEST_HUNT = LONGEVITY_DIR / "stormhunt-2026-09-17.json"
LONGEVITY_RSS_SLOPES_HUNT = LONGEVITY_DIR / "rss_slopes-stormhunt.json"
LONGEVITY_WRITER_ERRORS_BY_CLASS_HUNT = LONGEVITY_DIR / "writer_error_counts_by_class-stormhunt.json"
LONGEVITY_HOST_LOAD_HUNT = LONGEVITY_DIR / "host_load-stormhunt.log"
LONGEVITY_READER_OP_ERROR_HUNT = LONGEVITY_DIR / "reader_op_error-stormhunt.jsonl"
LONGEVITY_READER_OP_ERROR_README_HUNT = LONGEVITY_DIR / "reader_op_error-stormhunt.README.txt"
LONGEVITY_GATE_E_REPORT_HUNT = LONGEVITY_DIR / "gate_e_report-stormhunt.md"
LONGEVITY_BUILD_INFO_HUNT = LONGEVITY_DIR / "build_info-stormhunt.log"

# Lane W2t -- benchmarks/ldbc-ref-v1/, the LDBC reference-correctness run
# (Claim C9's independent-validation axis). compare-2026-09-18.json is the
# revision of record (README.md's "Revision of record" section);
# compare-2026-09-17.json is the superseded interim, kept in place rather
# than overwritten (7 templates had an invalid reference side until the
# temporal-parameter fix). Every whole-file sha256 below is checked against
# the run's own SHA256SUMS.txt, not a separate frozen-constant table --
# there is no drift to guard against beyond that one file.
LDBC_REF_V1_DIR = ROOT / "benchmarks" / "ldbc-ref-v1"
LDBC_REF_V1_README = LDBC_REF_V1_DIR / "README.md"
LDBC_REF_V1_CAMPAIGN_YAML = LDBC_REF_V1_DIR / "campaign.yaml"
LDBC_REF_V1_SHA256SUMS = LDBC_REF_V1_DIR / "SHA256SUMS.txt"
LDBC_REF_V1_COMPARE = LDBC_REF_V1_DIR / "compare-2026-09-18.json"
LDBC_REF_V1_COMPARE_INTERIM = LDBC_REF_V1_DIR / "compare-2026-09-17.json"
LDBC_REF_V1_TGMS_CAMPAIGN = LDBC_REF_V1_DIR / "tgms-campaign-ldbc-ref-v1.json"


# --------------------------------------------------------------------------
# verification helpers (copied from scripts/tgir_paper_macros.py)
# --------------------------------------------------------------------------

FAILURES: list[str] = []
CHECKS = 0


def require(cond: bool, what: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILURES.append(what)


def eq(got, want, what: str):
    require(got == want, f"{what}: derived {got!r} != source {want!r}")
    return got


def close(got: float, want: float, tol: float, what: str):
    require(abs(got - want) <= tol,
            f"{what}: derived {got!r} not within {tol} of {want!r}")
    return got


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _readme_sha256_table(text: str) -> dict[str, str]:
    """Parse every ``| `file` ... | `<sha256>` |`` markdown table row in
    ``text`` into ``{file: sha256}``. Used by compute_b7_scale to check a
    record's sha256 against its own README's table rather than a second,
    driftable frozen constant."""
    return dict(re.findall(r"\|\s*`([^`]+)`[^|]*\|\s*`([0-9a-f]{64})`\s*\|", text))


def _readme_section(text: str, heading: str) -> str:
    """Slice out one ``## <heading>...`` section of a README.md (from that
    ``##`` heading to the next ``## `` heading, or EOF). Used so a per-lane
    regex/table parse against a README with several lanes' sections never
    accidentally matches another lane's row sharing the same field label
    (e.g. benchmarks/longevity-v1/README.md's P-SOAK2 and P-STORM-HUNT
    sections both have a ``**launched**:`` line)."""
    match = re.search(rf"^## {re.escape(heading)}\b.*?(?=^## |\Z)", text,
                       re.DOTALL | re.MULTILINE)
    require(match is not None, f"README.md: no '## {heading}' section found")
    return match.group(0) if match else ""


def _sha256sums_table(text: str) -> set[str]:
    """Parse a plain ``sha256sum``-style manifest (``<hex>␠␠<filename>`` per
    line) into the set of hex digests it names. Used by compute_overload to
    check a record's sha256 against benchmarks/overload-v1/SHA256SUMS by
    hash membership rather than by filename -- that file's own entry for
    rep1 is misnamed (see OVERLOAD_REP1's module comment above), so a
    filename-keyed lookup would be wrong for the right reason."""
    return set(re.findall(r"^([0-9a-f]{64})\s+\S+\s*$", text, re.MULTILINE))


def _sha256sums_by_name(text: str) -> dict[str, str]:
    """Parse a plain ``sha256sum``-style manifest into ``{basename: sha256}``,
    same convention ``_b7_check_sha256`` below uses against a README's own
    table: keyed by filename rather than the full path the manifest names,
    so a tampered copy under a test's ``tmp_path`` (same basename, moved
    directory) is still looked up correctly. Safe here because none of the
    specific files this lane looks up share a basename with each other
    anywhere in ``benchmarks/ldbc-ref-v1/SHA256SUMS.txt`` (unlike the
    ``ref-<ID>.json`` rows/warmup/tN sidecars, which repeat basenames across
    directories and are never looked up by this dict)."""
    return dict((Path(path).name, digest) for digest, path in
                re.findall(r"^([0-9a-f]{64})\s+(\S+)\s*$", text, re.MULTILINE))


def relpath(p: Path) -> str:
    """`p` relative to ROOT for a provenance string, or `p` itself when it
    is not under ROOT (e.g. a tampered copy under a test's tmp_path --
    tests monkeypatch the module-level path constants to such a copy and
    still need a working provenance string, not a ValueError)."""
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def us_to_ms_str(us: int, ndigits: int) -> str:
    """Format an integer-microsecond figure as milliseconds with exact
    decimal rounding. ``f"{us / 1000:.{ndigits}f}"`` looks equivalent but is
    not: a binary float can't represent an exact half like 5935/1000 ==
    5.935, so it may land a hair below the tie and round the wrong way
    (5.93 instead of 5.94). Decimal division on the exact integer input
    rounds the true half correctly."""
    ms = Decimal(int(us)) / Decimal(1000)
    quant = Decimal(1).scaleb(-ndigits)
    return str(ms.quantize(quant, rounding=ROUND_HALF_UP))


def tex_num(n: int) -> str:
    """LaTeX thousands separator that survives both text and math mode."""
    s = str(n)
    if len(s) <= 4:
        return s
    out = []
    for i, ch in enumerate(reversed(s)):
        if i and i % 3 == 0:
            out.append("{,}")
        out.append(ch)
    return "".join(reversed(out))


def tex_float(val: float) -> str:
    """Like tex_num, but for a float: LaTeX-safe thousands separator on
    the integer part, the value's own decimal digits kept exactly as
    given (no forced rounding/padding beyond what the caller already
    did)."""
    s = repr(val)
    whole, sep, frac = s.partition(".")
    grouped = tex_num(int(whole))
    return f"{grouped}.{frac}" if sep else grouped


class Macros:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []  # (name, value, provenance)
        self.seen: set[str] = set()

    def add(self, name: str, value, provenance: str) -> None:
        assert name not in self.seen, f"duplicate macro {name}"
        self.seen.add(name)
        self.items.append((name, str(value), provenance))

    def add_pending(self, name: str, lane: str, reason: str) -> None:
        """A macro for a claim whose record has not landed yet.

        Renders to a real LaTeX error via ``\\errmessage`` -- expanding
        the macro in the paper halts compilation with the lane name and
        the reason, rather than silently rendering a placeholder number.
        """
        assert name not in self.seen, f"duplicate macro {name}"
        self.seen.add(name)
        msg = f"osdi_paper_macros: {name} is PENDING ({lane}): {reason}"
        value = r"\errmessage{" + msg.replace("{", "(").replace("}", ")") + "}"
        self.items.append((name, value, f"PENDING -- {lane}: {reason}"))

    def render(self) -> str:
        lines = [
            "% osdi-macros.tex --- GENERATED by scripts/osdi_paper_macros.py.",
            "% Do not hand-edit; re-run the generator.",
            "%",
            "% Every landed-record number in the manuscript resolves through one",
            "% of these; each was recomputed from row-level data and checked",
            "% against a frozen expectation in tests/test_osdi_paper_macros.py.",
            f"% This run performed {CHECKS} such assertions and refused to write",
            "% on any failure. A macro whose provenance begins 'PENDING' raises",
            "% a LaTeX error if the manuscript expands it -- its record has not",
            "% landed and no placeholder number is emitted for it.",
            "%",
            "% Every name below is defined via \\csname...\\endcsname, not a bare",
            "% \\newcommand{\\name}: most names carry digits (e.g.",
            "% osdiB7BuildWall30M) and a LaTeX control-sequence name may not,",
            "% outside \\csname. \\csname osdiFoo\\endcsname is the same control",
            "% sequence as \\osdiFoo, so digit-free names still work under their",
            "% bare \\osdiFoo spelling. The \\osdi{Name} accessor just below is",
            "% \\csname osdi<Name>\\endcsname for prose that prefers it.",
            "",
            r"\providecommand{\osdi}[1]{\csname osdi#1\endcsname}",
            "",
        ]
        width = max(len(n) for n, _, _ in self.items)
        for name, value, prov in self.items:
            pad = " " * (width - len(name))
            lines.append(
                f"\\expandafter\\newcommand\\csname {name}\\endcsname{{{value}}}{pad}  % {prov}"
            )
        lines.append("")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# C1 --- crash campaign (10,000 seeded trials, 0 problems)
# --------------------------------------------------------------------------

def compute_c1(m: Macros) -> None:
    d = json.loads(CRASH_V1.read_text(encoding="utf-8"))
    results = d["results"]
    per_boundary = d["per_boundary"]

    eq(len(results), 10000, "C1: crash campaign result row count")
    eq(d["total_trials"], len(results), "C1: total_trials matches len(results)")

    recomputed_problems = sum(1 for r in results if r["problems"])
    eq(recomputed_problems, 0, "C1: recomputed problem count over every trial row")
    eq(recomputed_problems, d["total_problems"],
       "C1: recomputed problems matches record's own total_problems")

    for r in results:
        require(r["q1_acked_survive"] and r["q2_deterministic"]
                and r["q3_single_generation"] and r["q4_orphans_reclaimed"],
                f"C1: trial {r['boundary']}/{r['trial']} failed a Q1-Q4 check")

    boundaries = sorted(per_boundary)
    eq(len(boundaries), 10, "C1: boundary count")
    per_boundary_trials = sum(v["trials"] for v in per_boundary.values())
    eq(per_boundary_trials, 10000, "C1: per_boundary trial counts sum to 10,000")
    for name, v in per_boundary.items():
        actual = [r for r in results if r["boundary"] == name]
        eq(len(actual), v["trials"], f"C1: {name} trial count matches per_boundary")
        eq(sum(1 for r in actual if r["problems"]), v["problems_total"],
           f"C1: {name} recomputed problems matches per_boundary")

    recomputed_wall = round(sum(r["wall_s"] for r in results), 2)
    eq(recomputed_wall, d["wall_s"], "C1: summed per-trial wall_s matches record wall_s")

    eq(d["total_trials"], 10000, "C1 frozen: total_trials")
    eq(recomputed_problems, 0, "C1 frozen: total_problems")
    eq(len(boundaries), 10, "C1 frozen: boundary count")
    eq(recomputed_wall, 4912.85, "C1 frozen: summed wall_s")

    m.add("osdiCrashTrials", tex_num(d["total_trials"]),
          f"{relpath(CRASH_V1)}: len(results), == total_trials")
    m.add("osdiCrashProblems", recomputed_problems,
          f"{relpath(CRASH_V1)}: trials with a non-empty problems list")
    m.add("osdiCrashBoundaries", len(boundaries),
          f"{relpath(CRASH_V1)}: distinct per_boundary keys")
    m.add("osdiCrashWall", f"{recomputed_wall:,.2f}".replace(",", "{,}"),
          f"{relpath(CRASH_V1)}: sum of results[*].wall_s, seconds")


# --------------------------------------------------------------------------
# C3 --- incremental manifest bytes (B1 A/B)
# --------------------------------------------------------------------------

def compute_c3(m: Macros) -> None:
    raw = json.loads(B1_RAW.read_text(encoding="utf-8"))
    b1a = raw["b1a"]
    ctl, trt = b1a["control"], b1a["treatment"]

    eq(ctl["stopped_at_ops"], 2_500_000, "C3: control stopped at 2.5M ops")
    eq(trt["stopped_at_ops"], 2_500_000, "C3: treatment stopped at 2.5M ops")
    eq(ctl["ops_series"][-1][0], 2_500_000, "C3: control ops_series last point is 2.5M ops")
    eq(trt["ops_series"][-1][0], 2_500_000, "C3: treatment ops_series last point is 2.5M ops")

    bytes_ctl = ctl["manifest_bytes_at_stop"]
    bytes_trt = trt["manifest_bytes_at_stop"]
    eq(bytes_ctl, 25_970_987_762, "C3 frozen: control manifest bytes at 2.5M ops")
    eq(bytes_trt, 62_046_913, "C3 frozen: treatment manifest bytes at 2.5M ops")
    ratio_bytes = bytes_ctl / bytes_trt
    close(ratio_bytes, 418.57, 0.01, "C3 frozen: manifest-byte reduction ratio (README's "
          "own prose rounds this to \"~419x\")")

    b1b = raw["b1b"]["summary"]
    total_ctl = b1b["control"]["total_us"]
    total_trt = b1b["treatment"]["total_us"]
    ratio_ctl = total_ctl["last_decile_us"] / total_ctl["first_decile_us"]
    ratio_trt = total_trt["last_decile_us"] / total_trt["first_decile_us"]
    close(ratio_ctl, total_ctl["ratio_last_over_first"], 0.005,
          "C3: control engine-commit decile ratio matches recorded field")
    close(ratio_trt, total_trt["ratio_last_over_first"], 0.005,
          "C3: treatment engine-commit decile ratio matches recorded field")
    close(ratio_trt, 1.798, 0.01, "C3 frozen: treatment engine-commit total_us decile ratio")
    require(ratio_trt > 1.2, "C3: F2's <=1.2x falsifier bar is refuted by the treatment ratio")

    b1c = raw["b1c"]["authoritative"]
    ctl_open = b1c["control_open_ms"]
    trt_open = b1c["treatment_open_ms"]
    eq(len(ctl_open), 3, "C3: control cold-open reps")
    eq(len(trt_open), 3, "C3: treatment cold-open reps")
    ctl_open_med = statistics.median(ctl_open)
    trt_open_med = statistics.median(trt_open)
    close(ctl_open_med, 1070, 2, "C3 frozen: control cold-open median, ms")
    close(trt_open_med, 8589, 2, "C3 frozen: treatment cold-open median, ms")
    cold_open_ratio = trt_open_med / ctl_open_med
    close(cold_open_ratio, 8.03, 0.02, "C3 frozen: cold-open slowdown ratio")
    require(trt_open_med > 100, "C3: F3's <=100ms falsifier bar is refuted by treatment open")

    m.add("osdiManifestBytesCtl", f"{bytes_ctl / 1e9:.2f}",
          f"{relpath(B1_RAW)}: b1a.control.manifest_bytes_at_stop, decimal GB")
    m.add("osdiManifestBytesTrt", f"{bytes_trt / 1e6:.1f}",
          f"{relpath(B1_RAW)}: b1a.treatment.manifest_bytes_at_stop, decimal MB")
    m.add("osdiManifestCommitRatio", f"{ratio_trt:.3f}",
          f"{relpath(B1_RAW)}: b1b.summary.treatment.total_us last/first decile "
          "(the F2 falsifier: refuted, bar <=1.2x)")
    m.add("osdiManifestColdOpen", f"{cold_open_ratio:.2f}",
          f"{relpath(B1_RAW)}: b1c.authoritative median(treatment_open_ms) / "
          "median(control_open_ms) at G~10k (the F3 falsifier: refuted, bar <=100ms)")


# --------------------------------------------------------------------------
# B1-v2 --- manifest format 3 (reader torn-tail fix) A/B, Lane B / W2f.
#
# Same Addendum-3 (manifest forecast) protocol as v1's B1 A/B above (C3):
# SF1 manifest bytes at matched 2.5M ops, SS20 batch=1 commitcost phase
# deciles/p50 (300 commits, 100k-row seed store), chain-open at G~10k
# K=512, plus the off-default K=128/K=1024 sweep (1 rep each). These
# osdiB1v2* macros stand beside the osdiManifest* ones above as v2-era
# measurements -- v1's numbers are never overwritten. No verdict macro:
# the brief's thresholds and pass/fail scoring live in the internal
# freeze doc, not here.
# --------------------------------------------------------------------------

def compute_b1_v2(m: Macros) -> None:
    manifest = json.loads(B1_V2_MANIFEST.read_text(encoding="utf-8"))
    raw = json.loads(B1_V2_RAW.read_text(encoding="utf-8"))

    # digest-check: the manifest's own result_digest is sha256 of its
    # `measurements` block (canonical, sort_keys JSON) -- this is the
    # manifest-format-3 record's own tamper-evidence, distinct from (and
    # in addition to) recomputing every figure from the raw record below.
    recomputed_digest = hashlib.sha256(
        json.dumps(manifest["measurements"], sort_keys=True).encode("utf-8")
    ).hexdigest()
    eq(recomputed_digest, manifest["result_digest"],
       f"{relpath(B1_V2_MANIFEST)}: sha256(measurements, sort_keys=True) matches "
       "the manifest's own result_digest")

    ctl_commit = manifest["config"]["control_commit"]
    trt_commit = manifest["config"]["treatment_commit"]
    eq(ctl_commit[:7], "886805f", "B1-v2 frozen: control_commit short sha")
    eq(trt_commit[:7], "7a5ff98", "B1-v2 frozen: treatment_commit short sha")

    # --- B1(a): manifest/segment bytes + build ops/s at matched 2.5M ops ---
    b1a = raw["b1a"]
    ctl, trt = b1a["control"], b1a["treatment"]
    eq(ctl["stopped_at_ops"], 2_500_000, "B1-v2: control stopped at 2.5M ops")
    eq(trt["stopped_at_ops"], 2_500_000, "B1-v2: treatment stopped at 2.5M ops")
    eq(ctl["ops_series"][-1][0], 2_500_000,
       "B1-v2: control ops_series last point is 2.5M ops")
    eq(trt["ops_series"][-1][0], 2_500_000,
       "B1-v2: treatment ops_series last point is 2.5M ops")

    bytes_ctl = ctl["manifest_bytes_at_stop"]
    bytes_trt = trt["manifest_bytes_at_stop"]
    seg_ctl = ctl["segment_bytes_at_stop"]
    seg_trt = trt["segment_bytes_at_stop"]
    eq(bytes_ctl, 73_790_679, "B1-v2 frozen: control manifest bytes at 2.5M ops")
    eq(bytes_trt, 74_403_447, "B1-v2 frozen: treatment manifest bytes at 2.5M ops")
    eq(seg_ctl, 158_966_639, "B1-v2 frozen: control segment bytes at 2.5M ops")
    eq(seg_trt, 163_447_175, "B1-v2 frozen: treatment segment bytes at 2.5M ops")

    meas_bytes = manifest["measurements"]["bytes_at_2_5m_ops"]
    eq(bytes_ctl, meas_bytes["control_manifest_bytes"],
       "B1-v2: recomputed control manifest bytes matches manifest's own field")
    eq(bytes_trt, meas_bytes["treatment_manifest_bytes"],
       "B1-v2: recomputed treatment manifest bytes matches manifest's own field")
    eq(seg_ctl, meas_bytes["control_segment_bytes"],
       "B1-v2: recomputed control segment bytes matches manifest's own field")
    eq(seg_trt, meas_bytes["treatment_segment_bytes"],
       "B1-v2: recomputed treatment segment bytes matches manifest's own field")

    bytes_paired = bytes_trt / bytes_ctl
    close(bytes_paired, 1.008, 0.001,
          "B1-v2 frozen: manifest-bytes paired ratio treatment/control")

    ops_ctl_2_5m = ctl["ops_series"][-1][1]
    ops_trt_2_5m = trt["ops_series"][-1][1]
    eq(ops_ctl_2_5m, 2543, "B1-v2 frozen: control build ops/s at the 2.5M-op checkpoint")
    eq(ops_trt_2_5m, 5335, "B1-v2 frozen: treatment build ops/s at the 2.5M-op checkpoint")
    ops_ratio = ops_trt_2_5m / ops_ctl_2_5m
    close(ops_ratio, 2.098, 0.001,
          "B1-v2 frozen: build ops/s ratio (treatment/control) at 2.5M ops -- "
          "observation only, no claim attached (per the record's own note)")

    # --- B1(b): commitcost phase deciles / p50, K=512 default + K-sweep ---
    b1b = raw["b1b"]["raw"]

    def decile_ratio(rep: dict, field: str) -> float:
        return rep["last_decile_us"][field] / rep["first_decile_us"][field]

    def median_decile(reps: list, field: str) -> float:
        return statistics.median(decile_ratio(r, field) for r in reps)

    eq(len(b1b["control"]), 3, "B1-v2: control commitcost has 3 reps")
    eq(len(b1b["treatment"]), 3, "B1-v2: treatment commitcost has 3 reps")
    eq(len(b1b["treatment_k128"]), 1, "B1-v2: K=128 sweep is 1 rep")
    eq(len(b1b["treatment_k1024"]), 1, "B1-v2: K=1024 sweep is 1 rep")

    manifest_decile_ctl = median_decile(b1b["control"], "manifest_us")
    manifest_decile_trt = median_decile(b1b["treatment"], "manifest_us")
    total_decile_ctl = median_decile(b1b["control"], "total_us")
    total_decile_trt = median_decile(b1b["treatment"], "total_us")

    close(manifest_decile_ctl, 1.115, 0.001,
          "B1-v2 frozen: control manifest_us decile ratio (median of 3 reps)")
    close(manifest_decile_trt, 1.079, 0.001,
          "B1-v2 frozen: treatment manifest_us decile ratio (median of 3 reps)")
    close(total_decile_ctl, 1.733, 0.001,
          "B1-v2 frozen: control engine-commit total_us decile ratio (median of 3 reps)")
    close(total_decile_trt, 1.674, 0.001,
          "B1-v2 frozen: treatment engine-commit total_us decile ratio (median of 3 reps)")

    meas_decile = manifest["measurements"]["commitcost_decile"]
    md = meas_decile["manifest_us_first_last_decile_ratio"]
    td = meas_decile["engine_commit_total_us_first_last_decile_ratio"]
    close(manifest_decile_ctl, md["control_median"], 0.001,
          "B1-v2: recomputed control manifest_us decile matches manifest's own median")
    close(manifest_decile_trt, md["treatment_median"], 0.001,
          "B1-v2: recomputed treatment manifest_us decile matches manifest's own median")
    close(total_decile_ctl, td["control_median"], 0.001,
          "B1-v2: recomputed control total_us decile matches manifest's own median")
    close(total_decile_trt, td["treatment_median"], 0.001,
          "B1-v2: recomputed treatment total_us decile matches manifest's own median")

    manifest_decile_k128 = decile_ratio(b1b["treatment_k128"][0], "manifest_us")
    manifest_decile_k1024 = decile_ratio(b1b["treatment_k1024"][0], "manifest_us")
    close(manifest_decile_k128, 1.068, 0.001,
          "B1-v2 frozen: treatment K=128 manifest_us decile ratio (1 rep)")
    close(manifest_decile_k1024, 1.150, 0.001,
          "B1-v2 frozen: treatment K=1024 manifest_us decile ratio (1 rep)")

    p50_ctl = statistics.median(r["phase_p50_us"]["total_us"] for r in b1b["control"])
    p50_trt = statistics.median(r["phase_p50_us"]["total_us"] for r in b1b["treatment"])
    eq(p50_ctl, 5430, "B1-v2 frozen: control engine-commit p50 total_us (median of 3 reps)")
    eq(p50_trt, 5478, "B1-v2 frozen: treatment engine-commit p50 total_us (median of 3 reps)")

    meas_p50 = manifest["measurements"]["commitcost_p50"]
    eq(p50_ctl, meas_p50["control_median_us"],
       "B1-v2: recomputed control p50 matches manifest's own field")
    eq(p50_trt, meas_p50["treatment_median_us"],
       "B1-v2: recomputed treatment p50 matches manifest's own field")

    p50_paired = p50_trt / p50_ctl
    close(p50_paired, 1.009, 0.001, "B1-v2 frozen: p50 paired ratio treatment/control")
    close(p50_paired, meas_p50["paired_ratio_treatment_over_control"], 0.001,
          "B1-v2: recomputed p50 paired ratio matches manifest's own field")

    # --- B1(c): cold/warm chain-open at G~10k ---
    b1c = raw["b1c"]["authoritative"]
    ctl_open = b1c["control_open_ms"]
    trt_open = b1c["treatment_open_ms"]
    eq(len(ctl_open), 3, "B1-v2: control cold-open reps")
    eq(len(trt_open), 3, "B1-v2: treatment cold-open reps")
    ctl_open_med = statistics.median(ctl_open)
    trt_open_med = statistics.median(trt_open)
    close(ctl_open_med, 2247.9, 0.05, "B1-v2 frozen: control cold-open median, ms")
    close(trt_open_med, 1705.3, 0.05, "B1-v2 frozen: treatment cold-open median, ms")

    meas_open = manifest["measurements"]["chain_open_cold_warm"]
    close(ctl_open_med, meas_open["control_median_ms"], 0.05,
          "B1-v2: recomputed control open median matches manifest's own field")
    close(trt_open_med, meas_open["treatment_median_ms"], 0.05,
          "B1-v2: recomputed treatment open median matches manifest's own field")

    open_paired = trt_open_med / ctl_open_med
    close(open_paired, 0.76, 0.005,
          "B1-v2 frozen: cold-open paired ratio treatment/control")

    ctl_generation = b1c["control_generation"]
    trt_generation = b1c["treatment_generation"]
    eq(ctl_generation, 10759, "B1-v2 frozen: control chain-open generation (G)")
    eq(trt_generation, 11029, "B1-v2 frozen: treatment chain-open generation (G)")

    # The checkpoint-load vs. delta-replay component split was not measured:
    # NativeAdapter() is a single opaque constructor with no internal phase
    # timer exposed to Python (unlike the commit path's phase_p50_us).
    # Assert the record says so honestly rather than silently treating a
    # missing split as zero.
    require(b1c["component_breakdown"] is None,
            "B1-v2: component_breakdown is genuinely absent (flagged), not fabricated")

    # --- emit macros ---
    m.add("osdiB1v2ControlCommit", ctl_commit[:7],
          f"{relpath(B1_V2_MANIFEST)}: config.control_commit, short sha")
    m.add("osdiB1v2TreatmentCommit", trt_commit[:7],
          f"{relpath(B1_V2_MANIFEST)}: config.treatment_commit, short sha")

    m.add("osdiB1v2BytesControlMB", f"{bytes_ctl / 1e6:.1f}",
          f"{relpath(B1_V2_RAW)}: b1a.control.manifest_bytes_at_stop, decimal MB")
    m.add("osdiB1v2BytesTreatmentMB", f"{bytes_trt / 1e6:.1f}",
          f"{relpath(B1_V2_RAW)}: b1a.treatment.manifest_bytes_at_stop, decimal MB")
    m.add("osdiB1v2BytesPaired", f"{bytes_paired:.3f}",
          f"{relpath(B1_V2_RAW)}: b1a.treatment.manifest_bytes_at_stop / "
          "b1a.control.manifest_bytes_at_stop")
    m.add("osdiB1v2SegmentBytesControlMB", f"{seg_ctl / 1e6:.1f}",
          f"{relpath(B1_V2_RAW)}: b1a.control.segment_bytes_at_stop, decimal MB")
    m.add("osdiB1v2SegmentBytesTreatmentMB", f"{seg_trt / 1e6:.1f}",
          f"{relpath(B1_V2_RAW)}: b1a.treatment.segment_bytes_at_stop, decimal MB")

    m.add("osdiB1v2ManifestDecileControl", f"{manifest_decile_ctl:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.control[*].last_decile_us.manifest_us / "
          "[*].first_decile_us.manifest_us, median over 3 reps")
    m.add("osdiB1v2ManifestDecileTreatment", f"{manifest_decile_trt:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.treatment[*].last_decile_us.manifest_us / "
          "[*].first_decile_us.manifest_us, median over 3 reps")
    m.add("osdiB1v2ManifestDecileK128", f"{manifest_decile_k128:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.treatment_k128[0].last_decile_us.manifest_us / "
          "first_decile_us.manifest_us (1 rep)")
    m.add("osdiB1v2ManifestDecileK1024", f"{manifest_decile_k1024:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.treatment_k1024[0].last_decile_us.manifest_us / "
          "first_decile_us.manifest_us (1 rep)")

    m.add("osdiB1v2TotalDecileControl", f"{total_decile_ctl:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.control[*].last_decile_us.total_us / "
          "[*].first_decile_us.total_us, median over 3 reps")
    m.add("osdiB1v2TotalDecileTreatment", f"{total_decile_trt:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.treatment[*].last_decile_us.total_us / "
          "[*].first_decile_us.total_us, median over 3 reps")

    m.add("osdiB1v2P50ControlMs", f"{p50_ctl / 1000:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.control[*].phase_p50_us.total_us, "
          "median over 3 reps, /1000, ms")
    m.add("osdiB1v2P50TreatmentMs", f"{p50_trt / 1000:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.treatment[*].phase_p50_us.total_us, "
          "median over 3 reps, /1000, ms")
    m.add("osdiB1v2P50Paired", f"{p50_paired:.3f}",
          f"{relpath(B1_V2_RAW)}: median(treatment[*].phase_p50_us.total_us) / "
          "median(control[*].phase_p50_us.total_us)")

    m.add("osdiB1v2OpenControlMs", f"{ctl_open_med:.1f}",
          f"{relpath(B1_V2_RAW)}: median(b1c.authoritative.control_open_ms), 3 reps, ms")
    m.add("osdiB1v2OpenTreatmentMs", f"{trt_open_med:.1f}",
          f"{relpath(B1_V2_RAW)}: median(b1c.authoritative.treatment_open_ms), 3 reps, ms")
    m.add("osdiB1v2OpenPaired", f"{open_paired:.2f}",
          f"{relpath(B1_V2_RAW)}: median(b1c.authoritative.treatment_open_ms) / "
          "median(b1c.authoritative.control_open_ms)")
    m.add("osdiB1v2OpenControlGeneration", tex_num(ctl_generation),
          f"{relpath(B1_V2_RAW)}: b1c.authoritative.control_generation")
    m.add("osdiB1v2OpenTreatmentGeneration", tex_num(trt_generation),
          f"{relpath(B1_V2_RAW)}: b1c.authoritative.treatment_generation")

    m.add("osdiB1v2BuildOpsPerSecRatioAt2p5M", f"{ops_ratio:.2f}",
          f"{relpath(B1_V2_RAW)}: b1a.treatment.ops_series[-1][1] (5335) / "
          "b1a.control.ops_series[-1][1] (2543), build ops/s at the 2.5M-op "
          "checkpoint -- observation only, no claim attached")

    m.add("osdiB1v2OpenComponentStatus", "not computed",
          f"{relpath(B1_V2_RAW)}: b1c.authoritative.component_breakdown is null -- "
          "NativeAdapter() exposes no internal phase timer to split checkpoint-load "
          "from delta-replay (text macro, not a number -- see "
          "b1c.authoritative.component_breakdown_note)")


# --------------------------------------------------------------------------
# B1-v2e --- remeasure of the two B1-v2 cells that A/B could not score,
# under the fully-timed B1-v2d harness (`open_phase_us`, `build_info`,
# per-commit phase decile/residual fields).
#
# `osdiB1v2*ManifestDecile*`/`osdiB1v2TotalDecile*`/`osdiB1v2P50*` (the v2
# commit-cost treatment macros above) are LEFT UNCHANGED here -- values and
# all -- but the B1-v2 A/B's 2026-09-15 README correction found their
# provenance strings understate what they actually measured: the v2 A/B's
# commit-cost "treatment" reps wrote manifest records sized like the
# format-2 control (1,565-1,568 B) rather than format 3 (1,592-1,595 B),
# because `Manifest::digest()` (`crates/tgms-engine-core/src/manifest.rs`
# :615-636) picks the digest rule from the manifest's own `format` field --
# a format-2 chain pays the O(segments) `legacy_body_sha` fallback no
# matter which binary is timing it. Those macros' provenance is relabeled
# below (`_VOID_AS_FORMAT3` appended) without touching a single digit; this
# is prose correction, the same discipline as the README's own "Correction
# (2026-09-15)" paragraph, applied to the generated macros file.
#
# The `osdiB1v2e*` macros below are new: recomputed from
# `b1-manifest-v2e-remeasure-2026-09{,-raw}.json`, the re-measurement that
# supersedes v2's commit-cost and chain-open cells (treatment `e5d4171`,
# built and verified on a genuine format-3 chain -- `build_info()` and the
# 1,592-1,595 B manifest records both confirm it). No verdict macro here
# either, same convention as v1/v2.
# --------------------------------------------------------------------------

_VOID_AS_FORMAT3 = (
    " -- VOID AS A FORMAT-3 MEASUREMENT (2026-09-15 correction): this "
    "treatment rep's manifest records are 1,565-1,568 B, byte-identical in "
    "size to the format-2 control, not the 1,592-1,595 B a format-3 chain "
    "writes -- Manifest::digest() (manifest.rs:615-636) dispatches on the "
    "manifest's own `format` field, so this arm measured a format-2 chain "
    "(O(segments) legacy_body_sha) under the format-3 binary, not the "
    "format-3 path. Superseded for commit cost and chain-open by "
    "osdiB1v2e* below; values here are unchanged (frozen as measured)."
)


def _void_b1_v2_treatment_provenance(m: Macros) -> None:
    """Append `_VOID_AS_FORMAT3` to the v2 commit-cost treatment macros'
    provenance strings, in place, without touching their values.

    `Macros` stores `(name, value, provenance)` tuples in `m.items` and
    guards against duplicate names via `m.seen` -- there is no public
    "amend a provenance string" method, so this rewrites the tuple in
    `m.items` directly by name. Called right after `compute_b1_v2` so every
    other macro it emitted is already in `m.items` to relabel.
    """
    voided = {
        "osdiB1v2TotalDecileTreatment", "osdiB1v2ManifestDecileTreatment",
        "osdiB1v2ManifestDecileK128", "osdiB1v2ManifestDecileK1024",
        "osdiB1v2P50TreatmentMs", "osdiB1v2P50Paired",
    }
    found = set()
    for i, (name, value, provenance) in enumerate(m.items):
        if name in voided:
            m.items[i] = (name, value, provenance + _VOID_AS_FORMAT3)
            found.add(name)
    eq(found, voided, "osdi_paper_macros: every v2 commit-cost treatment "
       "macro named for relabeling was actually emitted by compute_b1_v2")


def compute_b1_v2e(m: Macros) -> None:
    manifest = json.loads(B1_V2E_MANIFEST.read_text(encoding="utf-8"))
    raw_bytes = B1_V2E_RAW.read_bytes()
    raw = json.loads(raw_bytes)

    # digest-check: unlike B1-v2's own manifest (sha256 of its *own*
    # `measurements` block), this record's `result_digest` is the sha256 of
    # the raw-records *file's bytes* -- matching both the raw file's own
    # committed sha256 in the README's "Records and transfer" section and
    # the summary manifest's `result_digest` field verbatim. Recomputed from
    # the file this module actually reads, not copied from either document.
    recomputed_digest = hashlib.sha256(raw_bytes).hexdigest()
    eq(recomputed_digest, manifest["result_digest"],
       f"{relpath(B1_V2E_RAW)}: sha256(raw file bytes) matches the summary "
       f"manifest's ({relpath(B1_V2E_MANIFEST)}) own result_digest")

    trt_commit = manifest["git_commit"]
    eq(trt_commit[:7], "e5d4171", "B1-v2e frozen: treatment_commit short sha")

    # --- cell (b): commit-cost phase attribution, K=512, 3 reps/arm ---
    cb = raw["cell_b_commitcost"]
    trt_reps, ctl_reps = cb["treatment_reps_full"], cb["control_reps_full"]
    eq(len(trt_reps), 3, "B1-v2e: treatment commitcost has 3 reps")
    eq(len(ctl_reps), 3, "B1-v2e: control commitcost has 3 reps")

    def decile_ratio(rep: dict) -> float:
        return rep["last_decile_us"]["total_us"] / rep["first_decile_us"]["total_us"]

    total_decile_trt = statistics.median(decile_ratio(r) for r in trt_reps)
    total_decile_ctl = statistics.median(decile_ratio(r) for r in ctl_reps)
    close(total_decile_trt, 1.017, 0.001,
          "B1-v2e frozen: treatment engine-commit total_us decile ratio (median of 3 reps)")
    close(total_decile_ctl, 1.696, 0.001,
          "B1-v2e frozen: control engine-commit total_us decile ratio (median of 3 reps)")

    meas_cb = manifest["measurements"]["cell_b_commitcost"]
    close(total_decile_trt, meas_cb["treatment_decile_ratio"], 0.001,
          "B1-v2e: recomputed treatment decile ratio matches manifest's own field")
    close(total_decile_ctl, meas_cb["control_decile_ratio"], 0.001,
          "B1-v2e: recomputed control decile ratio matches manifest's own field")

    # manifest_bytes -- the format evidence itself: format 3 is 1,592-1,595 B,
    # format 2 is 1,565-1,568 B (this is exactly what the B1-v2 README
    # correction checked to find the v2 A/B's treatment reps mislabeled).
    trt_first_bytes = {r["first_decile_us"]["manifest_bytes"] for r in trt_reps}
    ctl_first_bytes = {r["first_decile_us"]["manifest_bytes"] for r in ctl_reps}
    eq(trt_first_bytes, {1592}, "B1-v2e frozen: every treatment rep's "
       "first-decile manifest_bytes is 1,592 B (format 3)")
    eq(ctl_first_bytes, {1565}, "B1-v2e frozen: every control rep's "
       "first-decile manifest_bytes is 1,565 B (format 2, unchanged engine)")
    manifest_bytes_trt = next(iter(trt_first_bytes))
    manifest_bytes_ctl = next(iter(ctl_first_bytes))

    # residual_first_us/residual_last_us -- the B1V2_AB_DIAGNOSIS memo's own
    # Q1 metric, mean over reps (the record's own field name says so:
    # `*_mean_of_reps`; this is a mean, not a median, deliberately -- a mean
    # would show a fat-tailed residual a median could hide).
    residual_first = statistics.fmean(r["residual_first_us"] for r in trt_reps)
    residual_last = statistics.fmean(r["residual_last_us"] for r in trt_reps)
    close(residual_first, meas_cb["treatment_residual_first_us_mean_of_reps"], 0.01,
          "B1-v2e: recomputed treatment residual_first_us (mean of reps) matches "
          "manifest's own field")
    close(residual_last, meas_cb["treatment_residual_last_us_mean_of_reps"], 0.01,
          "B1-v2e: recomputed treatment residual_last_us (mean of reps) matches "
          "manifest's own field")

    # engine-commit p50 -- phase_p50_us.total_us (the commit's engine-side
    # total, excluding wal_us/apply_us), median of reps. This is the frozen
    # Addendum 3 quantity: v1's README reports it as "engine-commit p50
    # (total_us)", the 5.07 ms baseline and 5.58 ms bar are this quantity,
    # and osdiB1v2P50* (5.478/5.430) already tracks it -- osdiB1v2eP50*
    # below is the same quantity for this remeasurement, not a different
    # one. The manifest's own digested measurements block reports it too,
    # and the README's prose quotes it (paired ratio 0.715x).
    engine_p50_trt = statistics.median(r["phase_p50_us"]["total_us"] for r in trt_reps)
    engine_p50_ctl = statistics.median(r["phase_p50_us"]["total_us"] for r in ctl_reps)
    eq(engine_p50_trt, 3365,
       "B1-v2e frozen: treatment engine-internal p50 total_us (median of 3 reps)")
    eq(engine_p50_ctl, 4704,
       "B1-v2e frozen: control engine-internal p50 total_us (median of 3 reps)")
    eq(engine_p50_trt, meas_cb["treatment_p50_median_us"],
       "B1-v2e: recomputed treatment engine-internal p50 matches manifest's own field")
    eq(engine_p50_ctl, meas_cb["control_p50_median_us"],
       "B1-v2e: recomputed control engine-internal p50 matches manifest's own field")

    engine_p50_paired = engine_p50_trt / engine_p50_ctl
    close(engine_p50_paired, 0.715, 0.001,
          "B1-v2e frozen: engine-internal p50 paired ratio treatment/control")
    close(engine_p50_paired, meas_cb["paired_p50_ratio_treatment_over_control"], 0.001,
          "B1-v2e: recomputed engine-internal p50 paired ratio matches manifest's own field")

    # wall-clock p50 -- commit_ms.p50 (the full per-commit latency
    # `_timed_write` measures: wal fsync + Python-side eventlog append +
    # apply_ops + engine commit), median of reps. This is *not* the frozen
    # Addendum 3 quantity above -- it is reported separately, under its own
    # osdiB1v2eWallP50* names, so a future reader of this file never
    # mistakes it for the number the paper's threshold actually binds.
    wall_p50_trt = statistics.median(r["commit_ms"]["p50"] for r in trt_reps)
    wall_p50_ctl = statistics.median(r["commit_ms"]["p50"] for r in ctl_reps)
    close(wall_p50_trt, 4.274, 0.001,
          "B1-v2e frozen: treatment wall-clock commit_ms.p50 (median of 3 reps)")
    close(wall_p50_ctl, 5.303, 0.001,
          "B1-v2e frozen: control wall-clock commit_ms.p50 (median of 3 reps)")

    wall_p50_paired = wall_p50_trt / wall_p50_ctl
    close(wall_p50_paired, 0.806, 0.001,
          "B1-v2e frozen: wall-clock p50 paired ratio treatment/control")

    # --- cell (a): chain-open component split at G~10k, K=512 ---
    ca = raw["cell_a_chain_open"]
    trt_open, ctl_open = ca["treatment"], ca["control"]
    meas_ca = manifest["measurements"]["cell_a_chain_open"]

    trt_generation = trt_open["generation"]
    ctl_generation = ctl_open["generation"]
    eq(trt_generation, 10365, "B1-v2e frozen: treatment chain-open generation (G)")
    eq(ctl_generation, 10052, "B1-v2e frozen: control chain-open generation (G)")
    eq(trt_generation, meas_ca["treatment_generation"],
       "B1-v2e: recomputed treatment generation matches manifest's own field")
    eq(ctl_generation, meas_ca["control_generation"],
       "B1-v2e: recomputed control generation matches manifest's own field")

    phases = trt_open["open_phase_p50_us"]
    eq(phases["chain_format"], 3, "B1-v2e frozen: treatment open chain_format is 3")
    checkpoint_us = phases["checkpoint_read_parse_us"]
    merkle_us = phases["merkle_verify_us"]
    state_build_us = phases["state_build_us"]
    delta_replay_us = phases["delta_replay_us"]
    dictionary_us = phases["dictionary_open_us"]
    total_us = phases["total_us"]

    eq(checkpoint_us, 42124, "B1-v2e frozen: checkpoint_read_parse_us (median)")
    eq(merkle_us, 40204, "B1-v2e frozen: merkle_verify_us (median)")
    eq(state_build_us, 1828, "B1-v2e frozen: state_build_us (median)")
    eq(delta_replay_us, 2899, "B1-v2e frozen: delta_replay_us (median)")
    eq(dictionary_us, 1066460, "B1-v2e frozen: dictionary_open_us (median)")
    eq(total_us, 1191773, "B1-v2e frozen: open total_us (median)")

    component_us = checkpoint_us + merkle_us + state_build_us + delta_replay_us
    eq(component_us, 87055, "B1-v2e: recomputed manifest-chain component "
       "(checkpoint+merkle+state_build+delta_replay) sums to 87,055 us")
    eq(component_us, meas_ca["treatment_manifest_chain_component_us_median"],
       "B1-v2e: recomputed manifest-chain component matches manifest's own field")
    breakdown = meas_ca["treatment_manifest_chain_component_breakdown_us_median"]
    eq(checkpoint_us, breakdown["checkpoint_read_parse_us"],
       "B1-v2e: recomputed checkpoint_read_parse_us matches manifest's own field")
    eq(merkle_us, breakdown["merkle_verify_us"],
       "B1-v2e: recomputed merkle_verify_us matches manifest's own field")
    eq(state_build_us, breakdown["state_build_us"],
       "B1-v2e: recomputed state_build_us matches manifest's own field")
    eq(delta_replay_us, breakdown["delta_replay_us"],
       "B1-v2e: recomputed delta_replay_us matches manifest's own field")
    eq(dictionary_us, meas_ca["treatment_dictionary_open_us_median"],
       "B1-v2e: recomputed dictionary_open_us matches manifest's own field")
    eq(total_us, meas_ca["treatment_total_us_median"],
       "B1-v2e: recomputed open total_us matches manifest's own field")

    # control's chain-open is flagged confounded by the concurrent phase-2
    # backup transfer (raw record's own caveat) -- not a measurement of
    # relative open cost, so no control-side open macro is emitted, and no
    # paired ratio either.
    require(ctl_open["component_breakdown"] is not None
            and "not available" in ctl_open["component_breakdown"],
            "B1-v2e: control component_breakdown is genuinely absent (flagged), "
            "not fabricated")
    require("caveat" in meas_ca and "backup" in meas_ca["caveat"].lower(),
            "B1-v2e: manifest's own measurements record the concurrent-backup "
            "caveat for cell (a)")

    # --- emit macros ---
    m.add("osdiB1v2eTreatmentCommit", trt_commit[:7],
          f"{relpath(B1_V2E_MANIFEST)}: git_commit, short sha")

    m.add("osdiB1v2eTotalDecileTreatment", f"{total_decile_trt:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.treatment_reps_full[*]."
          "last_decile_us.total_us / [*].first_decile_us.total_us, median over 3 reps "
          "-- supersedes osdiB1v2TotalDecileTreatment, which measured a format-2 chain")
    m.add("osdiB1v2eTotalDecileControl", f"{total_decile_ctl:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.control_reps_full[*]."
          "last_decile_us.total_us / [*].first_decile_us.total_us, median over 3 reps")

    m.add("osdiB1v2eResidualFirstUs", f"{residual_first:.2f}",
          f"{relpath(B1_V2E_RAW)}: mean(cell_b_commitcost.treatment_reps_full[*]."
          "residual_first_us) over 3 reps -- the B1V2_AB_DIAGNOSIS memo's Q1 metric, "
          "now ~30us against a ~3,300-3,400us total_us (closed, not hidden)")
    m.add("osdiB1v2eResidualLastUs", f"{residual_last:.2f}",
          f"{relpath(B1_V2E_RAW)}: mean(cell_b_commitcost.treatment_reps_full[*]."
          "residual_last_us) over 3 reps")

    m.add("osdiB1v2eP50TreatmentMs", f"{engine_p50_trt / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.treatment_reps_full[*]."
          "phase_p50_us.total_us, median over 3 reps, /1000, ms -- engine-commit p50 "
          "(total_us), the Addendum 3 quantity, same as osdiB1v2P50*")
    m.add("osdiB1v2eP50ControlMs", f"{engine_p50_ctl / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.control_reps_full[*]."
          "phase_p50_us.total_us, median over 3 reps, /1000, ms -- engine-commit p50 "
          "(total_us), the Addendum 3 quantity, same as osdiB1v2P50*")
    m.add("osdiB1v2eP50Paired", f"{engine_p50_paired:.3f}",
          f"{relpath(B1_V2E_RAW)}: median(treatment[*].phase_p50_us.total_us) / "
          "median(control[*].phase_p50_us.total_us) -- engine-commit p50 (total_us), "
          "the Addendum 3 quantity, same as osdiB1v2P50*")

    m.add("osdiB1v2eWallP50TreatmentMs", f"{wall_p50_trt:.3f}",
          f"{relpath(B1_V2E_RAW)}: median(cell_b_commitcost.treatment_reps_full[*]."
          "commit_ms.p50) over 3 reps, ms -- wall-clock commit_ms.p50 incl. "
          "Python-side eventlog append -- not the frozen quantity")
    m.add("osdiB1v2eWallP50ControlMs", f"{wall_p50_ctl:.3f}",
          f"{relpath(B1_V2E_RAW)}: median(cell_b_commitcost.control_reps_full[*]."
          "commit_ms.p50) over 3 reps, ms -- wall-clock commit_ms.p50 incl. "
          "Python-side eventlog append -- not the frozen quantity")
    m.add("osdiB1v2eWallPaired", f"{wall_p50_paired:.3f}",
          f"{relpath(B1_V2E_RAW)}: median(treatment[*].commit_ms.p50) / "
          "median(control[*].commit_ms.p50) -- wall-clock commit_ms.p50 incl. "
          "Python-side eventlog append -- not the frozen quantity")

    m.add("osdiB1v2eOpenComponentMs", f"{component_us / 1000:.2f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us -- "
          "checkpoint_read_parse_us + merkle_verify_us + state_build_us + "
          "delta_replay_us, /1000, ms")
    m.add("osdiB1v2eOpenCheckpointMs", f"{checkpoint_us / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "checkpoint_read_parse_us, /1000, ms")
    m.add("osdiB1v2eOpenMerkleVerifyMs", f"{merkle_us / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "merkle_verify_us, /1000, ms")
    m.add("osdiB1v2eOpenStateBuildMs", f"{state_build_us / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "state_build_us, /1000, ms")
    m.add("osdiB1v2eOpenDeltaReplayMs", f"{delta_replay_us / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "delta_replay_us, /1000, ms")
    m.add("osdiB1v2eOpenDictionaryMs", f"{dictionary_us / 1000:.1f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "dictionary_open_us, /1000, ms -- dominates the open (89.5% of total_us)")
    m.add("osdiB1v2eOpenTotalMs", f"{total_us / 1000:.1f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "total_us, /1000, ms")
    m.add("osdiB1v2eOpenGeneration", tex_num(trt_generation),
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.generation")

    m.add("osdiB1v2eControlOpenStatus", "confounded (concurrent backup transfer)",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.control's open time (9.6-10.4s) "
          "was measured while a phase-2 tar backup ran concurrently on xzgpu (text "
          "macro, not a number -- see cell_a_chain_open.control and the manifest's "
          "own measurements.cell_a_chain_open.caveat)")

    m.add("osdiB1v2eManifestBytesTreatment", tex_num(manifest_bytes_trt),
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.treatment_reps_full[*]."
          "first_decile_us.manifest_bytes, constant across 3 reps -- the format-3 "
          "evidence (versus the v2 A/B's mislabeled treatment, which matched "
          "osdiB1v2eManifestBytesControl instead)")
    m.add("osdiB1v2eManifestBytesControl", tex_num(manifest_bytes_ctl),
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.control_reps_full[*]."
          "first_decile_us.manifest_bytes, constant across 3 reps -- the format-2 "
          "byte size the v2 A/B's treatment reps actually matched")


# --------------------------------------------------------------------------
# C3 (cont.) --- Lane P-CO7: chain-open re-measurement on a confirmed-quiet
# host (same v2e B1(c) cell, treatment ebe1dc2, re-run because the v2e lane's
# control open time was confounded by a concurrent tar backup). Every
# component figure below is a per-field median recomputed from the raw
# record's 3-rep ``open_phase_us`` array, cross-checked against that same
# record's own precomputed ``open_phase_p50_us`` aggregate and against the
# summary manifest's own quoted fields -- never read off either aggregate
# without first recomputing it from the reps underneath.
# --------------------------------------------------------------------------

def compute_b1_co7(m: Macros) -> None:
    manifest = json.loads(B1_CO7_MANIFEST.read_text(encoding="utf-8"))
    raw_bytes = B1_CO7_RAW.read_bytes()
    raw = json.loads(raw_bytes)

    # digest-check: same discipline as B1-v2e -- result_digest is sha256 of
    # the raw records file's own bytes, recomputed from the file this module
    # actually reads, not copied from either document.
    recomputed_digest = hashlib.sha256(raw_bytes).hexdigest()
    eq(recomputed_digest, manifest["result_digest"],
       f"{relpath(B1_CO7_RAW)}: sha256(raw file bytes) matches the summary "
       f"manifest's ({relpath(B1_CO7_MANIFEST)}) own result_digest")

    trt_commit = manifest["git_commit"]
    eq(trt_commit, raw["treatment_commit"],
       "P-CO7: manifest's git_commit matches the raw record's own treatment_commit")
    eq(trt_commit, "ebe1dc2", "P-CO7 frozen: treatment commit")

    ca = raw["cell_a_chain_open"]
    trt, ctl = ca["treatment"], ca["control"]
    meas_ca = manifest["measurements"]["cell_a_chain_open"]

    reps = trt["open_phase_us"]
    eq(len(reps), 3, "P-CO7: treatment chain-open has 3 reps")

    def med(field: str) -> float:
        return statistics.median(r[field] for r in reps)

    checkpoint_us = med("checkpoint_read_parse_us")
    merkle_us = med("merkle_verify_us")
    state_build_us = med("state_build_us")
    delta_replay_us = med("delta_replay_us")
    dictionary_us = med("dictionary_open_us")
    total_us = med("total_us")

    eq(checkpoint_us, 12670, "P-CO7 frozen: checkpoint_read_parse_us (median of 3 reps)")
    eq(merkle_us, 34858, "P-CO7 frozen: merkle_verify_us (median of 3 reps)")
    eq(state_build_us, 5935, "P-CO7 frozen: state_build_us (median of 3 reps)")
    eq(delta_replay_us, 6256, "P-CO7 frozen: delta_replay_us (median of 3 reps)")
    eq(dictionary_us, 1092729, "P-CO7 frozen: dictionary_open_us (median of 3 reps)")
    eq(total_us, 1155032, "P-CO7 frozen: open total_us (median of 3 reps)")

    # cross-check the recomputed per-field medians against the raw record's
    # own precomputed open_phase_p50_us aggregate
    p50 = trt["open_phase_p50_us"]
    eq(checkpoint_us, p50["checkpoint_read_parse_us"],
       "P-CO7: recomputed checkpoint_read_parse_us matches raw record's own open_phase_p50_us")
    eq(merkle_us, p50["merkle_verify_us"],
       "P-CO7: recomputed merkle_verify_us matches raw record's own open_phase_p50_us")
    eq(state_build_us, p50["state_build_us"],
       "P-CO7: recomputed state_build_us matches raw record's own open_phase_p50_us")
    eq(delta_replay_us, p50["delta_replay_us"],
       "P-CO7: recomputed delta_replay_us matches raw record's own open_phase_p50_us")
    eq(dictionary_us, p50["dictionary_open_us"],
       "P-CO7: recomputed dictionary_open_us matches raw record's own open_phase_p50_us")
    eq(total_us, p50["total_us"],
       "P-CO7: recomputed open total_us matches raw record's own open_phase_p50_us")

    # ... and against the summary manifest's own quoted fields
    eq(checkpoint_us, meas_ca["treatment_checkpoint_read_parse_us_median"],
       "P-CO7: recomputed checkpoint_read_parse_us matches manifest's own field")
    eq(merkle_us, meas_ca["treatment_merkle_verify_us_median"],
       "P-CO7: recomputed merkle_verify_us matches manifest's own field")
    eq(state_build_us, meas_ca["treatment_state_build_us_median"],
       "P-CO7: recomputed state_build_us matches manifest's own field")
    eq(delta_replay_us, meas_ca["treatment_delta_replay_us_median"],
       "P-CO7: recomputed delta_replay_us matches manifest's own field")
    eq(dictionary_us, meas_ca["treatment_dictionary_open_us_median"],
       "P-CO7: recomputed dictionary_open_us matches manifest's own field")
    eq(total_us, meas_ca["treatment_total_us_median"],
       "P-CO7: recomputed open total_us matches manifest's own field")

    component_us = checkpoint_us + merkle_us + state_build_us + delta_replay_us
    eq(component_us, 59719, "P-CO7: recomputed manifest-chain component "
       "(checkpoint+merkle+state_build+delta_replay) sums to 59,719 us")
    eq(component_us, ca["manifest_chain_component_us_treatment_median"]["sum_us"],
       "P-CO7: recomputed component sum matches raw record's own sum_us field")
    eq(component_us, meas_ca["treatment_manifest_chain_component_us_median_sum"],
       "P-CO7: recomputed component sum matches manifest's own field")

    # delta_count is constant across reps (K=512 checkpoint cadence)
    delta_counts = {r["delta_count"] for r in reps}
    eq(delta_counts, {388}, "P-CO7 frozen: every treatment rep's delta_count is 388")
    delta_count = next(iter(delta_counts))

    trt_generation = trt["generation"]
    eq(trt_generation, 10116, "P-CO7 frozen: treatment chain-open generation (G)")
    eq(trt_generation, meas_ca["treatment_generation"],
       "P-CO7: recomputed treatment generation matches manifest's own field")

    checkpoint_generation = trt_generation - delta_count
    eq(checkpoint_generation, 9728,
       "P-CO7: recomputed checkpoint generation (generation - delta_count)")
    require(checkpoint_generation % 512 == 0,
            "P-CO7: checkpoint generation is a multiple of K=512, consistent with the "
            "chain's checkpoint cadence")

    state_build_per_delta_us = state_build_us / delta_count
    delta_replay_per_delta_us = delta_replay_us / delta_count
    close(state_build_per_delta_us, 15.3, 0.05,
          "P-CO7 frozen: state_build_us / delta_count (us/delta)")
    close(delta_replay_per_delta_us, 16.1, 0.05,
          "P-CO7 frozen: delta_replay_us / delta_count (us/delta)")

    # --- control ---
    ctl_generation = ctl["generation"]
    eq(ctl_generation, 10042, "P-CO7 frozen: control chain-open generation (G)")
    eq(ctl_generation, meas_ca["control_generation"],
       "P-CO7: recomputed control generation matches manifest's own field")

    ctl_reps_ms = ctl["open_ms"]
    eq(len(ctl_reps_ms), 3, "P-CO7: control chain-open has 3 reps")
    ctl_open_ms = statistics.median(ctl_reps_ms)
    close(ctl_open_ms, 10100.238, 0.001,
          "P-CO7 frozen: control open_ms (median of 3 reps)")
    eq(ctl_open_ms, ctl["open_ms_median"],
       "P-CO7: recomputed control open_ms median matches raw record's own field")
    close(ctl_open_ms, meas_ca["control_open_ms_median_wallclock"], 0.001,
          "P-CO7: recomputed control open_ms matches manifest's own field")

    require(ctl["component_breakdown"] is not None
            and "not available" in ctl["component_breakdown"],
            "P-CO7: control component_breakdown is genuinely absent (flagged), "
            "not fabricated -- the pinned control engine predates open_phase_us()")

    ctl_delta_count = ctl_generation % 512
    eq(ctl_delta_count, 314, "P-CO7: recomputed control delta_count (control_generation mod K=512)")

    # control has no phase breakdown; the treatment's measured dictionary-open
    # time is used only as an *estimate* of the control's own dictionary-open
    # cost (both arms open the same dictionary format) to back out an
    # approximate per-delta manifest-chain cost for the control arm. This is
    # explicitly an estimate built from one measured term and one measured
    # total, not a second independent measurement.
    dictionary_ms = dictionary_us / 1000
    ctl_per_delta_ms = (ctl_open_ms - dictionary_ms) / ctl_delta_count
    close(ctl_per_delta_ms, 28.7, 0.05,
          "P-CO7 frozen: estimated control per-delta manifest-chain cost "
          "((control open_ms - treatment's measured dictionary_open_ms estimate) / "
          "control delta_count)")

    open_ratio = (total_us / 1000) / ctl_open_ms
    close(open_ratio, 0.114, 0.001,
          "P-CO7 frozen: treatment/control open ratio (treatment total_us/1000 / "
          "control open_ms)")
    close(open_ratio, meas_ca["total_open_ratio_treatment_over_control"], 0.001,
          "P-CO7: recomputed open ratio matches manifest's own field")

    # --- derived (not measurements): projected worst-phase open cost at
    # K-1=511 deltas, the last generation before the next checkpoint reset --
    # arithmetic on the measured per-delta costs above, clearly not itself a
    # measurement.
    worst_phase_deltas = 511
    worst_phase_open_trt_ms = (
        checkpoint_us / 1000 + merkle_us / 1000
        + (state_build_per_delta_us + delta_replay_per_delta_us) * worst_phase_deltas / 1000
    )
    close(worst_phase_open_trt_ms, 63.6, 0.1,
          "P-CO7 derived (arithmetic, not measured): projected treatment manifest-chain "
          "open at K-1=511 deltas (checkpoint_ms + merkle_ms + "
          "(state_build_per_delta_us + delta_replay_per_delta_us) * 511 / 1000)")

    worst_phase_open_ctl_s = ctl_per_delta_ms * worst_phase_deltas / 1000
    close(worst_phase_open_ctl_s, 14.7, 0.1,
          "P-CO7 derived (arithmetic, not measured): projected control manifest-chain "
          "open at K-1=511 deltas (estimated control per-delta cost * 511 / 1000)")

    # --- emit macros ---
    m.add("osdiB1co7TreatmentCommit", trt_commit,
          f"{relpath(B1_CO7_MANIFEST)}: git_commit")

    m.add("osdiB1co7CheckpointReadParseMs", us_to_ms_str(checkpoint_us, 2),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "checkpoint_read_parse_us) over 3 reps, /1000, ms")
    m.add("osdiB1co7MerkleVerifyMs", us_to_ms_str(merkle_us, 2),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "merkle_verify_us) over 3 reps, /1000, ms")
    m.add("osdiB1co7StateBuildMs", us_to_ms_str(state_build_us, 2),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "state_build_us) over 3 reps, /1000, ms")
    m.add("osdiB1co7DeltaReplayMs", us_to_ms_str(delta_replay_us, 2),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "delta_replay_us) over 3 reps, /1000, ms")
    m.add("osdiB1co7ComponentMs", us_to_ms_str(component_us, 2),
          f"{relpath(B1_CO7_RAW)}: checkpoint_read_parse_us + merkle_verify_us + "
          "state_build_us + delta_replay_us (each median of 3 reps), /1000, ms -- "
          "asserted equal to the raw record's own manifest_chain_component_us_treatment_"
          "median.sum_us")
    m.add("osdiB1co7DictionaryOpenMs", us_to_ms_str(dictionary_us, 1),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "dictionary_open_us) over 3 reps, /1000, ms -- dominates the open")
    m.add("osdiB1co7TotalMs", us_to_ms_str(total_us, 1),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "total_us) over 3 reps, /1000, ms")
    m.add("osdiB1co7Generation", tex_num(trt_generation),
          f"{relpath(B1_CO7_RAW)}: cell_a_chain_open.treatment.generation")
    m.add("osdiB1co7DeltaCount", tex_num(delta_count),
          f"{relpath(B1_CO7_RAW)}: cell_a_chain_open.treatment.open_phase_us[*]."
          "delta_count, constant across 3 reps")
    m.add("osdiB1co7CheckpointGeneration", tex_num(checkpoint_generation),
          f"{relpath(B1_CO7_RAW)}: cell_a_chain_open.treatment.generation - delta_count "
          "-- asserted a multiple of the K=512 checkpoint cadence")
    m.add("osdiB1co7StateBuildPerDeltaUs", f"{state_build_per_delta_us:.1f}",
          f"{relpath(B1_CO7_RAW)}: treatment state_build_us (median) / delta_count, "
          "us/delta")
    m.add("osdiB1co7DeltaReplayPerDeltaUs", f"{delta_replay_per_delta_us:.1f}",
          f"{relpath(B1_CO7_RAW)}: treatment delta_replay_us (median) / delta_count, "
          "us/delta")

    m.add("osdiB1co7ControlOpenMs", f"{ctl_open_ms:.1f}",
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.control.open_ms) over 3 reps, ms")
    m.add("osdiB1co7ControlGeneration", tex_num(ctl_generation),
          f"{relpath(B1_CO7_RAW)}: cell_a_chain_open.control.generation")
    m.add("osdiB1co7ControlDeltaCount", tex_num(ctl_delta_count),
          f"{relpath(B1_CO7_RAW)}: cell_a_chain_open.control.generation mod K=512 "
          "-- the control engine predates open_phase_us() and reports no delta_count "
          "of its own")
    m.add("osdiB1co7ControlPerDeltaMs", f"{ctl_per_delta_ms:.1f}",
          f"{relpath(B1_CO7_RAW)}: (control open_ms (median) - treatment's measured "
          "dictionary_open_us/1000, used as an estimate of the control's own "
          "dictionary-open cost) / control delta_count -- an estimate, not a second "
          "independent measurement")
    m.add("osdiB1co7OpenRatio", f"{open_ratio:.3f}",
          f"{relpath(B1_CO7_RAW)}: (treatment total_us (median) / 1000) / control "
          "open_ms (median)")

    m.add("osdiB1WorstPhaseOpenTreatmentMs", f"{worst_phase_open_trt_ms:.1f}",
          "derived, not measured: osdiB1co7CheckpointReadParseMs + osdiB1co7MerkleVerifyMs "
          "+ (osdiB1co7StateBuildPerDeltaUs + osdiB1co7DeltaReplayPerDeltaUs) * 511 / 1000 "
          "-- projected treatment manifest-chain open cost at K-1=511 deltas")
    m.add("osdiB1WorstPhaseOpenControlS", f"{worst_phase_open_ctl_s:.1f}",
          "derived, not measured: osdiB1co7ControlPerDeltaMs * 511 / 1000 -- projected "
          "control manifest-chain open cost at K-1=511 deltas")


# --------------------------------------------------------------------------
# C4 --- bounded version_history (B2 A/B)
# --------------------------------------------------------------------------

def compute_c4(m: Macros) -> None:
    raw = json.loads(B2_RAW.read_text(encoding="utf-8"))
    summary = json.loads(B2_SUMMARY.read_text(encoding="utf-8"))
    vh = raw["version_history"]["raw"]
    vh_summary = raw["version_history"]["summary"]

    def median_of(records, field):
        return statistics.median(r[field] for r in records)

    for key in ("control_1m", "treatment_1m", "control_10m", "treatment_10m"):
        recs = vh[key]
        eq(len(recs), 3, f"C4: {key} has 3 reps")
        require(all(r["reps"] == 1 for r in recs),
                f"C4: {key} rows are each a single rep (3 rows == 3 reps)")
        wall_med = median_of(recs, "median_ms")
        vmhwm_med = median_of(recs, "vmhwm_kb")
        close(wall_med, vh_summary[key]["wall_ms"]["median"], 0.5,
              f"C4: recomputed {key} wall_ms median matches raw summary")
        close(vmhwm_med, vh_summary[key]["vmhwm_kb"]["median"], 1,
              f"C4: recomputed {key} vmhwm_kb median matches raw summary")

    trt_10m = vh["treatment_10m"]
    trt_1m = vh["treatment_1m"]
    ctl_10m = vh["control_10m"]

    trt_10m_wall = median_of(trt_10m, "median_ms")
    trt_10m_vmhwm = median_of(trt_10m, "vmhwm_kb")
    trt_1m_vmhwm = median_of(trt_1m, "vmhwm_kb")
    ctl_10m_wall = median_of(ctl_10m, "median_ms")
    ctl_10m_vmhwm = median_of(ctl_10m, "vmhwm_kb")

    close(trt_10m_vmhwm, 1_229_724, 1, "C4 frozen: treatment 10M VmHWM median, KB")
    close(trt_10m_wall, 1866.8, 0.1, "C4 frozen: treatment 10M wall median, ms")
    close(trt_1m_vmhwm, 167_968, 1, "C4 frozen: treatment 1M VmHWM median, KB")
    close(ctl_10m_vmhwm, 13_414_332, 1, "C4 frozen: control 10M VmHWM median, KB")
    close(ctl_10m_wall, 179_250.1, 0.1, "C4 frozen: control 10M wall median, ms")

    rss_reduction = ctl_10m_vmhwm / trt_10m_vmhwm
    wall_speedup = ctl_10m_wall / trt_10m_wall
    close(rss_reduction, 10.9, 0.05, "C4 frozen: peak-RSS reduction ratio at 10M")
    # NOTE: b2-version-history-ab-2026-09.json's own falsifier prose says
    # "95.2x faster" for this ratio; recomputing it from the raw per-rep
    # median wall times (179,250.1 / 1,866.8, both already verified above
    # against the raw record) gives 96.02x, not 95.2x. The two other ratios
    # in this claim (RSS reduction, flatness) use the median consistently
    # and match the record's own summary fields exactly (checked above);
    # this is the one place recomputation disagrees with the record's own
    # hand-written prose -- almost certainly because that prose used the
    # mean of the three reps (177,262.2 / 1,862.2 = 95.19x) instead of the
    # median. Per this script's "assert, do not trust" discipline the
    # macro uses the recomputed (median-based) value, not the prose's.
    close(wall_speedup, 96.02, 0.05,
          "C4 frozen: wall speedup ratio at 10M (median-based; the record's own prose says "
          "95.2x, apparently computed from the mean of reps instead -- see comment)")

    flatness_ratio = trt_10m_vmhwm / trt_1m_vmhwm
    close(flatness_ratio, 7.32, 0.02,
          "C4 frozen: 1M->10M peak-RSS flatness ratio (refuted, bar <=2.5x)")
    require(flatness_ratio > 2.5, "C4: the near-flatness falsifier is refuted by the treatment ratio")

    falsifiers = summary["falsifiers"]
    eq(falsifiers["peak_rss_le_2.5GB_at_10M"]["verdict"], "PASS",
       "C4: F1 (peak RSS) verdict is PASS in the summary record")
    eq(falsifiers["1M_to_10M_peak_rss_ratio_le_2.5x"]["verdict"], "REFUTED",
       "C4: the flatness falsifier verdict is REFUTED in the summary record")

    # The 100M projection is the same linear (10x) extrapolation off the
    # measured 10M point that BOUNDED_VERSION_HISTORY_FORECAST_2026-09-13.md
    # uses ("~12.6 GB at 100M by the same linear extrapolation"); computed
    # here from the record alone so this macro has no dependency on that
    # gitignored, coordinator-internal doc. When the doc happens to be
    # present on disk (it is not shipped with this checkout or any
    # worktree), cross-check against its own stated figure.
    rss_gb = trt_10m_vmhwm * 1024 / 1e9
    proj_gb_value = 10 * rss_gb
    proj_gb = f"{proj_gb_value:.1f}"
    eq(proj_gb, "12.6", "C4 frozen: 100M linear-extrapolation projection (10x the 10M point)")
    if VH_FORECAST.exists():
        vh_txt = VH_FORECAST.read_text(encoding="utf-8")
        doc_proj = re.search(r"~(\d+\.\d+) GB at 100M by the same linear extrapolation", vh_txt)
        require(doc_proj is not None,
                "C4: BOUNDED_VERSION_HISTORY_FORECAST states the 100M linear-extrapolation "
                "projection (cross-check only; not this macro's source)")
        if doc_proj:
            eq(doc_proj.group(1), proj_gb,
               "C4: recomputed 100M projection matches the forecast doc's own stated figure")

    m.add("osdiVhRss", f"{rss_gb:.3f}",
          f"{relpath(B2_RAW)}: median(treatment_10m[*].vmhwm_kb), decimal GB")
    m.add("osdiVhWall", f"{trt_10m_wall / 1000:.2f}",
          f"{relpath(B2_RAW)}: median(treatment_10m[*].wall_ms) / 1000, s")
    m.add("osdiVhRatio", f"{flatness_ratio:.2f}",
          f"{relpath(B2_RAW)}: treatment 10M/1M VmHWM median ratio "
          "(the near-flatness falsifier: refuted, bar <=2.5x)")
    m.add("osdiVhProjHundredM", proj_gb,
          f"{relpath(B2_RAW)}: 10x median(treatment_10m[*].vmhwm_kb) -- the same "
          "linear extrapolation BOUNDED_VERSION_HISTORY_FORECAST_2026-09-13.md uses, cross-"
          "checked against that (gitignored) doc's own figure when it is present on disk")


# --------------------------------------------------------------------------
# C5 --- reader scaling at 10M
# --------------------------------------------------------------------------

def compute_c5(m: Macros) -> None:
    d = json.loads(READERS_10M.read_text(encoding="utf-8"))
    quiescent = {q["readers"]: q for q in d["quiescent"]}
    eq(sorted(quiescent), [1, 2, 4, 8, 16, 32], "C5: quiescent reader counts")

    readers_max = max(quiescent)
    eq(readers_max, 32, "C5 frozen: max concurrent readers")

    agg_one = quiescent[1]["aggregate_qps"]
    agg_max = quiescent[readers_max]["aggregate_qps"]
    eq(agg_one, 2.85, "C5 frozen: aggregate q/s at 1 reader")
    eq(agg_max, 27.72, "C5 frozen: aggregate q/s at 32 readers")

    vmhwm_medians_gib = [q["vmhwm_kb"]["median"] / 1024 / 1024 for q in quiescent.values()]
    vmhwm_lo, vmhwm_hi = min(vmhwm_medians_gib), max(vmhwm_medians_gib)
    close(vmhwm_lo, 1.19, 0.01, "C5 frozen: reader VmHWM flat-range low end, GiB")
    close(vmhwm_hi, 1.22, 0.01, "C5 frozen: reader VmHWM flat-range high end, GiB")

    mixed16 = {mm["writer"]: mm for mm in d["mixed"] if mm["readers"] == 16}
    require(False in mixed16 and True in mixed16, "C5: readers=16 has both writer arms")
    no_writer = mixed16[False]["per_query_p50_ms"]
    with_writer = mixed16[True]["per_query_p50_ms"]
    series_no = statistics.median(no_writer["series.count"])
    series_yes = statistics.median(with_writer["series.count"])
    coactive_no = statistics.median(no_writer["coactive.narrow"])
    coactive_yes = statistics.median(with_writer["coactive.narrow"])
    series_pct = (series_yes / series_no - 1) * 100
    coactive_pct = (coactive_yes / coactive_no - 1) * 100
    close(series_pct, 47.53, 0.01, "C5 frozen: series.count p50 writer tail cost at 16 readers")
    close(coactive_pct, 42.71, 0.01, "C5 frozen: coactive.narrow p50 writer tail cost at 16 readers")

    m.add("osdiReadersMax", readers_max,
          f"{relpath(READERS_10M)}: max(quiescent[*].readers)")
    m.add("osdiReaderVmhwm", f"{vmhwm_lo:.2f}--{vmhwm_hi:.2f}",
          f"{relpath(READERS_10M)}: min/max over readers of quiescent[*].vmhwm_kb.median, GiB")
    m.add("osdiAggQpsOne", f"{agg_one:.2f}",
          f"{relpath(READERS_10M)}: quiescent[readers=1].aggregate_qps")
    m.add("osdiAggQpsThirtyTwo", f"{agg_max:.2f}",
          f"{relpath(READERS_10M)}: quiescent[readers=32].aggregate_qps")
    m.add("osdiWriterTailCost",
          f"+{series_pct:.1f}\\%/+{coactive_pct:.1f}\\%",
          f"{relpath(READERS_10M)}: mixed[readers=16] per_query_p50_ms median, "
          "series.count/coactive.narrow, writer vs quiescent")


# --------------------------------------------------------------------------
# C6 --- selective refresh never reports a stale artifact fresh
# --------------------------------------------------------------------------

def compute_c6(m: Macros) -> None:
    # M4 record of account: trials-full.json + trials-fixture.json (never
    # trials-{full,fixture}-run1-superseded.json -- see M4_MEASURED_REPORT.md
    # "The floor is scored on run 2 alone").
    fresh_full = json.loads(FRESH_FULL.read_text(encoding="utf-8"))
    fresh_fixture = json.loads(FRESH_FIXTURE.read_text(encoding="utf-8"))
    m4_trials = fresh_full["trials"] + fresh_fixture["trials"]
    eq(len(m4_trials), fresh_full["trial_count"] + fresh_fixture["trial_count"],
       "C6: combined M4 trial pool matches each file's own trial_count")
    eq(len(m4_trials), 3354, "C6 frozen: M4 record-of-account trial pool")

    m4_changed = [t for t in m4_trials if t["changed"]]
    eq(len(m4_changed), 447, "C6 frozen: M4 changed-column trials")
    m4_false_fresh = sum(1 for t in m4_changed if t["verdict"] == "fresh")
    eq(m4_false_fresh, 0, "C6 frozen: M4 false-fresh count")
    eq(m4_false_fresh,
       fresh_full["summary"]["false_fresh"] + fresh_fixture["summary"]["false_fresh"],
       "C6: recomputed M4 false-fresh matches the sum of each file's own summary")

    m4_rt_false_fresh = sum(1 for t in m4_changed if t.get("rowtouch_verdict") == "fresh")
    eq(m4_rt_false_fresh, 212, "C6 frozen: M4 naive row-touch false-fresh count")
    rt_rate = round(m4_rt_false_fresh / len(m4_changed) * 1000) / 10
    eq(rt_rate, 47.4, "C6 frozen: M4 naive row-touch false-fresh rate")

    m4_newid = [t for t in m4_changed if t["placement"] == "new-identity"]
    eq(len(m4_newid), 89, "C6 frozen: M4 new-identity changed trials")
    m4_newid_missed = sum(1 for t in m4_newid if t.get("rowtouch_verdict") == "fresh")
    eq(m4_newid_missed, 89, "C6 frozen: every new-identity changed trial is missed by row-touch")

    # M5 round 2 (commit d830f13, Addendum 8): the carve-2 (topup-2) run and
    # the propagation-2 (topup-2) runs are the campaign's closing, scored
    # figures -- never the earlier run-1/topup-1 rounds, and never pooled
    # with them (Addendum 8 scores "round 2 ... alone per store"). Recomputed
    # here from the per-trial `rows`, never trusted from the record's own
    # `summary` block, though the two are cross-checked against each other.
    carve2 = json.loads(M5_CARVE_TWO.read_text(encoding="utf-8"))
    eq(carve2["git_sha"][:7], "d830f13", "C6: carve-2 record is at commit d830f13")
    carve2_rows = carve2["rows"]
    eq(len(carve2_rows), 28_044, "C6 frozen: carve-2 trial count")
    carve2_false_fresh = sum(1 for r in carve2_rows if r["changed"] and r["verdict"] == "fresh")
    eq(carve2_false_fresh, 0, "C6 frozen: carve-2 false-fresh count")
    eq(carve2_false_fresh, carve2["summary"]["control_invariant_violations"],
       "C6: recomputed carve-2 false-fresh matches the record's own control_invariant_violations")

    prop_rows_total = 0
    prop_avoided = 0
    prop_false_safe = 0
    prop_payload_changed = 0
    prop_false_safe_payload = 0
    for path in M5_PROP_TWO:
        d = json.loads(path.read_text(encoding="utf-8"))
        eq(d["git_sha"][:7], "d830f13", f"C6: {path.name} is at commit d830f13")
        rows = d["rows"]
        eq(len(rows), d["summary"]["decisions"],
           f"C6: {path.name} row count matches summary.decisions")
        prop_rows_total += len(rows)
        prop_avoided += sum(1 for r in rows if r["child_recomputed"] is False)
        prop_false_safe += sum(1 for r in rows if r["false_safe"])
        payload = [r for r in rows if r["parent_payload_changed"]]
        prop_payload_changed += len(payload)
        prop_false_safe_payload += sum(1 for r in payload if r["false_safe"])

    eq(prop_rows_total, 5867, "C6 frozen: M5 round-2 propagation decisions")
    eq(prop_payload_changed, 308, "C6 frozen: M5 round-2 payload-changing decisions")
    eq(prop_false_safe, 0, "C6 frozen: M5 round-2 false-safe count (total)")
    eq(prop_false_safe_payload, 0, "C6 frozen: M5 round-2 false-safe count (payload-changing)")
    eq(prop_avoided, 5808, "C6 frozen: M5 round-2 avoided-recomputation decisions")
    avoided_pct = round(1000 * prop_avoided / prop_rows_total) / 10
    eq(avoided_pct, 99.0, "C6 frozen: M5 round-2 avoided-recomputation rate")

    m.add("osdiFalseFreshCarveTwo", carve2_false_fresh,
          f"{relpath(M5_CARVE_TWO)}: rows with changed and verdict=='fresh', of 28,044")
    m.add("osdiRowTouchRate", f"{rt_rate:.1f}",
          f"{relpath(FRESH_FULL)}+{FRESH_FIXTURE.name}: naive row-touch false-fresh "
          "rate over M4's changed column, 212/447")
    m.add("osdiNewIdentityFF", m4_newid_missed,
          f"{relpath(FRESH_FULL)}+{FRESH_FIXTURE.name}: new-identity changed trials "
          "the row-touch rule calls fresh, of 89")
    m.add("osdiAvoidedPct", f"{avoided_pct:.1f}",
          "benchmarks/m5-v1/topup-propagation2-{bitcoinotc,collegemsg,sx-mathoverflow}.json: "
          "decisions with child_recomputed==False, 5,808/5,867")


# --------------------------------------------------------------------------
# C8 --- trust-boundary fault matrix, before and after D-160
# --------------------------------------------------------------------------

def outcome_totals(cells, *, strict: bool | None = None):
    tot = {"correct": 0, "safe-refusal": 0, "explicit-failure": 0, "silent-violation": 0}
    for c in cells:
        if strict is not None and c["strict_gate"] != strict:
            continue
        for k in tot:
            tot[k] += c["counts"][k]
    return tot


def compute_c8(m: Macros) -> None:
    pre = json.loads(FAULTS_PRE.read_text(encoding="utf-8"))
    post = json.loads(FAULTS_POST.read_text(encoding="utf-8"))

    # The post-fix (deployed-gate) record carries only the primary gate.
    post_cells = post["per_cell_gate_table"]
    require(all(not c["strict_gate"] for c in post_cells),
            "C8: the post-D-160 record carries only the primary (non-strict) gate")
    eq(post["total_trials"], sum(c["n_cases"] for c in post_cells),
       "C8: post-fix total_trials matches the sum of per-cell n_cases")
    eq(post["total_trials"], 3102, "C8 frozen: post-fix (deployed-gate) trial count")

    post_tot = outcome_totals(post_cells)
    eq(post_tot["explicit-failure"], 2236, "C8 frozen: post-fix explicit-failure count")
    eq(sum(post_tot.values()), post["total_trials"],
       "C8: post-fix outcome totals partition total_trials")

    post_headline = sum(sv["count"] for sv in post["silent_violations"] if sv["headline"])
    eq(post_headline, 0, "C8 frozen: post-fix headline silent-violation count")
    eq(post_headline, post["headline_silent_violation_count"],
       "C8: recomputed headline silent-violations matches the record's own field")

    f23_post = [c for c in post_cells if c["cell"] == "F2-3"]
    eq(len(f23_post), 1, "C8: exactly one F2-3 row in the post-fix primary-gate table")
    f23_post_count = f23_post[0]["counts"]["silent-violation"]
    eq(f23_post_count, 32, "C8 frozen: F2-3 (declared blind spot) silent-violation count, post-fix")

    # The pre-fix record carries both gate arms for the same underlying
    # trial set; the primary (non-strict) arm is the one the deployed
    # system actually used and is the one comparable to the post-fix
    # record (same 23 cells, same 3,102-trial population -- only F1-9's
    # outcome differs).
    pre_primary = [c for c in pre["per_cell_gate_table"] if not c["strict_gate"]]
    eq(sum(c["n_cases"] for c in pre_primary), 3102,
       "C8: pre-fix primary-gate trial count matches the post-fix population")

    f19_pre = [c for c in pre_primary if c["cell"] == "F1-9"]
    eq(len(f19_pre), 1, "C8: exactly one F1-9 row in the pre-fix primary-gate table")
    f19_pre_count = f19_pre[0]["counts"]["silent-violation"]
    eq(f19_pre_count, 271, "C8 frozen: F1-9 silent-violation count, pre-fix primary gate")

    f23_pre = [c for c in pre_primary if c["cell"] == "F2-3"]
    eq(len(f23_pre), 1, "C8: exactly one F2-3 row in the pre-fix primary-gate table")
    f23_pre_count = f23_pre[0]["counts"]["silent-violation"]
    eq(f23_pre_count, 32, "C8 frozen: F2-3 silent-violation count, pre-fix primary gate (unchanged)")
    eq(f23_pre_count, f23_post_count, "C8: F2-3's blind spot is unchanged by D-160")

    pre_primary_tot = outcome_totals(pre_primary)
    silent_pre = pre_primary_tot["silent-violation"]
    eq(silent_pre, f19_pre_count + f23_pre_count,
       "C8: pre-fix primary-gate silent-violation total is exactly F1-9 + F2-3 "
       "(no other cell produced one)")
    eq(silent_pre, 303, "C8 frozen: pre-fix (primary gate) total silent-violation count")

    # D-160's fix: every F1-9 trial that was silent-violation becomes
    # explicit-failure; nothing else about the population changes.
    eq(pre_primary_tot["correct"], post_tot["correct"], "C8: correct count unchanged by D-160")
    eq(pre_primary_tot["safe-refusal"], post_tot["safe-refusal"],
       "C8: safe-refusal count unchanged by D-160")
    eq(post_tot["explicit-failure"] - pre_primary_tot["explicit-failure"], f19_pre_count,
       "C8: the explicit-failure increase equals exactly F1-9's former silent-violation count")
    eq(pre_primary_tot["silent-violation"] - post_tot["silent-violation"], f19_pre_count,
       "C8: the silent-violation decrease equals exactly F1-9's former count")

    m.add("osdiFaultTrials", tex_num(post["total_trials"]),
          f"{relpath(FAULTS_POST)}: total_trials, the deployed (post-D-160) gate")
    m.add("osdiSilentPre", silent_pre,
          f"{relpath(FAULTS_PRE)}: primary-gate silent-violation total "
          "(F1-9 271 + F2-3 32), before D-160")
    m.add("osdiSilentPost", post_headline,
          f"{relpath(FAULTS_POST)}: headline_silent_violation_count (F2-3's 32 is a "
          "declared non-headline blind spot, see osdiFTwoThree)")
    m.add("osdiFOneNineBefore", f19_pre_count,
          f"{relpath(FAULTS_PRE)}: F1-9 (wrong_step_citation) silent-violation count, "
          "primary gate, before D-160 -- 0 after")
    m.add("osdiFTwoThree", f23_post_count,
          f"{relpath(FAULTS_POST)}: F2-3 silent-violation count, the pre-registered "
          "assumption-A2 blind spot, unchanged by D-160")


# --------------------------------------------------------------------------
# C7 (partial) --- correction-storm DAG phase, v1/v2/v3 (40 cells each)
# --------------------------------------------------------------------------

def compute_c7_dag(m: Macros) -> None:
    v1 = json.loads(DAG_V1.read_text(encoding="utf-8"))
    v2 = json.loads(DAG_V2.read_text(encoding="utf-8"))
    v3 = json.loads(DAG_V3.read_text(encoding="utf-8"))
    v1_rows = load_jsonl(DAG_V1_ROWS)
    v2_rows = load_jsonl(DAG_V2_ROWS)
    v3_rows = load_jsonl(DAG_V3_ROWS)

    for name, d, rows in (("v1", v1, v1_rows), ("v2", v2, v2_rows), ("v3", v3, v3_rows)):
        eq(d["total_tasks"], 40, f"DAG {name}: total_tasks")
        eq(len(d["per_cell"]), 40, f"DAG {name}: per_cell row count")
        eq(len(rows), 40, f"DAG {name}: rows.jsonl line count")

    eq(len({v1["total_tasks"], v2["total_tasks"], v3["total_tasks"]}), 1,
       "DAG: v1/v2/v3 all run the same 40-cell grid")
    dag_cells = 40

    def by_task(per_cell):
        return {c["task_id"]: c for c in per_cell}

    def rows_by_task(rows):
        return {r["_task_id"]: r for r in rows}

    v1c, v2c, v3c = by_task(v1["per_cell"]), by_task(v2["per_cell"]), by_task(v3["per_cell"])

    # Cross-check every per_cell summary field against the fuller per-row
    # dag.cascade block in the companion -rows.jsonl file -- two different
    # serializations of the same underlying measurement.
    for name, cells, rows in (("v1", v1c, v1_rows), ("v2", v2c, v2_rows), ("v3", v3c, v3_rows)):
        for r in rows:
            c = cells[r["_task_id"]]
            cascade = r["dag"]["cascade"]
            eq(cascade["false_safe_count"], c["false_safe_count"],
               f"DAG {name} task {c['task_id']}: rows.jsonl dag.cascade.false_safe_count "
               "matches per_cell")
            eq(cascade["nodes_visited"], c["nodes_visited"],
               f"DAG {name} task {c['task_id']}: rows.jsonl dag.cascade.nodes_visited "
               "matches per_cell")
            eq(cascade["quiescent"], c["quiescent"],
               f"DAG {name} task {c['task_id']}: rows.jsonl dag.cascade.quiescent matches per_cell")

    v1_fs_cells = [c for c in v1["per_cell"] if c["false_safe_count"] > 0]
    eq(len(v1_fs_cells), 20, "DAG v1 frozen: false-safe cell count")
    require(all(c["false_safe_count"] == 3 for c in v1_fs_cells),
            "DAG v1: every false-safe cell reports exactly 3 false-safes")
    v1_false_safe_per_cell = 3

    v2_fs_cells = sum(1 for c in v2["per_cell"] if c["false_safe_count"] > 0)
    eq(v2_fs_cells, 0, "DAG v2 frozen: false-safe cell count")
    v3_fs_cells = sum(1 for c in v3["per_cell"] if c["false_safe_count"] > 0)
    eq(v3_fs_cells, 0, "DAG v3 frozen: false-safe cell count")

    # G-S2 gate cross-check: v1 fails it (some cells false-safe), v2/v3 pass.
    eq(v1["gates"]["g_s2_false_safe_zero"], False, "DAG v1: G-S2 gate fails as expected")
    eq(len(v1["gates"]["g_s2_failing_cells"]), len(v1_fs_cells),
       "DAG v1: gate's failing-cell count matches recomputed false-safe cell count")
    for name, d in (("v2", v2), ("v3", v3)):
        eq(d["gates"]["g_s2_false_safe_zero"], True, f"DAG {name}: G-S2 gate passes")
        eq(d["gates"]["g_s2_failing_cells"], [], f"DAG {name}: no G-S2 failing cells")

    for seed in (0, 1):
        n = sum(1 for c in v1["per_cell"] if c["seed"] == seed)
        eq(n, 20, f"DAG: {n} cells at seed {seed} (expected 20 -- half the 40-cell grid)")

    def extra_visits(other_cells, seed):
        diffs = {other_cells[tid]["nodes_visited"] - v1c[tid]["nodes_visited"]
                 for tid in v1c if v1c[tid]["seed"] == seed}
        require(len(diffs) == 1,
                f"DAG: nodes_visited delta vs v1 is not constant across seed-{seed} cells "
                f"(got {sorted(diffs)})")
        return next(iter(diffs))

    v2_extra_s0 = extra_visits(v2c, 0)
    v2_extra_s1 = extra_visits(v2c, 1)
    v3_extra_s0 = extra_visits(v3c, 0)
    v3_extra_s1 = extra_visits(v3c, 1)

    eq(v2_extra_s0, 61, "DAG v2 frozen: nodes_visited delta vs v1, seed 0")
    eq(v2_extra_s1, 62, "DAG v2 frozen: nodes_visited delta vs v1, seed 1")
    eq(v3_extra_s0, 15, "DAG v3 frozen: nodes_visited delta vs v1, seed 0")
    eq(v3_extra_s1, 11, "DAG v3 frozen: nodes_visited delta vs v1, seed 1")

    # v3-only: the D-161 rollout's narrowing_coverage block (absent from v1/v2).
    require(all("narrowing_coverage" not in r["summary"] for r in v1_rows + v2_rows),
            "DAG v1/v2: narrowing_coverage is a v3-only (post-D-161-rollout) field")
    require(all("narrowing_coverage" in r["summary"] for r in v3_rows),
            "DAG v3: every cell carries a narrowing_coverage block")
    all_top_term_total = sum(r["summary"]["narrowing_coverage"]["n_all_top_term"] for r in v3_rows)
    eq(all_top_term_total, 0, "DAG v3 frozen: n_all_top_term summed over all 40 cells")

    def tgms_false_fresh(rows):
        return sum(r["summary"]["arms"][arm]["false_fresh"]
                   for r in rows for arm in ("tgms-L0", "tgms-L1"))

    ff_v1, ff_v2, ff_v3 = (tgms_false_fresh(v1_rows), tgms_false_fresh(v2_rows),
                           tgms_false_fresh(v3_rows))
    eq(ff_v1, 0, "DAG v1 frozen: tgms-L0+tgms-L1 false-fresh total")
    eq(ff_v2, 0, "DAG v2 frozen: tgms-L0+tgms-L1 false-fresh total")
    eq(ff_v3, 0, "DAG v3 frozen: tgms-L0+tgms-L1 false-fresh total")
    ff_total = ff_v1 + ff_v2 + ff_v3
    eq(ff_total, 0, "DAG v1+v2+v3 frozen: tgms-L0+tgms-L1 false-fresh total")

    m.add("osdiDagCells", dag_cells,
          f"{relpath(DAG_V1)}: total_tasks, == v2/v3's own total_tasks (40-cell grid, all three)")
    m.add("osdiDagV1FalseSafeCells", len(v1_fs_cells),
          f"{relpath(DAG_V1)}: per_cell entries with false_safe_count>0, of 40")
    m.add("osdiDagV2FalseSafeCells", v2_fs_cells,
          f"{relpath(DAG_V2)}: per_cell entries with false_safe_count>0, of 40")
    m.add("osdiDagV3FalseSafeCells", v3_fs_cells,
          f"{relpath(DAG_V3)}: per_cell entries with false_safe_count>0, of 40")
    m.add("osdiDagV1FalseSafePerCell", v1_false_safe_per_cell,
          f"{relpath(DAG_V1)}: false_safe_count in each of the 20 affected cells (uniform)")
    m.add("osdiDagV2ExtraVisitsSeedZero", v2_extra_s0,
          f"{relpath(DAG_V2)} vs {DAG_V1.name}: nodes_visited delta, seed-0 cells "
          "(constant across all 20, asserted)")
    m.add("osdiDagV2ExtraVisitsSeedOne", v2_extra_s1,
          f"{relpath(DAG_V2)} vs {DAG_V1.name}: nodes_visited delta, seed-1 cells "
          "(constant across all 20, asserted)")
    m.add("osdiDagV3ExtraVisitsSeedZero", v3_extra_s0,
          f"{relpath(DAG_V3)} vs {DAG_V1.name}: nodes_visited delta, seed-0 cells "
          "(constant across all 20, asserted)")
    m.add("osdiDagV3ExtraVisitsSeedOne", v3_extra_s1,
          f"{relpath(DAG_V3)} vs {DAG_V1.name}: nodes_visited delta, seed-1 cells "
          "(constant across all 20, asserted)")
    m.add("osdiDagV3AllTopTerm", all_top_term_total,
          f"{relpath(DAG_V3_ROWS)}: sum of summary.narrowing_coverage.n_all_top_term over "
          "all 40 cells")
    m.add("osdiDagFalseFreshTotal", ff_total,
          f"{relpath(DAG_V1_ROWS)}+{DAG_V2_ROWS.name}+{DAG_V3_ROWS.name}: sum of "
          "summary.arms.{tgms-L0,tgms-L1}.false_fresh over all 120 cells (v1+v2+v3)")


# --------------------------------------------------------------------------
# C7 (partial) --- R-18 probe, N=10,000 c1 seed 0, 5 batches
# --------------------------------------------------------------------------

def compute_c7_r18(m: Macros) -> None:
    d = json.loads(R18_PROBE.read_text(encoding="utf-8"))
    rows = load_jsonl(R18_PROBE_ROWS)
    rows.sort(key=lambda r: r["batch_index"])

    eq(d["config"]["n_artifacts"], 10000, "R18 frozen: probe artifact count")
    eq(d["config"]["batches"], 5, "R18 frozen: batch count")
    eq(len(rows), d["config"]["batches"], "R18: rows.jsonl line count matches config.batches")
    eq(d["config"]["wall_capped"], False, "R18: probe was not wall-capped")
    eq(d["config"]["mix"], "c1", "R18: this probe is the c1 mix (P7's R-18 trip criterion "
       "targets c4, not this cell)")

    n_registered_vals = {r["n_registered"] for r in rows}
    require(len(n_registered_vals) == 1, "R18: n_registered constant across batches")
    n_registered = next(iter(n_registered_vals))
    eq(n_registered, d["config"]["n_registered"], "R18: recomputed n_registered matches config")

    intersects = [r["intersects_calls"] for r in rows]
    lookup_ms = [r["lookup_wall_ms"] for r in rows]
    survivors = [r["candidate_survivors"] for r in rows]
    changed = [r["changed_count"] for r in rows]

    intersects_med = statistics.median(intersects)
    lookup_med = statistics.median(lookup_ms)
    survivors_med = statistics.median(survivors)
    eq(intersects_med, 13009, "R18 frozen: median intersects_calls/batch")
    require(intersects_med <= 50000,
            "R18: median intersects_calls/batch does not exceed the R-18 trip threshold "
            "(P7: trips in c4, not this c1 cell)")

    survivor_fraction = survivors_med / n_registered
    close(survivor_fraction, 0.7131, 0.001,
          "R18 frozen: median candidate_survivors / n_registered")

    precision_per_batch = [c / s for c, s in zip(changed, survivors)]
    precision_med = statistics.median(precision_per_batch)
    close(precision_med, 0.0851, 0.001,
          "R18 frozen: median(changed_count / candidate_survivors) over the 5 batches")

    l1_check_ms = [r["arms"]["tgms-L1"]["check_wall_ms"] for r in rows]
    l1_ttf_ms = [r["arms"]["tgms-L1"]["ttf_ms"] for r in rows]
    global_ttf_ms = [r["arms"]["global-recompute"]["ttf_ms"] for r in rows]
    l1_invalidated = [r["arms"]["tgms-L1"]["invalidated_count"] for r in rows]
    l1_false_fresh = [r["arms"]["tgms-L1"]["false_fresh_count"] for r in rows]

    eq(sum(l1_false_fresh), 0, "R18 frozen: tgms-L1 false-fresh count, summed over batches")
    eq(sum(l1_false_fresh), d["summary"]["arms"]["tgms-L1"]["false_fresh"],
       "R18: recomputed tgms-L1 false-fresh matches the record's own summary.arms field")

    l1_check_med = statistics.median(l1_check_ms)
    l1_ttf_med = statistics.median(l1_ttf_ms)
    global_ttf_med = statistics.median(global_ttf_ms)

    # Cross-check every recomputed per-batch median against the record's own
    # aggregate summary.arms block -- never trusted without this.
    eq(l1_ttf_med, d["summary"]["arms"]["tgms-L1"]["ttf_p50_ms"],
       "R18: recomputed tgms-L1 ttf p50 (median of per-batch ttf_ms) matches "
       "the record's own summary.arms field")
    eq(global_ttf_med, d["summary"]["arms"]["global-recompute"]["ttf_p50_ms"],
       "R18: recomputed global-recompute ttf p50 matches the record's own summary.arms field")

    avoided_decision = 1 - sum(l1_invalidated) / (n_registered * len(rows))
    eq(avoided_decision, d["summary"]["arms"]["tgms-L1"]["avoided_recompute_decision"],
       "R18: recomputed avoided_recompute_decision (1 - sum(invalidated)/sum(n_registered)) "
       "matches the record's own summary.arms field")

    # "Speedup of L1 over global-recompute" == baseline/candidate == global/L1,
    # per campaign.yaml's P5/P6 convention (>1.0 means L1 is faster). This
    # probe measures speedup < 1.0 -- L1 pays more in end-to-end time-to-fresh
    # than global-recompute here even though R-18 itself did not trip (median
    # intersects_calls 13,009 << the 50,000 trip threshold) -- a genuine,
    # asserted result, not a falsifier-triggering trip.
    speedup = global_ttf_med / l1_ttf_med
    close(speedup, 0.8067, 0.001, "R18 frozen: speedup of tgms-L1 over global-recompute "
          "(global_ttf_median / l1_ttf_median)")
    require(0.80 <= speedup <= 0.82,
            "R18: speedup(L1 over global) falls within the documented [0.80, 0.82] range")

    check_seconds_med = l1_check_med / 1000
    ttf_l1_s = l1_ttf_med / 1000
    ttf_global_s = global_ttf_med / 1000

    m.add("osdiR18Artifacts", tex_num(d["config"]["n_artifacts"]),
          f"{relpath(R18_PROBE)}: config.n_artifacts")
    m.add("osdiR18IntersectsCallsMedian", tex_num(int(intersects_med)),
          f"{relpath(R18_PROBE_ROWS)}: median(intersects_calls) over the 5 batches")
    m.add("osdiR18LookupMsMedian", f"{lookup_med:.2f}",
          f"{relpath(R18_PROBE_ROWS)}: median(lookup_wall_ms) over the 5 batches, ms")
    m.add("osdiR18SurvivorFraction", f"{survivor_fraction * 100:.1f}",
          f"{relpath(R18_PROBE_ROWS)}: median(candidate_survivors) / n_registered, percent")
    m.add("osdiR18CheckSecondsMedian", f"{check_seconds_med:.1f}",
          f"{relpath(R18_PROBE_ROWS)}: median(arms.tgms-L1.check_wall_ms) over the 5 "
          "batches, /1000, s")
    m.add("osdiR18TtfL1Seconds", f"{ttf_l1_s:.1f}",
          f"{relpath(R18_PROBE_ROWS)}: median(arms.tgms-L1.ttf_ms) over the 5 batches, /1000, s "
          "(== record's own summary.arms.tgms-L1.ttf_p50_ms)")
    m.add("osdiR18TtfGlobalSeconds", f"{ttf_global_s:.1f}",
          f"{relpath(R18_PROBE_ROWS)}: median(arms.global-recompute.ttf_ms) over the 5 "
          "batches, /1000, s (== record's own summary.arms.global-recompute.ttf_p50_ms)")
    m.add("osdiR18Speedup", f"{speedup:.3f}",
          f"{relpath(R18_PROBE_ROWS)}: median(global-recompute ttf_ms) / median(tgms-L1 "
          "ttf_ms) -- P5/P6's speedup convention, <1.0 means L1 is slower here")
    m.add("osdiR18Precision", f"{precision_med * 100:.2f}",
          f"{relpath(R18_PROBE_ROWS)}: median(changed_count / candidate_survivors) over the "
          "5 batches, percent")
    m.add("osdiR18AvoidedRecompute", f"{avoided_decision * 100:.1f}",
          f"{relpath(R18_PROBE_ROWS)}: 1 - sum(arms.tgms-L1.invalidated_count) / "
          "sum(n_registered) over the 5 batches, percent (== record's own summary field)")


# --------------------------------------------------------------------------
# C7 (partial) -- storm-v1 main grid (Lane W2m; addendum-1's 36-cell grid:
# 2 stores x {c1,c3,c4} x {none,deep} x N=1,000 x seeds{0,1,2}, end-to-end
# TTF only; commit 8962b78, pre-D-161-rollout):
# benchmarks/storm-v1/storm-v1-main-grid-2026-09-15.json + -rows.jsonl. All
# 36 cells landed, assembled from a five-job seam across an iTiger
# disk-quota incident (README.md's "storm-v1 main grid" section, seam
# table) -- provenance only, no verdict drawn from which job produced
# which cell. `osdiStormV1*` macros mirror `osdiStormV2*`
# (`compute_c7_storm_v2` below) exactly, on this pre-rollout grid instead
# of the post-rollout rerun, resolving the `osdiStormV1SpeedupN1kSeed0`
# stub that stood in add_pending_stubs below, and additionally landing the
# v1-scoring quantities the C6 pre-registration template (README.md) asked
# for -- the row-touch/entity-touch/window-overlap false-fresh rates and
# the P4 (wall-avoided < decision-avoided) check -- which only the
# per-task per-batch rows packed in storm-v1-records-36-tasks.tar.gz carry
# (the merged grid's own per_cell table has only cell summaries, same
# reason Lane W2l unpacked the v2 tarball for its own survivor-fraction/
# precision pair below). Unlike storm-v2's rows, these rows carry no
# `narrowing_coverage` block, so there is no v1 counterpart to
# `osdiStormV2{AllTopTerms,NonComputeArtifacts}`.
# --------------------------------------------------------------------------

_STORM_V1_STORE_TOKEN = {"synth-iv-60k": "Synth", "collegemsg": "CollegeMsg"}
_STORM_V1_MIX_TOKEN = {"c1": "C1", "c3": "C3", "c4": "C4"}
_STORM_V1_AGE_TOKEN = {None: "None", "deep": "Deep"}


def compute_c7_storm_v1(m: Macros) -> None:
    merged = json.loads(STORM_V1_MAIN_GRID.read_text(encoding="utf-8"))
    rows = load_jsonl(STORM_V1_MAIN_GRID_ROWS)

    eq(merged["record"], relpath(STORM_V1_MAIN_GRID_ROWS),
       f"storm-v1 main grid: {relpath(STORM_V1_MAIN_GRID)}'s own record field names its "
       "rows.jsonl sidecar")
    eq(len(rows), 36, "storm-v1 main grid: rows.jsonl line count")
    eq(merged["total_tasks"], 36, "storm-v1 main grid: merged.total_tasks")
    eq(merged["config"]["n_tasks"], 36, "storm-v1 main grid: merged.config.n_tasks")

    # digest-check #1: result_digest is sha256 of every row's own
    # (_task_id, result_digest), sorted by task id -- storm_campaign_merge.
    # py's own result_digest() function, restated here rather than trusted.
    canon = sorted(
        ({"task_id": r.get("_task_id"), "result_digest": r.get("result_digest")} for r in rows),
        key=lambda x: x["task_id"])
    recomputed_result_digest = hashlib.sha256(
        json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    eq(recomputed_result_digest, merged["result_digest"],
       f"storm-v1 main grid: sha256 over every {relpath(STORM_V1_MAIN_GRID_ROWS)} row's own "
       f"(_task_id, result_digest), sorted by task_id, matches {relpath(STORM_V1_MAIN_GRID)}'s "
       "own result_digest")

    # digest-check #2: dataset.digest is sha256 of the campaign recipe
    # (stores/mixes/ages/n_artifacts_list/n_seeds/base_seed/ttf_modes) --
    # storm_campaign_merge.py's own dataset_digest() function.
    cfg = merged["config"]
    recipe = {"stores": cfg["stores"], "mixes": cfg["mixes"], "ages": cfg["ages"],
              "n_artifacts_list": cfg["n_artifacts_list"], "n_seeds": cfg["n_seeds"],
              "base_seed": cfg["base_seed"], "ttf_modes": cfg["ttf_modes"]}
    recomputed_dataset_digest = hashlib.sha256(
        json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    eq(merged["dataset"]["digest_kind"], "manifest", "storm-v1 main grid: dataset.digest_kind")
    eq(recomputed_dataset_digest, merged["dataset"]["digest"],
       f"storm-v1 main grid: sha256 of {relpath(STORM_V1_MAIN_GRID)}'s own config.{{stores,"
       "mixes,ages,n_artifacts_list,n_seeds,base_seed,ttf_modes}} matches its dataset.digest")

    commits = {r["git_commit"] for r in rows}
    eq(len(commits), 1, "storm-v1 main grid: single git_commit across all 36 cells")
    commit = next(iter(commits))
    eq(commit, merged["git_commit"],
       f"storm-v1 main grid: row git_commit matches {relpath(STORM_V1_MAIN_GRID)}'s own "
       "git_commit")
    eq(commit, "8962b78", "storm-v1 main grid frozen: engine commit (pre-D-161-rollout, the "
       "same anchor the v1 DAG phase and v1 R-18 probe ran)")

    addenda = {r["config"]["addendum_id"] for r in rows}
    eq(addenda, {"storm-v1-addendum-1"},
       "storm-v1 main grid frozen: single addendum_id across all 36 cells")
    freezes = {r["config"]["freeze_sha256"] for r in rows}
    eq(len(freezes), 1, "storm-v1 main grid: single freeze_sha256 across all 36 cells")
    eq(next(iter(freezes)), cfg["freeze_sha256"],
       f"storm-v1 main grid: row config.freeze_sha256 matches {relpath(STORM_V1_MAIN_GRID)}'s "
       "own config.freeze_sha256")
    eq(next(iter(freezes)),
       "49613eb54e9d237c463f16a026fd28df9dc9cf4eb5887904be5706a710b61cb0",
       "storm-v1 main grid frozen: freeze_sha256 (addendum-1)")

    stores = sorted({r["config"]["store"] for r in rows})
    mixes = sorted({r["config"]["mix"] for r in rows})
    ages = sorted({r["config"]["age"] for r in rows}, key=lambda a: (a is None, a))
    seeds = sorted({r["config"]["seed"] for r in rows})
    n_artifacts_vals = {r["config"]["n_artifacts"] for r in rows}
    eq(stores, ["collegemsg", "synth-iv-60k"], "storm-v1 main grid frozen: store axis")
    eq(mixes, ["c1", "c3", "c4"], "storm-v1 main grid frozen: mix axis")
    eq(ages, ["deep", None], "storm-v1 main grid frozen: age axis")
    eq(seeds, [0, 1, 2], "storm-v1 main grid frozen: seed axis")
    eq(n_artifacts_vals, {1000}, "storm-v1 main grid frozen: n_artifacts axis (N=1,000 "
       "throughout)")
    eq(len(stores) * len(mixes) * len(ages) * len(seeds), 36,
       "storm-v1 main grid: 2 stores x 3 mixes x 2 ages x 3 seeds == 36 cells")

    # Cross-check every row's cell-level gate outcome against the merged
    # record's own per_cell entries -- G-S1/G-S2 recomputed straight from
    # each row's own summary.arms/dag blocks, never trusted from the
    # per_cell table or the top-level gates block without this (same
    # discipline as compute_c7_storm_v2 below).
    per_cell_by_id = {c["task_id"]: c for c in merged["per_cell"]}
    eq(set(per_cell_by_id), {r["_task_id"] for r in rows},
       "storm-v1 main grid: merged.per_cell task_ids match rows.jsonl _task_ids")
    failing_cells: list[int] = []
    ff_nonzero = 0
    for r in rows:
        tid = r["_task_id"]
        cell = per_cell_by_id[tid]
        arms = r["summary"]["arms"]
        g_s1 = all(arms.get(arm, {}).get("false_fresh", 0) == 0
                   for arm in ("tgms-L0", "tgms-L1") if arm in arms)
        dag = r.get("dag")
        g_s2 = True if dag is None else dag.get("cascade", {}).get("false_safe_count", 0) == 0
        eq(g_s1, cell["g_s1_false_fresh_zero"],
           f"storm-v1 main grid task {tid}: recomputed G-S1 matches merged.per_cell")
        eq(g_s2, cell["g_s2_false_safe_zero"],
           f"storm-v1 main grid task {tid}: recomputed G-S2 matches merged.per_cell")
        if not (g_s1 and g_s2):
            failing_cells.append(tid)
        for arm in ("tgms-L0", "tgms-L1"):
            if arms[arm]["false_fresh"] > 0:
                ff_nonzero += 1

    eq(failing_cells, [], "storm-v1 main grid: no cell recomputes to fail G-S1 or G-S2")
    eq(merged["gates"]["g_s1_failing_cells"], [],
       "storm-v1 main grid: merged.gates.g_s1_failing_cells is empty")
    eq(merged["gates"]["g_s2_failing_cells"], [],
       "storm-v1 main grid: merged.gates.g_s2_failing_cells is empty")
    eq(ff_nonzero, 0, "storm-v1 main grid frozen: (cell, arm) pairs among tgms-L0/tgms-L1 "
       "over all 36 cells (72 total) with false_fresh > 0")

    def _speedup(r: dict) -> float:
        arms = r["summary"]["arms"]
        return arms["global-recompute"]["ttf_p50_ms"] / arms["tgms-L1"]["ttf_p50_ms"]

    all_speedups = [_speedup(r) for r in rows]
    grid_min, grid_max = min(all_speedups), max(all_speedups)
    close(grid_min, 1.742, 0.001,
          "storm-v1 main grid frozen: minimum per-cell speedup over all 36 cells")
    close(grid_max, 2.558, 0.001,
          "storm-v1 main grid frozen: maximum per-cell speedup over all 36 cells")

    target = [r for r in rows if r["config"]["store"] == "synth-iv-60k"
              and r["config"]["mix"] == "c1" and r["config"]["age"] is None
              and r["config"]["seed"] == 0]
    eq(len(target), 1, "storm-v1 main grid: exactly one cell at (synth-iv-60k, c1, age none, "
       "seed 0)")
    n1k_speedup = _speedup(target[0])
    close(n1k_speedup, 1.938, 0.001, "storm-v1 main grid frozen: N=1,000 speedup at "
          "synth-iv-60k/c1/age-none/seed-0 (global-recompute ttf_p50_ms / tgms-L1 ttf_p50_ms)")

    groups: dict[tuple[str, str, str | None], list[dict]] = {}
    for r in rows:
        cfg_r = r["config"]
        groups.setdefault((cfg_r["store"], cfg_r["mix"], cfg_r["age"]), []).append(r)
    eq(len(groups), 12, "storm-v1 main grid: 12 distinct (store, mix, age) groups")

    frozen_group_medians = {
        ("synth-iv-60k", "c1", None): 1.895, ("synth-iv-60k", "c1", "deep"): 1.908,
        ("synth-iv-60k", "c3", None): 1.933, ("synth-iv-60k", "c3", "deep"): 1.939,
        ("synth-iv-60k", "c4", None): 1.788, ("synth-iv-60k", "c4", "deep"): 1.756,
        ("collegemsg", "c1", None): 2.333, ("collegemsg", "c1", "deep"): 2.304,
        ("collegemsg", "c3", None): 2.382, ("collegemsg", "c3", "deep"): 2.427,
        ("collegemsg", "c4", None): 2.325, ("collegemsg", "c4", "deep"): 2.151,
    }
    eq(set(groups), set(frozen_group_medians), "storm-v1 main grid: (store, mix, age) group "
       "keys match the frozen table")

    per_group_median: dict[tuple[str, str, str | None], float] = {}
    for key, grp in groups.items():
        eq(sorted(g["config"]["seed"] for g in grp), [0, 1, 2],
           f"storm-v1 main grid: group {key} covers seeds 0/1/2 exactly")
        med = statistics.median(_speedup(g) for g in grp)
        close(med, frozen_group_medians[key], 0.001,
              f"storm-v1 main grid frozen: median speedup for {key}")
        per_group_median[key] = med

    # avoided-recomputation (decision) medians by mix, tgms-L1 -- median
    # over each mix's 12 cells of the cell's own summary.arms.tgms-L1.
    # avoided_recompute_decision (same per-cell aggregate compute_c7_storm_v2
    # uses for its c1-only osdiStormV2AvoidedDecisionC1Median, generalized
    # here to all three mixes per the C6 template's own per-mix prediction).
    avoided_by_mix: dict[str, float] = {}
    for mix in ("c1", "c3", "c4"):
        mix_rows = [r for r in rows if r["config"]["mix"] == mix]
        eq(len(mix_rows), 12, f"storm-v1 main grid: {mix}-mix cell count (2 stores x 2 ages "
           "x 3 seeds)")
        vals = [r["summary"]["arms"]["tgms-L1"]["avoided_recompute_decision"] for r in mix_rows]
        avoided_by_mix[mix] = statistics.median(vals)
    close(avoided_by_mix["c1"], 0.309, 0.001, "storm-v1 main grid frozen: median "
          "summary.arms.tgms-L1.avoided_recompute_decision over the 12 c1-mix cells")
    close(avoided_by_mix["c3"], 0.312, 0.001, "storm-v1 main grid frozen: median "
          "summary.arms.tgms-L1.avoided_recompute_decision over the 12 c3-mix cells")
    close(avoided_by_mix["c4"], 0.312, 0.001, "storm-v1 main grid frozen: median "
          "summary.arms.tgms-L1.avoided_recompute_decision over the 12 c4-mix cells")

    # P4 ("avoided recomputation (wall clock) < the decision count" --
    # README.md's C6 pre-registration table): per cell, tgms-L1's
    # avoided_recompute_wall must be strictly less than its
    # avoided_recompute_decision (check cost is O(prefix), paid regardless
    # of how many artifacts turn out invalidated). Counted, not asserted,
    # since the C6 template records this as a prediction to score, not a
    # gate -- the frozen count below is the coordinator's own scoring of
    # it, restated here as an assertion the same way every other frozen
    # number in this module is.
    p4_violations = 0
    for r in rows:
        a = r["summary"]["arms"]["tgms-L1"]
        if not (a["avoided_recompute_wall"] < a["avoided_recompute_decision"]):
            p4_violations += 1
    eq(p4_violations, 0, "storm-v1 main grid frozen: P4 violations (cells where tgms-L1's "
       "avoided_recompute_wall is not < avoided_recompute_decision), of 36")

    # --- v1-scoring quantities from the per-task per-batch rows packed in
    # storm-v1-records-36-tasks.tar.gz (never committed as individual
    # files -- see the module docstring's C7 section). sha256-checked
    # before anything inside it is trusted, and that frozen constant is
    # itself cross-checked against README.md's own quoted value, not just
    # asserted from a first read (same discipline as the v2 tarball read
    # in compute_c7_storm_v2 below).
    readme_text = STORM_V1_README.read_text(encoding="utf-8")
    readme_sha_match = re.search(
        r"storm-v1-records-36-tasks\.tar\.gz`\s*\(sha256\s*\n?`([0-9a-f]{64})`\)", readme_text)
    require(readme_sha_match is not None,
            f"storm-v1 records tarball: {relpath(STORM_V1_README)} names a sha256 for "
            "storm-v1-records-36-tasks.tar.gz in its storm-v1 main grid section")
    if readme_sha_match is not None:
        eq(readme_sha_match.group(1), STORM_V1_RECORDS_TARBALL_SHA256,
           "storm-v1 records tarball: frozen sha256 constant matches "
           f"{relpath(STORM_V1_README)}'s own quoted value")
    eq(sha256_file(STORM_V1_RECORDS_TARBALL), STORM_V1_RECORDS_TARBALL_SHA256,
       f"{relpath(STORM_V1_RECORDS_TARBALL)}: sha256 matches the frozen/README-quoted value")

    # Per cell: Sum(false_fresh_count) / Sum(changed_count) over that
    # cell's own 20 batches, for each of the three coarse baselines
    # (row-touch/entity-touch/window-overlap) -- the freeze's own
    # denominator. The reported quantity is the median of that per-cell
    # ratio over the 36 cells; separately, the window-overlap
    # "nonzero-batch" count and the new-identity-placement row-touch
    # ratios are raw per-batch quantities pooled over all 720 batches
    # (36 cells x 20 batches), not per-cell aggregates -- two different
    # granularities read from the same 720 rows, both restated here rather
    # than trusted from prose.
    per_cell_row_touch_ratio: list[float] = []
    per_cell_entity_touch_ratio: list[float] = []
    per_cell_window_overlap_ratio: list[float] = []
    window_overlap_nonzero_batches = 0
    total_batches = 0
    new_identity_row_touch_ratios: list[float] = []

    with tarfile.open(STORM_V1_RECORDS_TARBALL, "r:gz") as tf:
        tar_names = set(tf.getnames())
        for r in rows:
            tid = r["_task_id"]
            idx = r["record"].index("records/")
            member = r["record"][idx:]
            require(member in tar_names,
                    f"storm-v1 records tarball: task {tid}'s own record field names a member "
                    f"({member}) present in the tarball")
            raw = tf.extractfile(member).read().decode("utf-8")
            batch_rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
            eq(len(batch_rows), r["config"]["batches"],
               f"storm-v1 records tarball task {tid}: batch row count matches "
               "config.batches")

            cell_changed = 0
            cell_row_ff = 0
            cell_entity_ff = 0
            cell_wo_ff = 0
            for br in batch_rows:
                total_batches += 1
                changed_count = br["changed_count"]
                row_ff = br["arms"]["row-touch"]["false_fresh_count"]
                ent_ff = br["arms"]["entity-touch"]["false_fresh_count"]
                wo_ff = br["arms"]["window-overlap"]["false_fresh_count"]
                cell_changed += changed_count
                cell_row_ff += row_ff
                cell_entity_ff += ent_ff
                cell_wo_ff += wo_ff
                if wo_ff > 0:
                    window_overlap_nonzero_batches += 1
                if br["correction_placement"] == "new-identity" and changed_count > 0:
                    new_identity_row_touch_ratios.append(row_ff / changed_count)

            require(cell_changed > 0,
                    f"storm-v1 records tarball task {tid}: at least one changed artifact "
                    "across its 20 batches")
            per_cell_row_touch_ratio.append(cell_row_ff / cell_changed)
            per_cell_entity_touch_ratio.append(cell_entity_ff / cell_changed)
            per_cell_window_overlap_ratio.append(cell_wo_ff / cell_changed)

    eq(total_batches, 720, "storm-v1 records tarball: total per-batch rows over all 36 cells "
       "(36 cells x 20 batches)")

    row_touch_median = statistics.median(per_cell_row_touch_ratio)
    entity_touch_median = statistics.median(per_cell_entity_touch_ratio)
    window_overlap_median = statistics.median(per_cell_window_overlap_ratio)
    close(row_touch_median, 1.000, 0.001, "storm-v1 main grid frozen: median per-cell "
          "Sum(row-touch false_fresh_count) / Sum(changed_count) over the 36 cells")
    close(entity_touch_median, 0.993, 0.001, "storm-v1 main grid frozen: median per-cell "
          "Sum(entity-touch false_fresh_count) / Sum(changed_count) over the 36 cells")
    close(window_overlap_median, 0.189, 0.001, "storm-v1 main grid frozen: median per-cell "
          "Sum(window-overlap false_fresh_count) / Sum(changed_count) over the 36 cells")
    eq(window_overlap_nonzero_batches, 258, "storm-v1 main grid frozen: batches (of 720) with "
       "window-overlap false_fresh_count > 0")

    eq(len(new_identity_row_touch_ratios), 195, "storm-v1 main grid frozen: batches (of 720) "
       "whose correction_placement is new-identity")
    new_identity_row_touch_median = statistics.median(new_identity_row_touch_ratios)
    close(new_identity_row_touch_median, 1.000, 0.001, "storm-v1 main grid frozen: median "
          "row-touch false_fresh_count / changed_count over the 195 new-identity batches")

    m.add("osdiStormV1Commit", commit,
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: git_commit, uniform over all 36 cells (== "
          f"{relpath(STORM_V1_MAIN_GRID)}'s own git_commit)")
    m.add("osdiStormV1Cells", len(rows),
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: line count (== "
          f"{relpath(STORM_V1_MAIN_GRID)}'s own total_tasks/config.n_tasks)")
    m.add("osdiStormV1CellsFailed", len(failing_cells),
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: cells recomputed to fail G-S1 or G-S2, of 36 "
          f"(== union of {relpath(STORM_V1_MAIN_GRID)}'s own gates.g_s{{1,2}}_failing_cells)")
    m.add("osdiStormV1FalseFreshTgmsCellsNonzero", ff_nonzero,
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: count of (cell, arm) pairs among tgms-L0/"
          "tgms-L1 over all 36 cells (72 total) with summary.arms[arm].false_fresh > 0")
    m.add("osdiStormV1SpeedupN1kSeed0", f"{n1k_speedup:.3f}",
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: summary.arms.{{global-recompute,tgms-L1}}."
          "ttf_p50_ms ratio at (store=synth-iv-60k, mix=c1, age=none, seed=0, "
          "n_artifacts=1000)")

    for key, med in per_group_median.items():
        store, mix, age = key
        name = (f"osdiStormV1Speedup{_STORM_V1_STORE_TOKEN[store]}{_STORM_V1_MIX_TOKEN[mix]}"
                f"{_STORM_V1_AGE_TOKEN[age]}")
        m.add(name, f"{med:.3f}",
              f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: median over seeds 0/1/2 of "
              "summary.arms.{global-recompute,tgms-L1}.ttf_p50_ms ratio at "
              f"(store={store}, mix={mix}, age={age or 'none'}, n_artifacts=1000)")

    m.add("osdiStormV1SpeedupGridMin", f"{grid_min:.3f}",
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: minimum per-cell "
          "summary.arms.{global-recompute,tgms-L1}.ttf_p50_ms ratio over all 36 cells")
    m.add("osdiStormV1SpeedupGridMax", f"{grid_max:.3f}",
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: maximum per-cell "
          "summary.arms.{global-recompute,tgms-L1}.ttf_p50_ms ratio over all 36 cells")

    m.add("osdiStormV1AvoidedDecisionC1Median", f"{avoided_by_mix['c1']:.3f}",
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: median(summary.arms.tgms-L1."
          "avoided_recompute_decision) over the 12 c1-mix cells")
    m.add("osdiStormV1AvoidedDecisionC3Median", f"{avoided_by_mix['c3']:.3f}",
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: median(summary.arms.tgms-L1."
          "avoided_recompute_decision) over the 12 c3-mix cells")
    m.add("osdiStormV1AvoidedDecisionC4Median", f"{avoided_by_mix['c4']:.3f}",
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: median(summary.arms.tgms-L1."
          "avoided_recompute_decision) over the 12 c4-mix cells")

    m.add("osdiStormV1Batches", tex_num(total_batches),
          f"{relpath(STORM_V1_RECORDS_TARBALL)}: total per-batch rows over all 36 cells "
          "(36 cells x 20 batches)")
    m.add("osdiStormV1RowTouchFalseFreshMedian", f"{row_touch_median:.3f}",
          f"{relpath(STORM_V1_RECORDS_TARBALL)}: median over the 36 cells of Sum(row-touch "
          "arms.false_fresh_count) / Sum(changed_count), each summed over that cell's own "
          "20 batches")
    m.add("osdiStormV1EntityTouchFalseFreshMedian", f"{entity_touch_median:.3f}",
          f"{relpath(STORM_V1_RECORDS_TARBALL)}: median over the 36 cells of Sum(entity-touch "
          "arms.false_fresh_count) / Sum(changed_count), each summed over that cell's own "
          "20 batches")
    m.add("osdiStormV1WindowOverlapFalseFreshMedian", f"{window_overlap_median:.3f}",
          f"{relpath(STORM_V1_RECORDS_TARBALL)}: median over the 36 cells of "
          "Sum(window-overlap arms.false_fresh_count) / Sum(changed_count), each summed over "
          "that cell's own 20 batches")
    m.add("osdiStormV1WindowOverlapNonzeroBatches", tex_num(window_overlap_nonzero_batches),
          f"{relpath(STORM_V1_RECORDS_TARBALL)}: batches (of 720) with window-overlap "
          "arms.false_fresh_count > 0")
    m.add("osdiStormV1NewIdentityBatches", tex_num(len(new_identity_row_touch_ratios)),
          f"{relpath(STORM_V1_RECORDS_TARBALL)}: batches (of 720) whose correction_placement "
          "is new-identity")
    m.add("osdiStormV1NewIdentityRowTouchMedian", f"{new_identity_row_touch_median:.3f}",
          f"{relpath(STORM_V1_RECORDS_TARBALL)}: median row-touch arms.false_fresh_count / "
          "changed_count over the 195 new-identity batches")
    m.add("osdiStormV1P4ViolationCells", tex_num(p4_violations),
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: cells (of 36) where summary.arms.tgms-L1."
          "avoided_recompute_wall is not < avoided_recompute_decision (P4: check cost is "
          "O(prefix), paid regardless)")


# --------------------------------------------------------------------------
# C7 (partial) -- storm-v2 R-18 probe (Lane W2m; job 212295, addendum-3,
# post-D-161-rollout): benchmarks/storm-v1/storm-v2-r18-probe-2026-09-15.json
# + -rows.jsonl. Same cell as the v1 R-18 probe above (compute_c7_r18: store
# synth-iv-60k, mix c1, N=10,000, seed 0, 5 batches, sum TTF) -- see
# README.md's "R-18 probe v2 (addendum-3) -- run of record, v1 vs v2
# comparison" section for the full side-by-side table. `osdiR18*` above
# stay untouched; `osdiStormV2Probe*` land beside them here, and beside
# `osdiStormV2*` (compute_c7_storm_v2, the different 36-cell main-grid
# record) without resolving or comparing across either -- different
# record, different macro namespace, no verdict macro (the paper's own
# P5/P6/P7 scoring lives in the internal freeze, same as compute_c7_r18).
# --------------------------------------------------------------------------

def compute_c7_storm_v2_probe(m: Macros) -> None:
    d = json.loads(STORM_V2_R18_PROBE.read_text(encoding="utf-8"))
    rows = load_jsonl(STORM_V2_R18_PROBE_ROWS)
    rows.sort(key=lambda r: r["batch_index"])

    eq(d["config"]["n_artifacts"], 10000, "storm-v2 R18 probe frozen: probe artifact count")
    eq(d["config"]["batches"], 5, "storm-v2 R18 probe frozen: batch count")
    eq(len(rows), d["config"]["batches"],
       "storm-v2 R18 probe: rows.jsonl line count matches config.batches")
    eq(d["config"]["wall_capped"], False, "storm-v2 R18 probe: probe was not wall-capped")
    eq(d["config"]["mix"], "c1", "storm-v2 R18 probe: this probe is the c1 mix (same cell as "
       "the v1 R-18 probe above)")
    eq(d["config"]["addendum_id"], "storm-v1-addendum-3",
       "storm-v2 R18 probe frozen: addendum_id (post-D-161-rollout)")

    commit = d["git_commit"]
    eq(commit, "fdd393c", "storm-v2 R18 probe frozen: engine commit (the D-161-rolled-out "
       "build, same as the storm-v2 main grid)")

    # digest-check: result_digest is sha256 of the canonical-JSON list of
    # every row (sorted by batch_index), per bench_correction_storm.py's
    # own `"result_digest": _sha256_json(rows_json)` (canonical_json ==
    # json.dumps(..., sort_keys=True, separators=(",", ":"))) -- restated
    # here rather than trusted from the record's own field.
    recomputed_result_digest = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    eq(recomputed_result_digest, d["result_digest"],
       f"storm-v2 R18 probe: sha256 over every {relpath(STORM_V2_R18_PROBE_ROWS)} row "
       f"(sorted by batch_index) matches {relpath(STORM_V2_R18_PROBE)}'s own result_digest")

    n_registered_vals = {r["n_registered"] for r in rows}
    require(len(n_registered_vals) == 1,
            "storm-v2 R18 probe: n_registered constant across batches")
    n_registered = next(iter(n_registered_vals))
    eq(n_registered, d["config"]["n_registered"],
       "storm-v2 R18 probe: recomputed n_registered matches config")

    intersects = [r["intersects_calls"] for r in rows]
    survivors = [r["candidate_survivors"] for r in rows]
    changed = [r["changed_count"] for r in rows]

    intersects_med = statistics.median(intersects)
    eq(intersects_med, 29193, "storm-v2 R18 probe frozen: median intersects_calls/batch")
    r18_tripped = intersects_med > 50000
    eq(r18_tripped, False,
       "storm-v2 R18 probe: median intersects_calls/batch does not exceed the R-18 trip "
       "threshold (same non-trip as the v1 probe, at roughly double the raw call count)")

    survivor_fraction_per_batch = [s / n_registered for s in survivors]
    survivor_median = statistics.median(survivor_fraction_per_batch)
    close(survivor_median, 0.283, 0.001,
          "storm-v2 R18 probe frozen: median candidate_survivors / n_registered")

    precision_per_batch = [c / s for c, s in zip(changed, survivors)]
    precision_median = statistics.median(precision_per_batch)
    close(precision_median, 0.211, 0.001,
          "storm-v2 R18 probe frozen: median(changed_count / candidate_survivors) over the "
          "5 batches")

    l1_check_ms = [r["arms"]["tgms-L1"]["check_wall_ms"] for r in rows]
    l1_ttf_ms = [r["arms"]["tgms-L1"]["ttf_ms"] for r in rows]
    global_ttf_ms = [r["arms"]["global-recompute"]["ttf_ms"] for r in rows]
    l1_invalidated = [r["arms"]["tgms-L1"]["invalidated_count"] for r in rows]

    l1_check_med = statistics.median(l1_check_ms)
    l1_ttf_med = statistics.median(l1_ttf_ms)
    global_ttf_med = statistics.median(global_ttf_ms)

    # Cross-check every recomputed per-batch median against the record's
    # own aggregate summary.arms block -- never trusted without this (same
    # discipline as compute_c7_r18 above).
    eq(l1_ttf_med, d["summary"]["arms"]["tgms-L1"]["ttf_p50_ms"],
       "storm-v2 R18 probe: recomputed tgms-L1 ttf p50 (median of per-batch ttf_ms) matches "
       "the record's own summary.arms field")
    eq(global_ttf_med, d["summary"]["arms"]["global-recompute"]["ttf_p50_ms"],
       "storm-v2 R18 probe: recomputed global-recompute ttf p50 matches the record's own "
       "summary.arms field")

    avoided_decision = 1 - sum(l1_invalidated) / (n_registered * len(rows))
    eq(avoided_decision, d["summary"]["arms"]["tgms-L1"]["avoided_recompute_decision"],
       "storm-v2 R18 probe: recomputed avoided_recompute_decision (1 - sum(invalidated) / "
       "sum(n_registered)) matches the record's own summary.arms field")
    close(avoided_decision, 0.758, 0.001,
          "storm-v2 R18 probe frozen: tgms-L1 avoided_recompute_decision")

    # "Speedup of L1 over global-recompute" == baseline/candidate ==
    # global/L1, per campaign.yaml's P5/P6 convention (>1.0 means L1 is
    # faster) -- same convention as compute_c7_r18's osdiR18Speedup, but
    # this probe measures speedup > 1.0 (L1 faster here, unlike the v1
    # probe): a genuine, asserted result, no verdict drawn on it here.
    speedup = global_ttf_med / l1_ttf_med
    close(speedup, 1.946, 0.001, "storm-v2 R18 probe frozen: speedup of tgms-L1 over "
          "global-recompute (global_ttf_median / l1_ttf_median)")

    nc = d["summary"]["narrowing_coverage"]
    eq(nc["n_all_top_term"], 0,
       "storm-v2 R18 probe frozen: summary.narrowing_coverage.n_all_top_term")
    non_compute_artifacts = nc["n_artifacts"] - nc["n_empty_scope"]
    eq(non_compute_artifacts, 8203,
       "storm-v2 R18 probe frozen: n_artifacts - n_empty_scope")

    check_wall_median_s = l1_check_med / 1000
    close(check_wall_median_s, 336.9, 0.05,
          "storm-v2 R18 probe frozen: median arms.tgms-L1.check_wall_ms over the 5 batches, "
          "seconds (README.md's own quoted field)")

    wall_s = d["config"]["wall_s"]

    m.add("osdiStormV2ProbeCommit", commit,
          f"{relpath(STORM_V2_R18_PROBE)}: git_commit")
    m.add("osdiStormV2ProbeBatches", tex_num(d["config"]["batches"]),
          f"{relpath(STORM_V2_R18_PROBE)}: config.batches")
    m.add("osdiStormV2ProbeWallS", f"{wall_s:,.1f}".replace(",", "{,}"),
          f"{relpath(STORM_V2_R18_PROBE)}: config.wall_s")
    m.add("osdiStormV2ProbeGlobalTtfS", f"{global_ttf_med / 1000:,.1f}".replace(",", "{,}"),
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(arms.global-recompute.ttf_ms) over "
          "the 5 batches, /1000, s (== record's own summary.arms.global-recompute."
          "ttf_p50_ms)")
    m.add("osdiStormV2ProbeTgmsL1TtfS", f"{l1_ttf_med / 1000:.1f}",
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(arms.tgms-L1.ttf_ms) over the 5 "
          "batches, /1000, s (== record's own summary.arms.tgms-L1.ttf_p50_ms)")
    m.add("osdiStormV2ProbeSpeedupN10k", f"{speedup:.3f}",
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(global-recompute ttf_ms) / "
          "median(tgms-L1 ttf_ms) -- P5/P6's speedup convention, at N=10,000")
    m.add("osdiStormV2ProbeAvoidedDecision", f"{avoided_decision:.3f}",
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: summary.arms.tgms-L1."
          "avoided_recompute_decision (== 1 - sum(invalidated_count)/sum(n_registered) "
          "over the 5 batches)")
    m.add("osdiStormV2ProbeSurvivorMedian", f"{survivor_median:.3f}",
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(candidate_survivors / n_registered) "
          "over the 5 batches")
    m.add("osdiStormV2ProbePrecisionMedian", f"{precision_median:.3f}",
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(changed_count / candidate_survivors) "
          "over the 5 batches")
    m.add("osdiStormV2ProbeIntersectsMedian", tex_num(int(intersects_med)),
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(intersects_calls) over the 5 batches")
    m.add("osdiStormV2ProbeR18Tripped", "yes" if r18_tripped else "no",
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(intersects_calls) > 50,000 (the R-18 "
          "trip threshold; arithmetic fact only, no verdict)")
    m.add("osdiStormV2ProbeAllTopTerms", nc["n_all_top_term"],
          f"{relpath(STORM_V2_R18_PROBE)}: summary.narrowing_coverage.n_all_top_term")
    m.add("osdiStormV2ProbeNonComputeArtifacts", tex_num(non_compute_artifacts),
          f"{relpath(STORM_V2_R18_PROBE)}: summary.narrowing_coverage.n_artifacts - "
          "n_empty_scope")
    m.add("osdiStormV2ProbeCheckWallMedianS", f"{check_wall_median_s:.1f}",
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(arms.tgms-L1.check_wall_ms) over the "
          "5 batches, /1000, s (README.md's own quoted field)")


# --------------------------------------------------------------------------
# C7 (partial) -- storm-v2 main grid (addendum-3, post-D-161-rollout rerun
# of the same 36-cell (store x mix x age x seed) recipe addendum-1 never
# finished): benchmarks/storm-v1/storm-v2-main-grid-2026-09-15.json +
# -rows.jsonl. Each rows.jsonl line is one task's own per-task manifest
# embedded verbatim (storm_campaign_merge.py's own "why per-task manifests
# are embedded whole" module note) -- config/summary/dag, never the raw
# per-batch rows (those stay on iTiger's stage directory). `osdiStormV2*`
# macros stand beside `osdiStormV1*` (compute_c7_storm_v1 above) without
# resolving or overwriting them -- different grid, different commit, no
# v1-vs-v2 comparison macro here. (The addendum-1 dead-end's own legacy
# names -- `osdiStormCells` et al. -- were retired; see the module
# docstring's C7 section.)
# --------------------------------------------------------------------------

_STORM_V2_STORE_TOKEN = {"synth-iv-60k": "Synth", "collegemsg": "CollegeMsg"}
_STORM_V2_MIX_TOKEN = {"c1": "C1", "c3": "C3", "c4": "C4"}
_STORM_V2_AGE_TOKEN = {None: "None", "deep": "Deep"}


def compute_c7_storm_v2(m: Macros) -> None:
    merged = json.loads(STORM_V2_MAIN_GRID.read_text(encoding="utf-8"))
    rows = load_jsonl(STORM_V2_MAIN_GRID_ROWS)

    eq(merged["record"], relpath(STORM_V2_MAIN_GRID_ROWS),
       f"storm-v2 main grid: {relpath(STORM_V2_MAIN_GRID)}'s own record field names its "
       "rows.jsonl sidecar")
    eq(len(rows), 36, "storm-v2 main grid: rows.jsonl line count")
    eq(merged["total_tasks"], 36, "storm-v2 main grid: merged.total_tasks")
    eq(merged["config"]["n_tasks"], 36, "storm-v2 main grid: merged.config.n_tasks")

    # digest-check #1: result_digest is sha256 of every row's own
    # (_task_id, result_digest), sorted by task id -- storm_campaign_merge.
    # py's own result_digest() function, restated here rather than trusted.
    canon = sorted(
        ({"task_id": r.get("_task_id"), "result_digest": r.get("result_digest")} for r in rows),
        key=lambda x: x["task_id"])
    recomputed_result_digest = hashlib.sha256(
        json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    eq(recomputed_result_digest, merged["result_digest"],
       f"storm-v2 main grid: sha256 over every {relpath(STORM_V2_MAIN_GRID_ROWS)} row's own "
       f"(_task_id, result_digest), sorted by task_id, matches {relpath(STORM_V2_MAIN_GRID)}'s "
       "own result_digest")

    # digest-check #2: dataset.digest is sha256 of the campaign recipe
    # (stores/mixes/ages/n_artifacts_list/n_seeds/base_seed/ttf_modes) --
    # storm_campaign_merge.py's own dataset_digest() function.
    cfg = merged["config"]
    recipe = {"stores": cfg["stores"], "mixes": cfg["mixes"], "ages": cfg["ages"],
              "n_artifacts_list": cfg["n_artifacts_list"], "n_seeds": cfg["n_seeds"],
              "base_seed": cfg["base_seed"], "ttf_modes": cfg["ttf_modes"]}
    recomputed_dataset_digest = hashlib.sha256(
        json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    eq(merged["dataset"]["digest_kind"], "manifest", "storm-v2 main grid: dataset.digest_kind")
    eq(recomputed_dataset_digest, merged["dataset"]["digest"],
       f"storm-v2 main grid: sha256 of {relpath(STORM_V2_MAIN_GRID)}'s own config.{{stores,"
       "mixes,ages,n_artifacts_list,n_seeds,base_seed,ttf_modes}} matches its dataset.digest")

    commits = {r["git_commit"] for r in rows}
    eq(len(commits), 1, "storm-v2 main grid: single git_commit across all 36 cells")
    commit = next(iter(commits))
    eq(commit, merged["git_commit"],
       f"storm-v2 main grid: row git_commit matches {relpath(STORM_V2_MAIN_GRID)}'s own "
       "git_commit")
    eq(commit, "fdd393c", "storm-v2 main grid frozen: engine commit (the D-161-rolled-out "
       "build, same as DAG v3)")

    addenda = {r["config"]["addendum_id"] for r in rows}
    eq(addenda, {"storm-v1-addendum-3"},
       "storm-v2 main grid frozen: single addendum_id across all 36 cells")
    freezes = {r["config"]["freeze_sha256"] for r in rows}
    eq(len(freezes), 1, "storm-v2 main grid: single freeze_sha256 across all 36 cells")
    eq(next(iter(freezes)), cfg["freeze_sha256"],
       f"storm-v2 main grid: row config.freeze_sha256 matches {relpath(STORM_V2_MAIN_GRID)}'s "
       "own config.freeze_sha256")

    stores = sorted({r["config"]["store"] for r in rows})
    mixes = sorted({r["config"]["mix"] for r in rows})
    ages = sorted({r["config"]["age"] for r in rows}, key=lambda a: (a is None, a))
    seeds = sorted({r["config"]["seed"] for r in rows})
    n_artifacts_vals = {r["config"]["n_artifacts"] for r in rows}
    eq(stores, ["collegemsg", "synth-iv-60k"], "storm-v2 main grid frozen: store axis")
    eq(mixes, ["c1", "c3", "c4"], "storm-v2 main grid frozen: mix axis")
    eq(ages, ["deep", None], "storm-v2 main grid frozen: age axis")
    eq(seeds, [0, 1, 2], "storm-v2 main grid frozen: seed axis")
    eq(n_artifacts_vals, {1000}, "storm-v2 main grid frozen: n_artifacts axis (N=1,000 "
       "throughout)")
    eq(len(stores) * len(mixes) * len(ages) * len(seeds), 36,
       "storm-v2 main grid: 2 stores x 3 mixes x 2 ages x 3 seeds == 36 cells")

    # Cross-check every row's cell-level gate outcome against the merged
    # record's own per_cell entries -- G-S1/G-S2 recomputed straight from
    # each row's own summary.arms/dag blocks (storm_campaign_merge.py's
    # per_cell_table() logic, restated here), never trusted from the
    # per_cell table or the top-level gates block without this.
    per_cell_by_id = {c["task_id"]: c for c in merged["per_cell"]}
    eq(set(per_cell_by_id), {r["_task_id"] for r in rows},
       "storm-v2 main grid: merged.per_cell task_ids match rows.jsonl _task_ids")
    failing_cells: list[int] = []
    ff_nonzero = 0
    all_top_term_total = 0
    non_compute_artifacts = 0
    for r in rows:
        tid = r["_task_id"]
        cell = per_cell_by_id[tid]
        arms = r["summary"]["arms"]
        g_s1 = all(arms.get(arm, {}).get("false_fresh", 0) == 0
                   for arm in ("tgms-L0", "tgms-L1") if arm in arms)
        dag = r.get("dag")
        g_s2 = True if dag is None else dag.get("cascade", {}).get("false_safe_count", 0) == 0
        eq(g_s1, cell["g_s1_false_fresh_zero"],
           f"storm-v2 main grid task {tid}: recomputed G-S1 matches merged.per_cell")
        eq(g_s2, cell["g_s2_false_safe_zero"],
           f"storm-v2 main grid task {tid}: recomputed G-S2 matches merged.per_cell")
        if not (g_s1 and g_s2):
            failing_cells.append(tid)
        for arm in ("tgms-L0", "tgms-L1"):
            if arms[arm]["false_fresh"] > 0:
                ff_nonzero += 1
        nc = r["summary"]["narrowing_coverage"]
        all_top_term_total += nc["n_all_top_term"]
        non_compute_artifacts += nc["n_artifacts"] - nc["n_empty_scope"]

    eq(failing_cells, [], "storm-v2 main grid: no cell recomputes to fail G-S1 or G-S2")
    eq(merged["gates"]["g_s1_failing_cells"], [],
       "storm-v2 main grid: merged.gates.g_s1_failing_cells is empty")
    eq(merged["gates"]["g_s2_failing_cells"], [],
       "storm-v2 main grid: merged.gates.g_s2_failing_cells is empty")
    eq(ff_nonzero, 0, "storm-v2 main grid frozen: (cell, arm) pairs among tgms-L0/tgms-L1 "
       "over all 36 cells (72 total) with false_fresh > 0")
    eq(all_top_term_total, 0, "storm-v2 main grid frozen: sum of "
       "summary.narrowing_coverage.n_all_top_term over all 36 cells")

    c1_rows = [r for r in rows if r["config"]["mix"] == "c1"]
    eq(len(c1_rows), 12, "storm-v2 main grid: c1-mix cell count (2 stores x 2 ages x 3 seeds)")
    c1_avoided = [r["summary"]["arms"]["tgms-L1"]["avoided_recompute_decision"]
                  for r in c1_rows]
    avoided_median = statistics.median(c1_avoided)
    close(avoided_median, 0.7547, 0.001, "storm-v2 main grid frozen: median "
          "summary.arms.tgms-L1.avoided_recompute_decision over the 12 c1-mix cells")

    # --- Lane W2l: c1-mix survivor-fraction/precision pair, from the
    # per-task per-batch rows packed in storm-v2-records-36-tasks.tar.gz
    # (never committed as individual files -- see the module docstring's
    # C7 section). sha256-checked before anything inside it is trusted,
    # and that frozen constant is itself cross-checked against README.md's
    # own quoted value, not just asserted from a first read.
    readme_text = STORM_V1_README.read_text(encoding="utf-8")
    readme_sha_match = re.search(
        r"storm-v2-records-36-tasks\.tar\.gz`\s*\n\(sha256 `([0-9a-f]{64})`\)", readme_text)
    require(readme_sha_match is not None,
            f"storm-v2 records tarball: {relpath(STORM_V1_README)} names a sha256 for "
            "storm-v2-records-36-tasks.tar.gz in its Per-batch rows section")
    if readme_sha_match is not None:
        eq(readme_sha_match.group(1), STORM_V2_RECORDS_TARBALL_SHA256,
           "storm-v2 records tarball: frozen sha256 constant matches "
           f"{relpath(STORM_V1_README)}'s own quoted value")
    eq(sha256_file(STORM_V2_RECORDS_TARBALL), STORM_V2_RECORDS_TARBALL_SHA256,
       f"{relpath(STORM_V2_RECORDS_TARBALL)}: sha256 matches the frozen/README-quoted value")

    survivor_fracs: list[float] = []
    precisions: list[float] = []
    per_store_survivor: dict[str, list[float]] = {"synth-iv-60k": [], "collegemsg": []}
    per_store_precision: dict[str, list[float]] = {"synth-iv-60k": [], "collegemsg": []}
    with tarfile.open(STORM_V2_RECORDS_TARBALL, "r:gz") as tf:
        tar_names = set(tf.getnames())
        for r in c1_rows:
            tid = r["_task_id"]
            # locate this cell's per-batch rows.jsonl inside the tarball via
            # the merged grid's own `record` field (its provenance path
            # ends in the same records/task-<N>/...-rows.jsonl the tarball
            # holds), never by assuming task-<N> == _task_id
            idx = r["record"].index("records/")
            member = r["record"][idx:]
            require(member in tar_names,
                    f"storm-v2 records tarball: task {tid}'s own record field names a member "
                    f"({member}) present in the tarball")
            raw = tf.extractfile(member).read().decode("utf-8")
            batch_rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
            eq(len(batch_rows), r["config"]["batches"],
               f"storm-v2 records tarball task {tid}: batch row count matches "
               "config.batches")
            n_registered = r["config"]["n_registered"]
            store = r["config"]["store"]
            for br in batch_rows:
                survivors = br["candidate_survivors"]
                sf = survivors / n_registered
                pr = br["changed_count"] / survivors
                survivor_fracs.append(sf)
                precisions.append(pr)
                per_store_survivor[store].append(sf)
                per_store_precision[store].append(pr)

    eq(len(survivor_fracs), 240,
       "storm-v2 c1 batches: total rows over the 12 c1-mix cells (12 x 20 batches)")
    eq(len(precisions), 240,
       "storm-v2 c1 batches: total rows over the 12 c1-mix cells (12 x 20 batches)")

    survivor_median = statistics.median(survivor_fracs)
    precision_median = statistics.median(precisions)
    close(survivor_median, 0.262, 0.001, "storm-v2 c1 frozen: median candidate_survivors / "
          "n_registered over the 240 c1-mix batches")
    close(precision_median, 0.187, 0.001, "storm-v2 c1 frozen: median changed_count / "
          "candidate_survivors over the 240 c1-mix batches")

    synth_survivor_median = statistics.median(per_store_survivor["synth-iv-60k"])
    collegemsg_survivor_median = statistics.median(per_store_survivor["collegemsg"])
    synth_precision_median = statistics.median(per_store_precision["synth-iv-60k"])
    collegemsg_precision_median = statistics.median(per_store_precision["collegemsg"])
    close(synth_survivor_median, 0.266, 0.001, "storm-v2 c1 frozen: median "
          "candidate_survivors / n_registered over the 120 synth-iv-60k c1-mix batches")
    close(collegemsg_survivor_median, 0.257, 0.001, "storm-v2 c1 frozen: median "
          "candidate_survivors / n_registered over the 120 collegemsg c1-mix batches")
    close(synth_precision_median, 0.182, 0.001, "storm-v2 c1 frozen: median changed_count / "
          "candidate_survivors over the 120 synth-iv-60k c1-mix batches")
    close(collegemsg_precision_median, 0.194, 0.001, "storm-v2 c1 frozen: median changed_count "
          "/ candidate_survivors over the 120 collegemsg c1-mix batches")

    def _speedup(r: dict) -> float:
        arms = r["summary"]["arms"]
        return arms["global-recompute"]["ttf_p50_ms"] / arms["tgms-L1"]["ttf_p50_ms"]

    all_speedups = [_speedup(r) for r in rows]
    grid_min, grid_max = min(all_speedups), max(all_speedups)
    close(grid_min, 4.5883, 0.001,
          "storm-v2 main grid frozen: minimum per-cell speedup over all 36 cells")
    close(grid_max, 8.8635, 0.001,
          "storm-v2 main grid frozen: maximum per-cell speedup over all 36 cells")

    target = [r for r in rows if r["config"]["store"] == "synth-iv-60k"
              and r["config"]["mix"] == "c1" and r["config"]["age"] is None
              and r["config"]["seed"] == 0]
    eq(len(target), 1, "storm-v2 main grid: exactly one cell at (synth-iv-60k, c1, age none, "
       "seed 0)")
    n1k_speedup = _speedup(target[0])
    close(n1k_speedup, 5.1729, 0.001, "storm-v2 main grid frozen: N=1,000 speedup at "
          "synth-iv-60k/c1/age-none/seed-0 (global-recompute ttf_p50_ms / tgms-L1 ttf_p50_ms)")

    groups: dict[tuple[str, str, str | None], list[dict]] = {}
    for r in rows:
        cfg_r = r["config"]
        groups.setdefault((cfg_r["store"], cfg_r["mix"], cfg_r["age"]), []).append(r)
    eq(len(groups), 12, "storm-v2 main grid: 12 distinct (store, mix, age) groups")

    frozen_group_medians = {
        ("synth-iv-60k", "c1", None): 5.173, ("synth-iv-60k", "c1", "deep"): 4.902,
        ("synth-iv-60k", "c3", None): 5.642, ("synth-iv-60k", "c3", "deep"): 5.217,
        ("synth-iv-60k", "c4", None): 6.367, ("synth-iv-60k", "c4", "deep"): 5.022,
        ("collegemsg", "c1", None): 6.189, ("collegemsg", "c1", "deep"): 6.821,
        ("collegemsg", "c3", None): 6.662, ("collegemsg", "c3", "deep"): 6.332,
        ("collegemsg", "c4", None): 7.433, ("collegemsg", "c4", "deep"): 8.071,
    }
    eq(set(groups), set(frozen_group_medians), "storm-v2 main grid: (store, mix, age) group "
       "keys match the frozen table")

    per_group_median: dict[tuple[str, str, str | None], float] = {}
    for key, grp in groups.items():
        eq(sorted(g["config"]["seed"] for g in grp), [0, 1, 2],
           f"storm-v2 main grid: group {key} covers seeds 0/1/2 exactly")
        med = statistics.median(_speedup(g) for g in grp)
        close(med, frozen_group_medians[key], 0.001,
              f"storm-v2 main grid frozen: median speedup for {key}")
        per_group_median[key] = med

    m.add("osdiStormV2Commit", commit,
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: git_commit, uniform over all 36 cells (== "
          f"{relpath(STORM_V2_MAIN_GRID)}'s own git_commit)")
    m.add("osdiStormV2Cells", len(rows),
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: line count (== "
          f"{relpath(STORM_V2_MAIN_GRID)}'s own total_tasks/config.n_tasks)")
    m.add("osdiStormV2CellsFailed", len(failing_cells),
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: cells recomputed to fail G-S1 or G-S2, of 36 "
          f"(== union of {relpath(STORM_V2_MAIN_GRID)}'s own gates.g_s{{1,2}}_failing_cells)")
    m.add("osdiStormV2AllTopTerms", all_top_term_total,
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: sum of summary.narrowing_coverage."
          "n_all_top_term over all 36 cells")
    m.add("osdiStormV2NonComputeArtifacts", tex_num(non_compute_artifacts),
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: sum of (summary.narrowing_coverage.n_artifacts "
          "- n_empty_scope) over all 36 cells")
    m.add("osdiStormV2SpeedupN1kSeed0", f"{n1k_speedup:.3f}",
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: summary.arms.{{global-recompute,tgms-L1}}."
          "ttf_p50_ms ratio at (store=synth-iv-60k, mix=c1, age=none, seed=0, "
          "n_artifacts=1000)")
    m.add("osdiStormV2AvoidedDecisionC1Median", f"{avoided_median:.3f}",
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: median(summary.arms.tgms-L1."
          "avoided_recompute_decision) over the 12 c1-mix cells")
    m.add("osdiStormV2C1Batches", len(survivor_fracs),
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: total per-batch rows over the 12 c1-mix "
          "cells' own -rows.jsonl files (12 cells x 20 batches)")
    m.add("osdiStormV2SurvivorFractionC1Median", f"{survivor_median:.3f}",
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: median(candidate_survivors / "
          "config.n_registered) over the 240 c1-mix batches")
    m.add("osdiStormV2PrecisionC1Median", f"{precision_median:.3f}",
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: median(changed_count / candidate_survivors) "
          "over the 240 c1-mix batches")
    m.add("osdiStormV2SurvivorFractionSynthC1Median", f"{synth_survivor_median:.3f}",
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: median(candidate_survivors / "
          "config.n_registered) over the 120 synth-iv-60k c1-mix batches")
    m.add("osdiStormV2SurvivorFractionCollegeMsgC1Median", f"{collegemsg_survivor_median:.3f}",
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: median(candidate_survivors / "
          "config.n_registered) over the 120 collegemsg c1-mix batches")
    m.add("osdiStormV2PrecisionSynthC1Median", f"{synth_precision_median:.3f}",
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: median(changed_count / candidate_survivors) "
          "over the 120 synth-iv-60k c1-mix batches")
    m.add("osdiStormV2PrecisionCollegeMsgC1Median", f"{collegemsg_precision_median:.3f}",
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: median(changed_count / candidate_survivors) "
          "over the 120 collegemsg c1-mix batches")
    m.add("osdiStormV2FalseFreshTgmsCellsNonzero", ff_nonzero,
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: count of (cell, arm) pairs among tgms-L0/"
          "tgms-L1 over all 36 cells (72 total) with summary.arms[arm].false_fresh > 0")

    for key, med in per_group_median.items():
        store, mix, age = key
        name = (f"osdiStormV2Speedup{_STORM_V2_STORE_TOKEN[store]}{_STORM_V2_MIX_TOKEN[mix]}"
                f"{_STORM_V2_AGE_TOKEN[age]}")
        m.add(name, f"{med:.3f}",
              f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: median over seeds 0/1/2 of "
              "summary.arms.{global-recompute,tgms-L1}.ttf_p50_ms ratio at "
              f"(store={store}, mix={mix}, age={age or 'none'}, n_artifacts=1000)")

    m.add("osdiStormV2SpeedupGridMin", f"{grid_min:.3f}",
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: minimum per-cell "
          "summary.arms.{global-recompute,tgms-L1}.ttf_p50_ms ratio over all 36 cells")
    m.add("osdiStormV2SpeedupGridMax", f"{grid_max:.3f}",
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: maximum per-cell "
          "summary.arms.{global-recompute,tgms-L1}.ttf_p50_ms ratio over all 36 cells")


# --------------------------------------------------------------------------
# D-160 CollegeMsg re-measurement (Lane W2c): the production claim gate now
# drops `unverifiable` claims too (docs/STABILITY.md section 9), and this is
# the fresh coverage/conditional-accuracy/UCR record measured under it. The
# old-gate numbers already on public surfaces are never overwritten -- see
# docs/site_facts.json's `unsupported_claims` fact and the new
# `*_d160` facts beside it.
# --------------------------------------------------------------------------

def _carries_claim(row: dict) -> bool:
    ao = row.get("answer_object")
    return bool(ao and ao.get("claims"))


def compute_d160(m: Macros) -> None:
    manifest = json.loads(D160_MANIFEST.read_text(encoding="utf-8"))
    rows_bytes = D160_ROWS.read_bytes()
    digest = hashlib.sha256(rows_bytes).hexdigest()
    eq(digest, manifest["result_digest"],
       "D160: sha256(rows-2026-09-14.json) matches manifest.result_digest")

    rows = json.loads(rows_bytes)
    eq(len(rows), 1128, "D160 frozen: total row count")
    eq(len(rows), manifest["provenance"]["n_rows"], "D160: row count matches manifest n_rows")

    def by_system(name: str) -> list[dict]:
        return [r for r in rows if r["system"] == name]

    ours = by_system("ours")
    b6e = by_system("b6e")
    b5 = by_system("b5")
    llm_direct = by_system("llm_direct")

    eq(len(ours), 282, "D160 frozen: ours row count")
    eq(len(b6e), 282, "D160 frozen: b6e row count")
    eq(len(b5), 282, "D160 frozen: b5 row count")
    eq(len(llm_direct), 282, "D160 frozen: llm_direct row count")

    task_ids = {r["task_id"] for r in ours}
    eq(len(task_ids), 94, "D160 frozen: distinct CollegeMsg task count")
    seeds = sorted({r["seed"] for r in ours})
    eq(seeds, [0, 1, 2], "D160: three seeds, 0/1/2")

    # ---- ours: coverage, conditional accuracy, ucr, ucr_pre_gate ----
    ours_carry = [r for r in ours if _carries_claim(r)]
    eq(len(ours_carry), 112, "D160 frozen: ours claim-carrying row count, pooled")
    ours_coverage = len(ours_carry) / len(ours)
    close(ours_coverage, 0.397, 0.001, "D160 frozen: ours pooled coverage")

    ours_cond_acc = statistics.mean(r["em"] for r in ours_carry)
    close(ours_cond_acc, 0.509, 0.001, "D160 frozen: ours pooled conditional accuracy")

    ours_ucr_gated = statistics.mean(r["ucr"] for r in ours_carry)
    eq(ours_ucr_gated, 0.0, "D160 frozen: ours pooled post-gate UCR")

    # ucr_pre_gate is only meaningful for rows that reached claim proposal at
    # all -- a safe-refusal row with no plan never produced a raw AnswerObject
    # to score pre-gate, and the record correctly omits the field there.
    ours_pre = [r for r in ours if "ucr_pre_gate" in r]
    eq(len(ours_pre), 257, "D160 frozen: ours rows that reached claim proposal pre-gate")
    ours_ucr_pre = statistics.mean(r["ucr_pre_gate"] for r in ours_pre)
    close(ours_ucr_pre, 0.212, 0.001, "D160 frozen: ours pooled pre-gate UCR")

    # Constancy: a per-seed coverage outlier averaged away by pooling must
    # not pass silently.
    for s in seeds:
        rs = [r for r in ours if r["seed"] == s]
        c = [r for r in rs if _carries_claim(r)]
        seed_cov = len(c) / len(rs)
        close(seed_cov, ours_coverage, 0.02,
              f"D160: ours seed {s} coverage {seed_cov:.4f} not within 0.02 of pooled "
              f"{ours_coverage:.4f}")

    # ---- b6e: its own ECQR-based gate, coverage + conditional accuracy ----
    b6e_carry = [r for r in b6e if _carries_claim(r)]
    b6e_coverage = len(b6e_carry) / len(b6e)
    close(b6e_coverage, 0.830, 0.001, "D160 frozen: b6e pooled coverage")
    b6e_cond_acc = statistics.mean(r["em"] for r in b6e_carry)
    close(b6e_cond_acc, 0.333, 0.001, "D160 frozen: b6e pooled conditional accuracy")
    for s in seeds:
        rs = [r for r in b6e if r["seed"] == s]
        c = [r for r in rs if _carries_claim(r)]
        seed_cov = len(c) / len(rs)
        close(seed_cov, b6e_coverage, 0.02,
              f"D160: b6e seed {s} coverage {seed_cov:.4f} not within 0.02 of pooled "
              f"{b6e_coverage:.4f}")

    # ---- b5: ungated interface ablation, raw EM (deterministic, temp 0) ----
    b5_em = statistics.mean(r["em"] for r in b5)
    close(b5_em, 0.181, 0.001, "D160 frozen: b5 pooled raw EM")
    for s in seeds:
        rs = [r for r in b5 if r["seed"] == s]
        seed_em = statistics.mean(r["em"] for r in rs)
        eq(round(seed_em, 6), round(b5_em, 6),
           f"D160: b5 seed {s} EM must equal the pooled EM (deterministic, temperature 0)")

    # ---- llm_direct: 0 claim-carrying rows, 216 context-overflow errors ----
    llm_carry = [r for r in llm_direct if _carries_claim(r)]
    eq(len(llm_carry), 0, "D160 frozen: llm_direct pooled claim-carrying row count")
    llm_errors = [r for r in llm_direct if r.get("task_error")]
    eq(len(llm_errors), 216, "D160 frozen: llm_direct task_error row count")
    llm_overflow = [r for r in llm_errors if "ContextWindowExceededError" in str(r["task_error"])]
    eq(len(llm_overflow), len(llm_errors),
       "D160: every llm_direct task_error is the known context-overflow error -- no other "
       "failure mode is silently folded into this count")

    # ---- old-gate counterparts: parsed out of docs/site_facts.json's
    # unsupported_claims fact (the only place the pre-D-160 CollegeMsg
    # numbers live as structured data) and cross-checked against
    # docs/STABILITY.md section 9, which is the only place the pre-D-160
    # conditional accuracy (0.548) is stated at all -- site_facts.json has
    # no separate coverage/conditional-accuracy fact for the old gate.
    facts = json.loads(SITE_FACTS.read_text(encoding="utf-8"))["facts"]
    old_uc = facts["unsupported_claims"]
    eq(old_uc["value"], "0", "D160: old-gate unsupported_claims value is still 0")
    eq(old_uc.get("label_required"), "pre-D-160 gate",
       "D160: old-gate unsupported_claims must be labelled 'pre-D-160 gate' now that the "
       "D-160-gate numbers land beside it")

    m_prose = re.search(
        r"0 of (\d+) on the frozen CollegeMsg campaign; before gating it was \d+ of \d+",
        old_uc["prose"])
    require(m_prose is not None,
            "D160: could not parse the old-gate emitted-answer count out of "
            "site_facts.json's unsupported_claims prose")
    old_gate_ucr_denominator = int(m_prose.group(1))
    eq(old_gate_ucr_denominator, 199, "D160 frozen: old-gate UCR denominator (emitted answers)")

    m_scope = re.search(r"\((\d+) emitted, of (\d+) task runs\)", old_uc["scope_required"])
    require(m_scope is not None,
            "D160: could not parse the old-gate coverage numerator/denominator out of "
            "site_facts.json's unsupported_claims scope_required")
    old_gate_coverage_num = int(m_scope.group(1))
    old_gate_coverage_den = int(m_scope.group(2))
    eq(old_gate_coverage_num, old_gate_ucr_denominator,
       "D160: old-gate coverage numerator matches the UCR denominator (same 199 emitted answers)")
    eq(old_gate_coverage_den, 282, "D160 frozen: old-gate coverage denominator (task runs)")
    old_gate_coverage = old_gate_coverage_num / old_gate_coverage_den
    close(old_gate_coverage, 0.706, 0.001, "D160 frozen: old-gate pooled coverage")

    stab_text = STABILITY_MD.read_text(encoding="utf-8")
    m_stab = re.search(
        r"ucr_gated`\s+0/(\d+),\s+coverage\s+(0\.\d+)\s+at\s+conditional accuracy\s+(0\.\d+)",
        stab_text)
    require(m_stab is not None,
            "D160: could not find the old-gate coverage/conditional-accuracy sentence in "
            "docs/STABILITY.md section 9 (D-160)")
    eq(int(m_stab.group(1)), old_gate_ucr_denominator,
       "D160: STABILITY.md's old-gate UCR denominator matches site_facts.json's")
    eq(float(m_stab.group(2)), round(old_gate_coverage, 3),
       "D160: STABILITY.md's old-gate coverage matches the ratio recomputed from "
       "site_facts.json's unsupported_claims fact")
    old_gate_cond_acc = float(m_stab.group(3))
    eq(old_gate_cond_acc, 0.548, "D160 frozen: old-gate conditional accuracy")

    m.add("osdiD160Tasks", len(task_ids),
          f"{relpath(D160_ROWS)}: distinct task_id values among system==ours rows")
    m.add("osdiD160TaskRuns", len(ours),
          f"{relpath(D160_ROWS)}: row count for system==ours (94 tasks x 3 seeds)")
    m.add("osdiD160OursCarrying", len(ours_carry),
          f"{relpath(D160_ROWS)}: ours rows with answer_object.claims non-empty, pooled "
          "over 3 seeds")
    m.add("osdiD160OursCoverage", f"{ours_coverage:.3f}",
          f"{relpath(D160_ROWS)}: osdiD160OursCarrying / osdiD160TaskRuns")
    m.add("osdiD160OursCondAcc", f"{ours_cond_acc:.3f}",
          f"{relpath(D160_ROWS)}: mean(em) over ours claim-carrying rows")
    m.add("osdiD160OursUcrGated", int(ours_ucr_gated),
          f"{relpath(D160_ROWS)}: mean(ucr) over ours claim-carrying rows (post-gate)")
    m.add("osdiD160OursUcrPreGate", f"{ours_ucr_pre:.3f}",
          f"{relpath(D160_ROWS)}: mean(ucr_pre_gate) over the 257 ours rows that reached "
          "claim proposal pre-gate")
    m.add("osdiD160B6eCoverage", f"{b6e_coverage:.3f}",
          f"{relpath(D160_ROWS)}: b6e rows with answer_object.claims non-empty / 282, pooled")
    m.add("osdiD160B6eCondAcc", f"{b6e_cond_acc:.3f}",
          f"{relpath(D160_ROWS)}: mean(em) over b6e claim-carrying rows")
    m.add("osdiD160B5Em", f"{b5_em:.3f}",
          f"{relpath(D160_ROWS)}: mean(em) over all 282 b5 rows (ungated, deterministic)")
    m.add("osdiD160LlmDirectCarrying", len(llm_carry),
          f"{relpath(D160_ROWS)}: llm_direct rows with answer_object.claims non-empty, pooled")
    m.add("osdiD160LlmDirectOverflowErrors", len(llm_overflow),
          f"{relpath(D160_ROWS)}: llm_direct rows whose task_error is "
          "litellm.ContextWindowExceededError, of 282 (known pre-tokenizer-fix limitation, "
          "see benchmarks/d160-collegemsg-v1/README.md)")
    m.add("osdiOldGateCoverage", f"{old_gate_coverage:.3f}",
          f"{relpath(SITE_FACTS)}: unsupported_claims.scope_required 199/282, cross-checked "
          f"against {relpath(STABILITY_MD)} section 9's own stated 0.706 -- the pre-D-160-gate "
          "coverage, printed only beside osdiD160OursCoverage, never in its place")
    m.add("osdiOldGateUcr", int(float(old_uc["value"])),
          f"{relpath(SITE_FACTS)}: unsupported_claims.value, of {old_gate_ucr_denominator} "
          "emitted answers -- the pre-D-160-gate UCR")
    m.add("osdiOldGateCondAcc", f"{old_gate_cond_acc:.3f}",
          f"{relpath(STABILITY_MD)} section 9 (D-160): the pre-D-160-gate conditional accuracy "
          "among emitted answers -- not a separate site_facts.json field, so parsed from and "
          "cross-checked against that section's own prose")


# --------------------------------------------------------------------------
# D-160 llm_direct follow-up (Lane W2d): the whitespace-token budget
# approximation that produced 216/282 task_error rows in compute_d160's
# first-shipped llm_direct arm is fixed in tgms/eval/baselines.py (a real
# HF tokenizer, commits dab5c2a/8de040f) -- this re-runs llm_direct only,
# same task set/model/seeds, under that fix (job 212231). It does not
# touch or overwrite compute_d160's numbers or docs/site_facts.json; see
# benchmarks/d160-collegemsg-v1/README.md.
# --------------------------------------------------------------------------

def compute_d160_llm_direct_fix(m: Macros) -> None:
    manifest = json.loads(D160_MANIFEST_FIX.read_text(encoding="utf-8"))
    rows_bytes = D160_ROWS_FIX.read_bytes()
    digest = hashlib.sha256(rows_bytes).hexdigest()
    eq(digest, manifest["result_digest"],
       "D160 fix: sha256(rows-llm-direct-fix-2026-09-14.json) matches manifest.result_digest")

    rows = json.loads(rows_bytes)
    eq(len(rows), 282, "D160 fix frozen: total row count")
    eq(len(rows), manifest["provenance"]["n_rows"],
       "D160 fix: row count matches manifest n_rows")

    llm_direct = [r for r in rows if r["system"] == "llm_direct"]
    eq(len(llm_direct), len(rows), "D160 fix frozen: every row is system==llm_direct")

    task_ids = {r["task_id"] for r in llm_direct}
    eq(len(task_ids), 94, "D160 fix frozen: distinct CollegeMsg task count")
    seeds = sorted({r["seed"] for r in llm_direct})
    eq(seeds, [0, 1, 2], "D160 fix: three seeds, 0/1/2")

    # ---- 0 task_error rows: the fix's whole point ----
    errors = [r for r in llm_direct if r.get("task_error")]
    eq(len(errors), 0, "D160 fix frozen: llm_direct task_error row count")

    # ---- still 0 claim-carrying rows: the fix removes the context-overflow
    # crash, not the gate's verdict on this arm's claims ----
    carry = [r for r in llm_direct if _carries_claim(r)]
    eq(len(carry), 0, "D160 fix frozen: llm_direct pooled claim-carrying row count")
    coverage = len(carry) / len(llm_direct)
    eq(coverage, 0.0, "D160 fix frozen: llm_direct pooled coverage")

    # ---- meta.tokenizer_kind / meta.budget_effective_tokens: uniform
    # across all 282 rows, confirming the fix measured the real tokenizer
    # and the nominal budget in real tokens, not whitespace-approximated
    # ones ----
    tokenizer_kinds = {r["meta"]["tokenizer_kind"] for r in llm_direct}
    eq(tokenizer_kinds, {"hf_real"},
       "D160 fix: meta.tokenizer_kind must be uniformly 'hf_real' across all 282 rows")
    budgets = {r["meta"]["budget_effective_tokens"] for r in llm_direct}
    eq(budgets, {8000},
       "D160 fix: meta.budget_effective_tokens must be uniformly 8000 across all 282 rows")
    eq(manifest["protocol"]["ceilings"]["llm_direct_budget_tokens"], 8000,
       "D160 fix: manifest's nominal llm_direct_budget_tokens is 8000, matching every row's "
       "meta.budget_effective_tokens")

    # ---- raw pre-gate EM: score meta.pre_gate_answer (the answer before
    # GATED_VERDICTS dropped anything) against the frozen suite's gold
    # answers, via the same tgms.eval.metrics.score_answer/extract_pred the
    # harness itself uses to score every row's post-gate `em`. This is
    # exactly the README's "raw pre-gate exact-match 0.064" computation. ----
    from tgms.eval.metrics import extract_pred, score_answer
    suite = json.loads(D160_SUITE.read_text(encoding="utf-8"))
    tasks_by_id = {t["id"]: t for t in suite["test"]}
    eq(len(tasks_by_id), 94, "D160 fix: frozen suite has 94 distinct test tasks")

    pre_gate_ems = []
    for r in llm_direct:
        pga = r["meta"]["pre_gate_answer"]
        task = tasks_by_id[r["task_id"]]
        pred = extract_pred(task["answer_kind"], pga)
        pre_gate_ems.append(score_answer(task["answer_kind"], task["gold"], pred)["em"])
    eq(len(pre_gate_ems), 282,
       "D160 fix: every row scored pre-gate (0 errors, meta.pre_gate_answer present throughout)")
    raw_em = statistics.mean(pre_gate_ems)
    close(raw_em, 0.064, 0.001, "D160 fix frozen: llm_direct raw pre-gate EM")

    m.add("osdiD160LlmDirectCoverageFixed", f"{coverage:.3f}",
          f"{relpath(D160_ROWS_FIX)}: llm_direct rows with answer_object.claims non-empty / "
          "282, pooled over 3 seeds -- the fixed-budget re-run of osdiD160LlmDirectCarrying, "
          "still 0.000 (the fix removes the context-overflow crash, not the gate's verdict)")
    m.add("osdiD160LlmDirectErrorsFixed", len(errors),
          f"{relpath(D160_ROWS_FIX)}: llm_direct task_error row count, of 282 -- 0 after the "
          "real-tokenizer budget fix, vs osdiD160LlmDirectOverflowErrors (216) before it")
    m.add("osdiD160LlmDirectRawEmFixed", f"{raw_em:.3f}",
          f"{relpath(D160_ROWS_FIX)}: mean(em) of meta.pre_gate_answer scored via "
          "tgms.eval.metrics.score_answer/extract_pred against "
          "benchmarks/frozen-v1/suite-collegemsg.json's gold/answer_kind fields, over all 282 "
          "rows (complete, vs the pre-fix record's partial n=66 easy-task-only sample)")
    m.add("osdiD160LlmDirectTokenizerFixed", r"hf\_real",
          f"{relpath(D160_ROWS_FIX)}: meta.tokenizer_kind, asserted uniform across all 282 rows")
    m.add("osdiD160LlmDirectBudgetFixed", 8000,
          f"{relpath(D160_ROWS_FIX)}: meta.budget_effective_tokens, asserted uniform across all "
          "282 rows (== manifest protocol.ceilings.llm_direct_budget_tokens)")


# --------------------------------------------------------------------------
# C2 --- corruption-detection sweep, pre- and post-A10 (task A10 fixed the
# artifact_blob reader to content-address the whole file)
# --------------------------------------------------------------------------

def _corruption_result_digest(results: list[dict]) -> str:
    """Mirrors scripts/corruption_campaign_merge.py's result_digest(kind="corruption"):
    sha256 over the results sorted by (class, mutation, task_id, trial), canonical JSON."""
    key_fn = lambda r: (r["class"], r["mutation"], r.get("task_id", -1), r["trial"])  # noqa: E731
    canon = sorted(results, key=key_fn)
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _corruption_detection_matrix(results: list[dict]) -> dict[tuple[str, str], list[int]]:
    dm: dict[tuple[str, str], list[int]] = {}
    for r in results:
        key = (r["class"], r["mutation"])
        cell = dm.setdefault(key, [0, 0])
        cell[0] += 1
        if r["verdict"] == "DETECTED":
            cell[1] += 1
    return dm


def compute_c2(m: Macros) -> None:
    pre = json.loads(CORRUPTION_PRE.read_text(encoding="utf-8"))
    post = json.loads(CORRUPTION_POST.read_text(encoding="utf-8"))

    for label, d, path in (("pre-A10", pre, CORRUPTION_PRE), ("post-A10", post, CORRUPTION_POST)):
        digest = _corruption_result_digest(d["results"])
        eq(digest, d["result_digest"],
           f"C2 {label}: sha256(sorted results, corruption_campaign_merge.py's own scheme) "
           f"matches {relpath(path)}'s own result_digest")
        eq(len(d["results"]), 10000, f"C2 {label} frozen: trial row count")
        eq(d["total_trials"], len(d["results"]), f"C2 {label}: total_trials matches len(results)")

    pre_dm = _corruption_detection_matrix(pre["results"])
    post_dm = _corruption_detection_matrix(post["results"])

    # Cross-check the recomputed per-cell (trials, detected) against each
    # record's own stats.detection_matrix -- never trusted without this.
    for label, d, dm in (("pre-A10", pre, pre_dm), ("post-A10", post, post_dm)):
        stat_dm = d["stats"]["detection_matrix"]
        eq(len(stat_dm), len(dm),
           f"C2 {label}: recomputed cell count matches stats.detection_matrix")
        for key, cell in stat_dm.items():
            c, mut = key.split("|")
            trials, detected = dm[(c, mut)]
            eq(trials, cell["trials"], f"C2 {label}: {key} trials matches recomputed")
            eq(detected, cell["detected"], f"C2 {label}: {key} detected matches recomputed")

    classes = sorted({c for c, _ in pre_dm})
    mutations = sorted({mut for _, mut in pre_dm})
    eq(len(classes), 13, "C2 frozen: distinct on-disk file classes")
    eq(len(mutations), 7, "C2 frozen: distinct mutation kinds")
    eq(sorted({c for c, _ in post_dm}), classes,
       "C2: post-A10 record's file classes match the pre-A10 record's")
    eq(sorted({mut for _, mut in post_dm}), mutations,
       "C2: post-A10 record's mutation kinds match the pre-A10 record's")

    recomputed_silent_pre = sum(1 for r in pre["results"] if r["verdict"] == "SILENT")
    recomputed_silent_post = sum(1 for r in post["results"] if r["verdict"] == "SILENT")
    eq(recomputed_silent_pre, 0, "C2 frozen: pre-A10 SILENT count")
    eq(recomputed_silent_post, 0, "C2 frozen: post-A10 SILENT count")
    eq(recomputed_silent_pre, pre["stats"]["verdict_counts"].get("SILENT", 0),
       "C2: recomputed pre-A10 SILENT matches the record's own verdict_counts")
    eq(recomputed_silent_post, post["stats"]["verdict_counts"].get("SILENT", 0),
       "C2: recomputed post-A10 SILENT matches the record's own verdict_counts")

    detected_pre = sum(1 for r in pre["results"] if r["verdict"] == "DETECTED")
    detected_post = sum(1 for r in post["results"] if r["verdict"] == "DETECTED")
    eq(detected_pre, 5966, "C2 frozen: pre-A10 total DETECTED count")
    eq(detected_post, 6587, "C2 frozen: post-A10 total DETECTED count")
    eq(detected_pre, pre["stats"]["verdict_counts"]["DETECTED"],
       "C2: recomputed pre-A10 DETECTED matches the record's own verdict_counts")
    eq(detected_post, post["stats"]["verdict_counts"]["DETECTED"],
       "C2: recomputed post-A10 DETECTED matches the record's own verdict_counts")

    # The six artifact_blob mutations task A10 fixed. delete_file is
    # deliberately excluded: it was already DETECTED 89/89 before A10 (a
    # different code path -- an outright-missing plan blob, not a doctored
    # one), and the README's own six-cell diff excludes it too.
    blob_fixed = ("append_garbage", "flip_bit", "flip_byte", "swap_same_class",
                  "truncate", "zero_span")
    require(set(blob_fixed).issubset(mutations),
            "C2: the six blob-fix mutations are a subset of the campaign's own mutation set")
    require("delete_file" in mutations and "delete_file" not in blob_fixed,
            "C2: delete_file (already DETECTED pre-A10) is deliberately excluded from the "
            "blob-fix set")

    n_blob_pre = sum(pre_dm[("artifact_blob", mut)][0] for mut in blob_fixed)
    det_blob_pre = sum(pre_dm[("artifact_blob", mut)][1] for mut in blob_fixed)
    n_blob_post = sum(post_dm[("artifact_blob", mut)][0] for mut in blob_fixed)
    det_blob_post = sum(post_dm[("artifact_blob", mut)][1] for mut in blob_fixed)
    eq(n_blob_pre, n_blob_post,
       "C2: the blob-fix trial population is identical pre/post-A10 (same seeds, same design)")
    eq(n_blob_pre, 621, "C2 frozen: blob-fix mutation trial count (N)")
    eq(det_blob_pre, 0, "C2 frozen: pre-A10 blob-fix mutations DETECTED count (0 of N)")
    eq(det_blob_post, n_blob_post, "C2 frozen: post-A10 blob-fix mutations DETECTED count (N of N)")

    # All seven artifact_blob mutations (the six A10 fixed, plus the one
    # that was already DETECTED before A10 via a different mechanism) --
    # a reader citing "blob detected" without qualification would otherwise
    # assume this population, not the six-mutation fix subset above.
    all_blob_mutations = sorted(mut for c, mut in pre_dm if c == "artifact_blob")
    eq(len(all_blob_mutations), 7, "C2 frozen: artifact_blob mutation count (all seven)")
    eq(set(all_blob_mutations), set(mutations),
       "C2: artifact_blob's own mutation set equals the campaign's full mutation set")

    n_blob_all_pre = sum(pre_dm[("artifact_blob", mut)][0] for mut in all_blob_mutations)
    det_blob_all_pre = sum(pre_dm[("artifact_blob", mut)][1] for mut in all_blob_mutations)
    n_blob_all_post = sum(post_dm[("artifact_blob", mut)][0] for mut in all_blob_mutations)
    det_blob_all_post = sum(post_dm[("artifact_blob", mut)][1] for mut in all_blob_mutations)
    eq(n_blob_all_pre, n_blob_all_post,
       "C2: artifact_blob's total trial population (all seven mutations) is identical "
       "pre/post-A10")
    eq(n_blob_all_pre, 710, "C2 frozen: artifact_blob total trial count, all seven mutations")
    eq(det_blob_all_post, n_blob_all_post,
       "C2 frozen: post-A10 artifact_blob DETECTED count over all seven mutations (N of N)")

    # The one artifact_blob mutation DETECTED pre-A10 -- recomputed, never
    # hard-coded, as "the mutation outside the six-mutation fix set with a
    # nonzero pre-A10 DETECTED count".
    already_detected = [mut for mut in all_blob_mutations
                        if mut not in blob_fixed and pre_dm[("artifact_blob", mut)][1] > 0]
    eq(len(already_detected), 1,
       "C2: exactly one artifact_blob mutation was already DETECTED pre-A10")
    already_detected_mutation = already_detected[0]
    eq(already_detected_mutation, "delete_file",
       "C2 frozen: the pre-A10-already-detected artifact_blob mutation")

    # Partition check: the six-mutation fix set and the already-detected
    # mutation must cover all seven blob mutations, disjointly, and their
    # trial counts must sum to the all-mutations total (710) -- not merely
    # assumed from the mutation-name partition alone.
    eq(set(blob_fixed) | set(already_detected), set(all_blob_mutations),
       "C2: the six-mutation fix set + the already-detected mutation cover all seven blob "
       "mutations")
    eq(set(blob_fixed) & set(already_detected), set(),
       "C2: the six-mutation fix set and the already-detected mutation are disjoint")
    n_already_pre, det_already_pre = pre_dm[("artifact_blob", already_detected_mutation)]
    eq(n_blob_pre + n_already_pre, n_blob_all_pre,
       "C2: the six-mutation trial count plus the already-detected mutation's trial count "
       "sums to the all-seven-mutations total -- a partition of the 710 blob trials")
    eq(det_already_pre, n_already_pre,
       "C2 frozen: the already-detected mutation was DETECTED in every one of its own "
       "pre-A10 trials")
    eq(det_blob_all_pre, det_already_pre,
       "C2: pre-A10 DETECTED over all seven blob mutations equals the already-detected "
       "mutation's own count alone (the six fixed mutations contributed 0)")

    # Every non-blob cell must be within +/-2 trials of the pre-A10 record
    # (the README's own pre-registered prediction #8); this is the
    # constancy assertion, never averaged away.
    moved = [key for key in pre["stats"]["detection_matrix"]
             if not key.startswith("artifact_blob|")
             and abs(pre_dm[tuple(key.split("|"))][1] - post_dm[tuple(key.split("|"))][1]) > 2]
    eq(len(moved), 0,
       f"C2 frozen: non-blob class x mutation cells whose DETECTED count moved by >2 trials "
       f"post-A10 (got {moved})")

    m.add("osdiCorruptionTrials", tex_num(pre["total_trials"]),
          f"{relpath(CORRUPTION_PRE)}: total_trials, == {relpath(CORRUPTION_POST)}'s own "
          "(both 10,000-trial sweeps)")
    m.add("osdiCorruptionClasses", len(classes),
          f"{relpath(CORRUPTION_PRE)}: distinct classes in stats.detection_matrix keys "
          "(the 13 on-disk file families), unchanged post-A10")
    m.add("osdiCorruptionMutations", len(mutations),
          f"{relpath(CORRUPTION_PRE)}: distinct mutations in stats.detection_matrix keys, "
          "unchanged post-A10")
    m.add("osdiCorruptionDetected", tex_num(detected_post),
          f"{relpath(CORRUPTION_POST)}: recomputed DETECTED total, of 10,000 -- the deployed "
          "(post-A10) system's headline count; see osdiCorruptionDetectedPre/Post for the "
          "before/after pair")
    m.add("osdiCorruptionDetectedPre", tex_num(detected_pre),
          f"{relpath(CORRUPTION_PRE)}: recomputed DETECTED total, of 10,000, before task A10")
    m.add("osdiCorruptionDetectedPost", tex_num(detected_post),
          f"{relpath(CORRUPTION_POST)}: recomputed DETECTED total, of 10,000, after task A10")
    m.add("osdiCorruptionSilentPre", recomputed_silent_pre,
          f"{relpath(CORRUPTION_PRE)}: recomputed SILENT total, of 10,000, before task A10")
    m.add("osdiCorruptionSilentPost", recomputed_silent_post,
          f"{relpath(CORRUPTION_POST)}: recomputed SILENT total, of 10,000, after task A10")
    m.add("osdiCorruptionBlobDetectedPre", f"{det_blob_pre}/{n_blob_pre}",
          f"{relpath(CORRUPTION_PRE)}: DETECTED/trials summed over the six artifact_blob "
          "mutations task A10 fixed (append_garbage/flip_bit/flip_byte/swap_same_class/"
          "truncate/zero_span), before the fix")
    m.add("osdiCorruptionBlobDetectedPost", f"{det_blob_post}/{n_blob_post}",
          f"{relpath(CORRUPTION_POST)}: DETECTED/trials summed over the same six artifact_blob "
          "mutations, after the fix")
    m.add("osdiCorruptionBlobTrials", tex_num(n_blob_all_pre),
          f"{relpath(CORRUPTION_PRE)}: trials summed over all seven artifact_blob mutations "
          "(== the six-mutation fix population 621 + the already-detected mutation's own "
          f"{n_already_pre}), unchanged post-A10")
    m.add("osdiCorruptionBlobDetectedAllPre", f"{det_blob_all_pre}/{n_blob_all_pre}",
          f"{relpath(CORRUPTION_PRE)}: DETECTED/trials summed over all seven artifact_blob "
          "mutations, before the fix -- distinct from osdiCorruptionBlobDetectedPre, which "
          "covers only the six mutations A10 fixed")
    m.add("osdiCorruptionBlobDetectedAllPost", f"{det_blob_all_post}/{n_blob_all_post}",
          f"{relpath(CORRUPTION_POST)}: DETECTED/trials summed over all seven artifact_blob "
          "mutations, after the fix")
    m.add("osdiCorruptionBlobMutationAlreadyDetected", already_detected_mutation.replace("_", r"\_"),
          f"{relpath(CORRUPTION_PRE)}: the one artifact_blob mutation (of all seven) with a "
          "nonzero pre-A10 DETECTED count, recomputed as the mutation outside the six-mutation "
          "fix set with detected>0 -- not hard-coded")
    m.add("osdiCorruptionCellsMovedPost", len(moved),
          f"{relpath(CORRUPTION_PRE)} vs {CORRUPTION_POST.name}: non-blob class x mutation "
          "cells whose DETECTED count moved by more than 2 trials (asserted 0 above)")


# --------------------------------------------------------------------------
# D4/D4b --- overhead ladder, five rungs, twelve plans, three seeds
# --------------------------------------------------------------------------

def compute_ladder(m: Macros) -> None:
    merged = json.loads(LADDER_MERGED.read_text(encoding="utf-8"))
    raws = [json.loads(p.read_text(encoding="utf-8")) for p in LADDER_RAW]

    for raw, path in zip(raws, LADDER_RAW):
        digest = hashlib.sha256(
            json.dumps(raw["rows"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        eq(digest, raw["result_digest"],
           f"ladder: sha256(rows) matches {relpath(path)}'s own result_digest")

    summary_digest = hashlib.sha256(
        json.dumps(merged["summary"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    eq(summary_digest, merged["result_digest"],
       f"ladder: sha256(summary) matches {relpath(LADDER_MERGED)}'s own result_digest")
    eq(merged["seeds"], [0, 1, 2], "ladder: pooled record covers seeds 0/1/2")

    def rows_of(raw, rung):
        return [r for r in raw["rows"] if r["rung"] == rung]

    # ---- rung 1: leaf_overhead, per operator, flattened over every
    # (plan-occurrence, seed) sample -- never trusted from the summary's own
    # median without this recomputation. ----
    r1_samples: dict[str, list[float]] = {}
    for raw in raws:
        for outer in rows_of(raw, 1):
            for inner in outer["rows"]:
                r1_samples.setdefault(inner["op"], []).append(inner["leaf_over_direct"])
    r1_summary = merged["summary"]["rung1_leaf_overhead"]
    eq(len(r1_samples), 14, "ladder frozen: rung-1 distinct operator count (13 LEAF_SCOPES + "
       "compute)")
    eq(set(r1_samples), set(r1_summary), "ladder: rung-1 recomputed op set matches summary's")
    r1_medians = {}
    for op, samples in r1_samples.items():
        med = statistics.median(samples)
        eq(sorted(samples), sorted(r1_summary[op]["per_seed"]),
           f"ladder rung1 {op}: recomputed per-sample values match summary's per_seed list")
        close(med, r1_summary[op]["leaf_over_direct_median"], 1e-9,
              f"ladder rung1 {op}: recomputed median matches summary")
        r1_medians[op] = med
    rung1_min_op = min(r1_medians, key=r1_medians.get)
    rung1_max_op = max(r1_medians, key=r1_medians.get)
    rung1_min, rung1_max = r1_medians[rung1_min_op], r1_medians[rung1_max_op]
    close(rung1_min, 0.9919982626082593, 1e-9, "ladder frozen: rung-1 minimum op median")
    close(rung1_max, 1.064464807738779, 1e-9, "ladder frozen: rung-1 maximum op median")
    require(0.7 <= rung1_min and rung1_max <= 1.5,
            "ladder: every rung-1 op median falls within the freeze's falsifier band [0.7, 1.5]")

    # ---- rung 2: compiled_vs_kernel, the two tgms.tgir.compiled.COMPILED
    # ops only. ----
    r2_samples: dict[str, list[float]] = {}
    for raw in raws:
        for outer in rows_of(raw, 2):
            for inner in outer["rows"]:
                r2_samples.setdefault(inner["op"], []).append(inner["compiled_over_kernel"])
    r2_summary = merged["summary"]["rung2_compiled_vs_kernel"]
    eq(set(r2_samples), {"entity_history", "version_history"},
       "ladder frozen: rung-2 population is exactly the two compiled ops")
    eq(len(r2_samples["entity_history"]), 6,
       "ladder frozen: entity_history rung-2 sample count (2 plan-occurrences x 3 seeds)")
    eq(len(r2_samples["version_history"]), 3,
       "ladder frozen: version_history rung-2 sample count (1 plan-occurrence x 3 seeds)")
    entity_ratio = statistics.median(r2_samples["entity_history"])
    version_ratio = statistics.median(r2_samples["version_history"])
    close(entity_ratio, r2_summary["entity_history"]["compiled_over_kernel_median"], 1e-9,
          "ladder rung2: recomputed entity_history median matches summary")
    close(version_ratio, r2_summary["version_history"]["compiled_over_kernel_median"], 1e-9,
          "ladder rung2: recomputed version_history median matches summary")
    close(entity_ratio, 1.1321759928956914, 1e-9, "ladder frozen: rung-2 entity_history median")
    close(version_ratio, 2.5403795318319973, 1e-9, "ladder frozen: rung-2 version_history median")
    require(1 <= entity_ratio <= 2000,
            "ladder: entity_history compiled_over_kernel falls within the freeze's pass_if "
            "band [1, 2000]")

    # ---- plan set: 12 plans, 14/14 operator coverage (union of every
    # rung's own per-plan ops_in_plan field, cross-checked against rung 1's
    # own 14-op population above). ----
    plan_ids = sorted({outer["plan_id"] for outer in rows_of(raws[0], 1)})
    eq(len(plan_ids), 12, "ladder frozen: plan count")
    all_ops = {op for outer in rows_of(raws[0], 1) for op in outer["ops_in_plan"]}
    eq(all_ops, set(r1_samples), "ladder: union of every plan's ops_in_plan matches rung-1's "
       "own 14-op population")

    # Step count per plan is only carried explicitly in the rung-5 rows
    # (n_steps); reuse that rather than hard-coding which plans are 1-step
    # vs 3-step.
    n_steps_by_plan = {outer["plan_id"]: outer["n_steps"] for outer in rows_of(raws[0], 5)}
    one_step_plans = {p for p, n in n_steps_by_plan.items() if n == 1}
    three_step_plans = {p for p, n in n_steps_by_plan.items() if n == 3}
    eq(len(one_step_plans), 9, "ladder frozen: one-step plan count")
    eq(len(three_step_plans), 3, "ladder frozen: three-step plan count")
    eq(one_step_plans | three_step_plans, set(plan_ids),
       "ladder: every plan is either 1-step or 3-step, no other shape")

    # ---- rung 3: trace_bytes, bit-identical across reps and across seeds
    # for every plan. ----
    r3_summary = merged["summary"]["rung3_trace_bytes"]
    plan_bytes: dict[str, list[int]] = {}
    for raw in raws:
        for outer in rows_of(raw, 3):
            require(outer["bytes_min"] == outer["bytes_max"] == outer["bytes_median"],
                    f"ladder rung3 {outer['plan_id']}: not bit-identical across its own 5 reps")
            require(len(set(outer["bytes_all"])) == 1,
                    f"ladder rung3 {outer['plan_id']}: bytes_all has more than one distinct value")
            plan_bytes.setdefault(outer["plan_id"], []).append(outer["bytes_median"])
    n_deterministic = 0
    for plan, vals in plan_bytes.items():
        require(len(set(vals)) == 1,
                f"ladder rung3 {plan}: bytes_median differs across seeds {vals}")
        n_deterministic += 1
        eq(vals[0], r3_summary[plan]["bytes_median"],
           f"ladder rung3 {plan}: recomputed bytes_median matches summary")
        require(r3_summary[plan]["reproducible_across_seeds"] is True,
                f"ladder rung3 {plan}: summary's own reproducible_across_seeds flag is not True")
    eq(n_deterministic, 12, "ladder frozen: plans confirmed bit-identical across reps and seeds")
    one_step_bytes_median = statistics.median(plan_bytes[p][0] for p in one_step_plans)
    three_step_bytes_median = statistics.median(plan_bytes[p][0] for p in three_step_plans)
    eq(one_step_bytes_median, 3309, "ladder frozen: 1-step trace_bytes median across plans")
    eq(three_step_bytes_median, 7549, "ladder frozen: 3-step trace_bytes median across plans")
    for plan in plan_bytes:
        band = (1500, 30000) if plan in one_step_plans else (4000, 90000)
        require(band[0] <= plan_bytes[plan][0] <= band[1],
                f"ladder rung3 {plan}: bytes_median {plan_bytes[plan][0]} outside the freeze's "
                f"predicted band {band}")

    # ---- rung 4: verify_ms, median p50 per plan, then median across plans. ----
    r4_summary = merged["summary"]["rung4_verify_ms"]
    plan_p50: dict[str, list[float]] = {}
    for raw in raws:
        for outer in rows_of(raw, 4):
            recomputed_p50 = statistics.median(outer["ms_all"])
            close(recomputed_p50, outer["p50_ms"], 1e-9,
                  f"ladder rung4 {outer['plan_id']}: median(ms_all) matches the raw row's own "
                  "p50_ms")
            plan_p50.setdefault(outer["plan_id"], []).append(outer["p50_ms"])
    plan_p50_median = {}
    for plan, vals in plan_p50.items():
        med = statistics.median(vals)
        close(med, r4_summary[plan]["p50_ms_median"], 1e-9,
              f"ladder rung4 {plan}: recomputed median matches summary")
        plan_p50_median[plan] = med
    verify_ms_median = statistics.median(plan_p50_median.values())
    close(verify_ms_median, 5.061005940660834, 1e-6, "ladder frozen: rung-4 median-of-plan-medians")
    require(0.5 <= min(plan_p50_median.values()) and max(plan_p50_median.values()) <= 50,
            "ladder: every rung-4 plan median falls within the freeze's pass_if band [0.5, 50]")

    # ---- rung 5: tokens_tool_calls -- tokens median, and the tool-calls ==
    # executed-steps invariant on every one of the 12 plans. ----
    r5_summary = merged["summary"]["rung5_tokens_tool_calls"]
    plan_tokens: dict[str, list[int]] = {}
    plan_tool_calls: dict[str, list[int]] = {}
    for raw in raws:
        for outer in rows_of(raw, 5):
            plan_tokens.setdefault(outer["plan_id"], []).append(outer["tokens"]["total"])
            plan_tool_calls.setdefault(outer["plan_id"], []).append(outer["tool_calls"])
    plan_tokens_median = {}
    truncated_plans = []
    n_tool_calls_eq_executed = 0
    for plan in plan_ids:
        med = statistics.median(plan_tokens[plan])
        eq(med, r5_summary[plan]["tokens_total_median"],
           f"ladder rung5 {plan}: recomputed tokens median matches summary")
        plan_tokens_median[plan] = med
        tc = plan_tool_calls[plan]
        require(len(set(tc)) == 1, f"ladder rung5 {plan}: tool_calls not constant across seeds")
        eq(sorted(tc), sorted(r5_summary[plan]["tool_calls_per_seed"]),
           f"ladder rung5 {plan}: recomputed tool_calls_per_seed matches summary")
        n_steps = n_steps_by_plan[plan]
        eq(n_steps, r5_summary[plan]["n_steps"], f"ladder rung5 {plan}: n_steps matches summary")
        # The merge's own "executed_steps" field, by this campaign's
        # construction (README: Executor.run's truncation guard stops a
        # plan before its trailing compute step ever reaches
        # ToolRouter.call), is exactly tool_calls -- confirmed here, not
        # assumed, since executed_steps is not itself a raw-row field.
        eq(r5_summary[plan]["executed_steps"], tc[0],
           f"ladder rung5 {plan}: summary's executed_steps equals raw tool_calls")
        eq(tc[0] == r5_summary[plan]["executed_steps"],
           r5_summary[plan]["tool_calls_eq_executed_steps"],
           f"ladder rung5 {plan}: tool_calls_eq_executed_steps recomputes correctly")
        require(r5_summary[plan]["tool_calls_eq_executed_steps"] is True,
                f"ladder rung5 {plan}: tool_calls != executed_steps -- falsifier (a) trips")
        n_tool_calls_eq_executed += 1
        if tc[0] < n_steps:
            truncated_plans.append(plan)
        band = (800, 8000) if plan in one_step_plans else (2000, 20000)
        require(band[0] <= med <= band[1],
                f"ladder rung5 {plan}: tokens median {med} outside the freeze's predicted "
                f"band {band}")
    eq(n_tool_calls_eq_executed, 12,
       "ladder frozen: plans asserted tool_calls == executed_steps")
    tokens_median_overall = statistics.median(plan_tokens_median.values())
    eq(tokens_median_overall, 1811.5, "ladder frozen: rung-5 median-of-plan-medians tokens.total")
    eq(sorted(truncated_plans), ["p02-compiled-entity-and-version", "p10-reachability-and-paths"],
       "ladder frozen: the exact two plans truncated at 2-of-3 executed steps")

    m.add("osdiLadderPlans", tex_num(len(plan_ids)),
          f"{relpath(LADDER_RAW[0])}: distinct plan_id values in the rung-1 rows")
    m.add("osdiLadderOperatorsCovered", tex_num(len(all_ops)),
          f"{relpath(LADDER_RAW[0])}: union of every plan's ops_in_plan, rung 1 (13 LEAF_SCOPES "
          "+ compute)")
    m.add("osdiLadderRung1Min", f"{rung1_min:.3f}",
          f"{relpath(LADDER_MERGED)}: min over 14 ops of rung1_leaf_overhead[op]."
          f"leaf_over_direct_median ({rung1_min_op})")
    m.add("osdiLadderRung1Max", f"{rung1_max:.3f}",
          f"{relpath(LADDER_MERGED)}: max over 14 ops of rung1_leaf_overhead[op]."
          f"leaf_over_direct_median ({rung1_max_op})")
    m.add("osdiLadderRung2EntityHistory", f"{entity_ratio:.2f}",
          f"{relpath(LADDER_MERGED)}: rung2_compiled_vs_kernel.entity_history."
          "compiled_over_kernel_median")
    m.add("osdiLadderRung2VersionHistory", f"{version_ratio:.2f}",
          f"{relpath(LADDER_MERGED)}: rung2_compiled_vs_kernel.version_history."
          "compiled_over_kernel_median")
    m.add("osdiLadderRung3BytesOneStepMedian", tex_num(one_step_bytes_median),
          f"{relpath(LADDER_MERGED)}: median over the 9 one-step plans of "
          "rung3_trace_bytes[plan].bytes_median")
    m.add("osdiLadderRung3BytesThreeStepMedian", tex_num(three_step_bytes_median),
          f"{relpath(LADDER_MERGED)}: median over the 3 three-step plans of "
          "rung3_trace_bytes[plan].bytes_median")
    m.add("osdiLadderRung3Deterministic", tex_num(n_deterministic),
          f"{relpath(LADDER_DIR)}/raw/*.json: plans confirmed bit-identical across all 5 reps "
          "and all 3 seeds (asserted above), of 12")
    m.add("osdiLadderRung4VerifyMsMedian", f"{verify_ms_median:.2f}",
          f"{relpath(LADDER_MERGED)}: median over the 12 plans of rung4_verify_ms[plan]."
          "p50_ms_median, ms")
    m.add("osdiLadderRung5TokensMedian", f"{tokens_median_overall:.1f}",
          f"{relpath(LADDER_MERGED)}: median over the 12 plans of rung5_tokens_tool_calls[plan]."
          "tokens_total_median")
    m.add("osdiLadderRung5ToolCallsEqualExecutedSteps", tex_num(n_tool_calls_eq_executed),
          f"{relpath(LADDER_DIR)}/raw/*.json: plans with tool_calls == executed steps "
          "(falsifier (a)'s own 'executed steps' wording, not a naive n_steps comparison), "
          "asserted on all 12")
    m.add("osdiLadderPlansTruncated", tex_num(len(truncated_plans)),
          f"{relpath(LADDER_DIR)}/raw/*.json: plans where tool_calls < n_steps "
          "(p02, p10 -- Executor.run's own truncation guard on a downstream compute step, "
          "see README)")


# --------------------------------------------------------------------------
# Longevity -- 24h soak (Lane W2g / Lane B task B7a, Gate G1/Gate E)
#
# benchmarks/longevity-v1/: the real (non-dev-host) 24h soak,
# scripts/longevity_run.py at commit 886805f (pre-fix for
# D-086-reader-torn-tail-race) against stores/synth-1m-native on xzgpu.
# README.md documents three harness defects this generator must not paper
# over, and this function's own checks recompute past every one of them
# rather than trusting a summary field:
#   (1) manifest.summary.error_count (1) is the LAST writer life's counter
#       only -- counter_latest's unlabeled-key collision silently drops
#       every earlier life's counters. The true total (249) is summed here
#       from writer_error_counts_by_life.json's per-life rows and asserted
#       against that file's own true_total_errors_all_lives field, never
#       trusted from either without the recomputation. Both numbers are
#       emitted, side by side, per this script's non-overwrite discipline --
#       osdiSoakWriterErrorsManifest is explicitly labelled as the wrong,
#       harness-defect value.
#   (2) digest_equal is JSON `null` ("not computed") because the disk guard
#       skipped the mandatory final replay: 1,074,952 uncompacted batches
#       project (D-149's own O(batches^2) manifest-growth formula,
#       recomputed here, not just read) to ~280.8 TB, refused by
#       --max-disk-mb=20000. Never "computed and found False" --
#       gate_e_report.md's own "digest_equal=False" text is a display
#       artifact of scripts/longevity_report.py coercing None to False,
#       asserted below as a known, cited discrepancy, not this macro's
#       source of truth.
#   (3) reader_restarts.jsonl's two rows (readers 6 and 5) are both the same
#       D-086 reader-torn-tail race, cross-checked against
#       ops/failure_ledger.jsonl's own entry when that file is present on
#       disk (it is coordinator-maintained on main, not edited by this
#       measurement worktree).
# No verdict macro is emitted here, per the lane brief -- Gate E's own
# PASS/FAIL/FLAG table lives in gate_e_report.md, not in this script.
# --------------------------------------------------------------------------

def compute_longevity_soak(m: Macros) -> None:
    # Whole-file digest check against benchmarks/longevity-v1/README.md's
    # own "Files here" table (sha256, verified byte-identical to the xzgpu
    # originals before commit) -- catches an edited committed copy before
    # any field inside it is even parsed.
    eq(sha256_file(LONGEVITY_MANIFEST), LONGEVITY_MANIFEST_SHA256,
       f"{relpath(LONGEVITY_MANIFEST)}: sha256 matches README.md's Files-here table")
    eq(sha256_file(LONGEVITY_RECOVERIES), LONGEVITY_RECOVERIES_SHA256,
       f"{relpath(LONGEVITY_RECOVERIES)}: sha256 matches README.md's Files-here table")
    eq(sha256_file(LONGEVITY_READER_RESTARTS), LONGEVITY_READER_RESTARTS_SHA256,
       f"{relpath(LONGEVITY_READER_RESTARTS)}: sha256 matches README.md's Files-here table")
    eq(sha256_file(LONGEVITY_LEDGER), LONGEVITY_LEDGER_SHA256,
       f"{relpath(LONGEVITY_LEDGER)}: sha256 matches README.md's Files-here table")
    eq(sha256_file(LONGEVITY_ORCHESTRATOR_LOG), LONGEVITY_ORCHESTRATOR_LOG_SHA256,
       f"{relpath(LONGEVITY_ORCHESTRATOR_LOG)}: sha256 matches README.md's Files-here table")

    manifest = json.loads(LONGEVITY_MANIFEST.read_text(encoding="utf-8"))
    summary = manifest["summary"]

    # --- commit + duration ---
    eq(manifest["git_commit"], "886805f", "Longevity frozen: measured commit")
    duration_s = manifest["config"]["duration_s"]
    eq(duration_s, 86400.0, "Longevity frozen: configured soak duration_s")
    hours = duration_s / 3600.0
    eq(hours, 24.0, "Longevity: config.duration_s / 3600 is exactly 24 hours")

    # --- entity count start/end ---
    # The starting count (1,000,000) is not a counted field anywhere in the
    # manifest or any side-file -- it is the store's own name
    # ("synth-1m-native"), the same "<N>m" naming convention compute_c4
    # above already treats as meaningful (control_1m/control_10m). Asserted
    # here only as "the store name says 1m", not as a counted field; the end
    # count *is* a counted field (final_stats.n_entities) and is checked as
    # one, with a frozen expected value.
    store_name = manifest["config"]["store_name"]
    eq(store_name, "synth-1m-native", "Longevity: config.store_name")
    eq(manifest["dataset"]["name"], store_name,
       "Longevity: dataset.name matches config.store_name")
    entities_start = 1_000_000
    entities_end = summary["final_stats"]["n_entities"]
    eq(entities_end, 1_730_492, "Longevity frozen: summary.final_stats.n_entities")
    require(entities_end > entities_start,
            "Longevity: the store grew past its nominal 1M starting size over the soak")

    # --- batches ---
    total_batches = summary["total_batches"]
    eq(total_batches, 1_074_952, "Longevity frozen: summary.total_batches")

    # --- writer lives / the true-vs-manifest error count defect ---
    by_life = json.loads(LONGEVITY_WRITER_ERRORS_BY_LIFE.read_text(encoding="utf-8"))
    per_life_errors = by_life["per_life_errors"]
    eq(by_life["lives"], 42, "Longevity frozen: writer_error_counts_by_life.json lives")
    eq(len(per_life_errors), by_life["lives"],
       "Longevity: per_life_errors row count matches the file's own lives field")
    true_total_errors = sum(per_life_errors)
    eq(true_total_errors, 249,
       "Longevity frozen: true total writer errors, summed over 42 lives")
    eq(true_total_errors, by_life["true_total_errors_all_lives"],
       "Longevity: recomputed sum(per_life_errors) matches the file's own "
       "true_total_errors_all_lives field")

    manifest_error_count = summary["error_count"]
    eq(manifest_error_count, 1, "Longevity frozen: manifest's own (wrong) summary.error_count")
    eq(manifest_error_count, summary["writer_final"]["errors"],
       "Longevity: summary.error_count matches writer_final.errors (both life-41-only)")
    eq(manifest_error_count, by_life["manifest_reported_error_count"],
       "Longevity: manifest's error_count matches writer_error_counts_by_life.json's "
       "own record of that (wrong) figure")
    require(true_total_errors != manifest_error_count,
            "Longevity: the true per-life sum must differ from the manifest's single-life "
            "figure -- this is the harness defect the README documents, not a no-op check")

    # --- writer corrections applied/skipped, all 42 lives (same
    # counter_latest label-collision defect as the error count above --
    # writer_error_counts_by_life.json's own aggregate fields, no per-life
    # breakdown array exists for these two, unlike per_life_errors) ---
    corrections_applied = by_life["true_total_corrections_applied_all_lives"]
    corrections_skipped = by_life["true_total_corrections_skipped_all_lives"]
    eq(corrections_applied, 207_850,
       "Longevity frozen: true total corrections applied, 42 lives")
    eq(corrections_skipped, 137_163,
       "Longevity frozen: true total corrections skipped, 42 lives")

    # --- recoveries: designed restart cycle, by cause ---
    recoveries_rows = load_jsonl(LONGEVITY_RECOVERIES)
    eq(len(recoveries_rows), 41, "Longevity frozen: recoveries.jsonl row count")
    eq(len(recoveries_rows), summary["recoveries"],
       "Longevity: recoveries.jsonl row count matches summary.recoveries")
    require(all(r["kind"] == "designed" for r in recoveries_rows),
            "Longevity: every recovery row is the harness's own designed restart cycle")
    n_sigabrt = sum(1 for r in recoveries_rows if r["returncode"] == -6)
    n_exit137 = sum(1 for r in recoveries_rows if r["returncode"] == 137)
    eq(n_sigabrt, 23, "Longevity frozen: SIGABRT (-6) recovery count")
    eq(n_exit137, 18, "Longevity frozen: os._exit(137) recovery count")
    eq(n_sigabrt + n_exit137, len(recoveries_rows),
       "Longevity: every recovery row is one of exactly these two returncodes")
    unexpected = summary["unexpected_writer_deaths"]
    eq(unexpected, 0, "Longevity frozen: unexpected_writer_deaths")

    # --- reader deaths: both the D-086 torn-tail race ---
    reader_rows = load_jsonl(LONGEVITY_READER_RESTARTS)
    eq(len(reader_rows), 2, "Longevity frozen: reader_restarts.jsonl row count")
    eq(len(reader_rows), summary["reader_restarts"],
       "Longevity: reader_restarts.jsonl row count matches summary.reader_restarts")
    eq(sorted(r["idx"] for r in reader_rows), [5, 6],
       "Longevity frozen: reader indices that died (5 and 6)")
    require(all(r["returncode"] == 1 for r in reader_rows),
            "Longevity: both reader deaths are returncode 1 (uncaught StateError)")
    if FAILURE_LEDGER.exists():
        ledger_entries = [json.loads(line) for line in
                           FAILURE_LEDGER.read_text(encoding="utf-8").splitlines() if line.strip()]
        d086 = [e for e in ledger_entries if e.get("id") == "D-086-reader-torn-tail-race"]
        require(len(d086) == 1,
                "Longevity: exactly one D-086-reader-torn-tail-race entry in "
                "ops/failure_ledger.jsonl (cross-check only, not this macro's source)")
        if d086:
            wild = d086[0].get("observed_in_the_wild", "")
            require("reader 6" in wild and "reader 5" in wild,
                    "Longevity: the D-086 ledger entry's observed_in_the_wild note names "
                    "both reader 6 and reader 5 (cross-check only)")

    # --- verify_healthy ---
    require(summary["verify_healthy"] is True,
            "Longevity: summary.verify_healthy is the JSON literal true")

    # --- throughput / p99 drift, first hour vs. last hour ---
    drift = summary["drift"]
    throughput_start = round(drift["throughput_first_hour_avg"], 2)
    throughput_end = round(drift["throughput_last_hour_avg"], 2)
    eq(throughput_start, 24.03, "Longevity frozen: first-hour throughput, commits/s")
    eq(throughput_end, 16.41, "Longevity frozen: last-hour throughput, commits/s")

    p99_start = round(drift["commit_p99_first_hour_max"], 1)
    p99_end = round(drift["commit_p99_last_hour_max"], 1)
    eq(p99_start, 485.6, "Longevity frozen: first-hour commit p99, ms")
    eq(p99_end, 3740.5, "Longevity frozen: last-hour commit p99, ms")

    # --- metadata growth slopes ---
    growth = summary["metadata_growth_slope_bytes_per_s"]
    manifest_growth = round(growth["manifests"], 1)
    segment_growth = round(growth["segments"], 1)
    eq(manifest_growth, -3.4, "Longevity frozen: manifest-bytes growth slope, B/s")
    eq(segment_growth, 1341.1, "Longevity frozen: segment-bytes growth slope, B/s")

    # --- compactions ---
    compactions = summary["compactions"]
    eq(compactions, 2_415, "Longevity frozen: summary.compactions")

    # --- digest_equal / replay skip, cross-checked across the log, the
    #     ledger, the manifest, and the harness's own projection formula ---
    require(summary["digest_equal"] is None,
            "Longevity: summary.digest_equal is JSON null (\"not computed\"), never False")
    eq(summary["replay_skipped_reason"], "projected_replay_exceeds_limit",
       "Longevity frozen: summary.replay_skipped_reason")
    report_text = LONGEVITY_GATE_E_REPORT.read_text(encoding="utf-8")
    require("digest_equal=False" in report_text,
            "Longevity: gate_e_report.md's own digest_equal=False text confirms scripts/"
            "longevity_report.py's None->False display coercion (a known reporting "
            "artifact -- the manifest's own null is this macro's source, not this report)")

    log_text = LONGEVITY_ORCHESTRATOR_LOG.read_text(encoding="utf-8")
    log_match = re.search(
        r"disk_guard_replay_skip: total_batches=(\d+), projected_mb=([\d.]+)", log_text)
    require(log_match is not None,
            "Longevity: orchestrator.log carries a disk_guard_replay_skip line")
    log_batches = int(log_match.group(1))
    log_projected_mb = float(log_match.group(2))
    eq(log_batches, total_batches,
       "Longevity: orchestrator.log's disk_guard_replay_skip total_batches matches "
       "summary.total_batches")

    ledger_rows = load_jsonl(LONGEVITY_LEDGER)
    eq(len(ledger_rows), 1, "Longevity frozen: longevity_ledger.jsonl row count")
    ledger_event = ledger_rows[0]
    eq(ledger_event["event"], "disk_guard_replay_skip",
       "Longevity: the ledger's one row is the disk_guard_replay_skip event")
    eq(ledger_event["total_batches"], total_batches,
       "Longevity: ledger's total_batches matches summary.total_batches")
    close(ledger_event["projected_mb"], log_projected_mb, 1.0,
          "Longevity: ledger's projected_mb matches the orchestrator log line")

    # Recompute the projection from the harness's own documented formula
    # (scripts/longevity_run.py::cmd_run, quoted in README.md: projected_mb
    # = 243.0 * total_batches**2 / 1e6) rather than trusting either copy of
    # the number verbatim.
    recomputed_projected_mb = 243.0 * (total_batches ** 2) / 1e6
    close(recomputed_projected_mb, log_projected_mb, 1.0,
          "Longevity: 243.0*total_batches**2/1e6 matches the orchestrator log's "
          "projected_mb (D-149's own O(batches^2) formula, quoted in README.md)")
    projected_tb = round(recomputed_projected_mb / 1e6, 1)
    eq(projected_tb, 280.8, "Longevity frozen: replay projection, TB")

    # --- emit macros ---
    m.add("osdiSoakHours", tex_num(int(hours)),
          f"{relpath(LONGEVITY_MANIFEST)}: config.duration_s / 3600")
    m.add("osdiSoakCommit", manifest["git_commit"],
          f"{relpath(LONGEVITY_MANIFEST)}: git_commit")
    m.add("osdiSoakEntitiesStart", tex_num(entities_start),
          f"{relpath(LONGEVITY_MANIFEST)}: config.store_name / dataset.name "
          "(\"synth-1m-native\") -- the store's own \"1m\" label, not a counted field; "
          "final_stats.n_entities confirms growth past this nominal start")
    m.add("osdiSoakEntitiesEnd", tex_num(entities_end),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.final_stats.n_entities")
    m.add("osdiSoakBatches", tex_num(total_batches),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.total_batches")
    m.add("osdiSoakWriterLives", tex_num(by_life["lives"]),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_LIFE)}: lives (== len(per_life_errors))")
    m.add("osdiSoakRecoveries", tex_num(len(recoveries_rows)),
          f"{relpath(LONGEVITY_RECOVERIES)}: row count, == summary.recoveries")
    m.add("osdiSoakRecoveriesSigabrt", tex_num(n_sigabrt),
          f"{relpath(LONGEVITY_RECOVERIES)}: rows with returncode == -6 (SIGABRT)")
    m.add("osdiSoakRecoveriesExit137", tex_num(n_exit137),
          f"{relpath(LONGEVITY_RECOVERIES)}: rows with returncode == 137 (os._exit(137))")
    m.add("osdiSoakUnexpectedRecoveries", tex_num(unexpected),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.unexpected_writer_deaths -- all 41 "
          "recoveries are the harness's own designed restart cycle (kind==\"designed\")")
    m.add("osdiSoakReaderDeaths", tex_num(len(reader_rows)),
          f"{relpath(LONGEVITY_READER_RESTARTS)}: row count, == summary.reader_restarts")
    m.add("osdiSoakReaderDeathCause", "reader torn-tail race, pre-fix engine",
          f"{relpath(LONGEVITY_READER_RESTARTS)}: both rows (readers 6, 5) are the "
          "D-086-reader-torn-tail-race StateError shape; ops/failure_ledger.jsonl's D-086 "
          "entry (cross-checked when present) confirms both instances against commit "
          "886805f, which predates the fix series (ef97d2d, 43f6ef4, 3a664a8)")
    m.add("osdiSoakVerifyHealthy", "true",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.verify_healthy")
    m.add("osdiSoakWriterErrorsManifest", tex_num(manifest_error_count),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.error_count -- the harness's own "
          "counter_latest label-collision defect (last writer life only, see README.md); "
          "not a run total")
    m.add("osdiSoakWriterErrorsTrue", tex_num(true_total_errors),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_LIFE)}: sum(per_life_errors), 42 lives")
    m.add("osdiSoakCorrectionsAppliedTrue", tex_num(corrections_applied),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_LIFE)}: "
          "true_total_corrections_applied_all_lives, 42 lives -- ops issued, not "
          "identities landed (see README.md's \"errors observed\" section)")
    m.add("osdiSoakCorrectionsSkippedTrue", tex_num(corrections_skipped),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_LIFE)}: "
          "true_total_corrections_skipped_all_lives, 42 lives")
    m.add("osdiSoakThroughputStart", f"{throughput_start:.2f}",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.drift.throughput_first_hour_avg, commits/s")
    m.add("osdiSoakThroughputEnd", f"{throughput_end:.2f}",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.drift.throughput_last_hour_avg, commits/s")
    m.add("osdiSoakP99StartMs", f"{p99_start:.1f}",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.drift.commit_p99_first_hour_max, ms")
    m.add("osdiSoakP99EndMs", f"{p99_end:,.1f}".replace(",", "{,}"),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.drift.commit_p99_last_hour_max, ms")
    m.add("osdiSoakManifestGrowthBps", f"{manifest_growth:.1f}",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.metadata_growth_slope_bytes_per_s.manifests")
    m.add("osdiSoakSegmentGrowthBps", f"{segment_growth:,.1f}".replace(",", "{,}"),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.metadata_growth_slope_bytes_per_s.segments")
    m.add("osdiSoakDigestStatus", "not computed",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.digest_equal is JSON null -- the final "
          "replay/digest-equivalence step never ran (disk guard skip), never \"computed "
          "and found False\" (text macro, not a number)")
    m.add("osdiSoakReplayProjectedTB", f"{projected_tb:.1f}",
          f"{relpath(LONGEVITY_ORCHESTRATOR_LOG)}: disk_guard_replay_skip line's "
          "projected_mb / 1e6, cross-checked against longevity_ledger.jsonl and "
          "recomputed from 243.0*total_batches**2/1e6 (scripts/longevity_run.py's own "
          "D-149 projection formula, quoted in README.md)")
    m.add("osdiSoakCompactions", tex_num(compactions),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.compactions")


def compute_longevity_rederived(m: Macros) -> None:
    """Lane W2j: the 2026-09-15 re-derived Gate E report (post-fix harness,
    same soak) and the same-day aborted post-hoc replay check. Both are
    read-only re-measurements against the W2g soak's own preserved raw
    inputs -- see benchmarks/longevity-v1/README.md's "Re-derived Gate E
    report" and "Post-hoc replay check" sections. No verdict macro."""
    # --- whole-file digest checks ---
    eq(sha256_file(LONGEVITY_SUMMARY_REDERIVED), LONGEVITY_SUMMARY_REDERIVED_SHA256,
       f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: sha256 matches this lane's frozen value")
    eq(sha256_file(LONGEVITY_GATE_E_REPORT_REDERIVED), LONGEVITY_GATE_E_REPORT_REDERIVED_SHA256,
       f"{relpath(LONGEVITY_GATE_E_REPORT_REDERIVED)}: sha256 matches this lane's frozen value")
    eq(sha256_file(LONGEVITY_REPLAY_CHECK), LONGEVITY_REPLAY_CHECK_SHA256,
       f"{relpath(LONGEVITY_REPLAY_CHECK)}: sha256 matches README.md's own quoted value in "
       "its \"Post-hoc replay check\" section")

    summary_doc = json.loads(LONGEVITY_SUMMARY_REDERIVED.read_text(encoding="utf-8"))

    # The re-derivation must be provably against the *same* soak: its own
    # derived_from field names the original manifest's sha256, checked
    # here against W2g's own frozen constant for that same file, not just
    # trusted as a string.
    eq(summary_doc["derived_from"]["original_manifest_sha256"], LONGEVITY_MANIFEST_SHA256,
       f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: derived_from.original_manifest_sha256 "
       "matches the sha256 of longevity-synth-1m-native-0.json (W2g's own frozen constant) "
       "-- proof this re-derivation ran against the same soak, not a different one")
    eq(sha256_file(LONGEVITY_MANIFEST), LONGEVITY_MANIFEST_SHA256,
       f"{relpath(LONGEVITY_MANIFEST)}: sha256 matches README.md's Files-here table "
       "(re-asserted here since the check above depends on it)")

    # --- writer within-life RSS slope: recomputed from the per-life rows,
    # not trusted from the file's own aggregate fields ---
    writer_slope = summary_doc["writer_within_life_rss_slope"]
    per_life = writer_slope["per_life"]
    eq(len(per_life), 42, "Longevity re-derived frozen: writer_within_life_rss_slope.per_life row count")
    life_slopes = [row["slope_kb_per_s"] for row in per_life]
    writer_median = statistics.median(life_slopes)
    writer_min = min(life_slopes)
    writer_max = max(life_slopes)
    close(writer_median, writer_slope["median_kb_per_s"], 1e-6,
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: recomputed median(per_life[*]."
          "slope_kb_per_s) matches the file's own writer_within_life_rss_slope.median_kb_per_s")
    close(writer_min, writer_slope["min_kb_per_s"], 1e-6,
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: recomputed min(per_life[*].slope_kb_per_s) "
          "matches the file's own writer_within_life_rss_slope.min_kb_per_s")
    close(writer_max, writer_slope["max_kb_per_s"], 1e-6,
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: recomputed max(per_life[*].slope_kb_per_s) "
          "matches the file's own writer_within_life_rss_slope.max_kb_per_s")
    eq(writer_slope["n_lives_with_fit"], 42,
       "Longevity re-derived frozen: writer_within_life_rss_slope.n_lives_with_fit")
    noise_floor = writer_slope["noise_threshold_kb_per_s"]
    eq(noise_floor, 5.0, "Longevity re-derived frozen: writer noise_threshold_kb_per_s")
    n_positive = sum(1 for s in life_slopes if s > noise_floor)
    eq(n_positive, writer_slope["n_positive_beyond_noise_5kb_s"],
       f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: recomputed count(per_life[*].slope_kb_per_s "
       "> noise_threshold_kb_per_s) matches the file's own n_positive_beyond_noise_5kb_s")
    eq(n_positive, 42, "Longevity re-derived frozen: all 42 writer lives clear the noise floor")

    median_kb = round(writer_median, 1)
    min_kb = round(writer_min, 1)
    max_kb = round(writer_max, 1)
    eq(median_kb, 3165.6, "Longevity re-derived frozen: writer within-life slope median, kB/s")
    eq(min_kb, 1921.1, "Longevity re-derived frozen: writer within-life slope min, kB/s")
    eq(max_kb, 4154.2, "Longevity re-derived frozen: writer within-life slope max, kB/s")

    # --- reader within-life RSS slope: recomputed by pooling every
    # fitted segment across all 8 readers, not trusted from the file's own
    # pooled aggregate ---
    by_idx = summary_doc["reader_rss_slope_by_idx"]
    eq(len(by_idx), 8, "Longevity re-derived frozen: reader_rss_slope_by_idx reader count")
    pooled_slopes: list[float] = []
    for rec in by_idx.values():
        pooled_slopes.extend(rec["segment_slopes_kb_per_s"])
    eq(len(pooled_slopes), 10,
       "Longevity re-derived frozen: total fitted reader RSS segments (8 readers, 2 of "
       "which restarted once each, contributing a second segment)")
    reader_pooled = summary_doc["reader_rss_slope_pooled"]
    reader_median = statistics.median(pooled_slopes)
    reader_max = max(pooled_slopes)
    close(reader_median, reader_pooled["median_kb_per_s"], 1e-6,
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: recomputed median(all segment "
          "slopes across reader_rss_slope_by_idx) matches reader_rss_slope_pooled.median_kb_per_s")
    close(reader_max, reader_pooled["max_kb_per_s"], 1e-6,
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: recomputed max(all segment slopes across "
          "reader_rss_slope_by_idx) matches reader_rss_slope_pooled.max_kb_per_s")
    eq(reader_pooled["n_fitted_segments"], 10,
       "Longevity re-derived frozen: reader_rss_slope_pooled.n_fitted_segments")

    reader_median_kb = round(reader_median, 2)
    reader_max_kb = round(reader_max, 2)
    eq(reader_median_kb, 5.32, "Longevity re-derived frozen: reader within-life slope median, kB/s")
    eq(reader_max_kb, 47.07, "Longevity re-derived frozen: reader within-life slope max, kB/s")

    # --- the old (superseded) two-point first-vs-last figure, carried
    # through unchanged by the re-derivation for direct side-by-side
    # comparison against the new within-life figures above ---
    first_vs_last = summary_doc["summary"]["memory_slope_kb_per_s"]
    eq(round(first_vs_last, 3), 111.639,
       "Longevity re-derived frozen: summary.memory_slope_kb_per_s (carried over unchanged "
       "from the original manifest, per README's Gate E comparison table)")
    close(first_vs_last, 111.639263, 1e-3,
          f"{relpath(LONGEVITY_MANIFEST)}: summary.memory_slope_kb_per_s -- the re-derived "
          "file's copy of this field matches the original manifest's own value exactly "
          "(unchanged by the re-derivation)")
    first_vs_last_kb = round(first_vs_last, 1)
    eq(first_vs_last_kb, 111.6, "Longevity re-derived frozen: first-vs-last slope, kB/s")

    # --- commits/s mean and the arithmetic per-commit retention estimate ---
    drift = summary_doc["summary"]["drift"]
    throughput_start = drift["throughput_first_hour_avg"]
    throughput_end = drift["throughput_last_hour_avg"]
    eq(throughput_start, 24.03, "Longevity re-derived frozen: throughput_first_hour_avg, commits/s")
    eq(throughput_end, 16.412, "Longevity re-derived frozen: throughput_last_hour_avg, commits/s")
    commits_per_sec_mean = (throughput_start + throughput_end) / 2
    commits_per_sec_mean_1dp = round(commits_per_sec_mean, 1)
    eq(commits_per_sec_mean_1dp, 20.2,
       "Longevity re-derived frozen: mean(throughput_first_hour_avg, throughput_last_hour_avg)")

    # Arithmetic, not a measurement: the within-life median RSS slope
    # divided by the mean commit rate, giving a rough per-commit retention
    # estimate -- README's own prose does the same division with a
    # rounded ~20 commits/s ("3,165.6 / 20 ~ 158 KB retained per commit").
    bytes_per_commit_kb = writer_median / commits_per_sec_mean
    bytes_per_commit_kb_rounded = round(bytes_per_commit_kb)
    eq(bytes_per_commit_kb_rounded, 157,
       "Longevity derived (arithmetic, not measured): round(within-life median slope kB/s "
       "/ mean commits-per-s) -- README's own prose gives ~158 using a coarser rate of "
       "~20 commits/s; both land in the same ~157-158 KB/commit neighborhood")
    require(150 <= bytes_per_commit_kb <= 165,
            "Longevity derived: per-commit retention estimate lands in the same order of "
            "magnitude as the replay check's independently computed ~161.8 KB/generation "
            "figure below")

    # --- the aborted post-hoc replay check ---
    replay_doc = json.loads(LONGEVITY_REPLAY_CHECK.read_text(encoding="utf-8"))
    outcome = replay_doc["outcome"]
    eq(outcome, "aborted_oom", "Longevity replay-check frozen: outcome")

    attempt_1 = replay_doc["summary"]["attempt_1"]
    dmesg_line = attempt_1["dmesg_line"]
    dmesg_match = re.search(r"anon-rss:(\d+)kB", dmesg_line)
    require(dmesg_match is not None,
            f"{relpath(LONGEVITY_REPLAY_CHECK)}: summary.attempt_1.dmesg_line carries an "
            "anon-rss:<N>kB field")
    oom_rss_kb = int(dmesg_match.group(1))
    eq(oom_rss_kb, 82_997_140,
       "Longevity replay-check frozen: OOM-kill anon-rss, kB (parsed from the verbatim "
       "dmesg line, the only place this run records it)")
    oom_rss_gb = round(oom_rss_kb / 1e6, 1)
    eq(oom_rss_gb, 83.0,
       "Longevity replay-check: anon-rss kB / 1e6 (decimal kB -> GB), rounded")

    oom_generation = attempt_1["highest_manifest_generation_observed"]
    eq(oom_generation, 513_024,
       "Longevity replay-check frozen: highest_manifest_generation_observed at the kill")

    # Cross-check the file's own generation/batches-applied figures against
    # its own stated compaction-inference arithmetic (compact_every=500:
    # 500 batch-commit generations + 1 compact()-commit generation = 501
    # generations/cycle), rather than trusting either field on its own.
    compactions_inferred = attempt_1["compactions_inferred"]
    eq(compactions_inferred, 1024, "Longevity replay-check frozen: compactions_inferred")
    eq(compactions_inferred * 501, oom_generation,
       f"{relpath(LONGEVITY_REPLAY_CHECK)}: compactions_inferred * 501 "
       "(500 batch-commit generations + 1 compact()-commit generation per cycle) matches "
       "highest_manifest_generation_observed")
    batches_applied = attempt_1["batches_applied_inferred"]
    eq(batches_applied, 512_000, "Longevity replay-check frozen: batches_applied_inferred")
    eq(compactions_inferred * 500, batches_applied,
       f"{relpath(LONGEVITY_REPLAY_CHECK)}: compactions_inferred * 500 matches "
       "batches_applied_inferred")

    total_batches_dataset = replay_doc["dataset"]["total_batches"]
    eq(total_batches_dataset, 1_074_952,
       "Longevity replay-check frozen: dataset.total_batches (matches the soak's own "
       "summary.total_batches, checked in compute_longevity_soak above)")
    fraction_applied = batches_applied / total_batches_dataset
    eq(round(fraction_applied, 3), 0.476,
       "Longevity replay-check: batches_applied_inferred / dataset.total_batches")

    kb_per_generation = oom_rss_kb / oom_generation
    eq(round(kb_per_generation, 1), 161.8,
       "Longevity replay-check: anon-rss kB / highest_manifest_generation_observed")

    wall_s = attempt_1["wall_s"]
    eq(wall_s, 12_142.0, "Longevity replay-check frozen: attempt_1.wall_s")
    elapsed_h = wall_s / 3600.0
    eq(round(elapsed_h, 2), 3.37, "Longevity replay-check: wall_s / 3600")

    # --- emit macros ---
    m.add("osdiSoakWriterWithinLifeSlopeMedianKBps", f"{median_kb:.1f}",
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: median(writer_within_life_rss_slope."
          "per_life[*].slope_kb_per_s), 42 lives")
    m.add("osdiSoakWriterWithinLifeSlopeMinKBps", f"{min_kb:.1f}",
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: min(writer_within_life_rss_slope."
          "per_life[*].slope_kb_per_s)")
    m.add("osdiSoakWriterWithinLifeSlopeMaxKBps", f"{max_kb:.1f}",
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: max(writer_within_life_rss_slope."
          "per_life[*].slope_kb_per_s)")
    m.add("osdiSoakWriterLivesFitted", tex_num(writer_slope["n_lives_with_fit"]),
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: writer_within_life_rss_slope."
          "n_lives_with_fit, recomputed as len(per_life)")
    m.add("osdiSoakWriterLivesPositive", tex_num(n_positive),
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: recomputed count(per_life[*]."
          "slope_kb_per_s > noise_threshold_kb_per_s=5.0), matches the file's own "
          "n_positive_beyond_noise_5kb_s")
    m.add("osdiSoakWriterNoiseFloorKBps", tex_num(int(noise_floor)),
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: writer_within_life_rss_slope."
          "noise_threshold_kb_per_s")
    m.add("osdiSoakReaderWithinLifeSlopeMedianKBps", f"{reader_median_kb:.2f}",
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: median of every fitted segment slope "
          "across reader_rss_slope_by_idx (10 segments, 8 readers), matches "
          "reader_rss_slope_pooled.median_kb_per_s")
    m.add("osdiSoakReaderWithinLifeSlopeMaxKBps", f"{reader_max_kb:.2f}",
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: max of every fitted segment slope "
          "across reader_rss_slope_by_idx, matches reader_rss_slope_pooled.max_kb_per_s "
          "-- the short post-restart segment for reader 5")
    m.add("osdiSoakFirstVsLastSlopeKBps", f"{first_vs_last_kb:.1f}",
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: summary.memory_slope_kb_per_s -- the "
          "old two-point first-vs-last figure, carried over unchanged from the original "
          "manifest; SUPERSEDED by the within-life figures above for the memory-FAIL "
          "finding (see gate_e_report_rederived_2026-09-15.md)")
    m.add("osdiSoakCommitsPerSecMean", f"{commits_per_sec_mean_1dp:.1f}",
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: mean(summary.drift."
          "throughput_first_hour_avg, summary.drift.throughput_last_hour_avg)")
    m.add("osdiSoakBytesPerCommitLiveKB", tex_num(bytes_per_commit_kb_rounded),
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: derived (arithmetic, not measured): "
          "round(writer_within_life_rss_slope.median_kb_per_s / osdiSoakCommitsPerSecMean's "
          "unrounded mean) -- README's own prose gives ~158 KB/commit using a coarser "
          "~20 commits/s rate")
    m.add("osdiSoakReplayOutcome", outcome,
          f"{relpath(LONGEVITY_REPLAY_CHECK)}: outcome (text macro, not a number)")
    m.add("osdiSoakReplayOomRssGB", f"{oom_rss_gb:.1f}",
          f"{relpath(LONGEVITY_REPLAY_CHECK)}: summary.attempt_1.dmesg_line's "
          "anon-rss:<N>kB, parsed and divided by 1e6")
    m.add("osdiSoakReplayOomGeneration", tex_num(oom_generation),
          f"{relpath(LONGEVITY_REPLAY_CHECK)}: summary.attempt_1."
          "highest_manifest_generation_observed, cross-checked against "
          "compactions_inferred * 501")
    m.add("osdiSoakReplayFractionApplied", f"{round(fraction_applied, 3):.3f}",
          f"{relpath(LONGEVITY_REPLAY_CHECK)}: summary.attempt_1.batches_applied_inferred "
          "/ dataset.total_batches")
    m.add("osdiSoakReplayKBPerGeneration", f"{round(kb_per_generation, 1):.1f}",
          f"{relpath(LONGEVITY_REPLAY_CHECK)}: (dmesg anon-rss kB) / "
          "highest_manifest_generation_observed")
    m.add("osdiSoakReplayElapsedH", f"{round(elapsed_h, 2):.2f}",
          f"{relpath(LONGEVITY_REPLAY_CHECK)}: summary.attempt_1.wall_s / 3600")


def compute_longevity_soak_two(m: Macros) -> None:
    """Lane W2p: P-SOAK2, the post-fix second soak (commit ``eed91c0``,
    OSDI'27 plan Sec 4.3b) -- see benchmarks/longevity-v1/README.md's
    "Soak 2 (post-fix)" section. Unlike W2g, the replay/digest step
    actually completed (``digest_equal: True``) and the dedicated
    full-mode ``tgms check`` cleared the believed-versions-overlap class
    W2g found (0 findings here, vs. 13,714 there); the new, unpredicted
    finding is a reader-side OSError/StateError failure storm. No verdict
    macro -- Gate E's own PASS/FAIL/FLAG table is gate_e_report-2.md, not
    this script."""
    # --- whole-file digest checks against README.md's "Files added here" table ---
    eq(sha256_file(LONGEVITY_MANIFEST_TWO), LONGEVITY_MANIFEST_TWO_SHA256,
       f"{relpath(LONGEVITY_MANIFEST_TWO)}: sha256 matches README.md's Files-added-here table")
    eq(sha256_file(LONGEVITY_RECOVERIES_TWO), LONGEVITY_RECOVERIES_TWO_SHA256,
       f"{relpath(LONGEVITY_RECOVERIES_TWO)}: sha256 matches README.md's Files-added-here table")
    eq(sha256_file(LONGEVITY_GATE_E_REPORT_TWO), LONGEVITY_GATE_E_REPORT_TWO_SHA256,
       f"{relpath(LONGEVITY_GATE_E_REPORT_TWO)}: sha256 matches README.md's Files-added-here table")
    eq(sha256_file(LONGEVITY_WRITER_ERRORS_BY_LIFE_TWO), LONGEVITY_WRITER_ERRORS_BY_LIFE_TWO_SHA256,
       f"{relpath(LONGEVITY_WRITER_ERRORS_BY_LIFE_TWO)}: sha256 matches README.md's "
       "Files-added-here table")
    eq(sha256_file(LONGEVITY_READER_ERRORS_BY_CLASS_TWO), LONGEVITY_READER_ERRORS_BY_CLASS_TWO_SHA256,
       f"{relpath(LONGEVITY_READER_ERRORS_BY_CLASS_TWO)}: sha256 matches README.md's "
       "Files-added-here table")
    eq(sha256_file(LONGEVITY_RSS_SLOPES_TWO), LONGEVITY_RSS_SLOPES_TWO_SHA256,
       f"{relpath(LONGEVITY_RSS_SLOPES_TWO)}: sha256 matches README.md's Files-added-here table")
    eq(sha256_file(LONGEVITY_VERIFY_FULL_TWO), LONGEVITY_VERIFY_FULL_TWO_SHA256,
       f"{relpath(LONGEVITY_VERIFY_FULL_TWO)}: sha256 matches README.md's Files-added-here table")
    eq(sha256_file(LONGEVITY_COMPACTIONS_TWO), LONGEVITY_COMPACTIONS_TWO_SHA256,
       f"{relpath(LONGEVITY_COMPACTIONS_TWO)}: sha256 matches README.md's Files-added-here table "
       "(appended 2026-09-18)")
    eq(sha256_file(LONGEVITY_READER_ONSET_ROWS_TWO), LONGEVITY_READER_ONSET_ROWS_TWO_SHA256,
       f"{relpath(LONGEVITY_READER_ONSET_ROWS_TWO)}: sha256 matches README.md's Files-added-here "
       "table (appended 2026-09-18)")

    manifest = json.loads(LONGEVITY_MANIFEST_TWO.read_text(encoding="utf-8"))
    summary = manifest["summary"]

    # --- commit + duration ---
    eq(manifest["git_commit"], "eed91c0", "Soak2 frozen: measured commit")
    duration_s = manifest["config"]["duration_s"]
    eq(duration_s, 86400.0, "Soak2 frozen: configured soak duration_s")
    hours = duration_s / 3600.0
    eq(hours, 24.0, "Soak2: config.duration_s / 3600 is exactly 24 hours")

    # --- writer lives (4, up from W2g's 42 -- --restart-every 6h instead
    # of 30m, so a leak cannot hide behind frequent restarts) ---
    writer_totals = summary["writer_totals_all_lives"]
    writer_lives = writer_totals["lives"]
    eq(writer_lives, 4, "Soak2 frozen: summary.writer_totals_all_lives.lives")

    by_life = json.loads(LONGEVITY_WRITER_ERRORS_BY_LIFE_TWO.read_text(encoding="utf-8"))
    per_life = by_life["per_life"]
    eq(len(per_life), writer_lives,
       "Soak2: writer_error_counts_by_life-2.json per_life row count matches "
       "summary.writer_totals_all_lives.lives")

    # --- designed restarts (3 = 4 lives - 1), by cause, with recovery times ---
    recoveries_rows = load_jsonl(LONGEVITY_RECOVERIES_TWO)
    eq(len(recoveries_rows), 3, "Soak2 frozen: recoveries-2.jsonl row count")
    eq(len(recoveries_rows), summary["recoveries"],
       "Soak2: recoveries-2.jsonl row count matches summary.recoveries")
    require(all(r["kind"] == "designed" for r in recoveries_rows),
            "Soak2: every recovery row is the harness's own designed restart cycle")
    n_sigabrt = sum(1 for r in recoveries_rows if r["returncode"] == -6)
    n_exit137 = sum(1 for r in recoveries_rows if r["returncode"] == 137)
    eq(n_sigabrt, 2, "Soak2 frozen: SIGABRT (-6) recovery count")
    eq(n_exit137, 1, "Soak2 frozen: os._exit(137) recovery count")
    eq(n_sigabrt + n_exit137, len(recoveries_rows),
       "Soak2: every recovery row is one of exactly these two returncodes")
    eq(sorted(r["life_index"] for r in recoveries_rows), [0, 1, 2],
       "Soak2 frozen: the restart cycle kills the ends of lives 0, 1, 2 (life 3 is "
       "still running at RUN_DONE)")
    recovery_times = sorted(r["recovery_s"] for r in recoveries_rows)
    close(recovery_times[0], 17.505, 1e-2, "Soak2 frozen: shortest recovery time, s")
    close(recovery_times[1], 29.130, 1e-2, "Soak2 frozen: middle recovery time, s")
    close(recovery_times[2], 43.510, 1e-2, "Soak2 frozen: longest recovery time, s")
    unexpected = summary["unexpected_writer_deaths"]
    eq(unexpected, 0, "Soak2 frozen: unexpected_writer_deaths")
    reader_restarts = summary["reader_restarts"]
    eq(reader_restarts, 0,
       "Soak2 frozen: summary.reader_restarts (no reader_restarts.jsonl counterpart "
       "for this soak per README.md -- one is never written when it would be empty)")

    require(summary["verify_healthy"] is True,
            "Soak2: summary.verify_healthy is the JSON literal true")

    # --- digest_equal: unlike W2g, the replay/digest step actually ran ---
    require(summary["digest_equal"] is True,
            "Soak2: summary.digest_equal is the JSON literal true -- the replay/digest "
            "step actually completed this time, unlike W2g's disk-guard skip")
    require(summary["replay_skipped"] is None,
            "Soak2: summary.replay_skipped is JSON null -- the replay was not skipped")

    total_batches = summary["total_batches"]
    eq(total_batches, 1_891_962, "Soak2 frozen: summary.total_batches (the replay's own "
       "batch count, now that digest_equal actually computed)")

    # --- writer within-life RSS slopes per life, and the whole-run
    # first-vs-last figure that the frozen bound does NOT gate on ---
    rss_doc = json.loads(LONGEVITY_RSS_SLOPES_TWO.read_text(encoding="utf-8"))
    writer_life_rows = rss_doc["writer_lives"]
    eq(len(writer_life_rows), writer_lives,
       "Soak2: rss_slopes-2.json writer_lives row count matches "
       "summary.writer_totals_all_lives.lives")
    writer_life_slopes = [row["slope_kb_per_s_least_squares"] for row in writer_life_rows]
    require(list(range(writer_lives)) == [row["life"] for row in writer_life_rows],
            "Soak2: rss_slopes-2.json writer_lives rows are ordered life 0, 1, 2, 3")
    life0, life1, life2, life3 = writer_life_slopes
    eq(round(life0, 3), 43.494, "Soak2 frozen: writer life 0 within-life RSS slope, kB/s")
    eq(round(life1, 3), 32.365, "Soak2 frozen: writer life 1 within-life RSS slope, kB/s")
    eq(round(life2, 3), 20.978, "Soak2 frozen: writer life 2 within-life RSS slope, kB/s")
    eq(round(life3, 3), 23.564, "Soak2 frozen: writer life 3 within-life RSS slope, kB/s")
    frozen_bound_writer = rss_doc["frozen_bound_writer_kb_per_s"]
    eq(frozen_bound_writer, 50, "Soak2 frozen: rss_slopes-2.json frozen_bound_writer_kb_per_s")
    require(all(s <= frozen_bound_writer for s in writer_life_slopes),
            "Soak2: every writer life's within-life RSS slope clears the frozen 50 kB/s bound")
    writer_median = statistics.median(writer_life_slopes)
    writer_min = min(writer_life_slopes)
    writer_max = max(writer_life_slopes)
    median_kb = round(writer_median, 3)
    min_kb = round(writer_min, 3)
    max_kb = round(writer_max, 3)
    eq(median_kb, 27.965, "Soak2 frozen: writer within-life slope median, kB/s")
    eq(min_kb, 20.978, "Soak2 frozen: writer within-life slope min, kB/s")
    eq(max_kb, 43.494, "Soak2 frozen: writer within-life slope max, kB/s")

    first_vs_last = summary["memory_slope_kb_per_s"]
    eq(round(first_vs_last, 3), 56.275,
       "Soak2 frozen: summary.memory_slope_kb_per_s (whole-run first-vs-last, FAILs the "
       "frozen bound even though every within-life fit above PASSes it)")

    # --- reader within-life RSS slopes: 8 readers, reader_restarts=0 so
    # exactly one fitted segment each ---
    reader_rows = rss_doc["readers"]
    eq(len(reader_rows), 8, "Soak2 frozen: rss_slopes-2.json readers row count")
    reader_slopes = [row["slope_kb_per_s_least_squares"] for row in reader_rows.values()]
    frozen_bound_reader = rss_doc["frozen_bound_reader_kb_per_s"]
    eq(frozen_bound_reader, 10, "Soak2 frozen: rss_slopes-2.json frozen_bound_reader_kb_per_s")
    require(all(s <= frozen_bound_reader for s in reader_slopes),
            "Soak2: every reader's within-life RSS slope clears the frozen 10 kB/s bound, "
            "including the readers hit hardest by the OSError storm below")
    reader_min = min(reader_slopes)
    reader_max = max(reader_slopes)
    reader_min_kb = round(reader_min, 3)
    reader_max_kb = round(reader_max, 3)
    eq(reader_min_kb, 7.212, "Soak2 frozen: reader within-life slope min, kB/s")
    eq(reader_max_kb, 8.098, "Soak2 frozen: reader within-life slope max, kB/s")

    # --- writer errors: true total (616), cross-checked against the
    # manifest's own writer_totals_all_lives.errors, and by exception class ---
    per_life_errors = [row["errors"] for row in per_life.values()]
    true_writer_errors = sum(per_life_errors)
    eq(true_writer_errors, 616, "Soak2 frozen: true total writer errors, summed over 4 lives")
    eq(true_writer_errors, by_life["true_total_writer_errors_all_lives"],
       "Soak2: recomputed sum(per_life[*].errors) matches the file's own "
       "true_total_writer_errors_all_lives field")
    eq(true_writer_errors, writer_totals["errors"],
       "Soak2: recomputed sum(per_life[*].errors) matches manifest's own "
       "writer_totals_all_lives.errors -- unlike W2g, this manifest's own cumulative "
       "counter is correct")
    writer_error_classes: set[str] = set()
    for row in per_life.values():
        writer_error_classes.update(row["errors_by_class"].keys())
    eq(writer_error_classes, {"NotFoundError"},
       "Soak2 frozen: every writer error across all 4 lives is NotFoundError")
    writer_errors_by_class_total = sum(
        row["errors_by_class"].get("NotFoundError", 0) for row in per_life.values())
    eq(writer_errors_by_class_total, true_writer_errors,
       "Soak2: sum(per_life[*].errors_by_class.NotFoundError) matches the true writer total "
       "(NotFoundError is the only class)")

    # --- reader errors: the headline finding -- 150,476,512 total, split
    # OSError/StateError, cross-checked against the manifest and against
    # the harness's now-correct error_count arithmetic ---
    reader_by_class = json.loads(LONGEVITY_READER_ERRORS_BY_CLASS_TWO.read_text(encoding="utf-8"))
    by_class_total = reader_by_class["by_class_total"]
    reader_oserror = int(by_class_total["OSError"])
    reader_stateerror = int(by_class_total["StateError"])
    eq(reader_oserror, 150_278_720, "Soak2 frozen: reader OSError total")
    eq(reader_stateerror, 197_792, "Soak2 frozen: reader StateError total")
    true_reader_errors = reader_oserror + reader_stateerror
    eq(true_reader_errors, int(reader_by_class["total_reader_errors_total"]),
       "Soak2: recomputed OSError + StateError matches "
       "reader_error_counts_by_class-2.json's own total_reader_errors_total field")
    eq(true_reader_errors, summary["reader_errors_total"],
       "Soak2: recomputed reader error total matches manifest's own reader_errors_total")

    reader_queries = summary["reader_queries_total"]
    eq(reader_queries, 16_055_124, "Soak2 frozen: summary.reader_queries_total")

    # --- the harness's own error_count arithmetic, now correct (unlike W2g) ---
    manifest_error_count = summary["error_count"]
    eq(manifest_error_count, 150_477_128, "Soak2 frozen: summary.error_count")
    eq(manifest_error_count, true_writer_errors + true_reader_errors + unexpected,
       "Soak2: summary.error_count == true writer errors + true reader errors + "
       "unexpected_writer_deaths -- this run's own cumulative counter is correct, "
       "unlike W2g's counter_latest label-collision defect")

    # --- throughput / p99 drift, first hour vs. last hour ---
    drift = summary["drift"]
    throughput_start = drift["throughput_first_hour_avg"]
    throughput_end = drift["throughput_last_hour_avg"]
    eq(throughput_start, 39.91, "Soak2 frozen: first-hour throughput, commits/s")
    eq(throughput_end, 19.117, "Soak2 frozen: last-hour throughput, commits/s")
    p99_start = drift["commit_p99_first_hour_max"]
    p99_end = drift["commit_p99_last_hour_max"]
    eq(p99_start, 60.42, "Soak2 frozen: first-hour commit p99, ms")
    eq(p99_end, 82.075, "Soak2 frozen: last-hour commit p99, ms")

    # --- metadata growth slopes ---
    growth = summary["metadata_growth_slope_bytes_per_s"]
    manifest_growth = round(growth["manifests"], 3)
    segment_growth = round(growth["segments"], 3)
    eq(manifest_growth, 3.141, "Soak2 frozen: manifest-bytes growth slope, B/s")
    eq(segment_growth, 2396.455, "Soak2 frozen: segment-bytes growth slope, B/s")

    # --- compactions ---
    compactions = summary["compactions"]
    eq(compactions, 4_215, "Soak2 frozen: summary.compactions")

    # --- final entity count: cross-checked between the manifest's own
    # field and the independent full-mode verify side-file's dictionary-
    # record count (this macro cites the manifest field; both agree) ---
    entities_end = summary["final_stats"]["n_entities"]
    eq(entities_end, 3_092_488, "Soak2 frozen: summary.final_stats.n_entities")

    # --- full-mode tgms check: 0 believed-versions-overlap findings (vs.
    # W2g's 13,714), and the verdict text ---
    verify_text = LONGEVITY_VERIFY_FULL_TWO.read_text(encoding="utf-8")
    require("verdict: healthy" in verify_text,
            f"{relpath(LONGEVITY_VERIFY_FULL_TWO)}: verdict line reads healthy")
    problems_match = re.search(r"PROBLEMS \((\d+)\):", verify_text)
    overlap_count = int(problems_match.group(1)) if problems_match else 0
    eq(overlap_count, 0,
       f"{relpath(LONGEVITY_VERIFY_FULL_TWO)}: PROBLEMS count (0 -- no PROBLEMS section "
       "at all, consistent with the healthy verdict)")
    require("believed-versions-overlap" not in verify_text,
            f"{relpath(LONGEVITY_VERIFY_FULL_TWO)}: no believed-versions-overlap findings "
            "text present anywhere in the file")
    verify_generation_match = re.search(r"generation:\s*(\d+)", verify_text)
    require(verify_generation_match is not None,
            f"{relpath(LONGEVITY_VERIFY_FULL_TWO)}: carries a generation: <N> line")
    verify_generation = int(verify_generation_match.group(1))
    eq(verify_generation, 1_894_946, "Soak2 frozen: verify-full-soak2 generation")
    require(verify_generation >= int(summary["generation_final"]),
            "Soak2: full-verify's generation is at/after the manifest's own "
            "generation_final -- the check ran against a store no older than RUN_DONE")
    verify_entities_match = re.search(r"(\d+) dictionary records", verify_text)
    require(verify_entities_match is not None,
            f"{relpath(LONGEVITY_VERIFY_FULL_TWO)}: carries a '<N> dictionary records' line")
    verify_entities = int(verify_entities_match.group(1))
    eq(verify_entities, entities_end,
       "Soak2: verify-full-soak2's dictionary-records count matches manifest's "
       "final_stats.n_entities -- two independent sources for the same figure")

    # --- emit macros ---
    m.add("osdiSoakCommitTwo", manifest["git_commit"],
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: git_commit")
    m.add("osdiSoakHoursTwo", tex_num(int(hours)),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: config.duration_s / 3600")
    m.add("osdiSoakWriterLivesTwo", tex_num(writer_lives),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.writer_totals_all_lives.lives")
    m.add("osdiSoakRecoveriesTwo", tex_num(len(recoveries_rows)),
          f"{relpath(LONGEVITY_RECOVERIES_TWO)}: row count, == summary.recoveries")
    m.add("osdiSoakRecoveriesSigabrtTwo", tex_num(n_sigabrt),
          f"{relpath(LONGEVITY_RECOVERIES_TWO)}: rows with returncode == -6 (SIGABRT)")
    m.add("osdiSoakRecoveriesExit137Two", tex_num(n_exit137),
          f"{relpath(LONGEVITY_RECOVERIES_TWO)}: rows with returncode == 137 (os._exit(137))")
    m.add("osdiSoakUnexpectedRecoveriesTwo", tex_num(unexpected),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.unexpected_writer_deaths")
    m.add("osdiSoakWriterWithinLifeSlopeMedianKBpsTwo", f"{median_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_TWO)}: median(writer_lives[*]."
          "slope_kb_per_s_least_squares), 4 lives")
    m.add("osdiSoakWriterWithinLifeSlopeMinKBpsTwo", f"{min_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_TWO)}: min(writer_lives[*]."
          "slope_kb_per_s_least_squares)")
    m.add("osdiSoakWriterWithinLifeSlopeMaxKBpsTwo", f"{max_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_TWO)}: max(writer_lives[*]."
          "slope_kb_per_s_least_squares)")
    m.add("osdiSoakReaderWithinLifeSlopeMinKBpsTwo", f"{reader_min_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_TWO)}: min(readers[*].slope_kb_per_s_least_squares), "
          "8 readers (reader_restarts=0, one fitted segment each)")
    m.add("osdiSoakReaderWithinLifeSlopeMaxKBpsTwo", f"{reader_max_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_TWO)}: max(readers[*].slope_kb_per_s_least_squares)")
    m.add("osdiSoakDigestEqualTwo", "true",
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.digest_equal is the JSON literal "
          "true -- the replay/digest step actually completed, unlike W2g's disk-guard skip")
    m.add("osdiSoakBatchesTwo", tex_num(total_batches),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.total_batches")
    m.add("osdiSoakFullVerifyOverlapCountTwo", tex_num(overlap_count),
          f"{relpath(LONGEVITY_VERIFY_FULL_TWO)}: PROBLEMS count (believed-versions-overlap "
          "class), 0 vs. W2g's 13,714 on the pre-fix store")
    m.add("osdiSoakFullVerifyVerdictTwo", "healthy",
          f"{relpath(LONGEVITY_VERIFY_FULL_TWO)}: verdict line (text macro, not a number)")
    m.add("osdiSoakWriterErrorsTrueTwo", tex_num(true_writer_errors),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_LIFE_TWO)}: sum(per_life[*].errors), 4 lives, "
          "cross-checked against manifest's own writer_totals_all_lives.errors")
    m.add("osdiSoakWriterErrorsClassTwo", "NotFoundError",
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_LIFE_TWO)}: the sole exception class across "
          "every writer error in all 4 lives (text macro, not a number)")
    m.add("osdiSoakReaderErrorsTrueTwo", tex_num(true_reader_errors),
          f"{relpath(LONGEVITY_READER_ERRORS_BY_CLASS_TWO)}: OSError + StateError totals, "
          "cross-checked against manifest's own reader_errors_total")
    m.add("osdiSoakReaderErrorsOSErrorTwo", tex_num(reader_oserror),
          f"{relpath(LONGEVITY_READER_ERRORS_BY_CLASS_TWO)}: by_class_total.OSError -- new "
          "to this soak, 0 in W2g's metrics.jsonl")
    m.add("osdiSoakReaderErrorsStateErrorTwo", tex_num(reader_stateerror),
          f"{relpath(LONGEVITY_READER_ERRORS_BY_CLASS_TWO)}: by_class_total.StateError")
    m.add("osdiSoakReaderQueriesTwo", tex_num(reader_queries),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.reader_queries_total")
    m.add("osdiSoakThroughputStartTwo", str(throughput_start),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.drift.throughput_first_hour_avg, "
          "commits/s")
    m.add("osdiSoakThroughputEndTwo", str(throughput_end),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.drift.throughput_last_hour_avg, "
          "commits/s")
    m.add("osdiSoakP99StartMsTwo", str(p99_start),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.drift.commit_p99_first_hour_max, ms")
    m.add("osdiSoakP99EndMsTwo", str(p99_end),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.drift.commit_p99_last_hour_max, ms")
    m.add("osdiSoakManifestGrowthBpsTwo", f"{manifest_growth:.3f}",
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.metadata_growth_slope_bytes_per_s."
          "manifests")
    m.add("osdiSoakSegmentGrowthBpsTwo", f"{segment_growth:,.3f}".replace(",", "{,}"),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.metadata_growth_slope_bytes_per_s."
          "segments")
    m.add("osdiSoakEntitiesEndTwo", tex_num(entities_end),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.final_stats.n_entities, "
          "cross-checked against verify-full-soak2-2026-09-17.txt's own dictionary-records "
          "count (both agree)")
    m.add("osdiSoakCompactionsTwo", tex_num(compactions),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.compactions")

    # --- reader-onset edge-row counts (added 2026-09-18, lane W2v) --
    # reader_onset_rows-2.json reconciles compactions-2.jsonl's writer-
    # perf_counter clock against reader_error_counts_by_class-2.json's
    # wall-clock onset_t_plus_s via metrics.jsonl's compactions_total
    # counter (see the file's own "method" field for the full procedure
    # and its stated mapping-error bound); cross-checked here against the
    # onset source file's own osrror_storm_onset_by_reader, not only
    # against frozen constants.
    onset_rows = json.loads(LONGEVITY_READER_ONSET_ROWS_TWO.read_text(encoding="utf-8"))
    onset_by_reader = reader_by_class["osrror_storm_onset_by_reader"]
    earliest = onset_rows["earliest_reader_onset"]
    latest = onset_rows["latest_reader_onset"]
    eq(earliest["reader"], 6, "Soak2 frozen: earliest reader-error onset is reader 6")
    eq(latest["reader"], 4, "Soak2 frozen: latest reader-error onset is reader 4")
    eq(earliest["onset_t_plus_s"], onset_by_reader[str(earliest["reader"])]["onset_t_plus_s"],
       "Soak2: reader_onset_rows-2.json earliest onset_t_plus_s matches "
       "reader_error_counts_by_class-2.json's own osrror_storm_onset_by_reader")
    eq(latest["onset_t_plus_s"], onset_by_reader[str(latest["reader"])]["onset_t_plus_s"],
       "Soak2: reader_onset_rows-2.json latest onset_t_plus_s matches "
       "reader_error_counts_by_class-2.json's own osrror_storm_onset_by_reader")
    require(earliest["onset_t_plus_s"] == min(v["onset_t_plus_s"] for v in onset_by_reader.values()),
            "Soak2: reader_onset_rows-2.json's earliest onset really is the minimum "
            "onset_t_plus_s across all 8 readers")
    require(latest["onset_t_plus_s"] == max(v["onset_t_plus_s"] for v in onset_by_reader.values()),
            "Soak2: reader_onset_rows-2.json's latest onset really is the maximum "
            "onset_t_plus_s across all 8 readers")
    onset_earliest_s = earliest["onset_t_plus_s"]
    onset_latest_s = latest["onset_t_plus_s"]
    eq(onset_earliest_s, 61417.1, "Soak2 frozen: earliest reader-error onset, s into the run")
    eq(onset_latest_s, 75993.1, "Soak2 frozen: latest reader-error onset, s into the run")
    onset_earliest_rows = earliest["last_compaction_before"]["edge_rows"]
    onset_latest_rows = latest["last_compaction_before"]["edge_rows"]
    eq(onset_earliest_rows, 2_235_044,
       "Soak2 frozen: edge_rows of the last compaction before the earliest reader onset")
    eq(onset_latest_rows, 2_450_815,
       "Soak2 frozen: edge_rows of the last compaction before the latest reader onset")
    require(onset_earliest_rows < onset_latest_rows,
            "Soak2: edge-row count grows from the earliest to the latest reader-error onset, "
            "consistent with a monotonically growing store")

    m.add("osdiSoakReaderOnsetEarliestRowsTwo", tex_num(onset_earliest_rows),
          f"{relpath(LONGEVITY_READER_ONSET_ROWS_TWO)}: earliest_reader_onset."
          "last_compaction_before.edge_rows -- the last compaction (compactions-2.jsonl) "
          "before reader 6's onset_t_plus_s=61417.1s, mapped from that file's own "
          "perf_counter clock onto reader_error_counts_by_class-2.json's wall-clock "
          "onset via metrics.jsonl's compactions_total counter (method and the mapping's "
          "own error bound, ~60-72s / up to 3 ambiguous rows, stated in the side-file)")
    m.add("osdiSoakReaderOnsetLatestRowsTwo", tex_num(onset_latest_rows),
          f"{relpath(LONGEVITY_READER_ONSET_ROWS_TWO)}: latest_reader_onset."
          "last_compaction_before.edge_rows -- the last compaction before reader 4's "
          "onset_t_plus_s=75993.1s, same reconciliation method as "
          "osdiSoakReaderOnsetEarliestRowsTwo")
    m.add("osdiSoakReaderOnsetEarliestSTwo", f"{onset_earliest_s:.1f}",
          f"{relpath(LONGEVITY_READER_ERRORS_BY_CLASS_TWO)}: "
          "osrror_storm_onset_by_reader.6.onset_t_plus_s, s into the run (RUN_STARTED "
          "2026-09-16T00:53:01Z) -- the minimum onset_t_plus_s across all 8 readers")
    m.add("osdiSoakReaderOnsetLatestSTwo", f"{onset_latest_s:.1f}",
          f"{relpath(LONGEVITY_READER_ERRORS_BY_CLASS_TWO)}: "
          "osrror_storm_onset_by_reader.4.onset_t_plus_s, s into the run -- the maximum "
          "onset_t_plus_s across all 8 readers")


# --------------------------------------------------------------------------
# W-lane -- the original soak's full-mode verify (benchmarks/longevity-v1/
# verify-full-2026-09-15.txt, two `tgms check` entries concatenated: the
# pre-replay check of the original store, then the post-hoc full verify of
# REPLAY-2's own replayed store) and REPLAY-2 itself
# (replay-check-2-2026-09.json, the post-D-087-fix replay that completed).
# Both are read-only re-measurements against the W2g soak's own preserved
# raw inputs (the eventlog), same discipline as compute_longevity_rederived
# above. No verdict macro -- "CORRUPT" here is the verifier's own verdict
# string, reported as a text macro, not scored by this script.
# --------------------------------------------------------------------------

def compute_longevity_verify_and_replay2(m: Macros) -> None:
    eq(sha256_file(LONGEVITY_VERIFY_FULL), LONGEVITY_VERIFY_FULL_SHA256,
       f"{relpath(LONGEVITY_VERIFY_FULL)}: sha256 matches this lane's frozen value")
    eq(sha256_file(LONGEVITY_REPLAY_CHECK_2), LONGEVITY_REPLAY_CHECK_2_SHA256,
       f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: sha256 matches this lane's frozen value")

    text = LONGEVITY_VERIFY_FULL.read_text(encoding="utf-8")

    # The file is two `tgms check --mode full` runs concatenated (the
    # original soak store, then REPLAY-2's replayed store, appended
    # unedited per README.md's "Post-hoc replay check -- attempt 3"
    # section) -- parse both header blocks and both verdict lines rather
    # than trusting the header count alone.
    generations = [int(g) for g in re.findall(r"^generation:\s*(\d+)", text, re.MULTILINE)]
    problem_counts = [int(n) for n in re.findall(r"^PROBLEMS \((\d+)\):", text, re.MULTILINE)]
    verdicts = re.findall(r"^verdict:\s*(\S+)", text, re.MULTILINE)
    bullet_counts = [
        len(re.findall(r"^\s*-\s*\[row/believed-versions-overlap\]:", block, re.MULTILINE))
        for block in re.split(r"^verdict:.*$", text, flags=re.MULTILINE)[:-1]
    ]

    eq(len(generations), 2, "Longevity verify-full frozen: two `generation:` header lines "
       "(one per tgms-check run)")
    eq(len(problem_counts), 2, "Longevity verify-full frozen: two `PROBLEMS (N):` headers")
    eq(len(verdicts), 2, "Longevity verify-full frozen: two `verdict:` lines")
    eq(bullet_counts, problem_counts,
       "Longevity verify-full: recomputed row/believed-versions-overlap bullet count per "
       "run matches that run's own PROBLEMS(N) header")

    orig_generation, replay_generation = generations
    orig_problems, replay_problems = problem_counts
    orig_verdict, replay_verdict = verdicts

    eq(orig_generation, 1_076_872, "Longevity verify-full frozen: original store's generation")
    eq(replay_generation, 1_076_598,
       "Longevity verify-full frozen: REPLAY-2 store's generation")
    eq(orig_problems, 13_714,
       "Longevity verify-full frozen: original store's row/believed-versions-overlap count")
    eq(replay_problems, 13_714,
       "Longevity verify-full frozen: REPLAY-2 store's row/believed-versions-overlap count "
       "-- identical to the original store's, the same corpus/ingest-semantics defect "
       "(disc \"#0\"), not a storage/compaction/replay-introduced one")
    eq(orig_problems, replay_problems,
       "Longevity verify-full: the two runs' problem counts are identical")
    require(orig_verdict == "CORRUPT" and replay_verdict == "CORRUPT",
            "Longevity verify-full frozen: both runs' verdict is CORRUPT")

    replay = json.loads(LONGEVITY_REPLAY_CHECK_2.read_text(encoding="utf-8"))
    eq(replay["git_commit"], "a6b3e94",
       "Longevity REPLAY-2 frozen: git_commit (post-D-087-fix worktree)")
    eq(replay["outcome"], "completed", "Longevity REPLAY-2 frozen: outcome")
    summary = replay["summary"]
    eq(summary["digest_equal"], True, "Longevity REPLAY-2 frozen: summary.digest_equal")
    eq(replay["result_digest"], summary["predicted_digest"],
       "Longevity REPLAY-2: the manifest's own top-level result_digest matches "
       "summary.predicted_digest (the soak's pre-registered final_digest)")
    eq(replay["dataset"]["total_batches"], 1_074_952,
       "Longevity REPLAY-2 frozen: dataset.total_batches (matches the soak's own "
       "summary.total_batches, W2g's osdiSoakBatches)")
    batches_applied = summary["batches_applied"]
    eq(batches_applied, 1_074_450, "Longevity REPLAY-2 frozen: summary.batches_applied")
    compactions_inferred = summary["compactions_inferred"]
    eq(compactions_inferred, 2_148, "Longevity REPLAY-2 frozen: summary.compactions_inferred")
    eq(summary["manifest_current_generation"] - batches_applied, compactions_inferred,
       "Longevity REPLAY-2: manifest_current_generation - batches_applied matches "
       "compactions_inferred (the file's own compaction_inference_basis arithmetic)")
    eq(summary["manifest_current_generation"], replay_generation,
       "Longevity REPLAY-2: summary.manifest_current_generation matches "
       "verify-full-2026-09-15.txt's own (second-entry) generation header")

    wall_s = summary["wall_s"]
    eq(wall_s, 30_015.0, "Longevity REPLAY-2 frozen: summary.wall_s")
    elapsed_h = round(wall_s / 3600.0, 2)
    eq(elapsed_h, 8.34, "Longevity REPLAY-2: wall_s / 3600, hours")

    peak_rss_kb = summary["peak_rss_kb"]
    eq(peak_rss_kb, 4_329_996, "Longevity REPLAY-2 frozen: summary.peak_rss_kb")
    peak_rss_gb = round(peak_rss_kb / 1e6, 2)
    eq(peak_rss_gb, 4.33, "Longevity REPLAY-2: peak_rss_kb / 1e6, GB")

    peak_disk_mb = summary["peak_disk_mb_observed"]
    eq(peak_disk_mb, 586, "Longevity REPLAY-2 frozen: summary.peak_disk_mb_observed")

    rss_series = summary["rss_series"]
    eq(len(rss_series), 101, "Longevity REPLAY-2 frozen: summary.rss_series sample count")
    recomputed_peak_kb = max(s["rss_kb"] for s in rss_series)
    eq(recomputed_peak_kb, peak_rss_kb,
       "Longevity REPLAY-2: recomputed max(rss_series[*].rss_kb) matches "
       "summary.peak_rss_kb")

    result_digest = replay["result_digest"]
    require(result_digest == summary["predicted_digest"],
            "Longevity REPLAY-2: top-level result_digest matches summary.predicted_digest "
            "(the digest equality this record measures)")
    digest_prefix = result_digest[:8]
    eq(digest_prefix, "8eb9bc26", "Longevity REPLAY-2 frozen: result_digest's first 8 hex "
       "characters")

    fv = summary["full_verify_of_replayed_store"]
    eq(fv["problems"], 13_714,
       "Longevity REPLAY-2 frozen: summary.full_verify_of_replayed_store.problems")
    eq(fv["problems"], replay_problems,
       "Longevity REPLAY-2: summary.full_verify_of_replayed_store.problems matches "
       "verify-full-2026-09-15.txt's own (second-entry) PROBLEMS(N) header")
    eq(fv["verdict"], "CORRUPT",
       "Longevity REPLAY-2 frozen: summary.full_verify_of_replayed_store.verdict")

    # --- emit macros ---
    m.add("osdiSoakVerifyFullGeneration", tex_num(orig_generation),
          f"{relpath(LONGEVITY_VERIFY_FULL)}: first entry's generation: header (the "
          "original, pre-replay soak store)")
    m.add("osdiSoakVerifyFullOverlapCount", tex_num(orig_problems),
          f"{relpath(LONGEVITY_VERIFY_FULL)}: first entry's PROBLEMS(N) header, "
          "recomputed as len(row/believed-versions-overlap bullets) in that entry")
    m.add("osdiSoakVerifyFullVerdict", orig_verdict,
          f"{relpath(LONGEVITY_VERIFY_FULL)}: first entry's verdict: line (text macro, "
          "not a number)")
    m.add("osdiSoakReplay2VerifyGeneration", tex_num(replay_generation),
          f"{relpath(LONGEVITY_VERIFY_FULL)}: second entry's generation: header (REPLAY-2's "
          "replayed store), cross-checked against replay-check-2-2026-09.json's own "
          "summary.manifest_current_generation")
    m.add("osdiSoakReplay2VerifyOverlapCount", tex_num(replay_problems),
          f"{relpath(LONGEVITY_VERIFY_FULL)}: second entry's PROBLEMS(N) header -- "
          "identical to osdiSoakVerifyFullOverlapCount, cross-checked against "
          "replay-check-2-2026-09.json's own summary.full_verify_of_replayed_store.problems")
    m.add("osdiSoakReplay2VerifyVerdict", replay_verdict,
          f"{relpath(LONGEVITY_VERIFY_FULL)}: second entry's verdict: line (text macro, "
          "not a number)")
    m.add("osdiSoakReplay2BatchesApplied", tex_num(batches_applied),
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: summary.batches_applied, of "
          "dataset.total_batches (== osdiSoakBatches)")
    m.add("osdiSoakReplay2Compactions", tex_num(compactions_inferred),
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: summary.compactions_inferred, "
          "cross-checked against manifest_current_generation - batches_applied")
    m.add("osdiSoakReplay2WallS", tex_num(int(wall_s)),
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: summary.wall_s")
    m.add("osdiSoakReplay2ElapsedH", f"{elapsed_h:.2f}",
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: summary.wall_s / 3600, hours (≈ 8h20m)")
    m.add("osdiSoakReplay2PeakRssKB", tex_num(peak_rss_kb),
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: summary.peak_rss_kb, recomputed as "
          "max(summary.rss_series[*].rss_kb)")
    m.add("osdiSoakReplay2PeakRssGB", f"{peak_rss_gb:.2f}",
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: summary.peak_rss_kb / 1e6, GB")
    m.add("osdiSoakReplay2PeakDiskMB", tex_num(peak_disk_mb),
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: summary.peak_disk_mb_observed")
    m.add("osdiSoakReplay2RssSamples", tex_num(len(rss_series)),
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: len(summary.rss_series)")
    m.add("osdiSoakReplay2DigestEqual", "true",
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: summary.digest_equal is the JSON literal "
          "true (text macro, not a number)")
    m.add("osdiSoakReplay2DigestPrefix", digest_prefix,
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: result_digest[:8] (== summary."
          "predicted_digest[:8], the soak's pre-registered final_digest) (text macro, "
          "not a number)")


# --------------------------------------------------------------------------
# W2u -- P-STORM-HUNT, the 6h observation-only run (commit 57952fa)
# --------------------------------------------------------------------------

def compute_longevity_soak_hunt(m: Macros) -> None:
    """Lane W2u: P-STORM-HUNT, a 6h observation-only run (commit
    ``57952fa``, single writer life, ``--restart-every`` unset) whose only
    job was to see whether P-SOAK2's reader-side ``OSError``/``StateError``
    failure storm would recur under the D-088 bounded reader-error-message
    capture merged at ``c3a5592`` -- see README.md's "P-STORM-HUNT (6 h
    observation run, 2026-09-17/18)" section. It did not recur (0 reader
    errors, 0 ``reader_op_error`` events); all 175 errors this run reported
    are the same writer-side ``NotFoundError`` correction-race class
    documented for both soaks. Unlike P-SOAK2, both reader RSS slopes
    (11.4-12.5 kB/s) exceed the 10 kB/s frozen bound. No verdict macro --
    Gate E's own PASS/FAIL/FLAG table is gate_e_report-stormhunt.md, not
    this script; ``osdiSoakHuntPatternReproduced`` states the
    pre-registration's own clause (e) non-conclusion in its provenance,
    not as a scored number.
    """
    readme_text = LONGEVITY_README.read_text(encoding="utf-8")
    section = _readme_section(readme_text, "P-STORM-HUNT")
    readme_sha = _readme_sha256_table(section)

    for path in (LONGEVITY_MANIFEST_HUNT, LONGEVITY_RSS_SLOPES_HUNT,
                 LONGEVITY_WRITER_ERRORS_BY_CLASS_HUNT, LONGEVITY_HOST_LOAD_HUNT,
                 LONGEVITY_READER_OP_ERROR_HUNT, LONGEVITY_READER_OP_ERROR_README_HUNT,
                 LONGEVITY_GATE_E_REPORT_HUNT, LONGEVITY_BUILD_INFO_HUNT):
        name = path.name
        require(name in readme_sha,
                f"{relpath(path)}: {name} not found in README.md's P-STORM-HUNT "
                "Files-added-here table")
        if name in readme_sha:
            eq(sha256_file(path), readme_sha[name],
               f"{relpath(path)}: sha256 matches README.md's P-STORM-HUNT "
               "Files-added-here table")

    # --- commit, duration, single writer life, no designed restarts ---
    manifest = json.loads(LONGEVITY_MANIFEST_HUNT.read_text(encoding="utf-8"))
    summary = manifest["summary"]
    eq(manifest["git_commit"], "57952fa", "StormHunt frozen: measured commit")
    config = manifest["config"]
    duration_s = config["duration_s"]
    eq(duration_s, 21_600.0, "StormHunt frozen: configured soak duration_s (6h)")
    eq(config["restart_every_s"], 0.0,
       "StormHunt frozen: config.restart_every_s is 0.0 -- --restart-every unset/never")
    writer_lives = summary["writer_totals_all_lives"]["lives"]
    eq(writer_lives, 1, "StormHunt frozen: summary.writer_totals_all_lives.lives")
    recoveries = summary["recoveries"]
    eq(recoveries, 0, "StormHunt frozen: summary.recoveries (designed restarts)")
    reader_restarts = summary["reader_restarts"]
    eq(reader_restarts, 0, "StormHunt frozen: summary.reader_restarts")

    require(summary["verify_healthy"] is True,
            "StormHunt: summary.verify_healthy is the JSON literal true")
    require(summary["digest_equal"] is True,
            "StormHunt: summary.digest_equal is the JSON literal true -- the (unscored) "
            "end-of-run replay completed")

    total_batches = summary["total_batches"]
    eq(total_batches, 476_813,
       "StormHunt frozen: summary.total_batches (the unscored end-of-run replay's own "
       "batch count)")

    # --- end-of-phase edge rows: the manifest's own final_stats field
    # carries it directly, so no compactions.jsonl fallback is needed here
    # (unlike a record that omits it) ---
    n_edge_versions = summary["final_stats"]["n_edge_versions"]
    eq(n_edge_versions, 1_402_818, "StormHunt frozen: summary.final_stats.n_edge_versions")

    # --- writer within-run RSS slope (single life, no restarts -> one fit) ---
    rss_doc = json.loads(LONGEVITY_RSS_SLOPES_HUNT.read_text(encoding="utf-8"))
    writer_life_rows = rss_doc["writer_lives"]
    eq(len(writer_life_rows), writer_lives,
       "StormHunt: rss_slopes-stormhunt.json writer_lives row count matches "
       "summary.writer_totals_all_lives.lives")
    eq(writer_life_rows[0]["life"], 0, "StormHunt: the sole writer life is life 0")
    writer_slope = writer_life_rows[0]["slope_kb_per_s_least_squares"]
    eq(round(writer_slope, 3), 20.444,
       "StormHunt frozen: writer within-run RSS slope, kB/s (least squares)")
    frozen_bound_writer = rss_doc["frozen_bound_writer_kb_per_s"]
    eq(frozen_bound_writer, 50, "StormHunt frozen: rss_slopes-stormhunt.json "
       "frozen_bound_writer_kb_per_s")
    require(writer_slope <= frozen_bound_writer,
            "StormHunt: the writer's within-run RSS slope PASSes the frozen 50 kB/s bound")
    writer_slope_1dp = round(writer_slope, 1)
    eq(f"{writer_slope_1dp:.1f}", "20.4",
       "StormHunt frozen: writer slope rounded to 1dp matches README.md's own "
       "\"coordinator's independently-computed value\" of 20.4 kB/s")

    # --- reader within-run RSS slopes: 8 readers, reader_restarts=0 so
    # exactly one fitted segment each -- unlike P-SOAK2, EVERY reader here
    # exceeds (fails) the frozen 10 kB/s bound ---
    reader_rows = rss_doc["readers"]
    eq(len(reader_rows), 8, "StormHunt frozen: rss_slopes-stormhunt.json readers row count")
    reader_slopes = [row["slope_kb_per_s_least_squares"] for row in reader_rows.values()]
    frozen_bound_reader = rss_doc["frozen_bound_reader_kb_per_s"]
    eq(frozen_bound_reader, 10, "StormHunt frozen: rss_slopes-stormhunt.json "
       "frozen_bound_reader_kb_per_s")
    require(all(s > frozen_bound_reader for s in reader_slopes),
            "StormHunt: every reader's within-run RSS slope EXCEEDS the frozen 10 kB/s "
            "bound -- unlike P-SOAK2, where every reader passed under it")
    reader_min = min(reader_slopes)
    reader_max = max(reader_slopes)
    eq(round(reader_min, 3), 11.362, "StormHunt frozen: reader within-run slope min, kB/s")
    eq(round(reader_max, 3), 12.543, "StormHunt frozen: reader within-run slope max, kB/s")
    reader_min_1dp = round(reader_min, 1)
    reader_max_1dp = round(reader_max, 1)
    eq(f"{reader_min_1dp:.1f}", "11.4",
       "StormHunt frozen: reader slope min rounded to 1dp matches README.md's stated range")
    eq(f"{reader_max_1dp:.1f}", "12.5",
       "StormHunt frozen: reader slope max rounded to 1dp matches README.md's stated range")

    # --- writer errors: 175, all one exception class, cross-checked
    # against the manifest's own cumulative counters ---
    by_class = json.loads(LONGEVITY_WRITER_ERRORS_BY_CLASS_HUNT.read_text(encoding="utf-8"))
    total_writer_errors = by_class["total_writer_errors"]
    eq(total_writer_errors, 175,
       "StormHunt frozen: writer_error_counts_by_class-stormhunt.json total_writer_errors")
    writer_by_class = by_class["writer_by_class"]
    eq(writer_by_class, {"NotFoundError": 175},
       "StormHunt frozen: writer_by_class -- the sole exception class, and its full count")
    eq(total_writer_errors, summary["error_count"],
       "StormHunt: total_writer_errors matches manifest's own summary.error_count "
       "(reader_errors_total=0 and unexpected_writer_deaths=0, so the two totals coincide)")
    eq(total_writer_errors, summary["writer_totals_all_lives"]["errors"],
       "StormHunt: total_writer_errors matches manifest's own "
       "writer_totals_all_lives.errors")

    # --- reader errors: 0, and 0 reader_op_error events captured despite
    # the D-088 capture (c3a5592) being armed for the whole run ---
    reader_errors_total = by_class["reader_errors_total"]
    eq(reader_errors_total, 0,
       "StormHunt frozen: writer_error_counts_by_class-stormhunt.json reader_errors_total")
    eq(reader_errors_total, summary["reader_errors_total"],
       "StormHunt: reader_errors_total matches manifest's own summary.reader_errors_total")
    reader_op_error_events = by_class["reader_op_error_events"]
    eq(reader_op_error_events, 0,
       "StormHunt frozen: writer_error_counts_by_class-stormhunt.json reader_op_error_events")
    capture_commit = by_class["capture_code_commit"]
    eq(capture_commit, "c3a5592",
       "StormHunt frozen: writer_error_counts_by_class-stormhunt.json capture_code_commit "
       "(D-088, the bounded reader-error-message capture)")

    reader_op_error_text = LONGEVITY_READER_OP_ERROR_HUNT.read_text(encoding="utf-8")
    require(reader_op_error_text == "",
            f"{relpath(LONGEVITY_READER_OP_ERROR_HUNT)}: file is empty (0 bytes) -- 0 "
            "reader_op_error events captured over the whole run")
    reader_op_error_readme = LONGEVITY_READER_OP_ERROR_README_HUNT.read_text(encoding="utf-8")
    require("0 reader_op_error events" in reader_op_error_readme,
            f"{relpath(LONGEVITY_READER_OP_ERROR_README_HUNT)}: names the 0 "
            "reader_op_error events finding")
    require(capture_commit in reader_op_error_readme,
            f"{relpath(LONGEVITY_READER_OP_ERROR_README_HUNT)}: names the same capture "
            f"commit ({capture_commit}) as writer_error_counts_by_class-stormhunt.json")

    # --- build info: release build, matching commit, cross-checked
    # against README.md's own quoted build_info fields ---
    build_info_text = LONGEVITY_BUILD_INFO_HUNT.read_text(encoding="utf-8")
    build_fields = dict(re.findall(r"(\w+)=(\S+)", build_info_text))
    eq(build_fields.get("commit"), manifest["git_commit"],
       f"{relpath(LONGEVITY_BUILD_INFO_HUNT)}: commit matches manifest's own git_commit")
    eq(build_fields.get("profile"), "release",
       f"{relpath(LONGEVITY_BUILD_INFO_HUNT)}: profile")
    eq(build_fields.get("debug_assertions"), "False",
       f"{relpath(LONGEVITY_BUILD_INFO_HUNT)}: debug_assertions")
    eq(build_fields.get("engine_version"), "0.8.0",
       f"{relpath(LONGEVITY_BUILD_INFO_HUNT)}: engine_version")
    eq(build_fields.get("manifest_format_version"), "3",
       f"{relpath(LONGEVITY_BUILD_INFO_HUNT)}: manifest_format_version")

    # --- host load (1-min averages), scoped to the run's own 6h window --
    # the log carries one further post-run sample (taken during the
    # unscored verify/replay step) that README.md's own prose explicitly
    # excludes from the range it states, so the window boundary is derived
    # from the section's own **launched**: timestamp + config.duration_s,
    # not hard-coded as "drop the last line" ---
    launched_match = re.search(r"\*\*launched\*\*:\s*([0-9T:Z-]+);", section)
    require(launched_match is not None,
            "README.md: P-STORM-HUNT section has a **launched**: timestamp")
    launched_dt = datetime.strptime(launched_match.group(1), "%Y-%m-%dT%H:%M:%SZ")
    window_end_dt = launched_dt + timedelta(seconds=duration_s)

    host_load_text = LONGEVITY_HOST_LOAD_HUNT.read_text(encoding="utf-8")
    blocks = re.findall(
        r"=== HOST_LOAD (\S+) ===\n(.*?)(?=(?:=== HOST_LOAD |\Z))",
        host_load_text, re.DOTALL)
    eq(len(blocks), 8,
       "StormHunt frozen: host_load-stormhunt.log HOST_LOAD sample count "
       "(7 in-window + 1 post-run verify/replay sample)")
    samples: list[tuple[datetime, float]] = []
    for ts_str, block in blocks:
        load_match = re.search(r"load average:\s*([\d.]+),", block)
        require(load_match is not None,
                f"host_load-stormhunt.log: HOST_LOAD {ts_str} block has a load average line")
        if load_match:
            samples.append((datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ"),
                             float(load_match.group(1))))
    in_window = [load for ts, load in samples if ts <= window_end_dt]
    post_run = [load for ts, load in samples if ts > window_end_dt]
    eq(len(in_window), 7,
       "StormHunt: host_load-stormhunt.log samples at/before launched + duration_s "
       "(the run's own 6h window)")
    eq(len(post_run), 1,
       "StormHunt: exactly one host_load-stormhunt.log sample after the run's own 6h "
       "window (the unscored post-run verify/replay step)")
    eq(round(post_run[0], 2), 3.56,
       "StormHunt frozen: the one post-run sample's 1-min load average")
    host_load_min = min(in_window)
    host_load_max = max(in_window)
    eq(round(host_load_min, 2), 10.79, "StormHunt frozen: in-window host load min, 1-min avg")
    eq(round(host_load_max, 2), 48.95, "StormHunt frozen: in-window host load max, 1-min avg")

    # --- clause (e): the pre-registration's own non-conclusion for a
    # negative result at this duration/edge-row count, quoted from
    # README.md's own Outcome paragraph rather than paraphrased here ---
    outcome_match = re.search(
        r"\*\*Outcome, stated exactly as the pre-registration's clause \(e\):\*\*\s*(.+?)\n\n",
        section, re.DOTALL)
    require(outcome_match is not None,
            "README.md: P-STORM-HUNT section has an Outcome/clause (e) paragraph")
    outcome_text = " ".join(outcome_match.group(1).split()) if outcome_match else ""
    require("not reproduced within 6 h at 1.40M edge rows" in outcome_text,
            "README.md: clause (e) outcome names the not-reproduced-within-6h-at-1.40M-"
            "edge-rows finding")
    require("not evidence that it is gone" in outcome_text,
            "README.md: clause (e) outcome carries the not-evidence-it-is-gone qualifier")
    eq(round(n_edge_versions / 1e6, 2), 1.40,
       "StormHunt: recomputed final_stats.n_edge_versions / 1e6 matches the outcome "
       "paragraph's own \"1.40M edge rows\" figure")

    # --- emit macros ---
    m.add("osdiSoakCommitHunt", manifest["git_commit"],
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: git_commit")
    m.add("osdiSoakDurationSHunt", tex_num(int(duration_s)),
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: config.duration_s")
    m.add("osdiSoakWriterLivesHunt", tex_num(writer_lives),
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: summary.writer_totals_all_lives.lives")
    m.add("osdiSoakRecoveriesHunt", tex_num(recoveries),
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: summary.recoveries (designed restarts; "
          "config.restart_every_s == 0.0, so none were scheduled)")
    m.add("osdiSoakWriterWithinLifeSlopeKBpsHunt", f"{writer_slope_1dp:.1f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_HUNT)}: writer_lives[0]."
          "slope_kb_per_s_least_squares, rounded to 1dp (single writer life, no restarts)")
    m.add("osdiSoakReaderWithinLifeSlopeMinKBpsHunt", f"{reader_min_1dp:.1f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_HUNT)}: min(readers[*].slope_kb_per_s_least_squares), "
          "8 readers, rounded to 1dp")
    m.add("osdiSoakReaderWithinLifeSlopeMaxKBpsHunt", f"{reader_max_1dp:.1f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_HUNT)}: max(readers[*].slope_kb_per_s_least_squares), "
          "rounded to 1dp")
    m.add("osdiSoakDigestEqualHunt", "true",
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: summary.digest_equal is the JSON literal "
          "true (text macro, not a number)")
    m.add("osdiSoakBatchesHunt", tex_num(total_batches),
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: summary.total_batches (the unscored "
          "end-of-run replay's own batch count)")
    m.add("osdiSoakVerifyHealthyHunt", "true",
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: summary.verify_healthy is the JSON literal "
          "true (text macro, not a number)")
    m.add("osdiSoakWriterErrorsTrueHunt", tex_num(total_writer_errors),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_CLASS_HUNT)}: total_writer_errors, "
          "cross-checked against manifest's own error_count and "
          "writer_totals_all_lives.errors")
    m.add("osdiSoakWriterErrorsClassHunt", "NotFoundError",
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_CLASS_HUNT)}: the sole key of "
          "writer_by_class (text macro, not a number)")
    m.add("osdiSoakReaderErrorsTrueHunt", tex_num(reader_errors_total),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_CLASS_HUNT)}: reader_errors_total, "
          "cross-checked against manifest's own summary.reader_errors_total")
    m.add("osdiSoakReaderOpErrorEventsHunt", tex_num(reader_op_error_events),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_CLASS_HUNT)}: reader_op_error_events -- "
          f"cross-checked against {relpath(LONGEVITY_READER_OP_ERROR_HUNT)} being empty "
          "(0 bytes)")
    m.add("osdiSoakReaderOpErrorCaptureCommitHunt", capture_commit,
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_CLASS_HUNT)}: capture_code_commit -- D-088, "
          "the bounded reader-error-message capture, armed for the whole 6h run (text "
          "macro, not a number)")
    m.add("osdiSoakEdgeRowsHunt", tex_num(n_edge_versions),
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: summary.final_stats.n_edge_versions "
          "(the record carries this field directly; no compactions.jsonl fallback needed)")
    m.add("osdiSoakHostLoadMinHunt", f"{host_load_min:.2f}",
          f"{relpath(LONGEVITY_HOST_LOAD_HUNT)}: min(1-min load averages), the 7 samples "
          "at/before launched + config.duration_s (the run's own 6h window)")
    m.add("osdiSoakHostLoadMaxHunt", f"{host_load_max:.2f}",
          f"{relpath(LONGEVITY_HOST_LOAD_HUNT)}: max(1-min load averages), same window "
          "(excludes the one post-run verify/replay-step sample)")
    m.add("osdiSoakHuntPatternReproduced", "false",
          f"README.md P-STORM-HUNT section, quoted verbatim (its own clause (e) wording): "
          f"\"{outcome_text}\" -- the P-SOAK2 reader OSError/StateError storm "
          "(osdiSoakReaderErrorsTrueHunt = 0) was not reproduced within this 6h/1.40M-"
          "edge-row observation window; per the pre-registration's own clause (e), that "
          "negative result is not evidence the pattern is gone (text macro, not a number)")


# --------------------------------------------------------------------------
# W-lane -- P-OV1, the xzgpu-calibrated overload sweep (EXP-B4):
# does tgms.tools.limits.ConcurrencyGate engage once open-loop callers
# outrun the service, and does the service recover once load drops.
# benchmarks/overload-v1/{overload-2026-09-15,overload-2026-09-15-rep2}.json
# (2 reps, --clients 1 2 4 8 16 32 64 --max-concurrent 8, `entity_history`
# under load) + hwm-checkpoints-v3.json (the "Pinned" VmHWM localization).
# No verdict macro -- the per-clause PASS/REFUTED scoring is README.md's
# own prose (a/b/c/d/e in its own words), not emitted here.
# --------------------------------------------------------------------------

def compute_overload(m: Macros) -> None:
    sums_hashes = _sha256sums_table(OVERLOAD_SHA256SUMS.read_text(encoding="utf-8"))
    for path in (OVERLOAD_REP1, OVERLOAD_REP2, OVERLOAD_REP1_RECORDS,
                 OVERLOAD_HWM_CHECKPOINTS_V3):
        require(sha256_file(path) in sums_hashes,
                f"{relpath(path)}: sha256 is one of the digests listed in "
                f"{relpath(OVERLOAD_SHA256SUMS)} (checked by hash membership, not by "
                "filename -- that file's own rep1 entry is misnamed, see OVERLOAD_REP1's "
                "module comment)")

    rep1 = json.loads(OVERLOAD_REP1.read_text(encoding="utf-8"))
    rep2 = json.loads(OVERLOAD_REP2.read_text(encoding="utf-8"))

    eq(rep1["git_commit"], "ebe1dc2", "Overload frozen: rep1 git_commit")
    eq(rep2["git_commit"], rep1["git_commit"], "Overload: rep2 git_commit matches rep1's")
    max_concurrent = rep1["config"]["max_concurrent"]
    eq(max_concurrent, 8, "Overload frozen: config.max_concurrent (the harness's own CLI "
       "default under test, not a value read from `tgms serve`)")
    eq(rep2["config"]["max_concurrent"], max_concurrent,
       "Overload: rep2 config.max_concurrent matches rep1's")
    eq(rep1["dataset"]["digest"], rep2["dataset"]["digest"],
       "Overload: rep1 and rep2 ran against the identical store state (store.digest() "
       "matches across reps)")

    def steps_by_clients(rec):
        return {s["n_clients"]: s for s in rec["steps"]}

    rep1_steps = steps_by_clients(rep1)
    rep2_steps = steps_by_clients(rep2)
    eq(sorted(rep1_steps), [1, 2, 4, 8, 16, 32, 64],
       "Overload frozen: rep1 steps.n_clients ladder")
    eq(sorted(rep2_steps), sorted(rep1_steps), "Overload: rep2 has the same clients ladder")
    clients_max = max(rep1_steps)
    eq(clients_max, 64, "Overload frozen: max(steps[*].n_clients) -- the gate-engagement cell")

    # --- refusal attribution at the n=64 cell: every refusal is a
    # concurrency-cap refusal (refusal_stage=="limit"), never a result-size
    # limit or an uncaught operator error -- recomputed from the per-call
    # records, not trusted from the summary's n_refused/n_error alone. ---
    records = json.loads(OVERLOAD_REP1_RECORDS.read_text(encoding="utf-8"))
    step64_records = next(s for s in records if s["n_clients"] == 64)["records"]
    eq(len(step64_records), rep1_steps[64]["n_calls"],
       "Overload: overload-2026-09-15.records.json's n=64 record count matches "
       "the manifest's own steps[64].n_calls")
    refused_records = [r for r in step64_records if r["outcome"] == "refused"]
    eq(len(refused_records), rep1_steps[64]["n_refused"],
       "Overload: recomputed count(outcome==refused) at n=64 matches steps[64].n_refused")
    refusal_stages = {r["refusal_stage"] for r in refused_records}
    eq(refusal_stages, {"limit"},
       "Overload frozen: every n=64 refusal (rep1) carries refusal_stage==\"limit\" "
       "(ConcurrencyGate's own check) -- never a result-size limit or an operator error")
    n_error_total = sum(s["n_error"] for s in rep1_steps.values()) + \
        sum(s["n_error"] for s in rep2_steps.values())
    eq(n_error_total, 0,
       "Overload frozen: sum(steps[*].n_error) is 0 across every step, both reps -- "
       "0 operator errors observed")

    refused_cap_rep1_at64 = rep1_steps[64]["n_refused"]
    refused_cap_rep2_at64 = rep2_steps[64]["n_refused"]
    eq(refused_cap_rep1_at64, 4_438, "Overload frozen: rep1 steps[64].n_refused")
    eq(refused_cap_rep2_at64, 285, "Overload frozen: rep2 steps[64].n_refused")
    refusal_ratio = refused_cap_rep1_at64 / refused_cap_rep2_at64
    close(round(refusal_ratio, 1), 15.6, 1e-9,
          "Overload: rep1/rep2 refused-at-64 ratio, rounded to 1 decimal")

    admitted_p95_at64 = rep1_steps[64]["concurrent_in_flight_p95"]
    eq(admitted_p95_at64, 8.0, "Overload frozen: rep1 steps[64].concurrent_in_flight_p95")
    require(admitted_p95_at64 <= max_concurrent,
            "Overload: p95 admitted concurrency at n=64 does not exceed the cap")

    p99_at32_rep1 = rep1_steps[32]["p99_ms"]
    eq(round(p99_at32_rep1, 1), 40.4, "Overload frozen: rep1 steps[32].p99_ms, rounded")
    eq(rep1_steps[32]["n_refused"], 0,
       "Overload frozen: rep1 steps[32].n_refused is 0 -- the p99 REFUTED clause's own "
       "n=32 cell admits every call")

    # --- recovery step: "exactly" the 1-client step, same rep ---
    recovery_qps = rep1["recovery"]["throughput_qps"]
    recovery_p50 = rep1["recovery"]["p50_ms"]
    eq(round(recovery_qps, 2), 20.13, "Overload frozen: rep1 recovery.throughput_qps")
    eq(round(recovery_p50, 2), 1.43, "Overload frozen: rep1 recovery.p50_ms")
    n1_step = rep1_steps[1]
    close(recovery_qps, n1_step["throughput_qps"], 0.01,
          "Overload: recovery.throughput_qps matches steps[n_clients=1].throughput_qps "
          "(same rep) -- \"recovery, exactly\"")
    close(recovery_p50, n1_step["p50_ms"], 0.05,
          "Overload: recovery.p50_ms is within 0.05ms of steps[n_clients=1].p50_ms "
          "(same rep) -- \"recovery, exactly\"")

    # --- service-surface high-water mark: the "Pinned" VmHWM localization,
    # not the harness's own end-of-sweep store.digest() lifetime peak
    # (~2 GB, a harness-side finalization call, not the service under
    # load -- see README.md's "Pinned" section; that ~2GB figure is
    # deliberately not landed as a macro here, it names what the high-water
    # figure is NOT). ---
    checkpoints = {c["label"]: c["vm_hwm_kb"] for c in
                   json.loads(OVERLOAD_HWM_CHECKPOINTS_V3.read_text(encoding="utf-8"))}
    hwm_after_step = checkpoints["after_step_n64"]
    hwm_after_recovery = checkpoints["after_recovery_step"]
    eq(hwm_after_step, 227_280,
       "Overload frozen: hwm-checkpoints-v3.json after_step_n64.vm_hwm_kb")
    eq(hwm_after_recovery, hwm_after_step,
       "Overload: VmHWM after the recovery step equals VmHWM after the 64-client step -- "
       "the recovery step adds exactly 0 KB (README.md's own \"Pinned\" finding)")
    hwm_after_digest = checkpoints["after_store_digest_full"]
    require(hwm_after_digest > hwm_after_step,
            "Overload: VmHWM jumps only after the harness's own store.digest() call, not "
            "during the 64-client step or the recovery step")
    high_water_mb = round(hwm_after_step / 1000, 1)
    eq(high_water_mb, 227.3, "Overload: service-surface VmHWM / 1000, MB")

    # --- emit macros ---
    m.add("osdiOverloadCommit", rep1["git_commit"],
          f"{relpath(OVERLOAD_REP1)}: git_commit (same in rep2)")
    m.add("osdiOverloadMaxConcurrent", tex_num(max_concurrent),
          f"{relpath(OVERLOAD_REP1)}: config.max_concurrent (== protocol.ceilings."
          "max_concurrent), the harness's own CLI default under test")
    m.add("osdiOverloadClientsMax", tex_num(clients_max),
          f"{relpath(OVERLOAD_REP1)}: max(steps[*].n_clients) -- the gate engages at this "
          "cell in both reps")
    m.add("osdiOverloadRefusalKindConcurrencyOnly", "true",
          f"{relpath(OVERLOAD_REP1_RECORDS)}: the set of refusal_stage values over every "
          "n=64 record with outcome==\"refused\" is exactly {\"limit\"} -- every refusal "
          "is a concurrency-cap refusal, never a result-size limit (text macro, not a "
          "number)")
    m.add("osdiOverloadOperatorErrorsTotal", tex_num(n_error_total),
          f"{relpath(OVERLOAD_REP1)}+{relpath(OVERLOAD_REP2)}: sum(steps[*].n_error) over "
          "every step, both reps")
    m.add("osdiOverloadRefusedCapAtMaxRep1", tex_num(refused_cap_rep1_at64),
          f"{relpath(OVERLOAD_REP1)}: steps[n_clients=64].n_refused")
    m.add("osdiOverloadRefusedCapAtMaxRep2", tex_num(refused_cap_rep2_at64),
          f"{relpath(OVERLOAD_REP2)}: steps[n_clients=64].n_refused")
    m.add("osdiOverloadRefusalRepRatio", f"{refusal_ratio:.1f}",
          "derived: rep1/rep2 refused-at-n=64 ratio (rep-to-rep variance at the knee, "
          "not a measured field)")
    m.add("osdiOverloadAdmittedConcurrencyP95AtMax", f"{admitted_p95_at64:.1f}",
          f"{relpath(OVERLOAD_REP1)}: steps[n_clients=64].concurrent_in_flight_p95")
    m.add("osdiOverloadP99At32ClientsMs", f"{p99_at32_rep1:.2f}",
          f"{relpath(OVERLOAD_REP1)}: steps[n_clients=32].p99_ms, with n_refused==0 at "
          "that cell -- the bounded-latency prediction's REFUTED evidence")
    m.add("osdiOverloadRecoveryQps", f"{recovery_qps:.2f}",
          f"{relpath(OVERLOAD_REP1)}: recovery.throughput_qps")
    m.add("osdiOverloadRecoveryP50Ms", f"{recovery_p50:.2f}",
          f"{relpath(OVERLOAD_REP1)}: recovery.p50_ms")
    m.add("osdiOverloadServiceHighWaterKB", tex_num(hwm_after_step),
          f"{relpath(OVERLOAD_HWM_CHECKPOINTS_V3)}: after_step_n64.vm_hwm_kb (== "
          "after_recovery_step.vm_hwm_kb) -- the \"Pinned\" localization, not the "
          "harness's own end-of-sweep store.digest() lifetime peak")
    m.add("osdiOverloadServiceHighWaterMB", f"{high_water_mb:.1f}",
          f"{relpath(OVERLOAD_HWM_CHECKPOINTS_V3)}: after_step_n64.vm_hwm_kb / 1000, MB")


# --------------------------------------------------------------------------
# C10 --- live OSV advisory-feed workload, first committed snapshot
# --------------------------------------------------------------------------

def compute_c10_live_osv(m: Macros) -> None:
    """Lane C10-snap: `benchmarks/live-osv-v1/snapshot-2026-09-16.json`, the
    first committed record snapshot of the live-osv poller
    (`docs/design/LIVE_WORKLOAD_OSV_DESIGN_2026-09-13.md`) running on xzgpu.
    Resolves the three macros that were PENDING for lack of any committed
    record: `osdiLiveDays`, `osdiLiveAdvisories`, `osdiLiveCorrections`.

    Every number below is recomputed from the snapshot's own row-level
    fields (`live_osv.cycles_raw`, the 38 raw per-cycle metrics lines the
    poller itself wrote) and cross-checked against the snapshot's own
    pre-computed aggregates -- never taken from an aggregate field on
    trust alone, the house rule this whole file follows."""
    eq(sha256_file(LIVE_OSV_SNAPSHOT), LIVE_OSV_SNAPSHOT_SHA256,
       f"{relpath(LIVE_OSV_SNAPSHOT)}: sha256 matches README.md's own quoted value "
       "in its \"Snapshot\" section")

    doc = json.loads(LIVE_OSV_SNAPSHOT.read_text(encoding="utf-8"))
    live = doc["live_osv"]
    cycles = live["cycles_raw"]
    eq(len(cycles), 38, "C10 frozen: live-osv-v1 snapshot cycle count")

    # --- recompute the per-cycle sums from the raw rows, not from the
    # manifest's own pre-aggregated `corrections` block ---
    sum_keys = ("records_seen", "revisions_seen", "corrections_written",
                "noop_revisions", "retractions", "withdrawn_seen",
                "events_appended", "feed_errors")
    recomputed = {k: sum(c.get(k, 0) for c in cycles) for k in sum_keys}

    corrections = live["corrections"]
    for k in sum_keys:
        eq(recomputed[k], corrections[k],
           f"{relpath(LIVE_OSV_SNAPSHOT)}: recomputed sum(cycles_raw[*].{k}) matches "
           f"live_osv.corrections.{k}")

    eq(recomputed["records_seen"], 95, "C10 frozen: total records_seen across all 38 cycles")
    eq(recomputed["revisions_seen"], 95, "C10 frozen: total revisions_seen across all 38 cycles")
    eq(recomputed["corrections_written"], 1, "C10 frozen: total corrections_written")
    eq(recomputed["noop_revisions"], 41, "C10 frozen: total noop_revisions")
    eq(recomputed["retractions"], 1, "C10 frozen: total retractions")
    eq(recomputed["withdrawn_seen"], 1, "C10 frozen: total withdrawn_seen")
    eq(recomputed["events_appended"], 989, "C10 frozen: total events_appended")
    eq(recomputed["feed_errors"], 0, "C10 frozen: total feed_errors (clean run, no restarts)")

    # --- advisories: bootstrap + live delta must equal the live total ---
    advisories = live["advisories"]
    eq(advisories["bootstrap"], 32787, "C10 frozen: bootstrap advisory count")
    eq(advisories["total_at_snapshot"], advisories["bootstrap"] + advisories["new_since_bootstrap"],
       f"{relpath(LIVE_OSV_SNAPSHOT)}: advisories.total_at_snapshot == "
       "advisories.bootstrap + advisories.new_since_bootstrap")
    eq(advisories["total_at_snapshot"], 32827, "C10 frozen: total advisories ingested at snapshot")

    # --- days of operation: recomputed from the raw epoch fields, not
    # trusted from the record's own rounded days_of_operation field ---
    operation = live["operation"]
    first_epoch = cycles[0]["ts"]
    eq(first_epoch, operation["first_record_epoch"],
       f"{relpath(LIVE_OSV_SNAPSHOT)}: cycles_raw[0].ts matches "
       "live_osv.operation.first_record_epoch")
    snapshot_epoch = operation["snapshot_epoch"]
    days = (snapshot_epoch - first_epoch) / 86400.0
    close(round(days, 3), operation["days_of_operation"], 1e-9,
          f"{relpath(LIVE_OSV_SNAPSHOT)}: recomputed (snapshot_epoch - first_record_epoch) "
          "/ 86400 matches live_osv.operation.days_of_operation")
    days_2dp = round(days, 2)
    eq(days_2dp, 1.89, "C10 frozen: days of operation (snapshot_epoch - first poll cycle), 2dp")

    # --- result_digest: recomputed over the same canonical counts object
    # scripts in this lane's own snapshot-building step hashed, a tamper
    # test on the manifest's headline numbers independent of the
    # whole-file sha256 check above ---
    counts = {
        "advisories_bootstrap": advisories["bootstrap"],
        "advisories_total_at_snapshot": advisories["total_at_snapshot"],
        "advisories_new_since_bootstrap": advisories["new_since_bootstrap"],
        "corrections_written": corrections["corrections_written"],
        "retractions": corrections["retractions"],
        "noop_revisions": corrections["noop_revisions"],
        "withdrawn_seen": corrections["withdrawn_seen"],
        "revisions_seen": corrections["revisions_seen"],
        "records_seen": corrections["records_seen"],
        "events_appended": corrections["events_appended"],
        "feed_errors": corrections["feed_errors"],
        "cycles": len(cycles),
        "days_of_operation": round(days, 6),
        "first_record_utc": operation["first_record_ts_utc"],
        "snapshot_utc": operation["snapshot_ts_utc"],
    }
    recomputed_digest = hashlib.sha256(
        json.dumps(counts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    eq(recomputed_digest, doc["result_digest"],
       f"{relpath(LIVE_OSV_SNAPSHOT)}: recomputed sha256 over the canonical counts object "
       "matches the manifest's own top-level result_digest")

    m.add("osdiLiveDays", f"{days_2dp:.2f}",
          f"{relpath(LIVE_OSV_SNAPSHOT)}: live_osv.operation, "
          "(snapshot_epoch - first_record_epoch) / 86400, recomputed from cycles_raw[0].ts")
    m.add("osdiLiveAdvisories", tex_num(advisories["total_at_snapshot"]),
          f"{relpath(LIVE_OSV_SNAPSHOT)}: live_osv.advisories.total_at_snapshot "
          f"({tex_num(advisories['bootstrap'])} bootstrap + "
          f"{advisories['new_since_bootstrap']} live-ingested)")
    m.add("osdiLiveCorrections", tex_num(corrections["corrections_written"]),
          f"{relpath(LIVE_OSV_SNAPSHOT)}: live_osv.corrections.corrections_written, "
          "recomputed as sum(cycles_raw[*].corrections_written)")


# --------------------------------------------------------------------------
# C9, independent-validation axis -- LDBC reference-correctness run
# (Lane W2t, benchmarks/ldbc-ref-v1/)
# --------------------------------------------------------------------------

def _ldbc_family(plan_id: str) -> str:
    for prefix in ("BI", "IC", "IS"):
        if plan_id.startswith(prefix):
            return prefix
    raise AssertionError(f"unrecognised LDBC ref-v1 template family: {plan_id!r}")


def compute_ldbc_ref_v1(m: Macros) -> None:
    """Lane W2t: `benchmarks/ldbc-ref-v1/`, the LDBC reference-correctness
    run -- Claim C9's independent-validation axis (TGIR's output vs. an
    independently loaded, unmodified-query Neo4j reference over the 24
    templates TGIR can express). Resolves the three stubs that used to
    stand PENDING here (`osdiLdbcExpressible`/`osdiLdbcExecuted`/
    `osdiLdbcValidated`) and lands the rest of the scorecard beside them.

    Every whole-file sha256 below is checked against the run's own
    `SHA256SUMS.txt` before anything inside is trusted. Every count is
    recomputed from `compare-2026-09-18.json`'s (the revision of record,
    per README.md's "Revision of record" section) per-template verdicts --
    `attempted`/`compared`/`agreeing`/`disagreeing`/
    `reference_columns_not_projected` -- never taken from the record's own
    aggregate, and then cross-checked against both README.md's stated
    verdict table and `campaign.yaml`'s addendum_3 (the frozen final
    gate/scoring addendum; addenda 1/2 are superseded readings reached
    before the temporal-parameter fix and before the column-correspondence
    ratification, kept in the file for provenance and never a number
    source here). `campaign.yaml` itself is prose-only provenance in this
    docstring and in the frozen literals below -- like the M5 freeze doc in
    the C6 section of the module docstring, nothing here parses it as
    YAML.

    24 templates have a vendored TGIR plan and ran (`osdiLdbcExpressible`);
    23 of those completed within the ceiling -- BI6.v2 hit the
    pre-registered ceiling (`bypass_ceiling_s` + `child_open_allowance_s`
    from the TGMS-side campaign record) and produced no rows -- giving
    `osdiLdbcExecuted`; 18 agree per campaign.yaml's addendum_3 scoring
    rule, giving `osdiLdbcValidated`.
    """
    sha_table = _sha256sums_by_name(LDBC_REF_V1_SHA256SUMS.read_text(encoding="utf-8"))
    for path in (LDBC_REF_V1_README, LDBC_REF_V1_CAMPAIGN_YAML, LDBC_REF_V1_COMPARE,
                 LDBC_REF_V1_COMPARE_INTERIM, LDBC_REF_V1_TGMS_CAMPAIGN):
        name = path.name
        require(name in sha_table,
                f"{relpath(path)}: {name} not found in benchmarks/ldbc-ref-v1/SHA256SUMS.txt")
        if name in sha_table:
            eq(sha256_file(path), sha_table[name],
               f"{relpath(path)}: sha256 matches benchmarks/ldbc-ref-v1/SHA256SUMS.txt")

    doc = json.loads(LDBC_REF_V1_COMPARE.read_text(encoding="utf-8"))
    manifest = doc["manifest"]
    verdicts = doc["verdicts"]
    eq(manifest["plans"], 24, f"{relpath(LDBC_REF_V1_COMPARE)}: manifest.plans")
    eq(len(verdicts), 24, f"{relpath(LDBC_REF_V1_COMPARE)}: verdicts row count")
    eq(manifest["supersedes"], "compare-2026-09-17.json",
       f"{relpath(LDBC_REF_V1_COMPARE)}: manifest.supersedes names the interim record")

    by_id = {v["plan_id"]: v for v in verdicts}
    eq(len(by_id), len(verdicts), f"{relpath(LDBC_REF_V1_COMPARE)}: plan_id is unique per row")

    # --- verdict-class counts, recomputed from each row's own fields, not
    # from any pre-aggregated field (the record carries none) ---
    timed_out = [v for v in verdicts if not v["attempted"]]
    attempted = [v for v in verdicts if v["attempted"]]
    agree = [v for v in attempted if v.get("verdict") == "agreeing"]
    not_projected = [v for v in attempted if v.get("verdict") == "reference-column-not-projected"]
    disagree = [v for v in attempted if v.get("verdict") == "disagreeing"]
    errored = [v for v in attempted if v.get("verdict") == "error"]
    not_comparable = [v for v in attempted if v.get("verdict") == "not-comparable"]
    eq(len(agree) + len(not_projected) + len(disagree) + len(errored)
       + len(not_comparable) + len(timed_out), 24,
       f"{relpath(LDBC_REF_V1_COMPARE)}: every verdict class partitions the 24 templates "
       "exactly once")

    # cross-check against README.md's stated verdict table ("18 agree / 3
    # reference-column-not-projected / 2 disagree / 1 timeout / 0 error / 0
    # not comparable") and campaign.yaml's addendum_3 verdict_counts block
    eq(len(agree), 18, "LDBC ref-v1 frozen (README.md / campaign.yaml addendum_3): agree count")
    eq(len(not_projected), 3,
       "LDBC ref-v1 frozen: reference-column-not-projected count")
    eq(len(disagree), 2, "LDBC ref-v1 frozen: disagree count")
    eq(len(timed_out), 1, "LDBC ref-v1 frozen: timeout count")
    eq(len(errored), 0, "LDBC ref-v1 frozen: error count")
    eq(len(not_comparable), 0, "LDBC ref-v1 frozen: not-comparable count")

    eq(sorted(v["plan_id"] for v in not_projected), ["BI4", "IC12", "IC5"],
       "LDBC ref-v1 frozen: the reference-column-not-projected templates (BI4, IC5, IC12)")
    eq(sorted(v["plan_id"] for v in disagree), ["IC2", "IS3"],
       "LDBC ref-v1 frozen: the disagreeing templates (IC2, IS3)")
    eq([v["plan_id"] for v in timed_out], ["BI6.v2"],
       "LDBC ref-v1 frozen: the timed-out template (BI6.v2)")

    # the reference_columns_not_projected field must agree with the verdict
    # label independently -- a tamper to one without the other is caught
    eq(sorted(v["plan_id"] for v in verdicts if v.get("reference_columns_not_projected")),
       sorted(v["plan_id"] for v in not_projected),
       f"{relpath(LDBC_REF_V1_COMPARE)}: reference_columns_not_projected non-empty iff "
       "verdict == reference-column-not-projected")

    # --- row agreement over the 23 comparable templates (every attempted
    # template; BI6.v2 never ran) ---
    eq(len(attempted), 23, "LDBC ref-v1 frozen: comparable (attempted) template count")
    rows_compared = sum(v["compared"] for v in attempted)
    rows_agreeing = sum(v["agreeing"] for v in attempted)
    eq(rows_compared, 736, "LDBC ref-v1 frozen: total compared rows over the 23 comparable templates")
    eq(rows_agreeing, 678, "LDBC ref-v1 frozen: total agreeing rows over the 23 comparable templates")
    row_fraction = rows_agreeing / rows_compared
    close(round(row_fraction, 4), 0.9212, 1e-9,
          "LDBC ref-v1: recomputed row agreement fraction matches campaign.yaml addendum_3's "
          "row_agreement.ratio")
    row_fraction_3dp = f"{row_fraction:.3f}"
    eq(row_fraction_3dp, "0.921", "LDBC ref-v1 frozen: row agreement fraction, 3dp (README.md)")

    gate = 0.90
    require(row_fraction >= gate,
            "LDBC ref-v1: recomputed row agreement fraction clears campaign.yaml's pass_if "
            "(>= 0.90) gate")

    # --- per-family (BI/IC/IS) template and row breakdown, cross-checked
    # against campaign.yaml addendum_3's row_agreement.by_group block ---
    fam_expect = {
        "BI": dict(templates=9, agree_verdicts=8, compared=607, agreeing=606),
        "IC": dict(templates=7, agree_verdicts=4, compared=66, agreeing=56),
        "IS": dict(templates=7, agree_verdicts=6, compared=63, agreeing=16),
    }
    fam_rows: dict[str, tuple[int, int]] = {}
    for fam, expect in fam_expect.items():
        entries = [v for v in attempted if _ldbc_family(v["plan_id"]) == fam]
        fam_agree_verdicts = sum(1 for v in entries if v.get("verdict") == "agreeing")
        fam_compared = sum(v["compared"] for v in entries)
        fam_agreeing = sum(v["agreeing"] for v in entries)
        eq(len(entries), expect["templates"],
           f"LDBC ref-v1 frozen: {fam} comparable template count")
        eq(fam_agree_verdicts, expect["agree_verdicts"],
           f"LDBC ref-v1 frozen: {fam} agreeing-verdict count")
        eq(fam_compared, expect["compared"], f"LDBC ref-v1 frozen: {fam} rows compared")
        eq(fam_agreeing, expect["agreeing"], f"LDBC ref-v1 frozen: {fam} rows agreeing")
        fam_rows[fam] = (fam_agreeing, fam_compared)
    eq(sum(c for _, c in fam_rows.values()), rows_compared,
       "LDBC ref-v1: per-family compared rows sum to the overall total")
    eq(sum(a for a, _ in fam_rows.values()), rows_agreeing,
       "LDBC ref-v1: per-family agreeing rows sum to the overall total")

    template_fraction = len(agree) / 24
    eq(f"{template_fraction:.2f}", "0.75",
       "LDBC ref-v1 frozen: template agreement fraction, 18/24")

    # --- D-090 (the M7 KNOWS-both-ways double count): the two disagreeing
    # templates are exactly its two instances, per README.md §5.4/§5.5 and
    # ops/failure_ledger.jsonl. IS3 is the original finding, IC2 the second
    # instance "newly visible" once its reference side became valid --
    # reported in that order (README.md's own §5 ordering), not
    # alphabetically.
    defect_id = "D-090"
    defect_templates_ordered = ["IS3", "IC2"]
    eq(set(defect_templates_ordered), {v["plan_id"] for v in disagree},
       "LDBC ref-v1: the D-090 defect template list matches the disagreeing verdicts exactly")
    if FAILURE_LEDGER.exists():
        ledger_entries = load_jsonl(FAILURE_LEDGER)
        d090 = [e for e in ledger_entries
                if e.get("id") == "D-090-is3-knows-both-ways-double-count"]
        require(len(d090) == 1,
                "LDBC ref-v1: exactly one D-090-is3-knows-both-ways-double-count entry in "
                "ops/failure_ledger.jsonl (cross-check only, not this macro's source)")
        if d090:
            entry_id = d090[0]["id"]
            eq(entry_id.split("-", 2)[0] + "-" + entry_id.split("-", 2)[1], defect_id,
               "LDBC ref-v1: the ledger entry's own id starts with D-090 (cross-check only)")
            symptom = d090[0].get("symptom", "")
            require("IS3" in symptom and "IC2" in symptom,
                    "LDBC ref-v1: the D-090 ledger entry's symptom names both IS3 and IC2 "
                    "(cross-check only)")

    # --- the pre-registered TGIR-side ceiling BI6.v2 hit, read from the
    # TGMS-side campaign record rather than hard-coded independently ---
    tgms_campaign = json.loads(LDBC_REF_V1_TGMS_CAMPAIGN.read_text(encoding="utf-8"))
    tgms_manifest = tgms_campaign["manifest"]
    ceiling_s = tgms_manifest["bypass_ceiling_s"] + tgms_manifest["child_open_allowance_s"]
    eq(ceiling_s, 1020,
       f"{relpath(LDBC_REF_V1_TGMS_CAMPAIGN)}: manifest.bypass_ceiling_s + "
       "manifest.child_open_allowance_s (BI6.v2's timeout ceiling, README.md §5.6)")
    tgms_records = tgms_campaign["records"]
    eq(sum(1 for r in tgms_records if r["outcome"] == "COMPLETED"), 23,
       f"{relpath(LDBC_REF_V1_TGMS_CAMPAIGN)}: COMPLETED record count matches the 23 "
       "comparable templates")
    eq(sum(1 for r in tgms_records if r["outcome"] == "TIMEOUT"), 1,
       f"{relpath(LDBC_REF_V1_TGMS_CAMPAIGN)}: TIMEOUT record count (BI6.v2)")
    eq(sum(1 for r in tgms_records if r["outcome"] == "ERRORED"), 1,
       f"{relpath(LDBC_REF_V1_TGMS_CAMPAIGN)}: ERRORED record count (BI6.json, the v1 "
       "artifact kept as defect evidence per RUNBOOK.md §6 -- not one of the 24 "
       "scored templates)")
    eq(len(tgms_records), 25,
       f"{relpath(LDBC_REF_V1_TGMS_CAMPAIGN)}: 24 scored templates + BI6's v1-artifact "
       "evidence row")

    # --- the superseded interim (compare-2026-09-17.json), landed as
    # osdiLdbcInterim* so the paper can cite the reference-side fix. The
    # excluded set (7 templates with an invalid reference side at that
    # point, plus the always-timed-out BI6.v2) is read out of the revision
    # of record's own manifest.supersedes_reason text, not hard-coded
    # independently of it. ---
    invalid_ref_match = re.search(
        r"(\d+) templates \(([^)]+)\) had an invalid reference side",
        manifest["supersedes_reason"])
    require(invalid_ref_match is not None,
            f"{relpath(LDBC_REF_V1_COMPARE)}: manifest.supersedes_reason names the invalid-"
            "reference template count and list")
    invalid_ref_ids: list[str] = []
    excluded_ids: set[str] = set()
    if invalid_ref_match:
        invalid_ref_ids = invalid_ref_match.group(2).split()
        eq(len(invalid_ref_ids), int(invalid_ref_match.group(1)),
           f"{relpath(LDBC_REF_V1_COMPARE)}: supersedes_reason's own count matches its own list")
        eq(sorted(invalid_ref_ids), ["BI11", "BI12", "BI4", "BI9", "IC2", "IC5", "IC9"],
           "LDBC ref-v1 frozen: the 7 templates with an invalid reference side on 2026-09-17")
        excluded_ids = set(invalid_ref_ids) | {v["plan_id"] for v in timed_out}
        eq(len(excluded_ids), 8, "LDBC ref-v1: 7 invalid-reference + 1 timeout excluded")

    interim_doc = json.loads(LDBC_REF_V1_COMPARE_INTERIM.read_text(encoding="utf-8"))
    interim_verdicts = interim_doc["verdicts"]
    eq(len(interim_verdicts), 24,
       f"{relpath(LDBC_REF_V1_COMPARE_INTERIM)}: verdicts row count")

    interim_agree = sum(1 for v in interim_verdicts if v.get("verdict") == "agreeing")
    eq(interim_agree, 14, "LDBC ref-v1 frozen: interim (2026-09-17) agree count")

    interim_scoreable = [v for v in interim_verdicts if v["plan_id"] not in excluded_ids]
    eq(len(interim_scoreable), 16,
       f"{relpath(LDBC_REF_V1_COMPARE_INTERIM)}: templates that produced a valid comparison "
       "on 2026-09-17 (24 - 7 invalid-reference - 1 timeout)")
    interim_rows_compared = sum(v["compared"] for v in interim_scoreable)
    interim_rows_agreeing = sum(v["agreeing"] for v in interim_scoreable)
    eq(interim_rows_compared, 329,
       "LDBC ref-v1 frozen: interim compared rows over the 16 templates with a valid "
       "comparison (campaign.yaml addendum_2)")
    eq(interim_rows_agreeing, 282,
       "LDBC ref-v1 frozen: interim agreeing rows over the same 16 templates")

    # --- emit ---
    m.add("osdiLdbcTemplates", tex_num(24),
          f"{relpath(LDBC_REF_V1_COMPARE)}: manifest.plans -- the 24 LDBC templates with a "
          "vendored TGIR plan run by this campaign")
    m.add("osdiLdbcExpressible", tex_num(24),
          f"{relpath(LDBC_REF_V1_COMPARE)}: manifest.plans -- templates the vendored TGIR "
          "plans cover of the 24 in this campaign")
    m.add("osdiLdbcExecuted", tex_num(len(attempted)),
          f"{relpath(LDBC_REF_V1_COMPARE)}: count of verdicts with attempted == true -- "
          f"templates whose TGMS side completed within the {tex_num(ceiling_s)} s ceiling "
          "(BI6.v2 timed out at that ceiling, README.md §5.6)")
    m.add("osdiLdbcValidated", tex_num(len(agree)),
          f"{relpath(LDBC_REF_V1_COMPARE)}: count of verdicts with verdict == agreeing -- "
          "templates agreeing per campaign.yaml addendum_3's scoring rule")
    m.add("osdiLdbcAgree", tex_num(len(agree)),
          f"{relpath(LDBC_REF_V1_COMPARE)}: count of verdict == agreeing")
    m.add("osdiLdbcNotProjected", tex_num(len(not_projected)),
          f"{relpath(LDBC_REF_V1_COMPARE)}: count of verdict == reference-column-not-projected "
          "(BI4, IC5, IC12)")
    m.add("osdiLdbcDisagree", tex_num(len(disagree)),
          f"{relpath(LDBC_REF_V1_COMPARE)}: count of verdict == disagreeing (IC2, IS3)")
    m.add("osdiLdbcTimeout", tex_num(len(timed_out)),
          f"{relpath(LDBC_REF_V1_COMPARE)}: count of attempted == false (BI6.v2)")
    m.add("osdiLdbcComparableTemplates", tex_num(len(attempted)),
          f"{relpath(LDBC_REF_V1_COMPARE)}: count of attempted == true -- the denominator of "
          "the row-agreement fraction")
    m.add("osdiLdbcRowsCompared", tex_num(rows_compared),
          f"{relpath(LDBC_REF_V1_COMPARE)}: sum(compared) over the 23 comparable templates")
    m.add("osdiLdbcRowsAgreeing", tex_num(rows_agreeing),
          f"{relpath(LDBC_REF_V1_COMPARE)}: sum(agreeing) over the 23 comparable templates")
    m.add("osdiLdbcRowAgreementFraction", row_fraction_3dp,
          f"{relpath(LDBC_REF_V1_COMPARE)}: sum(agreeing) / sum(compared) over the 23 "
          "comparable templates, 3dp")
    m.add("osdiLdbcGate", f"{gate:.2f}",
          "campaign.yaml addendum_3 scoring.overall_agreement: the pre-registered pass_if bar")
    m.add("osdiLdbcGateMet", "true" if row_fraction >= gate else "false",
          f"{relpath(LDBC_REF_V1_COMPARE)}: recomputed row agreement fraction "
          f"({row_fraction_3dp}) >= campaign.yaml's pass_if gate (0.90)")
    m.add("osdiLdbcTemplateAgreementFraction", f"{template_fraction:.2f}",
          f"{relpath(LDBC_REF_V1_COMPARE)}: count(verdict == agreeing) / 24")

    for fam, (fam_agreeing, fam_compared) in fam_rows.items():
        m.add(f"osdiLdbcRows{fam}Agreeing", tex_num(fam_agreeing),
              f"{relpath(LDBC_REF_V1_COMPARE)}: sum(agreeing) over the {fam} family's "
              "comparable templates (campaign.yaml addendum_3 row_agreement.by_group)")
        m.add(f"osdiLdbcRows{fam}Compared", tex_num(fam_compared),
              f"{relpath(LDBC_REF_V1_COMPARE)}: sum(compared) over the {fam} family's "
              "comparable templates (campaign.yaml addendum_3 row_agreement.by_group)")

    m.add("osdiLdbcDefectId", defect_id,
          "ops/failure_ledger.jsonl: D-090-is3-knows-both-ways-double-count")
    m.add("osdiLdbcDefectTemplates", ", ".join(defect_templates_ordered),
          f"{relpath(LDBC_REF_V1_COMPARE)}: the disagreeing verdicts (IC2, IS3), both the "
          "M7 KNOWS-both-ways double count (D-090) per README.md §5.4/§5.5 -- IS3 "
          "the original finding, IC2 the second instance found once its reference became "
          "valid, reported in that order")

    m.add("osdiLdbcInterimAgree", tex_num(interim_agree),
          f"{relpath(LDBC_REF_V1_COMPARE_INTERIM)}: count of verdict == agreeing in the "
          "superseded 2026-09-17 revision")
    m.add("osdiLdbcInterimRowsAgreeing", tex_num(interim_rows_agreeing),
          f"{relpath(LDBC_REF_V1_COMPARE_INTERIM)}: sum(agreeing) over the 16 templates that "
          "produced a valid comparison on 2026-09-17 (excludes the 7 templates named in this "
          "record's own manifest.supersedes_reason plus BI6.v2)")
    m.add("osdiLdbcInterimRowsCompared", tex_num(interim_rows_compared),
          f"{relpath(LDBC_REF_V1_COMPARE_INTERIM)}: sum(compared) over the same 16 templates")


# --------------------------------------------------------------------------
# B7 -- scale campaign (Stage 0 iTiger calibration + Stage 1 30M)
# --------------------------------------------------------------------------

def _b7_check_sha256(path: Path, table: dict[str, str]) -> None:
    name = path.name
    require(name in table, f"B7: {relpath(path)}: {name} not found in "
            f"{relpath(path.parent)}'s README sha256 table")
    if name not in table:
        return
    eq(sha256_file(path), table[name],
       f"{relpath(path)}: sha256 matches its own README's sha256 table")


def compute_b7_scale(m: Macros) -> None:
    """Lane B7 (see the module docstring's B7 section). Every whole-file
    sha256 below is checked against the sha256 table in the record's own
    README before anything inside that file is trusted; every aggregate
    (medians, sums) is recomputed from row-level data and cross-checked
    against the record's own pre-computed field, never taken on trust."""
    readme_sha = _readme_sha256_table(B7_README.read_text(encoding="utf-8"))
    calib_readme_sha = _readme_sha256_table(B7_ITIGER_CALIB_README.read_text(encoding="utf-8"))

    for path in (B7_BUILD_30M, B7_SCALE_CURVE_30M, B7_SCALE_CURVE_30M_RAW,
                 B7_CHECK_FULL_30M, B7_RECOVERY_30M, B7_RECOVERY_30M_CE5000,
                 B7_VERSION_HISTORY_30M, B7_QUERYFLOOR_30M):
        _b7_check_sha256(path, readme_sha)
    for path in (B7_ITIGER_CALIB_1M, B7_ITIGER_CALIB_10M):
        _b7_check_sha256(path, calib_readme_sha)

    build = json.loads(B7_BUILD_30M.read_text(encoding="utf-8"))
    curve = json.loads(B7_SCALE_CURVE_30M.read_text(encoding="utf-8"))
    curve_raw = json.loads(B7_SCALE_CURVE_30M_RAW.read_text(encoding="utf-8"))
    check_full = json.loads(B7_CHECK_FULL_30M.read_text(encoding="utf-8"))
    rec500 = json.loads(B7_RECOVERY_30M.read_text(encoding="utf-8"))
    rec5000 = json.loads(B7_RECOVERY_30M_CE5000.read_text(encoding="utf-8"))
    vh = json.loads(B7_VERSION_HISTORY_30M.read_text(encoding="utf-8"))
    qf = json.loads(B7_QUERYFLOOR_30M.read_text(encoding="utf-8"))
    calib1m = json.loads(B7_ITIGER_CALIB_1M.read_text(encoding="utf-8"))
    calib10m = json.loads(B7_ITIGER_CALIB_10M.read_text(encoding="utf-8"))

    # --- one content digest ties every 30M sidecar to the same store ---
    eq(build["dataset"]["digest"], B7_30M_STORE_DIGEST,
       f"{relpath(B7_BUILD_30M)}: dataset.digest matches the frozen 30M store digest")
    eq(build["result_digest"], B7_30M_STORE_DIGEST,
       f"{relpath(B7_BUILD_30M)}: result_digest matches the frozen 30M store digest")
    eq(curve["dataset"]["digest"], B7_30M_STORE_DIGEST,
       f"{relpath(B7_SCALE_CURVE_30M)}: dataset.digest matches the frozen 30M store digest")
    eq(curve["result_digest"], B7_30M_STORE_DIGEST,
       f"{relpath(B7_SCALE_CURVE_30M)}: result_digest matches the frozen 30M store digest")
    eq(check_full["dataset"]["digest"], B7_30M_STORE_DIGEST,
       f"{relpath(B7_CHECK_FULL_30M)}: dataset.digest matches the frozen 30M store digest")
    for rec, path in ((rec500, B7_RECOVERY_30M), (rec5000, B7_RECOVERY_30M_CE5000)):
        require(rec["digest_compare"]["digest_equal"] is True,
                f"{relpath(path)}: digest_compare.digest_equal is true")
        eq(rec["digest_compare"]["src_digest"], B7_30M_STORE_DIGEST,
           f"{relpath(path)}: digest_compare.src_digest matches the frozen 30M store digest")
        eq(rec["digest_compare"]["replayed_digest"], B7_30M_STORE_DIGEST,
           f"{relpath(path)}: digest_compare.replayed_digest matches the frozen 30M store digest")
    eq(vh["dataset"]["digest"], B7_30M_STORE_DIGEST,
       f"{relpath(B7_VERSION_HISTORY_30M)}: dataset.digest matches the frozen 30M store digest")
    eq(vh["result_digest"], B7_30M_STORE_DIGEST,
       f"{relpath(B7_VERSION_HISTORY_30M)}: result_digest matches the frozen 30M store digest")

    # --- build: wall, peak RSS, manifest/segment bytes, steady-decile median ---
    bi = build["build_info"]
    wall_s = bi["wall_s"]
    eq(wall_s, build["falsifiers"]["build_wall"]["measured_s"],
       f"{relpath(B7_BUILD_30M)}: build_info.wall_s matches falsifiers.build_wall.measured_s")
    eq(round(wall_s, 3), 3786.004, "B7 frozen: 30M build wall_s")

    peak_vmhwm_kb = bi["peak_rss"]["vmhwm"]
    peak_rss_gb = round(peak_vmhwm_kb / 1e6, 2)
    readme_peak_row = re.search(
        r"peak RSS \(VmHWM\)\s*\|\s*([\d,]+) KB \(([\d.]+) GB\)", B7_README.read_text(encoding="utf-8"))
    require(readme_peak_row is not None, f"{relpath(B7_README)}: peak RSS (VmHWM) row found")
    if readme_peak_row is not None:
        eq(int(readme_peak_row.group(1).replace(",", "")), peak_vmhwm_kb,
           f"{relpath(B7_README)}: peak RSS row's KB figure matches build_info.peak_rss.vmhwm")
        eq(float(readme_peak_row.group(2)), peak_rss_gb,
           f"{relpath(B7_README)}: peak RSS row's GB figure matches vmhwm/1e6, rounded 2dp")
    eq(peak_rss_gb, 59.31, "B7 frozen: 30M build peak RSS, GB")

    manifest_bytes = bi["store_bytes"]["manifest_bytes"]
    eq(manifest_bytes, build["falsifiers"]["manifest_bytes"]["measured_bytes"],
       f"{relpath(B7_BUILD_30M)}: build_info.store_bytes.manifest_bytes matches "
       "falsifiers.manifest_bytes.measured_bytes")
    eq(manifest_bytes, 175244, "B7 frozen: 30M manifest bytes")

    segment_bytes = bi["store_bytes"]["segment_bytes"]
    segment_gb = round(segment_bytes / 1e9, 3)
    eq(segment_gb, build["falsifiers"]["segment_bytes"]["measured_gb"],
       f"{relpath(B7_BUILD_30M)}: build_info.store_bytes.segment_bytes / 1e9, rounded 3dp, "
       "matches falsifiers.segment_bytes.measured_gb")
    eq(segment_gb, 1.549, "B7 frozen: 30M segment bytes, GB")

    decile_ops = [d["ops_per_s"] for d in bi["ops_per_s_by_decile"]]
    eq(len(decile_ops), 10, "B7 frozen: 30M build decile count")
    decile_median = statistics.median(decile_ops)
    close(decile_median, bi["steady_decile_median_ops_per_s"], 0.06,
          f"{relpath(B7_BUILD_30M)}: recomputed median(ops_per_s_by_decile[*].ops_per_s) "
          "matches build_info.steady_decile_median_ops_per_s")
    close(decile_median, build["falsifiers"]["steady_decile_ops_per_s"]["measured"], 0.06,
          f"{relpath(B7_BUILD_30M)}: recomputed decile median matches "
          "falsifiers.steady_decile_ops_per_s.measured")
    steady_decile_median = bi["steady_decile_median_ops_per_s"]
    eq(steady_decile_median, 8463.0, "B7 frozen: 30M steady-decile median ops/s")

    # --- query-ready floor: queryfloor-30m.json cross-checked against the
    # copy build-30m.json itself carries ---
    qf_vmhwm_kb = qf["vmhwm_kb"]
    eq(qf_vmhwm_kb, build["query_ready_floor"]["vmhwm_kb"],
       f"{relpath(B7_QUERYFLOOR_30M)}: vmhwm_kb matches build-30m.json's own "
       "query_ready_floor.vmhwm_kb")
    eq(qf["n_ok"], qf["n_queries"], f"{relpath(B7_QUERYFLOOR_30M)}: n_ok == n_queries (13/13)")
    query_floor_gb = round(qf_vmhwm_kb / 1e6, 2)
    require(f"{query_floor_gb} GB (job 213173)" in B7_README.read_text(encoding="utf-8"),
            f"{relpath(B7_README)}: quotes the recomputed query-ready floor GB figure "
            "(job 213173) verbatim")
    eq(query_floor_gb, 6.81, "B7 frozen: 30M query-ready floor, GB")

    # --- check --full: canonical (clean, job 213174) run ---
    check_wall = check_full["canonical_run"]["wall_s"]
    eq(check_full["canonical_run"]["job_id"], "213174",
       f"{relpath(B7_CHECK_FULL_30M)}: canonical_run is the clean rerun, job 213174")
    require(check_full["canonical_run"]["raw"]["healthy"] is True,
            f"{relpath(B7_CHECK_FULL_30M)}: canonical_run.raw.healthy is true")
    eq(check_wall, 98.673, "B7 frozen: 30M check --full wall_s (clean, 213174)")

    # --- recovery: frozen cadence 500, and the Addendum-6 cadence 5000 ---
    recovery_500_s = round(rec500["replay_wall_s"], 1)
    eq(recovery_500_s, 14618.6, "B7 frozen: 30M recovery wall_s, cadence 500")
    recovery_5000_s = round(rec5000["replay_wall_s"], 1)
    eq(recovery_5000_s, 2656.9, "B7 frozen: 30M recovery wall_s, cadence 5000 (Addendum 6)")

    # --- version_history: clean (job 213175) run, wall/RSS medians
    # recomputed from the 3 reps, not trusted from the record's own
    # pre-aggregated *_median fields ---
    clean_vh = vh["clean_alone"]
    eq(clean_vh["job_id"], "213175",
       f"{relpath(B7_VERSION_HISTORY_30M)}: clean_alone is the clean rerun, job 213175")
    reps = clean_vh["reps"]
    eq(len(reps), 3, f"{relpath(B7_VERSION_HISTORY_30M)}: clean_alone has 3 reps")
    vh_wall_median = statistics.median(r["wall_ms"] for r in reps)
    eq(vh_wall_median, clean_vh["wall_ms_median"],
       f"{relpath(B7_VERSION_HISTORY_30M)}: recomputed median(reps[*].wall_ms) matches "
       "clean_alone.wall_ms_median")
    vh_rss_median_kb = statistics.median(r["vmhwm_kb"] for r in reps)
    eq(vh_rss_median_kb, clean_vh["vmhwm_kb_median"],
       f"{relpath(B7_VERSION_HISTORY_30M)}: recomputed median(reps[*].vmhwm_kb) matches "
       "clean_alone.vmhwm_kb_median")
    vh_wall_s = round(vh_wall_median / 1000, 3)
    vh_rss_gb = round(vh_rss_median_kb / 1e6, 3)
    eq(vh_wall_s, 5.496, "B7 frozen: 30M version_history wall_s (clean, 213175)")
    eq(vh_rss_gb, 3.863, "B7 frozen: 30M version_history VmHWM, GB (clean, 213175)")

    # --- scale-curve: 13-operator p50s, recomputed from the raw per-rep
    # timings, cross-checked against the raw file's own p50_ms and the
    # aggregated summary's clean_213174_p50_ms ---
    eq(set(curve["per_operator_p50_ms"]), set(B7_SCALE_CURVE_OPS),
       f"{relpath(B7_SCALE_CURVE_30M)}: per_operator_p50_ms operator set matches "
       "the known 13-operator registry")
    raw_by_query = {r["query"]: r for r in curve_raw["results"]["native"]}
    eq(set(raw_by_query), set(B7_SCALE_CURVE_OPS),
       f"{relpath(B7_SCALE_CURVE_30M_RAW)}: results.native operator set matches "
       "the known 13-operator registry")
    p50_by_op: dict[str, float] = {}
    for op_id in B7_SCALE_CURVE_OPS:
        summary = curve["per_operator_p50_ms"][op_id]
        raw_row = raw_by_query[op_id]
        recomputed = statistics.median(raw_row["timings_ms"])
        close(recomputed, raw_row["p50_ms"], 0.01,
              f"{relpath(B7_SCALE_CURVE_30M_RAW)}: {op_id}: recomputed median(timings_ms) "
              "matches this row's own p50_ms")
        eq(raw_row["p50_ms"], summary["clean_213174_p50_ms"],
           f"{relpath(B7_SCALE_CURVE_30M)}: {op_id}: per_operator_p50_ms.clean_213174_p50_ms "
           f"matches {relpath(B7_SCALE_CURVE_30M_RAW)}'s own p50_ms")
        p50_by_op[op_id] = summary["clean_213174_p50_ms"]

    # --- reach.window admission: predicted admitted (not refused) under
    # the revised (Addendum 5's second re-examination) admission policy;
    # confirmed both clean and contended runs ---
    reach = curve["reach_window_admission"]
    require(reach["clean_213174"]["admitted"] is True,
            f"{relpath(B7_SCALE_CURVE_30M)}: reach_window_admission.clean_213174.admitted "
            "is true")
    require("Addendum 5" in reach["note"] and "admission" in reach["note"],
            f"{relpath(B7_SCALE_CURVE_30M)}: reach_window_admission.note names the "
            "Addendum-5 admission-policy re-examination")
    estimate_ms = reach["clean_213174"]["estimate"]["time_est_ms"]
    eq(estimate_ms, reach["clean_213174"]["time_est_ms"],
       f"{relpath(B7_SCALE_CURVE_30M)}: reach_window_admission.clean_213174.estimate."
       "time_est_ms matches its own top-level time_est_ms")
    eq(estimate_ms, reach["contended_213069"]["time_est_ms"],
       f"{relpath(B7_SCALE_CURVE_30M)}: reach.window time_est_ms agrees between the "
       "clean and contended reruns (the estimator, not the store, produced it)")
    eq(estimate_ms, 4371, "B7 frozen: 30M reach.window admission estimate, ms")

    # --- Stage 0 (iTiger calibration): k_build/k_recover and the 10M anchors ---
    cbi = calib10m["build_info"]
    calib10m_wall = cbi["wall_s"]
    eq(calib10m_wall, 865.592, "B7 frozen: Stage-0 10M calib build wall_s")
    calib10m_decs = [d["ops_per_s"] for d in cbi["ops_per_s_by_decile"]]
    eq(len(calib10m_decs), 10, "B7 frozen: Stage-0 10M calib decile count")
    calib10m_median = statistics.median(calib10m_decs)
    close(calib10m_median, 24390.8, 0.05,
          f"{relpath(B7_ITIGER_CALIB_10M)}: recomputed median(ops_per_s_by_decile[*]."
          "ops_per_s) matches the calibration README's own quoted 24,390.8 ops/s")

    calib10m_peak_kb = cbi["peak_rss"]["vmhwm"]
    calib_readme_text = B7_ITIGER_CALIB_README.read_text(encoding="utf-8")
    peak_row = re.search(
        r"peak RSS.*\|\s*[\d,]+ KB \([\d.]+ GB\)\s*\|\s*([\d,]+) KB \([\d.]+ GB\)",
        calib_readme_text)
    require(peak_row is not None,
            f"{relpath(B7_ITIGER_CALIB_README)}: peak RSS row (1M | 10M columns) found")
    if peak_row is not None:
        eq(int(peak_row.group(1).replace(",", "")), calib10m_peak_kb,
           f"{relpath(B7_ITIGER_CALIB_README)}: peak RSS row's 10M KB figure matches "
           "itiger-calib-10m.json's build_info.peak_rss.vmhwm")
    # The calibration README's own peak-RSS-row prose ("19.43 GB") divides kB by
    # 1024 then by 1000 -- a mixed-unit slip, not this campaign's convention.
    # Every other B7 peak-RSS macro (e.g. osdiB7PeakRSS30M above) computes GB
    # as kB/1e6; recompute this one the same way, from the record field alone,
    # never from the README's prose GB figure. See itiger-calib-2026-09.
    # README.md's appended "Unit note".
    calib10m_peak_gb = round(calib10m_peak_kb / 1e6, 2)
    eq(calib10m_peak_gb, 19.90,
       "B7 frozen: Stage-0 10M calib peak RSS, GB (kB/1e6, the campaign convention -- "
       "NOT the calibration README's own mixed-unit-slip prose figure of 19.43 GB)")

    recovery_1m = calib1m["recovery"]
    require(recovery_1m["digest_equal"] is True,
            f"{relpath(B7_ITIGER_CALIB_1M)}: recovery.digest_equal is true")
    recovery_1m_wall = recovery_1m["wall_s"]
    close(round(recovery_1m_wall, 3), 75.860, 0.001,
          "B7 frozen: Stage-0 1M calib recovery wall_s")

    # --- Stage 0 (iTiger calibration): the 1M anchors (build wall, peak RSS,
    # recovery, final segment bytes) -- itiger-calib-1m.json was missing its
    # own macros even though itiger-calib-2026-09.README.md's build-results
    # table quotes its 1M column throughout ---
    cbi1m = calib1m["build_info"]
    calib1m_wall = cbi1m["wall_s"]
    eq(round(calib1m_wall, 3), 78.339, "B7 frozen: Stage-0 1M calib build wall_s")
    calib1m_peak_kb = cbi1m["peak_rss"]["vmhwm"]
    calib1m_peak_gb = round(calib1m_peak_kb / 1e6, 2)
    eq(calib1m_peak_gb, 2.37,
       "B7 frozen: Stage-0 1M calib peak RSS, GB (kB/1e6, same convention as "
       "osdiB7Calib10MPeakRSS above)")
    calib1m_segment_bytes = cbi1m["store_bytes"]["segment_bytes"]
    calib1m_segment_gb = round(calib1m_segment_bytes / 1e9, 3)
    eq(calib1m_segment_gb, 0.050, "B7 frozen: Stage-0 1M calib final segment bytes, GB")
    close(round(recovery_1m_wall, 2), 75.86, 0.005,
          "B7 frozen: Stage-0 1M calib recovery wall_s, 2dp")

    k_build = calib10m_median / 5300
    close(round(k_build, 3), 4.602, 0.0005,
          f"{relpath(B7_ITIGER_CALIB_10M)}: recomputed (10M steady-decile-median ops/s) / "
          f"5,300 (the xzgpu bulk-rate basis quoted by {relpath(B7_ITIGER_CALIB_README)}) "
          "matches that README's own quoted k_build = 4.602")
    k_recover = recovery_1m_wall / 91.01
    close(round(k_recover, 3), 0.834, 0.0005,
          f"{relpath(B7_ITIGER_CALIB_1M)}: recomputed (1M replay wall_s) / 91.01 (the xzgpu "
          f"1% anchor quoted by {relpath(B7_ITIGER_CALIB_README)}) matches that README's "
          "own quoted k_recover = 0.834")

    # ---------------------------------------------------------------
    # macros
    # ---------------------------------------------------------------
    m.add("osdiB7BuildWall30M", tex_float(round(wall_s, 3)),
          f"{relpath(B7_BUILD_30M)}: build_info.wall_s, seconds")
    m.add("osdiB7PeakRSS30M", f"{peak_rss_gb:.2f}",
          f"{relpath(B7_BUILD_30M)}: build_info.peak_rss.vmhwm / 1e6, GB, 2dp "
          f"({tex_num(peak_vmhwm_kb)} KB)")
    m.add("osdiB7VersionHistoryWall30M", f"{vh_wall_s:.3f}",
          f"{relpath(B7_VERSION_HISTORY_30M)}: clean_alone (job 213175), median(reps[*]."
          "wall_ms) / 1000, seconds")
    m.add("osdiB7VersionHistoryRSS30M", f"{vh_rss_gb:.3f}",
          f"{relpath(B7_VERSION_HISTORY_30M)}: clean_alone (job 213175), "
          "median(reps[*].vmhwm_kb) / 1e6, GB")
    m.add("osdiB7ManifestBytes30M", tex_num(manifest_bytes),
          f"{relpath(B7_BUILD_30M)}: build_info.store_bytes.manifest_bytes")
    m.add("osdiB7SegmentBytes30M", f"{segment_gb:.3f}",
          f"{relpath(B7_BUILD_30M)}: build_info.store_bytes.segment_bytes / 1e9, GB")
    m.add("osdiB7CheckFullWall30M", f"{check_wall:.3f}",
          f"{relpath(B7_CHECK_FULL_30M)}: canonical_run (clean, job 213174), wall_s")
    m.add("osdiB7Recovery30M", tex_float(recovery_500_s),
          f"{relpath(B7_RECOVERY_30M)}: replay_wall_s, rounded 1dp, seconds "
          "(frozen cadence 500, same as the Stage-0/EXP-A4 1M recovery); "
          "digest_compare.digest_equal true")
    m.add("osdiB7RecoveryCe5000At30M", tex_float(recovery_5000_s),
          f"{relpath(B7_RECOVERY_30M_CE5000)}: replay_wall_s, rounded 1dp, seconds "
          "(Addendum 6 cadence-isolation run, compact_every=5000); "
          "digest_compare.digest_equal true; does not supersede osdiB7Recovery30M "
          "(both stand, per the campaign's non-overwrite discipline)")
    for op_id, frag in B7_SCALE_CURVE_OPS.items():
        val = p50_by_op[op_id]
        m.add(f"osdiB7ScaleCurveP50{frag}30M", tex_float(val),
              f"{relpath(B7_SCALE_CURVE_30M)}: per_operator_p50_ms.{op_id}."
              "clean_213174_p50_ms, ms (clean, job 213174), recomputed from "
              f"{relpath(B7_SCALE_CURVE_30M_RAW)}'s own timings_ms")
    m.add("osdiB7ReachWindowRefused30M", "false",
          f"{relpath(B7_SCALE_CURVE_30M)}: reach_window_admission.clean_213174.admitted "
          "is true (not refused) -- Addendum 5's second re-examination revised the "
          "admission-policy prediction from refused to admitted at 30M/100M, confirmed "
          "by both the clean and contended reruns")
    m.add("osdiB7QueryFloor30M", f"{query_floor_gb:.2f}",
          f"{relpath(B7_QUERYFLOOR_30M)}: vmhwm_kb / 1e6, GB (fresh read-only process, "
          "cold-open, job 213173, alone)")
    m.add("osdiB7BuildSteadyDecileMedian30M", tex_float(steady_decile_median),
          f"{relpath(B7_BUILD_30M)}: build_info.steady_decile_median_ops_per_s, "
          "recomputed as median(ops_per_s_by_decile[*].ops_per_s), ops/s")
    m.add("osdiB7ReachWindowEstimateMs30M", tex_num(estimate_ms),
          f"{relpath(B7_SCALE_CURVE_30M)}: reach_window_admission.clean_213174."
          "estimate.time_est_ms, ms")

    # osdiB7300MGate: not a measurement -- the pre-registration's §3 ruling
    # ("RULED MOOT", the PI's "300M dropped" superseding the blueprint's
    # "300M only if 100M is clean") means no 300M step is planned or
    # pre-registered at all; scale-independent, so no {30M,100M} suffix.
    m.add("osdiB7300MGate", "false",
          "docs/design/SCALE_BUILD_FORECAST_2026-09-15.md §3 (gitignored, internal): "
          "\"RULED MOOT\" -- the PI's later ruling (\"300M dropped\") supersedes the "
          "blueprint's \"300M only if 100M is clean\"; no 300M step is planned or "
          "pre-registered. Not a record-derived measurement, unlike every other macro "
          "in this function")

    m.add("osdiB7Calib10MBuildWall", f"{calib10m_wall:.3f}",
          f"{relpath(B7_ITIGER_CALIB_10M)}: build_info.wall_s, seconds")
    m.add("osdiB7Calib10MPeakRSS", f"{calib10m_peak_gb:.2f}",
          f"{relpath(B7_ITIGER_CALIB_10M)}: build_info.peak_rss.vmhwm "
          f"({tex_num(calib10m_peak_kb)} kB) / 1e6, GB, 2dp -- this campaign's kB/1e6 "
          "convention (same as every other B7 peak-RSS macro), NOT the calibration "
          f"README's own mixed-unit-slip prose figure; see "
          f"{relpath(B7_ITIGER_CALIB_README)}'s appended Unit note")
    m.add("osdiB7Calib10MSteadyOps", tex_float(round(calib10m_median, 1)),
          f"{relpath(B7_ITIGER_CALIB_10M)}: recomputed median(ops_per_s_by_decile[*]."
          "ops_per_s), ops/s")
    m.add("osdiB7KBuild", f"{k_build:.3f}",
          f"{relpath(B7_ITIGER_CALIB_10M)}: (10M steady-decile-median ops/s) / 5,300 "
          f"(xzgpu bulk basis, quoted by {relpath(B7_ITIGER_CALIB_README)}); retired as "
          "a Stage-1 scaling factor (Addendum 5) but kept as a recorded observation")
    m.add("osdiB7KRecover", f"{k_recover:.3f}",
          f"{relpath(B7_ITIGER_CALIB_1M)}: recovery.wall_s / 91.01 (xzgpu 1% anchor, "
          f"quoted by {relpath(B7_ITIGER_CALIB_README)}); used as Addendum 4/5's Stage-1 "
          "recovery-band scaling factor")
    m.add("osdiB7Calib1MBuildWall", f"{calib1m_wall:.3f}",
          f"{relpath(B7_ITIGER_CALIB_1M)}: build_info.wall_s, seconds")
    m.add("osdiB7Calib1MPeakRSS", f"{calib1m_peak_gb:.2f}",
          f"{relpath(B7_ITIGER_CALIB_1M)}: build_info.peak_rss.vmhwm "
          f"({tex_num(calib1m_peak_kb)} kB) / 1e6, GB, 2dp -- same kB/1e6 convention as "
          "osdiB7Calib10MPeakRSS")
    m.add("osdiB7Calib1MRecovery", f"{recovery_1m_wall:.2f}",
          f"{relpath(B7_ITIGER_CALIB_1M)}: recovery.wall_s, seconds (EXP-A4 replay, "
          "compact_every=500, the same cadence as osdiB7Recovery30M); "
          "recovery.digest_equal is true")
    m.add("osdiB7Calib1MSegmentBytes", f"{calib1m_segment_gb:.3f}",
          f"{relpath(B7_ITIGER_CALIB_1M)}: build_info.store_bytes.segment_bytes / 1e9, GB")
    # osdiB7Calib10MRecovery: itiger-calib-10m.json carries no "recovery" field
    # (Stage 0's EXP-A4 recovery replay ran only at 1M, per the calibration
    # README's "What ran" §3) -- so no such macro exists, deliberately.

    # =================================================================
    # 100M (Stage 1, landed -- merge of fe52997)
    # =================================================================
    for path in (B7_BUILD_100M, B7_SCALE_CURVE_100M, B7_SCALE_CURVE_100M_RAW,
                 B7_CHECK_FULL_100M, B7_RECOVERY_100M_CE5000,
                 B7_VERSION_HISTORY_100M, B7_QUERYFLOOR_100M,
                 B7_BUILD_COMPACTION_WALLS_100M):
        _b7_check_sha256(path, readme_sha)

    build100 = json.loads(B7_BUILD_100M.read_text(encoding="utf-8"))
    curve100 = json.loads(B7_SCALE_CURVE_100M.read_text(encoding="utf-8"))
    curve100_raw = json.loads(B7_SCALE_CURVE_100M_RAW.read_text(encoding="utf-8"))
    check100 = json.loads(B7_CHECK_FULL_100M.read_text(encoding="utf-8"))
    rec100_5000 = json.loads(B7_RECOVERY_100M_CE5000.read_text(encoding="utf-8"))
    vh100 = json.loads(B7_VERSION_HISTORY_100M.read_text(encoding="utf-8"))
    qf100 = json.loads(B7_QUERYFLOOR_100M.read_text(encoding="utf-8"))
    walls100 = json.loads(B7_BUILD_COMPACTION_WALLS_100M.read_text(encoding="utf-8"))

    # --- one content digest ties every 100M sidecar to the same store ---
    for rec, path in ((build100, B7_BUILD_100M), (curve100, B7_SCALE_CURVE_100M),
                       (check100, B7_CHECK_FULL_100M), (vh100, B7_VERSION_HISTORY_100M)):
        eq(rec["dataset"]["digest"], B7_100M_STORE_DIGEST,
           f"{relpath(path)}: dataset.digest matches the frozen 100M store digest")
        eq(rec["result_digest"], B7_100M_STORE_DIGEST,
           f"{relpath(path)}: result_digest matches the frozen 100M store digest")
    require(rec100_5000["digest_compare"]["digest_equal"] is True,
            f"{relpath(B7_RECOVERY_100M_CE5000)}: digest_compare.digest_equal is true")
    eq(rec100_5000["digest_compare"]["src_digest"], B7_100M_STORE_DIGEST,
       f"{relpath(B7_RECOVERY_100M_CE5000)}: digest_compare.src_digest matches the "
       "frozen 100M store digest")
    eq(rec100_5000["digest_compare"]["replayed_digest"], B7_100M_STORE_DIGEST,
       f"{relpath(B7_RECOVERY_100M_CE5000)}: digest_compare.replayed_digest matches "
       "the frozen 100M store digest")

    # --- build: wall, peak RSS, manifest/segment bytes ---
    bi100 = build100["build_info"]
    wall100_s = bi100["wall_s"]
    eq(wall100_s, build100["falsifiers"]["build_wall"]["measured_s"],
       f"{relpath(B7_BUILD_100M)}: build_info.wall_s matches falsifiers.build_wall.measured_s")
    eq(round(wall100_s, 3), 28372.936, "B7 frozen: 100M build wall_s")

    peak100_vmhwm_kb = bi100["peak_rss"]["vmhwm"]
    eq(peak100_vmhwm_kb, build100["falsifiers"]["build_vmhwm_h1_h2"]["measured_kb"],
       f"{relpath(B7_BUILD_100M)}: build_info.peak_rss.vmhwm matches "
       "falsifiers.build_vmhwm_h1_h2.measured_kb")
    # Campaign convention (stated and cross-checked against the Stage-0 10M
    # anchor in falsifiers.build_vmhwm_h1_h2.unit_note): "GB" = vmhwm_kb /
    # 1e6, not true decimal GB or GiB -- recomputed here, not trusted.
    peak100_rss_gb = round(peak100_vmhwm_kb / 1e6, 2)
    eq(peak100_rss_gb, build100["falsifiers"]["build_vmhwm_h1_h2"]["measured_gb"],
       f"{relpath(B7_BUILD_100M)}: vmhwm/1e6, rounded 2dp, matches "
       "falsifiers.build_vmhwm_h1_h2.measured_gb (the campaign's own 'GB' convention)")
    require("vmhwm_kb / 1,000,000" in build100["falsifiers"]["build_vmhwm_h1_h2"]["unit_note"],
            f"{relpath(B7_BUILD_100M)}: falsifiers.build_vmhwm_h1_h2.unit_note states the "
            "campaign's vmhwm_kb/1e6 'GB' convention")
    eq(peak100_rss_gb, 186.42, "B7 frozen: 100M build peak RSS, campaign-convention GB")

    manifest100_bytes = bi100["store_bytes"]["manifest_bytes"]
    eq(manifest100_bytes, build100["falsifiers"]["manifest_bytes"]["measured_bytes"],
       f"{relpath(B7_BUILD_100M)}: build_info.store_bytes.manifest_bytes matches "
       "falsifiers.manifest_bytes.measured_bytes")
    eq(manifest100_bytes, 206946, "B7 frozen: 100M manifest bytes")

    segment100_bytes = bi100["store_bytes"]["segment_bytes"]
    segment100_gb = round(segment100_bytes / 1e9, 3)
    eq(segment100_gb, build100["falsifiers"]["segment_bytes"]["measured_gb"],
       f"{relpath(B7_BUILD_100M)}: build_info.store_bytes.segment_bytes / 1e9, rounded "
       "3dp, matches falsifiers.segment_bytes.measured_gb")
    eq(segment100_gb, 5.218, "B7 frozen: 100M segment bytes, GB")

    # steady-decile median: build_info.steady_decile_median_ops_per_s is
    # null for this run (build_info.ops_per_s_by_decile_source: the
    # harness's own field was null, so ops_per_s_by_decile itself is
    # derived from build-100m.stdout.log's progress ticks, not the
    # harness) -- recomputed here and cross-checked against the one
    # aggregate this record does carry, falsifiers.steady_decile_ops_per_s.
    require(bi100.get("steady_decile_median_ops_per_s") is None,
            f"{relpath(B7_BUILD_100M)}: build_info.steady_decile_median_ops_per_s is "
            "null/absent, as flagged by ops_per_s_by_decile_source")
    decile100_ops = [d["ops_per_s"] for d in bi100["ops_per_s_by_decile"]]
    eq(len(decile100_ops), 10, "B7 frozen: 100M build decile count")
    decile100_median = statistics.median(decile100_ops)
    close(decile100_median, build100["falsifiers"]["steady_decile_ops_per_s"]["measured"], 0.06,
          f"{relpath(B7_BUILD_100M)}: recomputed median(ops_per_s_by_decile[*].ops_per_s) "
          "matches falsifiers.steady_decile_ops_per_s.measured")
    eq(round(decile100_median, 1), 3738.9, "B7 frozen: 100M steady-decile median ops/s")

    # --- compaction share of the build wall (build-100m-compaction-
    # walls.json, cross-checked against build_info's own embedded copy) ---
    eq(bi100["compaction_wall_share"], walls100,
       f"{relpath(B7_BUILD_100M)}: build_info.compaction_wall_share matches "
       f"{relpath(B7_BUILD_COMPACTION_WALLS_100M)} verbatim (the same content, embedded)")
    compaction_only_pct = walls100["compaction_only_share_of_wall_pct"]
    recomputed_compaction_s = (walls100["in_loop_compaction_overhead_estimate_s"]
                                + walls100["final_compaction_wall_s_authoritative"])
    close(round(100 * recomputed_compaction_s / walls100["total_wall_s"], 2), compaction_only_pct,
          0.01,
          f"{relpath(B7_BUILD_COMPACTION_WALLS_100M)}: recomputed 100 * (in_loop_compaction_"
          "overhead_estimate_s + final_compaction_wall_s_authoritative) / total_wall_s "
          "matches compaction_only_share_of_wall_pct")
    eq(walls100["total_wall_s"], wall100_s,
       f"{relpath(B7_BUILD_COMPACTION_WALLS_100M)}: total_wall_s matches "
       f"{relpath(B7_BUILD_100M)}'s build_info.wall_s")
    eq(compaction_only_pct, 83.76, "B7 frozen: 100M compaction share of build wall, %")

    # --- query-ready floor: queryfloor-100m.json cross-checked against
    # the copy build-100m.json itself carries ---
    qf100_vmhwm_kb = qf100["vmhwm_kb"]
    eq(qf100_vmhwm_kb, build100["query_ready_floor"]["vmhwm_kb"],
       f"{relpath(B7_QUERYFLOOR_100M)}: vmhwm_kb matches build-100m.json's own "
       "query_ready_floor.vmhwm_kb")
    eq(qf100["n_ok"], 6, f"{relpath(B7_QUERYFLOOR_100M)}: n_ok is 6/13 (7 operators refused "
       "by the cost guardrail even at the query-ready-floor probe)")
    query100_floor_gb = round(qf100_vmhwm_kb / 1e6, 2)
    eq(query100_floor_gb, build100["query_ready_floor"]["vmhwm_gb"],
       f"{relpath(B7_QUERYFLOOR_100M)}: vmhwm_kb/1e6, rounded 2dp, matches build-100m.json's "
       "own query_ready_floor.vmhwm_gb")
    eq(query100_floor_gb, 19.67, "B7 frozen: 100M query-ready floor, GB")

    # --- check --full: single run (no clean/contended split at 100M) ---
    check100_wall = check100["single_run"]["wall_s"]
    eq(check100["single_run"]["job_id"], "213190",
       f"{relpath(B7_CHECK_FULL_100M)}: single_run.job_id is 213190")
    require(check100["single_run"]["raw"]["healthy"] is True,
            f"{relpath(B7_CHECK_FULL_100M)}: single_run.raw.healthy is true")
    eq(round(check100_wall, 3), 336.353, "B7 frozen: 100M check --full wall_s")

    # --- recovery: cadence 5000 only -- no cadence-500 100M run exists
    # (recovery-100m-ce5000.json's own protocol_note says so explicitly:
    # ~45h at ce500 was pre-judged infeasible within the 2-day Slurm wall
    # and would only re-measure D-164 again). osdiB7Recovery100M aliases
    # this single measurement rather than sitting PENDING forever for a
    # run that was never planned -- the §4 record layout has no
    # recovery-100m.json file at all, only recovery-100m-ce5000.json.
    require("no 500-cadence 100M run" in rec100_5000["protocol_note"],
            f"{relpath(B7_RECOVERY_100M_CE5000)}: protocol_note states there is no "
            "500-cadence 100M run to reconcile against")
    recovery100_5000_s = round(rec100_5000["replay_wall_s"], 1)
    eq(recovery100_5000_s, 22717.7, "B7 frozen: 100M recovery wall_s, cadence 5000")

    # --- version_history: single run (3 reps), medians recomputed ---
    sr_vh100 = vh100["single_run"]
    eq(sr_vh100["job_id"], "213191", f"{relpath(B7_VERSION_HISTORY_100M)}: single_run.job_id")
    reps100 = sr_vh100["reps"]
    eq(len(reps100), 3, f"{relpath(B7_VERSION_HISTORY_100M)}: single_run has 3 reps")
    vh100_wall_median = statistics.median(r["wall_ms"] for r in reps100)
    eq(vh100_wall_median, sr_vh100["wall_ms_median"],
       f"{relpath(B7_VERSION_HISTORY_100M)}: recomputed median(reps[*].wall_ms) matches "
       "single_run.wall_ms_median")
    vh100_rss_median_kb = statistics.median(r["vmhwm_kb"] for r in reps100)
    eq(vh100_rss_median_kb, sr_vh100["vmhwm_kb_median"],
       f"{relpath(B7_VERSION_HISTORY_100M)}: recomputed median(reps[*].vmhwm_kb) matches "
       "single_run.vmhwm_kb_median")
    vh100_wall_s = round(vh100_wall_median / 1000, 3)
    vh100_rss_gb = round(vh100_rss_median_kb / 1e6, 3)
    eq(vh100_wall_s, 18.982, "B7 frozen: 100M version_history wall_s")
    eq(vh100_rss_gb, 12.817, "B7 frozen: 100M version_history VmHWM, GB")

    # --- scale-curve: 6/13 executed, 7/13 refused by the cost guardrail.
    # Executed operators' p50s recomputed from the raw per-rep timings,
    # cross-checked against the raw file's own p50_ms and the aggregated
    # summary's measured_p50_ms (single run, no clean/contended split). ---
    pops100 = curve100["per_operator_p50_ms"]
    eq(set(pops100), set(B7_SCALE_CURVE_OPS),
       f"{relpath(B7_SCALE_CURVE_100M)}: per_operator_p50_ms operator set matches "
       "the known 13-operator registry")
    raw100_by_query = {r["query"]: r for r in curve100_raw["results"]["native"]}
    eq(set(raw100_by_query), set(B7_SCALE_CURVE_OPS),
       f"{relpath(B7_SCALE_CURVE_100M_RAW)}: results.native operator set matches "
       "the known 13-operator registry")
    refused100_ops = {op for op in B7_SCALE_CURVE_OPS if "error" in pops100[op]}
    eq(refused100_ops, set(B7_SCALE_CURVE_100M_REFUSED_NO_ESTIMATE) | {"reach.window"},
       f"{relpath(B7_SCALE_CURVE_100M)}: exactly 7 operators refused (reach.window + the "
       "six unanticipated CostError refusals)")
    executed100_p50_by_op: dict[str, float] = {}
    for op_id in B7_SCALE_CURVE_OPS:
        if op_id in refused100_ops:
            eq(pops100[op_id]["measured_p50_ms"], None,
               f"{relpath(B7_SCALE_CURVE_100M)}: {op_id}: refused operator's "
               "measured_p50_ms is null, not a fabricated figure")
            raw_row = raw100_by_query[op_id]
            require(raw_row["ok"] is False and raw_row["p50_ms"] is None,
                    f"{relpath(B7_SCALE_CURVE_100M_RAW)}: {op_id}: refused in the raw "
                    "file too (ok=false, p50_ms=null)")
            continue
        summary = pops100[op_id]
        raw_row = raw100_by_query[op_id]
        recomputed = statistics.median(raw_row["timings_ms"])
        close(recomputed, raw_row["p50_ms"], 0.01,
              f"{relpath(B7_SCALE_CURVE_100M_RAW)}: {op_id}: recomputed median(timings_ms) "
              "matches this row's own p50_ms")
        eq(raw_row["p50_ms"], summary["measured_p50_ms"],
           f"{relpath(B7_SCALE_CURVE_100M)}: {op_id}: per_operator_p50_ms.measured_p50_ms "
           f"matches {relpath(B7_SCALE_CURVE_100M_RAW)}'s own p50_ms")
        executed100_p50_by_op[op_id] = summary["measured_p50_ms"]
    eq(len(executed100_p50_by_op), 6, "B7 frozen: 100M scale-curve executed-operator count")

    # reach.window: refused, but with a numeric estimate (a dedicated
    # admission probe, same shape as 30M's) -- this is the one refused
    # operator whose ScaleCurveP50 macro stays PENDING-with-reason rather
    # than becoming an osdiB7Refused* macro.
    reach100 = curve100["reach_window_admission"]
    require(reach100["run"]["admitted"] is False,
            f"{relpath(B7_SCALE_CURVE_100M)}: reach_window_admission.run.admitted is false")
    require("time_est_ms=14,571" in reach100["note"],
            f"{relpath(B7_SCALE_CURVE_100M)}: reach_window_admission.note names the "
            "time_est_ms=14,571 estimate that tripped the ceiling")
    estimate100_ms = reach100["run"]["estimate"]["time_est_ms"]
    eq(estimate100_ms, reach100["run"]["time_est_ms"],
       f"{relpath(B7_SCALE_CURVE_100M)}: reach_window_admission.run.estimate.time_est_ms "
       "matches its own top-level time_est_ms")
    eq(estimate100_ms, 14571, "B7 frozen: 100M reach.window admission estimate, ms")

    # ---------------------------------------------------------------
    # 100M macros
    # ---------------------------------------------------------------
    m.add("osdiB7BuildWall100M", tex_float(round(wall100_s, 3)),
          f"{relpath(B7_BUILD_100M)}: build_info.wall_s, seconds")
    m.add("osdiB7PeakRSS100M", f"{peak100_rss_gb:.2f}",
          f"{relpath(B7_BUILD_100M)}: build_info.peak_rss.vmhwm / 1e6, GB, 2dp "
          f"({tex_num(peak100_vmhwm_kb)} KB) -- campaign convention per "
          "falsifiers.build_vmhwm_h1_h2.unit_note, not true decimal GB/GiB")
    m.add("osdiB7VersionHistoryWall100M", f"{vh100_wall_s:.3f}",
          f"{relpath(B7_VERSION_HISTORY_100M)}: single_run (job 213191), "
          "median(reps[*].wall_ms) / 1000, seconds")
    m.add("osdiB7VersionHistoryRSS100M", f"{vh100_rss_gb:.3f}",
          f"{relpath(B7_VERSION_HISTORY_100M)}: single_run (job 213191), "
          "median(reps[*].vmhwm_kb) / 1e6, GB")
    m.add("osdiB7ManifestBytes100M", tex_num(manifest100_bytes),
          f"{relpath(B7_BUILD_100M)}: build_info.store_bytes.manifest_bytes")
    m.add("osdiB7SegmentBytes100M", f"{segment100_gb:.3f}",
          f"{relpath(B7_BUILD_100M)}: build_info.store_bytes.segment_bytes / 1e9, GB")
    m.add("osdiB7CheckFullWall100M", f"{check100_wall:.3f}",
          f"{relpath(B7_CHECK_FULL_100M)}: single_run (job 213190), wall_s")
    m.add("osdiB7RecoveryCe5000At100M", tex_float(recovery100_5000_s),
          f"{relpath(B7_RECOVERY_100M_CE5000)}: replay_wall_s, rounded 1dp, seconds "
          "(compact_every=5000, job 213192); digest_compare.digest_equal true")
    m.add("osdiB7Recovery100M", tex_float(recovery100_5000_s),
          f"ALIAS of osdiB7RecoveryCe5000At100M -- {relpath(B7_RECOVERY_100M_CE5000)}'s "
          "own protocol_note states there is no cadence-500 100M run (Addendum 6 "
          "pre-judged it infeasible, ~45h extrapolated, over the 2-day Slurm wall, and "
          "would only re-measure D-164 again); the §4 record layout has no separate "
          "recovery-100m.json for a base cadence, so this macro reuses the campaign's "
          "single 100M recovery measurement rather than staying pending for a run that "
          "was never planned")
    for op_id, val in executed100_p50_by_op.items():
        frag = B7_SCALE_CURVE_OPS[op_id]
        m.add(f"osdiB7ScaleCurveP50{frag}100M", tex_float(val),
              f"{relpath(B7_SCALE_CURVE_100M)}: per_operator_p50_ms.{op_id}."
              "measured_p50_ms, ms (single run, job 213190), recomputed from "
              f"{relpath(B7_SCALE_CURVE_100M_RAW)}'s own timings_ms")
    m.add_pending("osdiB7ScaleCurveP50ReachWindow100M", "B7 (100M, cost guardrail)",
                  f"refused by the cost guardrail (time_est_ms {estimate100_ms}) -- "
                  f"{relpath(B7_SCALE_CURVE_100M)}: reach_window_admission.run, anticipated "
                  "by Addendum 5's restated reach.window prediction (the only refusal the "
                  "pre-registration foresaw); see osdiB7ReachWindowRefused100M and "
                  "osdiB7ReachWindowEstimateMs100M")
    for op_id in B7_SCALE_CURVE_100M_REFUSED_NO_ESTIMATE:
        frag = B7_SCALE_CURVE_OPS[op_id]
        m.add(f"osdiB7Refused{frag}100M", "true",
              f"{relpath(B7_SCALE_CURVE_100M)}: per_operator_p50_ms.{op_id}.error = "
              f"{pops100[op_id]['error']!r} -- refused by the cost guardrail with no "
              "numeric time_est_ms in the record (unlike reach.window's dedicated "
              "admission probe); NOT anticipated by SCALE_BUILD_FORECAST_2026-09-15.md "
              "(a new finding at 100M -- all 13 operators executed at 30M). No "
              "osdiB7ScaleCurveP50 macro is emitted for this operator at 100M: no "
              "measured p50 exists and no estimate exists to explain a PENDING one")
    m.add("osdiB7ReachWindowRefused100M", "true",
          f"{relpath(B7_SCALE_CURVE_100M)}: reach_window_admission.run.admitted is false "
          "(time_est_ms=14,571 > the 10,000 ceiling) -- unlike 30M (admitted), 100M's "
          "reach.window is refused, exactly as Addendum 5's restated admission policy "
          "predicted for this scale")
    m.add("osdiB7QueryFloor100M", f"{query100_floor_gb:.2f}",
          f"{relpath(B7_QUERYFLOOR_100M)}: vmhwm_kb / 1e6, GB (fresh read-only process, "
          "cold-open, job 213189, n_ok=6/13 -- 7 operators refused even at the "
          "query-ready-floor probe)")
    m.add("osdiB7BuildSteadyDecileMedian100M", tex_float(round(decile100_median, 1)),
          f"{relpath(B7_BUILD_100M)}: recomputed median(ops_per_s_by_decile[*].ops_per_s) "
          "-- build_info.steady_decile_median_ops_per_s itself is null for this run "
          "(ops_per_s_by_decile_source flags it), cross-checked instead against "
          "falsifiers.steady_decile_ops_per_s.measured, ops/s")
    m.add("osdiB7ReachWindowEstimateMs100M", tex_num(estimate100_ms),
          f"{relpath(B7_SCALE_CURVE_100M)}: reach_window_admission.run.estimate."
          "time_est_ms, ms")
    m.add("osdiB7CompactionShare100M", f"{compaction_only_pct:.2f}",
          f"{relpath(B7_BUILD_COMPACTION_WALLS_100M)}: compaction_only_share_of_wall_pct "
          "= 100 * (in_loop_compaction_overhead_estimate_s [checkpoint-delta-pair "
          "estimate over the 100 in-loop compactions, method field quoted below] + "
          "final_compaction_wall_s_authoritative [the 101st compaction, separately and "
          "authoritatively timed in build_info.finalisation_phases]) / total_wall_s. "
          f"method: {walls100['method']!r}. A wider "
          f"compaction_plus_all_finalization_share_of_wall_pct = "
          f"{walls100['compaction_plus_all_finalization_share_of_wall_pct']}% (adds gc+"
          "stats+digest) is also on record but not emitted as a separate macro")

    # osdiB7300MGate already covers both scales (scale-independent) --
    # see the 30M section above.


# --------------------------------------------------------------------------
# pending stubs (records not yet landed)
# --------------------------------------------------------------------------

def add_pending_stubs(m: Macros) -> None:
    pass
    # The DAG-phase (v1/v2/v3, all 40/40 cells), the R-18 probe (5/5
    # batches), and the addendum-1 main correction-storm cell grid (36/36
    # cells, storm-v1-main-grid-2026-09-15.json, Lane W2m) are all now
    # landed and scored -- see compute_c7_dag, compute_c7_r18, and
    # compute_c7_storm_v1 above. The five legacy stub names that once stood
    # in for that same grid under an earlier, never-committed attempt
    # (storm-campaign-2026-09.json: 12/36 cells complete, the other 24
    # blocked on an iTiger disk-quota incident, 2026-09-14) --
    # osdiTtfSpeedup, osdiStormCells, osdiStormFalseFresh,
    # osdiStormSpeedupN1k, osdiStormAvoidedN1k -- are retired: superseded
    # by the landed osdiStormV1* macros (compute_c7_storm_v1 above) and
    # referenced nowhere in the paper skeleton or paper/ under their own
    # names, so there is nothing left for them to stand in for. See the
    # module docstring's C7 section for the full provenance trail.

    # osdiLdbcExpressible/osdiLdbcExecuted/osdiLdbcValidated (C9,
    # independent-validation axis) have landed -- see compute_ldbc_ref_v1
    # above, reading benchmarks/ldbc-ref-v1/compare-2026-09-18.json (Lane
    # W2t, the Neo4j reference run that this stub named as the reason to
    # wait). No longer emitted here.

    # osdiLiveDays/osdiLiveAdvisories/osdiLiveCorrections (C10, live OSV
    # workload) have landed -- see compute_c10_live_osv above, reading
    # benchmarks/live-osv-v1/snapshot-2026-09-16.json, the first committed
    # record snapshot of the live-osv poller running on xzgpu. No longer
    # emitted here.

    # B7 100M: landed -- see compute_b7_scale above (merge of fe52997). Every
    # core 100M macro, 6/13 executed scale-curve p50s, and the 6 unestimated
    # refusals (osdiB7Refused<Op>100M) are landed; only
    # osdiB7ScaleCurveP50ReachWindow100M stays PENDING, with a reason naming
    # its own time_est_ms rather than "not landed yet" (compute_b7_scale
    # calls add_pending directly for that one, since the reason is derived
    # from the just-read record).


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--check", action="store_true",
        help=("verify every macro, then regenerate the .tex if it is stale "
              "or missing and re-verify the write; exits 0 once the file "
              "matches (this WRITES when stale -- use --check-only for a "
              "read-only gate)"),
    )
    ap.add_argument(
        "--check-only", "--no-write", dest="check_only", action="store_true",
        help=("verify every macro and exit 1 if the generated .tex is stale "
              "or missing, without writing anything -- the old strict "
              "--check behavior, kept for CI"),
    )
    args = ap.parse_args()
    if args.check and args.check_only:
        ap.error("--check and --check-only/--no-write are mutually exclusive")

    m = Macros()
    compute_c1(m)
    compute_c3(m)
    compute_b1_v2(m)
    _void_b1_v2_treatment_provenance(m)
    compute_b1_v2e(m)
    compute_b1_co7(m)
    compute_c4(m)
    compute_c5(m)
    compute_c6(m)
    compute_c8(m)
    compute_c7_dag(m)
    compute_c7_r18(m)
    compute_c7_storm_v1(m)
    compute_c7_storm_v2_probe(m)
    compute_c7_storm_v2(m)
    compute_d160(m)
    compute_d160_llm_direct_fix(m)
    compute_c2(m)
    compute_ladder(m)
    compute_longevity_soak(m)
    compute_longevity_rederived(m)
    compute_longevity_soak_two(m)
    compute_longevity_verify_and_replay2(m)
    compute_longevity_soak_hunt(m)
    compute_overload(m)
    compute_c10_live_osv(m)
    compute_ldbc_ref_v1(m)
    compute_b7_scale(m)
    add_pending_stubs(m)

    if FAILURES:
        print(f"VERIFICATION FAILED after {CHECKS} checks:", file=sys.stderr)
        for f in FAILURES:
            print(f"  - {f}", file=sys.stderr)
        return 1

    out_path = OUT_DIR / "osdi-macros.tex"
    text = m.render()
    old = out_path.read_text(encoding="utf-8") if out_path.exists() else None
    changed = old != text

    if args.check_only:
        # today's strict contract: verify only, never write.
        if changed:
            print(f"stale generated file: {out_path.name}", file=sys.stderr)
            return 1
        status = "up to date"
    elif args.check:
        # verify, and heal a stale/missing file rather than just refusing --
        # a fresh worktree or a merge that outran the gitignored paper/ tree
        # is not a verification failure, it is just an unwritten file.
        if changed:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text, encoding="utf-8")
            reread = out_path.read_text(encoding="utf-8")
            if reread != text:
                print(f"error: {out_path.name} did not verify after "
                      "regeneration", file=sys.stderr)
                return 1
            status = "regenerated"
        else:
            status = "up to date"
    else:
        if changed:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text, encoding="utf-8")
        status = "wrote"

    landed = sum(1 for _, v, _ in m.items if not v.startswith("\\errmessage"))
    pending = len(m.items) - landed
    print(f"osdi_paper_macros: {landed} landed macros, {pending} pending stubs, "
          f"{CHECKS} verifications, all passed.")
    print(f"  {status}: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

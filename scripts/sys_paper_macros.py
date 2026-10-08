#!/usr/bin/env python
"""Generate the submission campaign's number macros from committed receipts.

House rule (copied verbatim from ``scripts/tgir_paper_macros.py`` and
``scripts/paper_macros.py``): **assert, do not trust.** Every macro this
script emits is recomputed from the row-level fields of the record that
owns it, cross-checked against whatever aggregate the record states for
itself, and then checked against a frozen expected value in
``tests/test_sys_paper_macros.py``. A disagreement anywhere in that chain
is a hard failure -- the script refuses to write output, never silently
adjusts a number, and never falls back to a record's own summary field
without first recomputing it from the rows underneath.

This is Lane W (task W2)'s receipts machinery for the submission campaign. The
claims -> evidence map that assigns every macro name to a claim and a
record is an internal design memo (paper skeleton, 2026-09-15) S3 (gitignored,
internal; not shipped with this script, but every macro below documents
its own record path and field so the mapping is reconstructable without
it). Implemented here: every claim whose record has LANDED per that
skeleton --

  C1  benchmarks/crash-v1/eval-crash-campaign-2026-09-13.json
  C3  benchmarks/results-v1/b1-manifest-ab-2026-09{,-raw}.json (v1 B1 A/B) and
      benchmarks/results-v1/b1-manifest-v2-ab-2026-09{,-raw}.json (Lane W2f's
      B1-v2, manifest format 3 -- the reader torn-tail fix -- vs. the pinned
      format-2 soak engine; ``recB1v2*`` macros stand beside the v1-era
      ``recManifest*`` ones without overwriting them; no verdict macro --
      scoring is the coordinator's, per the internal freeze doc) and
      benchmarks/results-v1/b1-manifest-co7-chain-open-2026-09{,-raw}.json
      (Lane P-CO7's chain-open re-measurement on a confirmed-quiet host,
      treatment ``ebe1dc2``; ``recB1co7*`` macros stand beside the v2e-era
      ``recB1v2e*`` ones without overwriting them; the two
      ``recB1WorstPhaseOpen*`` macros are arithmetic on the co7 per-delta
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
  D160-collegemsg (Lane W2c, not in the design memo's C-numbering --
      the coordinator's D-160 ruling deliverable) --
      benchmarks/d160-collegemsg-v1/{manifest,rows}-2026-09-14.json, the
      CollegeMsg coverage/conditional-accuracy/UCR re-measurement under the
      production claim gate that also drops `unverifiable` claims (see
      docs/STABILITY.md section 9). Its pre-D-160-gate counterparts
      (recOldGate*) are parsed out of docs/site_facts.json's
      `unsupported_claims` fact and cross-checked against that same
      STABILITY.md section, never hard-coded independently of both. The
      `llm_direct` follow-up re-run under the real-tokenizer budget fix
      (`manifest-llm-direct-fix-2026-09-14.json` /
      `rows-llm-direct-fix-2026-09-14.json`, job 212231) has now landed --
      `recD160LlmDirectCoverageFixed` and its siblings
      (`recD160LlmDirectErrorsFixed`/`RawEmFixed`/`TokenizerFixed`/
      `BudgetFixed`) are computed by `compute_d160_llm_direct_fix` below.
      The pre-fix record's own numbers (`recD160LlmDirectCarrying`,
      `recD160LlmDirectOverflowErrors`) are unchanged and still stand
      beside them, per the campaign's non-overwrite discipline.
  C7  (partial) benchmarks/storm-v1/storm-campaign-dag-{,v2-,v3-}2026-09.json
      (+ each one's -rows.jsonl) -- the DAG-phase v1/v2/v3 grids, all 40/40
      cells each -- and benchmarks/storm-v1/storm-r18-probe-2026-09.json
      (+ -rows.jsonl) -- the N=10,000 c1 seed-0 R-18 characterization probe,
      5/5 batches, not wall-capped. An earlier attempt at the main
      correction-storm cell grid (addendum-1, ``storm-campaign-2026-09.json``,
      12/36 cells complete, 24 blocked on an iTiger disk-quota incident) was
      never committed and stays a dead end -- its own quantities
      (``recStormCells``/``recStormFalseFresh``/``recTtfSpeedup``/
      ``recStormSpeedupN1k``/``recStormAvoidedN1k``) were retired (no
      longer emitted, PENDING or otherwise) since no record under that
      name ever landed and no paper text references those legacy names.
      Addendum-1's grid itself since **has** landed under
      a different name and seam (Lane W2m; ``storm-v1-main-grid-2026-09-15
      .json`` + its -rows.jsonl, five submissions across the same quota
      incident, 36/36 cells, commit ``8962b78``, pre-D-161-rollout) -- its
      macros are ``recStormV1*`` (``compute_c7_storm_v1`` below), which
      resolves the previously-PENDING ``recStormV1SpeedupN1kSeed0`` stub
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
      job 212294) -- **has** also landed; its macros are ``recStormV2*``
      (``compute_c7_storm_v2`` below) and stand beside ``recStormV1*``
      without overwriting or resolving them -- a v1/v2 speedup comparison
      is prose-only (README.md's own two main-grid sections), never a
      macro. The survivor-fraction/precision pair
      (``recStormV2SurvivorFractionC1Median``/``recStormV2PrecisionC1Median``)
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
      (``recStormV2C1Batches``) are read from it in memory (``tarfile``,
      nothing extracted to the repo) and matched to their cell via each
      merged-grid row's own ``record`` field. The overall and per-store
      medians (``recStormV2{SurvivorFraction,Precision}{Synth,CollegeMsg}
      C1Median``) are computed in ``compute_c7_storm_v2`` below alongside
      the rest of the c1-mix quantities.

  W2ad (external baselines -- Neo4j 5.26 recompute / differential-dataflow
      IVM / TGMS same-host control, Lane C1-records, landed 2026-10-08) --
      benchmarks/external-v1/{neo4j-recompute,ivm-differential}-2026-10-07
      .json (+ -rows.jsonl) and tgms-control-2026-10-05.json (+
      -rows.jsonl), all three sha256-checked against that directory's own
      SHA256SUMS.txt. The 29 ``recExt1*``/``recExt2*`` macros that stood
      PENDING since this file's first draft of ``compute_external_baselines``
      (naming the not-yet-landed record paths) are landed here, plus one
      sibling (``recExt1SpeedupCellsScored``, the denominator
      ``recExt1SpeedupCellsMeeting`` counts against). Per
      benchmarks/external-v1/README.md's own A11 reading note, every ratio
      against a committed TGMS number reads the SAME-HOST tgms-control
      record (lane T1, measured on xzgpu, the same host as lanes N1/D1)
      rather than the committed storm-v2 cluster record (measured on
      iTiger) -- except ``recExt1RatioGr{Min,Median,Max}`` and
      ``recExt1ProbeRatio``, which the README's A11 explicitly keeps
      scored against the committed cluster record (P-EXT1(a), "the
      committed internal baseline is representative", on the 18
      synth-iv-60k L2-partial cells + the probe only). One macro
      (``recExt2UnanswerableMs``) lands as the literal text "not measured"
      -- the withheld-correction cell's own record carries no timing
      field for it, only booleans.

  W2ae (sum-mode time-to-fresh reconstruction, landed 2026-10-08) --
      ``tgms/eval/storm.py`` has two TTF measurement modes: ``sum``
      (``ttf_ms = check_wall_ms + refresh_wall_ms``) and ``end-to-end``
      (one continuous timed check-then-refresh interval). The committed
      storm-v2 main grid (all 36 cells) and 18 of the 19 external-v1
      tgms-control cells ran in ``end-to-end`` mode -- and because the
      ``tgms-L0`` interval runs first each batch and republishes every
      artifact it nominates, the ``tgms-L1`` interval that follows finds
      its own nominated set already fresh and refreshes nothing: its
      *recorded* ``ttf_ms`` collapses to roughly its own (separately
      measured) ``check_wall_ms`` alone (``recStormV2LOneE2eOverCheckMedian``
      below, median 0.999 over every end-to-end cell/batch), not
      ``check_wall_ms + refresh_wall_ms``. None of the already-landed
      ``recStormV2Speedup*``/``recExt1*``/``recExt2Ratio*`` macros above
      are touched -- this section lands their sum-mode-corrected siblings
      beside them (``compute_c7_storm_v2_sum_mode``,
      ``compute_c7_storm_v2_probe_sum_mode``, ``compute_ext_sum_mode``
      below), recomputing the sum-mode value directly from each cell's
      per-batch rows rather than its (misleading, for L1 under
      end-to-end) committed ``ttf_p50_ms``. For the main grid and the
      R-18 probe those per-batch rows are already committed and read
      fresh, live, every run; the control's own ``-rows.jsonl`` sidecar
      carries only per-cell equality manifests, never per-batch detail,
      so its raw per-batch rows landed as their own record addendum
      (``tgms-control-2026-10-05-batches.jsonl`` + ``.SOURCES.txt``,
      2026-10-08, pulled read-only from xzgpu -- see
      benchmarks/external-v1/README.md's "Per-batch rows (addendum
      2026-10-08)" section) and are read fresh, live, every run too, via
      ``_read_control_batches`` below, sha256-gated the same way as
      every other file in that directory. One finding this task's own
      background note did not anticipate:
      the R-18 probe turns out to already run in ``sum`` mode (every one
      of its 5 batch rows carries ``ttf_mode: "sum"``) and so was never
      affected by the end-to-end bug at all -- ``recStormV2SpeedupSumProbe``
      recomputes to the same number (to rounding) as the existing,
      already-correct ``recStormV2ProbeSpeedupN10k``, landed anyway both
      because the task asked for it and as a live self-check that this
      section's sum-mode arithmetic reproduces a value that needed no
      correcting.

  W2m (storm-v2 R-18 probe, job 212295, addendum-3) --
      benchmarks/storm-v1/storm-v2-r18-probe-2026-09-15.json (+
      -rows.jsonl), the same cell as the v1 R-18 probe above
      (synth-iv-60k/c1/N=10,000/seed=0/5 batches) rerun post-D-161-rollout
      under commit ``fdd393c``. ``recStormV2Probe*``
      (``compute_c7_storm_v2_probe`` below) land beside the still-untouched
      ``recR18*`` (v1) macros and beside ``recStormV2*`` (the different
      36-cell main-grid record) -- no v1-vs-v2 or probe-vs-grid comparison
      macro here, only prose (README.md's own "R-18 probe v2 (addendum-3)"
      table); ``recStormSpeedupShrinksWithN`` in particular is a verdict,
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

  W2p (P-SOAK2, post-fix second soak, internal plan memo Sec 4.3b) --
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
      below computes every ``recSoak*Two`` macro; the Gate E table's own
      verdict column is prose (gate_e_report-2.md), not emitted here.
      Added later (2026-09-18, lane W2v): compactions-2.jsonl (the writer's
      raw per-compaction log, copied from xzgpu) and reader_onset_rows-2.json
      (the edge-row count at the reader-error storm's earliest/latest
      onset, reconciling compactions-2.jsonl's ``time.perf_counter()``
      clock against reader_error_counts_by_class-2.json's wall-clock onset
      via metrics.jsonl's ``compactions_total`` counter -- method and
      error bound in the side-file itself) round out the same
      ``compute_longevity_soak_two`` function's ``recSoakReaderOnset*Two``
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
      ``compute_longevity_soak_hunt`` below computes every ``recSoak*Hunt``
      macro; ``recSoakHuntPatternReproduced`` states the pre-registration's
      own clause (e) non-conclusion (a negative 6h result is not evidence
      the pattern is gone) as its provenance, not as a number.

  W2x (P-SOAK3, the 72h third soak, commit 9e21a83 pre-fix engine) --
      benchmarks/longevity-v1/README.md's "Soak 3 (72 h)" section --
      longevity-synth-1m-native-2.json (manifest), compactions-3.jsonl,
      recoveries-3.jsonl, host_load-3.log and verify-full-3.txt (all five
      whole-file sha256-checked against that section's own "Files added
      here" table), plus rss_slopes-3.json, throughput-3.json,
      reader_op_error-3.json, reader_onset_rows-3.json and
      writer_error_counts_by_class-3.json (derived locally, no xzgpu twin
      to hash against, per that section's own note). Only 9 of the
      pre-registered 12 writer lives ran (the ``gc_mid_delete``
      restart-arming defect, fixed later at ``3267725``/``7b5d6ba``, both
      after ``9e21a83``). Unlike P-SOAK2's monotonic, never-healing reader
      OSError/StateError storm, the D-088 ledger's 17 first-onset lines
      understate a reconstructed 662 true active/healed episodes across 8
      readers x 2 classes (every combination heals repeatedly) -- root
      cause not established here; P-SOAK4 re-measures the fixed engine.
      ``compute_longevity_soak_three`` below computes every
      ``recSoak*Three`` macro, including ``recSoakWriterErrorsClassThree``
      (recomputed from writer_error_counts_by_class-3.json, this lane's own
      per-life/per-class writer-error side-file). No verdict macro -- Gate E
      scoring is the coordinator's.

  W-lane (the original soak's full-mode verify + REPLAY-2) --
      benchmarks/longevity-v1/verify-full-2026-09-15.txt (two `tgms check`
      entries concatenated: the pre-replay check of the original store,
      then the post-hoc full verify of REPLAY-2's own replayed store) and
      replay-check-2-2026-09.json (REPLAY-2 itself, the post-D-087-fix
      replay that completed, unlike W2j's OOM-killed attempts 1/2 above).
      Both whole-file sha256-checked (frozen here, same discipline as
      W2j's pair). ``compute_longevity_verify_and_replay2`` below computes
      every ``recSoakVerifyFull*``/``recSoakReplay2*`` macro; no verdict
      macro (``CORRUPT`` is the verifier's own text, not scored here).

  W-lane (P-OV1, the xzgpu-calibrated overload sweep, EXP-B4) --
      benchmarks/overload-v1/{overload-2026-09-15,overload-2026-09-15-rep2}.json
      (2 reps, `--clients 1 2 4 8 16 32 64 --max-concurrent 8`,
      `entity_history` under load) + hwm-checkpoints-v3.json (the "Pinned"
      VmHWM localization). ``compute_overload`` below computes every
      ``recOverload*`` macro, sha256-checked by hash membership against
      benchmarks/overload-v1/SHA256SUMS; no verdict macro (the per-clause
      PASS/REFUTED scoring is README.md's own prose).

C10 (live OSV workload) -- benchmarks/live-osv-v1/snapshot-2026-09-16.json,
Lane C10-snap's first committed record snapshot of the live-osv poller
running on xzgpu (docs/design/LIVE_WORKLOAD_OSV_DESIGN_2026-09-13.md).
``recLiveDays``/``recLiveAdvisories``/``recLiveCorrections`` are computed
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
fully landed. ``compute_b7_scale`` below computes every ``recB7*{30M,100M}``
macro, the Stage-0 anchors, and the (scale-independent) ``recB7300MGate``,
whole-file sha256-checking every record it reads against the sha256 table
in its own README (``benchmarks/scale-v1/README.md`` for Stage 1,
``itiger-calib-2026-09.README.md`` for Stage 0) before trusting anything
inside it. At 100M, 7 of the 13 scale-curve operators are refused by the
cost guardrail: ``reach.window`` (anticipated by Addendum 5, carries a
numeric ``time_est_ms`` estimate) stays a PENDING
``recB7ScaleCurveP50ReachWindow100M`` naming that estimate as the reason;
the other six (unanticipated, no numeric estimate in the record) land as
``recB7Refused<Op>100M = true`` instead of a fabricated p50.
``recB7Recovery100M`` aliases ``recB7RecoveryCe5000At100M`` since no
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
``recLdbcInterim*`` macros that let the paper cite the reference-side
fix. This resolves the three stubs that used to stand here
(``recLdbcExpressible``/``recLdbcExecuted``/``recLdbcValidated``) and
lands the rest of the scorecard beside them --
``compute_ldbc_ref_v1`` below computes every ``recLdbc*`` macro, every
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
``\\expandafter\\newcommand\\csname rec<Name>\\endcsname{<value>}`` rather
than a bare ``\\newcommand{\\rec<Name>}``: roughly two-thirds of the ~400
names carry digit tokens (e.g. ``recB7BuildWall30M``), and a LaTeX
control-sequence name may not contain a digit outside ``\\csname``. The
file also defines, once at its top, ``\\providecommand{\\rec}[1]{\\csname
rec#1\\endcsname}`` so manuscript prose can write ``\\rec{B7BuildWall30M}``
instead of the raw ``\\csname`` form. Digit-free names still work under
their bare ``\\recFoo`` spelling unmodified, since ``\\csname
recFoo\\endcsname`` denotes that same control sequence.

Usage:  $HOME/.venvs/tgms/bin/python scripts/sys_paper_macros.py [--check | --check-only]

Every mode first recomputes and verifies all macros (assert, do not trust,
per the house rule above); a verification failure exits 1 regardless of
flags. With no flag, the script writes
``paper/sys/generated/sys-paper-macros.tex`` unconditionally (creating
``paper/sys/generated/`` if needed). ``--check`` additionally regenerates
that file when it is stale or missing -- e.g. a fresh worktree, or after a
merge, whose gitignored ``paper/`` tree lags the committed records it is
derived from -- and re-verifies the write, printing "regenerated" or "up to
date"; it exits 0 once the file matches, never failing merely because the
file was stale. ``--check-only`` (alias ``--no-write``) is the old strict
contract: it never writes, and exits 1 with "stale generated file: ..." if
the file would differ -- use this where a write is undesired (e.g. a CI
gate). Nothing under ``paper/`` is committed (``paper/`` is gitignored
publicly); this is the local convention this script and
``scripts/sys_paper_figures.py`` share for that directory. As of 2026-09
no CI workflow invokes this script at all (nothing under ``.github/workflows/``
references it) -- there is no committed generated file for CI to check
staleness against, so CI's actual paper-side gate is just that this script
and its test suite (``tests/test_sys_paper_macros.py``) pass; ``--check-only``
is provided for if/when a workflow starts calling it directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
import tarfile
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "paper" / "sys" / "generated"

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

# Lane C1-records (benchmarks/external-v1/README.md, landed 2026-10-08):
# the three external-baseline campaigns compute_external_baselines below
# reads -- Neo4j 5.26 full recompute (N1), differential-dataflow IVM (D1),
# and the TGMS same-host control (T1). Every file this lane's own
# compute_* function reads is sha256-checked against
# EXTERNAL_V1_SHA256SUMS by filename below; the two -rows.jsonl sidecars
# are read only for their per-row `result.config.cypher`/`families_present`
# blocks (the query-family lists), not for per-burst timing -- that comes
# from each top-level file's own summary.per_cell.
EXTERNAL_V1 = ROOT / "benchmarks" / "external-v1"
EXTERNAL_V1_SHA256SUMS = EXTERNAL_V1 / "SHA256SUMS.txt"
EXTERNAL_NEO4J = EXTERNAL_V1 / "neo4j-recompute-2026-10-07.json"
EXTERNAL_NEO4J_ROWS = EXTERNAL_V1 / "neo4j-recompute-2026-10-07-rows.jsonl"
EXTERNAL_IVM = EXTERNAL_V1 / "ivm-differential-2026-10-07.json"
EXTERNAL_IVM_ROWS = EXTERNAL_V1 / "ivm-differential-2026-10-07-rows.jsonl"
EXTERNAL_TGMS_CONTROL = EXTERNAL_V1 / "tgms-control-2026-10-05.json"
# Lane W2ae (sum-mode time-to-fresh reconstruction): the committed raw
# per-batch rows behind EXTERNAL_TGMS_CONTROL's 19 cells (record
# addendum, 2026-10-08) -- the repo's own tgms-control-2026-10-05-rows
# .jsonl sidecar carries only per-cell equality manifests, never this
# per-batch detail. See
# benchmarks/external-v1/README.md's "Per-batch rows (addendum
# 2026-10-08)" section and the module docstring's own W2ae section for
# the full story; `_read_control_batches` below is the reader, sha256-
# gated against EXTERNAL_V1_SHA256SUMS the same way every other file in
# this directory is.
EXTERNAL_TGMS_CONTROL_BATCHES = EXTERNAL_V1 / "tgms-control-2026-10-05-batches.jsonl"
EXTERNAL_TGMS_CONTROL_BATCHES_SOURCES = (
    EXTERNAL_V1 / "tgms-control-2026-10-05-batches.SOURCES.txt")

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
# per the coordinator's ruling, those six get a landed recB7Refused<Op>
# 100M = true macro instead of a PENDING recB7ScaleCurveP50<Op>100M (no
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

# Lane W2x -- P-SOAK3, the 72h third soak (commit 9e21a83, pre-fix engine --
# the gc_mid_delete restart-arming defect this run's harness carries is
# fixed later at 3267725/7b5d6ba, both after 9e21a83). Every path below is
# whole-file sha256-checked against benchmarks/longevity-v1/README.md's
# "Soak 3 (72 h)" section's own "Files added here" table, same discipline
# as P-SOAK2's five-file table above -- not an independently-frozen
# first-read digest. rss_slopes-3.json, throughput-3.json,
# reader_op_error-3.json and reader_onset_rows-3.json have no xzgpu twin to
# hash against (derived locally from metrics.jsonl, which stays on xzgpu
# per the same PI ruling as every earlier soak/stormhunt) -- README.md
# says so explicitly, so this lane reads them without a sha256 gate,
# same as reader_onset_rows-2.json's own convention.
LONGEVITY_MANIFEST_THREE = LONGEVITY_DIR / "longevity-synth-1m-native-2.json"
LONGEVITY_COMPACTIONS_THREE = LONGEVITY_DIR / "compactions-3.jsonl"
LONGEVITY_RECOVERIES_THREE = LONGEVITY_DIR / "recoveries-3.jsonl"
LONGEVITY_HOST_LOAD_THREE = LONGEVITY_DIR / "host_load-3.log"
LONGEVITY_VERIFY_FULL_THREE = LONGEVITY_DIR / "verify-full-3.txt"
LONGEVITY_RSS_SLOPES_THREE = LONGEVITY_DIR / "rss_slopes-3.json"
LONGEVITY_THROUGHPUT_THREE = LONGEVITY_DIR / "throughput-3.json"
LONGEVITY_READER_OP_ERROR_THREE = LONGEVITY_DIR / "reader_op_error-3.json"
LONGEVITY_READER_ONSET_ROWS_THREE = LONGEVITY_DIR / "reader_onset_rows-3.json"
LONGEVITY_WRITER_ERRORS_BY_CLASS_THREE = LONGEVITY_DIR / "writer_error_counts_by_class-3.json"
LONGEVITY_MANIFEST_THREE_SHA256 = "e094fd5acbcb95e523f3f00fa544e634a531d3b3c991525422a6546981c2251a"
LONGEVITY_COMPACTIONS_THREE_SHA256 = "9c66241d34b45c9b550b931fe78471e6789113338e163c3f4a7ba293e89a0bdf"
LONGEVITY_RECOVERIES_THREE_SHA256 = "36032c9ad858150e0d914d98e6399b62124492238b8dd013a9c15c8f78b9329b"
LONGEVITY_HOST_LOAD_THREE_SHA256 = "93836026f0c88eb8733b746a1114d716ad463eb2b9e81898291d6be93419e02d"
LONGEVITY_VERIFY_FULL_THREE_SHA256 = "6643bbda8fd0508c53715346b0a00796a733b8aa55326fe3275fa40f79e9c89b"

# Lane W2ab -- P-SOAK4, the 24h fourth soak (commit b6cdde0, the D-088
# reader-error fix) -- see benchmarks/longevity-v1/README.md's "Soak 4
# (24 h, D-088-fixed engine)" section. Every path below is whole-file
# sha256-checked against that section's own "Files added here" table,
# same discipline as soak3's five-file table above. rss_slopes-4.json,
# throughput-4.json, reader_error_counts_by_class-4.json,
# reader_reopen_on_enoent-4.json, writer_error_counts_by_class-4.json and
# compaction_stall-4.json have no xzgpu twin to hash against (derived
# locally from metrics.jsonl/longevity_ledger.jsonl/rss_composition-4.log,
# which stay on xzgpu per the same PI ruling as every earlier soak) --
# README.md says so explicitly, so this lane reads them without a sha256
# gate, same as soak3's reader_op_error-3.json convention.
LONGEVITY_MANIFEST_FOUR = LONGEVITY_DIR / "longevity-synth-1m-native-3.json"
LONGEVITY_COMPACTIONS_FOUR = LONGEVITY_DIR / "compactions-4.jsonl"
LONGEVITY_RECOVERIES_FOUR = LONGEVITY_DIR / "recoveries-4.jsonl"
LONGEVITY_HOST_LOAD_FOUR = LONGEVITY_DIR / "host_load-4.log"
LONGEVITY_VERIFY_FULL_FOUR = LONGEVITY_DIR / "verify-full-4.txt"
LONGEVITY_RSS_COMPOSITION_FOUR = LONGEVITY_DIR / "rss_composition-4.log"
LONGEVITY_RSS_SLOPES_FOUR = LONGEVITY_DIR / "rss_slopes-4.json"
LONGEVITY_THROUGHPUT_FOUR = LONGEVITY_DIR / "throughput-4.json"
LONGEVITY_READER_ERRORS_BY_CLASS_FOUR = LONGEVITY_DIR / "reader_error_counts_by_class-4.json"
LONGEVITY_READER_REOPEN_ON_ENOENT_FOUR = LONGEVITY_DIR / "reader_reopen_on_enoent-4.json"
LONGEVITY_WRITER_ERRORS_BY_CLASS_FOUR = LONGEVITY_DIR / "writer_error_counts_by_class-4.json"
LONGEVITY_COMPACTION_STALL_FOUR = LONGEVITY_DIR / "compaction_stall-4.json"
LONGEVITY_MANIFEST_FOUR_SHA256 = "fcb55a4f3126002c5d474593730bca1652e1880dfc6537aa65ae8b49d0f4de49"
LONGEVITY_COMPACTIONS_FOUR_SHA256 = "aedcd152ac470b188e74eb9a54f56750577ceeaa8e8bf64584547d403e790783"
LONGEVITY_RECOVERIES_FOUR_SHA256 = "44460cb97274195606acbb7d166d3121deb6da816defc08f9023a87f9ae5ce45"
LONGEVITY_HOST_LOAD_FOUR_SHA256 = "aa6b9ac89d978c6641766cf44f8be3eea015d0cd7ca6aa0e548492fd349eda56"
LONGEVITY_VERIFY_FULL_FOUR_SHA256 = "118f4fd726ffee67396c63f698eed879346dbfee942152442305ae55a7dc6742"
LONGEVITY_RSS_COMPOSITION_FOUR_SHA256 = "06a0972eee02a0f35b129e7d9e9715493ce3f5c4538c72b5b79b2ca09bdc9006"

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

# Lane W2z: eval.tex:120's "the store rebuild costs 12.8% more, in band" --
# P-SF1's format-3 baseline store build (ldbc-sf1-campaign-fmt3-2026-09,
# treatment a6b3e94) vs. P-SF1b's corrected characterization-interactive
# rerun's own store build (ldbc-sf1-campaign-fmt3-interactive-2026-09,
# treatment 54dcab0 -- same store-build flags, a fresh build because the
# original P-SF1 store build predates the --csv bind fix). Both wall times
# are prose-only in these READMEs (no JSON sidecar carries the store-build
# wall clock), so this lane reads them the same way the module's other
# README-prose macros do: whole-file sha256-checked below, then the exact
# number pulled by a regex anchored on its own surrounding sentence.
LDBC_FMT3_README = ROOT / "benchmarks" / "results-v1" / "ldbc-sf1-campaign-fmt3-2026-09.README.md"
LDBC_FMT3_INTERACTIVE_README = (
    ROOT / "benchmarks" / "results-v1" / "ldbc-sf1-campaign-fmt3-interactive-2026-09.README.md")
LDBC_FMT3_README_SHA256 = "2868631a2d6e4dd1be6816e2138f0b18218a8f26e27af90457284abb0a695c7b"
LDBC_FMT3_INTERACTIVE_README_SHA256 = (
    "fd75776917dc49865e78998cc6e51c828a76394d9bd2bc653b96418be6fc8930")


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


def close_rel(got: float, want: float, rel_tol: float, what: str):
    """Relative-tolerance sibling of ``close()`` -- for quantities (like a
    millisecond-scale ``ttf_p50_ms``) whose absolute magnitude varies by
    orders of magnitude across cells, where a fixed absolute tolerance
    would be meaninglessly loose on a small cell and meaninglessly tight
    on a large one. Added for Lane W2ae's sum-mode reconstruction, whose
    task brief asks for a reproduction check "to within 0.5%"."""
    denom = abs(want) if want else 1.0
    require(abs(got - want) <= rel_tol * denom,
            f"{what}: derived {got!r} not within {rel_tol:.3%} of {want!r}")
    return got


def _e2e_percentile(values: list[float], p: float) -> float:
    """Reimplements ``tgms/eval/storm.py``'s own ``_percentile`` (nearest-
    rank, ``k = round(p * (len-1))``) -- restated here, never imported
    (this generator never imports ``tgms/**``, per its own house rule
    above). Every ``ttf_p50_ms``/``ttf_p95_ms`` this generator cross-
    checks anywhere was produced by that exact function; critically,
    ``statistics.median`` disagrees with it on an even-sized batch list
    (every storm-v2 cell here has exactly 20 batches) -- ``median``
    averages the two middle values, nearest-rank does not -- which
    silently fails a same-value reproduction check at the ~1% level, not
    byte-for-byte as it should. Used throughout Lane W2ae's sum-mode
    reconstruction (``compute_c7_storm_v2_sum_mode`` and friends below)."""
    if not values:
        raise ValueError("_e2e_percentile: empty values")
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[k]


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


def _read_control_batches() -> dict[str, list[dict]]:
    """Lane W2ae: load the external-v1 same-host control's committed raw
    per-batch rows (``tgms-control-2026-10-05-batches.jsonl``, record
    addendum 2026-10-08 -- see that directory's README "Per-batch rows"
    section), sha256-gated against ``EXTERNAL_V1_SHA256SUMS`` the same
    way every other file under ``benchmarks/external-v1`` is, including
    its own ``.SOURCES.txt`` sidecar (the per-cell xzgpu provenance this
    addendum's batches were pulled from). Returns ``{cell_id: [batch,
    ...]}``, each cell's batches sorted by ``batch_index``. Called
    independently by both ``compute_c7_storm_v2_sum_mode`` (the pooled
    end-to-end-over-check instrument-error macro) and
    ``compute_ext_sum_mode`` (the sum-mode control macros) -- each
    re-runs this gate itself rather than trusting the other already
    ran, same discipline as every other sha256 gate in this file."""
    sums = _sha256sums_by_name(EXTERNAL_V1_SHA256SUMS.read_text(encoding="utf-8"))
    for path in (EXTERNAL_TGMS_CONTROL_BATCHES, EXTERNAL_TGMS_CONTROL_BATCHES_SOURCES):
        eq(sha256_file(path), sums.get(path.name),
           f"control batches: {relpath(path)} sha256 matches "
           f"{relpath(EXTERNAL_V1_SHA256SUMS)}'s entry for {path.name}")

    rows = load_jsonl(EXTERNAL_TGMS_CONTROL_BATCHES)
    eq(len(rows), 365, f"{relpath(EXTERNAL_TGMS_CONTROL_BATCHES)}: line count (18 "
       "end-to-end cells x 20 batches + the 1 sum-mode probe cell x 5 batches)")

    by_cell: dict[str, list[dict]] = {}
    for r in rows:
        by_cell.setdefault(r["cell_id"], []).append(r)
    eq(len(by_cell), 19, f"{relpath(EXTERNAL_TGMS_CONTROL_BATCHES)}: distinct cell_ids "
       "(all 19 external-v1 tgms-control cells)")
    for cell, batches in by_cell.items():
        batches.sort(key=lambda b: b["batch_index"])
        eq([b["batch_index"] for b in batches], list(range(len(batches))),
           f"control batches cell {cell}: batch_index is a dense 0..N-1 range")
    return by_cell


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


def sci_3sf(x: float) -> str:
    """Render a positive dimensionless ratio in 3-significant-figure
    scientific notation, as a LaTeX ``mantissa\\times 10^{exp}`` expression
    -- for a ratio small enough (< 0.01) that a plain decimal would bury
    its leading digits under zeros. No such formatter existed in this
    module before (checked: no sigfig/scientific-notation helper anywhere
    above); added here for compute_external_baselines's recent/hours/days/
    deep Ext2 ratio macros. Uses Decimal rounding (ROUND_HALF_UP) on the
    mantissa for the same reason us_to_ms_str above does: a binary float
    can land a hair below an exact rounding boundary."""
    assert x > 0, f"sci_3sf: expected a positive ratio, got {x!r}"
    exp = math.floor(math.log10(x))
    mantissa = Decimal(x) / (Decimal(10) ** exp)
    mantissa_q = mantissa.quantize(Decimal("1.00"), rounding=ROUND_HALF_UP)
    if mantissa_q >= 10:
        mantissa_q = (mantissa_q / 10).quantize(Decimal("1.00"), rounding=ROUND_HALF_UP)
        exp += 1
    return f"{mantissa_q}\\times 10^{{{exp}}}"


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
        msg = f"sys_paper_macros: {name} is PENDING ({lane}): {reason}"
        value = r"\errmessage{" + msg.replace("{", "(").replace("}", ")") + "}"
        self.items.append((name, value, f"PENDING -- {lane}: {reason}"))

    def render(self) -> str:
        lines = [
            "% sys-paper-macros.tex --- GENERATED by scripts/sys_paper_macros.py.",
            "% Do not hand-edit; re-run the generator.",
            "%",
            "% Every landed-record number in the manuscript resolves through one",
            "% of these; each was recomputed from row-level data and checked",
            "% against a frozen expectation in tests/test_sys_paper_macros.py.",
            f"% This run performed {CHECKS} such assertions and refused to write",
            "% on any failure. A macro whose provenance begins 'PENDING' raises",
            "% a LaTeX error if the manuscript expands it -- its record has not",
            "% landed and no placeholder number is emitted for it.",
            "%",
            "% Every name below is defined via \\csname...\\endcsname, not a bare",
            "% \\newcommand{\\name}: most names carry digits (e.g.",
            "% recB7BuildWall30M) and a LaTeX control-sequence name may not,",
            "% outside \\csname. \\csname recFoo\\endcsname is the same control",
            "% sequence as \\recFoo, so digit-free names still work under their",
            "% bare \\recFoo spelling. The \\rec{Name} accessor just below is",
            "% \\csname rec<Name>\\endcsname for prose that prefers it.",
            "",
            r"\providecommand{\rec}[1]{\csname rec#1\endcsname}",
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

    m.add("recCrashTrials", tex_num(d["total_trials"]),
          f"{relpath(CRASH_V1)}: len(results), == total_trials")
    m.add("recCrashProblems", recomputed_problems,
          f"{relpath(CRASH_V1)}: trials with a non-empty problems list")
    m.add("recCrashBoundaries", len(boundaries),
          f"{relpath(CRASH_V1)}: distinct per_boundary keys")
    m.add("recCrashWall", f"{recomputed_wall:,.2f}".replace(",", "{,}"),
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

    m.add("recManifestBytesCtl", f"{bytes_ctl / 1e9:.2f}",
          f"{relpath(B1_RAW)}: b1a.control.manifest_bytes_at_stop, decimal GB")
    m.add("recManifestBytesTrt", f"{bytes_trt / 1e6:.1f}",
          f"{relpath(B1_RAW)}: b1a.treatment.manifest_bytes_at_stop, decimal MB")
    m.add("recManifestCommitRatio", f"{ratio_trt:.3f}",
          f"{relpath(B1_RAW)}: b1b.summary.treatment.total_us last/first decile "
          "(the F2 falsifier: refuted, bar <=1.2x)")
    m.add("recManifestColdOpen", f"{cold_open_ratio:.2f}",
          f"{relpath(B1_RAW)}: b1c.authoritative median(treatment_open_ms) / "
          "median(control_open_ms) at G~10k (the F3 falsifier: refuted, bar <=100ms)")


# --------------------------------------------------------------------------
# B1-v2 --- manifest format 3 (reader torn-tail fix) A/B, Lane B / W2f.
#
# Same Addendum-3 (manifest forecast) protocol as v1's B1 A/B above (C3):
# SF1 manifest bytes at matched 2.5M ops, SS20 batch=1 commitcost phase
# deciles/p50 (300 commits, 100k-row seed store), chain-open at G~10k
# K=512, plus the off-default K=128/K=1024 sweep (1 rep each). These
# recB1v2* macros stand beside the recManifest* ones above as v2-era
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
    m.add("recB1v2ControlCommit", ctl_commit[:7],
          f"{relpath(B1_V2_MANIFEST)}: config.control_commit, short sha")
    m.add("recB1v2TreatmentCommit", trt_commit[:7],
          f"{relpath(B1_V2_MANIFEST)}: config.treatment_commit, short sha")

    m.add("recB1v2BytesControlMB", f"{bytes_ctl / 1e6:.1f}",
          f"{relpath(B1_V2_RAW)}: b1a.control.manifest_bytes_at_stop, decimal MB")
    m.add("recB1v2BytesTreatmentMB", f"{bytes_trt / 1e6:.1f}",
          f"{relpath(B1_V2_RAW)}: b1a.treatment.manifest_bytes_at_stop, decimal MB")
    m.add("recB1v2BytesPaired", f"{bytes_paired:.3f}",
          f"{relpath(B1_V2_RAW)}: b1a.treatment.manifest_bytes_at_stop / "
          "b1a.control.manifest_bytes_at_stop")
    m.add("recB1v2SegmentBytesControlMB", f"{seg_ctl / 1e6:.1f}",
          f"{relpath(B1_V2_RAW)}: b1a.control.segment_bytes_at_stop, decimal MB")
    m.add("recB1v2SegmentBytesTreatmentMB", f"{seg_trt / 1e6:.1f}",
          f"{relpath(B1_V2_RAW)}: b1a.treatment.segment_bytes_at_stop, decimal MB")

    m.add("recB1v2ManifestDecileControl", f"{manifest_decile_ctl:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.control[*].last_decile_us.manifest_us / "
          "[*].first_decile_us.manifest_us, median over 3 reps")
    m.add("recB1v2ManifestDecileTreatment", f"{manifest_decile_trt:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.treatment[*].last_decile_us.manifest_us / "
          "[*].first_decile_us.manifest_us, median over 3 reps")
    m.add("recB1v2ManifestDecileK128", f"{manifest_decile_k128:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.treatment_k128[0].last_decile_us.manifest_us / "
          "first_decile_us.manifest_us (1 rep)")
    m.add("recB1v2ManifestDecileK1024", f"{manifest_decile_k1024:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.treatment_k1024[0].last_decile_us.manifest_us / "
          "first_decile_us.manifest_us (1 rep)")

    m.add("recB1v2TotalDecileControl", f"{total_decile_ctl:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.control[*].last_decile_us.total_us / "
          "[*].first_decile_us.total_us, median over 3 reps")
    m.add("recB1v2TotalDecileTreatment", f"{total_decile_trt:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.treatment[*].last_decile_us.total_us / "
          "[*].first_decile_us.total_us, median over 3 reps")

    m.add("recB1v2P50ControlMs", f"{p50_ctl / 1000:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.control[*].phase_p50_us.total_us, "
          "median over 3 reps, /1000, ms")
    m.add("recB1v2P50TreatmentMs", f"{p50_trt / 1000:.3f}",
          f"{relpath(B1_V2_RAW)}: b1b.raw.treatment[*].phase_p50_us.total_us, "
          "median over 3 reps, /1000, ms")
    m.add("recB1v2P50Paired", f"{p50_paired:.3f}",
          f"{relpath(B1_V2_RAW)}: median(treatment[*].phase_p50_us.total_us) / "
          "median(control[*].phase_p50_us.total_us)")

    m.add("recB1v2OpenControlMs", f"{ctl_open_med:.1f}",
          f"{relpath(B1_V2_RAW)}: median(b1c.authoritative.control_open_ms), 3 reps, ms")
    m.add("recB1v2OpenTreatmentMs", f"{trt_open_med:.1f}",
          f"{relpath(B1_V2_RAW)}: median(b1c.authoritative.treatment_open_ms), 3 reps, ms")
    m.add("recB1v2OpenPaired", f"{open_paired:.2f}",
          f"{relpath(B1_V2_RAW)}: median(b1c.authoritative.treatment_open_ms) / "
          "median(b1c.authoritative.control_open_ms)")
    m.add("recB1v2OpenControlGeneration", tex_num(ctl_generation),
          f"{relpath(B1_V2_RAW)}: b1c.authoritative.control_generation")
    m.add("recB1v2OpenTreatmentGeneration", tex_num(trt_generation),
          f"{relpath(B1_V2_RAW)}: b1c.authoritative.treatment_generation")

    m.add("recB1v2BuildOpsPerSecRatioAt2p5M", f"{ops_ratio:.2f}",
          f"{relpath(B1_V2_RAW)}: b1a.treatment.ops_series[-1][1] (5335) / "
          "b1a.control.ops_series[-1][1] (2543), build ops/s at the 2.5M-op "
          "checkpoint -- observation only, no claim attached")

    m.add("recB1v2OpenComponentStatus", "not computed",
          f"{relpath(B1_V2_RAW)}: b1c.authoritative.component_breakdown is null -- "
          "NativeAdapter() exposes no internal phase timer to split checkpoint-load "
          "from delta-replay (text macro, not a number -- see "
          "b1c.authoritative.component_breakdown_note)")


# --------------------------------------------------------------------------
# B1-v2e --- remeasure of the two B1-v2 cells that A/B could not score,
# under the fully-timed B1-v2d harness (`open_phase_us`, `build_info`,
# per-commit phase decile/residual fields).
#
# `recB1v2*ManifestDecile*`/`recB1v2TotalDecile*`/`recB1v2P50*` (the v2
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
# The `recB1v2e*` macros below are new: recomputed from
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
    "recB1v2e* below; values here are unchanged (frozen as measured)."
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
        "recB1v2TotalDecileTreatment", "recB1v2ManifestDecileTreatment",
        "recB1v2ManifestDecileK128", "recB1v2ManifestDecileK1024",
        "recB1v2P50TreatmentMs", "recB1v2P50Paired",
    }
    found = set()
    for i, (name, value, provenance) in enumerate(m.items):
        if name in voided:
            m.items[i] = (name, value, provenance + _VOID_AS_FORMAT3)
            found.add(name)
    eq(found, voided, "sys_paper_macros: every v2 commit-cost treatment "
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
    # and recB1v2P50* (5.478/5.430) already tracks it -- recB1v2eP50*
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
    # recB1v2eWallP50* names, so a future reader of this file never
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
    m.add("recB1v2eTreatmentCommit", trt_commit[:7],
          f"{relpath(B1_V2E_MANIFEST)}: git_commit, short sha")

    m.add("recB1v2eTotalDecileTreatment", f"{total_decile_trt:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.treatment_reps_full[*]."
          "last_decile_us.total_us / [*].first_decile_us.total_us, median over 3 reps "
          "-- supersedes recB1v2TotalDecileTreatment, which measured a format-2 chain")
    m.add("recB1v2eTotalDecileControl", f"{total_decile_ctl:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.control_reps_full[*]."
          "last_decile_us.total_us / [*].first_decile_us.total_us, median over 3 reps")

    m.add("recB1v2eResidualFirstUs", f"{residual_first:.2f}",
          f"{relpath(B1_V2E_RAW)}: mean(cell_b_commitcost.treatment_reps_full[*]."
          "residual_first_us) over 3 reps -- the B1V2_AB_DIAGNOSIS memo's Q1 metric, "
          "now ~30us against a ~3,300-3,400us total_us (closed, not hidden)")
    m.add("recB1v2eResidualLastUs", f"{residual_last:.2f}",
          f"{relpath(B1_V2E_RAW)}: mean(cell_b_commitcost.treatment_reps_full[*]."
          "residual_last_us) over 3 reps")

    m.add("recB1v2eP50TreatmentMs", f"{engine_p50_trt / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.treatment_reps_full[*]."
          "phase_p50_us.total_us, median over 3 reps, /1000, ms -- engine-commit p50 "
          "(total_us), the Addendum 3 quantity, same as recB1v2P50*")
    m.add("recB1v2eP50ControlMs", f"{engine_p50_ctl / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.control_reps_full[*]."
          "phase_p50_us.total_us, median over 3 reps, /1000, ms -- engine-commit p50 "
          "(total_us), the Addendum 3 quantity, same as recB1v2P50*")
    m.add("recB1v2eP50Paired", f"{engine_p50_paired:.3f}",
          f"{relpath(B1_V2E_RAW)}: median(treatment[*].phase_p50_us.total_us) / "
          "median(control[*].phase_p50_us.total_us) -- engine-commit p50 (total_us), "
          "the Addendum 3 quantity, same as recB1v2P50*")

    m.add("recB1v2eWallP50TreatmentMs", f"{wall_p50_trt:.3f}",
          f"{relpath(B1_V2E_RAW)}: median(cell_b_commitcost.treatment_reps_full[*]."
          "commit_ms.p50) over 3 reps, ms -- wall-clock commit_ms.p50 incl. "
          "Python-side eventlog append -- not the frozen quantity")
    m.add("recB1v2eWallP50ControlMs", f"{wall_p50_ctl:.3f}",
          f"{relpath(B1_V2E_RAW)}: median(cell_b_commitcost.control_reps_full[*]."
          "commit_ms.p50) over 3 reps, ms -- wall-clock commit_ms.p50 incl. "
          "Python-side eventlog append -- not the frozen quantity")
    m.add("recB1v2eWallPaired", f"{wall_p50_paired:.3f}",
          f"{relpath(B1_V2E_RAW)}: median(treatment[*].commit_ms.p50) / "
          "median(control[*].commit_ms.p50) -- wall-clock commit_ms.p50 incl. "
          "Python-side eventlog append -- not the frozen quantity")

    m.add("recB1v2eOpenComponentMs", f"{component_us / 1000:.2f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us -- "
          "checkpoint_read_parse_us + merkle_verify_us + state_build_us + "
          "delta_replay_us, /1000, ms")
    m.add("recB1v2eOpenCheckpointMs", f"{checkpoint_us / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "checkpoint_read_parse_us, /1000, ms")
    m.add("recB1v2eOpenMerkleVerifyMs", f"{merkle_us / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "merkle_verify_us, /1000, ms")
    m.add("recB1v2eOpenStateBuildMs", f"{state_build_us / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "state_build_us, /1000, ms")
    m.add("recB1v2eOpenDeltaReplayMs", f"{delta_replay_us / 1000:.3f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "delta_replay_us, /1000, ms")
    m.add("recB1v2eOpenDictionaryMs", f"{dictionary_us / 1000:.1f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "dictionary_open_us, /1000, ms -- dominates the open (89.5% of total_us)")
    m.add("recB1v2eOpenTotalMs", f"{total_us / 1000:.1f}",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.open_phase_p50_us."
          "total_us, /1000, ms")
    m.add("recB1v2eOpenGeneration", tex_num(trt_generation),
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.treatment.generation")

    m.add("recB1v2eControlOpenStatus", "confounded (concurrent backup transfer)",
          f"{relpath(B1_V2E_RAW)}: cell_a_chain_open.control's open time (9.6-10.4s) "
          "was measured while a phase-2 tar backup ran concurrently on xzgpu (text "
          "macro, not a number -- see cell_a_chain_open.control and the manifest's "
          "own measurements.cell_a_chain_open.caveat)")

    m.add("recB1v2eManifestBytesTreatment", tex_num(manifest_bytes_trt),
          f"{relpath(B1_V2E_RAW)}: cell_b_commitcost.treatment_reps_full[*]."
          "first_decile_us.manifest_bytes, constant across 3 reps -- the format-3 "
          "evidence (versus the v2 A/B's mislabeled treatment, which matched "
          "recB1v2eManifestBytesControl instead)")
    m.add("recB1v2eManifestBytesControl", tex_num(manifest_bytes_ctl),
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
    m.add("recB1co7TreatmentCommit", trt_commit,
          f"{relpath(B1_CO7_MANIFEST)}: git_commit")

    m.add("recB1co7CheckpointReadParseMs", us_to_ms_str(checkpoint_us, 2),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "checkpoint_read_parse_us) over 3 reps, /1000, ms")
    m.add("recB1co7MerkleVerifyMs", us_to_ms_str(merkle_us, 2),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "merkle_verify_us) over 3 reps, /1000, ms")
    m.add("recB1co7StateBuildMs", us_to_ms_str(state_build_us, 2),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "state_build_us) over 3 reps, /1000, ms")
    m.add("recB1co7DeltaReplayMs", us_to_ms_str(delta_replay_us, 2),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "delta_replay_us) over 3 reps, /1000, ms")
    m.add("recB1co7ComponentMs", us_to_ms_str(component_us, 2),
          f"{relpath(B1_CO7_RAW)}: checkpoint_read_parse_us + merkle_verify_us + "
          "state_build_us + delta_replay_us (each median of 3 reps), /1000, ms -- "
          "asserted equal to the raw record's own manifest_chain_component_us_treatment_"
          "median.sum_us")
    m.add("recB1co7DictionaryOpenMs", us_to_ms_str(dictionary_us, 1),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "dictionary_open_us) over 3 reps, /1000, ms -- dominates the open")
    m.add("recB1co7TotalMs", us_to_ms_str(total_us, 1),
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.treatment.open_phase_us[*]."
          "total_us) over 3 reps, /1000, ms")
    m.add("recB1co7Generation", tex_num(trt_generation),
          f"{relpath(B1_CO7_RAW)}: cell_a_chain_open.treatment.generation")
    m.add("recB1co7DeltaCount", tex_num(delta_count),
          f"{relpath(B1_CO7_RAW)}: cell_a_chain_open.treatment.open_phase_us[*]."
          "delta_count, constant across 3 reps")
    m.add("recB1co7CheckpointGeneration", tex_num(checkpoint_generation),
          f"{relpath(B1_CO7_RAW)}: cell_a_chain_open.treatment.generation - delta_count "
          "-- asserted a multiple of the K=512 checkpoint cadence")
    m.add("recB1co7StateBuildPerDeltaUs", f"{state_build_per_delta_us:.1f}",
          f"{relpath(B1_CO7_RAW)}: treatment state_build_us (median) / delta_count, "
          "us/delta")
    m.add("recB1co7DeltaReplayPerDeltaUs", f"{delta_replay_per_delta_us:.1f}",
          f"{relpath(B1_CO7_RAW)}: treatment delta_replay_us (median) / delta_count, "
          "us/delta")

    m.add("recB1co7ControlOpenMs", f"{ctl_open_ms:.1f}",
          f"{relpath(B1_CO7_RAW)}: median(cell_a_chain_open.control.open_ms) over 3 reps, ms")
    m.add("recB1co7ControlGeneration", tex_num(ctl_generation),
          f"{relpath(B1_CO7_RAW)}: cell_a_chain_open.control.generation")
    m.add("recB1co7ControlDeltaCount", tex_num(ctl_delta_count),
          f"{relpath(B1_CO7_RAW)}: cell_a_chain_open.control.generation mod K=512 "
          "-- the control engine predates open_phase_us() and reports no delta_count "
          "of its own")
    m.add("recB1co7ControlPerDeltaMs", f"{ctl_per_delta_ms:.1f}",
          f"{relpath(B1_CO7_RAW)}: (control open_ms (median) - treatment's measured "
          "dictionary_open_us/1000, used as an estimate of the control's own "
          "dictionary-open cost) / control delta_count -- an estimate, not a second "
          "independent measurement")
    m.add("recB1co7OpenRatio", f"{open_ratio:.3f}",
          f"{relpath(B1_CO7_RAW)}: (treatment total_us (median) / 1000) / control "
          "open_ms (median)")

    m.add("recB1WorstPhaseOpenTreatmentMs", f"{worst_phase_open_trt_ms:.1f}",
          "derived, not measured: recB1co7CheckpointReadParseMs + recB1co7MerkleVerifyMs "
          "+ (recB1co7StateBuildPerDeltaUs + recB1co7DeltaReplayPerDeltaUs) * 511 / 1000 "
          "-- projected treatment manifest-chain open cost at K-1=511 deltas")
    m.add("recB1WorstPhaseOpenControlS", f"{worst_phase_open_ctl_s:.1f}",
          "derived, not measured: recB1co7ControlPerDeltaMs * 511 / 1000 -- projected "
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

    m.add("recVhRss", f"{rss_gb:.3f}",
          f"{relpath(B2_RAW)}: median(treatment_10m[*].vmhwm_kb), decimal GB")
    m.add("recVhWall", f"{trt_10m_wall / 1000:.2f}",
          f"{relpath(B2_RAW)}: median(treatment_10m[*].wall_ms) / 1000, s")
    m.add("recVhRatio", f"{flatness_ratio:.2f}",
          f"{relpath(B2_RAW)}: treatment 10M/1M VmHWM median ratio "
          "(the near-flatness falsifier: refuted, bar <=2.5x)")
    m.add("recVhProjHundredM", proj_gb,
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

    m.add("recReadersMax", readers_max,
          f"{relpath(READERS_10M)}: max(quiescent[*].readers)")
    m.add("recReaderVmhwm", f"{vmhwm_lo:.2f}--{vmhwm_hi:.2f}",
          f"{relpath(READERS_10M)}: min/max over readers of quiescent[*].vmhwm_kb.median, GiB")
    m.add("recAggQpsOne", f"{agg_one:.2f}",
          f"{relpath(READERS_10M)}: quiescent[readers=1].aggregate_qps")
    m.add("recAggQpsThirtyTwo", f"{agg_max:.2f}",
          f"{relpath(READERS_10M)}: quiescent[readers=32].aggregate_qps")
    m.add("recWriterTailCost",
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

    m.add("recFalseFreshCarveTwo", carve2_false_fresh,
          f"{relpath(M5_CARVE_TWO)}: rows with changed and verdict=='fresh', of 28,044")
    m.add("recRowTouchRate", f"{rt_rate:.1f}",
          f"{relpath(FRESH_FULL)}+{FRESH_FIXTURE.name}: naive row-touch false-fresh "
          "rate over M4's changed column, 212/447")
    m.add("recNewIdentityFF", m4_newid_missed,
          f"{relpath(FRESH_FULL)}+{FRESH_FIXTURE.name}: new-identity changed trials "
          "the row-touch rule calls fresh, of 89")
    m.add("recAvoidedPct", f"{avoided_pct:.1f}",
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

    m.add("recFaultTrials", tex_num(post["total_trials"]),
          f"{relpath(FAULTS_POST)}: total_trials, the deployed (post-D-160) gate")
    m.add("recSilentPre", silent_pre,
          f"{relpath(FAULTS_PRE)}: primary-gate silent-violation total "
          "(F1-9 271 + F2-3 32), before D-160")
    m.add("recSilentPost", post_headline,
          f"{relpath(FAULTS_POST)}: headline_silent_violation_count (F2-3's 32 is a "
          "declared non-headline blind spot, see recFTwoThree)")
    m.add("recFOneNineBefore", f19_pre_count,
          f"{relpath(FAULTS_PRE)}: F1-9 (wrong_step_citation) silent-violation count, "
          "primary gate, before D-160 -- 0 after")
    m.add("recFTwoThree", f23_post_count,
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

    m.add("recDagCells", dag_cells,
          f"{relpath(DAG_V1)}: total_tasks, == v2/v3's own total_tasks (40-cell grid, all three)")
    m.add("recDagV1FalseSafeCells", len(v1_fs_cells),
          f"{relpath(DAG_V1)}: per_cell entries with false_safe_count>0, of 40")
    m.add("recDagV2FalseSafeCells", v2_fs_cells,
          f"{relpath(DAG_V2)}: per_cell entries with false_safe_count>0, of 40")
    m.add("recDagV3FalseSafeCells", v3_fs_cells,
          f"{relpath(DAG_V3)}: per_cell entries with false_safe_count>0, of 40")
    m.add("recDagV1FalseSafePerCell", v1_false_safe_per_cell,
          f"{relpath(DAG_V1)}: false_safe_count in each of the 20 affected cells (uniform)")
    m.add("recDagV2ExtraVisitsSeedZero", v2_extra_s0,
          f"{relpath(DAG_V2)} vs {DAG_V1.name}: nodes_visited delta, seed-0 cells "
          "(constant across all 20, asserted)")
    m.add("recDagV2ExtraVisitsSeedOne", v2_extra_s1,
          f"{relpath(DAG_V2)} vs {DAG_V1.name}: nodes_visited delta, seed-1 cells "
          "(constant across all 20, asserted)")
    m.add("recDagV3ExtraVisitsSeedZero", v3_extra_s0,
          f"{relpath(DAG_V3)} vs {DAG_V1.name}: nodes_visited delta, seed-0 cells "
          "(constant across all 20, asserted)")
    m.add("recDagV3ExtraVisitsSeedOne", v3_extra_s1,
          f"{relpath(DAG_V3)} vs {DAG_V1.name}: nodes_visited delta, seed-1 cells "
          "(constant across all 20, asserted)")
    m.add("recDagV3AllTopTerm", all_top_term_total,
          f"{relpath(DAG_V3_ROWS)}: sum of summary.narrowing_coverage.n_all_top_term over "
          "all 40 cells")
    m.add("recDagFalseFreshTotal", ff_total,
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

    m.add("recR18Artifacts", tex_num(d["config"]["n_artifacts"]),
          f"{relpath(R18_PROBE)}: config.n_artifacts")
    m.add("recR18IntersectsCallsMedian", tex_num(int(intersects_med)),
          f"{relpath(R18_PROBE_ROWS)}: median(intersects_calls) over the 5 batches")
    m.add("recR18LookupMsMedian", f"{lookup_med:.2f}",
          f"{relpath(R18_PROBE_ROWS)}: median(lookup_wall_ms) over the 5 batches, ms")
    m.add("recR18SurvivorFraction", f"{survivor_fraction * 100:.1f}",
          f"{relpath(R18_PROBE_ROWS)}: median(candidate_survivors) / n_registered, percent")
    m.add("recR18CheckSecondsMedian", f"{check_seconds_med:.1f}",
          f"{relpath(R18_PROBE_ROWS)}: median(arms.tgms-L1.check_wall_ms) over the 5 "
          "batches, /1000, s")
    m.add("recR18TtfL1Seconds", f"{ttf_l1_s:.1f}",
          f"{relpath(R18_PROBE_ROWS)}: median(arms.tgms-L1.ttf_ms) over the 5 batches, /1000, s "
          "(== record's own summary.arms.tgms-L1.ttf_p50_ms)")
    m.add("recR18TtfGlobalSeconds", f"{ttf_global_s:.1f}",
          f"{relpath(R18_PROBE_ROWS)}: median(arms.global-recompute.ttf_ms) over the 5 "
          "batches, /1000, s (== record's own summary.arms.global-recompute.ttf_p50_ms)")
    m.add("recR18Speedup", f"{speedup:.3f}",
          f"{relpath(R18_PROBE_ROWS)}: median(global-recompute ttf_ms) / median(tgms-L1 "
          "ttf_ms) -- P5/P6's speedup convention, <1.0 means L1 is slower here")
    m.add("recR18Precision", f"{precision_med * 100:.2f}",
          f"{relpath(R18_PROBE_ROWS)}: median(changed_count / candidate_survivors) over the "
          "5 batches, percent")
    m.add("recR18AvoidedRecompute", f"{avoided_decision * 100:.1f}",
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
# which cell. `recStormV1*` macros mirror `recStormV2*`
# (`compute_c7_storm_v2` below) exactly, on this pre-rollout grid instead
# of the post-rollout rerun, resolving the `recStormV1SpeedupN1kSeed0`
# stub that stood in add_pending_stubs below, and additionally landing the
# v1-scoring quantities the C6 pre-registration template (README.md) asked
# for -- the row-touch/entity-touch/window-overlap false-fresh rates and
# the P4 (wall-avoided < decision-avoided) check -- which only the
# per-task per-batch rows packed in storm-v1-records-36-tasks.tar.gz carry
# (the merged grid's own per_cell table has only cell summaries, same
# reason Lane W2l unpacked the v2 tarball for its own survivor-fraction/
# precision pair below). Unlike storm-v2's rows, these rows carry no
# `narrowing_coverage` block, so there is no v1 counterpart to
# `recStormV2{AllTopTerms,NonComputeArtifacts}`.
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
    # uses for its c1-only recStormV2AvoidedDecisionC1Median, generalized
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

    m.add("recStormV1Commit", commit,
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: git_commit, uniform over all 36 cells (== "
          f"{relpath(STORM_V1_MAIN_GRID)}'s own git_commit)")
    m.add("recStormV1Cells", len(rows),
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: line count (== "
          f"{relpath(STORM_V1_MAIN_GRID)}'s own total_tasks/config.n_tasks)")
    m.add("recStormV1CellsFailed", len(failing_cells),
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: cells recomputed to fail G-S1 or G-S2, of 36 "
          f"(== union of {relpath(STORM_V1_MAIN_GRID)}'s own gates.g_s{{1,2}}_failing_cells)")
    m.add("recStormV1FalseFreshTgmsCellsNonzero", ff_nonzero,
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: count of (cell, arm) pairs among tgms-L0/"
          "tgms-L1 over all 36 cells (72 total) with summary.arms[arm].false_fresh > 0")
    m.add("recStormV1SpeedupN1kSeed0", f"{n1k_speedup:.3f}",
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: summary.arms.{{global-recompute,tgms-L1}}."
          "ttf_p50_ms ratio at (store=synth-iv-60k, mix=c1, age=none, seed=0, "
          "n_artifacts=1000)")

    for key, med in per_group_median.items():
        store, mix, age = key
        name = (f"recStormV1Speedup{_STORM_V1_STORE_TOKEN[store]}{_STORM_V1_MIX_TOKEN[mix]}"
                f"{_STORM_V1_AGE_TOKEN[age]}")
        m.add(name, f"{med:.3f}",
              f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: median over seeds 0/1/2 of "
              "summary.arms.{global-recompute,tgms-L1}.ttf_p50_ms ratio at "
              f"(store={store}, mix={mix}, age={age or 'none'}, n_artifacts=1000)")

    m.add("recStormV1SpeedupGridMin", f"{grid_min:.3f}",
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: minimum per-cell "
          "summary.arms.{global-recompute,tgms-L1}.ttf_p50_ms ratio over all 36 cells")
    m.add("recStormV1SpeedupGridMax", f"{grid_max:.3f}",
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: maximum per-cell "
          "summary.arms.{global-recompute,tgms-L1}.ttf_p50_ms ratio over all 36 cells")

    m.add("recStormV1AvoidedDecisionC1Median", f"{avoided_by_mix['c1']:.3f}",
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: median(summary.arms.tgms-L1."
          "avoided_recompute_decision) over the 12 c1-mix cells")
    m.add("recStormV1AvoidedDecisionC3Median", f"{avoided_by_mix['c3']:.3f}",
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: median(summary.arms.tgms-L1."
          "avoided_recompute_decision) over the 12 c3-mix cells")
    m.add("recStormV1AvoidedDecisionC4Median", f"{avoided_by_mix['c4']:.3f}",
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: median(summary.arms.tgms-L1."
          "avoided_recompute_decision) over the 12 c4-mix cells")

    m.add("recStormV1Batches", tex_num(total_batches),
          f"{relpath(STORM_V1_RECORDS_TARBALL)}: total per-batch rows over all 36 cells "
          "(36 cells x 20 batches)")
    m.add("recStormV1RowTouchFalseFreshMedian", f"{row_touch_median:.3f}",
          f"{relpath(STORM_V1_RECORDS_TARBALL)}: median over the 36 cells of Sum(row-touch "
          "arms.false_fresh_count) / Sum(changed_count), each summed over that cell's own "
          "20 batches")
    m.add("recStormV1EntityTouchFalseFreshMedian", f"{entity_touch_median:.3f}",
          f"{relpath(STORM_V1_RECORDS_TARBALL)}: median over the 36 cells of Sum(entity-touch "
          "arms.false_fresh_count) / Sum(changed_count), each summed over that cell's own "
          "20 batches")
    m.add("recStormV1WindowOverlapFalseFreshMedian", f"{window_overlap_median:.3f}",
          f"{relpath(STORM_V1_RECORDS_TARBALL)}: median over the 36 cells of "
          "Sum(window-overlap arms.false_fresh_count) / Sum(changed_count), each summed over "
          "that cell's own 20 batches")
    m.add("recStormV1WindowOverlapNonzeroBatches", tex_num(window_overlap_nonzero_batches),
          f"{relpath(STORM_V1_RECORDS_TARBALL)}: batches (of 720) with window-overlap "
          "arms.false_fresh_count > 0")
    m.add("recStormV1NewIdentityBatches", tex_num(len(new_identity_row_touch_ratios)),
          f"{relpath(STORM_V1_RECORDS_TARBALL)}: batches (of 720) whose correction_placement "
          "is new-identity")
    m.add("recStormV1NewIdentityRowTouchMedian", f"{new_identity_row_touch_median:.3f}",
          f"{relpath(STORM_V1_RECORDS_TARBALL)}: median row-touch arms.false_fresh_count / "
          "changed_count over the 195 new-identity batches")
    m.add("recStormV1P4ViolationCells", tex_num(p4_violations),
          f"{relpath(STORM_V1_MAIN_GRID_ROWS)}: cells (of 36) where summary.arms.tgms-L1."
          "avoided_recompute_wall is not < avoided_recompute_decision (P4: check cost is "
          "O(prefix), paid regardless)")


# --------------------------------------------------------------------------
# C7 (partial) -- storm-v2 R-18 probe (Lane W2m; job 212295, addendum-3,
# post-D-161-rollout): benchmarks/storm-v1/storm-v2-r18-probe-2026-09-15.json
# + -rows.jsonl. Same cell as the v1 R-18 probe above (compute_c7_r18: store
# synth-iv-60k, mix c1, N=10,000, seed 0, 5 batches, sum TTF) -- see
# README.md's "R-18 probe v2 (addendum-3) -- run of record, v1 vs v2
# comparison" section for the full side-by-side table. `recR18*` above
# stay untouched; `recStormV2Probe*` land beside them here, and beside
# `recStormV2*` (compute_c7_storm_v2, the different 36-cell main-grid
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
    # faster) -- same convention as compute_c7_r18's recR18Speedup, but
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

    m.add("recStormV2ProbeCommit", commit,
          f"{relpath(STORM_V2_R18_PROBE)}: git_commit")
    m.add("recStormV2ProbeBatches", tex_num(d["config"]["batches"]),
          f"{relpath(STORM_V2_R18_PROBE)}: config.batches")
    m.add("recStormV2ProbeWallS", f"{wall_s:,.1f}".replace(",", "{,}"),
          f"{relpath(STORM_V2_R18_PROBE)}: config.wall_s")
    m.add("recStormV2ProbeGlobalTtfS", f"{global_ttf_med / 1000:,.1f}".replace(",", "{,}"),
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(arms.global-recompute.ttf_ms) over "
          "the 5 batches, /1000, s (== record's own summary.arms.global-recompute."
          "ttf_p50_ms)")
    m.add("recStormV2ProbeTgmsL1TtfS", f"{l1_ttf_med / 1000:.1f}",
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(arms.tgms-L1.ttf_ms) over the 5 "
          "batches, /1000, s (== record's own summary.arms.tgms-L1.ttf_p50_ms)")
    m.add("recStormV2ProbeSpeedupN10k", f"{speedup:.3f}",
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(global-recompute ttf_ms) / "
          "median(tgms-L1 ttf_ms) -- P5/P6's speedup convention, at N=10,000")
    m.add("recStormV2ProbeAvoidedDecision", f"{avoided_decision:.3f}",
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: summary.arms.tgms-L1."
          "avoided_recompute_decision (== 1 - sum(invalidated_count)/sum(n_registered) "
          "over the 5 batches)")
    m.add("recStormV2ProbeSurvivorMedian", f"{survivor_median:.3f}",
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(candidate_survivors / n_registered) "
          "over the 5 batches")
    m.add("recStormV2ProbePrecisionMedian", f"{precision_median:.3f}",
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(changed_count / candidate_survivors) "
          "over the 5 batches")
    m.add("recStormV2ProbeIntersectsMedian", tex_num(int(intersects_med)),
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(intersects_calls) over the 5 batches")
    m.add("recStormV2ProbeR18Tripped", "yes" if r18_tripped else "no",
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(intersects_calls) > 50,000 (the R-18 "
          "trip threshold; arithmetic fact only, no verdict)")
    m.add("recStormV2ProbeAllTopTerms", nc["n_all_top_term"],
          f"{relpath(STORM_V2_R18_PROBE)}: summary.narrowing_coverage.n_all_top_term")
    m.add("recStormV2ProbeNonComputeArtifacts", tex_num(non_compute_artifacts),
          f"{relpath(STORM_V2_R18_PROBE)}: summary.narrowing_coverage.n_artifacts - "
          "n_empty_scope")
    m.add("recStormV2ProbeCheckWallMedianS", f"{check_wall_median_s:.1f}",
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(arms.tgms-L1.check_wall_ms) over the "
          "5 batches, /1000, s (README.md's own quoted field)")


# --------------------------------------------------------------------------
# C7 (partial) -- storm-v2 main grid (addendum-3, post-D-161-rollout rerun
# of the same 36-cell (store x mix x age x seed) recipe addendum-1 never
# finished): benchmarks/storm-v1/storm-v2-main-grid-2026-09-15.json +
# -rows.jsonl. Each rows.jsonl line is one task's own per-task manifest
# embedded verbatim (storm_campaign_merge.py's own "why per-task manifests
# are embedded whole" module note) -- config/summary/dag, never the raw
# per-batch rows (those stay on iTiger's stage directory). `recStormV2*`
# macros stand beside `recStormV1*` (compute_c7_storm_v1 above) without
# resolving or overwriting them -- different grid, different commit, no
# v1-vs-v2 comparison macro here. (The addendum-1 dead-end's own legacy
# names -- `recStormCells` et al. -- were retired; see the module
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

    # P4 ("avoided recomputation (wall clock) < the decision count", same
    # per-cell check as compute_c7_storm_v1's p4_violations above, restated
    # here for the v2 grid): clean_cells is counted directly from each
    # cell's own summary.arms.tgms-L1 fields, never derived as
    # len(rows) - a separately-counted violation total, so a record with a
    # different violation count (or a different total cell count) cannot
    # silently leave this macro at its old value.
    p4_clean_cells = 0
    p4_violation_cells: list[int] = []
    for r in rows:
        a = r["summary"]["arms"]["tgms-L1"]
        if a["avoided_recompute_wall"] < a["avoided_recompute_decision"]:
            p4_clean_cells += 1
        else:
            p4_violation_cells.append(r["_task_id"])
    eq(p4_clean_cells + len(p4_violation_cells), len(rows),
       "storm-v2 main grid: every cell is either P4-clean or a P4 violation, no double count")
    eq(len(p4_violation_cells), 2, "storm-v2 main grid frozen: P4 violations (cells where "
       "tgms-L1's avoided_recompute_wall is not < avoided_recompute_decision), of 36")
    eq(p4_clean_cells, 34, "storm-v2 main grid frozen: P4-clean cells (wrong.tex row 12's "
       "\"34 of StormV2Cells\"), of 36")
    violation_configs = [
        (r["config"]["store"], r["config"]["mix"], r["config"]["age"])
        for r in rows if r["_task_id"] in p4_violation_cells]
    violation_configs.sort(key=lambda cfg: (cfg[0], cfg[1], cfg[2] is None, cfg[2]))
    eq(violation_configs, [("collegemsg", "c4", "deep"), ("collegemsg", "c4", None)],
       "storm-v2 main grid frozen: the two P4-violation cells are collegemsg/c4 at both ages "
       "(the fourth correction class, per wrong.tex's own prose)")

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

    m.add("recStormV2Commit", commit,
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: git_commit, uniform over all 36 cells (== "
          f"{relpath(STORM_V2_MAIN_GRID)}'s own git_commit)")
    m.add("recStormV2Cells", len(rows),
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: line count (== "
          f"{relpath(STORM_V2_MAIN_GRID)}'s own total_tasks/config.n_tasks)")
    m.add("recStormV2CellsFailed", len(failing_cells),
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: cells recomputed to fail G-S1 or G-S2, of 36 "
          f"(== union of {relpath(STORM_V2_MAIN_GRID)}'s own gates.g_s{{1,2}}_failing_cells)")
    m.add("recStormV2CleanCells", tex_num(p4_clean_cells),
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: cells (of recStormV2Cells==36) where "
          "summary.arms.tgms-L1.avoided_recompute_wall < avoided_recompute_decision (P4), "
          "counted directly from each cell's own per-cell status, not derived from "
          "recStormV2Cells minus a separately-counted violation total -- wrong.tex row 12's "
          "\"34 of StormV2Cells\"")
    m.add("recStormV2AllTopTerms", all_top_term_total,
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: sum of summary.narrowing_coverage."
          "n_all_top_term over all 36 cells")
    m.add("recStormV2NonComputeArtifacts", tex_num(non_compute_artifacts),
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: sum of (summary.narrowing_coverage.n_artifacts "
          "- n_empty_scope) over all 36 cells")
    m.add("recStormV2SpeedupN1kSeed0", f"{n1k_speedup:.3f}",
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: summary.arms.{{global-recompute,tgms-L1}}."
          "ttf_p50_ms ratio at (store=synth-iv-60k, mix=c1, age=none, seed=0, "
          "n_artifacts=1000)")
    m.add("recStormV2AvoidedDecisionC1Median", f"{avoided_median:.3f}",
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: median(summary.arms.tgms-L1."
          "avoided_recompute_decision) over the 12 c1-mix cells")
    m.add("recStormV2C1Batches", len(survivor_fracs),
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: total per-batch rows over the 12 c1-mix "
          "cells' own -rows.jsonl files (12 cells x 20 batches)")
    m.add("recStormV2SurvivorFractionC1Median", f"{survivor_median:.3f}",
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: median(candidate_survivors / "
          "config.n_registered) over the 240 c1-mix batches")
    m.add("recStormV2PrecisionC1Median", f"{precision_median:.3f}",
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: median(changed_count / candidate_survivors) "
          "over the 240 c1-mix batches")
    m.add("recStormV2SurvivorFractionSynthC1Median", f"{synth_survivor_median:.3f}",
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: median(candidate_survivors / "
          "config.n_registered) over the 120 synth-iv-60k c1-mix batches")
    m.add("recStormV2SurvivorFractionCollegeMsgC1Median", f"{collegemsg_survivor_median:.3f}",
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: median(candidate_survivors / "
          "config.n_registered) over the 120 collegemsg c1-mix batches")
    m.add("recStormV2PrecisionSynthC1Median", f"{synth_precision_median:.3f}",
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: median(changed_count / candidate_survivors) "
          "over the 120 synth-iv-60k c1-mix batches")
    m.add("recStormV2PrecisionCollegeMsgC1Median", f"{collegemsg_precision_median:.3f}",
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: median(changed_count / candidate_survivors) "
          "over the 120 collegemsg c1-mix batches")
    m.add("recStormV2FalseFreshTgmsCellsNonzero", ff_nonzero,
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: count of (cell, arm) pairs among tgms-L0/"
          "tgms-L1 over all 36 cells (72 total) with summary.arms[arm].false_fresh > 0")

    for key, med in per_group_median.items():
        store, mix, age = key
        name = (f"recStormV2Speedup{_STORM_V2_STORE_TOKEN[store]}{_STORM_V2_MIX_TOKEN[mix]}"
                f"{_STORM_V2_AGE_TOKEN[age]}")
        m.add(name, f"{med:.3f}",
              f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: median over seeds 0/1/2 of "
              "summary.arms.{global-recompute,tgms-L1}.ttf_p50_ms ratio at "
              f"(store={store}, mix={mix}, age={age or 'none'}, n_artifacts=1000)")

    m.add("recStormV2SpeedupGridMin", f"{grid_min:.3f}",
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: minimum per-cell "
          "summary.arms.{global-recompute,tgms-L1}.ttf_p50_ms ratio over all 36 cells")
    m.add("recStormV2SpeedupGridMax", f"{grid_max:.3f}",
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: maximum per-cell "
          "summary.arms.{global-recompute,tgms-L1}.ttf_p50_ms ratio over all 36 cells")


# --------------------------------------------------------------------------
# W2ae (partial) -- sum-mode time-to-fresh reconstruction, storm-v2 main
# grid (module docstring's own W2ae section has the full story). Reads
# the same two committed files as compute_c7_storm_v2 above
# (STORM_V2_MAIN_GRID_ROWS + STORM_V2_RECORDS_TARBALL) fresh, independent
# of that function having already run -- including the tarball's own
# sha256 gate, not assumed already-checked (this function is called on
# its own in some tests). None of compute_c7_storm_v2's own macros
# (recStormV2Speedup*, recStormV2SpeedupGridMin/Max, ...) are read,
# recomputed differently, or overwritten here -- every macro below is a
# new name.
# --------------------------------------------------------------------------

def compute_c7_storm_v2_sum_mode(m: Macros) -> None:
    readme_text = STORM_V1_README.read_text(encoding="utf-8")
    readme_sha_match = re.search(
        r"storm-v2-records-36-tasks\.tar\.gz`\s*\n\(sha256 `([0-9a-f]{64})`\)", readme_text)
    require(readme_sha_match is not None,
            f"storm-v2 sum-mode: {relpath(STORM_V1_README)} names a sha256 for "
            "storm-v2-records-36-tasks.tar.gz in its Per-batch rows section")
    if readme_sha_match is not None:
        eq(readme_sha_match.group(1), STORM_V2_RECORDS_TARBALL_SHA256,
           "storm-v2 sum-mode: frozen sha256 constant matches "
           f"{relpath(STORM_V1_README)}'s own quoted value")
    eq(sha256_file(STORM_V2_RECORDS_TARBALL), STORM_V2_RECORDS_TARBALL_SHA256,
       f"storm-v2 sum-mode: {relpath(STORM_V2_RECORDS_TARBALL)} sha256 matches the frozen/"
       "README-quoted value")

    rows = load_jsonl(STORM_V2_MAIN_GRID_ROWS)
    eq(len(rows), 36, "storm-v2 sum-mode: rows.jsonl line count")

    with tarfile.open(STORM_V2_RECORDS_TARBALL, "r:gz") as tf:
        tar_names = set(tf.getnames())
        sum_l1_p50: dict[int, float] = {}
        gr_p50: dict[int, float] = {}
        e2e_over_check: list[float] = []
        max_l1_dev = 0.0
        max_gr_dev = 0.0
        for r in rows:
            tid = r["_task_id"]
            idx = r["record"].index("records/")
            member = r["record"][idx:]
            require(member in tar_names,
                    f"storm-v2 sum-mode: task {tid}'s own record field names a member "
                    f"({member}) present in the tarball")
            raw = tf.extractfile(member).read().decode("utf-8")
            batch_rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
            eq(len(batch_rows), r["config"]["batches"],
               f"storm-v2 sum-mode task {tid}: batch row count matches config.batches")

            l1_check = [b["arms"]["tgms-L1"]["check_wall_ms"] for b in batch_rows]
            l1_refresh = [b["arms"]["tgms-L1"]["refresh_wall_ms"] for b in batch_rows]
            l1_ttf = [b["arms"]["tgms-L1"]["ttf_ms"] for b in batch_rows]
            gr_ttf = [b["arms"]["global-recompute"]["ttf_ms"] for b in batch_rows]
            modes = {b.get("ttf_mode") for b in batch_rows}
            eq(modes, {"end-to-end"},
               f"storm-v2 sum-mode task {tid}: every batch row's own ttf_mode is "
               "end-to-end (the committed main grid's config.ttf_modes == "
               "[\"end-to-end\"])")

            # Reproduction check (verify the file mapping and the nearest-
            # rank percentile match tgms/eval/storm.py's own aggregation,
            # before trusting the sum-mode value below): recompute both
            # tgms-L1's and global-recompute's end-to-end ttf_p50_ms from
            # the raw per-batch ttf_ms and compare against the committed
            # summary.arms[*].ttf_p50_ms this same row already carries.
            recomputed_l1_ttf_p50 = _e2e_percentile(l1_ttf, 0.5)
            recomputed_gr_ttf_p50 = _e2e_percentile(gr_ttf, 0.5)
            committed_l1_ttf_p50 = r["summary"]["arms"]["tgms-L1"]["ttf_p50_ms"]
            committed_gr_ttf_p50 = r["summary"]["arms"]["global-recompute"]["ttf_p50_ms"]
            close_rel(recomputed_l1_ttf_p50, committed_l1_ttf_p50, 0.005,
                      f"storm-v2 sum-mode task {tid}: recomputed end-to-end tgms-L1 "
                      "ttf_p50_ms (nearest-rank over raw per-batch ttf_ms) reproduces "
                      "the committed summary.arms.tgms-L1.ttf_p50_ms")
            close_rel(recomputed_gr_ttf_p50, committed_gr_ttf_p50, 0.005,
                      f"storm-v2 sum-mode task {tid}: recomputed global-recompute "
                      "ttf_p50_ms reproduces the committed summary.arms.global-recompute"
                      ".ttf_p50_ms")
            dev_l1 = abs(recomputed_l1_ttf_p50 - committed_l1_ttf_p50) / committed_l1_ttf_p50
            dev_gr = abs(recomputed_gr_ttf_p50 - committed_gr_ttf_p50) / committed_gr_ttf_p50
            max_l1_dev = max(max_l1_dev, dev_l1)
            max_gr_dev = max(max_gr_dev, dev_gr)

            sum_vals = [c + rf for c, rf in zip(l1_check, l1_refresh)]
            sum_l1_p50[tid] = _e2e_percentile(sum_vals, 0.5)
            gr_p50[tid] = committed_gr_ttf_p50
            for c, t in zip(l1_check, l1_ttf):
                if c:
                    e2e_over_check.append(t / c)

    close(max_l1_dev, 0.0, 0.0005, "storm-v2 sum-mode frozen: max relative deviation, "
          "recomputed vs. committed end-to-end tgms-L1 ttf_p50_ms, over all 36 cells "
          "(0.5% was the task's own bar; this reproduces exactly)")
    close(max_gr_dev, 0.0, 0.0005, "storm-v2 sum-mode frozen: max relative deviation, "
          "recomputed vs. committed end-to-end global-recompute ttf_p50_ms, over all 36 "
          "cells")

    speedup_sum = {tid: gr_p50[tid] / sum_l1_p50[tid] for tid in sum_l1_p50}

    groups: dict[tuple[str, str, str | None], list[int]] = {}
    for r in rows:
        cfg = r["config"]
        groups.setdefault((cfg["store"], cfg["mix"], cfg["age"]), []).append(r["_task_id"])
    eq(len(groups), 12, "storm-v2 sum-mode: 12 distinct (store, mix, age) groups")

    for key, tids in groups.items():
        store, mix, age = key
        eq(len(tids), 3, f"storm-v2 sum-mode: group {key} covers exactly 3 seeds")
        sum_p50_med = statistics.median(sum_l1_p50[tid] for tid in tids)
        speedup_med = statistics.median(speedup_sum[tid] for tid in tids)
        name_p50 = (f"recStormV2LOneSumP50{_STORM_V2_STORE_TOKEN[store]}"
                    f"{_STORM_V2_MIX_TOKEN[mix]}{_STORM_V2_AGE_TOKEN[age]}")
        name_speedup = (f"recStormV2SpeedupSum{_STORM_V2_STORE_TOKEN[store]}"
                        f"{_STORM_V2_MIX_TOKEN[mix]}{_STORM_V2_AGE_TOKEN[age]}")
        m.add(name_p50, tex_num(round(sum_p50_med)),
              f"{relpath(STORM_V2_RECORDS_TARBALL)}: median over seeds 0/1/2 of the "
              "per-seed sum-mode tgms-L1 time-to-fresh p50 (_e2e_percentile of "
              "arms.tgms-L1.check_wall_ms + arms.tgms-L1.refresh_wall_ms over the 20 "
              f"per-batch rows), ms, at (store={store}, mix={mix}, "
              f"age={age or 'none'}, n_artifacts=1000)")
        m.add(name_speedup, f"{speedup_med:.2f}",
              f"{relpath(STORM_V2_RECORDS_TARBALL)}: median over seeds 0/1/2 of "
              "(summary.arms.global-recompute.ttf_p50_ms / sum-mode tgms-L1 p50) -- "
              f"the corrected sibling of recStormV2Speedup{_STORM_V2_STORE_TOKEN[store]}"
              f"{_STORM_V2_MIX_TOKEN[mix]}{_STORM_V2_AGE_TOKEN[age]}, at "
              f"(store={store}, mix={mix}, age={age or 'none'}, n_artifacts=1000)")

    all_speedups_sum = list(speedup_sum.values())
    grid_min_sum = min(all_speedups_sum)
    grid_median_sum = statistics.median(all_speedups_sum)
    grid_max_sum = max(all_speedups_sum)
    m.add("recStormV2SpeedupSumGridMin", f"{grid_min_sum:.2f}",
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: minimum per-cell (global-recompute "
          "ttf_p50_ms / sum-mode tgms-L1 p50) over all 36 cells -- the corrected "
          "sibling of recStormV2SpeedupGridMin")
    m.add("recStormV2SpeedupSumGridMedian", f"{grid_median_sum:.2f}",
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: median per-cell (global-recompute "
          "ttf_p50_ms / sum-mode tgms-L1 p50) over all 36 cells (compute_c7_storm_v2's "
          "own recStormV2Speedup{Grid}* has no Median sibling; added here)")
    m.add("recStormV2SpeedupSumGridMax", f"{grid_max_sum:.2f}",
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: maximum per-cell (global-recompute "
          "ttf_p50_ms / sum-mode tgms-L1 p50) over all 36 cells -- the corrected "
          "sibling of recStormV2SpeedupGridMax")

    n1k_target = [r for r in rows if r["config"]["store"] == "synth-iv-60k"
                  and r["config"]["mix"] == "c1" and r["config"]["age"] is None
                  and r["config"]["seed"] == 0]
    eq(len(n1k_target), 1, "storm-v2 sum-mode: exactly one cell at (synth-iv-60k, c1, "
       "age none, seed 0)")
    n1k_tid = n1k_target[0]["_task_id"]
    m.add("recStormV2SpeedupSumN1kSeed0", f"{speedup_sum[n1k_tid]:.2f}",
          f"{relpath(STORM_V2_RECORDS_TARBALL)}: (global-recompute ttf_p50_ms / "
          "sum-mode tgms-L1 p50) at (store=synth-iv-60k, mix=c1, age=none, seed=0, "
          "n_artifacts=1000) -- the corrected sibling of recStormV2SpeedupN1kSeed0, "
          "same cell")

    # recStormV2LOneE2eOverCheckMedian: pooled over every end-to-end cell
    # and batch this generator has access to -- the main grid's 720
    # batches, recomputed live just above, plus the external-v1 control's
    # 360 batches, read live from the committed EXTERNAL_TGMS_CONTROL_BATCHES
    # record addendum (2026-10-08) via _read_control_batches. The probe
    # cell (synth-iv-60k-c1-none-n10000-s0) and the storm-v2 R-18 probe
    # are both deliberately excluded: both run in ttf_mode == "sum"
    # (compute_c7_storm_v2_probe_sum_mode below), so neither is an
    # end-to-end cell at all.
    control_batches = _read_control_batches()
    control_probe_cid = "synth-iv-60k-c1-none-n10000-s0"
    control_e2e_cells = sorted(c for c in control_batches if c != control_probe_cid)
    eq(len(control_e2e_cells), 18, f"{relpath(EXTERNAL_TGMS_CONTROL_BATCHES)}: "
       "end-to-end control cells (19 - the 1 sum-mode probe cell)")

    pooled_ratios = list(e2e_over_check)
    for cell in control_e2e_cells:
        batches = control_batches[cell]
        eq(len(batches), 20, f"control batches cell {cell}: batch count")
        modes = {b["ttf_mode"] for b in batches}
        eq(modes, {"end-to-end"}, f"control batches cell {cell}: every batch's own "
           "ttf_mode is end-to-end")
        for b in batches:
            c = b["arms"]["tgms-L1"]["check_wall_ms"]
            t = b["arms"]["tgms-L1"]["ttf_ms"]
            if c:
                pooled_ratios.append(t / c)
    eq(len(pooled_ratios), 720 + 18 * 20,
       "storm-v2 sum-mode: pooled end-to-end (ttf_ms / check_wall_ms) sample size "
       "(36 main-grid cells x 20 batches + 18 control cells x 20 batches)")
    e2e_over_check_median = statistics.median(pooled_ratios)
    close(e2e_over_check_median, 0.999, 0.001, "storm-v2 sum-mode frozen: median "
          "(tgms-L1 ttf_ms / tgms-L1 check_wall_ms) pooled over every end-to-end cell "
          "and batch (main grid + control) -- the instrument-error evidence that the "
          "end-to-end TTF mode's recorded tgms-L1.ttf_ms is really just its own "
          "check_wall_ms, not check_wall_ms + refresh_wall_ms")
    m.add("recStormV2LOneE2eOverCheckMedian", f"{e2e_over_check_median:.3f}",
          f"{relpath(STORM_V2_MAIN_GRID_ROWS)} (720 batches) + "
          f"{relpath(EXTERNAL_TGMS_CONTROL_BATCHES)}'s 18 end-to-end cells (360 "
          "batches): median over all 1,080 pooled batches of arms.tgms-L1.ttf_ms / "
          "arms.tgms-L1.check_wall_ms")


# --------------------------------------------------------------------------
# W2ae (partial) -- sum-mode time-to-fresh reconstruction, storm-v2 R-18
# probe. Lands recStormV2SpeedupSumProbe beside compute_c7_storm_v2_probe's
# own recStormV2ProbeSpeedupN10k without touching it. As the module
# docstring's W2ae section explains, this probe's own rows already carry
# ttf_mode == "sum" -- it was never affected by the end-to-end bug -- so
# this macro recomputes to the same number (to rounding) as the existing
# one; landed anyway, both because the task asked for it by name and
# because it is a live self-check that this reconstruction reproduces a
# value that needed no correcting.
# --------------------------------------------------------------------------

def compute_c7_storm_v2_probe_sum_mode(m: Macros) -> None:
    rows = load_jsonl(STORM_V2_R18_PROBE_ROWS)
    rows.sort(key=lambda r: r["batch_index"])
    eq(len(rows), 5, "storm-v2 probe sum-mode: rows.jsonl line count")

    modes = {r.get("ttf_mode") for r in rows}
    eq(modes, {"sum"}, "storm-v2 probe sum-mode frozen: every batch row's own ttf_mode "
       "is sum -- this probe was never affected by the end-to-end collapse (see the "
       "module docstring's W2ae section)")

    l1_check = [r["arms"]["tgms-L1"]["check_wall_ms"] for r in rows]
    l1_refresh = [r["arms"]["tgms-L1"]["refresh_wall_ms"] for r in rows]
    l1_ttf = [r["arms"]["tgms-L1"]["ttf_ms"] for r in rows]
    gr_ttf = [r["arms"]["global-recompute"]["ttf_ms"] for r in rows]

    # Since ttf_mode is already "sum", ttf_ms must equal check_wall_ms +
    # refresh_wall_ms exactly, batch by batch -- checked directly (not
    # just at the aggregate p50 level below) before trusting that.
    for i, (c, rf, t) in enumerate(zip(l1_check, l1_refresh, l1_ttf)):
        eq(t, c + rf, f"storm-v2 probe sum-mode batch {i}: arms.tgms-L1.ttf_ms == "
           "check_wall_ms + refresh_wall_ms exactly")

    sum_vals = [c + rf for c, rf in zip(l1_check, l1_refresh)]
    sum_l1_p50 = _e2e_percentile(sum_vals, 0.5)
    gr_p50 = _e2e_percentile(gr_ttf, 0.5)

    # Since ttf_mode is already "sum" on every row, ttf_ms == check_wall_ms
    # + refresh_wall_ms exactly, batch by batch -- so the sum-mode p50
    # must equal the probe's own already-recomputed/committed
    # summary.arms.tgms-L1.ttf_p50_ms exactly, not just within 0.5%.
    d = json.loads(STORM_V2_R18_PROBE.read_text(encoding="utf-8"))
    eq(sum_l1_p50, d["summary"]["arms"]["tgms-L1"]["ttf_p50_ms"],
       "storm-v2 probe sum-mode: sum-mode tgms-L1 p50 (check_wall_ms + refresh_wall_ms, "
       "nearest-rank over the 5 batches) equals the committed summary.arms.tgms-L1."
       "ttf_p50_ms exactly -- ttf_mode is already sum, so there is nothing to correct")
    eq(gr_p50, d["summary"]["arms"]["global-recompute"]["ttf_p50_ms"],
       "storm-v2 probe sum-mode: recomputed global-recompute ttf p50 matches the "
       "committed summary.arms.global-recompute.ttf_p50_ms")

    speedup_sum = gr_p50 / sum_l1_p50
    close(speedup_sum, 1.946, 0.001, "storm-v2 probe sum-mode frozen: speedup of "
          "tgms-L1 over global-recompute, sum-mode definition -- same value (to "
          "rounding) as compute_c7_storm_v2_probe's own frozen recStormV2ProbeSpeedupN10k "
          "speedup, since this probe's ttf_mode is already sum")

    m.add("recStormV2SpeedupSumProbe", f"{speedup_sum:.2f}",
          f"{relpath(STORM_V2_R18_PROBE_ROWS)}: median(arms.global-recompute.ttf_ms) / "
          "_e2e_percentile(arms.tgms-L1.check_wall_ms + arms.tgms-L1.refresh_wall_ms) "
          "over the 5 batches -- the sum-mode sibling of recStormV2ProbeSpeedupN10k; "
          "this probe's own ttf_mode is already \"sum\" (never affected by the "
          "end-to-end collapse), so the two values agree to rounding")


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

    m.add("recD160Tasks", len(task_ids),
          f"{relpath(D160_ROWS)}: distinct task_id values among system==ours rows")
    m.add("recD160TaskRuns", len(ours),
          f"{relpath(D160_ROWS)}: row count for system==ours (94 tasks x 3 seeds)")
    m.add("recD160OursCarrying", len(ours_carry),
          f"{relpath(D160_ROWS)}: ours rows with answer_object.claims non-empty, pooled "
          "over 3 seeds")
    m.add("recD160OursCoverage", f"{ours_coverage:.3f}",
          f"{relpath(D160_ROWS)}: recD160OursCarrying / recD160TaskRuns")
    m.add("recD160OursCondAcc", f"{ours_cond_acc:.3f}",
          f"{relpath(D160_ROWS)}: mean(em) over ours claim-carrying rows")
    m.add("recD160OursUcrGated", int(ours_ucr_gated),
          f"{relpath(D160_ROWS)}: mean(ucr) over ours claim-carrying rows (post-gate)")
    m.add("recD160OursUcrPreGate", f"{ours_ucr_pre:.3f}",
          f"{relpath(D160_ROWS)}: mean(ucr_pre_gate) over the 257 ours rows that reached "
          "claim proposal pre-gate")
    m.add("recD160B6eCoverage", f"{b6e_coverage:.3f}",
          f"{relpath(D160_ROWS)}: b6e rows with answer_object.claims non-empty / 282, pooled")
    m.add("recD160B6eCondAcc", f"{b6e_cond_acc:.3f}",
          f"{relpath(D160_ROWS)}: mean(em) over b6e claim-carrying rows")
    m.add("recD160B5Em", f"{b5_em:.3f}",
          f"{relpath(D160_ROWS)}: mean(em) over all 282 b5 rows (ungated, deterministic)")
    m.add("recD160LlmDirectCarrying", len(llm_carry),
          f"{relpath(D160_ROWS)}: llm_direct rows with answer_object.claims non-empty, pooled")
    m.add("recD160LlmDirectOverflowErrors", len(llm_overflow),
          f"{relpath(D160_ROWS)}: llm_direct rows whose task_error is "
          "litellm.ContextWindowExceededError, of 282 (known pre-tokenizer-fix limitation, "
          "see benchmarks/d160-collegemsg-v1/README.md)")
    m.add("recOldGateCoverage", f"{old_gate_coverage:.3f}",
          f"{relpath(SITE_FACTS)}: unsupported_claims.scope_required 199/282, cross-checked "
          f"against {relpath(STABILITY_MD)} section 9's own stated 0.706 -- the pre-D-160-gate "
          "coverage, printed only beside recD160OursCoverage, never in its place")
    m.add("recOldGateUcr", int(float(old_uc["value"])),
          f"{relpath(SITE_FACTS)}: unsupported_claims.value, of {old_gate_ucr_denominator} "
          "emitted answers -- the pre-D-160-gate UCR")
    m.add("recOldGateCondAcc", f"{old_gate_cond_acc:.3f}",
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

    m.add("recD160LlmDirectCoverageFixed", f"{coverage:.3f}",
          f"{relpath(D160_ROWS_FIX)}: llm_direct rows with answer_object.claims non-empty / "
          "282, pooled over 3 seeds -- the fixed-budget re-run of recD160LlmDirectCarrying, "
          "still 0.000 (the fix removes the context-overflow crash, not the gate's verdict)")
    m.add("recD160LlmDirectErrorsFixed", len(errors),
          f"{relpath(D160_ROWS_FIX)}: llm_direct task_error row count, of 282 -- 0 after the "
          "real-tokenizer budget fix, vs recD160LlmDirectOverflowErrors (216) before it")
    m.add("recD160LlmDirectRawEmFixed", f"{raw_em:.3f}",
          f"{relpath(D160_ROWS_FIX)}: mean(em) of meta.pre_gate_answer scored via "
          "tgms.eval.metrics.score_answer/extract_pred against "
          "benchmarks/frozen-v1/suite-collegemsg.json's gold/answer_kind fields, over all 282 "
          "rows (complete, vs the pre-fix record's partial n=66 easy-task-only sample)")
    m.add("recD160LlmDirectTokenizerFixed", r"hf\_real",
          f"{relpath(D160_ROWS_FIX)}: meta.tokenizer_kind, asserted uniform across all 282 rows")
    m.add("recD160LlmDirectBudgetFixed", 8000,
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

    m.add("recCorruptionTrials", tex_num(pre["total_trials"]),
          f"{relpath(CORRUPTION_PRE)}: total_trials, == {relpath(CORRUPTION_POST)}'s own "
          "(both 10,000-trial sweeps)")
    m.add("recCorruptionClasses", len(classes),
          f"{relpath(CORRUPTION_PRE)}: distinct classes in stats.detection_matrix keys "
          "(the 13 on-disk file families), unchanged post-A10")
    m.add("recCorruptionMutations", len(mutations),
          f"{relpath(CORRUPTION_PRE)}: distinct mutations in stats.detection_matrix keys, "
          "unchanged post-A10")
    m.add("recCorruptionDetected", tex_num(detected_post),
          f"{relpath(CORRUPTION_POST)}: recomputed DETECTED total, of 10,000 -- the deployed "
          "(post-A10) system's headline count; see recCorruptionDetectedPre/Post for the "
          "before/after pair")
    m.add("recCorruptionDetectedPre", tex_num(detected_pre),
          f"{relpath(CORRUPTION_PRE)}: recomputed DETECTED total, of 10,000, before task A10")
    m.add("recCorruptionDetectedPost", tex_num(detected_post),
          f"{relpath(CORRUPTION_POST)}: recomputed DETECTED total, of 10,000, after task A10")
    m.add("recCorruptionSilentPre", recomputed_silent_pre,
          f"{relpath(CORRUPTION_PRE)}: recomputed SILENT total, of 10,000, before task A10")
    m.add("recCorruptionSilentPost", recomputed_silent_post,
          f"{relpath(CORRUPTION_POST)}: recomputed SILENT total, of 10,000, after task A10")
    m.add("recCorruptionBlobDetectedPre", f"{det_blob_pre}/{n_blob_pre}",
          f"{relpath(CORRUPTION_PRE)}: DETECTED/trials summed over the six artifact_blob "
          "mutations task A10 fixed (append_garbage/flip_bit/flip_byte/swap_same_class/"
          "truncate/zero_span), before the fix")
    m.add("recCorruptionBlobDetectedPost", f"{det_blob_post}/{n_blob_post}",
          f"{relpath(CORRUPTION_POST)}: DETECTED/trials summed over the same six artifact_blob "
          "mutations, after the fix")
    m.add("recCorruptionBlobTrials", tex_num(n_blob_all_pre),
          f"{relpath(CORRUPTION_PRE)}: trials summed over all seven artifact_blob mutations "
          "(== the six-mutation fix population 621 + the already-detected mutation's own "
          f"{n_already_pre}), unchanged post-A10")
    m.add("recCorruptionBlobDetectedAllPre", f"{det_blob_all_pre}/{n_blob_all_pre}",
          f"{relpath(CORRUPTION_PRE)}: DETECTED/trials summed over all seven artifact_blob "
          "mutations, before the fix -- distinct from recCorruptionBlobDetectedPre, which "
          "covers only the six mutations A10 fixed")
    m.add("recCorruptionBlobDetectedAllPost", f"{det_blob_all_post}/{n_blob_all_post}",
          f"{relpath(CORRUPTION_POST)}: DETECTED/trials summed over all seven artifact_blob "
          "mutations, after the fix")
    m.add("recCorruptionBlobMutationAlreadyDetected", already_detected_mutation.replace("_", r"\_"),
          f"{relpath(CORRUPTION_PRE)}: the one artifact_blob mutation (of all seven) with a "
          "nonzero pre-A10 DETECTED count, recomputed as the mutation outside the six-mutation "
          "fix set with detected>0 -- not hard-coded")
    m.add("recCorruptionCellsMovedPost", len(moved),
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

    m.add("recLadderPlans", tex_num(len(plan_ids)),
          f"{relpath(LADDER_RAW[0])}: distinct plan_id values in the rung-1 rows")
    m.add("recLadderOperatorsCovered", tex_num(len(all_ops)),
          f"{relpath(LADDER_RAW[0])}: union of every plan's ops_in_plan, rung 1 (13 LEAF_SCOPES "
          "+ compute)")
    m.add("recLadderRung1Min", f"{rung1_min:.3f}",
          f"{relpath(LADDER_MERGED)}: min over 14 ops of rung1_leaf_overhead[op]."
          f"leaf_over_direct_median ({rung1_min_op})")
    m.add("recLadderRung1Max", f"{rung1_max:.3f}",
          f"{relpath(LADDER_MERGED)}: max over 14 ops of rung1_leaf_overhead[op]."
          f"leaf_over_direct_median ({rung1_max_op})")
    m.add("recLadderRung2EntityHistory", f"{entity_ratio:.2f}",
          f"{relpath(LADDER_MERGED)}: rung2_compiled_vs_kernel.entity_history."
          "compiled_over_kernel_median")
    m.add("recLadderRung2VersionHistory", f"{version_ratio:.2f}",
          f"{relpath(LADDER_MERGED)}: rung2_compiled_vs_kernel.version_history."
          "compiled_over_kernel_median")
    m.add("recLadderRung3BytesOneStepMedian", tex_num(one_step_bytes_median),
          f"{relpath(LADDER_MERGED)}: median over the 9 one-step plans of "
          "rung3_trace_bytes[plan].bytes_median")
    m.add("recLadderRung3BytesThreeStepMedian", tex_num(three_step_bytes_median),
          f"{relpath(LADDER_MERGED)}: median over the 3 three-step plans of "
          "rung3_trace_bytes[plan].bytes_median")
    m.add("recLadderRung3Deterministic", tex_num(n_deterministic),
          f"{relpath(LADDER_DIR)}/raw/*.json: plans confirmed bit-identical across all 5 reps "
          "and all 3 seeds (asserted above), of 12")
    m.add("recLadderRung4VerifyMsMedian", f"{verify_ms_median:.2f}",
          f"{relpath(LADDER_MERGED)}: median over the 12 plans of rung4_verify_ms[plan]."
          "p50_ms_median, ms")
    m.add("recLadderRung5TokensMedian", f"{tokens_median_overall:.1f}",
          f"{relpath(LADDER_MERGED)}: median over the 12 plans of rung5_tokens_tool_calls[plan]."
          "tokens_total_median")
    m.add("recLadderRung5ToolCallsEqualExecutedSteps", tex_num(n_tool_calls_eq_executed),
          f"{relpath(LADDER_DIR)}/raw/*.json: plans with tool_calls == executed steps "
          "(falsifier (a)'s own 'executed steps' wording, not a naive n_steps comparison), "
          "asserted on all 12")
    m.add("recLadderPlansTruncated", tex_num(len(truncated_plans)),
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
#       recSoakWriterErrorsManifest is explicitly labelled as the wrong,
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
    m.add("recSoakHours", tex_num(int(hours)),
          f"{relpath(LONGEVITY_MANIFEST)}: config.duration_s / 3600")
    m.add("recSoakCommit", manifest["git_commit"],
          f"{relpath(LONGEVITY_MANIFEST)}: git_commit")
    m.add("recSoakEntitiesStart", tex_num(entities_start),
          f"{relpath(LONGEVITY_MANIFEST)}: config.store_name / dataset.name "
          "(\"synth-1m-native\") -- the store's own \"1m\" label, not a counted field; "
          "final_stats.n_entities confirms growth past this nominal start")
    m.add("recSoakEntitiesEnd", tex_num(entities_end),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.final_stats.n_entities")
    m.add("recSoakBatches", tex_num(total_batches),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.total_batches")
    m.add("recSoakWriterLives", tex_num(by_life["lives"]),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_LIFE)}: lives (== len(per_life_errors))")
    m.add("recSoakRecoveries", tex_num(len(recoveries_rows)),
          f"{relpath(LONGEVITY_RECOVERIES)}: row count, == summary.recoveries")
    m.add("recSoakRecoveriesSigabrt", tex_num(n_sigabrt),
          f"{relpath(LONGEVITY_RECOVERIES)}: rows with returncode == -6 (SIGABRT)")
    m.add("recSoakRecoveriesExit137", tex_num(n_exit137),
          f"{relpath(LONGEVITY_RECOVERIES)}: rows with returncode == 137 (os._exit(137))")
    m.add("recSoakUnexpectedRecoveries", tex_num(unexpected),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.unexpected_writer_deaths -- all 41 "
          "recoveries are the harness's own designed restart cycle (kind==\"designed\")")
    m.add("recSoakReaderDeaths", tex_num(len(reader_rows)),
          f"{relpath(LONGEVITY_READER_RESTARTS)}: row count, == summary.reader_restarts")
    m.add("recSoakReaderDeathCause", "reader torn-tail race, pre-fix engine",
          f"{relpath(LONGEVITY_READER_RESTARTS)}: both rows (readers 6, 5) are the "
          "D-086-reader-torn-tail-race StateError shape; ops/failure_ledger.jsonl's D-086 "
          "entry (cross-checked when present) confirms both instances against commit "
          "886805f, which predates the fix series (ef97d2d, 43f6ef4, 3a664a8)")
    m.add("recSoakVerifyHealthy", "true",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.verify_healthy")
    m.add("recSoakWriterErrorsManifest", tex_num(manifest_error_count),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.error_count -- the harness's own "
          "counter_latest label-collision defect (last writer life only, see README.md); "
          "not a run total")
    m.add("recSoakWriterErrorsTrue", tex_num(true_total_errors),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_LIFE)}: sum(per_life_errors), 42 lives")
    m.add("recSoakCorrectionsAppliedTrue", tex_num(corrections_applied),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_LIFE)}: "
          "true_total_corrections_applied_all_lives, 42 lives -- ops issued, not "
          "identities landed (see README.md's \"errors observed\" section)")
    m.add("recSoakCorrectionsSkippedTrue", tex_num(corrections_skipped),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_LIFE)}: "
          "true_total_corrections_skipped_all_lives, 42 lives")
    m.add("recSoakThroughputStart", f"{throughput_start:.2f}",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.drift.throughput_first_hour_avg, commits/s")
    m.add("recSoakThroughputEnd", f"{throughput_end:.2f}",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.drift.throughput_last_hour_avg, commits/s")
    m.add("recSoakP99StartMs", f"{p99_start:.1f}",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.drift.commit_p99_first_hour_max, ms")
    m.add("recSoakP99EndMs", f"{p99_end:,.1f}".replace(",", "{,}"),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.drift.commit_p99_last_hour_max, ms")
    m.add("recSoakManifestGrowthBps", f"{manifest_growth:.1f}",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.metadata_growth_slope_bytes_per_s.manifests")
    m.add("recSoakSegmentGrowthBps", f"{segment_growth:,.1f}".replace(",", "{,}"),
          f"{relpath(LONGEVITY_MANIFEST)}: summary.metadata_growth_slope_bytes_per_s.segments")
    m.add("recSoakDigestStatus", "not computed",
          f"{relpath(LONGEVITY_MANIFEST)}: summary.digest_equal is JSON null -- the final "
          "replay/digest-equivalence step never ran (disk guard skip), never \"computed "
          "and found False\" (text macro, not a number)")
    m.add("recSoakReplayProjectedTB", f"{projected_tb:.1f}",
          f"{relpath(LONGEVITY_ORCHESTRATOR_LOG)}: disk_guard_replay_skip line's "
          "projected_mb / 1e6, cross-checked against longevity_ledger.jsonl and "
          "recomputed from 243.0*total_batches**2/1e6 (scripts/longevity_run.py's own "
          "D-149 projection formula, quoted in README.md)")
    m.add("recSoakCompactions", tex_num(compactions),
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
    m.add("recSoakWriterWithinLifeSlopeMedianKBps", f"{median_kb:.1f}",
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: median(writer_within_life_rss_slope."
          "per_life[*].slope_kb_per_s), 42 lives")
    m.add("recSoakWriterWithinLifeSlopeMinKBps", f"{min_kb:.1f}",
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: min(writer_within_life_rss_slope."
          "per_life[*].slope_kb_per_s)")
    m.add("recSoakWriterWithinLifeSlopeMaxKBps", f"{max_kb:.1f}",
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: max(writer_within_life_rss_slope."
          "per_life[*].slope_kb_per_s)")
    m.add("recSoakWriterLivesFitted", tex_num(writer_slope["n_lives_with_fit"]),
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: writer_within_life_rss_slope."
          "n_lives_with_fit, recomputed as len(per_life)")
    m.add("recSoakWriterLivesPositive", tex_num(n_positive),
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: recomputed count(per_life[*]."
          "slope_kb_per_s > noise_threshold_kb_per_s=5.0), matches the file's own "
          "n_positive_beyond_noise_5kb_s")
    m.add("recSoakWriterNoiseFloorKBps", tex_num(int(noise_floor)),
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: writer_within_life_rss_slope."
          "noise_threshold_kb_per_s")
    m.add("recSoakReaderWithinLifeSlopeMedianKBps", f"{reader_median_kb:.2f}",
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: median of every fitted segment slope "
          "across reader_rss_slope_by_idx (10 segments, 8 readers), matches "
          "reader_rss_slope_pooled.median_kb_per_s")
    m.add("recSoakReaderWithinLifeSlopeMaxKBps", f"{reader_max_kb:.2f}",
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: max of every fitted segment slope "
          "across reader_rss_slope_by_idx, matches reader_rss_slope_pooled.max_kb_per_s "
          "-- the short post-restart segment for reader 5")
    m.add("recSoakFirstVsLastSlopeKBps", f"{first_vs_last_kb:.1f}",
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: summary.memory_slope_kb_per_s -- the "
          "old two-point first-vs-last figure, carried over unchanged from the original "
          "manifest; SUPERSEDED by the within-life figures above for the memory-FAIL "
          "finding (see gate_e_report_rederived_2026-09-15.md)")
    m.add("recSoakCommitsPerSecMean", f"{commits_per_sec_mean_1dp:.1f}",
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: mean(summary.drift."
          "throughput_first_hour_avg, summary.drift.throughput_last_hour_avg)")
    m.add("recSoakBytesPerCommitLiveKB", tex_num(bytes_per_commit_kb_rounded),
          f"{relpath(LONGEVITY_SUMMARY_REDERIVED)}: derived (arithmetic, not measured): "
          "round(writer_within_life_rss_slope.median_kb_per_s / recSoakCommitsPerSecMean's "
          "unrounded mean) -- README's own prose gives ~158 KB/commit using a coarser "
          "~20 commits/s rate")
    m.add("recSoakReplayOutcome", outcome,
          f"{relpath(LONGEVITY_REPLAY_CHECK)}: outcome (text macro, not a number)")
    m.add("recSoakReplayOomRssGB", f"{oom_rss_gb:.1f}",
          f"{relpath(LONGEVITY_REPLAY_CHECK)}: summary.attempt_1.dmesg_line's "
          "anon-rss:<N>kB, parsed and divided by 1e6")
    m.add("recSoakReplayOomGeneration", tex_num(oom_generation),
          f"{relpath(LONGEVITY_REPLAY_CHECK)}: summary.attempt_1."
          "highest_manifest_generation_observed, cross-checked against "
          "compactions_inferred * 501")
    m.add("recSoakReplayFractionApplied", f"{round(fraction_applied, 3):.3f}",
          f"{relpath(LONGEVITY_REPLAY_CHECK)}: summary.attempt_1.batches_applied_inferred "
          "/ dataset.total_batches")
    m.add("recSoakReplayKBPerGeneration", f"{round(kb_per_generation, 1):.1f}",
          f"{relpath(LONGEVITY_REPLAY_CHECK)}: (dmesg anon-rss kB) / "
          "highest_manifest_generation_observed")
    m.add("recSoakReplayElapsedH", f"{round(elapsed_h, 2):.2f}",
          f"{relpath(LONGEVITY_REPLAY_CHECK)}: summary.attempt_1.wall_s / 3600")


def compute_longevity_soak_two(m: Macros) -> None:
    """Lane W2p: P-SOAK2, the post-fix second soak (commit ``eed91c0``,
    internal plan memo Sec 4.3b) -- see benchmarks/longevity-v1/README.md's
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
    throughput_ratio_end_over_start = throughput_end / throughput_start
    eq(round(throughput_ratio_end_over_start, 3), 0.479,
       "Soak2 frozen: ratio, last-hour throughput average / first-hour throughput "
       "average -- the 'last/first $0.479$' figure the eval paragraph currently "
       "types directly rather than through a macro (soak3's analogous figure is "
       "recSoakThroughputLastOverFirstDayRatioThree)")
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
    m.add("recSoakCommitTwo", manifest["git_commit"],
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: git_commit")
    m.add("recSoakHoursTwo", tex_num(int(hours)),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: config.duration_s / 3600")
    m.add("recSoakWriterLivesTwo", tex_num(writer_lives),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.writer_totals_all_lives.lives")
    m.add("recSoakRecoveriesTwo", tex_num(len(recoveries_rows)),
          f"{relpath(LONGEVITY_RECOVERIES_TWO)}: row count, == summary.recoveries")
    m.add("recSoakRecoveriesSigabrtTwo", tex_num(n_sigabrt),
          f"{relpath(LONGEVITY_RECOVERIES_TWO)}: rows with returncode == -6 (SIGABRT)")
    m.add("recSoakRecoveriesExit137Two", tex_num(n_exit137),
          f"{relpath(LONGEVITY_RECOVERIES_TWO)}: rows with returncode == 137 (os._exit(137))")
    m.add("recSoakUnexpectedRecoveriesTwo", tex_num(unexpected),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.unexpected_writer_deaths")
    m.add("recSoakWriterWithinLifeSlopeMedianKBpsTwo", f"{median_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_TWO)}: median(writer_lives[*]."
          "slope_kb_per_s_least_squares), 4 lives")
    m.add("recSoakWriterWithinLifeSlopeMinKBpsTwo", f"{min_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_TWO)}: min(writer_lives[*]."
          "slope_kb_per_s_least_squares)")
    m.add("recSoakWriterWithinLifeSlopeMaxKBpsTwo", f"{max_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_TWO)}: max(writer_lives[*]."
          "slope_kb_per_s_least_squares)")
    m.add("recSoakReaderWithinLifeSlopeMinKBpsTwo", f"{reader_min_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_TWO)}: min(readers[*].slope_kb_per_s_least_squares), "
          "8 readers (reader_restarts=0, one fitted segment each)")
    m.add("recSoakReaderWithinLifeSlopeMaxKBpsTwo", f"{reader_max_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_TWO)}: max(readers[*].slope_kb_per_s_least_squares)")
    m.add("recSoakDigestEqualTwo", "true",
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.digest_equal is the JSON literal "
          "true -- the replay/digest step actually completed, unlike W2g's disk-guard skip")
    m.add("recSoakBatchesTwo", tex_num(total_batches),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.total_batches")
    m.add("recSoakFullVerifyOverlapCountTwo", tex_num(overlap_count),
          f"{relpath(LONGEVITY_VERIFY_FULL_TWO)}: PROBLEMS count (believed-versions-overlap "
          "class), 0 vs. W2g's 13,714 on the pre-fix store")
    m.add("recSoakFullVerifyVerdictTwo", "healthy",
          f"{relpath(LONGEVITY_VERIFY_FULL_TWO)}: verdict line (text macro, not a number)")
    m.add("recSoakWriterErrorsTrueTwo", tex_num(true_writer_errors),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_LIFE_TWO)}: sum(per_life[*].errors), 4 lives, "
          "cross-checked against manifest's own writer_totals_all_lives.errors")
    m.add("recSoakWriterErrorsClassTwo", "NotFoundError",
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_LIFE_TWO)}: the sole exception class across "
          "every writer error in all 4 lives (text macro, not a number)")
    m.add("recSoakReaderErrorsTrueTwo", tex_num(true_reader_errors),
          f"{relpath(LONGEVITY_READER_ERRORS_BY_CLASS_TWO)}: OSError + StateError totals, "
          "cross-checked against manifest's own reader_errors_total")
    m.add("recSoakReaderErrorsOSErrorTwo", tex_num(reader_oserror),
          f"{relpath(LONGEVITY_READER_ERRORS_BY_CLASS_TWO)}: by_class_total.OSError -- new "
          "to this soak, 0 in W2g's metrics.jsonl")
    m.add("recSoakReaderErrorsStateErrorTwo", tex_num(reader_stateerror),
          f"{relpath(LONGEVITY_READER_ERRORS_BY_CLASS_TWO)}: by_class_total.StateError")
    m.add("recSoakReaderQueriesTwo", tex_num(reader_queries),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.reader_queries_total")
    m.add("recSoakThroughputStartTwo", str(throughput_start),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.drift.throughput_first_hour_avg, "
          "commits/s")
    m.add("recSoakThroughputEndTwo", str(throughput_end),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.drift.throughput_last_hour_avg, "
          "commits/s")
    m.add("recSoakThroughputLastOverFirstRatioTwo", f"{throughput_ratio_end_over_start:.3f}",
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.drift.throughput_last_hour_avg / "
          "summary.drift.throughput_first_hour_avg, recomputed here from this record's own "
          "two figures (soak3's analogous macro is recSoakThroughputLastOverFirstDayRatioThree, "
          "sourced from throughput-3.json's own precomputed ratio field instead -- soak2 has "
          "no equivalent side-file, only the manifest's first/last-hour drift block)")
    m.add("recSoakP99StartMsTwo", str(p99_start),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.drift.commit_p99_first_hour_max, ms")
    m.add("recSoakP99EndMsTwo", str(p99_end),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.drift.commit_p99_last_hour_max, ms")
    m.add("recSoakManifestGrowthBpsTwo", f"{manifest_growth:.3f}",
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.metadata_growth_slope_bytes_per_s."
          "manifests")
    m.add("recSoakSegmentGrowthBpsTwo", f"{segment_growth:,.3f}".replace(",", "{,}"),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.metadata_growth_slope_bytes_per_s."
          "segments")
    m.add("recSoakEntitiesEndTwo", tex_num(entities_end),
          f"{relpath(LONGEVITY_MANIFEST_TWO)}: summary.final_stats.n_entities, "
          "cross-checked against verify-full-soak2-2026-09-17.txt's own dictionary-records "
          "count (both agree)")
    m.add("recSoakCompactionsTwo", tex_num(compactions),
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

    m.add("recSoakReaderOnsetEarliestRowsTwo", tex_num(onset_earliest_rows),
          f"{relpath(LONGEVITY_READER_ONSET_ROWS_TWO)}: earliest_reader_onset."
          "last_compaction_before.edge_rows -- the last compaction (compactions-2.jsonl) "
          "before reader 6's onset_t_plus_s=61417.1s, mapped from that file's own "
          "perf_counter clock onto reader_error_counts_by_class-2.json's wall-clock "
          "onset via metrics.jsonl's compactions_total counter (method and the mapping's "
          "own error bound, ~60-72s / up to 3 ambiguous rows, stated in the side-file)")
    m.add("recSoakReaderOnsetLatestRowsTwo", tex_num(onset_latest_rows),
          f"{relpath(LONGEVITY_READER_ONSET_ROWS_TWO)}: latest_reader_onset."
          "last_compaction_before.edge_rows -- the last compaction before reader 4's "
          "onset_t_plus_s=75993.1s, same reconciliation method as "
          "recSoakReaderOnsetEarliestRowsTwo")
    m.add("recSoakReaderOnsetEarliestSTwo", f"{onset_earliest_s:.1f}",
          f"{relpath(LONGEVITY_READER_ERRORS_BY_CLASS_TWO)}: "
          "osrror_storm_onset_by_reader.6.onset_t_plus_s, s into the run (RUN_STARTED "
          "2026-09-16T00:53:01Z) -- the minimum onset_t_plus_s across all 8 readers")
    m.add("recSoakReaderOnsetLatestSTwo", f"{onset_latest_s:.1f}",
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
       "summary.total_batches, W2g's recSoakBatches)")
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
    m.add("recSoakVerifyFullGeneration", tex_num(orig_generation),
          f"{relpath(LONGEVITY_VERIFY_FULL)}: first entry's generation: header (the "
          "original, pre-replay soak store)")
    m.add("recSoakVerifyFullOverlapCount", tex_num(orig_problems),
          f"{relpath(LONGEVITY_VERIFY_FULL)}: first entry's PROBLEMS(N) header, "
          "recomputed as len(row/believed-versions-overlap bullets) in that entry")
    m.add("recSoakVerifyFullVerdict", orig_verdict,
          f"{relpath(LONGEVITY_VERIFY_FULL)}: first entry's verdict: line (text macro, "
          "not a number)")
    m.add("recSoakReplay2VerifyGeneration", tex_num(replay_generation),
          f"{relpath(LONGEVITY_VERIFY_FULL)}: second entry's generation: header (REPLAY-2's "
          "replayed store), cross-checked against replay-check-2-2026-09.json's own "
          "summary.manifest_current_generation")
    m.add("recSoakReplay2VerifyOverlapCount", tex_num(replay_problems),
          f"{relpath(LONGEVITY_VERIFY_FULL)}: second entry's PROBLEMS(N) header -- "
          "identical to recSoakVerifyFullOverlapCount, cross-checked against "
          "replay-check-2-2026-09.json's own summary.full_verify_of_replayed_store.problems")
    m.add("recSoakReplay2VerifyVerdict", replay_verdict,
          f"{relpath(LONGEVITY_VERIFY_FULL)}: second entry's verdict: line (text macro, "
          "not a number)")
    m.add("recSoakReplay2BatchesApplied", tex_num(batches_applied),
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: summary.batches_applied, of "
          "dataset.total_batches (== recSoakBatches)")
    m.add("recSoakReplay2Compactions", tex_num(compactions_inferred),
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: summary.compactions_inferred, "
          "cross-checked against manifest_current_generation - batches_applied")
    m.add("recSoakReplay2WallS", tex_num(int(wall_s)),
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: summary.wall_s")
    m.add("recSoakReplay2ElapsedH", f"{elapsed_h:.2f}",
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: summary.wall_s / 3600, hours (≈ 8h20m)")
    m.add("recSoakReplay2PeakRssKB", tex_num(peak_rss_kb),
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: summary.peak_rss_kb, recomputed as "
          "max(summary.rss_series[*].rss_kb)")
    m.add("recSoakReplay2PeakRssGB", f"{peak_rss_gb:.2f}",
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: summary.peak_rss_kb / 1e6, GB")
    m.add("recSoakReplay2PeakDiskMB", tex_num(peak_disk_mb),
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: summary.peak_disk_mb_observed")
    m.add("recSoakReplay2RssSamples", tex_num(len(rss_series)),
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: len(summary.rss_series)")
    m.add("recSoakReplay2DigestEqual", "true",
          f"{relpath(LONGEVITY_REPLAY_CHECK_2)}: summary.digest_equal is the JSON literal "
          "true (text macro, not a number)")
    m.add("recSoakReplay2DigestPrefix", digest_prefix,
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
    this script; ``recSoakHuntPatternReproduced`` states the
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
    eq(manifest["machine"]["host"], "xzgpu", "StormHunt frozen: machine.host")
    host_cores_hunt = manifest["machine"]["cpus"]
    eq(host_cores_hunt, 40, "StormHunt frozen: machine.cpus (eval.tex's \"40 cores\")")
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
    m.add("recSoakCommitHunt", manifest["git_commit"],
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: git_commit")
    m.add("recSoakDurationSHunt", tex_num(int(duration_s)),
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: config.duration_s")
    m.add("recSoakWriterLivesHunt", tex_num(writer_lives),
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: summary.writer_totals_all_lives.lives")
    m.add("recSoakRecoveriesHunt", tex_num(recoveries),
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: summary.recoveries (designed restarts; "
          "config.restart_every_s == 0.0, so none were scheduled)")
    m.add("recSoakWriterWithinLifeSlopeKBpsHunt", f"{writer_slope_1dp:.1f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_HUNT)}: writer_lives[0]."
          "slope_kb_per_s_least_squares, rounded to 1dp (single writer life, no restarts)")
    m.add("recSoakReaderWithinLifeSlopeMinKBpsHunt", f"{reader_min_1dp:.1f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_HUNT)}: min(readers[*].slope_kb_per_s_least_squares), "
          "8 readers, rounded to 1dp")
    m.add("recSoakReaderWithinLifeSlopeMaxKBpsHunt", f"{reader_max_1dp:.1f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_HUNT)}: max(readers[*].slope_kb_per_s_least_squares), "
          "rounded to 1dp")
    m.add("recSoakDigestEqualHunt", "true",
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: summary.digest_equal is the JSON literal "
          "true (text macro, not a number)")
    m.add("recSoakBatchesHunt", tex_num(total_batches),
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: summary.total_batches (the unscored "
          "end-of-run replay's own batch count)")
    m.add("recSoakVerifyHealthyHunt", "true",
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: summary.verify_healthy is the JSON literal "
          "true (text macro, not a number)")
    m.add("recSoakWriterErrorsTrueHunt", tex_num(total_writer_errors),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_CLASS_HUNT)}: total_writer_errors, "
          "cross-checked against manifest's own error_count and "
          "writer_totals_all_lives.errors")
    m.add("recSoakWriterErrorsClassHunt", "NotFoundError",
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_CLASS_HUNT)}: the sole key of "
          "writer_by_class (text macro, not a number)")
    m.add("recSoakReaderErrorsTrueHunt", tex_num(reader_errors_total),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_CLASS_HUNT)}: reader_errors_total, "
          "cross-checked against manifest's own summary.reader_errors_total")
    m.add("recSoakReaderOpErrorEventsHunt", tex_num(reader_op_error_events),
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_CLASS_HUNT)}: reader_op_error_events -- "
          f"cross-checked against {relpath(LONGEVITY_READER_OP_ERROR_HUNT)} being empty "
          "(0 bytes)")
    m.add("recSoakReaderOpErrorCaptureCommitHunt", capture_commit,
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_CLASS_HUNT)}: capture_code_commit -- D-088, "
          "the bounded reader-error-message capture, armed for the whole 6h run (text "
          "macro, not a number)")
    m.add("recSoakEdgeRowsHunt", tex_num(n_edge_versions),
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: summary.final_stats.n_edge_versions "
          "(the record carries this field directly; no compactions.jsonl fallback needed)")
    m.add("recSoakHostLoadMinHunt", f"{host_load_min:.2f}",
          f"{relpath(LONGEVITY_HOST_LOAD_HUNT)}: min(1-min load averages), the 7 samples "
          "at/before launched + config.duration_s (the run's own 6h window)")
    m.add("recSoakHostLoadMaxHunt", f"{host_load_max:.2f}",
          f"{relpath(LONGEVITY_HOST_LOAD_HUNT)}: max(1-min load averages), same window "
          "(excludes the one post-run verify/replay-step sample)")
    m.add("recSoakHostCoresHunt", tex_num(host_cores_hunt),
          f"{relpath(LONGEVITY_MANIFEST_HUNT)}: machine.cpus (machine.host == \"xzgpu\")")
    m.add("recSoakHuntPatternReproduced", "false",
          f"README.md P-STORM-HUNT section, quoted verbatim (its own clause (e) wording): "
          f"\"{outcome_text}\" -- the P-SOAK2 reader OSError/StateError storm "
          "(recSoakReaderErrorsTrueHunt = 0) was not reproduced within this 6h/1.40M-"
          "edge-row observation window; per the pre-registration's own clause (e), that "
          "negative result is not evidence the pattern is gone (text macro, not a number)")


def compute_longevity_soak_three(m: Macros) -> None:
    """Lane W2x: P-SOAK3, the 72h third soak (commit ``9e21a83``, pre-fix
    engine -- the ``gc_mid_delete`` restart-arming defect this run's
    harness carries is fixed later, at ``3267725``/``7b5d6ba``, both after
    ``9e21a83``) -- see benchmarks/longevity-v1/README.md's "Soak 3 (72 h)"
    section. Only 9 of the pre-registered 12 writer lives ran (known fact
    1, a consequence of that same pre-fix harness). Unlike P-SOAK2's
    monotonic, never-healing reader OSError/StateError storm, this run's
    D-088 bounded reader-error capture (17 first-onset ledger lines) plus
    a reconstruction from metrics.jsonl's own reader_errors_total counter
    (reader_op_error-3.json's healed_at_next_reopen_evidence) shows 662
    true active/healed episodes across the 8 readers x 2 classes, with
    every combination healing repeatedly. Root cause is explicitly not
    established here (README.md's Honest limits); P-SOAK4 re-measures the
    fixed engine. No verdict macro -- Gate E scoring is the coordinator's,
    not this script's.

    The writer-error exception class (100% NotFoundError per README.md
    (h)) is landed as ``recSoakWriterErrorsClassThree``, recomputed from
    writer_error_counts_by_class-3.json -- this soak's own per-life/
    per-class writer-error side-file, built read-only from xzgpu's
    longevity_ledger.jsonl (854 writer_op_error lines, all NotFoundError)
    split per life via each writer_progress-<life>.json, the same
    convention as P-SOAK2's writer_error_counts_by_life-2.json.
    """
    # --- whole-file sha256 checks against README.md's Soak 3 "Files added
    # here" table (the 5 files with an xzgpu twin) ---
    eq(sha256_file(LONGEVITY_MANIFEST_THREE), LONGEVITY_MANIFEST_THREE_SHA256,
       f"{relpath(LONGEVITY_MANIFEST_THREE)}: sha256 matches README.md's Soak 3 "
       "Files-added-here table")
    eq(sha256_file(LONGEVITY_COMPACTIONS_THREE), LONGEVITY_COMPACTIONS_THREE_SHA256,
       f"{relpath(LONGEVITY_COMPACTIONS_THREE)}: sha256 matches README.md's Soak 3 "
       "Files-added-here table")
    eq(sha256_file(LONGEVITY_RECOVERIES_THREE), LONGEVITY_RECOVERIES_THREE_SHA256,
       f"{relpath(LONGEVITY_RECOVERIES_THREE)}: sha256 matches README.md's Soak 3 "
       "Files-added-here table")
    eq(sha256_file(LONGEVITY_HOST_LOAD_THREE), LONGEVITY_HOST_LOAD_THREE_SHA256,
       f"{relpath(LONGEVITY_HOST_LOAD_THREE)}: sha256 matches README.md's Soak 3 "
       "Files-added-here table")
    eq(sha256_file(LONGEVITY_VERIFY_FULL_THREE), LONGEVITY_VERIFY_FULL_THREE_SHA256,
       f"{relpath(LONGEVITY_VERIFY_FULL_THREE)}: sha256 matches README.md's Soak 3 "
       "Files-added-here table")

    manifest = json.loads(LONGEVITY_MANIFEST_THREE.read_text(encoding="utf-8"))
    summary = manifest["summary"]
    config = manifest["config"]

    # --- commit, duration, wall clock ---
    eq(manifest["git_commit"], "9e21a83",
       "Soak3 frozen: measured commit (pre-fix engine, by design)")
    duration_s = config["duration_s"]
    eq(duration_s, 259_200.0, "Soak3 frozen: configured soak duration_s (72h)")
    hours = duration_s / 3600.0
    eq(hours, 72.0, "Soak3: config.duration_s / 3600 is exactly 72 hours")
    wall_s = summary["wall_s"]
    eq(wall_s, 331_364.8, "Soak3 frozen: summary.wall_s")
    wall_hours = wall_s / 3600.0
    eq(round(wall_hours, 2), 92.05,
       "Soak3: summary.wall_s / 3600, rounded to 2dp (includes the final verify(), the "
       "end-of-run replay/digest check, and the full-mode tgms check -- not just the "
       "configured 72h duration_s)")

    # --- writer lives: 9 ran, not the pre-registered 12 (known fact 1: the
    # gc_mid_delete restart-arming defect, fixed later at 3267725/7b5d6ba,
    # both postdating this run's 9e21a83 build -- compact() never called
    # gc() in that harness copy, so the designed-restart boundary for that
    # scenario never fired) ---
    writer_lives = summary["writer_totals_all_lives"]["lives"]
    eq(writer_lives, 9, "Soak3 frozen: summary.writer_totals_all_lives.lives")

    # --- 8 designed restarts (9 lives - 1), by recovery time ---
    recoveries_rows = load_jsonl(LONGEVITY_RECOVERIES_THREE)
    eq(len(recoveries_rows), 8, "Soak3 frozen: recoveries-3.jsonl row count")
    eq(len(recoveries_rows), summary["recoveries"],
       "Soak3: recoveries-3.jsonl row count matches summary.recoveries")
    require(all(r["kind"] == "designed" for r in recoveries_rows),
            "Soak3: every recovery row is the harness's own designed restart cycle")
    require(len(recoveries_rows) + 1 == writer_lives,
            "Soak3: 8 designed deaths give 9 lives")
    recovery_times = sorted(r["recovery_s"] for r in recoveries_rows)
    recovery_min_s = recovery_times[0]
    recovery_max_s = recovery_times[-1]
    eq(round(recovery_min_s, 3), 25.667, "Soak3 frozen: shortest recovery time, s")
    eq(round(recovery_max_s, 3), 96.352, "Soak3 frozen: longest recovery time, s")
    unexpected = summary["unexpected_writer_deaths"]
    eq(unexpected, 0, "Soak3 frozen: unexpected_writer_deaths")
    reader_restarts = summary["reader_restarts"]
    eq(reader_restarts, 0, "Soak3 frozen: summary.reader_restarts -- no reader ever died "
       "or was restarted")

    require(summary["verify_healthy"] is True,
            "Soak3: summary.verify_healthy is the JSON literal true")
    require(summary["digest_equal"] is True,
            "Soak3: summary.digest_equal is the JSON literal true -- confirmed by the "
            "19.6h end-of-run replay (known fact 4)")
    require(summary["replay_skipped"] is None,
            "Soak3: summary.replay_skipped is JSON null -- the replay was not skipped")

    total_batches = summary["total_batches"]
    eq(total_batches, 3_618_225,
       "Soak3 frozen: summary.total_batches (the end-of-run replay's own batch count)")
    replay_cadence = config["replay_compact_every"]
    eq(replay_cadence, 5000, "Soak3 frozen: config.replay_compact_every")

    # --- writer within-life RSS slopes, 9 lives ---
    rss_doc = json.loads(LONGEVITY_RSS_SLOPES_THREE.read_text(encoding="utf-8"))
    writer_life_rows = rss_doc["writer_lives"]
    eq(len(writer_life_rows), writer_lives,
       "Soak3: rss_slopes-3.json writer_lives row count matches "
       "summary.writer_totals_all_lives.lives")
    require(list(range(writer_lives)) == [row["life"] for row in writer_life_rows],
            "Soak3: rss_slopes-3.json writer_lives rows are ordered life 0..8")
    writer_life_slopes = [row["slope_kb_per_s_least_squares"] for row in writer_life_rows]
    frozen_bound_writer = rss_doc["frozen_bound_writer_kb_per_s"]
    eq(frozen_bound_writer, 50, "Soak3 frozen: rss_slopes-3.json frozen_bound_writer_kb_per_s")
    require(all(s <= frozen_bound_writer for s in writer_life_slopes),
            "Soak3: every writer life's within-life RSS slope clears the frozen 50 kB/s bound")
    writer_median_kb = round(statistics.median(writer_life_slopes), 3)
    writer_max_kb = round(max(writer_life_slopes), 3)
    eq(writer_median_kb, 16.630, "Soak3 frozen: writer within-life slope median, kB/s")
    eq(writer_max_kb, 28.834, "Soak3 frozen: writer within-life slope max, kB/s (life 6)")

    # --- reader within-life RSS slopes: 8 readers, reader_restarts=0 so
    # exactly one fitted segment each, over the full ~259,134-259,163s span ---
    reader_rows = rss_doc["readers"]
    eq(len(reader_rows), 8, "Soak3 frozen: rss_slopes-3.json readers row count")
    reader_slopes = [row["slope_kb_per_s_least_squares"] for row in reader_rows.values()]
    frozen_bound_reader = rss_doc["frozen_bound_reader_kb_per_s"]
    eq(frozen_bound_reader, 10, "Soak3 frozen: rss_slopes-3.json frozen_bound_reader_kb_per_s")
    require(all(s <= frozen_bound_reader for s in reader_slopes),
            "Soak3: every reader's within-life RSS slope clears the frozen 10 kB/s bound -- "
            "better than both P-SOAK2 (7.2-8.1) and P-STORM-HUNT's failing 11.4-12.5 kB/s")
    reader_min_kb = round(min(reader_slopes), 3)
    reader_max_kb = round(max(reader_slopes), 3)
    eq(reader_min_kb, 4.271, "Soak3 frozen: reader within-life slope min, kB/s")
    eq(reader_max_kb, 4.796, "Soak3 frozen: reader within-life slope max, kB/s")

    # --- writer errors: true total (854), cross-checked against the
    # manifest's own writer_totals_all_lives.errors; the exception class
    # (100% NotFoundError per README.md (h)) is now recomputed from
    # writer_error_counts_by_class-3.json, this lane's own per-life/
    # per-class writer-error side-file (built read-only from xzgpu's
    # longevity_ledger.jsonl, see that file's own "source"/"method"
    # fields) ---
    writer_errors_true = summary["writer_totals_all_lives"]["errors"]
    eq(writer_errors_true, 854, "Soak3 frozen: summary.writer_totals_all_lives.errors")

    writer_by_class_doc = json.loads(
        LONGEVITY_WRITER_ERRORS_BY_CLASS_THREE.read_text(encoding="utf-8"))
    eq(writer_by_class_doc["total_writer_errors"], writer_errors_true,
       "Soak3: writer_error_counts_by_class-3.json total_writer_errors matches "
       "summary.writer_totals_all_lives.errors")
    writer_by_class = writer_by_class_doc["writer_by_class"]
    eq(writer_by_class, {"NotFoundError": 854},
       "Soak3 frozen: writer_error_counts_by_class-3.json writer_by_class -- the sole "
       "exception class, and its full count")
    per_life_errors_three = writer_by_class_doc["per_life"]
    eq(len(per_life_errors_three), writer_lives,
       "Soak3: writer_error_counts_by_class-3.json per_life row count matches "
       "summary.writer_totals_all_lives.lives")
    per_life_errors_three_values = [
        per_life_errors_three[str(life)]["errors"] for life in range(writer_lives)]
    eq(per_life_errors_three_values, [174, 125, 86, 97, 132, 90, 62, 36, 52],
       "Soak3 frozen: writer_error_counts_by_class-3.json per-life error counts, lives 0-8, "
       "matches README.md (h)'s own reported figures")
    eq(sum(per_life_errors_three_values), writer_errors_true,
       "Soak3: writer_error_counts_by_class-3.json per-life errors sum to the true total")

    # --- reader errors: true total, split OSError/StateError, recomputed
    # from reader_op_error-3.json's per-reader per_reader_totals_by_class
    # (itself sourced from each reader's own reader-<idx>-progress.json
    # error_details[].count, per that file's own "method" field) and
    # cross-checked against the manifest's own reader_errors_total and
    # error_count arithmetic ---
    reader_op_error_doc = json.loads(LONGEVITY_READER_OP_ERROR_THREE.read_text(encoding="utf-8"))
    per_reader_totals = reader_op_error_doc["per_reader_totals_by_class"]
    eq(len(per_reader_totals), 8,
       "Soak3: reader_op_error-3.json per_reader_totals_by_class reader count")
    reader_oserror = sum(int(v["OSError"]) for v in per_reader_totals.values())
    reader_stateerror = sum(int(v["StateError"]) for v in per_reader_totals.values())
    eq(reader_oserror, 2_202_621_096, "Soak3 frozen: reader OSError total, summed over 8 readers")
    eq(reader_stateerror, 930_710,
       "Soak3 frozen: reader StateError total, summed over 8 readers")
    reader_errors_true = reader_oserror + reader_stateerror
    eq(reader_errors_true, summary["reader_errors_total"],
       "Soak3: recomputed OSError + StateError matches manifest's own reader_errors_total")
    eq(summary["error_count"], writer_errors_true + reader_errors_true + unexpected,
       "Soak3: summary.error_count == true writer errors + true reader errors + "
       "unexpected_writer_deaths")

    # --- reopens per reader (reader-reopen-every-s=300, so this is a
    # count of --reader-reopen-every-s cycles completed, not an error
    # count) ---
    reopens_per_reader = reader_op_error_doc["reopens_per_reader"]
    eq(len(reopens_per_reader), 8, "Soak3: reader_op_error-3.json reopens_per_reader count")
    reopens_min = min(reopens_per_reader.values())
    reopens_max = max(reopens_per_reader.values())
    eq(reopens_min, 777, "Soak3 frozen: reopens per reader, min")
    eq(reopens_max, 779, "Soak3 frozen: reopens per reader, max")

    # --- D-088 ledger first-onset count: 17, not soak2's per-reader-once
    # pattern (reader 1 has 3 onset lines here, every other reader has 2) --
    # cross-checked three ways within the file itself (top-level count,
    # verbatim ledger-line count, and sum of per-reader onset counts) ---
    n_ledger_events = reader_op_error_doc["n_ledger_reader_op_error_events"]
    eq(n_ledger_events, 17, "Soak3 frozen: reader_op_error-3.json n_ledger_reader_op_error_events")
    eq(n_ledger_events, len(reader_op_error_doc["ledger_events_verbatim"]),
       "Soak3: n_ledger_reader_op_error_events matches len(ledger_events_verbatim)")
    eq(n_ledger_events, sum(reader_op_error_doc["per_reader_episode_count"].values()),
       "Soak3: n_ledger_reader_op_error_events matches sum(per_reader_episode_count[*])")

    # --- reconstructed true episode count (662, NOT the ledger's 17 --
    # the ledger only records each (reader,class)'s first-ever onset, per
    # the file's own method field): sum of active_run_count across all 16
    # reader x class combinations in healed_at_next_reopen_evidence, each
    # an active/healed transition-count reconstructed from metrics.jsonl's
    # reader_errors_total counter. The longest single healed interval
    # across all 16 combinations (37,272.1s, reader3_StateError) is the
    # max of every healing_intervals[*].duration_s entry, not the
    # reader0_OSError single-interval figure (35,382.5s) that
    # README.md's (g) quotes only as one cross-check example. ---
    healed_evidence = reader_op_error_doc["healed_at_next_reopen_evidence"]
    eq(len(healed_evidence), 16,
       "Soak3: reader_op_error-3.json healed_at_next_reopen_evidence has 16 "
       "(8 readers x 2 classes) entries")
    require(all(v["healed_at_any_reopen"] is True for v in healed_evidence.values()),
            "Soak3: every (reader,class) combination healed at least once at some reopen -- "
            "materially different from soak2's monotonic, never-healing storm")
    true_episode_count = sum(v["active_run_count"] for v in healed_evidence.values())
    eq(true_episode_count, 662,
       "Soak3 frozen: reconstructed true episode count, summed over all 16 (reader,class) "
       "combinations (57-72 each) -- one to two orders of magnitude more than the "
       "ledger's 17 first-onset events")
    longest_healed_s = max(
        interval["duration_s"]
        for v in healed_evidence.values()
        for interval in v["healing_intervals"]
    )
    eq(round(longest_healed_s, 1), 37272.1,
       "Soak3 frozen: longest single healed interval across all 16 (reader,class) "
       "combinations, s (reader3_StateError)")

    # --- reader-onset edge-row counts, reconciled the same way as
    # soak2's reader_onset_rows-2.json, generalized from 4 to 9 writer
    # lives (see that file's own "method" field) ---
    onset_doc = json.loads(LONGEVITY_READER_ONSET_ROWS_THREE.read_text(encoding="utf-8"))
    earliest = onset_doc["earliest_reader_onset"]
    latest = onset_doc["latest_reader_onset"]
    eq(earliest["reader"], 1, "Soak3 frozen: earliest reader-error onset is reader 1 (OSError)")
    eq(latest["reader"], 2, "Soak3 frozen: latest reader-error onset is reader 2 (StateError)")
    onset_earliest_s = earliest["onset_t_plus_s"]
    onset_latest_s = latest["onset_t_plus_s"]
    eq(onset_earliest_s, 65655.8, "Soak3 frozen: earliest reader-error onset, s into the run")
    eq(onset_latest_s, 94769.4, "Soak3 frozen: latest reader-error onset, s into the run")
    onset_earliest_rows = earliest["last_compaction_before"]["edge_rows"]
    onset_latest_rows = latest["last_compaction_before"]["edge_rows"]
    eq(onset_earliest_rows, 1_950_613,
       "Soak3 frozen: edge_rows of the last compaction before the earliest reader onset")
    eq(onset_latest_rows, 2_356_519,
       "Soak3 frozen: edge_rows of the last compaction before the latest reader onset")
    require(onset_earliest_rows < onset_latest_rows,
            "Soak3: edge-row count grows from the earliest to the latest reader-error onset")

    # --- throughput: manifest's own first-hour/last-hour drift figures,
    # plus this record's independently-bucketed first-day/last-day
    # averages and their ratio (throughput-3.json) ---
    drift = summary["drift"]
    throughput_start = drift["throughput_first_hour_avg"]
    throughput_end = drift["throughput_last_hour_avg"]
    eq(throughput_start, 40.648, "Soak3 frozen: first-hour throughput, commits/s")
    eq(throughput_end, 11.014, "Soak3 frozen: last-hour throughput, commits/s")

    throughput_doc = json.loads(LONGEVITY_THROUGHPUT_THREE.read_text(encoding="utf-8"))
    eq(throughput_doc["manifest_drift"], drift,
       "Soak3: throughput-3.json's own manifest_drift copy matches the manifest's "
       "summary.drift exactly")
    first_day_avg = throughput_doc["first_day_avg_commits_per_s"]
    last_day_avg = throughput_doc["last_day_avg_commits_per_s_soak_window"]
    ratio_last_over_first = throughput_doc["ratio_last_over_first_soak_window"]
    eq(round(first_day_avg, 3), 19.713,
       "Soak3 frozen: first-day (hours 0-23) throughput average, commits/s")
    eq(round(last_day_avg, 3), 12.952,
       "Soak3 frozen: last-day (hours 48-71) throughput average, commits/s")
    eq(round(ratio_last_over_first, 3), 0.657,
       "Soak3 frozen: ratio, last-day average / first-day average")
    require(abs(ratio_last_over_first - last_day_avg / first_day_avg) < 1e-9,
            "Soak3: ratio_last_over_first_soak_window recomputes as last_day_avg / "
            "first_day_avg from this same file's own two averages")

    # --- full-mode tgms check of the final store: 0 believed-versions-
    # overlap findings (0 findings of any kind), generation cross-checked
    # against the manifest's own generation_final ---
    verify_text = LONGEVITY_VERIFY_FULL_THREE.read_text(encoding="utf-8")
    require("verdict: healthy" in verify_text,
            f"{relpath(LONGEVITY_VERIFY_FULL_THREE)}: verdict line reads healthy")
    problems_match = re.search(r"PROBLEMS \((\d+)\):", verify_text)
    overlap_count = int(problems_match.group(1)) if problems_match else 0
    eq(overlap_count, 0,
       f"{relpath(LONGEVITY_VERIFY_FULL_THREE)}: PROBLEMS count (0 -- no PROBLEMS section "
       "at all, consistent with the healthy verdict)")
    require("believed-versions-overlap" not in verify_text,
            f"{relpath(LONGEVITY_VERIFY_FULL_THREE)}: no believed-versions-overlap findings "
            "text present anywhere in the file")
    verify_generation_match = re.search(r"generation:\s*(\d+)", verify_text)
    require(verify_generation_match is not None,
            f"{relpath(LONGEVITY_VERIFY_FULL_THREE)}: carries a generation: <N> line")
    verify_generation = int(verify_generation_match.group(1)) if verify_generation_match else 0
    eq(verify_generation, 3_624_668, "Soak3 frozen: verify-full-3 generation")
    eq(verify_generation, int(summary["generation_final"]),
       "Soak3: full-verify's generation matches the manifest's own generation_final exactly")

    # --- co-tenant host load (1-min averages): known fact 3 states the
    # co-tenant ran throughout this run, including especially hard during
    # the post-soak replay wind-down -- unlike P-STORM-HUNT's log (which
    # this script windows to the run's own duration_s and excludes one
    # post-run sample), README.md's own stated 93-sample min/max for this
    # soak covers the WHOLE committed file including that wind-down
    # window, so this lane takes min/max over every sample rather than
    # windowing to config.duration_s. ---
    host_load_text = LONGEVITY_HOST_LOAD_THREE.read_text(encoding="utf-8")
    blocks = re.findall(
        r"=== HOST_LOAD (\S+) ===\n(.*?)(?=(?:=== HOST_LOAD |\Z))",
        host_load_text, re.DOTALL)
    eq(len(blocks), 93, "Soak3 frozen: host_load-3.log HOST_LOAD sample count")
    host_loads: list[float] = []
    for ts_str, block in blocks:
        load_match = re.search(r"load average:\s*([\d.]+),", block)
        require(load_match is not None,
                f"host_load-3.log: HOST_LOAD {ts_str} block has a load average line")
        if load_match:
            host_loads.append(float(load_match.group(1)))
    host_load_min = min(host_loads)
    host_load_max = max(host_loads)
    eq(round(host_load_min, 2), 1.21, "Soak3 frozen: host load min, 1-min avg")
    eq(round(host_load_max, 2), 188.27, "Soak3 frozen: host load max, 1-min avg")

    # --- emit macros ---
    m.add("recSoakCommitThree", manifest["git_commit"],
          f"{relpath(LONGEVITY_MANIFEST_THREE)}: git_commit")
    m.add("recSoakHoursThree", tex_num(int(hours)),
          f"{relpath(LONGEVITY_MANIFEST_THREE)}: config.duration_s / 3600")
    m.add("recSoakWallHoursThree", f"{wall_hours:.2f}",
          f"{relpath(LONGEVITY_MANIFEST_THREE)}: summary.wall_s / 3600 -- includes the "
          "final verify(), the end-of-run replay/digest check, and the full-mode "
          "tgms check this record adds, not just the configured 72h duration_s")
    m.add("recSoakWriterLivesThree", tex_num(writer_lives),
          f"{relpath(LONGEVITY_MANIFEST_THREE)}: summary.writer_totals_all_lives.lives -- "
          "9, not the pre-registered 12 (known fact 1)")
    m.add("recSoakRecoveriesThree", tex_num(len(recoveries_rows)),
          f"{relpath(LONGEVITY_RECOVERIES_THREE)}: row count, == summary.recoveries")
    m.add("recSoakRecoveryMinSThree", f"{recovery_min_s:.3f}",
          f"{relpath(LONGEVITY_RECOVERIES_THREE)}: min(recovery_s), the 8 designed "
          "restart cycles")
    m.add("recSoakRecoveryMaxSThree", f"{recovery_max_s:.3f}",
          f"{relpath(LONGEVITY_RECOVERIES_THREE)}: max(recovery_s)")
    m.add("recSoakUnexpectedRecoveriesThree", tex_num(unexpected),
          f"{relpath(LONGEVITY_MANIFEST_THREE)}: summary.unexpected_writer_deaths -- all 8 "
          "recoveries are the harness's own designed restart cycle (kind==\"designed\")")
    m.add("recSoakReaderDeathsThree", tex_num(reader_restarts),
          f"{relpath(LONGEVITY_MANIFEST_THREE)}: summary.reader_restarts -- no reader ever "
          "died or was restarted this run (unlike soak1's 2), no reader_restarts-3.jsonl "
          "file to cross-check a row count against")
    m.add("recSoakDigestEqualThree", "true",
          f"{relpath(LONGEVITY_MANIFEST_THREE)}: summary.digest_equal is the JSON literal "
          "true, confirmed by the 19.6h end-of-run replay (known fact 4)")
    m.add("recSoakBatchesThree", tex_num(total_batches),
          f"{relpath(LONGEVITY_MANIFEST_THREE)}: summary.total_batches (the end-of-run "
          "replay's own batch count)")
    m.add("recSoakReplayCadenceThree", tex_num(replay_cadence),
          f"{relpath(LONGEVITY_MANIFEST_THREE)}: config.replay_compact_every")
    m.add("recSoakWriterWithinLifeSlopeMedianKBpsThree", f"{writer_median_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_THREE)}: median(writer_lives[*]."
          "slope_kb_per_s_least_squares), 9 lives")
    m.add("recSoakWriterWithinLifeSlopeMaxKBpsThree", f"{writer_max_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_THREE)}: max(writer_lives[*]."
          "slope_kb_per_s_least_squares) (life 6)")
    m.add("recSoakReaderWithinLifeSlopeMinKBpsThree", f"{reader_min_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_THREE)}: min(readers[*].slope_kb_per_s_least_squares), "
          "8 readers (reader_restarts=0, one fitted segment each)")
    m.add("recSoakReaderWithinLifeSlopeMaxKBpsThree", f"{reader_max_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_THREE)}: max(readers[*].slope_kb_per_s_least_squares)")
    m.add("recSoakWriterErrorsTrueThree", tex_num(writer_errors_true),
          f"{relpath(LONGEVITY_MANIFEST_THREE)}: summary.writer_totals_all_lives.errors")
    m.add("recSoakReaderErrorsTrueThree", tex_num(reader_errors_true),
          f"{relpath(LONGEVITY_READER_OP_ERROR_THREE)}: OSError + StateError totals, "
          "cross-checked against manifest's own reader_errors_total")
    m.add("recSoakReaderErrorsOSErrorThree", tex_num(reader_oserror),
          f"{relpath(LONGEVITY_READER_OP_ERROR_THREE)}: sum(per_reader_totals_by_class[*]."
          "OSError), 8 readers")
    m.add("recSoakReaderErrorsStateErrorThree", tex_num(reader_stateerror),
          f"{relpath(LONGEVITY_READER_OP_ERROR_THREE)}: sum(per_reader_totals_by_class[*]."
          "StateError), 8 readers")
    m.add("recSoakReaderReopensMinThree", tex_num(reopens_min),
          f"{relpath(LONGEVITY_READER_OP_ERROR_THREE)}: min(reopens_per_reader[*]), "
          "--reader-reopen-every-s=300 cycles completed per reader")
    m.add("recSoakReaderReopensMaxThree", tex_num(reopens_max),
          f"{relpath(LONGEVITY_READER_OP_ERROR_THREE)}: max(reopens_per_reader[*])")
    m.add("recSoakReaderOpErrorEventsThree", tex_num(n_ledger_events),
          f"{relpath(LONGEVITY_READER_OP_ERROR_THREE)}: n_ledger_reader_op_error_events -- "
          "the D-088 bounded capture's first-onset ledger line count (17, not soak2's "
          "strictly-one-per-reader pattern: reader 1 has 3 onset lines here)")
    m.add("recSoakReaderErrorEpisodesThree", tex_num(true_episode_count),
          f"{relpath(LONGEVITY_READER_OP_ERROR_THREE)}: sum(healed_at_next_reopen_evidence"
          "[*].active_run_count), 16 (reader,class) combinations -- the reconstructed true "
          "active/healed episode count, NOT the ledger's 17 first-onset events")
    m.add("recSoakReaderErrorLongestHealedIntervalSThree", f"{longest_healed_s:.1f}",
          f"{relpath(LONGEVITY_READER_OP_ERROR_THREE)}: max(healed_at_next_reopen_evidence"
          "[*].healing_intervals[*].duration_s) over all 16 combinations (reader3_StateError)")
    m.add("recSoakReaderOnsetEarliestRowsThree", tex_num(onset_earliest_rows),
          f"{relpath(LONGEVITY_READER_ONSET_ROWS_THREE)}: earliest_reader_onset."
          "last_compaction_before.edge_rows -- the last compaction before reader 1's "
          "OSError onset_t_plus_s=65655.8s, reconciled via metrics.jsonl's "
          "compactions_total counter (same method as soak2's reader_onset_rows-2.json, "
          "generalized from 4 to 9 writer lives)")
    m.add("recSoakReaderOnsetLatestRowsThree", tex_num(onset_latest_rows),
          f"{relpath(LONGEVITY_READER_ONSET_ROWS_THREE)}: latest_reader_onset."
          "last_compaction_before.edge_rows -- the last compaction before reader 2's "
          "StateError onset_t_plus_s=94769.4s, same reconciliation method")
    m.add("recSoakReaderOnsetEarliestSThree", f"{onset_earliest_s:.1f}",
          f"{relpath(LONGEVITY_READER_ONSET_ROWS_THREE)}: earliest_reader_onset."
          "onset_t_plus_s, s into the run (RUN_STARTED 2026-09-19T00:46:42Z) -- reader 1, "
          "OSError, the minimum first-onset time across all 8 readers x 2 classes")
    m.add("recSoakReaderOnsetLatestSThree", f"{onset_latest_s:.1f}",
          f"{relpath(LONGEVITY_READER_ONSET_ROWS_THREE)}: latest_reader_onset.onset_t_plus_s, "
          "s into the run -- reader 2, StateError, the maximum first-onset time")
    m.add("recSoakThroughputStartThree", str(throughput_start),
          f"{relpath(LONGEVITY_MANIFEST_THREE)}: summary.drift.throughput_first_hour_avg, "
          "commits/s")
    m.add("recSoakThroughputEndThree", str(throughput_end),
          f"{relpath(LONGEVITY_MANIFEST_THREE)}: summary.drift.throughput_last_hour_avg, "
          "commits/s")
    m.add("recSoakThroughputFirstDayAvgThree", f"{first_day_avg:.3f}",
          f"{relpath(LONGEVITY_THROUGHPUT_THREE)}: first_day_avg_commits_per_s (hours 0-23), "
          "commits/s")
    m.add("recSoakThroughputLastDayAvgThree", f"{last_day_avg:.3f}",
          f"{relpath(LONGEVITY_THROUGHPUT_THREE)}: "
          "last_day_avg_commits_per_s_soak_window (hours 48-71, the last 24h of the "
          "pre-registered 72h window), commits/s")
    m.add("recSoakThroughputLastOverFirstDayRatioThree", f"{ratio_last_over_first:.3f}",
          f"{relpath(LONGEVITY_THROUGHPUT_THREE)}: ratio_last_over_first_soak_window, "
          "recomputed here as last_day_avg / first_day_avg from this same file")
    m.add("recSoakFullVerifyOverlapCountThree", tex_num(overlap_count),
          f"{relpath(LONGEVITY_VERIFY_FULL_THREE)}: PROBLEMS count (believed-versions-"
          "overlap class), 0 -- matching soak2's clean full-mode result")
    m.add("recSoakFullVerifyGenerationThree", tex_num(verify_generation),
          f"{relpath(LONGEVITY_VERIFY_FULL_THREE)}: generation, cross-checked against "
          "the manifest's own summary.generation_final")
    m.add("recSoakHostLoadMinThree", f"{host_load_min:.2f}",
          f"{relpath(LONGEVITY_HOST_LOAD_THREE)}: min(1-min load averages), all 93 samples "
          "(known fact 3: a co-tenant ran throughout this record's whole wall-clock window, "
          "including the post-soak replay wind-down -- unlike P-STORM-HUNT's log, this "
          "is not windowed to config.duration_s)")
    m.add("recSoakHostLoadMaxThree", f"{host_load_max:.2f}",
          f"{relpath(LONGEVITY_HOST_LOAD_THREE)}: max(1-min load averages), same (unwindowed) "
          "93-sample set")

    m.add("recSoakWriterErrorsClassThree", "NotFoundError",
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_CLASS_THREE)}: the sole exception class "
          "across every writer error in all 9 lives (text macro, not a number) -- "
          "cross-checked there against README.md (h), longevity_ledger.jsonl's 854 "
          "writer_op_error lines, and each writer_progress-<life>.json's own errors field")

    # --- D-088 mechanism constants: the reader's --reader-reopen-every-s
    # window and the writer's gc(keep_last=N) retention depth, the two
    # figures the D-088 paragraph's "300s reopen ... gc(keep_last=2) ...
    # unlinked about 48s later" arithmetic is built from. No existing
    # macro family named these (they are neither a Soak-Two/-Three
    # measurement nor a B7/storm/DAG one), so they land under a fresh
    # recD088... prefix, the same way recD160... did for D-160.
    # reader_reopen_every_s is read from this record's own config (soak2's
    # config carries the identical 300.0); generations_retained is
    # cross-checked against every row of compactions-3.jsonl's own gc
    # block rather than typed from scripts/longevity_run.py's
    # gc(keep_last=2) call, so it is on record rather than merely quoted
    # from the harness source. ---
    reopen_interval_s = config["reader_reopen_every_s"]
    eq(reopen_interval_s, 300.0, "Soak3 frozen: config.reader_reopen_every_s")
    compaction_rows_for_gc = load_jsonl(LONGEVITY_COMPACTIONS_THREE)
    generations_retained_values = {
        row["gc"]["generations_retained"] for row in compaction_rows_for_gc}
    eq(generations_retained_values, {2},
       f"Soak3: all {len(compaction_rows_for_gc)} compactions-3.jsonl rows report "
       "gc.generations_retained == 2 -- the gc(keep_last=2) depth, confirmed on "
       "record rather than read from scripts/longevity_run.py")
    m.add("recD088ReopenIntervalS", tex_num(int(reopen_interval_s)),
          f"{relpath(LONGEVITY_MANIFEST_THREE)}: config.reader_reopen_every_s -- the "
          "reader's --reader-reopen-every-s window (identical in soak2's config)")
    m.add("recD088GenerationsRetained", tex_num(next(iter(generations_retained_values))),
          f"{relpath(LONGEVITY_COMPACTIONS_THREE)}: gc.generations_retained, uniform "
          f"across all {len(compaction_rows_for_gc)} rows -- the writer's "
          "gc(keep_last=N) retention depth")


# --------------------------------------------------------------------------
# Lane W2z -- compaction cadence macros (soak 2 and soak 3), replacing the
# draft's unrecorded "~24s"/"48s" D-088 constants. compactions-2.jsonl and
# compactions-3.jsonl carry no `life` field, and their own t_start/t_end
# clock is monotonic-but-not-epoch with no documented per-life reset
# (README.md's "Added later" section for soak2, "known fact 2" for soak3)
# -- reconciling it against recoveries-*.jsonl's epoch t_death would need
# the same metrics.jsonl compactions_total-counter procedure
# reader_onset_rows-{2,3}.json used, which is not committed here. Instead
# _split_compaction_lives below uses the one life-boundary signal these
# files carry on their own: both soaks ran with
# --compact-every-batches 500, so every writer life's first compaction is
# always batches==500, and a drop in `batches` relative to the previous
# row marks a new life. The resulting life count is cross-checked against
# recoveries-{2,3}.jsonl's own row count + 1 below (3+1=4 for soak2,
# 8+1=9 for soak3) as an independent confirmation that the split lands on
# the same boundaries the harness's own restart cycle produced.
# --------------------------------------------------------------------------

def _split_compaction_lives(rows: list[dict], expected_lives: int, what: str) -> list[list[dict]]:
    """Split one compactions-*.jsonl's rows into per-writer-life groups by
    `batches` resetting to a smaller value than the previous row (see the
    section comment above for why this, not the file's own t_start/t_end
    clock or a `life` field, is the life-boundary signal used here)."""
    lives: list[list[dict]] = [[rows[0]]]
    for prev, row in zip(rows, rows[1:]):
        if row["batches"] < prev["batches"]:
            lives.append([row])
        else:
            lives[-1].append(row)
    eq(len(lives), expected_lives,
       f"{what}: batches-reset life-boundary count matches recoveries row count + 1")
    return lives


def compute_longevity_compaction_cadence(m: Macros) -> None:
    """Lane W2z: replaces the draft's unrecorded "~24s"/"48s" D-088
    compaction-cadence constants with macros computed from
    compactions-2.jsonl (soak2) and compactions-3.jsonl (soak3) -- the same
    committed per-compaction logs compute_longevity_soak_two and
    compute_longevity_soak_three already sha256-check and read (for the
    reader-onset edge-row reconciliation and the D-088
    gc.generations_retained constant respectively).

    The inter-compaction interval is the gap between one writer life's own
    consecutive t_start values, pooled across all of that soak's lives
    after splitting on _split_compaction_lives above -- never the gap
    that spans a restart, which would conflate ordinary cadence with
    recovery downtime. The compaction duration is t_end - t_start,
    pooled across every compaction regardless of life (a single
    compaction's own interval never spans a restart). The
    generation-validity window is gc.generations_retained * median
    interval: the store retains that many compaction generations before a
    generation becomes collectible, so that many median inter-compaction
    intervals is the window a generation stays valid for.
    """
    eq(sha256_file(LONGEVITY_COMPACTIONS_TWO), LONGEVITY_COMPACTIONS_TWO_SHA256,
       f"{relpath(LONGEVITY_COMPACTIONS_TWO)}: sha256 matches README.md's Files-added-here "
       "table (appended 2026-09-18)")
    eq(sha256_file(LONGEVITY_RECOVERIES_TWO), LONGEVITY_RECOVERIES_TWO_SHA256,
       f"{relpath(LONGEVITY_RECOVERIES_TWO)}: sha256 matches README.md's Files-added-here table")
    eq(sha256_file(LONGEVITY_COMPACTIONS_THREE), LONGEVITY_COMPACTIONS_THREE_SHA256,
       f"{relpath(LONGEVITY_COMPACTIONS_THREE)}: sha256 matches README.md's Soak 3 "
       "Files-added-here table")
    eq(sha256_file(LONGEVITY_RECOVERIES_THREE), LONGEVITY_RECOVERIES_THREE_SHA256,
       f"{relpath(LONGEVITY_RECOVERIES_THREE)}: sha256 matches README.md's Soak 3 "
       "Files-added-here table")

    rows_two = load_jsonl(LONGEVITY_COMPACTIONS_TWO)
    rows_three = load_jsonl(LONGEVITY_COMPACTIONS_THREE)
    recoveries_two = load_jsonl(LONGEVITY_RECOVERIES_TWO)
    recoveries_three = load_jsonl(LONGEVITY_RECOVERIES_THREE)
    eq(len(rows_two), 4215, f"{relpath(LONGEVITY_COMPACTIONS_TWO)}: row count")
    eq(len(rows_three), 8147, f"{relpath(LONGEVITY_COMPACTIONS_THREE)}: row count")
    eq(len(recoveries_two), 3, f"{relpath(LONGEVITY_RECOVERIES_TWO)}: row count")
    eq(len(recoveries_three), 8, f"{relpath(LONGEVITY_RECOVERIES_THREE)}: row count")

    def cadence(rows: list[dict], expected_lives: int, what: str) -> tuple[float, float, int]:
        lives = _split_compaction_lives(rows, expected_lives, what)
        intervals: list[float] = []
        durations: list[float] = []
        for life in lives:
            ts = [r["t_start"] for r in life]
            intervals.extend(ts[i] - ts[i - 1] for i in range(1, len(ts)))
            durations.extend(r["t_end"] - r["t_start"] for r in life)
        gens = {r["gc"]["generations_retained"] for r in rows}
        eq(gens, {2}, f"{what}: gc.generations_retained, uniform across every row")
        return statistics.median(intervals), statistics.median(durations), next(iter(gens))

    interval_two, duration_two, gens_two = cadence(
        rows_two, len(recoveries_two) + 1, relpath(LONGEVITY_COMPACTIONS_TWO))
    interval_three, duration_three, gens_three = cadence(
        rows_three, len(recoveries_three) + 1, relpath(LONGEVITY_COMPACTIONS_THREE))

    close(interval_two, 20.4605, 0.01, "Soak2 frozen: median inter-compaction interval, s")
    close(duration_two, 12.0557, 0.01, "Soak2 frozen: median compaction duration, s")
    close(interval_three, 31.5257, 0.01, "Soak3 frozen: median inter-compaction interval, s")
    close(duration_three, 20.0500, 0.01, "Soak3 frozen: median compaction duration, s")

    window_two = gens_two * interval_two
    window_three = gens_three * interval_three

    m.add("recSoakCompactionIntervalMedianSTwo", f"{interval_two:.1f}",
          f"{relpath(LONGEVITY_COMPACTIONS_TWO)}: median(t_start[i] - t_start[i-1]) within "
          "each of the 4 writer lives (life boundaries: batches resets to 500, "
          f"{relpath(LONGEVITY_RECOVERIES_TWO)}'s 3 rows -> 4 lives), pooled over all 4 "
          "lives' own intervals, s")
    m.add("recSoakCompactionDurationMedianSTwo", f"{duration_two:.1f}",
          f"{relpath(LONGEVITY_COMPACTIONS_TWO)}: median(t_end - t_start) over all "
          f"{len(rows_two)} compactions, s")
    m.add("recSoakGenerationWindowSTwo", f"{window_two:.1f}",
          f"gc.generations_retained ({gens_two}, {relpath(LONGEVITY_COMPACTIONS_TWO)}, "
          "recD088GenerationsRetained) * recSoakCompactionIntervalMedianSTwo -- the "
          "generation-validity window, s")
    m.add("recSoakCompactionIntervalMedianSThree", f"{interval_three:.1f}",
          f"{relpath(LONGEVITY_COMPACTIONS_THREE)}: median(t_start[i] - t_start[i-1]) within "
          "each of the 9 writer lives (life boundaries: batches resets to 500, "
          f"{relpath(LONGEVITY_RECOVERIES_THREE)}'s 8 rows -> 9 lives), pooled over all 9 "
          "lives' own intervals, s")
    m.add("recSoakCompactionDurationMedianSThree", f"{duration_three:.1f}",
          f"{relpath(LONGEVITY_COMPACTIONS_THREE)}: median(t_end - t_start) over all "
          f"{len(rows_three)} compactions, s")
    m.add("recSoakGenerationWindowSThree", f"{window_three:.1f}",
          f"gc.generations_retained ({gens_three}, {relpath(LONGEVITY_COMPACTIONS_THREE)}, "
          "recD088GenerationsRetained) * recSoakCompactionIntervalMedianSThree -- the "
          "generation-validity window, s")


# --------------------------------------------------------------------------
# Lane W2ab -- P-SOAK4, the 24h fourth soak (commit b6cdde0, the D-088
# reader-error fix -- see benchmarks/longevity-v1/README.md's "Soak 4
# (24 h, D-088-fixed engine)" section). Mirrors compute_longevity_soak_three
# above (whole-file sha256 gate against README.md's own "Files added here"
# table, then recompute every macro from the row-level files underneath)
# plus the W2z compaction-cadence function (_split_compaction_lives) for
# the cadence/generation-window pair. Headline result: 0 reader errors of
# any kind over the full 24h run (predictions (a)/(b)/(e)/(f) all met; no
# event for (f)), the first soak in this campaign past the D-088 fix.
# --------------------------------------------------------------------------

def compute_longevity_soak_four(m: Macros) -> None:
    """Lane W2ab: P-SOAK4, the 24h fourth soak, re-measuring the D-088
    reader-error fix (engine commit ``b6cdde0``) on the same
    ``synth-1m-native`` store/harness recipe as soaks 1-3. 4 of the
    pre-registered 4 writer lives ran (3 designed restarts,
    ``--restart-every 6h``), unlike soak3's 9-of-12 shortfall -- this
    harness vintage carries the ``gc_mid_delete`` restart-arming fix
    soak3 lacked. 0 reader errors of any class fired over the whole run
    (manifest's own ``reader_errors_total=0``, cross-checked three ways
    in ``reader_error_counts_by_class-4.json``): predictions (a) and (b)
    are met outright, (e) (``digest_equal``/0 reader deaths/0 unexpected
    recoveries/0 full-verify overlaps) is met, and (f) (the D-086
    torn-tail ``StateError`` class is not credited) has no event to
    credit or discredit, reported as such rather than as a pass/fail.
    The pin-attributable sub-clause of (c) stays explicitly NOT CHECKED
    here (README.md's Honest limits): no pre-fix (D-088-broken-engine)
    file-backed RSS series exists at a matching store size to difference
    against, and the PI ruled no control run -- stated as a limit, not
    guessed at.

    ``compactions-4.jsonl`` carries both the batches-resets-to-500
    life-boundary signal _split_compaction_lives uses AND its own ``life``
    field directly (unlike soaks 2/3) -- this lane uses the ``batches``
    split for consistency with the W2z cadence function, then asserts
    both land on the same groups as an independent confirmation. Its
    rows also carry epoch ``ts_start``/``ts_end`` directly (unlike soak3's
    host-``CLOCK_MONOTONIC``-only ``t_start``/``t_end``), so the
    compaction-stall window (``compaction_stall-4.json``) is directly
    epoch-comparable against reader ``query_p99_ms`` samples -- the first
    soak in this campaign where that row is computable at all.

    The full-mode check's own ``generation: 1897091`` is 341 generations
    ahead of the manifest's ``summary.generation_final=1896750.0``
    (README.md's "Full-mode tgms check" section): the ``generation``
    gauge in ``metrics.jsonl`` simply stopped being updated a few seconds
    before the writer's true final commit, not a store defect (confirmed
    there via the store's own ``native/manifests/`` directory, numbered up
    to ``00000000000001897091.json``). This lane lands the check's own
    value (the on-disk truth) as ``recSoakFullVerifyGenerationFour`` and
    documents the 341-generation gap with a require(), rather than
    asserting the two numbers equal -- they are not expected to be.
    """
    # --- whole-file sha256 checks against README.md's Soak 4 "Files added
    # here" table (the 6 files with an xzgpu twin) ---
    eq(sha256_file(LONGEVITY_MANIFEST_FOUR), LONGEVITY_MANIFEST_FOUR_SHA256,
       f"{relpath(LONGEVITY_MANIFEST_FOUR)}: sha256 matches README.md's Soak 4 "
       "Files-added-here table")
    eq(sha256_file(LONGEVITY_COMPACTIONS_FOUR), LONGEVITY_COMPACTIONS_FOUR_SHA256,
       f"{relpath(LONGEVITY_COMPACTIONS_FOUR)}: sha256 matches README.md's Soak 4 "
       "Files-added-here table")
    eq(sha256_file(LONGEVITY_RECOVERIES_FOUR), LONGEVITY_RECOVERIES_FOUR_SHA256,
       f"{relpath(LONGEVITY_RECOVERIES_FOUR)}: sha256 matches README.md's Soak 4 "
       "Files-added-here table")
    eq(sha256_file(LONGEVITY_HOST_LOAD_FOUR), LONGEVITY_HOST_LOAD_FOUR_SHA256,
       f"{relpath(LONGEVITY_HOST_LOAD_FOUR)}: sha256 matches README.md's Soak 4 "
       "Files-added-here table")
    eq(sha256_file(LONGEVITY_VERIFY_FULL_FOUR), LONGEVITY_VERIFY_FULL_FOUR_SHA256,
       f"{relpath(LONGEVITY_VERIFY_FULL_FOUR)}: sha256 matches README.md's Soak 4 "
       "Files-added-here table")
    eq(sha256_file(LONGEVITY_RSS_COMPOSITION_FOUR), LONGEVITY_RSS_COMPOSITION_FOUR_SHA256,
       f"{relpath(LONGEVITY_RSS_COMPOSITION_FOUR)}: sha256 matches README.md's Soak 4 "
       "Files-added-here table")

    manifest = json.loads(LONGEVITY_MANIFEST_FOUR.read_text(encoding="utf-8"))
    summary = manifest["summary"]
    config = manifest["config"]

    # --- commit, duration, wall clock ---
    eq(manifest["git_commit"], "b6cdde0",
       "Soak4 frozen: measured commit (the D-088 reader-error fix)")
    duration_s = config["duration_s"]
    eq(duration_s, 86_400.0, "Soak4 frozen: configured soak duration_s (24h)")
    hours = duration_s / 3600.0
    eq(hours, 24.0, "Soak4: config.duration_s / 3600 is exactly 24 hours")
    wall_s = summary["wall_s"]
    eq(wall_s, 102_498.0, "Soak4 frozen: summary.wall_s")
    wall_hours = wall_s / 3600.0
    eq(round(wall_hours, 2), 28.47,
       "Soak4: summary.wall_s / 3600, rounded to 2dp (includes the final verify(), the "
       "end-of-run replay/digest check, and the full-mode tgms check -- not just the "
       "configured 24h duration_s)")

    # --- writer lives: 4 of the pre-registered 4 ran (unlike soak3's
    # 9-of-12 shortfall -- this harness vintage carries the
    # gc_mid_delete restart-arming fix soak3 lacked) ---
    writer_lives = summary["writer_totals_all_lives"]["lives"]
    eq(writer_lives, 4, "Soak4 frozen: summary.writer_totals_all_lives.lives")

    # --- 3 designed restarts (4 lives - 1), by recovery time ---
    recoveries_rows = load_jsonl(LONGEVITY_RECOVERIES_FOUR)
    eq(len(recoveries_rows), 3, "Soak4 frozen: recoveries-4.jsonl row count")
    eq(len(recoveries_rows), summary["recoveries"],
       "Soak4: recoveries-4.jsonl row count matches summary.recoveries")
    require(all(r["kind"] == "designed" for r in recoveries_rows),
            "Soak4: every recovery row is the harness's own designed restart cycle")
    require(len(recoveries_rows) + 1 == writer_lives,
            "Soak4: 3 designed deaths give 4 lives")
    recovery_times = sorted(r["recovery_s"] for r in recoveries_rows)
    recovery_min_s = recovery_times[0]
    recovery_max_s = recovery_times[-1]
    eq(round(recovery_min_s, 3), 17.636, "Soak4 frozen: shortest recovery time, s")
    eq(round(recovery_max_s, 3), 47.114, "Soak4 frozen: longest recovery time, s")
    unexpected = summary["unexpected_writer_deaths"]
    eq(unexpected, 0, "Soak4 frozen: unexpected_writer_deaths")
    reader_restarts = summary["reader_restarts"]
    eq(reader_restarts, 0, "Soak4 frozen: summary.reader_restarts -- no reader ever died "
       "or was restarted")

    require(summary["verify_healthy"] is True,
            "Soak4: summary.verify_healthy is the JSON literal true")
    require(summary["digest_equal"] is True,
            "Soak4: summary.digest_equal is the JSON literal true")
    require(summary["replay_skipped"] is None,
            "Soak4: summary.replay_skipped is JSON null -- the replay was not skipped")

    total_batches = summary["total_batches"]
    eq(total_batches, 1_894_067,
       "Soak4 frozen: summary.total_batches (the end-of-run replay's own batch count)")
    replay_cadence = config["replay_compact_every"]
    eq(replay_cadence, 5000, "Soak4 frozen: config.replay_compact_every")

    # --- writer within-life RSS slopes, 4 lives ---
    rss_doc = json.loads(LONGEVITY_RSS_SLOPES_FOUR.read_text(encoding="utf-8"))
    writer_life_rows = rss_doc["writer_lives"]
    eq(len(writer_life_rows), writer_lives,
       "Soak4: rss_slopes-4.json writer_lives row count matches "
       "summary.writer_totals_all_lives.lives")
    require(list(range(writer_lives)) == [row["life"] for row in writer_life_rows],
            "Soak4: rss_slopes-4.json writer_lives rows are ordered life 0..3")
    writer_life_slopes = [row["slope_kb_per_s_least_squares"] for row in writer_life_rows]
    frozen_bound_writer = rss_doc["frozen_bound_writer_kb_per_s"]
    eq(frozen_bound_writer, 50, "Soak4 frozen: rss_slopes-4.json frozen_bound_writer_kb_per_s")
    require(all(s <= frozen_bound_writer for s in writer_life_slopes),
            "Soak4: every writer life's within-life RSS slope clears the frozen 50 kB/s "
            "bound (prediction (d))")
    writer_median_kb = round(statistics.median(writer_life_slopes), 3)
    writer_max_kb = round(max(writer_life_slopes), 3)
    eq(writer_median_kb, 22.085, "Soak4 frozen: writer within-life slope median, kB/s")
    eq(writer_max_kb, 37.880, "Soak4 frozen: writer within-life slope max, kB/s (life 0)")

    # --- reader within-life RSS slopes (harness series): 8 readers,
    # reader_restarts=0 so one fitted segment each, over the full
    # ~86,334.4s (~24h) span -- the primary instrument for prediction
    # (c)'s <=10 kB/s bound ---
    reader_harness_rows = rss_doc["readers_harness_series"]
    eq(len(reader_harness_rows), 8, "Soak4 frozen: rss_slopes-4.json readers_harness_series "
       "row count")
    reader_harness_slopes = [row["slope_kb_per_s_least_squares"]
                              for row in reader_harness_rows.values()]
    frozen_bound_reader = rss_doc["frozen_bound_reader_kb_per_s"]
    eq(frozen_bound_reader, 10, "Soak4 frozen: rss_slopes-4.json frozen_bound_reader_kb_per_s")
    require(all(s <= frozen_bound_reader for s in reader_harness_slopes),
            "Soak4: every reader's within-life (harness-series) RSS slope clears the "
            "frozen 10 kB/s bound (prediction (c))")
    reader_min_kb = round(min(reader_harness_slopes), 3)
    reader_max_kb = round(max(reader_harness_slopes), 3)
    eq(reader_min_kb, 7.957, "Soak4 frozen: reader within-life (harness-series) slope min, "
       "kB/s, full ~86,334.4s (~24h) run")
    eq(reader_max_kb, 8.399, "Soak4 frozen: reader within-life (harness-series) slope max, "
       "kB/s, same full-run window")

    # --- reader RSS slopes (sampler/composition series): the coordinator's
    # independent /proc/<pid>/status sampler, over its own 17.5h window
    # (211 samples/reader, starting ~6.5h into the run -- NOT the full
    # 24h the harness-series fit above covers). RssAnon/RssFile/VmSize
    # min/max over the 8 readers; the pin-attributable sub-clause of (c)
    # is NOT CHECKED here (no pre-fix file-backed series to difference
    # against -- stated as a limit in rss_doc's own pin_attribution_limit
    # field, not computed as a number). VmRSS itself is not landed as a
    # macro (no paper claim cites it directly; RssAnon+RssFile sum to it). ---
    reader_comp_rows = rss_doc["readers_composition_series"]
    eq(len(reader_comp_rows), 8, "Soak4 frozen: rss_slopes-4.json readers_composition_series "
       "row count")
    comp_spans = {row["span_s"] for row in reader_comp_rows.values()}
    eq(comp_spans, {63_028.0},
       "Soak4 frozen: readers_composition_series span_s is uniform across all 8 readers "
       "-- the sampler's own 17.5h window (63,028s), not the full 24h run")
    rssanon_slopes = [row["RssAnon_kb"]["slope_kb_per_s"] for row in reader_comp_rows.values()]
    rssfile_slopes = [row["RssFile_kb"]["slope_kb_per_s"] for row in reader_comp_rows.values()]
    vmsize_slopes = [row["VmSize_kb"]["slope_kb_per_s"] for row in reader_comp_rows.values()]
    rssanon_min_kb = round(min(rssanon_slopes), 3)
    rssanon_max_kb = round(max(rssanon_slopes), 3)
    rssfile_min_kb = round(min(rssfile_slopes), 3)
    rssfile_max_kb = round(max(rssfile_slopes), 3)
    vmsize_min_kb = round(min(vmsize_slopes), 3)
    vmsize_max_kb = round(max(vmsize_slopes), 3)
    eq(rssanon_min_kb, 4.811, "Soak4 frozen: sampler RssAnon slope min, kB/s, 17.5h window")
    eq(rssanon_max_kb, 5.413, "Soak4 frozen: sampler RssAnon slope max, kB/s, 17.5h window")
    eq(rssfile_min_kb, 1.034, "Soak4 frozen: sampler RssFile slope min, kB/s, 17.5h window -- "
       "a narrow band regardless of reader, consistent with store-size-driven file-backed "
       "page residency")
    eq(rssfile_max_kb, 1.056, "Soak4 frozen: sampler RssFile slope max, kB/s, 17.5h window")
    eq(vmsize_min_kb, 6.618, "Soak4 frozen: sampler VmSize slope min, kB/s, 17.5h window")
    eq(vmsize_max_kb, 7.170, "Soak4 frozen: sampler VmSize slope max, kB/s, 17.5h window")

    # --- writer errors: true total (595), cross-checked against the
    # manifest's own writer_totals_all_lives.errors; the exception class
    # (100% NotFoundError) is recomputed from
    # writer_error_counts_by_class-4.json, this soak's own per-life/
    # per-class writer-error side-file, built read-only from xzgpu's
    # longevity_ledger.jsonl, same convention as soak3's ---
    writer_errors_true = summary["writer_totals_all_lives"]["errors"]
    eq(writer_errors_true, 595, "Soak4 frozen: summary.writer_totals_all_lives.errors")

    writer_by_class_doc = json.loads(
        LONGEVITY_WRITER_ERRORS_BY_CLASS_FOUR.read_text(encoding="utf-8"))
    eq(writer_by_class_doc["total_writer_errors"], writer_errors_true,
       "Soak4: writer_error_counts_by_class-4.json total_writer_errors matches "
       "summary.writer_totals_all_lives.errors")
    writer_by_class = writer_by_class_doc["writer_by_class"]
    eq(writer_by_class, {"NotFoundError": 595},
       "Soak4 frozen: writer_error_counts_by_class-4.json writer_by_class -- the sole "
       "exception class, and its full count")
    per_life_errors_four = writer_by_class_doc["per_life"]
    eq(len(per_life_errors_four), writer_lives,
       "Soak4: writer_error_counts_by_class-4.json per_life row count matches "
       "summary.writer_totals_all_lives.lives")
    per_life_errors_four_values = [
        per_life_errors_four[str(life)]["errors"] for life in range(writer_lives)]
    eq(per_life_errors_four_values, [218, 166, 126, 85],
       "Soak4 frozen: writer_error_counts_by_class-4.json per-life error counts, lives 0-3, "
       "matches README.md's per-life table")
    eq(sum(per_life_errors_four_values), writer_errors_true,
       "Soak4: writer_error_counts_by_class-4.json per-life errors sum to the true total")

    # --- reader errors: 0 of any class (prediction (a); the D-086
    # torn-tail StateError class of prediction (f) is included in this
    # zero, reported separately below as "no event", not a pass/fail).
    # Cross-checked against the manifest's own reader_errors_total and
    # against reader_reopen_on_enoent-4.json's independent 0. ---
    reader_errors_doc = json.loads(
        LONGEVITY_READER_ERRORS_BY_CLASS_FOUR.read_text(encoding="utf-8"))
    require(reader_errors_doc["all_zero"] is True,
            "Soak4: reader_error_counts_by_class-4.json all_zero is the JSON literal true")
    reader_errors_true = reader_errors_doc["reader_errors_total"]
    eq(reader_errors_true, 0, "Soak4 frozen: reader_error_counts_by_class-4.json "
       "reader_errors_total -- 0 reader errors of any class over the whole 24h run")
    eq(reader_errors_true, summary["reader_errors_total"],
       "Soak4: reader_error_counts_by_class-4.json reader_errors_total matches the "
       "manifest's own summary.reader_errors_total")
    eq(reader_errors_doc["by_class_total"], {},
       "Soak4 frozen: reader_error_counts_by_class-4.json by_class_total -- empty, no "
       "OSError/StateError/any class fired (prediction (f): the D-086 torn-tail "
       "StateError class has no event to credit or discredit)")
    reader_oserror = 0
    reader_stateerror = 0
    eq(summary["error_count"], writer_errors_true + reader_errors_true + unexpected,
       "Soak4: summary.error_count == true writer errors + true reader errors + "
       "unexpected_writer_deaths")

    # --- reopens per reader (reader-reopen-every-s=300) and
    # reopen-on-ENOENT (prediction (b)): both independent sources
    # (metrics.jsonl's own counter and each reader's progress-file field)
    # agree exactly at 0 for every reader ---
    reopen_doc = json.loads(LONGEVITY_READER_REOPEN_ON_ENOENT_FOUR.read_text(encoding="utf-8"))
    require(reopen_doc["all_zero"] is True,
            "Soak4: reader_reopen_on_enoent-4.json all_zero is the JSON literal true")
    eq(reopen_doc["manifest_reader_reopen_on_enoent_total"], 0,
       "Soak4 frozen: reader_reopen_on_enoent-4.json manifest_reader_reopen_on_enoent_total")
    eq(reopen_doc["manifest_reader_reopen_on_enoent_total"],
       summary["reader_reopen_on_enoent_total"],
       "Soak4: reader_reopen_on_enoent-4.json manifest_reader_reopen_on_enoent_total "
       "matches the manifest's own summary.reader_reopen_on_enoent_total")
    per_reader_reopen = reopen_doc["per_reader"]
    eq(len(per_reader_reopen), 8, "Soak4: reader_reopen_on_enoent-4.json per_reader count")
    reopens_per_reader = [v["reopens_total_progress_file"] for v in per_reader_reopen.values()]
    enoent_per_reader_metrics = [v["reopen_on_enoent_total_metrics_counter"]
                                  for v in per_reader_reopen.values()]
    enoent_per_reader_progress = [v["reopen_on_enoent_total_progress_file"]
                                   for v in per_reader_reopen.values()]
    reopens_min = min(reopens_per_reader)
    reopens_max = max(reopens_per_reader)
    eq(reopens_min, 272, "Soak4 frozen: reopens per reader, min -- uniform across all 8 "
       "readers over the ~86,334s run")
    eq(reopens_max, 272, "Soak4 frozen: reopens per reader, max")
    enoent_max = max(enoent_per_reader_progress)
    eq(enoent_max, 0, "Soak4 frozen: reader_reopen_on_enoent-4.json reopen_on_enoent_total, "
       "max over all 8 readers (progress-file source)")
    eq(max(enoent_per_reader_metrics), 0.0,
       "Soak4: metrics.jsonl's own reopen_on_enoent_total counter agrees exactly (0) with "
       "the progress-file source, the second of the two independent sources this file reads")

    # --- throughput: manifest's own first-hour/last-hour drift figures
    # (prediction (d)'s throughput clause), plus this record's
    # independently-bucketed hour-0/hour-23 averages and their ratio
    # (throughput-4.json) ---
    drift = summary["drift"]
    throughput_start = drift["throughput_first_hour_avg"]
    throughput_end = drift["throughput_last_hour_avg"]
    eq(throughput_start, 40.552, "Soak4 frozen: first-hour throughput, commits/s")
    eq(throughput_end, 19.164, "Soak4 frozen: last-hour throughput, commits/s")

    throughput_doc = json.loads(LONGEVITY_THROUGHPUT_FOUR.read_text(encoding="utf-8"))
    eq(throughput_doc["manifest_drift"], drift,
       "Soak4: throughput-4.json's own manifest_drift copy matches the manifest's "
       "summary.drift exactly")
    this_record_first_hour = throughput_doc["this_record_first_hour_avg_commits_per_s"]
    this_record_last_hour = throughput_doc["this_record_last_hour_avg_commits_per_s"]
    ratio_last_over_first = throughput_doc["ratio_last_over_first"]
    eq(round(this_record_first_hour, 3), 40.543,
       "Soak4 frozen: this record's own independently hour-0-bucketed average, commits/s")
    eq(round(this_record_last_hour, 3), 19.246,
       "Soak4 frozen: this record's own independently hour-23-bucketed average, commits/s")
    eq(round(ratio_last_over_first, 3), 0.475,
       "Soak4 frozen: ratio, this record's own last-hour average / first-hour average")
    require(abs(ratio_last_over_first - this_record_last_hour / this_record_first_hour) < 1e-9,
            "Soak4: ratio_last_over_first recomputes as this_record_last_hour_avg / "
            "this_record_first_hour_avg from this same file's own two averages")

    # --- full-mode tgms check of the final store: 0 believed-versions-
    # overlap findings (0 findings of any kind, prediction (e)). The
    # check's own generation (1,897,091) is 341 ahead of the manifest's
    # generation_final (1,896,750) -- README.md's "Full-mode tgms check"
    # section explains this as the generation gauge freezing a few
    # seconds before the writer's true final commit, confirmed via the
    # store's own native/manifests/ directory, not a store defect. This
    # is documented with a require() on the 341-generation gap, not
    # asserted equal -- the two numbers are not expected to match. ---
    verify_text = LONGEVITY_VERIFY_FULL_FOUR.read_text(encoding="utf-8")
    require("verdict: healthy" in verify_text,
            f"{relpath(LONGEVITY_VERIFY_FULL_FOUR)}: verdict line reads healthy")
    problems_match = re.search(r"PROBLEMS \((\d+)\):", verify_text)
    overlap_count = int(problems_match.group(1)) if problems_match else 0
    eq(overlap_count, 0,
       f"{relpath(LONGEVITY_VERIFY_FULL_FOUR)}: PROBLEMS count (0 -- no PROBLEMS section "
       "at all, consistent with the healthy verdict)")
    require("believed-versions-overlap" not in verify_text,
            f"{relpath(LONGEVITY_VERIFY_FULL_FOUR)}: no believed-versions-overlap findings "
            "text present anywhere in the file")
    verify_generation_match = re.search(r"generation:\s*(\d+)", verify_text)
    require(verify_generation_match is not None,
            f"{relpath(LONGEVITY_VERIFY_FULL_FOUR)}: carries a generation: <N> line")
    verify_generation = int(verify_generation_match.group(1)) if verify_generation_match else 0
    eq(verify_generation, 1_897_091, "Soak4 frozen: verify-full-4 generation")
    manifest_generation_final = int(summary["generation_final"])
    eq(manifest_generation_final, 1_896_750,
       "Soak4 frozen: manifest's own summary.generation_final")
    generation_gap = verify_generation - manifest_generation_final
    eq(generation_gap, 341,
       "Soak4: full-verify's generation is 341 ahead of the manifest's own "
       "generation_final -- the generation gauge in metrics.jsonl froze a few seconds "
       "before the writer's true final commit (README.md's Full-mode tgms check "
       "section), not a store defect; the check's own value (the on-disk truth) is what "
       "this lane lands, not the manifest's last-sampled-gauge-value summary field")

    # --- compaction stall window (prediction (e) has no stall clause,
    # but the window computation documents that every reader p99 sample
    # in the run is correctly windowed against compactions-4.jsonl's own
    # epoch ts_start/ts_end -- the first soak in this campaign where this
    # row is computable at all) ---
    stall_doc = json.loads(LONGEVITY_COMPACTION_STALL_FOUR.read_text(encoding="utf-8"))
    require(summary["compaction_stall_computable"] is True,
            "Soak4: manifest's own summary.compaction_stall_computable is the JSON "
            "literal true")
    stall_max = stall_doc["this_record_max_reader_p99_ms"]
    eq(stall_max, 222.871, "Soak4 frozen: compaction_stall-4.json this_record_max_reader_p99_ms")
    eq(stall_max, summary["compaction_stall_max_reader_p99_ms"],
       "Soak4: compaction_stall-4.json this_record_max_reader_p99_ms matches the "
       "manifest's own summary.compaction_stall_max_reader_p99_ms exactly")
    eq(stall_doc["n_windows_empty"], 0,
       "Soak4 frozen: compaction_stall-4.json n_windows_empty -- every one of the "
       f"{stall_doc['n_compactions_total']} compaction windows had >=1 reader p99 sample")

    # --- co-tenant host load (1-min averages), 33 hourly samples ---
    host_load_text = LONGEVITY_HOST_LOAD_FOUR.read_text(encoding="utf-8")
    blocks = re.findall(
        r"=== HOST_LOAD (\S+) ===\n(.*?)(?=(?:=== HOST_LOAD |\Z))",
        host_load_text, re.DOTALL)
    eq(len(blocks), 33, "Soak4 frozen: host_load-4.log HOST_LOAD sample count")
    host_loads: list[float] = []
    for ts_str, block in blocks:
        load_match = re.search(r"load average:\s*([\d.]+),", block)
        require(load_match is not None,
                f"host_load-4.log: HOST_LOAD {ts_str} block has a load average line")
        if load_match:
            host_loads.append(float(load_match.group(1)))
    host_load_min = min(host_loads)
    host_load_max = max(host_loads)
    eq(round(host_load_min, 2), 0.09, "Soak4 frozen: host load min, 1-min avg")
    eq(round(host_load_max, 2), 12.48, "Soak4 frozen: host load max, 1-min avg")

    # --- final edge-version count (manifest's own final_stats, the
    # store-size figure predictions (a)/(f) measure past the soak-2
    # onset band 2,235,044-2,450,815 rows against) ---
    final_edge_rows = summary["final_stats"]["n_edge_versions"]
    eq(final_edge_rows, 2_600_501,
       "Soak4 frozen: summary.final_stats.n_edge_versions")

    # --- emit macros ---
    m.add("recSoakCommitFour", manifest["git_commit"],
          f"{relpath(LONGEVITY_MANIFEST_FOUR)}: git_commit")
    m.add("recSoakHoursFour", tex_num(int(hours)),
          f"{relpath(LONGEVITY_MANIFEST_FOUR)}: config.duration_s / 3600")
    m.add("recSoakWallHoursFour", f"{wall_hours:.2f}",
          f"{relpath(LONGEVITY_MANIFEST_FOUR)}: summary.wall_s / 3600 -- includes the "
          "final verify(), the end-of-run replay/digest check, and the full-mode "
          "tgms check this record adds, not just the configured 24h duration_s")
    m.add("recSoakWriterLivesFour", tex_num(writer_lives),
          f"{relpath(LONGEVITY_MANIFEST_FOUR)}: summary.writer_totals_all_lives.lives -- "
          "4 of the pre-registered 4, unlike soak3's 9-of-12 shortfall")
    m.add("recSoakRecoveriesFour", tex_num(len(recoveries_rows)),
          f"{relpath(LONGEVITY_RECOVERIES_FOUR)}: row count, == summary.recoveries")
    m.add("recSoakRecoveryMinSFour", f"{recovery_min_s:.3f}",
          f"{relpath(LONGEVITY_RECOVERIES_FOUR)}: min(recovery_s), the 3 designed "
          "restart cycles")
    m.add("recSoakRecoveryMaxSFour", f"{recovery_max_s:.3f}",
          f"{relpath(LONGEVITY_RECOVERIES_FOUR)}: max(recovery_s)")
    m.add("recSoakUnexpectedRecoveriesFour", tex_num(unexpected),
          f"{relpath(LONGEVITY_MANIFEST_FOUR)}: summary.unexpected_writer_deaths -- all 3 "
          "recoveries are the harness's own designed restart cycle (kind==\"designed\")")
    m.add("recSoakReaderDeathsFour", tex_num(reader_restarts),
          f"{relpath(LONGEVITY_MANIFEST_FOUR)}: summary.reader_restarts -- no reader ever "
          "died or was restarted this run")
    m.add("recSoakDigestEqualFour", "true",
          f"{relpath(LONGEVITY_MANIFEST_FOUR)}: summary.digest_equal is the JSON literal "
          "true")
    m.add("recSoakBatchesFour", tex_num(total_batches),
          f"{relpath(LONGEVITY_MANIFEST_FOUR)}: summary.total_batches (the end-of-run "
          "replay's own batch count)")
    m.add("recSoakReplayCadenceFour", tex_num(replay_cadence),
          f"{relpath(LONGEVITY_MANIFEST_FOUR)}: config.replay_compact_every")
    m.add("recSoakWriterWithinLifeSlopeMedianKBpsFour", f"{writer_median_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_FOUR)}: median(writer_lives[*]."
          "slope_kb_per_s_least_squares), 4 lives")
    m.add("recSoakWriterWithinLifeSlopeMaxKBpsFour", f"{writer_max_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_FOUR)}: max(writer_lives[*]."
          "slope_kb_per_s_least_squares) (life 0)")
    m.add("recSoakReaderWithinLifeSlopeMinKBpsFour", f"{reader_min_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_FOUR)}: min(readers_harness_series[*]."
          "slope_kb_per_s_least_squares), 8 readers, full ~86,334.4s (~24h) run "
          "(reader_restarts=0, one fitted segment each)")
    m.add("recSoakReaderWithinLifeSlopeMaxKBpsFour", f"{reader_max_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_FOUR)}: max(readers_harness_series[*]."
          "slope_kb_per_s_least_squares), same full-run window")
    m.add("recSoakReaderRssAnonSlopeMinKBpsFour", f"{rssanon_min_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_FOUR)}: min(readers_composition_series[*]."
          "RssAnon_kb.slope_kb_per_s), 8 readers, the sampler's own 17.5h window "
          "(63,028s, starting ~6.5h into the run) -- not the full 24h")
    m.add("recSoakReaderRssAnonSlopeMaxKBpsFour", f"{rssanon_max_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_FOUR)}: max(readers_composition_series[*]."
          "RssAnon_kb.slope_kb_per_s), same 17.5h sampler window")
    m.add("recSoakReaderRssFileSlopeMinKBpsFour", f"{rssfile_min_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_FOUR)}: min(readers_composition_series[*]."
          "RssFile_kb.slope_kb_per_s), same 17.5h sampler window")
    m.add("recSoakReaderRssFileSlopeMaxKBpsFour", f"{rssfile_max_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_FOUR)}: max(readers_composition_series[*]."
          "RssFile_kb.slope_kb_per_s), same 17.5h sampler window")
    m.add("recSoakReaderVmSizeSlopeMinKBpsFour", f"{vmsize_min_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_FOUR)}: min(readers_composition_series[*]."
          "VmSize_kb.slope_kb_per_s), same 17.5h sampler window")
    m.add("recSoakReaderVmSizeSlopeMaxKBpsFour", f"{vmsize_max_kb:.3f}",
          f"{relpath(LONGEVITY_RSS_SLOPES_FOUR)}: max(readers_composition_series[*]."
          "VmSize_kb.slope_kb_per_s), same 17.5h sampler window")
    m.add("recSoakWriterErrorsTrueFour", tex_num(writer_errors_true),
          f"{relpath(LONGEVITY_MANIFEST_FOUR)}: summary.writer_totals_all_lives.errors")
    m.add("recSoakWriterErrorsClassFour", "NotFoundError",
          f"{relpath(LONGEVITY_WRITER_ERRORS_BY_CLASS_FOUR)}: the sole exception class "
          "across every writer error in all 4 lives (text macro, not a number)")
    m.add("recSoakReaderErrorsTrueFour", tex_num(reader_errors_true),
          f"{relpath(LONGEVITY_READER_ERRORS_BY_CLASS_FOUR)}: reader_errors_total -- 0 "
          "reader errors of any class over the whole 24h run (prediction (a))")
    m.add("recSoakReaderErrorsOSErrorFour", tex_num(reader_oserror),
          f"{relpath(LONGEVITY_READER_ERRORS_BY_CLASS_FOUR)}: by_class_total has no "
          "OSError key -- 0, same all-zero record as the StateError class")
    m.add("recSoakReaderErrorsStateErrorFour", tex_num(reader_stateerror),
          f"{relpath(LONGEVITY_READER_ERRORS_BY_CLASS_FOUR)}: by_class_total has no "
          "StateError key -- 0 (prediction (f): the D-086 torn-tail class has no event "
          "to credit or discredit)")
    m.add("recSoakReaderReopensMinFour", tex_num(reopens_min),
          f"{relpath(LONGEVITY_READER_REOPEN_ON_ENOENT_FOUR)}: min(per_reader[*]."
          "reopens_total_progress_file), --reader-reopen-every-s=300 cycles completed "
          "per reader")
    m.add("recSoakReaderReopensMaxFour", tex_num(reopens_max),
          f"{relpath(LONGEVITY_READER_REOPEN_ON_ENOENT_FOUR)}: max(per_reader[*]."
          "reopens_total_progress_file) -- uniform at 272 across all 8 readers")
    m.add("recSoakReaderReopenOnEnoentMaxFour", tex_num(enoent_max),
          f"{relpath(LONGEVITY_READER_REOPEN_ON_ENOENT_FOUR)}: max(per_reader[*]."
          "reopen_on_enoent_total_progress_file), cross-checked against "
          "metrics.jsonl's own counter (prediction (b)): 0 for every reader")
    m.add("recSoakThroughputStartFour", str(throughput_start),
          f"{relpath(LONGEVITY_MANIFEST_FOUR)}: summary.drift.throughput_first_hour_avg, "
          "commits/s")
    m.add("recSoakThroughputEndFour", str(throughput_end),
          f"{relpath(LONGEVITY_MANIFEST_FOUR)}: summary.drift.throughput_last_hour_avg, "
          "commits/s")
    m.add("recSoakThroughputFirstHourAvgFour", str(throughput_start),
          f"{relpath(LONGEVITY_MANIFEST_FOUR)}: summary.drift.throughput_first_hour_avg "
          "(same field as recSoakThroughputStartFour, landed separately for prediction "
          "(d)'s +-5%-of-soak2 throughput clause)")
    m.add("recSoakThroughputLastOverFirstRatioFour", f"{ratio_last_over_first:.3f}",
          f"{relpath(LONGEVITY_THROUGHPUT_FOUR)}: ratio_last_over_first, recomputed here "
          "as this_record_last_hour_avg_commits_per_s / "
          "this_record_first_hour_avg_commits_per_s from this same file's own two averages")
    m.add("recSoakFullVerifyOverlapCountFour", tex_num(overlap_count),
          f"{relpath(LONGEVITY_VERIFY_FULL_FOUR)}: PROBLEMS count (believed-versions-"
          "overlap class), 0 -- matching soak2/soak3's clean full-mode result "
          "(prediction (e))")
    m.add("recSoakFullVerifyGenerationFour", tex_num(verify_generation),
          f"{relpath(LONGEVITY_VERIFY_FULL_FOUR)}: generation -- 341 ahead of the "
          "manifest's own summary.generation_final=1,896,750 (the gauge froze a few "
          "seconds early, README.md's Full-mode tgms check section; this is the "
          "on-disk-confirmed true final generation, not a disagreement to resolve by "
          "equality)")
    m.add("recSoakCompactionStallMaxReaderP99MsFour", f"{stall_max:.3f}",
          f"{relpath(LONGEVITY_COMPACTION_STALL_FOUR)}: this_record_max_reader_p99_ms -- "
          "max query_p99_ms sample inside any of the 4,213 compaction windows "
          "([ts_start-60, ts_end+60]), matching manifest.summary."
          "compaction_stall_max_reader_p99_ms exactly; the first soak in this campaign "
          "where this row is computable at all")
    m.add("recSoakHostLoadMinFour", f"{host_load_min:.2f}",
          f"{relpath(LONGEVITY_HOST_LOAD_FOUR)}: min(1-min load averages), all 33 samples")
    m.add("recSoakHostLoadMaxFour", f"{host_load_max:.2f}",
          f"{relpath(LONGEVITY_HOST_LOAD_FOUR)}: max(1-min load averages), same 33-sample "
          "set")
    m.add("recSoakFinalEdgeRowsFour", tex_num(final_edge_rows),
          f"{relpath(LONGEVITY_MANIFEST_FOUR)}: summary.final_stats.n_edge_versions -- "
          "the final store size this run reached, past the soak-2 onset band "
          "2,235,044-2,450,815 rows (predictions (a)/(f))")

    # --- derived first-hour throughput band around soak2's own
    # first-hour figure (recSoakThroughputStartTwo), used to state
    # prediction (d)'s +-5% clause as a concrete [lo, hi] interval rather
    # than leaving the +-5% arithmetic to the manuscript's prose.
    # Computed from LONGEVITY_MANIFEST_TWO's own
    # drift.throughput_first_hour_avg at generation time (39.91), never
    # typed independently of it. ---
    manifest_two = json.loads(LONGEVITY_MANIFEST_TWO.read_text(encoding="utf-8"))
    throughput_start_two = manifest_two["summary"]["drift"]["throughput_first_hour_avg"]
    eq(throughput_start_two, 39.91,
       "Soak2 frozen (re-read for the soak4 band): summary.drift.throughput_first_hour_avg, "
       "commits/s -- the same value recSoakThroughputStartTwo lands")
    band_lo = 0.95 * throughput_start_two
    band_hi = 1.05 * throughput_start_two
    m.add("recSoakFourFirstHourBandLo", f"{band_lo:.4f}",
          f"0.95 * {relpath(LONGEVITY_MANIFEST_TWO)}'s summary.drift."
          "throughput_first_hour_avg (recSoakThroughputStartTwo, 39.91 commits/s) -- "
          "the lower edge of soak4 prediction (d)'s +-5%-of-soak2 first-hour-throughput "
          "band")
    m.add("recSoakFourFirstHourBandHi", f"{band_hi:.4f}",
          f"1.05 * {relpath(LONGEVITY_MANIFEST_TWO)}'s summary.drift."
          "throughput_first_hour_avg (recSoakThroughputStartTwo, 39.91 commits/s) -- "
          "the upper edge of the same band; recSoakThroughputFirstHourAvgFour=40.552 "
          "falls inside [recSoakFourFirstHourBandLo, recSoakFourFirstHourBandHi]")

    # --- compaction cadence (median inter-compaction interval/duration)
    # and the generation-validity window, same convention as the W2z
    # cadence function above (_split_compaction_lives), generalized to
    # this soak's own epoch ts_start/ts_end fields. compactions-4.jsonl
    # (unlike compactions-2/3.jsonl) also carries its own `life` field
    # directly -- assert it lands on exactly the same groups as the
    # batches-reset split, an independent confirmation that the split is
    # correct here, not just assumed from the soak2/soak3 precedent. ---
    rows_four = load_jsonl(LONGEVITY_COMPACTIONS_FOUR)
    eq(len(rows_four), 4213, f"{relpath(LONGEVITY_COMPACTIONS_FOUR)}: row count")
    lives_four = _split_compaction_lives(
        rows_four, len(recoveries_rows) + 1, relpath(LONGEVITY_COMPACTIONS_FOUR))
    for life_idx, group in enumerate(lives_four):
        require({row["life"] for row in group} == {life_idx},
                f"{relpath(LONGEVITY_COMPACTIONS_FOUR)}: batches-reset life group {life_idx} "
                "agrees with this file's own `life` field for every row in the group")

    cadence_intervals: list[float] = []
    cadence_durations: list[float] = []
    for life in lives_four:
        ts = [r["ts_start"] for r in life]
        cadence_intervals.extend(ts[i] - ts[i - 1] for i in range(1, len(ts)))
        cadence_durations.extend(r["ts_end"] - r["ts_start"] for r in life)
    cadence_gens = {r["gc"]["generations_retained"] for r in rows_four}
    eq(cadence_gens, {2},
       f"{relpath(LONGEVITY_COMPACTIONS_FOUR)}: gc.generations_retained, uniform across "
       "every row")
    interval_four = statistics.median(cadence_intervals)
    duration_four = statistics.median(cadence_durations)

    close(interval_four, 20.4881, 0.01,
          "Soak4 frozen: median inter-compaction interval (epoch ts_start), s")
    close(duration_four, 12.2326, 0.01,
          "Soak4 frozen: median compaction duration (epoch ts_end - ts_start), s")

    cadence_gens_retained = next(iter(cadence_gens))
    window_four = cadence_gens_retained * interval_four

    m.add("recSoakCompactionIntervalMedianSFour", f"{interval_four:.1f}",
          f"{relpath(LONGEVITY_COMPACTIONS_FOUR)}: median(ts_start[i] - ts_start[i-1]) "
          "within each of the 4 writer lives (life boundaries: batches resets to 500, "
          f"{relpath(LONGEVITY_RECOVERIES_FOUR)}'s 3 rows -> 4 lives, cross-checked "
          "against this file's own `life` field), pooled over all 4 lives' own "
          "intervals, s -- this run's compaction log carries epoch ts_start/ts_end "
          "directly, unlike soak3's host-CLOCK_MONOTONIC-only t_start/t_end")
    m.add("recSoakCompactionDurationMedianSFour", f"{duration_four:.1f}",
          f"{relpath(LONGEVITY_COMPACTIONS_FOUR)}: median(ts_end - ts_start) over all "
          f"{len(rows_four)} compactions, s")
    m.add("recSoakGenerationWindowSFour", f"{window_four:.1f}",
          f"gc.generations_retained ({cadence_gens_retained}, "
          f"{relpath(LONGEVITY_COMPACTIONS_FOUR)}, recD088GenerationsRetained) * "
          "recSoakCompactionIntervalMedianSFour -- the generation-validity window, s")


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
    m.add("recOverloadCommit", rep1["git_commit"],
          f"{relpath(OVERLOAD_REP1)}: git_commit (same in rep2)")
    m.add("recOverloadMaxConcurrent", tex_num(max_concurrent),
          f"{relpath(OVERLOAD_REP1)}: config.max_concurrent (== protocol.ceilings."
          "max_concurrent), the harness's own CLI default under test")
    m.add("recOverloadClientsMax", tex_num(clients_max),
          f"{relpath(OVERLOAD_REP1)}: max(steps[*].n_clients) -- the gate engages at this "
          "cell in both reps")
    m.add("recOverloadRefusalKindConcurrencyOnly", "true",
          f"{relpath(OVERLOAD_REP1_RECORDS)}: the set of refusal_stage values over every "
          "n=64 record with outcome==\"refused\" is exactly {\"limit\"} -- every refusal "
          "is a concurrency-cap refusal, never a result-size limit (text macro, not a "
          "number)")
    m.add("recOverloadOperatorErrorsTotal", tex_num(n_error_total),
          f"{relpath(OVERLOAD_REP1)}+{relpath(OVERLOAD_REP2)}: sum(steps[*].n_error) over "
          "every step, both reps")
    m.add("recOverloadRefusedCapAtMaxRep1", tex_num(refused_cap_rep1_at64),
          f"{relpath(OVERLOAD_REP1)}: steps[n_clients=64].n_refused")
    m.add("recOverloadRefusedCapAtMaxRep2", tex_num(refused_cap_rep2_at64),
          f"{relpath(OVERLOAD_REP2)}: steps[n_clients=64].n_refused")
    m.add("recOverloadRefusalRepRatio", f"{refusal_ratio:.1f}",
          "derived: rep1/rep2 refused-at-n=64 ratio (rep-to-rep variance at the knee, "
          "not a measured field)")
    m.add("recOverloadAdmittedConcurrencyP95AtMax", f"{admitted_p95_at64:.1f}",
          f"{relpath(OVERLOAD_REP1)}: steps[n_clients=64].concurrent_in_flight_p95")
    m.add("recOverloadP99At32ClientsMs", f"{p99_at32_rep1:.2f}",
          f"{relpath(OVERLOAD_REP1)}: steps[n_clients=32].p99_ms, with n_refused==0 at "
          "that cell -- the bounded-latency prediction's REFUTED evidence")
    m.add("recOverloadRecoveryQps", f"{recovery_qps:.2f}",
          f"{relpath(OVERLOAD_REP1)}: recovery.throughput_qps")
    m.add("recOverloadRecoveryP50Ms", f"{recovery_p50:.2f}",
          f"{relpath(OVERLOAD_REP1)}: recovery.p50_ms")
    m.add("recOverloadServiceHighWaterKB", tex_num(hwm_after_step),
          f"{relpath(OVERLOAD_HWM_CHECKPOINTS_V3)}: after_step_n64.vm_hwm_kb (== "
          "after_recovery_step.vm_hwm_kb) -- the \"Pinned\" localization, not the "
          "harness's own end-of-sweep store.digest() lifetime peak")
    m.add("recOverloadServiceHighWaterMB", f"{high_water_mb:.1f}",
          f"{relpath(OVERLOAD_HWM_CHECKPOINTS_V3)}: after_step_n64.vm_hwm_kb / 1000, MB")


# --------------------------------------------------------------------------
# C10 --- live OSV advisory-feed workload, first committed snapshot
# --------------------------------------------------------------------------

def compute_c10_live_osv(m: Macros) -> None:
    """Lane C10-snap: `benchmarks/live-osv-v1/snapshot-2026-09-16.json`, the
    first committed record snapshot of the live-osv poller
    (`docs/design/LIVE_WORKLOAD_OSV_DESIGN_2026-09-13.md`) running on xzgpu.
    Resolves the three macros that were PENDING for lack of any committed
    record: `recLiveDays`, `recLiveAdvisories`, `recLiveCorrections`.

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

    # --- poller identity: cycle count and restart count, cross-checked
    # against the raw row count rather than trusted from the manifest's
    # own pre-aggregated poller block alone ---
    poller = live["poller"]
    eq(poller["cycles_completed"], len(cycles),
       f"{relpath(LIVE_OSV_SNAPSHOT)}: live_osv.poller.cycles_completed matches "
       "len(cycles_raw)")
    eq(poller["cycles_completed"], 38, "C10 frozen: poller.cycles_completed")
    eq(poller["restart_count"], 0, "C10 frozen: poller.restart_count (one continuous life)")

    # --- store identity at snapshot time: node/edge counts, a distinct
    # field from every corrections/advisories count above ---
    store_identity = live["store_identity_at_snapshot"]
    eq(store_identity["nodes"], 247_845, "C10 frozen: store_identity_at_snapshot.nodes")
    eq(store_identity["edges"], 497_522, "C10 frozen: store_identity_at_snapshot.edges")

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

    m.add("recLiveDays", f"{days_2dp:.2f}",
          f"{relpath(LIVE_OSV_SNAPSHOT)}: live_osv.operation, "
          "(snapshot_epoch - first_record_epoch) / 86400, recomputed from cycles_raw[0].ts")
    m.add("recLiveAdvisories", tex_num(advisories["total_at_snapshot"]),
          f"{relpath(LIVE_OSV_SNAPSHOT)}: live_osv.advisories.total_at_snapshot "
          f"({tex_num(advisories['bootstrap'])} bootstrap + "
          f"{advisories['new_since_bootstrap']} live-ingested)")
    m.add("recLiveCorrections", tex_num(corrections["corrections_written"]),
          f"{relpath(LIVE_OSV_SNAPSHOT)}: live_osv.corrections.corrections_written, "
          "recomputed as sum(cycles_raw[*].corrections_written)")
    m.add("recLiveCycles", tex_num(poller["cycles_completed"]),
          f"{relpath(LIVE_OSV_SNAPSHOT)}: live_osv.poller.cycles_completed, cross-checked "
          "against len(cycles_raw) (eval.tex's \"38 hourly poller cycles\")")
    m.add("recLiveRestarts", tex_num(poller["restart_count"]),
          f"{relpath(LIVE_OSV_SNAPSHOT)}: live_osv.poller.restart_count "
          "(eval.tex's \"0 restarts\")")
    m.add("recLiveFeedErrors", tex_num(corrections["feed_errors"]),
          f"{relpath(LIVE_OSV_SNAPSHOT)}: live_osv.corrections.feed_errors, recomputed as "
          "sum(cycles_raw[*].feed_errors) (eval.tex's \"0 feed errors\")")
    m.add("recLiveNoopRevisions", tex_num(corrections["noop_revisions"]),
          f"{relpath(LIVE_OSV_SNAPSHOT)}: live_osv.corrections.noop_revisions, recomputed as "
          "sum(cycles_raw[*].noop_revisions) (eval.tex's \"41 no-op revisions\")")
    m.add("recLiveRecordsSeen", tex_num(corrections["records_seen"]),
          f"{relpath(LIVE_OSV_SNAPSHOT)}: live_osv.corrections.records_seen, recomputed as "
          "sum(cycles_raw[*].records_seen) (eval.tex's \"95 records seen\", the denominator)")
    m.add("recLiveRetractions", tex_num(corrections["retractions"]),
          f"{relpath(LIVE_OSV_SNAPSHOT)}: live_osv.corrections.retractions, recomputed as "
          "sum(cycles_raw[*].retractions) (eval.tex's \"one retraction\"; numerically equal "
          "to recLiveCorrections in this snapshot but a distinct record field)")
    m.add("recLiveNodes", tex_num(store_identity["nodes"]),
          f"{relpath(LIVE_OSV_SNAPSHOT)}: live_osv.store_identity_at_snapshot.nodes "
          "(eval.tex's \"247,845 nodes\")")
    m.add("recLiveEdges", tex_num(store_identity["edges"]),
          f"{relpath(LIVE_OSV_SNAPSHOT)}: live_osv.store_identity_at_snapshot.edges "
          "(eval.tex's \"497,522 edges\")")


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
    stand PENDING here (`recLdbcExpressible`/`recLdbcExecuted`/
    `recLdbcValidated`) and lands the rest of the scorecard beside them.

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

    24 templates have a vendored TGIR plan and ran (`recLdbcExpressible`);
    23 of those completed within the ceiling -- BI6.v2 hit the
    pre-registered ceiling (`bypass_ceiling_s` + `child_open_allowance_s`
    from the TGMS-side campaign record) and produced no rows -- giving
    `recLdbcExecuted`; 18 agree per campaign.yaml's addendum_3 scoring
    rule, giving `recLdbcValidated`.
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
    # recLdbcInterim* so the paper can cite the reference-side fix. The
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
    m.add("recLdbcTemplates", tex_num(24),
          f"{relpath(LDBC_REF_V1_COMPARE)}: manifest.plans -- the 24 LDBC templates with a "
          "vendored TGIR plan run by this campaign")
    m.add("recLdbcExpressible", tex_num(24),
          f"{relpath(LDBC_REF_V1_COMPARE)}: manifest.plans -- templates the vendored TGIR "
          "plans cover of the 24 in this campaign")
    m.add("recLdbcExecuted", tex_num(len(attempted)),
          f"{relpath(LDBC_REF_V1_COMPARE)}: count of verdicts with attempted == true -- "
          f"templates whose TGMS side completed within the {tex_num(ceiling_s)} s ceiling "
          "(BI6.v2 timed out at that ceiling, README.md §5.6)")
    m.add("recLdbcValidated", tex_num(len(agree)),
          f"{relpath(LDBC_REF_V1_COMPARE)}: count of verdicts with verdict == agreeing -- "
          "templates agreeing per campaign.yaml addendum_3's scoring rule")
    m.add("recLdbcAgree", tex_num(len(agree)),
          f"{relpath(LDBC_REF_V1_COMPARE)}: count of verdict == agreeing")
    m.add("recLdbcNotProjected", tex_num(len(not_projected)),
          f"{relpath(LDBC_REF_V1_COMPARE)}: count of verdict == reference-column-not-projected "
          "(BI4, IC5, IC12)")
    m.add("recLdbcDisagree", tex_num(len(disagree)),
          f"{relpath(LDBC_REF_V1_COMPARE)}: count of verdict == disagreeing (IC2, IS3)")
    m.add("recLdbcTimeout", tex_num(len(timed_out)),
          f"{relpath(LDBC_REF_V1_COMPARE)}: count of attempted == false (BI6.v2)")
    m.add("recLdbcComparableTemplates", tex_num(len(attempted)),
          f"{relpath(LDBC_REF_V1_COMPARE)}: count of attempted == true -- the denominator of "
          "the row-agreement fraction")
    m.add("recLdbcRowsCompared", tex_num(rows_compared),
          f"{relpath(LDBC_REF_V1_COMPARE)}: sum(compared) over the 23 comparable templates")
    m.add("recLdbcRowsAgreeing", tex_num(rows_agreeing),
          f"{relpath(LDBC_REF_V1_COMPARE)}: sum(agreeing) over the 23 comparable templates")
    m.add("recLdbcRowAgreementFraction", row_fraction_3dp,
          f"{relpath(LDBC_REF_V1_COMPARE)}: sum(agreeing) / sum(compared) over the 23 "
          "comparable templates, 3dp")
    m.add("recLdbcGate", f"{gate:.2f}",
          "campaign.yaml addendum_3 scoring.overall_agreement: the pre-registered pass_if bar")
    m.add("recLdbcGateMet", "true" if row_fraction >= gate else "false",
          f"{relpath(LDBC_REF_V1_COMPARE)}: recomputed row agreement fraction "
          f"({row_fraction_3dp}) >= campaign.yaml's pass_if gate (0.90)")
    m.add("recLdbcTemplateAgreementFraction", f"{template_fraction:.2f}",
          f"{relpath(LDBC_REF_V1_COMPARE)}: count(verdict == agreeing) / 24")

    for fam, (fam_agreeing, fam_compared) in fam_rows.items():
        m.add(f"recLdbcRows{fam}Agreeing", tex_num(fam_agreeing),
              f"{relpath(LDBC_REF_V1_COMPARE)}: sum(agreeing) over the {fam} family's "
              "comparable templates (campaign.yaml addendum_3 row_agreement.by_group)")
        m.add(f"recLdbcRows{fam}Compared", tex_num(fam_compared),
              f"{relpath(LDBC_REF_V1_COMPARE)}: sum(compared) over the {fam} family's "
              "comparable templates (campaign.yaml addendum_3 row_agreement.by_group)")

    m.add("recLdbcDefectId", defect_id,
          "ops/failure_ledger.jsonl: D-090-is3-knows-both-ways-double-count")
    m.add("recLdbcDefectTemplates", ", ".join(defect_templates_ordered),
          f"{relpath(LDBC_REF_V1_COMPARE)}: the disagreeing verdicts (IC2, IS3), both the "
          "M7 KNOWS-both-ways double count (D-090) per README.md §5.4/§5.5 -- IS3 "
          "the original finding, IC2 the second instance found once its reference became "
          "valid, reported in that order")

    m.add("recLdbcInterimAgree", tex_num(interim_agree),
          f"{relpath(LDBC_REF_V1_COMPARE_INTERIM)}: count of verdict == agreeing in the "
          "superseded 2026-09-17 revision")
    m.add("recLdbcInterimRowsAgreeing", tex_num(interim_rows_agreeing),
          f"{relpath(LDBC_REF_V1_COMPARE_INTERIM)}: sum(agreeing) over the 16 templates that "
          "produced a valid comparison on 2026-09-17 (excludes the 7 templates named in this "
          "record's own manifest.supersedes_reason plus BI6.v2)")
    m.add("recLdbcInterimRowsCompared", tex_num(interim_rows_compared),
          f"{relpath(LDBC_REF_V1_COMPARE_INTERIM)}: sum(compared) over the same 16 templates")


# --------------------------------------------------------------------------
# Lane W2z -- eval.tex:120's "the store rebuild costs 12.8% more, in band"
# (the format-3 LDBC SF1 store-rebuild cost). Both READMEs are prose-only
# records (no JSON sidecar carries the store-build wall clock); each is
# whole-file sha256-checked before its own wall-time sentence is pulled
# out by regex, and the percentage is recomputed from both wall times
# rather than trusted from the interactive README's own typed "+12.8%".
# --------------------------------------------------------------------------

def compute_ldbc_format3_rebuild(m: Macros) -> None:
    """The P-SF1 format-3 baseline store build (a6b3e94,
    ldbc-sf1-campaign-fmt3-2026-09.README.md) vs. P-SF1b's corrected
    characterization-interactive rerun's own fresh store build (54dcab0,
    ldbc-sf1-campaign-fmt3-interactive-2026-09.README.md -- rerun because
    P-SF1's original invocation predates the --csv bind fix, see that
    README's own "The bug" section). Both store builds share the same
    `scripts/build_snb_store.py` flags and input dataset; only the wall
    clock differs. eval.tex:120 reports the increase as "12.8%, in
    band" (the interactive README's own ±20% tolerance) -- recomputed
    here from both wall times, not read off the interactive README's own
    typed "+12.8%" string.
    """
    eq(sha256_file(LDBC_FMT3_README), LDBC_FMT3_README_SHA256,
       f"{relpath(LDBC_FMT3_README)}: sha256 matches this lane's frozen value")
    eq(sha256_file(LDBC_FMT3_INTERACTIVE_README), LDBC_FMT3_INTERACTIVE_README_SHA256,
       f"{relpath(LDBC_FMT3_INTERACTIVE_README)}: sha256 matches this lane's frozen value")

    base_text = LDBC_FMT3_README.read_text(encoding="utf-8")
    inter_text = LDBC_FMT3_INTERACTIVE_README.read_text(encoding="utf-8")

    base_match = re.search(r"Wall:\s*(\d+\.\d+)\s*s \(streaming", base_text)
    require(base_match is not None,
            f"{relpath(LDBC_FMT3_README)}: store-build 'Wall: <n> s (streaming' sentence found")
    inter_match = re.search(r"Wall:\s*(\d+\.\d+)\s*s \(streaming", inter_text)
    require(inter_match is not None,
            f"{relpath(LDBC_FMT3_INTERACTIVE_README)}: store-build 'Wall: <n> s (streaming' "
            "sentence found")
    # cross-check: the interactive README also quotes both wall times and its
    # own typed percentage in one sentence -- pulled independently so a typo
    # in either README's number is caught by disagreement, not by trusting
    # either copy alone.
    quoted_match = re.search(
        r"of the [\d.]+\s*s\)\s*—\s*\+([\d.]+)%\s*vs P-SF1's\s+([\d.]+)\s*s", inter_text)
    require(quoted_match is not None,
            f"{relpath(LDBC_FMT3_INTERACTIVE_README)}: '+<pct>% vs P-SF1's <n> s' sentence found")

    base_wall = float(base_match.group(1))
    inter_wall = float(inter_match.group(1))
    eq(base_wall, 780.8, "LDBC format-3 rebuild frozen: P-SF1 baseline store-build wall, s")
    eq(inter_wall, 881.0,
       "LDBC format-3 rebuild frozen: P-SF1b rerun store-build wall, s")
    eq(float(quoted_match.group(2)), base_wall,
       f"{relpath(LDBC_FMT3_INTERACTIVE_README)}: its own quoted P-SF1 baseline wall matches "
       f"{relpath(LDBC_FMT3_README)}'s own 'Wall:' sentence")

    rebuild_pct = (inter_wall - base_wall) / base_wall * 100.0
    close(rebuild_pct, float(quoted_match.group(1)), 0.05,
          f"{relpath(LDBC_FMT3_INTERACTIVE_README)}: recomputed (881.0-780.8)/780.8*100 "
          "matches its own quoted +12.8% figure")
    close(rebuild_pct, 12.8, 0.05, "LDBC format-3 rebuild frozen: rebuild cost increase, %")

    m.add("recLdbcFormatThreeRebuildPct", f"{rebuild_pct:.1f}",
          f"({relpath(LDBC_FMT3_INTERACTIVE_README)}'s Wall - {relpath(LDBC_FMT3_README)}'s "
          f"Wall) / {relpath(LDBC_FMT3_README)}'s Wall * 100 -- 881.0 s vs 780.8 s, both "
          "store builds via scripts/build_snb_store.py with identical flags, eval.tex:120's "
          "\"12.8% more, in band\"")


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

    # --- recovery ratios: cadence-500 wall vs its own frozen 46-minute upper
    # bound (wrong.tex's "$5.3\times$ its bar"), and cadence-500 wall vs
    # cadence-5000 wall on the same 30M store (eval.tex's "5.5 times less
    # wall") -- both recomputed from replay_wall_s and cross-checked against
    # the record's own verdict-string wording, never taken from that prose
    # alone ---
    bound_500_upper_min = rec500["band_min"][1]
    eq(bound_500_upper_min, 46,
       f"{relpath(B7_RECOVERY_30M)}: band_min[1], the cadence-500 upper bound, minutes")
    require(f"{bound_500_upper_min}-minute upper bound" in rec500["verdict"],
            f"{relpath(B7_RECOVERY_30M)}: verdict names the same 46-minute upper bound")
    recovery_bound_ratio_30m = round(rec500["replay_wall_s"] / (bound_500_upper_min * 60), 1)
    eq(recovery_bound_ratio_30m, 5.3,
       "B7 frozen: 30M recovery wall_s / (cadence-500 band_min upper bound * 60s)")

    recovery_cadence_ratio_30m = round(rec500["replay_wall_s"] / rec5000["replay_wall_s"], 1)
    eq(recovery_cadence_ratio_30m, 5.5,
       "B7 frozen: 30M recovery wall_s ratio, cadence 500 / cadence 5000 (Addendum 6)")
    require("5.5x less wall" in rec5000["verdict"],
            f"{relpath(B7_RECOVERY_30M_CE5000)}: verdict names the same ~5.5x figure")

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
    # Every other B7 peak-RSS macro (e.g. recB7PeakRSS30M above) computes GB
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
       "recB7Calib10MPeakRSS above)")
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
    m.add("recB7BuildWall30M", tex_float(round(wall_s, 3)),
          f"{relpath(B7_BUILD_30M)}: build_info.wall_s, seconds")
    m.add("recB7PeakRSS30M", f"{peak_rss_gb:.2f}",
          f"{relpath(B7_BUILD_30M)}: build_info.peak_rss.vmhwm / 1e6, GB, 2dp "
          f"({tex_num(peak_vmhwm_kb)} KB)")
    m.add("recB7VersionHistoryWall30M", f"{vh_wall_s:.3f}",
          f"{relpath(B7_VERSION_HISTORY_30M)}: clean_alone (job 213175), median(reps[*]."
          "wall_ms) / 1000, seconds")
    m.add("recB7VersionHistoryRSS30M", f"{vh_rss_gb:.3f}",
          f"{relpath(B7_VERSION_HISTORY_30M)}: clean_alone (job 213175), "
          "median(reps[*].vmhwm_kb) / 1e6, GB")
    m.add("recB7ManifestBytes30M", tex_num(manifest_bytes),
          f"{relpath(B7_BUILD_30M)}: build_info.store_bytes.manifest_bytes")
    m.add("recB7SegmentBytes30M", f"{segment_gb:.3f}",
          f"{relpath(B7_BUILD_30M)}: build_info.store_bytes.segment_bytes / 1e9, GB")
    m.add("recB7CheckFullWall30M", f"{check_wall:.3f}",
          f"{relpath(B7_CHECK_FULL_30M)}: canonical_run (clean, job 213174), wall_s")
    m.add("recB7Recovery30M", tex_float(recovery_500_s),
          f"{relpath(B7_RECOVERY_30M)}: replay_wall_s, rounded 1dp, seconds "
          "(frozen cadence 500, same as the Stage-0/EXP-A4 1M recovery); "
          "digest_compare.digest_equal true")
    m.add("recB7RecoveryCe5000At30M", tex_float(recovery_5000_s),
          f"{relpath(B7_RECOVERY_30M_CE5000)}: replay_wall_s, rounded 1dp, seconds "
          "(Addendum 6 cadence-isolation run, compact_every=5000); "
          "digest_compare.digest_equal true; does not supersede recB7Recovery30M "
          "(both stand, per the campaign's non-overwrite discipline)")
    m.add("recB7RecoveryBoundRatioAt30M", tex_float(recovery_bound_ratio_30m),
          f"{relpath(B7_RECOVERY_30M)}: replay_wall_s / (band_min[1] * 60s), rounded 1dp -- "
          "the cadence-500 recovery's own multiple of its frozen 46-minute upper bound "
          "(eval.tex's \"$5.3\\times$ its bar\")")
    m.add("recB7RecoveryCadenceRatioAt30M", tex_float(recovery_cadence_ratio_30m),
          f"{relpath(B7_RECOVERY_30M)}/{relpath(B7_RECOVERY_30M_CE5000)}: replay_wall_s "
          "ratio, cadence 500 / cadence 5000, rounded 1dp, same 30M store and digest "
          "(\"5.5 times less wall for ten times fewer compactions\")")
    for op_id, frag in B7_SCALE_CURVE_OPS.items():
        val = p50_by_op[op_id]
        m.add(f"recB7ScaleCurveP50{frag}30M", tex_float(val),
              f"{relpath(B7_SCALE_CURVE_30M)}: per_operator_p50_ms.{op_id}."
              "clean_213174_p50_ms, ms (clean, job 213174), recomputed from "
              f"{relpath(B7_SCALE_CURVE_30M_RAW)}'s own timings_ms")
    m.add("recB7ReachWindowRefused30M", "false",
          f"{relpath(B7_SCALE_CURVE_30M)}: reach_window_admission.clean_213174.admitted "
          "is true (not refused) -- Addendum 5's second re-examination revised the "
          "admission-policy prediction from refused to admitted at 30M/100M, confirmed "
          "by both the clean and contended reruns")
    m.add("recB7QueryFloor30M", f"{query_floor_gb:.2f}",
          f"{relpath(B7_QUERYFLOOR_30M)}: vmhwm_kb / 1e6, GB (fresh read-only process, "
          "cold-open, job 213173, alone)")
    m.add("recB7BuildSteadyDecileMedian30M", tex_float(steady_decile_median),
          f"{relpath(B7_BUILD_30M)}: build_info.steady_decile_median_ops_per_s, "
          "recomputed as median(ops_per_s_by_decile[*].ops_per_s), ops/s")
    m.add("recB7ReachWindowEstimateMs30M", tex_num(estimate_ms),
          f"{relpath(B7_SCALE_CURVE_30M)}: reach_window_admission.clean_213174."
          "estimate.time_est_ms, ms")

    # recB7300MGate: not a measurement -- the pre-registration's §3 ruling
    # ("RULED MOOT", the PI's "300M dropped" superseding the blueprint's
    # "300M only if 100M is clean") means no 300M step is planned or
    # pre-registered at all; scale-independent, so no {30M,100M} suffix.
    m.add("recB7300MGate", "false",
          "docs/design/SCALE_BUILD_FORECAST_2026-09-15.md §3 (gitignored, internal): "
          "\"RULED MOOT\" -- the PI's later ruling (\"300M dropped\") supersedes the "
          "blueprint's \"300M only if 100M is clean\"; no 300M step is planned or "
          "pre-registered. Not a record-derived measurement, unlike every other macro "
          "in this function")

    m.add("recB7Calib10MBuildWall", f"{calib10m_wall:.3f}",
          f"{relpath(B7_ITIGER_CALIB_10M)}: build_info.wall_s, seconds")
    m.add("recB7Calib10MPeakRSS", f"{calib10m_peak_gb:.2f}",
          f"{relpath(B7_ITIGER_CALIB_10M)}: build_info.peak_rss.vmhwm "
          f"({tex_num(calib10m_peak_kb)} kB) / 1e6, GB, 2dp -- this campaign's kB/1e6 "
          "convention (same as every other B7 peak-RSS macro), NOT the calibration "
          f"README's own mixed-unit-slip prose figure; see "
          f"{relpath(B7_ITIGER_CALIB_README)}'s appended Unit note")
    m.add("recB7Calib10MSteadyOps", tex_float(round(calib10m_median, 1)),
          f"{relpath(B7_ITIGER_CALIB_10M)}: recomputed median(ops_per_s_by_decile[*]."
          "ops_per_s), ops/s")
    m.add("recB7KBuild", f"{k_build:.3f}",
          f"{relpath(B7_ITIGER_CALIB_10M)}: (10M steady-decile-median ops/s) / 5,300 "
          f"(xzgpu bulk basis, quoted by {relpath(B7_ITIGER_CALIB_README)}); retired as "
          "a Stage-1 scaling factor (Addendum 5) but kept as a recorded observation")
    m.add("recB7KRecover", f"{k_recover:.3f}",
          f"{relpath(B7_ITIGER_CALIB_1M)}: recovery.wall_s / 91.01 (xzgpu 1% anchor, "
          f"quoted by {relpath(B7_ITIGER_CALIB_README)}); used as Addendum 4/5's Stage-1 "
          "recovery-band scaling factor")
    m.add("recB7Calib1MBuildWall", f"{calib1m_wall:.3f}",
          f"{relpath(B7_ITIGER_CALIB_1M)}: build_info.wall_s, seconds")
    m.add("recB7Calib1MPeakRSS", f"{calib1m_peak_gb:.2f}",
          f"{relpath(B7_ITIGER_CALIB_1M)}: build_info.peak_rss.vmhwm "
          f"({tex_num(calib1m_peak_kb)} kB) / 1e6, GB, 2dp -- same kB/1e6 convention as "
          "recB7Calib10MPeakRSS")
    m.add("recB7Calib1MRecovery", f"{recovery_1m_wall:.2f}",
          f"{relpath(B7_ITIGER_CALIB_1M)}: recovery.wall_s, seconds (EXP-A4 replay, "
          "compact_every=500, the same cadence as recB7Recovery30M); "
          "recovery.digest_equal is true")
    m.add("recB7Calib1MSegmentBytes", f"{calib1m_segment_gb:.3f}",
          f"{relpath(B7_ITIGER_CALIB_1M)}: build_info.store_bytes.segment_bytes / 1e9, GB")
    # recB7Calib10MRecovery: itiger-calib-10m.json carries no "recovery" field
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
    # and would only re-measure D-164 again). recB7Recovery100M aliases
    # this single measurement rather than sitting PENDING forever for a
    # run that was never planned -- the §4 record layout has no
    # recovery-100m.json file at all, only recovery-100m-ce5000.json.
    require("no 500-cadence 100M run" in rec100_5000["protocol_note"],
            f"{relpath(B7_RECOVERY_100M_CE5000)}: protocol_note states there is no "
            "500-cadence 100M run to reconcile against")
    recovery100_5000_s = round(rec100_5000["replay_wall_s"], 1)
    eq(recovery100_5000_s, 22717.7, "B7 frozen: 100M recovery wall_s, cadence 5000")

    # --- miss ratio: how far the 100M cadence-5000 wall exceeds its own
    # frozen 6h upper bound (eval.tex's "a 5% miss rather than a
    # refutation") -- recomputed from band_h and replay_wall_s, cross-
    # checked against the record's own verdict-string wording ---
    bound_100_upper_h = rec100_5000["band_h"][1]
    eq(bound_100_upper_h, 6.0,
       f"{relpath(B7_RECOVERY_100M_CE5000)}: band_h[1], the cadence-5000 upper bound, hours")
    require("exceeds the 6h upper bound" in rec100_5000["verdict"],
            f"{relpath(B7_RECOVERY_100M_CE5000)}: verdict names the same 6h upper bound")
    recovery100_miss_pct = round(
        (rec100_5000["replay_wall_s"] / (bound_100_upper_h * 3600) - 1) * 100, 1)
    eq(recovery100_miss_pct, 5.2,
       "B7 frozen: 100M recovery wall_s / (cadence-5000 band_h upper bound * 3600s) - 1, %")
    recovery100_miss_pct_rounded = round(recovery100_miss_pct)
    eq(recovery100_miss_pct_rounded, 5,
       "B7 frozen: 100M recovery miss, rounded to the nearest percent (eval.tex's \"5%\")")

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
    # than becoming an recB7Refused* macro.
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
    m.add("recB7BuildWall100M", tex_float(round(wall100_s, 3)),
          f"{relpath(B7_BUILD_100M)}: build_info.wall_s, seconds")
    m.add("recB7PeakRSS100M", f"{peak100_rss_gb:.2f}",
          f"{relpath(B7_BUILD_100M)}: build_info.peak_rss.vmhwm / 1e6, GB, 2dp "
          f"({tex_num(peak100_vmhwm_kb)} KB) -- campaign convention per "
          "falsifiers.build_vmhwm_h1_h2.unit_note, not true decimal GB/GiB")
    m.add("recB7VersionHistoryWall100M", f"{vh100_wall_s:.3f}",
          f"{relpath(B7_VERSION_HISTORY_100M)}: single_run (job 213191), "
          "median(reps[*].wall_ms) / 1000, seconds")
    m.add("recB7VersionHistoryRSS100M", f"{vh100_rss_gb:.3f}",
          f"{relpath(B7_VERSION_HISTORY_100M)}: single_run (job 213191), "
          "median(reps[*].vmhwm_kb) / 1e6, GB")
    m.add("recB7ManifestBytes100M", tex_num(manifest100_bytes),
          f"{relpath(B7_BUILD_100M)}: build_info.store_bytes.manifest_bytes")
    m.add("recB7SegmentBytes100M", f"{segment100_gb:.3f}",
          f"{relpath(B7_BUILD_100M)}: build_info.store_bytes.segment_bytes / 1e9, GB")
    m.add("recB7CheckFullWall100M", f"{check100_wall:.3f}",
          f"{relpath(B7_CHECK_FULL_100M)}: single_run (job 213190), wall_s")
    m.add("recB7RecoveryCe5000At100M", tex_float(recovery100_5000_s),
          f"{relpath(B7_RECOVERY_100M_CE5000)}: replay_wall_s, rounded 1dp, seconds "
          "(compact_every=5000, job 213192); digest_compare.digest_equal true")
    m.add("recB7RecoveryCe5000MissPctAt100M", tex_num(recovery100_miss_pct_rounded),
          f"{relpath(B7_RECOVERY_100M_CE5000)}: replay_wall_s / (band_h[1] * 3600s) - 1, "
          f"%, rounded to the nearest percent ({recovery100_miss_pct:.1f}% before rounding) "
          "-- the 100M cadence-5000 control's own miss against its 6h upper bound "
          "(eval.tex's \"a 5% miss rather than a refutation\")")
    m.add("recB7Recovery100M", tex_float(recovery100_5000_s),
          f"ALIAS of recB7RecoveryCe5000At100M -- {relpath(B7_RECOVERY_100M_CE5000)}'s "
          "own protocol_note states there is no cadence-500 100M run (Addendum 6 "
          "pre-judged it infeasible, ~45h extrapolated, over the 2-day Slurm wall, and "
          "would only re-measure D-164 again); the §4 record layout has no separate "
          "recovery-100m.json for a base cadence, so this macro reuses the campaign's "
          "single 100M recovery measurement rather than staying pending for a run that "
          "was never planned")
    for op_id, val in executed100_p50_by_op.items():
        frag = B7_SCALE_CURVE_OPS[op_id]
        m.add(f"recB7ScaleCurveP50{frag}100M", tex_float(val),
              f"{relpath(B7_SCALE_CURVE_100M)}: per_operator_p50_ms.{op_id}."
              "measured_p50_ms, ms (single run, job 213190), recomputed from "
              f"{relpath(B7_SCALE_CURVE_100M_RAW)}'s own timings_ms")
    m.add_pending("recB7ScaleCurveP50ReachWindow100M", "B7 (100M, cost guardrail)",
                  f"refused by the cost guardrail (time_est_ms {estimate100_ms}) -- "
                  f"{relpath(B7_SCALE_CURVE_100M)}: reach_window_admission.run, anticipated "
                  "by Addendum 5's restated reach.window prediction (the only refusal the "
                  "pre-registration foresaw); see recB7ReachWindowRefused100M and "
                  "recB7ReachWindowEstimateMs100M")
    for op_id in B7_SCALE_CURVE_100M_REFUSED_NO_ESTIMATE:
        frag = B7_SCALE_CURVE_OPS[op_id]
        m.add(f"recB7Refused{frag}100M", "true",
              f"{relpath(B7_SCALE_CURVE_100M)}: per_operator_p50_ms.{op_id}.error = "
              f"{pops100[op_id]['error']!r} -- refused by the cost guardrail with no "
              "numeric time_est_ms in the record (unlike reach.window's dedicated "
              "admission probe); NOT anticipated by SCALE_BUILD_FORECAST_2026-09-15.md "
              "(a new finding at 100M -- all 13 operators executed at 30M). No "
              "recB7ScaleCurveP50 macro is emitted for this operator at 100M: no "
              "measured p50 exists and no estimate exists to explain a PENDING one")
    m.add("recB7ReachWindowRefused100M", "true",
          f"{relpath(B7_SCALE_CURVE_100M)}: reach_window_admission.run.admitted is false "
          "(time_est_ms=14,571 > the 10,000 ceiling) -- unlike 30M (admitted), 100M's "
          "reach.window is refused, exactly as Addendum 5's restated admission policy "
          "predicted for this scale")
    m.add("recB7QueryFloor100M", f"{query100_floor_gb:.2f}",
          f"{relpath(B7_QUERYFLOOR_100M)}: vmhwm_kb / 1e6, GB (fresh read-only process, "
          "cold-open, job 213189, n_ok=6/13 -- 7 operators refused even at the "
          "query-ready-floor probe)")
    m.add("recB7BuildSteadyDecileMedian100M", tex_float(round(decile100_median, 1)),
          f"{relpath(B7_BUILD_100M)}: recomputed median(ops_per_s_by_decile[*].ops_per_s) "
          "-- build_info.steady_decile_median_ops_per_s itself is null for this run "
          "(ops_per_s_by_decile_source flags it), cross-checked instead against "
          "falsifiers.steady_decile_ops_per_s.measured, ops/s")
    m.add("recB7ReachWindowEstimateMs100M", tex_num(estimate100_ms),
          f"{relpath(B7_SCALE_CURVE_100M)}: reach_window_admission.run.estimate."
          "time_est_ms, ms")
    m.add("recB7CompactionShare100M", f"{compaction_only_pct:.2f}",
          f"{relpath(B7_BUILD_COMPACTION_WALLS_100M)}: compaction_only_share_of_wall_pct "
          "= 100 * (in_loop_compaction_overhead_estimate_s [checkpoint-delta-pair "
          "estimate over the 100 in-loop compactions, method field quoted below] + "
          "final_compaction_wall_s_authoritative [the 101st compaction, separately and "
          "authoritatively timed in build_info.finalisation_phases]) / total_wall_s. "
          f"method: {walls100['method']!r}. A wider "
          f"compaction_plus_all_finalization_share_of_wall_pct = "
          f"{walls100['compaction_plus_all_finalization_share_of_wall_pct']}% (adds gc+"
          "stats+digest) is also on record but not emitted as a separate macro")

    # recB7300MGate already covers both scales (scale-independent) --
    # see the 30M section above.


# --------------------------------------------------------------------------
# external baselines (Neo4j 5 recompute / differential-dataflow IVM /
# TGMS same-host control) -- landed 2026-10-08 (Lane C1-records);
# scored here (Lane W2ad) against benchmarks/external-v1/README.md's own
# A11 ruling
# --------------------------------------------------------------------------

def _external_cell_id(store: str, mix: str, age: str | None, n: int, seed: int) -> str:
    """The storm-v2 cell-id convention every external-v1/storm-v1 record
    shares: ``<store>-<mix>-<age-or-none>-n<n_artifacts>-s<seed>``. Used to
    join a storm-v2-main-grid/probe row (keyed by ``config.{store,mix,
    age,n_artifacts,seed}``) against the external-v1 records' own
    ``cell_id`` strings."""
    return f"{store}-{mix}-{age or 'none'}-n{n}-s{seed}"


def compute_external_baselines(m: Macros) -> None:
    """Three external-baseline campaigns, all run against the same 43-cell
    export of the storm-v2 correction-storm workload (see
    benchmarks/external-v1/README.md): Neo4j 5.26 full recompute (lane
    N1), differential-dataflow incremental view maintenance (lane D1),
    and a TGMS same-host control re-run of the committed storm-v2 harness
    on the same xzgpu box (lane T1, 19 cells, all seed 0). All three
    landed 2026-10-08.

    Every ratio macro below reads the COMMITTED-cluster TGMS number
    (benchmarks/storm-v1/storm-v2-main-grid-2026-09-15.json + its r18
    probe, both measured on iTiger) for its denominator only where the
    README's own A11 ruling asks for it (the 18 synth-iv-60k main-grid
    cells at equality L2-partial, scored against P-EXT1(a); and the
    probe). Every other ratio macro -- SpeedupLOne*, RatioGrSameHostMedian,
    and every Ext2 band ratio -- reads the SAME-HOST tgms-control record
    instead, per that same A11 note: the committed cluster row was
    measured on a different host (iTiger) than the three external
    configurations (xzgpu), so a cross-host ratio is never used where a
    same-host one is available. ``scripts/external_record.py``'s own
    ``predictions_measured`` block (inside each record) only ever scores
    against the committed cluster rows -- applying A11's same-host
    substitution is explicitly this generator's job, not that
    assembler's (see the README's "Macro-stub landing" section), so the
    same-host figures below have no record-side aggregate to cross-check
    against; every one is instead cross-checked by recomputing each
    per-cell ratio independently of the median/min/max reduction, and
    (where the population matches, i.e. the 18 L2-partial cells + probe
    read against the committed cluster) against the record's own
    ``predictions_measured.ext{1,2}.per_cell[*].ratio_gr``/``c_withheld``
    fields.
    """
    sums = _sha256sums_by_name(EXTERNAL_V1_SHA256SUMS.read_text(encoding="utf-8"))
    for path in (EXTERNAL_NEO4J, EXTERNAL_NEO4J_ROWS, EXTERNAL_IVM, EXTERNAL_IVM_ROWS,
                 EXTERNAL_TGMS_CONTROL):
        eq(sha256_file(path), sums.get(path.name),
           f"{relpath(path)}: sha256 matches {relpath(EXTERNAL_V1_SHA256SUMS)}'s entry for "
           f"{path.name}")

    neo = json.loads(EXTERNAL_NEO4J.read_text(encoding="utf-8"))
    neo_rows = load_jsonl(EXTERNAL_NEO4J_ROWS)
    ivm = json.loads(EXTERNAL_IVM.read_text(encoding="utf-8"))
    ivm_rows = load_jsonl(EXTERNAL_IVM_ROWS)
    ctrl = json.loads(EXTERNAL_TGMS_CONTROL.read_text(encoding="utf-8"))

    eq(neo["n_cells"], 43, f"{relpath(EXTERNAL_NEO4J)}: n_cells")
    eq(len(neo["summary"]["per_cell"]), 43,
       f"{relpath(EXTERNAL_NEO4J)}: len(summary.per_cell)")
    eq(len(neo_rows), 43, f"{relpath(EXTERNAL_NEO4J_ROWS)}: line count")
    eq(ivm["n_cells"], 43, f"{relpath(EXTERNAL_IVM)}: n_cells")
    eq(len(ivm["summary"]["per_cell"]), 43,
       f"{relpath(EXTERNAL_IVM)}: len(summary.per_cell)")
    eq(len(ivm_rows), 43, f"{relpath(EXTERNAL_IVM_ROWS)}: line count")
    eq(ctrl["n_cells"], 19, f"{relpath(EXTERNAL_TGMS_CONTROL)}: n_cells")
    eq(len(ctrl["summary"]["per_cell"]), 19,
       f"{relpath(EXTERNAL_TGMS_CONTROL)}: len(summary.per_cell)")

    neo_pc = {c["cell_id"]: c for c in neo["summary"]["per_cell"]}
    ivm_pc = {c["cell_id"]: c for c in ivm["summary"]["per_cell"]}
    ctrl_pc = {c["cell_id"]: c for c in ctrl["summary"]["per_cell"]}
    eq(len(neo_pc), 43, f"{relpath(EXTERNAL_NEO4J)}: distinct cell_ids")
    eq(len(ivm_pc), 43, f"{relpath(EXTERNAL_IVM)}: distinct cell_ids")
    eq(len(ctrl_pc), 19, f"{relpath(EXTERNAL_TGMS_CONTROL)}: distinct cell_ids")

    PROBE_CID = "synth-iv-60k-c1-none-n10000-s0"
    AGE_BAND_CELLS = {
        f"{store}-c3-{age}-n1000-s0"
        for store in ("collegemsg", "synth-iv-60k")
        for age in ("recent", "hours", "days")
    }
    eq(len(AGE_BAND_CELLS), 6, "external-v1: 2 stores x 3 new (A2) age bands == 6 age cells")

    # --- storm-v2 committed cluster rows (iTiger), read fresh here rather
    # than relying on compute_c7_storm_v2 having already run -- a light
    # structural check only (that function's own digest/gate checks are
    # not duplicated here). ---
    sv2_rows = load_jsonl(STORM_V2_MAIN_GRID_ROWS)
    eq(len(sv2_rows), 36, f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: line count")
    sv2_pc: dict[str, dict] = {}
    for r in sv2_rows:
        cfg = r["config"]
        cid = _external_cell_id(cfg["store"], cfg["mix"], cfg["age"], cfg["n_artifacts"],
                                 cfg["seed"])
        sv2_pc[cid] = r
    eq(len(sv2_pc), 36, f"{relpath(STORM_V2_MAIN_GRID_ROWS)}: distinct derived cell_ids")

    probe = json.loads(STORM_V2_R18_PROBE.read_text(encoding="utf-8"))
    probe_cid = _external_cell_id(probe["config"]["store"], probe["config"]["mix"],
                                   probe["config"]["age"], probe["config"]["n_artifacts"],
                                   probe["config"]["seed"])
    eq(probe_cid, PROBE_CID, f"{relpath(STORM_V2_R18_PROBE)}: derived cell_id matches the "
       "external-v1 probe cell_id")

    # ======================================================================
    # Ext1 -- Neo4j 5.26 full recompute
    # ======================================================================

    eq(len(neo["config"]["cell_ids"]), 43, f"{relpath(EXTERNAL_NEO4J)}: config.cell_ids count")

    agree_cells = sum(1 for c in neo_pc.values() if c["oracle_agreement"]["disagree"] == 0)
    disagree_total = sum(c["oracle_agreement"]["disagree"] for c in neo_pc.values())
    pm1 = neo["summary"]["predictions_measured"]["ext1"]
    eq(disagree_total, pm1["d_disagree_total"],
       f"{relpath(EXTERNAL_NEO4J)}: sum(summary.per_cell[*].oracle_agreement.disagree) matches "
       "summary.predictions_measured.ext1.d_disagree_total")
    eq(disagree_total, 195, "external-v1 frozen: neo4j-recompute total disagreeing answers "
       "(README.md Oracle-agreement summary: 195, all storm-000911 in synth-iv-60k seed 0)")
    eq(agree_cells, 43 - 10, "external-v1 frozen: neo4j-recompute cells with total oracle "
       "agreement (33 = 43 - the 10 synth-iv-60k seed-0 cells that disagree, per README.md)")

    neo_versions = {c["versions"]["neo4j_version"] for c in neo_pc.values()}
    eq(neo_versions, {"5.26.0"}, f"{relpath(EXTERNAL_NEO4J)}: summary.per_cell[*]."
       "versions.neo4j_version is uniform across all 43 cells")
    neo_version = next(iter(neo_versions))
    neo_version_major_minor = ".".join(neo_version.split(".")[:2])
    eq(neo_version_major_minor, "5.26", f"{relpath(EXTERNAL_NEO4J)}: neo4j_version's "
       "major.minor (5.26.0 -> 5.26)")

    # Families / IteratedFamilies: the 13 Cypher query families live in
    # each row's own result.config.cypher dict (the top-level per_cell
    # summary carries no family list) -- checked uniform across all 43
    # rows, not read from one row alone.
    cypher_keysets = {frozenset(r["result"]["config"]["cypher"].keys()) for r in neo_rows}
    eq(len(cypher_keysets), 1, f"{relpath(EXTERNAL_NEO4J_ROWS)}: result.config.cypher key set "
       "is uniform across all 43 rows")
    cypher_families = next(iter(cypher_keysets))
    eq(len(cypher_families), 13, f"{relpath(EXTERNAL_NEO4J_ROWS)}: result.config.cypher family "
       "count")
    require("temporal_reachability" in cypher_families,
            f"{relpath(EXTERNAL_NEO4J_ROWS)}: temporal_reachability is one of the 13 families")
    # external/neo4j-recompute/neo4j_recompute/reachability.py's own module
    # docstring is the source for "client-driven iterated Cypher" -- quoted
    # rather than asserted from this generator's own prose.
    reachability_doc = (ROOT / "external" / "neo4j-recompute" / "neo4j_recompute"
                         / "reachability.py").read_text(encoding="utf-8")
    require("client-driven iterated Cypher" in reachability_doc,
            "external/neo4j-recompute/neo4j_recompute/reachability.py: module docstring "
            "names temporal_reachability as client-driven iterated Cypher")

    # RatioGr{Min,Median,Max}: the 18 synth-iv-60k N=1,000 main-grid cells
    # at export equality L2-partial (README.md's per-cell equality table),
    # against the COMMITTED cluster row for the same cell_id (A11: scored
    # only on the cells that reproduce the committed campaign).
    l2partial_synth_cells = sorted(
        c for c, cell in neo_pc.items()
        if c.startswith("synth-iv-60k-") and cell["equality_level"] == "L2-partial"
        and c != PROBE_CID)
    eq(len(l2partial_synth_cells), 18, "external-v1: synth-iv-60k cells at export equality "
       "L2-partial (excluding the N=10,000 probe, which is L2 and scored separately as "
       "recExt1ProbeRatio)")
    for c in l2partial_synth_cells:
        require(c in sv2_pc, f"{relpath(STORM_V2_MAIN_GRID)}: has a committed row for {c}")

    ratio_gr = {}
    for c in l2partial_synth_cells:
        neo_med = neo_pc[c]["refresh_wall_ms"]["median"]
        committed_gr = sv2_pc[c]["summary"]["arms"]["global-recompute"]["ttf_p50_ms"]
        ratio_gr[c] = neo_med / committed_gr
        # cross-check against the record's own self-reported per-cell
        # ratio_gr (computed by scripts/external_record.py against the
        # same committed row) -- both sides read the committed cluster
        # row independently, so this is a real cross-check, not a
        # tautology.
        close(ratio_gr[c], pm1["per_cell"][c]["ratio_gr"], 1e-9,
              f"external-v1 cell {c}: recomputed ratio_gr matches "
              f"{relpath(EXTERNAL_NEO4J)}'s own predictions_measured.ext1.per_cell entry")
    ratio_gr_min = min(ratio_gr.values())
    ratio_gr_median = statistics.median(ratio_gr.values())
    ratio_gr_max = max(ratio_gr.values())

    # ProbeRatio: the N=10,000 probe cell, against the committed probe
    # record (not the main grid).
    probe_neo_med = neo_pc[PROBE_CID]["refresh_wall_ms"]["median"]
    probe_committed_gr = probe["summary"]["arms"]["global-recompute"]["ttf_p50_ms"]
    probe_ratio = probe_neo_med / probe_committed_gr
    close(probe_ratio, pm1["c_probe_ratio_gr"], 1e-9,
          f"external-v1 probe cell: recomputed ratio_gr matches {relpath(EXTERNAL_NEO4J)}'s "
          "own predictions_measured.ext1.c_probe_ratio_gr")

    # RatioGrSameHostMedian: the 9 synth-iv-60k cells the neo4j-recompute
    # and tgms-control records share, excluding the probe (A11 same-host
    # substitution -- no equality-level restriction here, since
    # tgms-control IS the same-host reference, not a cross-host one being
    # validated against).
    samehost_synth_cells = sorted(
        c for c in ctrl_pc if c.startswith("synth-iv-60k-") and c != PROBE_CID)
    eq(len(samehost_synth_cells), 9, "external-v1: synth-iv-60k cells shared by "
       "neo4j-recompute and tgms-control, excluding the probe")
    for c in samehost_synth_cells:
        require(c in neo_pc, f"{relpath(EXTERNAL_NEO4J)}: has cell {c}")
    samehost_ratios = [neo_pc[c]["refresh_wall_ms"]["median"]
                        / ctrl_pc[c]["arms"]["global-recompute"]["ttf_p50_ms"]
                        for c in samehost_synth_cells]
    ratio_gr_samehost_median = statistics.median(samehost_ratios)

    # SpeedupLOne{Median,Min}: the 19 tgms-control cells minus the probe
    # (18 cells), against tgms-control's own tgms-L1 arm (same host).
    control_nonprobe_cells = sorted(c for c in ctrl_pc if c != PROBE_CID)
    eq(len(control_nonprobe_cells), 18, "external-v1: tgms-control cells excluding the probe")
    speedup_l1 = {}
    for c in control_nonprobe_cells:
        require(c in neo_pc, f"{relpath(EXTERNAL_NEO4J)}: has cell {c}")
        neo_med = neo_pc[c]["refresh_wall_ms"]["median"]
        l1 = ctrl_pc[c]["arms"]["tgms-L1"]["ttf_p50_ms"]
        speedup_l1[c] = neo_med / l1
    speedup_l1_median = statistics.median(speedup_l1.values())
    speedup_l1_min = min(speedup_l1.values())

    # SpeedupCellsMeeting / (sibling) SpeedupCellsScored: of the 18
    # SpeedupLOne cells, those with a committed storm-v2-main-grid
    # counterpart (12 -- the 6 age-banded cells per store/mix have none,
    # per README.md A2) are "scored"; of those, count how many meet half
    # the committed internal speedup (global-recompute/tgms-L1 ttf_p50_ms
    # ratio) for the same cell_id.
    scored_cells = [c for c in control_nonprobe_cells if c in sv2_pc]
    excluded_cells = [c for c in control_nonprobe_cells if c not in sv2_pc]
    eq(len(scored_cells) + len(excluded_cells), 18,
       "external-v1: every SpeedupLOne cell is either scored or excluded, no double count")
    eq(len(scored_cells), 12, "external-v1: SpeedupLOne cells with a committed storm-v2 "
       "counterpart (the 6 age-banded cells per store/mix, 3 per store, have none)")
    eq(sorted(excluded_cells), sorted(AGE_BAND_CELLS),
       "external-v1: the excluded (unscored) SpeedupLOne cells are exactly the 6 new (A2) "
       "age-banded cells")
    meeting = 0
    for c in scored_cells:
        committed_speedup = (sv2_pc[c]["summary"]["arms"]["global-recompute"]["ttf_p50_ms"]
                              / sv2_pc[c]["summary"]["arms"]["tgms-L1"]["ttf_p50_ms"])
        if speedup_l1[c] >= 0.5 * committed_speedup:
            meeting += 1

    recompute_median_s_cells = sorted(set(neo_pc) - AGE_BAND_CELLS - {PROBE_CID})
    eq(len(recompute_median_s_cells), 36, "external-v1: neo4j-recompute main-grid N=1,000 "
       "cells (43 - 6 age-banded - 1 probe)")
    recompute_median_ms = statistics.median(
        neo_pc[c]["refresh_wall_ms"]["median"] for c in recompute_median_s_cells)
    recompute_median_s = recompute_median_ms / 1000

    m.add("recExt1Cells", 43, f"{relpath(EXTERNAL_NEO4J)}: n_cells / len(summary.per_cell)")
    m.add("recExt1AgreeCells", agree_cells,
          f"{relpath(EXTERNAL_NEO4J)}: count of summary.per_cell[*] with "
          "oracle_agreement.disagree == 0")
    m.add("recExt1Disagreements", disagree_total,
          f"{relpath(EXTERNAL_NEO4J)}: sum(summary.per_cell[*].oracle_agreement.disagree), "
          "cross-checked against summary.predictions_measured.ext1.d_disagree_total")
    m.add("recExt1Families", len(cypher_families),
          f"{relpath(EXTERNAL_NEO4J_ROWS)}: len(result.config.cypher), uniform over all 43 "
          "rows")
    m.add("recExt1IteratedFamilies", 1,
          "external/neo4j-recompute/neo4j_recompute/reachability.py's own module docstring: "
          "exactly one family (temporal_reachability, F11) is client-driven iterated Cypher; "
          "no other family in result.config.cypher is")
    m.add("recExt1NeoVersion", neo_version_major_minor,
          f"{relpath(EXTERNAL_NEO4J)}: summary.per_cell[*].versions.neo4j_version's "
          "major.minor, uniform over all 43 cells (5.26.0 -> 5.26)")
    m.add("recExt1RecomputeMedianS", f"{recompute_median_s:.1f}",
          f"{relpath(EXTERNAL_NEO4J)}: median(summary.per_cell[*].refresh_wall_ms.median) over "
          "the 36 main-grid N=1,000 cells (43 - 6 age-banded - 1 probe), ms/1000")
    m.add("recExt1RatioGrMin", f"{ratio_gr_min:.2f}",
          f"{relpath(EXTERNAL_NEO4J)}: min over the 18 synth-iv-60k L2-partial cells of "
          f"summary.per_cell[*].refresh_wall_ms.median / {relpath(STORM_V2_MAIN_GRID_ROWS)}'s "
          "summary.arms.global-recompute.ttf_p50_ms for the same cell_id")
    m.add("recExt1RatioGrMedian", f"{ratio_gr_median:.2f}",
          f"{relpath(EXTERNAL_NEO4J)}: median over the same 18 cells of the same ratio")
    m.add("recExt1RatioGrMax", f"{ratio_gr_max:.2f}",
          f"{relpath(EXTERNAL_NEO4J)}: max over the same 18 cells of the same ratio")
    m.add("recExt1RatioGrSameHostMedian", f"{ratio_gr_samehost_median:.2f}",
          f"{relpath(EXTERNAL_NEO4J)}: median over the 9 synth-iv-60k cells shared with "
          f"{relpath(EXTERNAL_TGMS_CONTROL)} (excluding the probe) of refresh_wall_ms.median / "
          "tgms-control's arms.global-recompute.ttf_p50_ms for the same cell_id -- the "
          "same-host (xzgpu) reference, per README.md's A11 ruling, not the committed "
          "iTiger-cluster row")
    m.add("recExt1ProbeRatio", f"{probe_ratio:.2f}",
          f"{relpath(EXTERNAL_NEO4J)}: refresh_wall_ms.median at {PROBE_CID} / "
          f"{relpath(STORM_V2_R18_PROBE)}'s arms.global-recompute.ttf_p50_ms")
    m.add("recExt1SpeedupLOneMedian", f"{speedup_l1_median:.2f}",
          f"{relpath(EXTERNAL_NEO4J)}: median over the 18 tgms-control cells (excluding the "
          f"probe) of refresh_wall_ms.median / {relpath(EXTERNAL_TGMS_CONTROL)}'s "
          "arms.tgms-L1.ttf_p50_ms for the same cell_id")
    m.add("recExt1SpeedupLOneMin", f"{speedup_l1_min:.2f}",
          f"{relpath(EXTERNAL_NEO4J)}: min over the same 18 cells of the same ratio")
    m.add("recExt1SpeedupCellsScored", len(scored_cells),
          f"{relpath(EXTERNAL_TGMS_CONTROL)}: of the 18 SpeedupLOne cells, those with a "
          f"committed {relpath(STORM_V2_MAIN_GRID)} counterpart for the same cell_id -- a "
          "sibling of recExt1SpeedupCellsMeeting giving its denominator (the 6 new (A2) "
          "age-banded cells have no committed counterpart and are excluded, not scored as 0)")
    m.add("recExt1SpeedupCellsMeeting", meeting,
          f"{relpath(EXTERNAL_TGMS_CONTROL)}: of the recExt1SpeedupCellsScored=={len(scored_cells)} "
          "scored cells, those where (neo4j refresh_wall_ms.median / tgms-control "
          f"tgms-L1.ttf_p50_ms) >= 0.5 x the committed internal speedup (global-recompute / "
          f"tgms-L1 ttf_p50_ms) from {relpath(STORM_V2_MAIN_GRID)} for the same cell_id")

    # ======================================================================
    # Ext2 -- differential-dataflow incremental view maintenance
    # ======================================================================

    eq(len(ivm["config"]["cell_ids"]), 43, f"{relpath(EXTERNAL_IVM)}: config.cell_ids count")
    ivm_agree_cells = sum(1 for c in ivm_pc.values() if c["oracle_agreement"]["disagree"] == 0)
    pm2 = ivm["summary"]["predictions_measured"]["ext2"]
    eq(pm2["d_disagree_total"], 0, f"{relpath(EXTERNAL_IVM)}: "
       "summary.predictions_measured.ext2.d_disagree_total")
    eq(ivm_agree_cells, 43, f"{relpath(EXTERNAL_IVM)}: count of summary.per_cell[*] with "
       "oracle_agreement.disagree == 0 (all 43 agree, matching d_disagree_total == 0)")

    dd_versions = {c["versions"]["differential_dataflow"] for c in ivm_pc.values()}
    eq(dd_versions, {"0.25.1"}, f"{relpath(EXTERNAL_IVM)}: summary.per_cell[*].versions."
       "differential_dataflow is uniform across all 43 cells")
    dd_version = next(iter(dd_versions))

    fam_sets = {frozenset(r["result"]["families_present"]) for r in ivm_rows}
    eq(len(fam_sets), 1, f"{relpath(EXTERNAL_IVM_ROWS)}: result.families_present is uniform "
       "across all 43 rows")
    ivm_families = next(iter(fam_sets))
    eq(len(ivm_families), 13, f"{relpath(EXTERNAL_IVM_ROWS)}: result.families_present count")

    workers = {r["result"]["config"]["workers"] for r in ivm_rows}
    eq(workers, {1}, f"{relpath(EXTERNAL_IVM_ROWS)}: result.config.workers is uniform (1) "
       "across all 43 rows -- the dataflow CLI hard-codes workers=1 (README.md's own "
       "\"Known contradictions\" note: the planned 8-worker rerun never ran)")

    ivm_refresh_median_cells = sorted(set(ivm_pc) - AGE_BAND_CELLS - {PROBE_CID})
    eq(len(ivm_refresh_median_cells), 36, "external-v1: ivm-differential main-grid N=1,000 "
       "cells (43 - 6 age-banded - 1 probe)")
    ivm_refresh_median_ms = statistics.median(
        ivm_pc[c]["refresh_wall_ms"]["median"] for c in ivm_refresh_median_cells)

    # Ratio{Recent,Hours,Days,Deep}: each band's matching cells' own ratio
    # of (ivm per-cell refresh_wall_ms.median) / (tgms-control tgms-L1
    # ttf_p50_ms for the same cell_id), median over the band's cells --
    # NOT summary.predictions_measured.ext2.a_b_per_band, which pools
    # bursts across cells and reads the committed cluster row (null for
    # recent/hours/days, since no committed row exists for those cells);
    # this generator's own same-host substitution per README.md's A11.
    bands = {
        "recent": [f"{store}-c3-recent-n1000-s0" for store in ("collegemsg", "synth-iv-60k")],
        "hours": [f"{store}-c3-hours-n1000-s0" for store in ("collegemsg", "synth-iv-60k")],
        "days": [f"{store}-c3-days-n1000-s0" for store in ("collegemsg", "synth-iv-60k")],
        "deep": [f"{store}-{mix}-deep-n1000-s0"
                 for store in ("collegemsg", "synth-iv-60k") for mix in ("c1", "c3", "c4")],
    }
    eq({k: len(v) for k, v in bands.items()}, {"recent": 2, "hours": 2, "days": 2, "deep": 6},
       "external-v1: Ext2 band cell counts (2 c3 age cells per recent/hours/days band, 6 "
       "seed-0 deep cells across all 3 mixes/2 stores)")
    band_ratio: dict[str, float] = {}
    for band, cells in bands.items():
        for c in cells:
            require(c in ivm_pc, f"{relpath(EXTERNAL_IVM)}: has cell {c}")
            require(c in ctrl_pc, f"{relpath(EXTERNAL_TGMS_CONTROL)}: has cell {c}")
        ratios = [ivm_pc[c]["refresh_wall_ms"]["median"]
                  / ctrl_pc[c]["arms"]["tgms-L1"]["ttf_p50_ms"] for c in cells]
        band_ratio[band] = statistics.median(ratios)

    crossover_band = "none"
    for band in ("recent", "hours", "days", "deep"):
        if band_ratio[band] >= 1:
            crossover_band = band
            break

    # The withheld-correction cell (synth-iv-60k-c4-deep-n1000-s0, not one
    # of the 43 scored cells): summary.predictions_measured.ext2.c_withheld.
    withheld = pm2["c_withheld"]
    eq(withheld["cell_id"], "synth-iv-60k-c4-deep-n1000-s0",
       f"{relpath(EXTERNAL_IVM)}: predictions_measured.ext2.c_withheld.cell_id")
    withheld_ivm_false_fresh = withheld["ivm_f_epoch_false_fresh"]
    eq(withheld_ivm_false_fresh, 39, f"{relpath(EXTERNAL_IVM)}: "
       "predictions_measured.ext2.c_withheld.ivm_f_epoch_false_fresh")
    withheld_watermark_false_fresh = withheld["ivm_f_watermark_false_fresh"]
    eq(withheld_watermark_false_fresh, 0, f"{relpath(EXTERNAL_IVM)}: "
       "predictions_measured.ext2.c_withheld.ivm_f_watermark_false_fresh")
    require(withheld["ivm_f_watermark_unanswerable"] is True,
            f"{relpath(EXTERNAL_IVM)}: c_withheld.ivm_f_watermark_unanswerable is true -- the "
            "watermark-variant probe never completes through batch 10, so its false_fresh=0 "
            "is reported as-is per the task's own fallback rule, not treated as a clean pass")
    eq(withheld["tgms_false_fresh"], None,
       f"{relpath(EXTERNAL_IVM)}: c_withheld.tgms_false_fresh is null (not supplied to lane "
       "D1's check run) -- falls back to the tgms-control record's own withheld-cell field")
    withheld_tgms_false_fresh = ctrl_pc[withheld["cell_id"]]["arms"]["tgms-L1"]["false_fresh"]
    eq(withheld_tgms_false_fresh, 0, f"{relpath(EXTERNAL_TGMS_CONTROL)}: "
       f"summary.per_cell[{withheld['cell_id']!r}].arms.tgms-L1.false_fresh (fallback source "
       "for recExt2WithheldFalseFreshTgms, since the withheld section's own tgms_false_fresh "
       "is null)")

    m.add("recExt2Cells", 43, f"{relpath(EXTERNAL_IVM)}: n_cells / len(summary.per_cell)")
    m.add("recExt2AgreeCells", ivm_agree_cells,
          f"{relpath(EXTERNAL_IVM)}: count of summary.per_cell[*] with "
          "oracle_agreement.disagree == 0, cross-checked against "
          "summary.predictions_measured.ext2.d_disagree_total == 0")
    m.add("recExt2Families", len(ivm_families),
          f"{relpath(EXTERNAL_IVM_ROWS)}: len(result.families_present), uniform over all 43 "
          "rows")
    m.add("recExt2DdVersion", dd_version,
          f"{relpath(EXTERNAL_IVM)}: summary.per_cell[*].versions.differential_dataflow, "
          "uniform over all 43 cells -- the differential-dataflow crate version the ivm-dd "
          "binary (built from public main 79e79c7b, see README.md) links against; NOT "
          "versions.crate_version (a static \"0.1.0\", the ivm-dd wrapper crate's own "
          "never-bumped Cargo.toml version, not informative here)")
    m.add("recExt2Workers", next(iter(workers)),
          f"{relpath(EXTERNAL_IVM_ROWS)}: result.config.workers, uniform (1) over all 43 rows")
    m.add("recExt2RefreshMedianMs", f"{ivm_refresh_median_ms:.1f}",
          f"{relpath(EXTERNAL_IVM)}: median(summary.per_cell[*].refresh_wall_ms.median) over "
          "the 36 main-grid N=1,000 cells (43 - 6 age-banded - 1 probe)")
    for band in ("recent", "hours", "days", "deep"):
        ratio = band_ratio[band]
        value = f"{ratio:.3f}" if ratio >= 0.01 else sci_3sf(ratio)
        m.add(f"recExt2Ratio{band.capitalize()}", value,
              f"{relpath(EXTERNAL_IVM)}: median over the band's "
              f"{len(bands[band])} cell(s) of refresh_wall_ms.median / "
              f"{relpath(EXTERNAL_TGMS_CONTROL)}'s arms.tgms-L1.ttf_p50_ms for the same "
              "cell_id (same-host substitution, not "
              "predictions_measured.ext2.a_b_per_band, which reads the committed cluster row "
              "and is null for recent/hours/days)")
    m.add("recExt2CrossoverBand", crossover_band,
          "derived: the first band in order recent -> hours -> days -> deep whose "
          "recExt2Ratio{Band} >= 1, else the literal \"none\" -- all four bands are < 1 here")
    m.add("recExt2WithheldFalseFreshIvm", withheld_ivm_false_fresh,
          f"{relpath(EXTERNAL_IVM)}: predictions_measured.ext2.c_withheld."
          "ivm_f_epoch_false_fresh, at the withheld cell synth-iv-60k-c4-deep-n1000-s0")
    m.add("recExt2WithheldFalseFreshWatermark", withheld_watermark_false_fresh,
          f"{relpath(EXTERNAL_IVM)}: predictions_measured.ext2.c_withheld."
          "ivm_f_watermark_false_fresh -- reported as-is per the task's own fallback rule, "
          "though the F-watermark probe's own ivm_f_watermark_unanswerable is true (its probe "
          "never completes through batch 10), so this 0 is not a verified clean pass")
    m.add("recExt2WithheldFalseFreshTgms", withheld_tgms_false_fresh,
          f"{relpath(EXTERNAL_TGMS_CONTROL)}: summary.per_cell[*].arms.tgms-L1.false_fresh at "
          "synth-iv-60k-c4-deep-n1000-s0 -- fallback source, since "
          f"{relpath(EXTERNAL_IVM)}'s own c_withheld.tgms_false_fresh is null (not supplied to "
          "lane D1's check run)")
    m.add("recExt2UnanswerableMs", "not measured",
          f"{relpath(EXTERNAL_IVM)}'s predictions_measured.ext2.c_withheld carries only "
          "booleans (ivm_f_{epoch,watermark}_probe_complete_through_10) for the withheld "
          "cell, no timing field -- this number cannot be computed from the committed "
          "records; landed as the literal text \"not measured\" rather than improvised")


# --------------------------------------------------------------------------
# W2ae (partial) -- sum-mode time-to-fresh reconstruction, external-v1
# same-host control (module docstring's own W2ae section has the full
# story). None of compute_external_baselines's own macros above
# (recExt1SpeedupLOne{Median,Min}, recExt2Ratio{Band}, ...) are touched;
# every macro below is a new name, read against the committed
# EXTERNAL_NEO4J/EXTERNAL_IVM/EXTERNAL_TGMS_CONTROL files (sha256-gated
# the same way compute_external_baselines gates them -- re-checked here,
# not assumed, since this function can run on its own) plus the control's
# own committed per-batch rows (EXTERNAL_TGMS_CONTROL_BATCHES, read via
# _read_control_batches, its own independent sha256 gate).
# --------------------------------------------------------------------------

def compute_ext_sum_mode(m: Macros) -> None:
    sums = _sha256sums_by_name(EXTERNAL_V1_SHA256SUMS.read_text(encoding="utf-8"))
    for path in (EXTERNAL_NEO4J, EXTERNAL_IVM, EXTERNAL_TGMS_CONTROL):
        eq(sha256_file(path), sums.get(path.name),
           f"ext sum-mode: {relpath(path)} sha256 matches "
           f"{relpath(EXTERNAL_V1_SHA256SUMS)}'s entry for {path.name}")

    neo = json.loads(EXTERNAL_NEO4J.read_text(encoding="utf-8"))
    ivm = json.loads(EXTERNAL_IVM.read_text(encoding="utf-8"))
    ctrl = json.loads(EXTERNAL_TGMS_CONTROL.read_text(encoding="utf-8"))
    neo_pc = {c["cell_id"]: c for c in neo["summary"]["per_cell"]}
    ivm_pc = {c["cell_id"]: c for c in ivm["summary"]["per_cell"]}
    ctrl_pc = {c["cell_id"]: c for c in ctrl["summary"]["per_cell"]}
    eq(len(ctrl_pc), 19, f"ext sum-mode: {relpath(EXTERNAL_TGMS_CONTROL)} distinct cell_ids")

    control_batches = _read_control_batches()
    eq(set(control_batches), set(ctrl_pc), "ext sum-mode: "
       f"{relpath(EXTERNAL_TGMS_CONTROL_BATCHES)}'s cell_ids match "
       f"{relpath(EXTERNAL_TGMS_CONTROL)}'s own summary.per_cell cell_ids exactly")

    PROBE_CID = "synth-iv-60k-c1-none-n10000-s0"
    control_nonprobe_cells = sorted(c for c in ctrl_pc if c != PROBE_CID)
    eq(len(control_nonprobe_cells), 18, "ext sum-mode: non-probe tgms-control cell count")
    eq(ctrl_pc[PROBE_CID]["arms"]["global-recompute"].get("ttf_p50_ms") is not None, True,
       f"ext sum-mode: {relpath(EXTERNAL_TGMS_CONTROL)} probe cell {PROBE_CID} carries a "
       "global-recompute ttf_p50_ms (sanity: this cell is excluded from every macro below, "
       "not silently dropped for lacking one)")

    # Reproduction check (same discipline as compute_c7_storm_v2_sum_mode
    # above, now possible live since the raw rows are committed): for
    # each of the 18 end-to-end cells, recompute tgms-L1's and
    # global-recompute's end-to-end ttf_p50_ms from the raw per-batch
    # ttf_ms via the exact nearest-rank percentile tgms/eval/storm.py
    # itself uses, and require it reproduces the committed
    # arms.{tgms-L1,global-recompute}.ttf_p50_ms to within 0.5% before
    # trusting the sum-mode value computed from the same batches.
    sum_l1_p50: dict[str, float] = {}
    max_l1_dev = 0.0
    max_gr_dev = 0.0
    for c in control_nonprobe_cells:
        batches = control_batches[c]
        eq(len(batches), 20, f"control batches cell {c}: batch count")
        l1_check = [b["arms"]["tgms-L1"]["check_wall_ms"] for b in batches]
        l1_refresh = [b["arms"]["tgms-L1"]["refresh_wall_ms"] for b in batches]
        l1_ttf = [b["arms"]["tgms-L1"]["ttf_ms"] for b in batches]
        gr_ttf = [b["arms"]["global-recompute"]["ttf_ms"] for b in batches]

        recomputed_l1_ttf_p50 = _e2e_percentile(l1_ttf, 0.5)
        recomputed_gr_ttf_p50 = _e2e_percentile(gr_ttf, 0.5)
        committed_l1_ttf_p50 = ctrl_pc[c]["arms"]["tgms-L1"]["ttf_p50_ms"]
        committed_gr_ttf_p50 = ctrl_pc[c]["arms"]["global-recompute"]["ttf_p50_ms"]
        close_rel(recomputed_l1_ttf_p50, committed_l1_ttf_p50, 0.005,
                  f"ext sum-mode cell {c}: recomputed end-to-end tgms-L1 ttf_p50_ms "
                  f"reproduces {relpath(EXTERNAL_TGMS_CONTROL)}'s own "
                  "summary.per_cell[*].arms.tgms-L1.ttf_p50_ms")
        close_rel(recomputed_gr_ttf_p50, committed_gr_ttf_p50, 0.005,
                  f"ext sum-mode cell {c}: recomputed global-recompute ttf_p50_ms "
                  f"reproduces {relpath(EXTERNAL_TGMS_CONTROL)}'s own "
                  "summary.per_cell[*].arms.global-recompute.ttf_p50_ms")
        max_l1_dev = max(max_l1_dev,
                          abs(recomputed_l1_ttf_p50 - committed_l1_ttf_p50) / committed_l1_ttf_p50)
        max_gr_dev = max(max_gr_dev,
                          abs(recomputed_gr_ttf_p50 - committed_gr_ttf_p50) / committed_gr_ttf_p50)

        sum_vals = [cw + rf for cw, rf in zip(l1_check, l1_refresh)]
        sum_l1_p50[c] = _e2e_percentile(sum_vals, 0.5)

    close(max_l1_dev, 0.0, 0.0005, "ext sum-mode frozen: max relative deviation, recomputed "
          "vs. committed end-to-end tgms-L1 ttf_p50_ms, over the 18 control cells (the "
          "task's own 0.5% bar; this reproduces exactly)")
    close(max_gr_dev, 0.0, 0.0005, "ext sum-mode frozen: max relative deviation, recomputed "
          "vs. committed end-to-end global-recompute ttf_p50_ms, over the 18 control cells")

    # recExt1ControlSpeedupSumMedian: the control's OWN internal speedup
    # (global-recompute / sum-mode tgms-L1), median over the 18 cells --
    # the corrected sibling of each cell's own (uncorrected, end-to-end)
    # arms.speedup_global_recompute_over_l1 field, which no macro reads
    # directly anywhere in this generator.
    ctrl_speedup_sum = {
        c: ctrl_pc[c]["arms"]["global-recompute"]["ttf_p50_ms"] / sum_l1_p50[c]
        for c in control_nonprobe_cells
    }
    ctrl_speedup_sum_median = statistics.median(ctrl_speedup_sum.values())
    m.add("recExt1ControlSpeedupSumMedian", f"{ctrl_speedup_sum_median:.2f}",
          f"{relpath(EXTERNAL_TGMS_CONTROL_BATCHES)}: median over the 18 end-to-end cells of "
          "(arms.global-recompute.ttf_p50_ms / sum-mode tgms-L1 p50) -- the same-host "
          "internal speedup, sum-mode corrected (no existing macro reads the record's own "
          "end-to-end arms.speedup_global_recompute_over_l1 field directly)")

    # recExt1SpeedupLOneSum{Median,Min}: same definition as
    # compute_external_baselines's own recExt1SpeedupLOne{Median,Min}
    # (neo4j-recompute refresh_wall_ms.median / tgms-control's own
    # tgms-L1 time-to-fresh, same cell_id), but with the sum-mode
    # denominator.
    for c in control_nonprobe_cells:
        require(c in neo_pc, f"ext sum-mode: {relpath(EXTERNAL_NEO4J)} has cell {c}")
    speedup_l1_sum = {
        c: neo_pc[c]["refresh_wall_ms"]["median"] / sum_l1_p50[c]
        for c in control_nonprobe_cells
    }
    speedup_l1_sum_median = statistics.median(speedup_l1_sum.values())
    speedup_l1_sum_min = min(speedup_l1_sum.values())
    m.add("recExt1SpeedupLOneSumMedian", f"{speedup_l1_sum_median:.2f}",
          f"{relpath(EXTERNAL_NEO4J)}: median over the 18 tgms-control cells (excluding "
          f"the probe) of refresh_wall_ms.median / {relpath(EXTERNAL_TGMS_CONTROL_BATCHES)}'s "
          "sum-mode tgms-L1 p50 for the same cell_id -- the sum-mode-corrected sibling of "
          "recExt1SpeedupLOneMedian")
    m.add("recExt1SpeedupLOneSumMin", f"{speedup_l1_sum_min:.2f}",
          f"{relpath(EXTERNAL_NEO4J)}: min over the same 18 cells of the same ratio -- the "
          "sum-mode-corrected sibling of recExt1SpeedupLOneMin")

    # recExt2RatioSum{Recent,Hours,Days,Deep}: same band groupings as
    # compute_external_baselines's own recExt2Ratio{Band}, same
    # (ivm-differential refresh_wall_ms.median / tgms-control's own
    # tgms-L1 time-to-fresh) definition, but with the sum-mode
    # denominator.
    bands = {
        "recent": [f"{store}-c3-recent-n1000-s0" for store in ("collegemsg", "synth-iv-60k")],
        "hours": [f"{store}-c3-hours-n1000-s0" for store in ("collegemsg", "synth-iv-60k")],
        "days": [f"{store}-c3-days-n1000-s0" for store in ("collegemsg", "synth-iv-60k")],
        "deep": [f"{store}-{mix}-deep-n1000-s0"
                 for store in ("collegemsg", "synth-iv-60k") for mix in ("c1", "c3", "c4")],
    }
    eq({k: len(v) for k, v in bands.items()}, {"recent": 2, "hours": 2, "days": 2, "deep": 6},
       "ext sum-mode: Ext2 band cell counts (same as compute_external_baselines)")
    for band, cells in bands.items():
        for c in cells:
            require(c in ivm_pc, f"ext sum-mode: {relpath(EXTERNAL_IVM)} has cell {c}")
            require(c in sum_l1_p50, f"ext sum-mode: sum_l1_p50 has cell {c}")
        ratios = [ivm_pc[c]["refresh_wall_ms"]["median"] / sum_l1_p50[c] for c in cells]
        ratio_med = statistics.median(ratios)
        value = f"{ratio_med:.3f}" if ratio_med >= 0.01 else sci_3sf(ratio_med)
        m.add(f"recExt2RatioSum{band.capitalize()}", value,
              f"{relpath(EXTERNAL_IVM)}: median over the band's {len(cells)} cell(s) of "
              f"refresh_wall_ms.median / {relpath(EXTERNAL_TGMS_CONTROL_BATCHES)}'s sum-mode "
              f"tgms-L1 p50 for the same cell_id -- the sum-mode-corrected sibling of "
              f"recExt2Ratio{band.capitalize()}")


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
    # recTtfSpeedup, recStormCells, recStormFalseFresh,
    # recStormSpeedupN1k, recStormAvoidedN1k -- are retired: superseded
    # by the landed recStormV1* macros (compute_c7_storm_v1 above) and
    # referenced nowhere in the paper skeleton or paper/ under their own
    # names, so there is nothing left for them to stand in for. See the
    # module docstring's C7 section for the full provenance trail.

    # recLdbcExpressible/recLdbcExecuted/recLdbcValidated (C9,
    # independent-validation axis) have landed -- see compute_ldbc_ref_v1
    # above, reading benchmarks/ldbc-ref-v1/compare-2026-09-18.json (Lane
    # W2t, the Neo4j reference run that this stub named as the reason to
    # wait). No longer emitted here.

    # recLiveDays/recLiveAdvisories/recLiveCorrections (C10, live OSV
    # workload) have landed -- see compute_c10_live_osv above, reading
    # benchmarks/live-osv-v1/snapshot-2026-09-16.json, the first committed
    # record snapshot of the live-osv poller running on xzgpu. No longer
    # emitted here.

    # B7 100M: landed -- see compute_b7_scale above (merge of fe52997). Every
    # core 100M macro, 6/13 executed scale-curve p50s, and the 6 unestimated
    # refusals (recB7Refused<Op>100M) are landed; only
    # recB7ScaleCurveP50ReachWindow100M stays PENDING, with a reason naming
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
    compute_c7_storm_v2_sum_mode(m)
    compute_c7_storm_v2_probe_sum_mode(m)
    compute_d160(m)
    compute_d160_llm_direct_fix(m)
    compute_c2(m)
    compute_ladder(m)
    compute_longevity_soak(m)
    compute_longevity_rederived(m)
    compute_longevity_soak_two(m)
    compute_longevity_verify_and_replay2(m)
    compute_longevity_soak_hunt(m)
    compute_longevity_soak_three(m)
    compute_longevity_compaction_cadence(m)
    compute_longevity_soak_four(m)
    compute_overload(m)
    compute_c10_live_osv(m)
    compute_ldbc_ref_v1(m)
    compute_ldbc_format3_rebuild(m)
    compute_b7_scale(m)
    compute_external_baselines(m)
    compute_ext_sum_mode(m)
    add_pending_stubs(m)

    if FAILURES:
        print(f"VERIFICATION FAILED after {CHECKS} checks:", file=sys.stderr)
        for f in FAILURES:
            print(f"  - {f}", file=sys.stderr)
        return 1

    out_path = OUT_DIR / "sys-paper-macros.tex"
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
    print(f"sys_paper_macros: {landed} landed macros, {pending} pending stubs, "
          f"{CHECKS} verifications, all passed.")
    print(f"  {status}: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

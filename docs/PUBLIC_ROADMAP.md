# TGMS Public Roadmap

This is the public view of where TGMS is going; the detailed milestone
planning and research-direction documents live in the maintainers'
internal notes. Questions about any item are welcome as issues.

---

## Recently shipped (v0.6.1 – v0.8.0)

**Adoption polish.** Streamlined quickstart (`pip install tgms && tgms demo`), hands-on tutorials, and a published stability contract ([STABILITY.md](STABILITY.md)) so you know what to expect when you upgrade.

**Compositional temporal-graph IR (TGIR) with general pattern matching.** A small set of composable query primitives beneath the fixed operator catalog — labeled scans, typed expansions, variable-length traversal, typed joins, multi-way pattern matching — built around bi-temporal semantics, with every result carrying its temporal scope and evidence metadata. Measured against a forecast frozen before implementation: LDBC read-template coverage moved from 3 to 24 of 41, independent-question coverage from 94 to 102 of 110, exactly as predicted.

**Correction-aware result freshness.** `tgms trace check` tells you whether a correction landed after an answer was computed could have changed it — "this result was computed then; a correction received since affects data it depended on; reconsider it" — and it never calls a stale answer fresh: 0 false-fresh verdicts over 898 changed answers across two injection campaigns, where the naive row-touch check is wrong 47% of the time.

**Result maintenance: the artifact registry, and one-level propagation.** A saved result can now be registered under a name, checked the same way `tgms trace check` checks a record, and selectively refreshed — a new generation publishes only when asked, and the old one stays byte-identical on disk. A refreshed result's own dependents get flagged too, one hop out, even when their own dependency scope was never touched. Measured across the M5 maintenance campaign: 0 false-safe propagation decisions (308 payload-changing, 5,867 total, 99.0% resolved without recomputing anything), a 600/600 pinned-answer exemption, and 0 false-fresh in 37,371 trials campaign-wide. `tgms.artifact.propagate.parent_recheck` has no CLI verb yet — today it's a library call, named here as a gap rather than a promise about its shape.

**Dependency precision: delivered, with an honest miss.** Pattern-match verdicts can now narrow from `POSSIBLY_STALE` to `FRESH` using the actual scan region a match touched, instead of the coarse "matches anything" scope every operator used before — never trading soundness for it (0 false-fresh on every population it was measured against, including a full soundness adjudication after a labeling-bug scare mid-campaign). The pre-registered precision bar — 10× more precise than the coarse check — did not clear: measured 1.86×, because an anchored pattern-match population already makes the coarse check ~42–48% precise on its own, which structurally compresses how much visible lift is left to gain. Reported as measured, not re-scored, and not re-labeled a partial success.

---

## Now

**Scale evidence.** Running the newly unlocked query shapes against a full-size LDBC social-network instance, with predictions registered before the runs — the same discipline every number above was produced under.

---

## Next

**Carve-extent logging.** Data-justified by the M5 campaign: on a store with real interval-valid-time data, corrections landing *outside* a result's own read window — not inside it — turned out to be the entire measured source of change (100% of the changed trials that qualified, at a pre-registered power floor). The freshness check's dependency footprint doesn't record that extent today; this is the next concrete step in dependency precision, not a speculative one.

**Write-side layout (D-149).** The carve-extent work above only pays off if writes can be organized to make that extent cheap to log and check — the layout decision M5's measurement now justifies making, rather than guessing at ahead of evidence.

---

## Later

**Query optimizer.** Intelligent execution planning and cost-based optimization for complex queries, reducing memory footprint and latency on large graphs.

**Learning integration.** A foundation for temporal graph learning tasks — building and training models that respect the graph's bi-temporal structure and correction history.

**Larger-scale infrastructure.** Distributed execution and storage for graphs that exceed single-machine capacity, keeping the same query semantics and correctness guarantees.

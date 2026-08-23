# TGMS Public Roadmap

This is the public view of where TGMS is going; the detailed milestone
planning and research-direction documents live in the maintainers'
internal notes. Questions about any item are welcome as issues.

---

## Recently shipped (v0.6.1 – v0.7.0)

**Adoption polish.** Streamlined quickstart (`pip install tgms && tgms demo`), hands-on tutorials, and a published stability contract ([STABILITY.md](STABILITY.md)) so you know what to expect when you upgrade.

**Compositional temporal-graph IR (TGIR) with general pattern matching.** A small set of composable query primitives beneath the fixed operator catalog — labeled scans, typed expansions, variable-length traversal, typed joins, multi-way pattern matching — built around bi-temporal semantics, with every result carrying its temporal scope and evidence metadata. Measured against a forecast frozen before implementation: LDBC read-template coverage moved from 3 to 24 of 41, independent-question coverage from 94 to 102 of 110, exactly as predicted.

**Correction-aware result freshness.** `tgms trace check` tells you whether a correction landed after an answer was computed could have changed it — "this result was computed then; a correction received since affects data it depended on; reconsider it" — and it never calls a stale answer fresh: 0 false-fresh verdicts over 898 changed answers across two injection campaigns, where the naive row-touch check is wrong 47% of the time.

---

## Now

**Dependency precision.** The freshness check is deliberately conservative; the research now is making it more precise without ever compromising soundness — finer dependency footprints, so fewer still-valid answers get flagged for reconsideration.

**Scale evidence.** Running the newly unlocked query shapes against a full-size LDBC social-network instance, with predictions registered before the runs — the same discipline every number above was produced under.

---

## Next

**Correction propagation.** From detecting that a result may be stale to repairing it: recomputing affected views, refreshing cached results and agent evidence selectively rather than wholesale.

---

## Later

**Query optimizer.** Intelligent execution planning and cost-based optimization for complex queries, reducing memory footprint and latency on large graphs.

**Learning integration.** A foundation for temporal graph learning tasks — building and training models that respect the graph's bi-temporal structure and correction history.

**Larger-scale infrastructure.** Distributed execution and storage for graphs that exceed single-machine capacity, keeping the same query semantics and correctness guarantees.

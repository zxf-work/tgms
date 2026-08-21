# TGMS Public Roadmap

For detailed internal planning and research direction, see [`docs/ROADMAP.md`](./ROADMAP.md) and [`docs/design/`](./design/).

---

## Now

**Adoption polish.** Making TGMS easy to install and run: streamlined quickstart guide, interactive demo, hands-on tutorials for ingesting event data and auditing results, and a published stability contract so you know what to expect when you upgrade.

**Compositional temporal-graph IR (TGIR).** A small set of composable query primitives that replaces the current fixed operator catalog, so new question shapes stop requiring new hand-coded operators. Built from the ground up around bi-temporal semantics (evolution vs. correction), with every result carrying its temporal scope and evidence metadata.

**General pattern matching.** Extending query expressiveness to support flexible graph patterns — labeled scans, typed expansions, multi-way joins, and path predicates — so you can ask questions that current operator sets cannot express.

---

## Next

**Correction-aware result freshness.** When historical graph facts are corrected, you need to know whether old answers are still valid. The goal: TGMS tells you "this result was computed on March 1; a correction received yesterday affects data it depended on; it should be reconsidered" — and never calls a result fresh when a relevant correction could have changed it.

**Provenance and dependency tracking.** Every query result carries metadata about what graph state it depended on, making it possible to automatically propagate corrections through downstream computation and keep derived results consistent with evolving history.

---

## Later

**Query optimizer.** Intelligent execution planning and cost-based optimization for complex queries, reducing memory footprint and latency on large graphs.

**Learning integration.** A foundation for temporal graph learning tasks — building and training models that respect the graph's bi-temporal structure and correction history.

**Larger-scale infrastructure.** Distributed execution and storage for graphs that exceed single-machine capacity, keeping the same query semantics and correctness guarantees.

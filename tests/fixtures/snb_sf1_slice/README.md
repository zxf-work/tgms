# LDBC SNB SF1 — two static files, vendored as test fixtures

`static/TagClass` (71 rows) and `static/Place` (1,460 rows) are the two smallest
files in the LDBC SNB SF1 `composite-merged-fk` distribution, copied verbatim
(gzip intact) so that `tests/test_snb_loader.py` exercises the parser against
**real bytes** rather than against our idea of them. Together they cover the
nullable-foreign-key case (one root `TagClass`, six `Continent`s with no parent)
and the `type` discriminator that gives `Place` and `Organisation` their
concrete labels.

- Source: `datasets.ldbcouncil.org`, `bi-sf1-composite-merged-fk.tar.zst`,
  path `graphs/csv/bi/composite-merged-fk/initial_snapshot/static/`
- Pinned by `external_workloads/MANIFEST.yaml` (`ldbc:` block)
- Row counts match LDBC's published SF1 figures
  (`ldbc_snb_docs`, `tables/table-number-of-entities-bi-initial.tex`)

**Attribution.** LDBC Social Network Benchmark material, used under the
Creative Commons CC-BY 4.0 licence. **This is not an LDBC Benchmark, this is not
an implementation of an LDBC Benchmark, and nothing derived from these files in
this repository is an LDBC Benchmark Result.**

# B1-v2 A/B — format 3 (reader torn-tail fix) vs. the pinned format-2 soak engine (2026-09)

Same Addendum-3 (manifest forecast) protocol as `b1-manifest-ab-2026-09.json`
(the v1 B1 A/B): SF1 manifest bytes at matched 2.5M ops, SS20 batch=1
commitcost phase deciles/p50 (300 commits, 100k-row seed store), chain-open
at G~10k K=512. Reported beside the thresholds the brief names; **no
verdicts here** — scoring is the coordinator's.

Machine: xzgpu, 40 cores / 93 GB, `Linux-5.4.0-216-generic-x86_64-with-glibc2.31`.
Control commit `886805f600bc` — the format-2 engine already pinned for the
24h longevity soak (`MANIFEST_FORMAT_VERSION == 2`, checkpoint-every default
512); **not rebuilt** for this lane. Treatment commit `7a5ff9871a0e` (public
main, `MANIFEST_FORMAT_VERSION == 3`, the reader torn-tail two-condition
rule) — built fresh into a new detached worktree `work/tgms-xz-7a5ff98`
(`cargo build --release -p tgms-engine-py`, artifact copied to
`tgms/_engine.cpython-312-x86_64-linux-gnu.so`; the shared venv's installed
packages were never touched).

Records: `b1-manifest-v2-ab-2026-09.json` (schema-conformant summary,
validated with `scripts/check_result_manifest.py`), `b1-manifest-v2-ab-2026-09-raw.json`
(consolidated raw records), companion `b1-manifest-v2-ab-2026-09-logs/`
(verbatim run logs + per-rep commitcost JSON, sha256-verified byte-identical
against the xzgpu copies before those were deleted).

## B1(a) — SF1 manifest bytes at matched 2.5M node ops

`scripts/build_snb_store.py --backend native --batch 250 --compact-every 0
--digest manifest --write-path assert`, killed at the `2,500,000 ops` log
line (process was still running at that line in both arms, same as v1;
confirmed exited before measuring).

| | control (886805f, format 2) | treatment (7a5ff98, format 3) | threshold |
|---|---:|---:|---|
| manifest bytes | 73,790,679 B (73.8 MB) | 74,403,447 B (74.4 MB) | 62 MB ± 5% (58.9–65.1 MB) |
| segment bytes | 158,966,639 B (159.0 MB) | 163,447,175 B (163.4 MB) | — |
| ops/s at each 500k checkpoint | 9,098 / 5,761 / 4,121 / 3,155 / 2,543 | 12,400 / 9,401 / 7,579 / 6,264 / 5,335 | — (observation only) |

Observation, no claim attached: the treatment ran at roughly double the
control's ops/s at every checkpoint (e.g. 5,335 vs 2,543 ops/s at 2.5M).
Both arms' manifest bytes land close together (~74 MB), well above the 62 MB
± 5% band recorded beside them.

## B1(b) — SS20 batch=1 commitcost, 300 commits, 100k-row seed store

3 reps each (control, treatment default K=512), 1 rep each for the two
off-default K-sweep values (K=512 is the treatment default already covered
by the 3-rep run, so only K=128 and K=1024 were additionally swept — this
differs slightly from v1, which re-ran K=512 explicitly as part of its
sweep).

| | control | treatment (K=512) | treatment K=128 | treatment K=1024 |
|---|---:|---:|---:|---:|
| manifest_us first→last decile (median of reps) | 1.115x | 1.079x | 1.068x (1 rep) | 1.150x (1 rep) |
| engine-commit total_us first→last decile (median of reps) | 1.733x | 1.674x | 1.818x (1 rep) | 2.074x (1 rep) |
| engine-commit p50 (total_us), reps | 4,652 / 5,490 / 5,430 | 5,478 / 5,320 / 5,772 | 4,597 (1 rep) | 5,276 (1 rep) |
| engine-commit p50 median | 5,430 us | 5,478 us | — | — |

Threshold: decile ≤ 1.10x (refute ≥ 1.2x) — reported per component above
(manifest_us and engine-commit total_us separately, as the underlying phases
move differently, same as v1's OR-of-two-components framing). Threshold: p50
paired ≤ 0.70x control **or** ≤ 5.58 ms absolute — treatment median p50 is
5,478 us = 5.478 ms (at the absolute bound); the paired ratio
treatment/control is 5,478/5,430 = 1.009x (not below 0.70x).

### Correction (2026-09-15): the treatment reps above measured a format-2 chain

**B1(b)'s treatment numbers on this page are format-2 numbers, not format-3
ones.** `b1-manifest-v2e-remeasure-2026-09-raw.json`'s `cell_b_commitcost.
treatment_reps_full[*].first_decile_us.manifest_bytes` /
`last_decile_us.manifest_bytes` — the same field this lane's own treatment
reps report — is 1,592 → 1,595 B for a genuine format-3 chain built by the
same `e5d4171` treatment engine used for that remeasurement. Every treatment
rep committed to *this* record
(`b1-manifest-v2-ab-2026-09-logs/b1v2-cc-treatment-{rep1,rep2,rep3,k128,
k1024}.json`) instead reports `first_decile_us.manifest_bytes` = 1,565 B and
`last_decile_us.manifest_bytes` = 1,568–1,570 B — byte-identical (to the
smaller value) with this page's own control (`b1v2-cc-control-rep*.json`:
1,565 → 1,568 B), which ran the pinned format-2 engine (`886805f600bc`,
`MANIFEST_FORMAT_VERSION == 2`) by design.

`Manifest::digest()` (`crates/tgms-engine-core/src/manifest.rs:615–636`)
picks the digest rule from the manifest's own `format` field, not from the
binary that opened it: formats 1–2 hash the whole blanked document
(`legacy_body_sha`, O(segments) per commit), format 3 takes the Merkle root
over the ordered segment set. A format-2 chain therefore keeps paying the
O(segments) rule regardless of which engine is timing it. Every treatment
commitcost rep on this page ran against a format-2 chain — **the B1(b)
treatment column above (decile 1.674x, p50 5,478 us, p50 parity 1.009x) is
what the format-3 binary measured *while pointed at a format-2 store*, not
a measurement of the format-3 commit path.** That the treatment column's
decile (1.674x) tracks the control's (1.733x) so closely, rather than
looking anything like the flat, O(1)-shaped decile the format-3 path
actually produces, is the same fact seen from the number side.

This is superseded, for commit cost and chain-open, by
`b1-manifest-v2e-remeasure-2026-09.json`/`-raw.json` (treatment `e5d4171`,
built fresh and confirmed on a genuine format-3 chain via `build_info()`
and this same manifest-bytes field: 1,592 → 1,595 B): commit-cost decile
1.017x (vs. this page's mislabeled 1.674x), p50 paired ratio 0.715x /
absolute 3.365 ms, chain-open manifest-chain component 87.06 ms,
dictionary-open component 1,066.5 ms. **The records on this page are left
exactly as measured** — nothing above is edited or re-scored — this
section only says what they actually measured and where the corrected
numbers live.

**How the treatment reps ended up on a format-2 chain.** Two explanations
were checked: (a) `eval_concurrency.py commitcost` copying a pre-built
format-2 seed store into the treatment run, or (b) the treatment
invocation's `PYTHONPATH`/venv resolving the pinned `886805f` engine
instead of the freshly built `7a5ff98` one.

(a) is ruled out by the script's own source at the commit this lane
actually ran, `7a5ff9871a0e` (`git show 7a5ff9871a0e:scripts/
eval_concurrency.py`): `cmd_commitcost` has never had a `--store`/copy
path — every rep builds its seed store fresh, in-process, via
`root = Path(tempfile.mkdtemp(prefix="tgms-cc-")) / "s"` followed by
`s = tgms.open(root, backend="native")` and `s.ingest_events(...)`, unlike
`cmd_mixed`'s `_pristine()`, which does copy a cached store per trial.
There is no code path in this function, at this commit, through which a
pre-built store of any format could be substituted.

That leaves (b), and the evidence is consistent with it but does not
independently confirm it, because the logs cannot decide it either way.
What the logs *do* show: every `b1v2-cc-treatment-*.json` record's
`provenance.commit` field (`git rev-parse --short HEAD` run in the
harness's own working directory) reads `"7a5ff98"`, and every
`b1v2-cc-control-*.json` record's reads `"886805f"` — confirming the
harness was invoked with its working directory inside the correct
per-arm worktree for each arm. That field says nothing about which
`_engine*.so` `import tgms` actually resolved, though: the working
directory a subprocess is launched from and the `PYTHONPATH`/venv that
resolves its imports are two independent things, and only the former is
in this field.

What is missing, and would have settled it either way: at
`7a5ff9871a0e` (2026-09-15 00:32:02, per `git log`), neither
`NativeAdapter.build_info()`/`_engine.build_info()` nor `open_phase_us()`'s
`chain_format` existed yet — `build_info()` landed in `23fd7665`
(02:17:19, +1h45m) and the fully-timed commit path with its own
`build_info` row field in `db3fd6c1`/`e5d4171` (02:44:27–02:44:33,
+2h12m). Neither the `b1v2-cc-treatment-*.json` records nor anything else
under `b1-manifest-v2-ab-2026-09-logs/` therefore carries a
`build_info()`/`MANIFEST_FORMAT_VERSION` printout, a captured
`PYTHONPATH` value, a `sys.path` dump, or `tgms._engine.__file__` — any of
which would have shown, directly, which engine `import tgms` actually
loaded for these invocations. The `b1v2-treatment.log`/`b1v2-control.log`
files in the same directory are B1(a)'s `build_snb_store.py` logs, not
B1(b)'s, and record only `RUN_STARTED commit=... csv=... backend=native
...` lines with no engine-path or PYTHONPATH field either.

**Conclusion:** the pre-built-seed-store hypothesis is ruled out by source
inspection; a stale `PYTHONPATH`/venv resolving `886805f`'s engine during
the treatment invocation is the only explanation consistent with the
manifest-bytes evidence, but it is not independently confirmed by
anything this lane recorded. `scripts/eval_concurrency.py commitcost` now
records each rep's own `chain_format` and refuses to run one whose format
is older than the loaded engine's `MANIFEST_FORMAT_VERSION` (see
`docs/eval_concurrency.md`'s 2026-09-15 note), so this specific failure
mode cannot recur silently regardless of which explanation was true here.

## B1(c) — cold/warm chain-open at G~10k, K=512

Same corrected methodology as v1: times `NativeAdapter(store/"native")`
construction directly (the Rust-level `NativeStore::open`), not
`tgms.open()`. 3 reps per arm, back-to-back in one process, immediately
after each arm's B1(a) run (no root on this host, so no explicit
posix_fadvise cache-drop between reps — first rep is process-cold, later
reps warm, same caveat v1 recorded elsewhere).

| | control (G≈10,759) | treatment (G≈11,029) |
|---|---:|---:|
| open, 3 reps | 1,427.6 / 2,279.2 / 2,247.9 ms | 1,252.7 / 1,705.3 / 2,691.3 ms |
| open, median | 2,247.9 ms | 1,705.3 ms |

Threshold: ≤ 70 ms, component-scored. `NativeAdapter()` exposes no internal
phase timer to Python (unlike the commit path's `phase_p50_us`), so a
checkpoint-load-vs-delta-replay component split was not measured — flagged
here rather than fabricated. The raw open time is reported instead.

## Notes

- Working store copies (`/mnt/project/xzhang/tgms/tmp/b1v2-control`,
  `.../b1v2-treatment`) were deleted from xzgpu after B1(c). The control
  worktree `tgms-xz-886805f` was left in place, untouched and unbuilt by
  this lane. The treatment worktree `tgms-xz-7a5ff98` remains on xzgpu.

## Provenance — dataset digest correction (2026-09-15)

The first version of this record carried a placeholder dataset digest,
corrected 2026-09-15. The originally recorded `dataset.digest`
(`d8ccf1970f67f657d7121b3586399d180c727e867055f1da7c73d73caebe67d1`) was not
an independently computed value: it is v1's own recorded digest
(`b1-manifest-ab-2026-09.json`'s 62-hex-character string, itself never
verified) with the two characters `d1` appended to reach a plausible
64-hex-character length — an exact prefix match, not a coincidence. The
"structurally identical corpus to v1" claim rested on that digest and was
therefore unverified, not confirmed.

`dataset.digest` now holds a real sha256
(`8b4168aa72042193a13648a003acd308f23c59922c8a0cf39b79a31246d0d590`) of a
freshly generated per-file manifest —
`b1-manifest-v2-ab-2026-09-logs/dataset-manifest.txt`, one line per file
(relative path, byte size, mtime in UTC ISO-8601, sha256), sorted by path,
covering every file under
`/mnt/project/xzhang/tgms/ldbc-sf1/bi-sf1-composite-merged-fk/graphs/csv/bi/composite-merged-fk/initial_snapshot`
on xzgpu, 55 files total. `dataset.digest_method` in the JSON record
describes exactly how it was produced.

Corpus-identity evidence, stated exactly: v1's `-logs/` did **not** record a
file listing, sizes, or hashes of its input at all — `b1a-control.log` and
`b1a-treatment.log` each record only the CSV directory path via a
`csv=...initial_snapshot` field in their `RUN_STARTED` line, and that path
string is byte-identical to the one this v2 lane read. Beyond the identical
path string, there is no committed record of what v1 actually read, so
byte-for-byte identity with v1's run cannot be confirmed from the record
alone. One additional, suggestive (not conclusive) data point: every file in
the freshly generated listing has mtime `2022-04-26` — the LDBC SF1 dataset's
original generation time — years before both the v1 (2026-09-14) and this
v2 (2026-09-15) run, consistent with the corpus having gone untouched across
both runs. v1's placeholder digest carries no evidentiary weight either way
and should not be cited as confirmation.

## Environment / build provenance (2026-09-15)

Per the internal diagnosis memo (`docs/design/B1V2_AB_DIAGNOSIS_2026-09-15.md`
§1.4-2a), one open candidate for the format-3 treatment's still-unexplained
commit-time residual is a `debug-assertions` build mismatch, which would
silently reintroduce `store.rs`'s O(segments)
`debug_assert_eq!(manifest_sha, body_sha_canonical())` on every commit. This
section records what the evidence on xzgpu shows, from `.so` provenance
only — no experiment was re-run.

**Control (886805f)** — `PYTHONPATH=/mnt/project/xzhang/tgms/work/tgms-xz-886805f`
loads `tgms/_engine.cpython-312-x86_64-linux-gnu.so`: 2,436,728 B, mtime
2026-09-14T05:05:15Z, sha256 `c476e5cb9cc415813b5fe9d3e66bb1e555f48d4eb6357bfd47e2f05a523286ba`.
`tgms-xz-886805f/target/` does not exist — this worktree was never built in
place, consistent with the "not rebuilt for this lane" note above. That
exact sha256/size/mtime triple matches, byte-for-byte,
`/mnt/project/xzhang/tgms/work/tgms/target/release/lib_engine.so` (the
separate, pre-pinned 24h-soak worktree, confirmed at commit `886805f600bc`
via `git rev-parse HEAD`) — i.e. the control `.so` was built there and
copied in, not built inside `tgms-xz-886805f`. `target/debug/` under
`work/tgms` holds no `.so` at all (`find target/debug -maxdepth 1
-iname '*.so'` empty) — only `target/release/lib_engine.so` was ever
produced. `/mnt/project/xzhang/tgms/tmp/uv-sync-rebuild4.log` (timestamped
2026-09-14 05:05, matching the `.so` mtime to the minute) shows this build
went through `uv sync` rebuilding the `tgms` package in place — `uv sync`'s
build backend is maturin; the `target/release` destination (not
`target/debug`) shows this run built release, not the maturin-default dev
profile.

**Treatment (7a5ff98)** — `PYTHONPATH=/mnt/project/xzhang/tgms/work/tgms-xz-7a5ff98`
loads `tgms/_engine.cpython-312-x86_64-linux-gnu.so`: 2,464,568 B, mtime
2026-09-15T06:02:46Z, sha256 `f2ae906f85c70e85873b1d5297ca91c00d58e29d8408a2db9f743ecad9901f7f`.
`ls target/` in this worktree shows only `CACHEDIR.TAG` and `release/` (no
`debug/` ever created). That exact sha256/size match
`target/release/lib_engine.so` in the same worktree (mtime
2026-09-15T06:02:19Z, 27s before the copy into `tgms/_engine...so`) —
confirms this `.so` was built in place via a release-profile `cargo build`,
consistent with the record's `cargo build --release -p tgms-engine-py`
claim, not `maturin develop` (which defaults to a debug build under
`target/debug/`, never populated here).

**Profile / rustflags, both arms** — the workspace `Cargo.toml` (identical
text in both worktrees) sets only:
```
[profile.release]
opt-level = 3
lto = "thin"
codegen-units = 1
panic = "unwind"
```
No `debug-assertions` key is set for `[profile.release]` in either
worktree, so it takes Cargo's own default for that profile (`false`).
Neither worktree has a `.cargo/config.toml` or `.cargo/config`, and
`~/.cargo/config.toml`/`config` do not exist on this account either — no
`RUSTFLAGS`/profile override was available to apply. Cargo's own build
fingerprint for the `tgms-engine-core` crate records `"rustflags":[]` for
both the control build (`work/tgms/target/release/.fingerprint/tgms-engine-core-509446aa74b9d6b7/lib-tgms_engine_core.json`)
and the treatment build (`work/tgms-xz-7a5ff98/target/release/.fingerprint/tgms-engine-core-509446aa74b9d6b7/lib-tgms_engine_core.json`)
— and both fingerprints carry the identical `"profile"` hash
(`1783587453833569552`), i.e. Cargo itself computed the same profile
configuration for both builds.

**What the evidence supports, and no further:** both arms' loaded `.so`
files trace, byte-for-byte, to a `target/release/` artifact (never a
`target/debug/` one) built under the same `[profile.release]` block with no
`debug-assertions` override and no `RUSTFLAGS` in play. This is consistent
with both arms having `debug-assertions = false` (Cargo's release-profile
default), which would mean candidate 2a (a debug-assertions build mismatch
reintroducing the O(segments) `debug_assert_eq!`) is not what is
happening — but this is inferred from build configuration and Cargo's own
fingerprint records, not from disassembling the binaries or instrumenting a
run, so it should be read as build-provenance evidence for that inference,
not as a direct measurement of the compiled code.

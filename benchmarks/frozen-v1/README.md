# Frozen v1 benchmark artifacts

Canonical, versioned artifacts for the pre-registered frozen-test campaign
(D-018). **Reproducibility depends on the store, not just the suite** — a
fresh `tgms ingest` re-assigns transaction times, so it does *not* reproduce
the store the frozen gold was computed on (D-023). Always rebuild from the
event log with `tgms replay`.

## Files
- `suite-collegemsg.json`, `suite-emaileu.json`, `suite-synth.json` — frozen
  task suites (task definitions + engine-computed gold + the 20/80 split).
  test_split_shas are recorded in `docs/DECISIONS.md` D-018.
- `collegemsg.eventlog.jsonl` — the append-only event log of the canonical
  CollegeMsg store (background events + the injected corrections the probe
  gold depends on). Replays to a **byte-identical** store, preserving
  transaction times.
- `collegemsg.memory.sqlite` — the evolution-memory DB used by the campaign
  (LLM-derived summaries; not deterministically regenerable, so vaulted).

## Rebuild the canonical CollegeMsg store
```bash
tgms replay benchmarks/frozen-v1/collegemsg.eventlog.jsonl \
     --store stores/collegemsg
cp benchmarks/frozen-v1/collegemsg.memory.sqlite \
     stores/collegemsg/memory.sqlite
# verify it matches the frozen split (must print D-018's test_split_sha):
tgms tasks --store stores/collegemsg --dataset collegemsg --seed 0 \
     --out /tmp/check.json    # -> cbdc36a0774e78cb5301c091131750ef403f95379f8e4b7d8a07334354a0142f
```
Then point eval configs at `stores/collegemsg` and
`benchmarks/frozen-v1/suite-collegemsg.json`.

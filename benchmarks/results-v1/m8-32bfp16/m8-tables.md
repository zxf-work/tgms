# M8 table of record (generated — do not edit)

| dataset | arm | rows | em | probes | ucr | ucr_pre |
|---|---|---:|---:|---:|---:|---:|
| sx-mathoverflow | ours | 72 | 0.3056 | 0.0 | 0.0 | 0.1224 |
| sx-mathoverflow | ours-noverify | 72 | 0.3056 | 0.0 | 0.125 | None |
| sx-mathoverflow | b6 | 72 | 0.3472 | 1.0 | None | None |
| sx-mathoverflow | b6e | 72 | 0.3472 | 1.0 | None | 0.0795 |
| sx-superuser | ours | 68 | 0.3529 | 0.0769 | 0.0 | 0.1132 |
| sx-superuser | ours-noverify | 68 | 0.3824 | 0.2308 | 0.0962 | None |
| sx-superuser | b6 | 68 | 0.3676 | 1.0 | None | None |
| sx-superuser | b6e | 68 | 0.3824 | 1.0 | None | 0.0811 |
| wiki-talk | ours | 62 | 0.3871 | 0.1538 | 0.0 | 0.1047 |
| wiki-talk | ours-noverify | 62 | 0.3387 | 0.0769 | 0.1279 | None |
| wiki-talk | b6 | 62 | 0.4516 | 1.0 | None | None |
| wiki-talk | b6e | 62 | 0.4516 | 1.0 | None | 0.0781 |

## Paired contrasts (per-task, seed-averaged, 10k bootstrap)

| contrast | Δem | 95% CI | n tasks |
|---|---:|---|---:|
| sx-mathoverflow | interface: b6e - ours | 0.0417 | [-0.1111, 0.2083] | 72 |
| sx-mathoverflow | evidence(sql): b6e - b6 | 0.0 | [0.0, 0.0] | 72 |
| sx-mathoverflow | evidence(tgms): ours - ours-noverify | 0.0 | [0.0, 0.0] | 72 |
| sx-superuser | interface: b6e - ours | 0.0294 | [-0.1324, 0.1912] | 68 |
| sx-superuser | evidence(sql): b6e - b6 | 0.0147 | [0.0, 0.0441] | 68 |
| sx-superuser | evidence(tgms): ours - ours-noverify | -0.0294 | [-0.0882, 0.0294] | 68 |
| wiki-talk | interface: b6e - ours | 0.0645 | [-0.0968, 0.2258] | 62 |
| wiki-talk | evidence(sql): b6e - b6 | 0.0 | [0.0, 0.0] | 62 |
| wiki-talk | evidence(tgms): ours - ours-noverify | 0.0484 | [0.0, 0.1129] | 62 |

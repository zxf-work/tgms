# M8 table of record (generated — do not edit)

| dataset | arm | rows | em | probes | ucr | ucr_pre |
|---|---|---:|---:|---:|---:|---:|
| sx-mathoverflow | ours | 72 | 0.125 | 0.0 | 0.0 | 0.2805 |
| sx-mathoverflow | ours-noverify | 72 | 0.1389 | 0.0 | 0.2361 | None |
| sx-mathoverflow | b6 | 72 | 0.3056 | 0.9231 | None | None |
| sx-mathoverflow | b6e | 72 | 0.3056 | 0.9231 | None | 0.0943 |
| sx-superuser | ours | 68 | 0.1176 | 0.0 | 0.0 | 0.1286 |
| sx-superuser | ours-noverify | 68 | 0.1029 | 0.0 | 0.1544 | None |
| sx-superuser | b6 | 68 | 0.3529 | 1.0 | None | None |
| sx-superuser | b6e | 68 | 0.3088 | 0.9231 | None | 0.18 |
| wiki-talk | ours | 62 | 0.1452 | 0.0 | 0.0 | 0.0333 |
| wiki-talk | ours-noverify | 62 | 0.1129 | 0.0 | 0.0741 | None |
| wiki-talk | b6 | 62 | 0.3871 | 1.0 | None | None |
| wiki-talk | b6e | 62 | 0.3871 | 0.9231 | None | 0.24 |

## Paired contrasts (per-task, seed-averaged, 10k bootstrap)

| contrast | Δem | 95% CI | n tasks |
|---|---:|---|---:|
| sx-mathoverflow | interface: b6e - ours | 0.1806 | [0.0556, 0.3056] | 72 |
| sx-mathoverflow | evidence(sql): b6e - b6 | 0.0 | [0.0, 0.0] | 72 |
| sx-mathoverflow | evidence(tgms): ours - ours-noverify | -0.0139 | [-0.0417, 0.0] | 72 |
| sx-superuser | interface: b6e - ours | 0.1912 | [0.0588, 0.3235] | 68 |
| sx-superuser | evidence(sql): b6e - b6 | -0.0441 | [-0.1029, 0.0] | 68 |
| sx-superuser | evidence(tgms): ours - ours-noverify | 0.0147 | [0.0, 0.0441] | 68 |
| wiki-talk | interface: b6e - ours | 0.2419 | [0.0968, 0.371] | 62 |
| wiki-talk | evidence(sql): b6e - b6 | 0.0 | [-0.0806, 0.0806] | 62 |
| wiki-talk | evidence(tgms): ours - ours-noverify | 0.0323 | [0.0, 0.0806] | 62 |

# M8 table of record (generated — do not edit)

| dataset | arm | rows | em | probes | ucr | ucr_pre |
|---|---|---:|---:|---:|---:|---:|
| sx-mathoverflow | ours | 72 | 0.3472 | 0.0 | 0.0 | 0.0909 |
| sx-mathoverflow | ours-noverify | 72 | 0.3472 | 0.0 | 0.0909 | None |
| sx-mathoverflow | b6 | 72 | 0.3889 | 1.0 | None | None |
| sx-mathoverflow | b6e | 72 | 0.3889 | 1.0 | None | 0.0667 |
| sx-superuser | ours | 68 | 0.2941 | 0.0 | 0.0 | 0.1058 |
| sx-superuser | ours-noverify | 68 | 0.2941 | 0.0 | 0.1058 | None |
| sx-superuser | b6 | 68 | 0.4118 | 1.0 | None | None |
| sx-superuser | b6e | 68 | 0.3971 | 1.0 | None | 0.0244 |
| wiki-talk | ours | 62 | 0.371 | 0.1538 | 0.0 | 0.066 |
| wiki-talk | ours-noverify | 62 | 0.371 | 0.1538 | 0.066 | None |
| wiki-talk | b6 | 62 | 0.4516 | 1.0 | None | None |
| wiki-talk | b6e | 62 | 0.4516 | 1.0 | None | 0.0676 |

## Paired contrasts (per-task, seed-averaged, 10k bootstrap)

| contrast | Δem | 95% CI | n tasks |
|---|---:|---|---:|
| sx-mathoverflow | interface: b6e - ours | 0.0417 | [-0.125, 0.2083] | 72 |
| sx-mathoverflow | evidence(sql): b6e - b6 | 0.0 | [0.0, 0.0] | 72 |
| sx-mathoverflow | evidence(tgms): ours - ours-noverify | 0.0 | [0.0, 0.0] | 72 |
| sx-superuser | interface: b6e - ours | 0.1029 | [-0.0735, 0.2647] | 68 |
| sx-superuser | evidence(sql): b6e - b6 | -0.0147 | [-0.0441, 0.0] | 68 |
| sx-superuser | evidence(tgms): ours - ours-noverify | 0.0 | [0.0, 0.0] | 68 |
| wiki-talk | interface: b6e - ours | 0.0806 | [-0.0968, 0.2581] | 62 |
| wiki-talk | evidence(sql): b6e - b6 | 0.0 | [0.0, 0.0] | 62 |
| wiki-talk | evidence(tgms): ours - ours-noverify | 0.0 | [0.0, 0.0] | 62 |

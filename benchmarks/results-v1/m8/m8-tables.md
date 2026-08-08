# M8 table of record (generated — do not edit)

| dataset | arm | rows | em | probes | ucr | ucr_pre |
|---|---|---:|---:|---:|---:|---:|
| sx-mathoverflow | ours | 216 | 0.4861 | 0.6923 | 0.0 | 0.2391 |
| sx-mathoverflow | ours-noverify | 216 | 0.4861 | 0.6923 | 0.2391 | None |
| sx-mathoverflow | b6 | 216 | 0.3611 | 0.7692 | None | None |
| sx-mathoverflow | b6e | 216 | 0.3611 | 0.7692 | None | 0.1176 |
| sx-superuser | ours | 204 | 0.4559 | 0.6154 | 0.0 | 0.1885 |
| sx-superuser | ours-noverify | 204 | 0.4559 | 0.6154 | 0.1885 | None |
| sx-superuser | b6 | 204 | 0.3676 | 0.8462 | None | None |
| sx-superuser | b6e | 204 | 0.3676 | 0.8462 | None | 0.125 |
| wiki-talk | ours | 186 | 0.4032 | 0.5385 | 0.0 | 0.2 |
| wiki-talk | ours-noverify | 186 | 0.4032 | 0.5385 | 0.2 | None |
| wiki-talk | b6 | 186 | 0.4194 | 0.7692 | None | None |
| wiki-talk | b6e | 186 | 0.4032 | 0.7436 | None | 0.129 |

## Paired contrasts (per-task, seed-averaged, 10k bootstrap)

| contrast | Δem | 95% CI | n tasks |
|---|---:|---|---:|
| sx-mathoverflow | interface: b6e - ours | -0.125 | [-0.25, 0.0] | 72 |
| sx-mathoverflow | evidence(sql): b6e - b6 | 0.0 | [0.0, 0.0] | 72 |
| sx-mathoverflow | evidence(tgms): ours - ours-noverify | 0.0 | [0.0, 0.0] | 72 |
| sx-superuser | interface: b6e - ours | -0.0882 | [-0.2059, 0.0294] | 68 |
| sx-superuser | evidence(sql): b6e - b6 | 0.0 | [0.0, 0.0] | 68 |
| sx-superuser | evidence(tgms): ours - ours-noverify | 0.0 | [0.0, 0.0] | 68 |
| wiki-talk | interface: b6e - ours | -0.0 | [-0.1398, 0.1398] | 62 |
| wiki-talk | evidence(sql): b6e - b6 | -0.0161 | [-0.043, 0.0] | 62 |
| wiki-talk | evidence(tgms): ours - ours-noverify | 0.0 | [0.0, 0.0] | 62 |

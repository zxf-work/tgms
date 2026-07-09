# TGMS matrix results

receipts — git_commit: `a564acbe6c723119b263bf5adea8b0c95fb7ae6d`; git_dirty: `True`; config_sha: `4b91c20e73bb8e46ba30d91df3c7f59310b1dfd31397a20cc5815de6220fce64`; test_split_sha: `30808ad897c418fc5e9d6f53de03aefaf93a25e31d1882268f24258709babbed`; dataset: `collegemsg`; suite_seed: `0`

| system | model | family | n | PVR | ESR | EM | F1 | UCR |
|---|---|---|---:|---:|---:|---:|---:|---:|
| b2 | openai/Qwen/Qwen2.5-7B-Instruct | probe | 3 | 0.00 | nan | 0.33 | 0.33 | nan |
| b2 | openai/Qwen/Qwen2.5-7B-Instruct | t1 | 12 | 0.00 | nan | 0.00 | 0.00 | nan |
| b2 | openai/Qwen/Qwen2.5-7B-Instruct | t3 | 4 | 0.00 | nan | 0.00 | 0.00 | nan |
| b2 | openai/Qwen/Qwen2.5-7B-Instruct | t4 | 3 | 0.00 | nan | 0.00 | 0.00 | nan |
| b5 | openai/Qwen/Qwen2.5-7B-Instruct | probe | 3 | 0.00 | nan | 0.00 | 0.00 | nan |
| b5 | openai/Qwen/Qwen2.5-7B-Instruct | t1 | 12 | 0.00 | nan | 0.17 | 0.17 | nan |
| b5 | openai/Qwen/Qwen2.5-7B-Instruct | t3 | 4 | 0.00 | nan | 0.00 | 0.00 | nan |
| b5 | openai/Qwen/Qwen2.5-7B-Instruct | t4 | 3 | 0.00 | nan | 0.00 | 0.00 | nan |
| ours | openai/Qwen/Qwen2.5-7B-Instruct | probe | 3 | 0.67 | 1.00 | 0.67 | 0.67 | 0.000 |
| ours | openai/Qwen/Qwen2.5-7B-Instruct | t1 | 12 | 0.17 | 0.50 | 0.25 | 0.25 | nan |
| ours | openai/Qwen/Qwen2.5-7B-Instruct | t3 | 4 | 0.00 | 0.00 | 0.00 | 0.00 | nan |
| ours | openai/Qwen/Qwen2.5-7B-Instruct | t4 | 3 | 0.00 | 0.33 | 0.00 | 0.00 | 0.000 |

---
Notes (2026-07-09, first live-model run — dev split only, single seed):
- Serving: vLLM 0.11.0 (torch 2.8 cu128, transformers 4.57.1, flashinfer
  removed — no nvcc on host), Qwen2.5-7B-Instruct fp16, FlexAttention
  backend on Quadro RTX 6000 (sm_75), max-model-len 16384, prefix caching on.
- Correction probes: ours 0.67 EM vs 0.33 (b2) / 0.00 (b5) — the bi-temporal
  as_of_tt differentiator is visible even at 7B.
- ours t1: first-emission PVR 0.17, repairs lift ESR to 0.50, EM 0.25 vs
  0.00 (b2) / 0.17 (b5). t3/t4 remain hard at 7B (n small; plans of 3+ steps).
- UCR 0.000 where measured: verifier gating emits only supported claims.
- v1 of this run (before output-field validation) had ESR 0.00 across the
  board — models invent output paths like s2.count; the static check +
  repair loop fixed it. Kept here as the motivating example for the
  operator-contract design.

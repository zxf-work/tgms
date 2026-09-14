"""[tests] LLMDirect's token-accurate budget (2026-09-14 D-160 postmortem).

72/94 llm_direct tasks on the real campaign overflowed the model's real
context window even though their whitespace-approximated size stayed
under the configured 8,000-token budget -- the crude approximation
undercounts real (BPE) tokens by 3-7x on this corpus (numbers/timestamps
fragment heavily). `LLMDirect` now prefers a real tokenizer when one is
resolvable and, given `max_model_len`, caps the effective budget at
`max_model_len - system_prompt_cost - answer_reserve - wrapper_reserve`
so the existing greedy-inclusion loop (most-recent-first) naturally
"drops the oldest events until it fits" rather than overflowing.

Fake-LLM, fake-tokenizer, tmp_path-only: no network, no real model. A
fake tokenizer standing in for a real one (word-count x 3, simulating
BPE fragmentation) is injected via the existing `tokenizer=` parameter,
which is also how a genuine real-tokenizer integration would be plugged
in -- these tests do not need `transformers` installed to exercise the
budget/degrade/meta logic.
"""

from __future__ import annotations

import json

import pytest

import tgms
from tgms.data.synth import generate
from tgms.eval.baselines import (
    DEFAULT_LLM_DIRECT_BUDGET_TOKENS,
    LLMDirect,
    _LLM_DIRECT_WRAPPER_RESERVE_TOKENS,
    _try_load_hf_tokenizer,
)


def _contract_llm(answer_obj):
    return lambda model, messages, temperature, seed, **kw: json.dumps(answer_obj)


def _fake_bpe_tokenizer(text: str) -> int:
    # simulates real-tokenizer inflation over whitespace count -- exact
    # multiplier is irrelevant to what these tests check, only that it
    # differs from a plain word count so budget math is exercised for
    # real rather than trivially.
    return len(text.split()) * 3


@pytest.fixture(scope="module")
def store_env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("llm_direct_budget")
    generate(tmp / "synth", n_nodes=60, n_events=2000, seed=13)
    store = tgms.open(tmp / "store")
    with open(tmp / "synth" / "events.jsonl") as f:
        store.ingest_events(json.loads(line) for line in f if line.strip())
    return {"tmp": tmp, "store": store}


# --------------------------------------------------------------------------- #
# budget derivation                                                           #
# --------------------------------------------------------------------------- #

def test_no_max_model_len_keeps_requested_budget(store_env):
    ao = {"text": "n/a", "claims": []}
    arm = LLMDirect(store_env["store"], _contract_llm(ao), "fake",
                    context_budget_tokens=500, tokenizer=_fake_bpe_tokenizer,
                    seed=0)
    assert arm.context_budget_tokens == 500
    assert arm.requested_budget_tokens == 500
    assert arm.tokenizer_kind == "injected"


def test_max_model_len_caps_the_effective_budget(store_env):
    from tgms.eval.baselines import _LLM_DIRECT_SYSTEM

    ao = {"text": "n/a", "claims": []}
    requested = 100_000  # deliberately far above any real window
    max_model_len = 4_000
    answer_reserve = 500
    arm = LLMDirect(store_env["store"], _contract_llm(ao), "fake",
                    context_budget_tokens=requested,
                    tokenizer=_fake_bpe_tokenizer, seed=0,
                    max_model_len=max_model_len,
                    answer_reserve_tokens=answer_reserve)
    sys_cost = _fake_bpe_tokenizer(_LLM_DIRECT_SYSTEM)
    expected = (max_model_len - sys_cost - answer_reserve
                - _LLM_DIRECT_WRAPPER_RESERVE_TOKENS)
    assert arm.context_budget_tokens == expected
    assert arm.requested_budget_tokens == requested  # original still recorded
    assert arm.context_budget_tokens < requested


def test_effective_budget_never_exceeds_requested(store_env):
    # a huge max_model_len must not WIDEN the budget past what was asked
    ao = {"text": "n/a", "claims": []}
    arm = LLMDirect(store_env["store"], _contract_llm(ao), "fake",
                    context_budget_tokens=200, tokenizer=_fake_bpe_tokenizer,
                    seed=0, max_model_len=1_000_000)
    assert arm.context_budget_tokens == 200


def test_default_tokenizer_is_whitespace_approx_when_none_resolvable(
        store_env):
    # this environment has no `transformers` installed (or, on a host
    # that does, no cached tokenizer for a nonsense model id) --
    # _try_load_hf_tokenizer must fail closed to None either way.
    assert _try_load_hf_tokenizer("not-a-real-model/definitely-not") is None
    ao = {"text": "n/a", "claims": []}
    arm = LLMDirect(store_env["store"], _contract_llm(ao), "fake", seed=0)
    assert arm.tokenizer_kind == "whitespace_approx"
    assert arm.context_budget_tokens == DEFAULT_LLM_DIRECT_BUDGET_TOKENS


# --------------------------------------------------------------------------- #
# degrade-to-fit (most-recent-first greedy inclusion under a tight budget)    #
# --------------------------------------------------------------------------- #

def test_tight_budget_degrades_by_dropping_oldest_events(store_env):
    ao = {"text": "n/a", "claims": []}
    wide = LLMDirect(store_env["store"], _contract_llm(ao), "fake",
                     context_budget_tokens=1_000_000,
                     tokenizer=_fake_bpe_tokenizer, seed=0)
    full = wide.answer("How many events are there?")
    assert full["meta"]["truncated"] is False

    tight = LLMDirect(store_env["store"], _contract_llm(ao), "fake",
                      context_budget_tokens=200,
                      tokenizer=_fake_bpe_tokenizer, seed=0)
    out = tight.answer("How many events are there?")
    meta = out["meta"]
    assert meta["truncated"] is True
    assert meta["events_included"] < meta["events_offered"]
    assert meta["events_included"] > 0  # at least one line always included
    # never crashes -- a normal (empty-claims) AnswerObject still comes back
    assert out["answer_object"] == {"text": "n/a", "claims": []}


# --------------------------------------------------------------------------- #
# meta fields                                                                 #
# --------------------------------------------------------------------------- #

def test_meta_carries_tokenizer_kind_and_both_budgets(store_env):
    ao = {"text": "n/a", "claims": []}
    arm = LLMDirect(store_env["store"], _contract_llm(ao), "fake",
                    context_budget_tokens=100_000,
                    tokenizer=_fake_bpe_tokenizer, seed=0,
                    max_model_len=5_000, answer_reserve_tokens=500)
    out = arm.answer("Who talks to whom?")
    meta = out["meta"]
    assert meta["tokenizer_kind"] == "injected"
    assert meta["budget_requested_tokens"] == 100_000
    assert meta["budget_effective_tokens"] == arm.context_budget_tokens
    assert meta["budget_effective_tokens"] < meta["budget_requested_tokens"]

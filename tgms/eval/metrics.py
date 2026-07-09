"""Metrics (WP2.6): answer scoring (EM/F1 per answer kind), PVR/ESR
aggregation, and the pre-registered statistical treatment (spec v1.1):
paired bootstrap over tasks, 10,000 resamples, 95% CIs; significance claimed
only when the CI of the paired difference excludes zero.
"""

from __future__ import annotations

import random
from typing import Any, Sequence

FLOAT_TOL = 1e-9


# --------------------------------------------------------------------------- #
# answer scoring                                                               #
# --------------------------------------------------------------------------- #

def _as_uid_set(value: Any) -> set[str]:
    if value is None:
        return set()
    out = set()
    for v in value if isinstance(value, (list, tuple, set)) else [value]:
        if isinstance(v, dict) and "uid" in v:
            out.add(str(v["uid"]))
        elif isinstance(v, str):
            out.add(v)
    return out


def _interval_of(value: Any) -> tuple[int, int] | None:
    if isinstance(value, dict):
        if "t_a" in value and "t_b" in value:
            return int(value["t_a"]), int(value["t_b"])
        if "start" in value and "end" in value:
            return int(value["start"]), int(value["end"])
    return None


def score_answer(kind: str, gold: Any, pred: Any) -> dict[str, float]:
    """EM/F1 per spec: exact for counts/values; set-F1 for entity sets;
    interval-IoU >= 0.5 counts as EM for interval answers."""
    if kind in ("count", "value"):
        if isinstance(gold, (int, float)) and isinstance(pred, (int, float)) \
                and not isinstance(gold, bool) and not isinstance(pred, bool):
            em = float(abs(float(gold) - float(pred)) <= FLOAT_TOL)
        else:
            em = float(gold == pred and gold is not None)
        return {"em": em, "f1": em}
    if kind == "entity_set":
        g, p = _as_uid_set(gold), _as_uid_set(pred)
        if not g and not p:
            return {"em": 1.0, "f1": 1.0}
        inter = len(g & p)
        prec = inter / len(p) if p else 0.0
        rec = inter / len(g) if g else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        return {"em": float(g == p), "f1": f1, "precision": prec, "recall": rec}
    if kind == "interval":
        gi, pi = _interval_of(gold), _interval_of(pred)
        if gi is None or pi is None:
            return {"em": 0.0, "f1": 0.0}
        inter = max(0, min(gi[1], pi[1]) - max(gi[0], pi[0]))
        union = max(gi[1], pi[1]) - min(gi[0], pi[0])
        iou = inter / union if union > 0 else 0.0
        return {"em": float(iou >= 0.5), "f1": iou}
    # series / paths / text: canonical equality
    from tgms.core.model import canonical_json
    em = float(canonical_json(gold) == canonical_json(pred))
    return {"em": em, "f1": em}


def extract_pred(kind: str, answer: Any) -> Any:
    """Pull the primary value out of a system answer: either a raw value
    (ours: trace.answer) or an AnswerObject (baselines/reporter)."""
    if isinstance(answer, dict) and "claims" in answer and "text" in answer:
        claims = answer["claims"]
        if kind in ("count", "value"):
            for c in claims:
                if c.get("type") in ("count", "value") and "value" in c:
                    return c["value"]
            return None
        if kind == "entity_set":
            uids: list[str] = []
            for c in claims:
                uids.extend(c.get("uids") or [])
            return uids
        if kind == "interval":
            for c in claims:
                if c.get("interval"):
                    return c["interval"]
                if c.get("type") == "value" and isinstance(c.get("value"), dict):
                    return c["value"]
            return None
        return answer.get("text")
    return answer


# --------------------------------------------------------------------------- #
# aggregate rates                                                              #
# --------------------------------------------------------------------------- #

def rates(rows: Sequence[dict[str, Any]]) -> dict[str, float]:
    """PVR / ESR / mean EM / mean F1 / mean UCR over per-task result rows.
    Row fields used: first_emission_valid, executed_ok, em, f1, ucr."""
    n = len(rows)
    if n == 0:
        return {"n": 0}

    def mean(key: str, only=None) -> float:
        vals = [r[key] for r in rows if key in r and r[key] is not None
                and (only is None or only(r))]
        return sum(vals) / len(vals) if vals else 0.0

    valid = [r for r in rows if r.get("first_emission_valid")]
    return {
        "n": n,
        "pvr": len(valid) / n,
        "esr": mean("executed_ok"),
        "em": mean("em"),
        "f1": mean("f1"),
        "ucr": mean("ucr"),
        "coverage": mean("coverage"),
    }


# --------------------------------------------------------------------------- #
# paired bootstrap (pre-registered statistical treatment)                      #
# --------------------------------------------------------------------------- #

def paired_bootstrap(a: Sequence[float], b: Sequence[float],
                     n_resamples: int = 10_000, seed: int = 0,
                     ci: float = 0.95) -> dict[str, Any]:
    """Paired bootstrap over tasks for the difference mean(a) - mean(b).
    Significance is claimed only when the CI excludes zero."""
    assert len(a) == len(b) and len(a) > 0, "paired scores required"
    rng = random.Random(seed)
    n = len(a)
    diffs = [x - y for x, y in zip(a, b)]
    point = sum(diffs) / n
    stats = []
    for _ in range(n_resamples):
        s = 0.0
        for _ in range(n):
            s += diffs[rng.randrange(n)]
        stats.append(s / n)
    stats.sort()
    lo_idx = int(((1 - ci) / 2) * n_resamples)
    hi_idx = min(n_resamples - 1, int((1 - (1 - ci) / 2) * n_resamples))
    lo, hi = stats[lo_idx], stats[hi_idx]
    return {
        "diff": point,
        "ci_low": lo,
        "ci_high": hi,
        "ci": ci,
        "n_tasks": n,
        "n_resamples": n_resamples,
        "significant": lo > 0 or hi < 0,
    }

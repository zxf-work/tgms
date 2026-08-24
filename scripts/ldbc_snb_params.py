"""Bind the 21 frozen LDBC plan artifacts to LDBC's own SF1 parameters (E13).

**This is not an LDBC Benchmark, this is not an implementation of an LDBC
Benchmark, and nothing produced here is an LDBC Benchmark Result.** LDBC
material is used under CC-BY 4.0.

The rules are frozen in `docs/design/PAPER_A_EVIDENCE_FREEZE.md` §A9 (the alias
table and the selection rule) and §A4 (identity), as amended by §E addenda 2
and 3. Nothing here is a binding decision made at implementation time.

**Selection rule (§A9, frozen before the files were read):** for each row, the
**first** parameter tuple in LDBC's own file order for SF1. No inspection of
result size, no re-draw, no picking one that returns rows. An empty result is
reported as an empty result with its tuple named.

**Identity (§A4):** every id-valued parameter goes through `snb_uid` — the same
single definition site the loader used. A parameter compared against an encoded
uid without the encoding matches nothing, and matches nothing *silently*, which
is the whole reason there is one function.

**Two parameters are derived, not bound (§A9):** `nearMaxHops` is
`minPathDistance - 1` and `deltaMicros` is LDBC's `delta` (a count of hours)
times 3,600,000,000. Both are bind-time derivations under R5; LDBC supplies
their inputs, not them.

**One parameter is a list (§E addendum 3):** BI12's `languages:STRING[]`. LDBC
supplies a variable-length list where the frozen artifact carries `$language1`
and `$language2`. The ruling is that a `STRING[]` parameter binds as an **n-way
OR expansion at bind time** (spec §4.3/§2.7) — the artifact is parameterized by
the rule and **never edited**. `expand_or_list` below is that rule.

    uv run python scripts/ldbc_snb_params.py --params <params-root> [--plan BI3]
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tgms.data.snb_loader import snb_uid  # noqa: E402

PLANS_DIR = ROOT / "benchmarks" / "tgir-v1" / "plans"

#: plan param -> the LDBC column/key it comes from (§A9's alias table). Where
#: the names already agree the entry is the identity, kept explicit so the
#: table is the whole contract rather than a set of exceptions.
BI_SOURCES: dict[str, tuple[str, dict[str, str]]] = {
    "BI3":  ("bi-3",   {"tagClass": "tagClass", "country": "country"}),
    "BI4":  ("bi-4",   {"date": "date"}),
    "BI6":  ("bi-6",   {"tagName": "tag"}),
    "BI7":  ("bi-7",   {"tagName": "tag"}),
    "BI9":  ("bi-9",   {"startDate": "startDate", "endDate": "endDate"}),
    "BI10": ("bi-10a", {"personId": "personId", "country": "country",
                        "tagClass": "tagClass",
                        "maxPathDistance": "maxPathDistance",
                        "_minPathDistance": "minPathDistance"}),
    "BI11": ("bi-11",  {"country": "country", "startDate": "startDate",
                        "endDate": "endDate"}),
    "BI12": ("bi-12",  {"startDate": "startDate",
                        "lengthThreshold": "lengthThreshold",
                        "_languages": "languages"}),
    "BI17": ("bi-17",  {"tagName": "tag", "_delta": "delta"}),
    "BI18": ("bi-18",  {"tagName": "tag"}),
}

#: Interactive rows read the validation-parameters file, which §A9 pre-registers
#: as an alternative parameter source. (Its *expected results* are a separate
#: question — the gold arm is UNAVAILABLE per §A10 caveat 2, because the first
#: operation in the file is an update, so no read precedes one.)
IV_SOURCES: dict[str, dict[str, str]] = {
    "IC2":  {"personId": "personIdQ2", "maxDate": "maxDate"},
    "IC5":  {"personId": "personIdQ5", "minDate": "minDate"},
    "IC6":  {"personId": "personIdQ6", "tagName": "tagName"},
    "IC8":  {"personId": "personIdQ8"},
    "IC9":  {"personId": "personIdQ9", "maxDate": "maxDate"},
    "IC11": {"personId": "personIdQ11", "countryName": "countryName",
             "workFromYear": "workFromYear"},
    "IC12": {"personId": "personIdQ12", "tagClassName": "tagClassName"},
    "IS2":  {"personId": "personIdSQ2"},
    "IS3":  {"personId": "personIdSQ3"},
    "IS6":  {"messageId": "messageForumId"},
    "IS7":  {"messageId": "messageRepliesId"},
}

#: id-valued parameters, and the id space they draw from (§A4).
ID_HIERARCHY = {"personId": "Person", "messageId": "Message"}

#: Validation-file temporal parameters are epoch **milliseconds** (raw JSON
#: ints), while the store keeps valid time in **microseconds** — SF1 reads
#: `vt_max = 1_354_157_561_840_001`. Binding a millisecond value against a
#: microsecond clock is off by a factor of 1,000 and fails *silently*: every
#: `creationDate < $maxDate` test would simply pass, and IC2/IC9 would return a
#: plausible-looking superset. The BI files avoid this only because their
#: headers declare `:DATE` and `_typed` converts.
#:
#: `workFromYear` is deliberately absent: it is a calendar year used as a plain
#: scalar (§A3 M4), not a clock, and scaling it would be the same error in the
#: other direction.
IV_MILLIS_PARAMS = {"maxDate", "minDate", "startDate", "endDate",
                    "creationDate", "joinDate"}

#: LDBC hours -> microseconds, for BI17's `delta`.
HOUR_US = 3_600_000_000


def date_to_us(text: str) -> int:
    """`2012-08-30` -> microseconds at midnight UTC."""
    return calendar.timegm((int(text[0:4]), int(text[5:7]), int(text[8:10]),
                            0, 0, 0, 0, 0, 0)) * 1_000_000


def _typed(name: str, raw: str) -> Any:
    """LDBC's parameter headers are `name:TYPE`; the type is the contract."""
    base, _, kind = name.partition(":")
    if kind == "ID" or kind == "INT":
        return int(raw)
    if kind == "DATE":
        return date_to_us(raw)
    if kind == "DATETIME":
        return date_to_us(raw[:10])
    if kind.endswith("[]"):
        return raw.split(";")
    return raw


def read_bi_first(params_root: Path, sf: str, name: str) -> dict[str, Any]:
    """The first tuple of a BI parameter file, typed by its own header."""
    path = (params_root / "bi" / "ldbc-snb-bi-parameters-sf1-to-sf30000"
            / f"parameters-{sf}" / f"{name}.csv")
    with open(path, encoding="utf-8") as f:
        header = f.readline().rstrip("\n").split("|")
        row = f.readline().rstrip("\n").split("|")
    if len(header) != len(row):
        raise ValueError(f"{path}: header/row arity mismatch")
    return {h.partition(":")[0]: _typed(h, v) for h, v in zip(header, row)}


def iv_rows(params_root: Path, sf: str) -> Iterator[dict[str, Any]]:
    """Validation-parameter rows as `{params}`, in file order."""
    path = params_root / "iv" / f"validation_params-{sf}.csv"
    with open(path, encoding="utf-8") as f:
        for line in f:
            head, _, _rest = line.partition("|")
            if not head.startswith("{"):
                continue
            try:
                yield json.loads(head)
            except json.JSONDecodeError:
                continue


def read_iv_first(params_root: Path, sf: str,
                  wanted: dict[str, str]) -> dict[str, Any]:
    """The first row carrying **every** key this plan needs — the file-order
    rule of §A9, applied to a file that interleaves every operation type."""
    keys = set(wanted.values())
    for row in iv_rows(params_root, sf):
        if keys <= set(row):
            return {k: row[k] for k in keys}
    raise LookupError(f"no validation row carries all of {sorted(keys)}")


def expand_or_list(node: Any, prefix: str, values: list[str]) -> Any:
    """§E addendum 3: a `STRING[]` parameter is an **n-way OR expansion**.

    Finds the `or` subtree whose leaves compare one expression against
    `$<prefix>1..k`, and rebuilds it with one disjunct per supplied value over
    the *same* left-hand expression. The checked-in artifact is untouched; only
    the bound document differs, which is what "parameterized by the rule, never
    edited" means.

    Right-nested, so `[a, b, c]` becomes `a ∨ (b ∨ c)` — the shape the frozen
    two-value artifact already has, extended rather than re-associated.
    """
    def leaves(n: Any) -> list[Any] | None:
        if not isinstance(n, dict):
            return None
        if n.get("bool") == "or":
            left, right = leaves(n.get("l")), leaves(n.get("r"))
            if left is None or right is None:
                return None
            return left + right
        if n.get("cmp") == "=" and isinstance(n.get("r"), dict):
            lit = n["r"].get("lit")
            if isinstance(lit, str) and lit.startswith("$" + prefix):
                return [n]
        return None

    def rebuild(n: Any) -> Any:
        if isinstance(n, dict):
            got = leaves(n)
            if got:
                lhs = got[0]["l"]
                chain: Any = {"cmp": "=", "l": lhs, "r": {"lit": values[-1]}}
                for v in reversed(values[:-1]):
                    chain = {"bool": "or",
                             "l": {"cmp": "=", "l": lhs, "r": {"lit": v}},
                             "r": chain}
                return chain
            return {k: rebuild(v) for k, v in n.items()}
        if isinstance(n, list):
            return [rebuild(v) for v in n]
        return n

    return rebuild(node)


# --------------------------------------------------------------------------
# §E addendum 4 — the secondary (characterization) arm
# --------------------------------------------------------------------------

#: The campaign seed. The freeze fixed no seed, so it is derived here from the
#: freeze id itself rather than chosen: any other constant would be a number
#: somebody picked, and a picked seed is a knob. Recorded per plan alongside
#: the drawn index so the whole draw replays from this file plus the CSVs.
CAMPAIGN_SEED_SOURCE = "paper-a-v1"
CAMPAIGN_SEED = int.from_bytes(
    hashlib.sha256(CAMPAIGN_SEED_SOURCE.encode()).digest()[:8], "big")

#: Which id space each Interactive id-valued parameter is drawn from.
#: `messageId` draws from the Message hierarchy, which is Post ∪ Comment — the
#: same union IS6/IS7 traverse and the same one LDBC guarantees disjoint.
SAMPLE_POPULATION = {"personId": ("Person",), "messageId": ("Post", "Comment")}

_POP_CACHE: dict[tuple[str, ...], list[int]] = {}


def population(csv_root: Path, files: tuple[str, ...]) -> list[int]:
    """Every LDBC id of the given node files, in the frozen file order.

    Read from the CSVs rather than the store because the store exposes no way
    to enumerate entities of a label cheaply — `nodes_columnar` and
    `all_node_versions` both build a Python object per version and take ~50 min
    at SF1. The CSVs are the artifact the store was built from, and every drawn
    anchor is verified against the store anyway.
    """
    if files in _POP_CACHE:
        return _POP_CACHE[files]
    from tgms.data.snb_loader import NODES, _parts, _rows

    ids: list[int] = []
    for name in files:
        spec = next(n for n in NODES if n.name == name)
        for row in _rows(_parts(csv_root, spec.group, spec.name), spec.header):
            ids.append(int(row[spec.id_col]))
    _POP_CACHE[files] = ids
    return ids


def sample_anchor(plan_id: str, param: str, csv_root: Path,
                  seed: int = CAMPAIGN_SEED) -> dict[str, Any]:
    """Draw one anchor, deterministically and blind.

    **The rule, fixed before any draw:** the anchor is the element at index
    `k = H(seed, plan_id, param) mod |population|` of the population listed in
    the frozen file order, where `H` is SHA-256 of the three joined by NUL. It
    depends on nothing but the seed, the plan, the parameter and the corpus —
    in particular not on any result, any row count, or whether the plan returns
    anything. Re-drawing on an empty result would be choosing.
    """
    files = SAMPLE_POPULATION[param]
    ids = population(csv_root, files)
    key = b"\0".join([str(seed).encode(), plan_id.encode(), param.encode()])
    k = int.from_bytes(hashlib.sha256(key).digest()[:8], "big") % len(ids)
    hierarchy = "Person" if files == ("Person",) else "Message"
    return {"param": param, "population": "+".join(files), "size": len(ids),
            "index": k, "ldbc_id": ids[k], "seed": seed,
            "uid": snb_uid(hierarchy, ids[k])}


class PhantomAnchor(LookupError):
    """A bound id-valued parameter names an entity the store does not have."""


def verify_anchors(bound: dict[str, Any], adapter: Any) -> list[str]:
    """Probe every id-valued parameter against the store. Cheap — one
    `dense_ids` call over a handful of uids.

    **Why this exists.** `NodeScan(uids=[...])` on a uid the store never saw is
    not an error: it is an empty domain, so the plan degrades to a full scan
    that returns nothing, slowly. The first SF1 campaign spent hours that way —
    every Interactive plan anchored on an id from a *different* LDBC dataset
    (§A9's parameter-source defect), and the result was indistinguishable from
    "this plan is expensive". A phantom anchor must fail loudly at bind time,
    naming the id, or it silently becomes a performance measurement of nothing.
    """
    uids = [str(v) for k, v in bound.items() if k in ID_HIERARCHY]
    if not uids:
        return []
    missing = []
    for uid in uids:                       # one at a time: we want the name
        try:
            adapter.dense_ids([uid])
        except Exception:                  # noqa: BLE001 — NotFoundError et al
            missing.append(uid)
    return missing


def bind(plan_id: str, params_root: Path, sf: str = "sf1",
         adapter: Any = None, csv_root: Path | None = None,
         seed: int = CAMPAIGN_SEED) -> dict[str, Any]:
    """Return the bound plan document plus a record of what was bound.

    Pass `adapter` to have every id-valued parameter checked for existence; a
    miss raises `PhantomAnchor` rather than producing a plan that scans the
    whole store to find nothing.
    """
    doc = json.loads((PLANS_DIR / f"{plan_id}.json").read_text())
    frozen = dict(doc.get("params", {}))

    if plan_id in BI_SOURCES:
        name, alias = BI_SOURCES[plan_id]
        raw = read_bi_first(params_root, sf, name)
        source = f"{name}.csv row 1"
    elif plan_id in IV_SOURCES:
        alias = IV_SOURCES[plan_id]
        raw = read_iv_first(params_root, sf, alias)
        source = f"validation_params-{sf}.csv, first row with all keys"
    else:
        raise KeyError(f"{plan_id} has no frozen parameter source")

    bound: dict[str, Any] = {}
    for plan_key, ldbc_key in alias.items():
        value = raw[ldbc_key]
        if plan_key in ID_HIERARCHY:
            value = snb_uid(ID_HIERARCHY[plan_key], value)
        elif plan_id in IV_SOURCES and ldbc_key in IV_MILLIS_PARAMS:
            value = int(value) * 1000
        bound[plan_key] = value

    # derived, not bound (§A9)
    if "_minPathDistance" in bound:
        bound["nearMaxHops"] = int(bound.pop("_minPathDistance")) - 1
    if "_delta" in bound:
        bound["deltaMicros"] = int(bound.pop("_delta")) * HOUR_US
    languages = bound.pop("_languages", None)

    root = doc["root"]
    expanded = None
    if languages is not None:
        root = expand_or_list(root, "language", list(languages))
        expanded = {"param": "languages", "values": list(languages),
                    "disjuncts": len(languages)}
        for k in [k for k in frozen if k.startswith("language")]:
            frozen.pop(k, None)

    #: §E addendum 4: the Interactive rows have no valid third-party parameter
    #: source against this substrate (their ids come from the separately
    #: generated Interactive dataset), so their anchors are sampled from the
    #: store's own corpus. This arm is **characterization only** and is
    #: excluded from every third-party-parameter claim.
    sampled: dict[str, Any] = {}
    if csv_root is not None and plan_id in IV_SOURCES:
        for key in [k for k in bound if k in SAMPLE_POPULATION]:
            draw = sample_anchor(plan_id, key, csv_root, seed)
            bound[key] = draw["uid"]
            sampled[key] = draw
        source = (f"SAMPLED anchors (seed {seed}) + "
                  f"non-id parameters from {source}")

    phantom: list[str] = []
    if adapter is not None:
        phantom = verify_anchors(bound, adapter)
        if phantom:
            raise PhantomAnchor(
                f"{plan_id}: bound id(s) {phantom} name no entity in the store "
                f"(source: {source}). Binding them would make the plan scan the "
                f"whole store and return nothing."
            )

    missing = [k for k in frozen if k not in bound]
    return {"plan_id": plan_id, "root": root, "params": bound,
            "phantom_anchors": phantom,
            "sampled_anchors": sampled,
            "arm": ("characterization-interactive" if plan_id in IV_SOURCES
                    else "scored-bi"),
            "plan_format": doc.get("plan_format"),
            "frozen_params": doc.get("params", {}), "source": source,
            "or_expansion": expanded, "unbound_frozen_params": missing,
            "sigma": doc.get("sigma")}


def substitute(node: Any, params: dict[str, Any]) -> Any:
    """`$name` -> its bound value, everywhere. Same rule `tgir_run.py` uses."""
    if isinstance(node, str) and node.startswith("$"):
        return params.get(node[1:], node)
    if isinstance(node, dict):
        return {k: substitute(v, params) for k, v in node.items()}
    if isinstance(node, list):
        return [substitute(v, params) for v in node]
    return node


LDBC_PLANS = sorted(set(BI_SOURCES) | set(IV_SOURCES))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params", required=True, help="the params root")
    ap.add_argument("--sf", default="sf1")
    ap.add_argument("--plan", default="all")
    args = ap.parse_args()
    ids = LDBC_PLANS if args.plan == "all" else [args.plan]
    out = {}
    for pid in ids:
        b = bind(pid, Path(args.params), args.sf)
        out[pid] = {k: b[k] for k in
                    ("params", "source", "or_expansion", "unbound_frozen_params")}
        print(f"{pid:6s} {b['source']:<48} {json.dumps(b['params'], default=str)}")
        if b["or_expansion"]:
            print(f"       OR-expanded: {b['or_expansion']}")
        if b["unbound_frozen_params"]:
            print(f"       UNBOUND: {b['unbound_frozen_params']}")
    print(json.dumps(out, indent=1, default=str), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

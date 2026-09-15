"""The E13 campaign: the frozen LDBC plans against the real SF1 instance.

The plan list is `ldbc_snb_params.LDBC_PLANS` — E13's frozen 21, plus IS1/IS4/IS5
added post-freeze (Lane D2, 2026-09-14). Nothing here enumerates plan ids, so a
row added to the binder's alias table is a row this runner already knows.
`BI6.v2` is deliberately **not** in that list yet: it needs a `BI_SOURCES` row
(`"BI6.v2": ("bi-6", {"tagName": "tag"})`) before it can be bound, which is the
coordinator's call together with the BI6 rerun.

**This is not an LDBC Benchmark, this is not an implementation of an LDBC
Benchmark, and nothing produced here is an LDBC Benchmark Result.**

What it does, per `docs/design/PAPER_A_EVIDENCE_FREEZE.md` §B and §C5:

1. **Bind** each plan to LDBC's own SF1 parameters (`ldbc_snb_params.py`). No
   plan is edited: `sigma` is carried through untouched (§A3 M11 — narrowing it
   is the one lever that would turn refusals into admissions, so it is closed by
   rule) and only `params` are rebound (§A3 M12).
2. **Re-derive** the admission estimate under the bound parameters. The frozen
   table was computed with the artifacts' fixture parameters; binding real ones
   changes the inputs, so any verdict flip is the forecast under test and is
   reported as a scored result, not repaired (§E addendum 3).
3. **Admit or refuse** at the frozen policy, recording the `RefusalCertificate`
   whenever it refuses.
4. **Execute** the admitted plans under the §C4 timing protocol.
5. **Re-run the refused plans with the guard bypassed but recording**, under a
   hard wall ceiling (§C5) — the D-086 method, which is what turns "10 of 21
   refuse" from an anecdote into a classifier score. A timeout is recorded as
   `TIMEOUT` and *is* a true rejection at a 10 s budget, whatever the plan would
   eventually have done.

Outcome classes are reported separately and never absorbed into one another.

    uv run python scripts/tgir_ldbc_sf1.py --store stores/snb-sf1 \
        --params /mnt/project/xzhang/tgms/ldbc-sf1/params --out benchmarks/results-v1/ldbc-sf1.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ldbc_compare import load_sort_keys, lookup_sort_keys  # noqa: E402
from ldbc_snb_params import (  # noqa: E402
    CAMPAIGN_SEED, CAMPAIGN_SEED_SOURCE, LDBC_PLANS, PhantomAnchor, bind,
    export_bindings, substitute,
)
from tgms.core.errors import CostError, TgmsError  # noqa: E402
from tgms.data.snb_loader import HIERARCHY_STRIDE, HIERARCHY_TAG, uid_to_ldbc_id  # noqa: E402
from tgms.temporal.algebra import ensure_all_registered  # noqa: E402
from tgms.temporal.guardrails import DEFAULT_CEILINGS  # noqa: E402
from tgms.tgir.admission import POLICY_VERSION, plan_estimate  # noqa: E402
from tgms.tgir.execute import run_plan  # noqa: E402
from tgms.tgir.loader import load  # noqa: E402

#: The inverse of `HIERARCHY_TAG`, for `--emit-rows`'s decoded sibling column.
_TAG_HIERARCHY: dict[int, str] = {v: k for k, v in HIERARCHY_TAG.items()}

#: The 24 vendored templates' declared `ORDER BY` columns (`{plan_id: []}`
#: when a template's own query carries no `ORDER BY` at all — "order-free" in
#: `rows_digest`'s rule below), the same source `ldbc_compare.py --sort-keys`
#: reads for its top-k tie rule. Loaded once; `sort_keys.yaml` does not change
#: mid-campaign.
SORT_KEYS_PATH = ROOT / "benchmarks" / "ldbc-ref-v1" / "sort_keys.yaml"

#: `rows_digest`'s own rule, carried in every record's `manifest` so a reader
#: two years from now does not have to find this file to know what the field
#: means. Kept as one string, not re-derived from docstrings, so the manifest
#: and this module cannot silently drift apart.
ROWS_DIGEST_RULE = (
    "sha256 over the plan's decoded result rows (the --emit-rows LDBC "
    "decode: uid columns -> plain LDBC id plus a <col>__hierarchy sibling, "
    "every other column passed through), each row serialized with "
    "json.dumps(row, sort_keys=True, default=str) and the per-row strings "
    "newline-joined in one order: for a template whose sort_keys.yaml "
    "order_by is empty ('order-free' -- the vendored query carries no "
    "ORDER BY of its own), the per-row strings are sorted lexicographically "
    "before hashing, so any permutation of the same rows digests identically; "
    "for a template with a declared order_by, the rows are hashed in the "
    "engine's own returned order, unsorted, so a change in that order changes "
    "the digest. A plan id with a template alias suffix (e.g. BI6.v2) resolves "
    "to its base template's order_by the same way ldbc_compare.py's "
    "lookup_sort_keys does; a plan absent from sort_keys.yaml is treated as "
    "order-free (no known order to preserve). This never changes plan "
    "execution, timings, or any existing record field -- rows_digest is an "
    "additive key computed from the same envelope --emit-rows already reads."
)


def _sort_key_columns() -> dict[str, list[str]]:
    """`{plan_id: [order_by columns]}`, cached at module scope — see
    `SORT_KEYS_PATH`."""
    if not hasattr(_sort_key_columns, "_cache"):
        _sort_key_columns._cache = load_sort_keys(SORT_KEYS_PATH)  # type: ignore[attr-defined]
    return _sort_key_columns._cache  # type: ignore[attr-defined]


def _is_order_free(plan_id: str) -> bool:
    """True when `plan_id`'s vendored template carries no `ORDER BY` (an
    empty `order_by` in `sort_keys.yaml`), or is not in `sort_keys.yaml` at
    all — both cases mean `rows_digest` has no declared row order to trust,
    so it canonicalizes by sorting the decoded rows instead of hashing
    them in returned order."""
    return not lookup_sort_keys(_sort_key_columns(), plan_id)


def _sort_keys_sha256() -> str:
    """sha256 of `sort_keys.yaml`'s own bytes, so a campaign record pins
    exactly which version of the order-free/ordered classification produced
    its `rows_digest` values."""
    return hashlib.sha256(SORT_KEYS_PATH.read_bytes()).hexdigest()


def compute_rows_digest(decoded_rows: list[dict[str, Any]],
                        order_free: bool) -> str:
    """`ROWS_DIGEST_RULE`, applied. `decoded_rows` is the same LDBC-decoded
    row list `write_rows_export` writes (see `decode_rows`) — decode happens
    once, upstream, so a digest and a `--emit-rows` dump of the same run are
    always over the same row values."""
    keys = [json.dumps(row, sort_keys=True, default=str) for row in decoded_rows]
    if order_free:
        keys.sort()
    h = hashlib.sha256()
    for k in keys:
        h.update(k.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()

#: §C5. The ceiling `external_workloads/FREEZE.md` already fixed for BIRD gold
#: validation, adopted for continuity. No plan runs unbounded.
#:
#: **Enforced, not observed.** The first version of this runner compared elapsed
#: time against the ceiling *after* `run_plan` returned, which bounds nothing:
#: the first campaign spent 870 s inside BI10 without stopping, against a plan
#: whose estimate is 1.7e5 ms and against BI17 whose estimate is 1.3e9 ms — two
#: weeks. A ceiling that cannot interrupt is a label.
#:
#: Enforcement is a subprocess per bypassed plan, because the work happens
#: inside a Rust call that a Python-level signal cannot reliably interrupt;
#: only killing the process is certain.
BYPASS_CEILING_S = 600

#: Extra wall the child gets on top of the ceiling, for opening the store. The
#: SF1 warm-up is ~165 s (the in-process index build over 20.4M versions) and it
#: is not part of what the ceiling is meant to bound, so the child measures the
#: plan alone and the parent allows for the open.
CHILD_OPEN_ALLOWANCE_S = 420

#: §C4's protocol, reduced for plans that are seconds rather than milliseconds:
#: the campaign is about admission and completion, not about a p95 on a scan.
WARMUPS = 1
REPS = 3


def _sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:                                  # noqa: BLE001
        return "unknown"


def _load_bound(plan_id: str, params_root: Path, sf: str,
                adapter: Any = None,
                csv_root: Path | None = None) -> tuple[Any, dict[str, Any]]:
    b = bind(plan_id, params_root, sf, adapter, csv_root)
    document = substitute({"root": b["root"], "sigma": b["sigma"]}, b["params"])
    # `plan_format` is the reader's version gate; the bound document is the
    # frozen artifact re-parameterised, so it carries the artifact's own.
    return load({"plan_format": b["plan_format"], "plan_id": plan_id,
                 **document}), b


def _time(fn: Any, reps: int) -> tuple[list[float], Any]:
    out = None
    for _ in range(WARMUPS):
        out = fn()
    times = []
    for _ in range(reps):
        t = time.time()
        out = fn()
        times.append((time.time() - t) * 1000.0)
    return times, out


def _last_json(text: str, phase: str | None) -> dict[str, Any]:
    """The last JSON line of a child, optionally restricted to one phase.

    `TimeoutExpired` carries whatever the child managed to write, which is why
    the child emits its identity and estimate up front.
    """
    out: dict[str, Any] = {}
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            got = json.loads(line)
        except json.JSONDecodeError:
            continue
        if phase is None and got.get("phase") == "pre":
            continue
        if phase is not None and got.get("phase") != phase:
            continue
        out = {k: v for k, v in got.items() if k != "phase"}
    return out


def _decode_row(row: dict[str, Any], schema: list[list[str]]) -> dict[str, Any]:
    """One result row, LDBC-decoded (`--emit-rows`).

    Every `uid` column becomes a plain LDBC id (`uid_to_ldbc_id`) plus a
    `<name>__hierarchy` sibling carrying the tag the decode consumed —
    decoding throws the hierarchy away, and a Post/Comment row decoded
    through the shared `Message` slot (M6) needs that tag on hand for
    `ldbc_compare.py` to name the union it applied, not just a bare int.
    Every other column is passed through unchanged; comparison-side
    normalization (ms/µs, NFC, float tolerance) is `ldbc_compare.py`'s job,
    not the exporter's.
    """
    out: dict[str, Any] = {}
    for name, tau in schema:
        value = row.get(name)
        if tau.rstrip("?") == "uid" and value is not None:
            out[name] = uid_to_ldbc_id(value)
            out[f"{name}__hierarchy"] = _TAG_HIERARCHY.get(
                int(value) % HIERARCHY_STRIDE, "?")
        else:
            out[name] = value
    return out


def decode_rows(envelope: dict[str, Any]
                ) -> tuple[list[list[str]], list[dict[str, Any]]]:
    """`(schema, decoded_rows)` for one plan's envelope — the LDBC decode
    both `write_rows_export` and `rows_digest` (`run_one`) apply, factored out
    so a digest and a `--emit-rows` dump of the same run are always computed
    from the identical decoded values."""
    schema = envelope.get("tgir", {}).get("schema", [])
    rows = [_decode_row(r, schema) for r in envelope.get("rows", [])]
    return schema, rows


def write_rows_export(out_dir: Path, plan_id: str, envelope: dict[str, Any],
                      params: dict[str, Any], arm: str, commit: str) -> Path:
    """`--emit-rows`: the envelope's full result, LDBC-decoded, to
    `<dir>/tgms-<ID>.json`. Column order is the envelope's own declared
    schema (`tgir.schema`) — never re-sorted, since "the envelope's declared
    column order" is itself part of the comparison contract (design §6.1).
    A sibling file; the existing campaign record format is untouched.
    """
    schema, rows = decode_rows(envelope)
    columns = [c[0] for c in schema]
    doc = {
        "plan_id": plan_id, "schema": schema, "columns": columns, "rows": rows,
        "result_digest": envelope.get("result_digest"),
        "plan_digest": envelope.get("tgir", {}).get("plan_digest"),
        "params": params, "arm": arm, "commit": commit,
        "host": platform.node(),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"tgms-{plan_id}.json"
    path.write_text(json.dumps(doc, indent=1, sort_keys=True, default=str))
    return path


def run_child(plan_id: str, store_path: str, params_root: Path,
              sf: str, csv_root: Path | None = None,
              emit_rows_dir: Path | None = None) -> dict[str, Any]:
    """Run one plan in a child under a hard kill.

    **Every** plan goes through here, not only the refused ones. The first
    bounded campaign bounded the bypassed arm and left the admitted arm
    in-process on the reasoning that an admitted plan is predicted under 6 s —
    which is exactly the assumption the experiment exists to test. BI18 was
    admitted at an estimate of 5,918 ms and had run **49 minutes** when the
    campaign was killed, so the arm that needed no bound was the arm that ran
    away. A ceiling that only guards the cases you expect to be slow is not a
    ceiling.

    The child measures `run_plan` alone; the parent allows it
    `BYPASS_CEILING_S + CHILD_OPEN_ALLOWANCE_S` of wall so that the store open
    is not charged against the ceiling. A kill is recorded as `TIMEOUT`, which
    at a 10 s budget **is** a true rejection whatever the plan would eventually
    have done — the point of the arm is the classifier score, not the runtime.
    """
    cmd = [sys.executable, "-u", str(Path(__file__).resolve()),
           "--single", plan_id, "--store", store_path,
           "--params", str(params_root), "--sf", sf, "--out", os.devnull]
    if csv_root is not None:
        cmd += ["--csv", str(csv_root)]
    if emit_rows_dir is not None:
        cmd += ["--emit-rows", str(emit_rows_dir)]
    t0 = time.time()
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=BYPASS_CEILING_S + CHILD_OPEN_ALLOWANCE_S,
                              cwd=ROOT)
    except subprocess.TimeoutExpired as e:
        pre = _last_json(e.stdout or "", phase="pre")
        return {**pre, "plan_id": plan_id, "outcome": "TIMEOUT",
                "wall_s": round(time.time() - t0, 1),
                "note": f"killed at the {BYPASS_CEILING_S}s ceiling "
                        f"(+{CHILD_OPEN_ALLOWANCE_S}s store-open allowance)"}
    final = _last_json(done.stdout, phase=None)
    if final:
        if final.get("ms", 0) > BYPASS_CEILING_S * 1000:
            final["outcome"] = "TIMEOUT"
        final.pop("phase", None)
        return final
    pre = _last_json(done.stdout, phase="pre")
    return {**pre, "plan_id": plan_id, "outcome": "ERRORED",
            "error": (done.stderr or done.stdout)[-300:]}


def run_one(plan_id: str, store: Any, params_root: Path, sf: str,
            bypass: bool, emit_pre: bool = False,
            csv_root: Path | None = None,
            emit_rows_dir: Path | None = None) -> dict[str, Any]:
    rec: dict[str, Any] = {"plan_id": plan_id}
    try:
        root, b = _load_bound(plan_id, params_root, sf, store.adapter, csv_root)
    except PhantomAnchor as e:
        rec.update(outcome="BIND_FAILED", phantom_anchor=True, error=str(e))
        if emit_pre:
            print(json.dumps({"phase": "pre", **rec}, default=str), flush=True)
        return rec
    except Exception as e:                             # noqa: BLE001
        rec.update(outcome="BIND_FAILED", error=f"{type(e).__name__}: {e}")
        if emit_pre:
            print(json.dumps({"phase": "pre", **rec}, default=str), flush=True)
        return rec
    rec["params"] = b["params"]
    rec["param_source"] = b["source"]
    rec["or_expansion"] = b["or_expansion"]
    rec["arm"] = b["arm"]
    rec["sampled_anchors"] = b["sampled_anchors"]

    stats = store.stats()
    est = plan_estimate(root, stats)
    hits = [k for k, c in DEFAULT_CEILINGS.items() if est.get(k, 0) > c]
    rec["estimate"] = {k: est[k] for k in
                       ("time_est_ms", "rows_scanned_est", "expansions_est")}
    rec["ceilings_hit"] = hits
    rec["derived_admission"] = "refuse" if hits else "admit"
    rec["policy_version"] = POLICY_VERSION
    # Emitted before a single row is read, so that a child killed at the
    # ceiling still leaves an attributable record. The first bounded campaign
    # produced thirteen rows of {bypassed, note, outcome, wall_s} with no
    # plan_id and no estimate, because the parent stopped computing anything
    # when every plan moved into a child.
    if emit_pre:
        print(json.dumps({"phase": "pre", **rec}, default=str), flush=True)

    ceilings = None
    if hits:
        # capture the certificate the policy would issue, then decide whether
        # to run anyway with the guard bypassed-but-recorded (§C5)
        try:
            from tgms.tgir.admission import admit
            admit(root, stats, plan_id)
        except CostError as e:
            rec["refusal_certificate"] = e.details.get("refusal_certificate")
        if not bypass:
            rec["outcome"] = "REFUSED"
            return rec
        ceilings = {k: 1 << 62 for k in DEFAULT_CEILINGS}
        rec["bypassed"] = True

    t0 = time.time()
    try:
        def go() -> Any:
            return run_plan(root, store.adapter, tt_source=store,
                            limit=100_000, plan_id=plan_id, cost_ceilings=ceilings)

        reps = 1 if hits else REPS
        times, envelope = _time(go, reps)
        rec["outcome"] = "COMPLETED"
        rec["ms"] = sorted(times)[len(times) // 2]
        rec["ms_all"] = [round(x, 3) for x in times]
        rec["rows"] = len(envelope.get("rows", []))
        rec["completeness"] = envelope.get("completeness")
        _, decoded_rows = decode_rows(envelope)
        rec["rows_digest"] = compute_rows_digest(
            decoded_rows, order_free=_is_order_free(plan_id))
        if emit_rows_dir is not None:
            write_rows_export(emit_rows_dir, plan_id, envelope, rec["params"],
                              rec["arm"], _sha())
    except CostError as e:
        rec["outcome"] = "REFUSED_ON_RUN"
        rec["error"] = e.to_payload().get("code")
    except TgmsError as e:
        rec["outcome"] = "ERRORED"
        rec["error"] = e.to_payload().get("code")
        rec["error_detail"] = str(e)[:300]
    except Exception as e:                             # noqa: BLE001
        rec["outcome"] = "ERRORED"
        rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    rec["wall_s"] = round(time.time() - t0, 3)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--params", required=True)
    ap.add_argument("--csv", default="",
                    help="the initial_snapshot directory. Enables the §E "
                         "addendum 4 characterization arm: the Interactive "
                         "rows, whose LDBC parameters name entities of a "
                         "different dataset, get seeded anchors sampled from "
                         "this corpus. Omit and they fail as BIND_FAILED.")
    ap.add_argument("--sf", default="sf1")
    ap.add_argument("--out", required=True)
    ap.add_argument("--plan", default="all")
    ap.add_argument("--single", default="",
                    help="internal: run one plan, guard bypassed, print one "
                         "JSON record. Used by the parent to enforce the "
                         "ceiling with a kill.")
    ap.add_argument("--no-bypass", action="store_true",
                    help="skip the guard-bypassed re-run of refused plans")
    ap.add_argument("--emit-rows", default="",
                    help="write each plan's full LDBC-decoded result rows to "
                         "<dir>/tgms-<ID>.json, alongside the usual row-COUNT "
                         "record (design D1 §6.1)")
    ap.add_argument("--export-params", default="",
                    help="write one params.json (LDBC-native `cypher` values "
                         "plus the already-bound `tgir` values, per plan) to "
                         "this path — a side artifact, read by both runners "
                         "(design D1 §3); does not change the campaign run")
    args = ap.parse_args()

    import tgms

    ensure_all_registered()

    if args.single:
        store = tgms.open(args.store, read_only=True)
        try:
            rec = run_one(args.single, store, Path(args.params), args.sf,
                          bypass=not args.no_bypass, emit_pre=True,
                          csv_root=Path(args.csv) if args.csv else None,
                          emit_rows_dir=(Path(args.emit_rows)
                                        if args.emit_rows else None))
        finally:
            store.close()
        print(json.dumps({"phase": "final", **rec}, default=str))
        return 0

    sha = _sha()

    if args.export_params:
        store = tgms.open(args.store, read_only=True)
        try:
            params_doc = export_bindings(
                Path(args.params), args.sf,
                Path(args.csv) if args.csv else None,
                adapter=store.adapter, campaign_commit=sha)
        finally:
            store.close()
        out_path = Path(args.export_params)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(params_doc, indent=1, sort_keys=True,
                                       default=str))
        print(f"params exported: {out_path}")
    ids = LDBC_PLANS if args.plan == "all" else [args.plan]
    print(f"RUN_STARTED commit={sha} store={args.store} sf={args.sf} "
          f"plans={len(ids)} policy={POLICY_VERSION} host={platform.node()}",
          flush=True)

    t0 = time.time()
    records: list[dict[str, Any]] = []
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    def flush() -> None:
        """After every plan, not at the end. The first bounded campaign wrote
        its record only on completion, so killing it at 2 h discarded four
        finished measurements that had to be read back out of the log."""
        out.write_text(json.dumps({"manifest": manifest(), "records": records},
                                  indent=1, sort_keys=True, default=str))

    def manifest() -> dict[str, Any]:
        return {"commit": sha, "host": platform.node(),
                "platform": platform.platform(), "store": args.store,
                "sf": args.sf, "policy_version": POLICY_VERSION,
                "ceilings": dict(DEFAULT_CEILINGS),
                "bypass_ceiling_s": BYPASS_CEILING_S,
                "child_open_allowance_s": CHILD_OPEN_ALLOWANCE_S,
                "every_plan_in_a_child": True,
                "campaign_seed": CAMPAIGN_SEED,
                "campaign_seed_source": CAMPAIGN_SEED_SOURCE,
                "csv_root": args.csv,
                "rows_digest_rule": ROWS_DIGEST_RULE,
                "sort_keys_sha256": _sort_keys_sha256(),
                "arms": {
                    "scored-bi": "the 10 BI rows, bound to LDBC's own SF1 "
                                 "parameters. The only arm that carries a "
                                 "third-party-parameter claim.",
                    "characterization-interactive":
                        "the 11 Interactive rows, whose LDBC parameters name "
                        "entities of the separately generated Interactive "
                        "dataset (§E addendum 4). Anchors are seeded draws "
                        "from this corpus; execution-at-scale characterization "
                        "only. Never summed with the scored arm.",
                },
                "protocol": f"warmups {WARMUPS}, reps {REPS} (1 when bypassed)",
                "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "wall_s": round(time.time() - t0, 1),
                "complete": len(records) == len(ids)}

    for pid in ids:
        t = time.time()
        rec = run_child(pid, args.store, Path(args.params), args.sf,
                        Path(args.csv) if args.csv else None,
                        Path(args.emit_rows) if args.emit_rows else None)
        records.append(rec)
        flush()
        print(f"  {pid:6s} {rec.get('derived_admission', '—'):>6} -> "
              f"{rec.get('outcome', '?'):<14} "
              f"est {rec.get('estimate', {}).get('time_est_ms', '?'):>12} ms  "
              f"actual {rec.get('ms', '—')!s:>12}  "
              f"rows {rec.get('rows', '—')!s:>8}  "
              f"[{time.time() - t:.1f}s]", flush=True)

    flush()

    # The two arms are reported apart and never added together: one carries a
    # third-party-parameter claim and the other cannot.
    for arm, title in (("scored-bi", "SCORED — BI rows, LDBC parameters"),
                       ("characterization-interactive",
                        "CHARACTERIZATION — Interactive rows, sampled anchors")):
        rows = [r for r in records if r.get("arm") == arm]
        if not rows:
            continue
        ref = sum(1 for r in rows if r.get("derived_admission") == "refuse")
        adm = sum(1 for r in rows if r.get("derived_admission") == "admit")
        print(f"\n{title}  ({len(rows)} plans)")
        print(f"  derived: {ref} refuse / {adm} admit")
        for name in ("COMPLETED", "REFUSED", "REFUSED_ON_RUN", "TIMEOUT",
                     "ERRORED", "BIND_FAILED"):
            n = sum(1 for r in rows if r.get("outcome") == name)
            if n:
                print(f"    {name:<16} {n}")
    orphan = [r for r in records if not r.get("arm")]
    if orphan:
        print(f"\nno arm recorded: {len(orphan)} "
              f"(bind failed before the arm was known)")
    print(f"record: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

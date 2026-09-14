"""CLI (spec 7.3.3): thin argparse wrappers over the library — no logic here."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


#: What `--backend` means when it is not given: nothing, so `tgms.open`
#: decides — an existing store keeps the layout it was written with, and a
#: new one gets `DEFAULT_BACKEND` (native, D-028). This used to be the
#: literal "duckdb", which made the documented first command of a clean
#: `pip install tgms` fail with "this store uses the duckdb backend, which
#: is now an optional extra" — the CLI asked for a backend the wheel no
#: longer ships. Naming "native" here instead would fix that and break the
#: other direction: an ingest into a pre-existing DuckDB store would create
#: an empty native store beside it and read as data loss, which is exactly
#: what `detect_backend` exists to prevent. Deferring is the only default
#: that is right in both cases.
BACKEND_DEFAULT: str | None = None
BACKENDS = ["native", "duckdb", "kuzu"]
BACKEND_HELP = ("storage backend; default: the existing store's layout, or "
                "the native engine for a new store. duckdb and kuzu are "
                "optional extras (`pip install tgms[duckdb]`).")


def build_parser() -> argparse.ArgumentParser:
    """The full argument parser, separate from `main` so tests can inspect
    the defaults a clean install actually gets."""
    p = argparse.ArgumentParser(prog="tgms")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_demo = sub.add_parser("demo", help="run the sixty-second guided demo: a "
                            "tiny bi-temporal graph, a correction, and the "
                            "same question asked of two belief states")
    p_demo.add_argument("--store", default=None,
                        help="where to build the demo store (default: a fresh "
                             "directory under the system temp, left in place "
                             "so its rendered trace can be opened)")

    p_ing = sub.add_parser("ingest", help="ingest an event JSONL file into a store")
    p_ing.add_argument("events_jsonl")
    p_ing.add_argument("--store", required=True)
    p_ing.add_argument("--backend", default=BACKEND_DEFAULT,
                       choices=BACKENDS, help=BACKEND_HELP)

    p_rep = sub.add_parser("replay", help="rebuild a store from a recorded "
                           "event log (byte-identical; preserves transaction "
                           "times, unlike a fresh ingest)")
    p_rep.add_argument("eventlog_jsonl")
    p_rep.add_argument("--store", required=True)
    p_rep.add_argument("--backend", default=BACKEND_DEFAULT,
                       choices=BACKENDS, help=BACKEND_HELP)

    p_synth = sub.add_parser("synth", help="generate a synthetic dataset")
    p_synth.add_argument("out_dir")
    p_synth.add_argument("--nodes", type=int, default=1000)
    p_synth.add_argument("--events", type=int, default=100_000)
    p_synth.add_argument("--seed", type=int, default=0)
    p_synth.add_argument("--rings", type=int, default=0)
    p_synth.add_argument("--pingpong", type=int, default=0)
    p_synth.add_argument("--bursts", type=int, default=0)

    p_call = sub.add_parser("call", help="call one operator against a store")
    p_call.add_argument("--store", required=True)
    p_call.add_argument("op")
    p_call.add_argument("args_json")

    p_bench = sub.add_parser("bench", help="run micro-benchmarks")
    p_bench.add_argument("what", choices=["ops"])
    p_bench.add_argument("--store", required=True)
    p_bench.add_argument("--out", default="bench_report.md")

    p_serve = sub.add_parser("serve", help="serve the store over MCP")
    p_serve.add_argument("--store", required=True)
    p_serve.add_argument("--readonly", action=argparse.BooleanOptionalAction,
                         default=True,
                         help="open the store in reader-process mode: no crash "
                              "recovery, no write API (default). Use "
                              "--no-readonly only when this server is the "
                              "store's single writer.")

    p_tasks = sub.add_parser("tasks", help="generate a task suite with oracle gold")
    p_tasks.add_argument("--store", required=True)
    p_tasks.add_argument("--dataset", required=True)
    p_tasks.add_argument("--seed", type=int, default=0)
    p_tasks.add_argument("--manifest", default=None,
                         help="synth manifest.json for T2 planted tasks")
    p_tasks.add_argument("--sizes", default=None,
                         help="JSON dict of per-family task counts, e.g. "
                              "'{\"t1\":60,\"probes\":60}'")
    p_tasks.add_argument("--out", required=True)
    p_tasks.add_argument("--oracle-budget", type=float, default=None,
                         help="oracle-lane wall budget in seconds per task "
                              "(D-098 two-lane gold; default 120)")
    p_tasks.add_argument("--oracle-max-rows", type=int, default=None,
                         help="oracle-lane materialized-row cap (oracle-v3.1 "
                              "declared envelope; default 50000 = the "
                              "production cap, keeping splits invariant)")

    p_ask = sub.add_parser("ask", help="ask one question against a store")
    p_ask.add_argument("question")
    p_ask.add_argument("--store", required=True)
    p_ask.add_argument("--model", required=True)
    p_ask.add_argument("--api-base", default=None)
    p_ask.add_argument("--input-uids", nargs="*", default=[])
    p_ask.add_argument("--save-record", default=None,
                       help="write the full ask record (plan/trace/claims) as JSON")
    p_ask.add_argument("--html", default=None,
                       help="also render the record as a self-contained trace.html")

    p_web = sub.add_parser("webapp", help="serve the interactive guided demo GUI")
    p_web.add_argument("--store", required=True)
    p_web.add_argument("--suite", required=True)
    p_web.add_argument("--model", required=True)
    p_web.add_argument("--api-base", default=None)
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8080)

    # `check` is a **second action on the existing parser**, not a new command:
    # additive, so every `tgms trace render …` invocation keeps working
    # verbatim (STABILITY.md — a non-breaking addition). `-o/--out` is required
    # for `render` and meaningless for `check`, so the requirement moves out of
    # argparse and into the dispatch, where it can name the action.
    p_trace = sub.add_parser(
        "trace", help="render a saved ask record, or check whether it is stale")
    p_trace.add_argument("action", choices=["render", "check"])
    p_trace.add_argument("record_json")
    p_trace.add_argument("-o", "--out", default=None,
                         help="output path (required for `render`)")
    p_trace.add_argument("--store", default=None,
                         help="store the record was produced against "
                              "(required for `check`)")
    p_trace.add_argument("--json", action="store_true",
                         help="emit the verdict as JSON instead of prose")
    p_trace.add_argument("--as-of", type=int, default=None, metavar="TT",
                         help="ask the narrower question 'was it fresh as of "
                              "this transaction time?'; the default scans the "
                              "whole log suffix")

    # M5 design memo §8 P1.2-f: `artifact check` follows `trace check`'s
    # posture exactly — opens the event log, not the store; exit 0 on
    # FRESH, 1 otherwise. `register`/`list` open the registry (which is
    # itself opened beside the event log, never a live backend connection).
    #
    # `refresh` (P2.1, `docs/design/M5_EXECUTION_PLAN_2026-08-27.md` §5) is
    # the one action that *does* open a live store — it re-executes the
    # named artifact's stored plan and publishes a new generation. Exit
    # codes: 0 the refresh ran and a new generation was published; 2 the
    # refresh was refused and nothing was published — the printed message
    # (or, with --json, the `"reason"` field) names one of refresh.py's
    # closed taxonomy: not-found, generation-mismatch, no-refresh-handle,
    # unknown-refresh-kind, ref-not-found, unknown-plan-format,
    # parent-vanished (a parents entry names an artifact absent from the
    # live fold — nothing to advance it to), or
    # execution-refused (the re-execution itself raised or errored, e.g. a
    # D-155 admission/budget refusal). The old generation is left
    # byte-identical on disk in every refusal case (§1.1).
    p_artifact = sub.add_parser(
        "artifact", help="the artifact registry: register a derived result, "
                          "list registrations, check whether one may be "
                          "stale, or refresh it",
        description="the artifact registry: register a derived result, list "
                    "registrations, check whether one may be stale, or "
                    "refresh it (P2.1). check: exit 0 FRESH, 1 otherwise "
                    "(POSSIBLY_STALE or UNDECIDABLE — not a third contract). "
                    "refresh: exit 0 the refresh ran and a new generation "
                    "was published, exit 2 the refresh was refused and "
                    "nothing was published — the printed message, or with "
                    "--json the details.reason field, names one of: "
                    "not-found (no such artifact), generation-mismatch "
                    "(--generation named something other than the current "
                    "one), no-refresh-handle (plan_format unrecognized), "
                    "handle-mismatch, unknown-refresh-kind, ref-not-found "
                    "(the plan/operator blob is missing), "
                    "unknown-plan-format (the blob itself disagrees), "
                    "parent-vanished (a parents entry no longer resolves "
                    "in the registry), or "
                    "execution-refused (the re-execution raised or "
                    "errored, e.g. a D-155 admission/budget refusal). The "
                    "old generation is left byte-identical on disk in "
                    "every refusal case.")
    p_artifact.add_argument("action", choices=["register", "list", "check", "refresh"])
    p_artifact.add_argument("store", help="store directory (holds eventlog.jsonl "
                                          "and artifacts.jsonl)")
    p_artifact.add_argument("--name", default=None,
                            help="artifact name (required for check and refresh; "
                                 "list shows one artifact's history if given, else "
                                 "every current generation)")
    p_artifact.add_argument("--generation", type=int, default=None,
                            help="a specific generation; default is the current one. "
                                 "refresh refuses a generation that is not current "
                                 "(exit 2, reason generation-mismatch)")
    p_artifact.add_argument("--record-json", default=None,
                            help="register: path to a JSON document with the "
                                 "Registry.register() fields (name, kind, plan, "
                                 "basis, state, refresh, and optionally steps, "
                                 "dependency, parents, payload, provenance, store)")
    p_artifact.add_argument("--json", action="store_true",
                            help="list/check/refresh: emit JSON instead of prose")
    p_artifact.add_argument("--as-of", type=int, default=None, metavar="TT",
                            help="check: ask the narrower question 'was it fresh "
                                 "as of this transaction time?'; the default scans "
                                 "the whole log suffix")

    p_eval = sub.add_parser("eval", help="run the experiment matrix / C2 readout")
    p_eval.add_argument("action", choices=["run", "c2"])
    p_eval.add_argument("--extended", action="store_true",
                        help="extended mutation classes (CIDR table)")
    p_eval.add_argument("--config", default=None)
    p_eval.add_argument("--store", default=None)
    p_eval.add_argument("--suite", default=None)
    p_eval.add_argument("--mutants", type=int, default=500)
    p_eval.add_argument("--force", default=None,
                        help="rerun the frozen test split; reason is logged (§8.3)")

    p_store = sub.add_parser(
        "store", help="native store maintenance",
        description="native store maintenance. backup/restore (Lane A "
                    "EXP-A2): `--store` always names the store this "
                    "invocation acts on or creates; `--dest` always names "
                    "the backup directory. `store backup --store <src> "
                    "--dest <backup_dir>` writes a quiesced copy (the "
                    "event log, the authoritative artifact, plus a "
                    "manifest record) to <backup_dir>. `store restore "
                    "--dest <backup_dir> --store <new_store>` replays that "
                    "backed-up log into a fresh store at <new_store> "
                    "(reusing `tgms replay`'s path), then compares the "
                    "result's identity and logical digest against the "
                    "backup manifest, printing PASS/FAIL with a nonzero "
                    "exit on any mismatch — including a tampered log, "
                    "caught before replay even starts.")
    p_store.add_argument(
        "action", choices=["gc", "compact", "verify", "upgrade-manifests", "backup", "restore", "ready"],
        help="gc: drop superseded generations and the files only they "
             "reference. compact: re-sort the live rows into fresh segments. "
             "verify: checksum-walk everything this generation names. "
             "upgrade-manifests: convert a store written by an earlier engine "
             "(on-disk manifest format 1, one full manifest per generation, "
             "or format 2, the checkpoint-plus-delta chain with a "
             "whole-document manifest_sha) to format 3, which this build "
             "writes: the same chain, with manifest_sha a Merkle root over "
             "the ordered segment set. A format-1 or format-2 store opens and "
             "reads fine but refuses every write until this is run. It "
             "republishes the current content as one checkpoint and flips "
             "CURRENT: one manifest written, no segment, close run, "
             "dictionary or event-log byte touched, and it is safe to run "
             "twice. The generation counter advances by one and manifest_sha "
             "changes, so any persisted TCSR index rebuilds on next use and "
             "build receipts must not be compared on manifest_sha across "
             "formats.")
    p_store.add_argument("--store", required=True,
                         help="gc/compact/verify/backup: the store to act on. "
                              "restore: the fresh store to create")
    p_store.add_argument("--dest", default=None,
                         help="backup: directory to write the backup into. "
                              "restore: the backup directory to restore from "
                              "(required for both)")
    p_store.add_argument("--keep", type=int, default=2,
                         help="gc: generations to retain besides those "
                              "pinned by live readers (default 2)")
    p_store.add_argument("--writer", action="store_true",
                         help="ready: probe as the writer (read_only=False, "
                              "taking the writer lock and running recovery) "
                              "instead of the default read-only reader probe")
    p_store.add_argument("--json", action="store_true",
                         help="ready/verify: emit JSON instead of prose")
    mode = p_store.add_mutually_exclusive_group()
    mode.add_argument("--fast", dest="mode", action="store_const", const="fast",
                      help="verify: the file walk only — every segment, close "
                           "run, dictionary and manifest this generation "
                           "names, checksummed against what the manifest "
                           "claims. The default.")
    mode.add_argument("--full", dest="mode", action="store_const", const="full",
                      help="verify: everything --fast checks, plus the "
                           "invariants that span files — the manifest parent "
                           "chain across every retained generation, "
                           "dictionary-code reference validity, the "
                           "bitemporal row invariants, the event log's "
                           "framing and hash chain against the applied "
                           "cursor, the persisted TCSR index, and the "
                           "artifact registry with the blobs it names. "
                           "Read-only: a torn log tail is reported, never "
                           "trimmed.")
    p_store.set_defaults(mode="fast")

    p_check = sub.add_parser(
        "check", help="full integrity check of a store (alias of "
                      "`store verify --full`)",
        description="Read-only, exhaustive integrity check: every layer, "
                    "every finding reported as {layer, kind, path, "
                    "generation, detail, severity}. Exits 0 when the store "
                    "is clean, 1 when there are findings, and 2 when the "
                    "store cannot be opened at all. Nothing is repaired — "
                    "the remedy for a corrupt store is `tgms replay` from "
                    "its event log.")
    p_check.add_argument("store", help="the store to check")
    p_check.add_argument("--json", action="store_true",
                         help="emit the report as JSON instead of prose")

    p_mem = sub.add_parser("memory", help="evolution-memory maintenance")
    p_mem.add_argument("action", choices=["build"])
    p_mem.add_argument("--store", required=True)
    p_mem.add_argument("--stride-days", type=int, default=7)
    p_mem.add_argument("--refresh-stale", action="store_true")

    return p


def _store_verify(path: str, mode: str, as_json: bool) -> int:
    """`tgms store verify` / `tgms check`. Exit 0 clean, 1 findings, 2 unreadable.

    Opened **read-only**, always. A read-write handle runs crash recovery on
    open, and recovery trims a torn event-log tail — so a writer handle would
    quietly repair the very defect full mode exists to report, and the check
    would then pass on a store it had just changed. Read-only also means a
    monitoring process can check a store its writer is still using.

    Exit 2 is reserved for "cannot be checked": the store will not open at
    all, because `CURRENT` is malformed, a manifest fails its own checksum,
    or the dictionary is shorter than the manifest commits. Those are
    refusals the engine makes before any report exists, and they are a
    different answer from "checked, and here is what is wrong" — the
    corruption sweep has to tell the two apart.

    The adapter is constructed directly rather than through `tgms.open`,
    which is not a shortcut: `Store.__init__` seeds its clock by scanning the
    whole event log, and that scan *raises* on the first record it cannot
    parse. Going through it would turn every torn tail into exit 2 —
    "unreadable" — when the store itself is perfectly readable and the log's
    tail is precisely the finding full mode exists to report.
    """
    from pathlib import Path

    from tgms.storage.native import NativeAdapter

    engine_dir = Path(path) / "native"
    try:
        if not engine_dir.is_dir():
            raise FileNotFoundError(f"there is no native store at {engine_dir}")
        store = NativeAdapter(engine_dir)
    except Exception as e:
        report = {"store": path, "mode": mode, "readable": False,
                  "reason": f"{type(e).__name__}: {e}"}
        if as_json:
            print(json.dumps(report, sort_keys=True))
        else:
            print(f"store:      {path}")
            print(f"\nverdict: UNREADABLE — {report['reason']}")
            print("\nthis store cannot be checked at all; rebuild it from its "
                  "event log with `tgms replay`")
        return 2
    try:
        r = store.verify(mode=mode)
    finally:
        store.close()
    # the adapter names the engine directory it walked; the report names the
    # store the operator asked about, so the two exits agree on the subject
    r["store"] = path
    r["readable"] = True

    if as_json:
        print(json.dumps(r, sort_keys=True))
        return 0 if r["healthy"] else 1

    findings = r["findings"]
    errors = [f for f in findings if f["severity"] == "error"]
    advisories = [f for f in findings if f["severity"] != "error"]
    print(f"store:      {path}")
    print(f"mode:       {r['mode']}")
    print(f"generation: {r['generation']}")
    print(f"checked:    {r['segments_checked']} segments "
          f"({r['rows']} rows), {r['close_runs_checked']} close runs "
          f"({r['closes']} closes), {r['dict_records']} dictionary "
          f"records")
    if r["mode"] == "full":
        print(f"walked:     {r['rows_walked']} rows "
              f"({r['believed_rows']} believed) across "
              f"{r['identities_checked']} identities")
    print(f"layout:     {r['tt_s_runs']} tt_s runs across live "
          f"segments, {r['max_tt_s_runs']} in the worst one")
    for title, group in (("PROBLEMS", errors), ("ADVISORIES", advisories)):
        if not group:
            continue
        print(f"\n{title} ({len(group)}):")
        for f in group:
            where = f" {f['path']}" if f["path"] else ""
            print(f"  - [{f['layer']}/{f['kind']}]{where}: {f['detail']}")
    if errors:
        print("\nverdict: CORRUPT — do not trust this store; rebuild "
              "it from its event log with `tgms replay`")
    elif r["mode"] == "full":
        print("\nverdict: healthy — every referenced file passed its "
              "checksums, every cross-reference resolved, and every "
              "bitemporal invariant held")
    else:
        print("\nverdict: healthy — every referenced file passed its "
              "checksums and cross-references")
    return 0 if not errors else 1


#: `store backup`'s manifest filename inside `--dest` — read back verbatim
#: by `store restore` (Lane A EXP-A2). Not a store-format file: it lives in
#: the backup directory, never inside a live store's own layout.
BACKUP_MANIFEST_NAME = "backup_manifest.json"


def _git_commit() -> str | None:
    """Best-effort `git rev-parse HEAD`, or None outside a git checkout —
    same fallback shape `scripts/eval_durability.py`'s manifest already
    uses for its own `commit` field."""
    import subprocess

    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or None
    except OSError:
        return None


def _store_backup(src: str, dest: str | None) -> int:
    """`tgms store backup --store <src> --dest <dest>`: a quiesced copy.

    The event log is the authoritative artifact (STABILITY.md §1) — it is
    copied verbatim, byte for byte — plus a manifest record identifying
    exactly what was backed up: `store_identity`, the source generation and
    its `manifest_sha`, the copied log's own sha256/record count, and the
    tooling that made the backup. `read_only=True` matters here: it is the
    one open mode that never runs `Store._recover` or publishes a
    generation (`tgms/store.py::Store._recover`'s "recovery is a writer's
    act"), so backing up a store never mutates it — this snapshots exactly
    what is currently durable on disk, nothing more.
    """
    import hashlib
    import shutil
    from pathlib import Path

    import tgms
    from tgms.storage.eventlog import EventLog

    if not dest:
        print("store backup: --dest is required", file=sys.stderr)
        return 2
    src_path, dest_path = Path(src), Path(dest)
    dest_path.mkdir(parents=True, exist_ok=True)

    store = tgms.open(src_path, backend="native", read_only=True)
    try:
        store_identity = store.store_identity
        generation = store.adapter.generation
        manifest_sha = store.adapter._store.manifest_sha()
        logical_digest = store.digest()
    finally:
        store.close()

    src_log = src_path / "eventlog.jsonl"
    dest_log = dest_path / "eventlog.jsonl"
    shutil.copyfile(src_log, dest_log)
    log_sha256 = hashlib.sha256(dest_log.read_bytes()).hexdigest()
    log_records = sum(1 for _ in EventLog(dest_log).batches())

    manifest = {
        "store_identity": store_identity,
        "generation": generation,
        "manifest_sha": manifest_sha,
        "log_sha256": log_sha256,
        "log_records": log_records,
        # not one of the plan's named fields, but restore needs a content
        # check beyond "the bytes are the bytes I copied" — this is the
        # backend-independent check (`StorageAdapter.store_digest`) that
        # verifies replaying that log actually reconstructs the same store.
        "logical_digest": logical_digest,
        "tgms_version": getattr(tgms, "__version__", None),
        "commit": _git_commit(),
    }
    (dest_path / BACKUP_MANIFEST_NAME).write_text(json.dumps(manifest, indent=1) + "\n")
    print(json.dumps(manifest, indent=1))
    return 0


def _store_restore(dest: str | None, new_store: str) -> int:
    """`tgms store restore --dest <dest> --store <new_store>`: replay a
    backup's log into a fresh store, then verify it against the manifest.

    Restore is deliberately just `tgms replay` plus a check: the backed-up
    log is copied into `new_store` and replayed there
    (`tgms.storage.eventlog.replay`, `thread_cursor=True` — the same call
    `tgms replay`'s CLI action makes), and the result's `store_identity` and
    logical digest are compared against what `store backup` recorded.
    PASS/FAIL is printed either way; the exit code is nonzero on any
    mismatch, including one caught before replay even runs: the backed-up
    log's sha256 is checked against the manifest first, so a tampered log
    fails loudly instead of silently replaying into something else.
    """
    import hashlib
    import shutil
    from pathlib import Path

    import tgms
    from tgms.storage.eventlog import replay

    if not dest:
        print("store restore: --dest is required", file=sys.stderr)
        return 2
    dest_path = Path(dest)
    manifest_path = dest_path / BACKUP_MANIFEST_NAME
    if not manifest_path.exists():
        print(f"store restore: no backup manifest at {manifest_path}", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text())

    backed_up_log = dest_path / "eventlog.jsonl"
    if not backed_up_log.exists():
        print(f"store restore: no backed-up log at {backed_up_log}", file=sys.stderr)
        return 2
    actual_sha = hashlib.sha256(backed_up_log.read_bytes()).hexdigest()
    if actual_sha != manifest.get("log_sha256"):
        print(f"FAIL: backed-up log does not match its manifest "
              f"(sha256 {actual_sha} != recorded {manifest.get('log_sha256')}) "
              f"— refusing to restore a tampered log", file=sys.stderr)
        return 1

    store = tgms.open(new_store, backend="native")
    dst_log = Path(store.path) / "eventlog.jsonl"
    if backed_up_log.resolve() != dst_log.resolve():
        shutil.copyfile(backed_up_log, dst_log)
    replay(dst_log, store.adapter, thread_cursor=True)

    got_identity = store.store_identity
    got_digest = store.digest()
    store.close()

    ok = (got_identity == manifest.get("store_identity")
         and got_digest == manifest.get("logical_digest"))
    print(json.dumps({
        "verdict": "PASS" if ok else "FAIL",
        "store_identity": got_identity,
        "expected_store_identity": manifest.get("store_identity"),
        "logical_digest": got_digest,
        "expected_logical_digest": manifest.get("logical_digest"),
    }, indent=1))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "demo":
        from tgms.demo import run_demo
        run_demo(args.store)
    elif args.cmd == "ingest":
        import tgms
        store = tgms.open(args.store, backend=args.backend)
        with open(args.events_jsonl) as f:
            tt = store.ingest_events(json.loads(line) for line in f if line.strip())
        print(json.dumps({"last_tt": tt, "stats": store.stats()}, default=str))
        store.close()
    elif args.cmd == "replay":
        import shutil
        from pathlib import Path

        import tgms
        from tgms.storage.eventlog import replay
        # replay writes into a fresh store. The log is copied into place
        # *first* and replayed from the copy, so the replay cursor a
        # cursor-keeping backend records (suffix recovery, D-042) names
        # offsets in the store's own log — and an interrupted replay simply
        # resumes from its cursor on the next open.
        store = tgms.open(args.store, backend=args.backend)
        dst = Path(store.path) / "eventlog.jsonl"
        if Path(args.eventlog_jsonl).resolve() != dst.resolve():
            shutil.copyfile(args.eventlog_jsonl, dst)
        n = replay(dst, store.adapter, thread_cursor=True)
        print(json.dumps({"batches": n, "stats": store.stats()}, default=str))
        store.close()
    elif args.cmd == "synth":
        from tgms.data.synth import generate
        m = generate(args.out_dir, args.nodes, args.events, args.seed,
                     n_rings=args.rings, n_pingpong=args.pingpong,
                     n_bursts=args.bursts)
        print(json.dumps(m))
    elif args.cmd == "call":
        import tgms
        from tgms.tools.server import ToolRouter
        store = tgms.open(args.store)
        # the store is the `tt_q` source: it knows the frontier its backend has
        # applied, which a bare adapter cannot answer for (TGIR_SPEC §5.6)
        res = ToolRouter(store.adapter, tt_source=store).call(
            args.op, json.loads(args.args_json))
        print(json.dumps(res, indent=1))
        store.close()
        if "error" in res:
            return 1
    elif args.cmd == "bench":
        from tgms.eval.bench_ops import run_bench
        report = run_bench(args.store)
        with open(args.out, "w") as f:
            f.write(report)
        print(report)
    elif args.cmd == "serve":
        from tgms.tools.server import build_mcp_server
        build_mcp_server(args.store, readonly=args.readonly).run()
    elif args.cmd == "tasks":
        import tgms
        from tgms.eval.tasks import generate_suite
        store = tgms.open(args.store)
        manifest = None
        if args.manifest:
            with open(args.manifest) as f:
                manifest = json.load(f)
        sizes = json.loads(args.sizes) if args.sizes else None
        kw = {}
        if args.oracle_budget is not None:
            kw["oracle_budget_s"] = args.oracle_budget
        if args.oracle_max_rows is not None:
            kw["oracle_max_rows"] = args.oracle_max_rows
        suite = generate_suite(store, args.dataset, seed=args.seed,
                               sizes=sizes, **kw,
                               manifest=manifest)
        with open(args.out, "w") as f:
            json.dump(suite, f, indent=1, sort_keys=True)
        print(json.dumps({"n_dev": suite["n_dev"], "n_test": suite["n_test"],
                          "test_split_sha": suite["test_split_sha"]}))
        store.close()
    elif args.cmd == "ask":
        import subprocess

        import tgms
        from tgms.agent.agent import Agent
        from tgms.agent.planner import make_llm_fn
        from tgms.agent.reporter import Reporter
        from tgms.agent.verifier import ClaimVerifier
        store = tgms.open(args.store)
        llm = make_llm_fn(api_base=args.api_base)
        agent = Agent(store, model=args.model, llm_fn=llm)
        out = agent.ask(args.question, task_input_uids=set(args.input_uids))
        trace, plan = out["trace"], out["plan_result"].plan
        record: dict[str, Any] = {"question": args.question, "answer": out["answer"]}
        if plan is not None and trace is not None:
            reporter = Reporter(args.model, llm_fn=llm)
            ao = reporter.report(args.question, plan, trace, agent.executor.results)
            report = ClaimVerifier(trace, agent.executor.results,
                                   store.adapter).verify(ao)
            try:
                commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                        capture_output=True, text=True).stdout.strip()
            except Exception:
                commit = "n/a"
            record.update(plan=plan.to_json(), trace=trace.to_json(),
                          answer_object=ao, verifier_report=report,
                          receipts=f"commit {commit}; store {args.store}; "
                                   f"model {args.model}")
        print(json.dumps({"answer": record.get("answer"),
                          "text": record.get("answer_object", {}).get("text"),
                          "pvr_first_emission":
                              out["plan_result"].first_emission_valid}))
        if args.save_record:
            with open(args.save_record, "w") as f:
                json.dump(record, f, indent=1)
        if args.html and "plan" in record:
            from tgms.tools.trace_viewer import render_trace_html
            with open(args.html, "w") as f:
                f.write(render_trace_html(record))
            print(json.dumps({"html": args.html}))
        store.close()
    elif args.cmd == "webapp":
        import tgms
        from tgms.agent.planner import make_llm_fn
        from tgms.tools.webapp import serve
        store = tgms.open(args.store)
        with open(args.suite) as f:
            suite = json.load(f)
        serve(store, suite, args.model, make_llm_fn(api_base=args.api_base),
              results_dir=f"{args.store}/demo-results",
              host=args.host, port=args.port)
    elif args.cmd == "trace" and args.action == "check":
        # **Opens the event log, not the store.** D13.20's whole point is that a
        # checker runs against a log it did not produce: the check reads the
        # record's scopes and the log suffix and touches no backend at all. So
        # this verb takes no database lock, needs no optional backend extra
        # installed, and runs happily while a writer holds the store — which
        # opening a `Store` here would forbid, read-only or not (DuckDB refuses
        # a second connection with a different configuration).
        from pathlib import Path as _Path

        from tgms.core.model import OPEN_END
        from tgms.storage.eventlog import EventLog
        from tgms.tgir.check import check_trace
        from tgms.tgir.explain import render_steps
        with open(args.record_json) as f:
            record = json.load(f)
        # `tgms ask --save-record` writes `{question, plan, trace, …}` and
        # `trace render` consumes that envelope; a bare `TraceRecord.to_json()`
        # is the inner object. Accept either, so the same file works with both
        # actions and a caller never has to know which shape they hold.
        if isinstance(record.get("trace"), dict):
            record = record["trace"]
        if not args.store:
            parser.error("trace check needs --store: a verdict is a question "
                         "about a record AND the log it was produced against")
        log_path = _Path(args.store)
        if log_path.is_dir():
            log_path = log_path / "eventlog.jsonl"
        if not log_path.exists():
            parser.error(f"no event log at {log_path}")
        verdict = check_trace(record, EventLog(log_path),
                              OPEN_END if args.as_of is None else args.as_of)
        if args.json:
            print(json.dumps(verdict.to_json(), indent=1))
        else:
            print(render_steps(verdict, produced_tt=record.get("tt_q")))
        # exit 0 for FRESH, 1 for anything else — `UNDECIDABLE` is not a third
        # contract (D13.25), so a script that branches on the status code gets
        # the conservative answer without having to know that
        return 0 if verdict.actionable_fresh else 1
    elif args.cmd == "artifact" and args.action == "register":
        from tgms.artifact.record import ArtifactId, StepDependency
        from tgms.artifact.registry import Registry
        from tgms.tgir.depscope import DependencyScope
        if not args.record_json:
            parser.error("artifact register needs --record-json")
        with open(args.record_json) as f:
            fields = json.load(f)
        # `--record-json` carries the wire (JSON) shape; `Registry.register`
        # takes the typed objects `ArtifactRecord` stores internally.
        if "steps" in fields:
            fields["steps"] = [StepDependency.from_json(s) for s in fields["steps"]]
        if fields.get("dependency") is not None:
            fields["dependency"] = DependencyScope.from_json(fields["dependency"])
        if "parents" in fields:
            fields["parents"] = [ArtifactId.from_json(p) for p in fields["parents"]]
        registry = Registry(args.store)
        record = registry.register(**fields)
        print(json.dumps({"name": record.name, "generation": record.generation,
                          "record_digest": record.record_digest}, indent=1))
    elif args.cmd == "artifact" and args.action == "list":
        from tgms.artifact.registry import Registry
        registry = Registry(args.store)
        records = (registry.history(args.name) if args.name
                  else registry.current_generations())
        if args.json:
            print(json.dumps([r.to_json() for r in records], indent=1))
        else:
            for r in records:
                print(f"{r.name}@{r.generation}  {r.kind}  {r.record_digest[:12]}")
    elif args.cmd == "artifact" and args.action == "check":
        # Same posture as `trace check` (§8 P1.2-f): opens the event log, not
        # the store. The registry itself is also just a file read beside it —
        # neither takes a database lock.
        from pathlib import Path as _ArtifactPath

        from tgms.artifact.registry import Registry
        from tgms.artifact.witness import check_artifact, render_verdict
        from tgms.core.model import OPEN_END
        from tgms.storage.eventlog import EventLog
        if not args.name:
            parser.error("artifact check needs --name")
        registry = Registry(args.store)
        record = (registry.at(args.name, args.generation) if args.generation is not None
                 else registry.current(args.name))
        if record is None:
            who = f"{args.name}@{args.generation}" if args.generation is not None else args.name
            parser.error(f"no such artifact: {who}")
        verdict = check_artifact(
            record, EventLog(_ArtifactPath(args.store) / "eventlog.jsonl"),
            OPEN_END if args.as_of is None else args.as_of)
        if args.json:
            print(json.dumps(verdict.to_json(), indent=1))
        else:
            # §5.4 point 2: the exemption receipt prints even on FRESH —
            # `render_verdict` already includes it unconditionally.
            print(render_verdict(verdict, produced_tt=record.registered_tt))
        return 0 if verdict.actionable_fresh else 1
    elif args.cmd == "artifact" and args.action == "refresh":
        # P2.1 (M5 execution plan §5). Selectivity stays the caller's: this
        # branch is what decides *whether* to refresh, by calling
        # `check_artifact` for the handle exactly the way `artifact check`
        # already does above — `tgms.artifact.refresh.refresh` itself never
        # imports `check_artifact` and never asks "is this stale" (§5.6).
        from pathlib import Path as _Path

        import tgms
        from tgms.artifact.refresh import RefreshRefused, refresh, resolve_current
        from tgms.artifact.registry import Registry
        from tgms.artifact.witness import check_artifact
        from tgms.storage.eventlog import EventLog
        if not args.name:
            parser.error("artifact refresh needs --name")
        registry = Registry(args.store)
        try:
            record = resolve_current(registry, args.name, args.generation)
            log = EventLog(_Path(args.store) / "eventlog.jsonl")
            verdict = check_artifact(record, log)
            live = tgms.open(args.store)
            try:
                new_record = refresh(record, verdict.refresh, live, registry)
            finally:
                live.close()
        except RefreshRefused as e:
            if args.json:
                print(json.dumps(e.to_payload(), indent=1))
            else:
                print(f"refused ({e.reason}): {e.message}")
            return 2
        if args.json:
            print(json.dumps({"name": new_record.name, "generation": new_record.generation,
                              "supersedes": (new_record.supersedes.to_json()
                                            if new_record.supersedes else None),
                              "record_digest": new_record.record_digest}, indent=1))
        else:
            print(f"{new_record.name}@{new_record.generation} "
                  f"(supersedes @{record.generation})  {new_record.record_digest[:12]}")
        return 0
    elif args.cmd == "trace":
        from tgms.tools.trace_viewer import render_trace_html
        with open(args.record_json) as f:
            record = json.load(f)
        if not args.out:
            parser.error("trace render needs -o/--out")
        with open(args.out, "w") as f:
            f.write(render_trace_html(record))
        print(json.dumps({"html": args.out}))
    elif args.cmd == "eval" and args.action == "c2":
        import tgms
        store = tgms.open(args.store)
        with open(args.suite) as f:
            suite = json.load(f)
        if args.extended:
            from tgms.eval.faults_ext import c2_extended_readout
            stats = c2_extended_readout(store, suite,
                                        per_class=args.mutants or 100)
            print(json.dumps(stats, indent=1))
            store.close()
            return 0
        from tgms.eval.faults import c2_readout_from_suite
        stats = c2_readout_from_suite(store, suite, n_mutants=args.mutants)
        print(json.dumps(stats, indent=1))
        store.close()
        if not stats["accepted"]:
            return 1
    elif args.cmd == "eval":
        import yaml
        from tgms.agent.planner import make_llm_fn
        from tgms.eval.harness import run_matrix
        with open(args.config) as f:
            cfg = yaml.safe_load(f)
        usage: list = []
        llm_fn = make_llm_fn(api_base=cfg.get("llm_api_base"),
                             api_key=cfg.get("llm_api_key"),
                             extra_body=cfg.get("llm_extra_body"),
                             max_tokens=cfg.get("llm_max_tokens", 4096),
                             usage_log=usage)
        rows = run_matrix(cfg, llm_fn=llm_fn, force=args.force,
                          usage_log=usage)
        print(json.dumps({"rows": len(rows), "out_dir": cfg["out_dir"]}))
    elif args.cmd == "store" and args.action == "ready":
        # B5/F2 health surface. `--writer` opens read_only=False (taking the
        # writer lock and running recovery, exactly like a real writer
        # process starting up); the default opens read_only=True, which is
        # the mode a monitoring sidecar for `tgms serve --readonly` should
        # use. Either way, an open that *raises* (corruption, or the writer
        # lock already held by another process) is reported as not-ready
        # rather than letting the traceback stand in for an exit code.
        import tgms
        from tgms.tools.webapp import ReadinessProbe
        try:
            store = tgms.open(args.store, read_only=not args.writer)
        except Exception as e:
            result = {"ready": False, "reason": f"{type(e).__name__}: {e}"}
            if args.json:
                print(json.dumps(result))
            else:
                print(f"not ready: {result['reason']}")
            return 2
        result = ReadinessProbe(store).check()
        store.close()
        if args.json:
            print(json.dumps(result))
        else:
            if result["ready"]:
                print(f"ready: generation {result['generation']} "
                      f"(read_only={result['read_only']}, "
                      f"frontier_verified={result['frontier_verified']})")
            else:
                print(f"not ready: {result['reason']}")
        return 0 if result["ready"] else 1
    elif args.cmd == "store":
        import tgms
        if args.action == "upgrade-manifests":
            # Deliberately *not* through `tgms.open`: that opens the event log
            # and may replay a suffix, which is a write — and a write is the
            # one thing a format-1 or format-2 store refuses until this
            # command has run.
            # The engine handle alone is enough, and touches nothing else.
            from pathlib import Path as _StorePath

            from tgms.storage.native import NativeAdapter
            adapter = NativeAdapter(_StorePath(args.store) / "native")
            report = adapter.upgrade_manifests()
            adapter.close()
            if report["upgraded"]:
                print(f"store:      {args.store}")
                print(f"upgraded:   manifest format {report['from_format']} "
                      f"-> {report['to_format']}")
                print(f"generation: {report['generation']} "
                      f"(sha {report['manifest_sha']})")
                print("\nOne checkpoint written and CURRENT flipped; segments, "
                      "close runs, the dictionary and the event log are "
                      "untouched. Persisted TCSR indexes will rebuild on next "
                      "use, and receipts must not be compared on manifest_sha "
                      "across the two formats.")
            else:
                print(f"store:      {args.store}")
                print(f"unchanged:  already at manifest format "
                      f"{report['to_format']}")
            return 0

        if args.action == "backup":
            return _store_backup(args.store, args.dest)
        if args.action == "restore":
            return _store_restore(args.dest, args.store)

        if args.action == "verify":
            return _store_verify(args.store, args.mode, args.json)

        store = tgms.open(args.store, backend="native")
        if args.action == "gc":
            report = store.adapter.gc(keep_last=args.keep)
        else:
            report = store.adapter.compact()
        print(json.dumps(report))
        store.close()
    elif args.cmd == "check":
        return _store_verify(args.store, "full", args.json)
    elif args.cmd == "memory":
        import tgms
        from tgms.agent.memory import MICROS_PER_DAY, EvolutionMemory
        store = tgms.open(args.store)
        mem = EvolutionMemory(f"{args.store}/memory.sqlite")
        n = mem.build(store.adapter, stride=args.stride_days * MICROS_PER_DAY,
                      as_of_tt=store.clock.last_tt,
                      refresh_stale=args.refresh_stale)
        print(json.dumps({"notes": n, "refresh_stale": args.refresh_stale}))
        mem.close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

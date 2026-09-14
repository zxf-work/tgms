"""Out-of-process wall-clock supervisor (Lane B5/F2).

**Why this has to live outside the process it watches.** The in-process
admission budget (`tgms.tgir.admission.Budget`) is a *work* ceiling, not a
wall-clock one, and its own docstring says why it cannot be made into one: a
charge is only levied between candidates, so a single long adapter call
sitting inside the Rust extension is never interrupted — a signal handler
needs the interpreter, and that call is not running it. A diagnostic run
once set a 240s in-process `signal.alarm` and was still inside one
`scan_nodes` twenty-five minutes later. Anything that must bound elapsed
wall-clock time has to watch from outside the process.

**The mechanism.** This runs a child process (typically `tgms serve` or
`tgms webapp`, but any command that writes the `tgms.tools.jsonlog` JSON
request log works), tails that log, and watches for a request whose
`"started"` line has no matching `"finished"` line within `max_wall_s`.
When it finds one, it SIGKILLs the child and starts a fresh one. This is
safe by construction on both sides of TGMS's own contracts: a reader child
just reopens (nothing to recover — D-049); a writer child recovers by
suffix replay on its next open (D-042) exactly as it would after any other
crash, because that is what this looks like to the child's own store — an
unclean process death, not a special case this script has to reason about.

Every kill is recorded twice: a `wall_kill` counter on the metrics sink
(`tgms.telemetry.metrics.Metrics`), and one entry appended to a failure
ledger in `ops/failure_ledger.jsonl`'s required-field schema
(`scripts/check_failure_ledger.py`) — a request the supervisor had to kill
is exactly the kind of "something actually went wrong, here is what and
when" record that ledger exists for. The default `--ledger-path` is the
real project ledger; pass a different path (tests always do) to avoid
appending operational noise to it.

    uv run python scripts/tgms_supervise.py --log-path /tmp/tgms.jsonl \\
        --max-wall-s 30 -- tgms serve --store stores/synth-300k

Limitations, stated plainly:
- This bounds *wall clock per outstanding request*, not per-child uptime or
  per-child memory; a child that never logs a `"started"` line for its slow
  work (library code that bypasses `ToolRouter.call`) is invisible to it.
- Restart is a bare re-exec of the same command; it does not back off, and
  a command that fails instantly on every start will restart in a tight
  loop unless `--max-restarts` bounds it.
- The log tail assumes the child's log file is append-only and that this
  script and the child agree on `TGMS_LOG_PATH` — `Supervisor.start_child`
  sets that environment variable on the child itself so the two can never
  disagree.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tgms.telemetry.metrics import Metrics  # noqa: E402

DEFAULT_LEDGER_PATH = ROOT / "ops" / "failure_ledger.jsonl"


class LogTail:
    """Incrementally reads whole new lines appended to a JSONL file.

    Tracks a byte offset, not a line count, and only ever consumes a line
    that ends in `\\n` — a partial line (the writer mid-`write()`) is left
    for the next poll rather than parsed early or dropped.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._offset = 0

    def read_new(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with open(self.path, "rb") as f:
            f.seek(self._offset)
            while True:
                line = f.readline()
                if not line.endswith(b"\n"):
                    break
                self._offset += len(line)
                try:
                    out.append(json.loads(line.decode("utf-8")))
                except json.JSONDecodeError:
                    continue
        return out


class Supervisor:
    """Runs `cmd` as a child, tails `log_path` for its request log, and
    kills+restarts the child if any `"started"` request outlives
    `max_wall_s` with no `"finished"` line."""

    def __init__(self, cmd: Sequence[str], log_path: str | Path,
                max_wall_s: float, *, poll_interval_s: float = 0.5,
                metrics: Metrics | None = None,
                ledger_path: str | Path = DEFAULT_LEDGER_PATH,
                max_restarts: int | None = None,
                env: dict[str, str] | None = None,
                quiet_child: bool = True) -> None:
        self.cmd = list(cmd)
        self.log_path = Path(log_path)
        self.max_wall_s = float(max_wall_s)
        self.poll_interval_s = float(poll_interval_s)
        self.metrics = metrics if metrics is not None else Metrics()
        self.ledger_path = Path(ledger_path)
        self.max_restarts = max_restarts
        self.env = env
        self.quiet_child = quiet_child
        self.restarts = 0
        self.kills: list[dict[str, Any]] = []
        self.child: subprocess.Popen | None = None
        self.tail: LogTail | None = None
        #: request_id -> wall-clock time.time() its "started" line arrived.
        self._pending: dict[str, float] = {}

    # --- child lifecycle -------------------------------------------------- #

    def start_child(self) -> None:
        env = dict(self.env) if self.env is not None else dict(os.environ)
        # the one thing that must never disagree between watcher and child
        env["TGMS_LOG_PATH"] = str(self.log_path)
        stdio = subprocess.DEVNULL if self.quiet_child else None
        self.child = subprocess.Popen(self.cmd, env=env, stdout=stdio, stderr=stdio)
        self.tail = LogTail(self.log_path)
        self._pending.clear()

    def _kill_and_restart(self, request_id: str) -> None:
        assert self.child is not None
        pid = self.child.pid
        if self.child.poll() is None:
            self.child.kill()
            try:
                self.child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass  # SIGKILL cannot be blocked; this is a very slow OS, not a hang
        self.metrics.counter("wall_kill", tool="supervised_child")
        self.metrics.flush()
        entry = _append_ledger_entry(
            self.ledger_path, request_id=request_id, pid=pid,
            max_wall_s=self.max_wall_s, cmd=self.cmd)
        self.kills.append(entry)
        self.restarts += 1
        self.start_child()

    # --- the watch loop ----------------------------------------------------#

    def _consume_log(self) -> None:
        for rec in self.tail.read_new():  # type: ignore[union-attr]
            rid = rec.get("request_id")
            if rid is None:
                continue
            event = rec.get("event")
            if event == "started":
                self._pending[rid] = time.time()
            elif event == "finished":
                self._pending.pop(rid, None)

    def _hung_request(self) -> str | None:
        now = time.time()
        for rid, started in self._pending.items():
            if now - started > self.max_wall_s:
                return rid
        return None

    def run(self, *, max_iterations: int | None = None) -> None:
        """The supervision loop. `max_iterations` bounds it — real
        deployments pass `None` and rely on an external stop signal; tests
        pass a bound so a bug here cannot hang the test suite."""
        self.start_child()
        iterations = 0
        try:
            while True:
                assert self.child is not None
                if self.child.poll() is not None:
                    # the child exited on its own (crash, or the command
                    # finished) — restart it, same as a wall-kill would,
                    # unless the caller has capped how many restarts to try
                    if self.max_restarts is not None and self.restarts >= self.max_restarts:
                        break
                    self.restarts += 1
                    self.start_child()
                self._consume_log()
                hung = self._hung_request()
                if hung is not None:
                    self._kill_and_restart(hung)
                    if self.max_restarts is not None and self.restarts >= self.max_restarts:
                        break
                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    break
                time.sleep(self.poll_interval_s)
        finally:
            if self.child is not None and self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.child.kill()


def _git_head_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=5)
        sha = out.stdout.strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"


def _append_ledger_entry(ledger_path: Path, *, request_id: str, pid: int | None,
                         max_wall_s: float, cmd: Sequence[str]) -> dict[str, Any]:
    """Append one entry in `ops/failure_ledger.jsonl`'s required-field shape
    (`scripts/check_failure_ledger.py`): a wall-clock kill is an operational
    record of "something the supervisor actually observed and mitigated",
    not a retroactive bug-fix entry — `fix_commit` names the commit this
    mitigation exists under, not a commit that fixed the underlying hang
    (the underlying hang, if any, is whatever workload produced the request;
    this script does not diagnose that, only bounds its blast radius)."""
    entry = {
        "id": f"OPS-wall-kill-{request_id}-{int(time.time() * 1000)}",
        "first_observed": time.strftime("%Y-%m-%d", time.gmtime()),
        "workload_or_seed": f"supervised child {list(cmd)!r} (pid {pid})",
        "symptom": f"request {request_id} exceeded the {max_wall_s}s "
                  f"wall-clock ceiling with no 'finished' log line; "
                  f"tgms_supervise SIGKILLed and restarted the child",
        "severity": "medium",
        "root_cause": "the in-process admission budget "
                      "(tgms.tgir.admission.Budget) is a work ceiling, not a "
                      "wall-clock one -- a charge is only levied between "
                      "candidates, so a single long adapter call inside the "
                      "Rust extension is never interrupted by an in-process "
                      "signal handler; bounding elapsed wall-clock time "
                      "requires an out-of-process supervisor (this script)",
        "fix_commit": _git_head_sha(),
        "regression_test": "tests/test_supervisor.py",
        "decision_ref": "tgms-osdi-lane-b5-f2-wall-clock-supervisor",
    }
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tgms_supervise",
        description="run a TGMS server child under a wall-clock ceiling per "
                    "request, killing and restarting it if one hangs")
    p.add_argument("--log-path", required=True,
                   help="the child's TGMS_LOG_PATH; this script sets that "
                        "env var on the child itself so the two agree")
    p.add_argument("--max-wall-s", type=float, required=True)
    p.add_argument("--poll-interval-s", type=float, default=0.5)
    p.add_argument("--metrics-path", default=None,
                   help="default: TGMS_METRICS_PATH env, else no-op")
    p.add_argument("--ledger-path", default=str(DEFAULT_LEDGER_PATH))
    p.add_argument("--max-restarts", type=int, default=None)
    p.add_argument("cmd", nargs=argparse.REMAINDER,
                   help="the child command, e.g. -- tgms serve --store S "
                        "--no-readonly")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        print("tgms_supervise: no child command given (pass it after --)",
              file=sys.stderr)
        return 2
    sup = Supervisor(cmd, args.log_path, args.max_wall_s,
                     poll_interval_s=args.poll_interval_s,
                     metrics=Metrics(args.metrics_path),
                     ledger_path=Path(args.ledger_path),
                     max_restarts=args.max_restarts, quiet_child=False)
    sup.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

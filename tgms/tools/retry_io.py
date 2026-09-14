"""Bounded retry for writes that hit a transient shared-quota error.

**Why this exists.** On the iTiger cluster, writes under the project
filesystem fail intermittently with `EDQUOT` ("Disk quota exceeded") for
seconds to minutes at a time while this project's own usage is unchanged —
observed as an identical `mkdir`/write succeeding and failing hours apart at
the same `du`, consistent with a shared group quota some other tenant
transiently exhausts. A campaign task that has computed for hours must not
lose its record to a few-second EDQUOT blip at the final write, and a task
must not die at its very first `mkdir` either.

**What this is not.** This is not a general-purpose retry framework and not
a fix for a real, sustained out-of-space condition: only `EDQUOT` and
`ENOSPC` are retried (`retry_errnos`), every other `OSError` (permission,
missing parent, read-only filesystem, ...) propagates on the first attempt.
Bounded backoff (`attempts`/`delay_s`/`backoff`) caps the total wait at
roughly ten minutes by default — long enough to ride out an observed blip,
short enough that a genuinely stuck job still fails instead of hanging.

**Atomicity.** `write_bytes_with_retry` writes to a sibling temp file in the
same directory and `os.replace`s it into place, so a reader never observes a
partial or half-written record at the destination path — a retried write
either lands whole or the destination is untouched (retries clean up the
temp file between attempts). This is orthogonal to the retry loop itself:
even a single successful attempt goes through the temp-file-then-rename
path.

Pure stdlib; no dependency on the rest of `tgms`.
"""

from __future__ import annotations

import errno
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

#: Default errnos this module treats as transient and worth retrying.
DEFAULT_RETRY_ERRNOS = (errno.EDQUOT, errno.ENOSPC)


def _errno_of(exc: OSError) -> int | None:
    return exc.errno


def _sleep_schedule(attempts: int, delay_s: float, backoff: float) -> list[float]:
    """Delays before retries 2..attempts (retry 1 has no prior delay)."""
    delays = []
    d = delay_s
    for _ in range(max(0, attempts - 1)):
        delays.append(d)
        d *= backoff
    return delays


def mkdir_with_retry(path: str | Path, *, attempts: int = 12, delay_s: float = 10.0,
                     backoff: float = 1.5,
                     retry_errnos: tuple[int, ...] = DEFAULT_RETRY_ERRNOS) -> None:
    """`Path(path).mkdir(parents=True, exist_ok=True)`, retrying only on
    `retry_errnos`. Idempotent: a directory that already exists (by this
    call or a concurrent one) is success, not an error.
    """
    path = Path(path)
    delays = _sleep_schedule(attempts, delay_s, backoff)
    for i in range(attempts):
        try:
            path.mkdir(parents=True, exist_ok=True)
            return
        except OSError as exc:
            if _errno_of(exc) not in retry_errnos or i == attempts - 1:
                raise
            wait = delays[i] if i < len(delays) else delays[-1]
            print(f"retry_io: mkdir {path} failed ({exc.strerror}); "
                 f"retry {i + 1}/{attempts - 1} in {wait:.1f}s", file=sys.stderr)
            time.sleep(wait)


def write_bytes_with_retry(path: str | Path, data: bytes, *, attempts: int = 12,
                           delay_s: float = 10.0, backoff: float = 1.5,
                           retry_errnos: tuple[int, ...] = DEFAULT_RETRY_ERRNOS) -> None:
    """Write `data` to `path` atomically (temp file in the same directory,
    then `os.replace`), retrying the whole attempt (temp write + fsync +
    rename) only on `retry_errnos`, with bounded backoff. Re-raises the last
    exception once `attempts` is exhausted. A reader never observes a
    partial file at `path` — a failed attempt's temp file is removed before
    the next try (or left for the caller to inspect only on the final,
    re-raised failure).
    """
    path = Path(path)
    delays = _sleep_schedule(attempts, delay_s, backoff)
    for i in range(attempts):
        tmp_path = path.with_name(f".{path.name}.tmp{os.getpid()}.{i}")
        try:
            with open(tmp_path, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            return
        except OSError as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            if _errno_of(exc) not in retry_errnos or i == attempts - 1:
                raise
            wait = delays[i] if i < len(delays) else delays[-1]
            print(f"retry_io: write {path} failed ({exc.strerror}); "
                 f"retry {i + 1}/{attempts - 1} in {wait:.1f}s", file=sys.stderr)
            time.sleep(wait)


def atomic_write_json_with_retry(path: str | Path, obj: Any, *, attempts: int = 12,
                                 delay_s: float = 10.0, backoff: float = 1.5,
                                 retry_errnos: tuple[int, ...] = DEFAULT_RETRY_ERRNOS) -> None:
    """`write_bytes_with_retry` of `obj`'s canonical JSON (`sort_keys=True`,
    trailing newline, UTF-8) — the same byte shape every canonical-JSON
    writer in this repo produces.
    """
    data = (json.dumps(obj, sort_keys=True) + "\n").encode("utf-8")
    write_bytes_with_retry(path, data, attempts=attempts, delay_s=delay_s, backoff=backoff,
                           retry_errnos=retry_errnos)

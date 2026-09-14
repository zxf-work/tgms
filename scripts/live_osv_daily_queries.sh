#!/usr/bin/env bash
# OSV live-workload daily query loop — Lane F3, P0.5, design §4/§8.
#
# scripts/live_osv_queries.py runs one seeded pass of 200 queries (seed =
# ISO date) and exits; it has no built-in schedule. xzgpu has no cron
# available to this account (the same reason scripts/live_supervise.sh
# self-daemonizes rather than relying on systemd), so this is the second,
# lightweight nohup loop script the F3 deploy runbook allows: sleep until
# the next occurrence of --hour local time, run the query script once,
# repeat forever.
#
# Deliberately simpler than live_supervise.sh: no backoff/restart, no
# generation tracking. A failed query run is one bad day's
# `queries-<date>.jsonl` (inspect it, rerun by hand if needed) — not a
# reason to retry immediately or perturb the writer, which this script
# never touches (every reader, including live_osv_queries.py, opens the
# store read_only, design §5). `stop` sends the loop's own process a
# SIGTERM (it is almost always inside `sleep`, for up to 24h, so this is
# the simplest correct way to interrupt it) rather than a sentinel file the
# loop would only notice on its next wake.
#
# Usage:
#   scripts/live_osv_daily_queries.sh start   # setsid nohup itself, return immediately
#   scripts/live_osv_daily_queries.sh stop    # kill the loop
#   scripts/live_osv_daily_queries.sh status  # print whether the loop is running
#   scripts/live_osv_daily_queries.sh run     # run the loop in the foreground
#                                              # (what `start` backgrounds via setsid nohup)
#
# Environment (all overridable, defaults match design §5's xzgpu layout):
#   TGMS_OSV_ROOT           repo root (default: this script's parent's parent)
#   TGMS_OSV_STORE          store directory (default: $RUN_DIR/store)
#   TGMS_OSV_QUERY_LOG_DIR  directory for per-day query logs (default: $TGMS_OSV_ROOT/logs)
#   TGMS_OSV_VENV           venv to run the query script under (default: /mnt/project/xzhang/tgms/venv)
#   TGMS_OSV_RUN_DIR        this loop's own run directory (default: $TGMS_OSV_ROOT/run)
#   TGMS_OSV_DAILY_HOUR     local hour to run at, 0-23 (default: 3, i.e. 03:00)
#   TMPDIR                  (design §5: /mnt/project/xzhang/tgms/tmp on xzgpu)

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${TGMS_OSV_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
RUN_DIR="${TGMS_OSV_RUN_DIR:-${ROOT}/run}"
STORE="${TGMS_OSV_STORE:-${RUN_DIR}/store}"
LOG_DIR="${TGMS_OSV_QUERY_LOG_DIR:-${ROOT}/logs}"
VENV="${TGMS_OSV_VENV:-/mnt/project/xzhang/tgms/venv}"
DAILY_HOUR="${TGMS_OSV_DAILY_HOUR:-3}"

PID_FILE="${RUN_DIR}/daily_queries.pid"
LOG_FILE="${RUN_DIR}/daily_queries.log"

mkdir -p "${RUN_DIR}" "${LOG_DIR}"

_python() {
  if [ -x "${VENV}/bin/python" ]; then
    echo "${VENV}/bin/python"
  else
    command -v python3
  fi
}

_is_running() {
  # $1: pid file
  [ -f "$1" ] || return 1
  local pid
  pid="$(cat "$1" 2>/dev/null || true)"
  [ -n "${pid}" ] || return 1
  kill -0 "${pid}" 2>/dev/null
}

_seconds_until_next_run() {
  # Seconds from now until the next local occurrence of ${DAILY_HOUR}:00
  # (GNU date; xzgpu is Linux). If that time today has already passed,
  # target tomorrow instead.
  local now target
  now="$(date +%s)"
  target="$(date -d "today ${DAILY_HOUR}:00" +%s)"
  if [ "${target}" -le "${now}" ]; then
    target="$(date -d "tomorrow ${DAILY_HOUR}:00" +%s)"
  fi
  echo $((target - now))
}

cmd_run() {
  # The loop itself, in the foreground. `start` backgrounds this via
  # setsid nohup; run it directly for debugging.
  echo "$$" > "${PID_FILE}"
  echo "daily-query loop started pid=$$ store=${STORE} hour=${DAILY_HOUR}:00 local" \
    | tee -a "${LOG_FILE}"

  while true; do
    local wait_s
    wait_s="$(_seconds_until_next_run)"
    echo "sleeping ${wait_s}s until next ${DAILY_HOUR}:00 local run" | tee -a "${LOG_FILE}"
    sleep "${wait_s}"

    local date_str py out status
    date_str="$(date +%F)"
    py="$(_python)"
    out="${LOG_DIR}/queries-${date_str}.jsonl"
    echo "running daily query pass -> ${out}" | tee -a "${LOG_FILE}"
    "${py}" "${ROOT}/scripts/live_osv_queries.py" --store "${STORE}" --log "${out}" \
      >> "${LOG_FILE}" 2>&1
    status=$?
    echo "daily query pass exit=${status} out=${out}" | tee -a "${LOG_FILE}"
  done
}

cmd_start() {
  if _is_running "${PID_FILE}"; then
    echo "already running (pid $(cat "${PID_FILE}"))"
    return 0
  fi
  setsid nohup "${BASH_SOURCE[0]}" run >> "${LOG_FILE}" 2>&1 < /dev/null &
  disown || true
  sleep 1
  if _is_running "${PID_FILE}"; then
    echo "started (pid $(cat "${PID_FILE}")), log at ${LOG_FILE}"
  else
    echo "failed to start; see ${LOG_FILE}" >&2
    return 1
  fi
}

cmd_stop() {
  if ! _is_running "${PID_FILE}"; then
    echo "not running"
    rm -f "${PID_FILE}"
    return 0
  fi
  local pid
  pid="$(cat "${PID_FILE}")"
  echo "stopping pid ${pid} (almost certainly asleep until the next scheduled run)"
  kill "${pid}" 2>/dev/null
  sleep 1
  if _is_running "${PID_FILE}"; then
    kill -9 "${pid}" 2>/dev/null
  fi
  rm -f "${PID_FILE}"
  echo "stopped"
}

cmd_status() {
  if _is_running "${PID_FILE}"; then
    echo "running (pid $(cat "${PID_FILE}"))"
    echo "tail -f ${LOG_FILE}          # loop log"
    echo "ls ${LOG_DIR}/queries-*.jsonl # per-day query results"
    return 0
  fi
  echo "not running"
  return 1
}

case "${1:-}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  run) cmd_run ;;
  *)
    echo "usage: $0 {start|stop|status|run}" >&2
    exit 2
    ;;
esac

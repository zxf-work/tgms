#!/usr/bin/env bash
# OSV live-workload poller supervisor — Lane F3, P0.5, design §5.
#
# No systemd (xzgpu has none available to this account). `setsid nohup`
# self-daemonizes; a PID file under RUN_DIR names the supervisor loop itself
# (not the poller child, which the loop re-execs on every restart); backoff
# is exponential 5s -> 300s, reset to 5s after 600s of clean uptime; a
# `stop` sentinel file requests graceful shutdown, checked between poller
# exits (never mid-cycle: the poller itself, not this script, decides cycle
# boundaries). Progress is read by tailing the poller's own metrics/log
# files, never by attaching to the process.
#
# Usage:
#   scripts/live_supervise.sh start   # setsid nohup itself, return immediately
#   scripts/live_supervise.sh stop    # request graceful shutdown, wait briefly
#   scripts/live_supervise.sh status  # print whether the loop is running
#   scripts/live_supervise.sh run     # run the supervisor loop in the foreground
#                                      # (what `start` backgrounds via setsid nohup)
#
# Environment (all overridable, defaults match design §5's xzgpu layout):
#   TGMS_OSV_ROOT       repo root (default: this script's parent's parent)
#   TGMS_OSV_STORE      store directory (default: $RUN_DIR/store)
#   TGMS_OSV_STATE      state.json path (default: $RUN_DIR/state.json)
#   TGMS_OSV_METRICS    metrics JSONL path (default: $RUN_DIR/live_metrics.jsonl)
#   TGMS_OSV_LEDGER     failure-ledger JSONL path (default: $TGMS_OSV_ROOT/ops/failure_ledger.jsonl)
#   TGMS_OSV_RUN_DIR    supervisor's own run directory (default: $TGMS_OSV_ROOT/run)
#   TGMS_OSV_VENV       venv to run the poller under (default: /mnt/project/xzhang/tgms/venv)
#   TMPDIR              (design §5: /mnt/project/xzhang/tgms/tmp on xzgpu)
#   TGMS_OSV_INTERVAL_S poll interval seconds (default: 3600)
#   TGMS_OSV_BACKOFF_MIN_S  backoff floor (default: 5)
#   TGMS_OSV_BACKOFF_MAX_S  backoff ceiling (default: 300)
#   TGMS_OSV_CLEAN_UPTIME_S uptime after which backoff resets (default: 600)

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${TGMS_OSV_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
RUN_DIR="${TGMS_OSV_RUN_DIR:-${ROOT}/run}"
STORE="${TGMS_OSV_STORE:-${RUN_DIR}/store}"
STATE="${TGMS_OSV_STATE:-${RUN_DIR}/state.json}"
METRICS="${TGMS_OSV_METRICS:-${RUN_DIR}/live_metrics.jsonl}"
LEDGER="${TGMS_OSV_LEDGER:-${ROOT}/ops/failure_ledger.jsonl}"
VENV="${TGMS_OSV_VENV:-/mnt/project/xzhang/tgms/venv}"
INTERVAL_S="${TGMS_OSV_INTERVAL_S:-3600}"
BACKOFF_MIN_S="${TGMS_OSV_BACKOFF_MIN_S:-5}"
BACKOFF_MAX_S="${TGMS_OSV_BACKOFF_MAX_S:-300}"
CLEAN_UPTIME_S="${TGMS_OSV_CLEAN_UPTIME_S:-600}"

PID_FILE="${RUN_DIR}/supervisor.pid"
STOP_FILE="${RUN_DIR}/stop"
LOG_FILE="${RUN_DIR}/supervisor.log"

mkdir -p "${RUN_DIR}"

_python() {
  if [ -x "${VENV}/bin/python" ]; then
    echo "${VENV}/bin/python"
  else
    command -v python3
  fi
}

_poller_cmd() {
  local py
  py="$(_python)"
  echo "${py}" "${ROOT}/scripts/live_osv_poller.py" \
    --store "${STORE}" --state "${STATE}" --log "${METRICS}" \
    --failure-ledger "${LEDGER}" --interval-s "${INTERVAL_S}"
}

_is_running() {
  # $1: pid file
  [ -f "$1" ] || return 1
  local pid
  pid="$(cat "$1" 2>/dev/null || true)"
  [ -n "${pid}" ] || return 1
  kill -0 "${pid}" 2>/dev/null
}

cmd_run() {
  # The supervisor loop itself, in the foreground. `start` backgrounds this
  # via setsid nohup; run it directly for debugging.
  echo "$$" > "${PID_FILE}"
  rm -f "${STOP_FILE}"
  local backoff="${BACKOFF_MIN_S}"
  local restart_count=0
  export TGMS_OSV_RESTART_COUNT="${restart_count}"

  echo "supervisor started pid=$$ store=${STORE} interval_s=${INTERVAL_S}" \
    | tee -a "${LOG_FILE}"

  while true; do
    if [ -f "${STOP_FILE}" ]; then
      echo "stop sentinel present; exiting cleanly" | tee -a "${LOG_FILE}"
      rm -f "${STOP_FILE}" "${PID_FILE}"
      return 0
    fi

    local start_ts
    start_ts="$(date +%s)"
    # shellcheck disable=SC2046
    $(_poller_cmd) >> "${LOG_FILE}" 2>&1
    local status=$?
    local end_ts uptime
    end_ts="$(date +%s)"
    uptime=$((end_ts - start_ts))

    if [ -f "${STOP_FILE}" ]; then
      echo "stop sentinel present after poller exit (status=${status}); exiting" \
        | tee -a "${LOG_FILE}"
      rm -f "${STOP_FILE}" "${PID_FILE}"
      return 0
    fi

    if [ "${status}" -eq 0 ]; then
      # a clean exit with no --once/loop-ending condition is not expected in
      # steady state (the poller's own loop only returns on an unhandled
      # exception or a KeyboardInterrupt-style signal); treat it the same as
      # a crash for restart/backoff purposes, but do not inflate the
      # exponential backoff for what may just be a deliberate short-lived run.
      echo "poller exited status=0 after ${uptime}s; restarting" | tee -a "${LOG_FILE}"
    else
      echo "poller exited status=${status} after ${uptime}s; restarting" | tee -a "${LOG_FILE}"
    fi

    restart_count=$((restart_count + 1))
    export TGMS_OSV_RESTART_COUNT="${restart_count}"

    if [ "${uptime}" -ge "${CLEAN_UPTIME_S}" ]; then
      backoff="${BACKOFF_MIN_S}"
    else
      backoff=$((backoff * 2))
      if [ "${backoff}" -gt "${BACKOFF_MAX_S}" ]; then
        backoff="${BACKOFF_MAX_S}"
      fi
    fi
    echo "backoff ${backoff}s before restart #${restart_count}" | tee -a "${LOG_FILE}"
    sleep "${backoff}"
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
  touch "${STOP_FILE}"
  if ! _is_running "${PID_FILE}"; then
    echo "not running"
    rm -f "${STOP_FILE}"
    return 0
  fi
  echo "stop requested; waiting up to $((BACKOFF_MAX_S + 30))s for graceful exit"
  local waited=0
  local deadline=$((BACKOFF_MAX_S + 30))
  while _is_running "${PID_FILE}" && [ "${waited}" -lt "${deadline}" ]; do
    sleep 2
    waited=$((waited + 2))
  done
  if _is_running "${PID_FILE}"; then
    local pid
    pid="$(cat "${PID_FILE}")"
    echo "graceful stop timed out; killing pid ${pid}" >&2
    kill "${pid}" 2>/dev/null
    sleep 1
    kill -9 "${pid}" 2>/dev/null
    rm -f "${PID_FILE}"
  else
    echo "stopped"
  fi
  rm -f "${STOP_FILE}"
}

cmd_status() {
  if _is_running "${PID_FILE}"; then
    echo "running (pid $(cat "${PID_FILE}"))"
    echo "tail -f ${LOG_FILE}   # supervisor log"
    echo "tail -f ${METRICS}    # per-cycle metrics"
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

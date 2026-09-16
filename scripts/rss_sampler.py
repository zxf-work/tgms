#!/usr/bin/env python3
"""Sample VmHWM (and VmRSS) for a given PID every --interval seconds,
appending one JSON line per sample to --out, until the PID exits.
Used by the B7 Stage-0 Slurm jobs as the "sampled every 30s" RSS series,
independent of build_synth_store.py's own periodic maxrss figure.
"""
import argparse
import json
import os
import sys
import time


def read_status(pid: int) -> dict:
    out = {}
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    out["vmhwm_kb"] = int(line.split()[1])
                elif line.startswith("VmRSS:"):
                    out["vmrss_kb"] = int(line.split()[1])
    except FileNotFoundError:
        return {}
    return out


def pid_alive(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.out, "a") as f:
        while pid_alive(args.pid):
            sample = read_status(args.pid)
            if sample:
                sample["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                sample["pid"] = args.pid
                f.write(json.dumps(sample, sort_keys=True) + "\n")
                f.flush()
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())

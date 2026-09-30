#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def read_data_units_written(device: str) -> int:
    result = subprocess.run(
        ["nvme", "smart-log", device, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    value = data.get("data_units_written")
    if value is None:
        raise RuntimeError("NVMe SMART log does not contain data_units_written")
    if isinstance(value, str):
        value = value.replace(",", "")
    return int(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sample NVMe SMART host-write telemetry.")
    parser.add_argument("--device", required=True, help="NVMe device, e.g. /dev/nvme0n1")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--stop-file", type=str, default="", help="Path to stop file trigger")
    args = parser.parse_args()

    stop_path = Path(args.stop_file) if args.stop_file else None
    start = time.time()
    previous_units = None

    print(
        "unix_time,elapsed_sec,data_units_written,host_bytes_written,interval_bytes,write_rate_mib_s",
        flush=True,
    )

    while True:
        if stop_path and stop_path.exists():
            break

        now = time.time()
        elapsed = now - start
        if elapsed > args.duration:
            break

        try:
            units = read_data_units_written(args.device)
            total_bytes = units * 1000 * 512
            interval_bytes = 0 if previous_units is None else (units - previous_units) * 1000 * 512
            rate_mib_s = interval_bytes / (1024 * 1024) / args.interval if previous_units is not None else 0.0
            print(
                f"{now:.6f},{elapsed:.3f},{units},{total_bytes},{interval_bytes},{rate_mib_s:.6f}",
                flush=True,
            )
            previous_units = units
        except Exception as exc:
            print(f"telemetry error: {exc}", file=sys.stderr, flush=True)

        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    sys.exit(main())
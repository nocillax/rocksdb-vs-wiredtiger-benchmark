#!/usr/bin/env python3
import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / "build"
RESULTS_ROOT = Path(os.environ.get("RESULTS_ROOT", ROOT / "results"))
RUNTIME_ROOT = Path(os.environ.get("RUNTIME_ROOT", "/dev/shm/storage_engine_bench"))
DATA_ROOT = os.environ.get("BENCH_DATA_ROOT")
NVME_DEVICE = os.environ.get("NVME_DEVICE")

KEY_COUNT = 500_000
VALUE_SIZE = 100
THREADS = 4
READ_PERCENT = 50
CACHE_MB = 512
CHECKPOINT_SEC = 15
SEED = 20260927
PROGRESS_SEC = 5
TELEMETRY_INTERVAL_SEC = 2

PILOT_CONFIGS = [
    ("rocksdb", "uniform", 0.0),
    ("wiredtiger", "uniform", 0.0),
    ("wiredtiger", "extreme", 1.20),
]

FINAL_CONFIGS = [
    ("rocksdb", "uniform", 0.0),
    ("rocksdb", "zipfian_standard", 0.99),
    ("rocksdb", "zipfian_extreme", 1.20),
    ("wiredtiger", "uniform", 0.0),
    ("wiredtiger", "zipfian_standard", 0.99),
    ("wiredtiger", "zipfian_extreme", 1.20),
]


def require_environment() -> None:
    if not DATA_ROOT:
        raise SystemExit("Set BENCH_DATA_ROOT to the filesystem where the benchmark database should live.")
    if not NVME_DEVICE:
        raise SystemExit("Set NVME_DEVICE to the NVMe namespace/controller used by the benchmark.")

    data_root = Path(DATA_ROOT)
    data_root.mkdir(parents=True, exist_ok=True)
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)

    subprocess.run(["sudo", "-v"], check=True)
    subprocess.run(["sudo", "-n", "nvme", "smart-log", NVME_DEVICE, "-o", "json"], check=True, stdout=subprocess.DEVNULL)


def git_revision(path: Path) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def run_config(
    mode: str,
    trial: int,
    engine: str,
    skew_name: str,
    alpha: float,
    warmup_sec: int,
    duration: int,
    order_index: int,
) -> None:
    run_id = f"{mode}_trial{trial:02d}_{engine}_{skew_name}"
    total_duration = warmup_sec + duration
    runtime_dir = RUNTIME_ROOT / run_id
    result_dir = RESULTS_ROOT / "runs" / run_id
    db_dir = Path(DATA_ROOT) / run_id

    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    if result_dir.exists():
        shutil.rmtree(result_dir)
    if db_dir.exists():
        shutil.rmtree(db_dir)

    runtime_dir.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    db_dir.mkdir(parents=True)

    metrics_path = runtime_dir / "metrics.csv"
    progress_path = runtime_dir / "progress.csv"
    telemetry_path = runtime_dir / "telemetry.csv"
    telemetry_err = runtime_dir / "telemetry.err"
    benchmark_log = runtime_dir / "benchmark.log"
    stop_file = runtime_dir / "stop.telemetry"

    binary = BUILD_DIR / ("bench_rocksdb" if engine == "rocksdb" else "bench_wiredtiger")
    if not binary.exists():
        raise SystemExit(f"Missing benchmark binary: {binary}. Run scripts/build_engines.sh first.")

    telemetry_cmd = [
        "sudo", "-n", "python3", str(ROOT / "scripts" / "telemetry_daemon.py"),
        "--device", NVME_DEVICE,
        "--interval", str(TELEMETRY_INTERVAL_SEC),
        "--duration", str(total_duration + 180),
        "--stop-file", str(stop_file),
    ]

    with telemetry_path.open("w") as telemetry_out, telemetry_err.open("w") as telemetry_error:
        telemetry_proc = subprocess.Popen(telemetry_cmd, stdout=telemetry_out, stderr=telemetry_error)

        time.sleep(15)
        benchmark_cmd = [
            str(binary),
            f"--db={db_dir}",
            f"--metrics={metrics_path}",
            f"--progress={progress_path}",
            f"--warmup={warmup_sec}",
            f"--duration={duration}",
            f"--threads={THREADS}",
            f"--keys={KEY_COUNT}",
            f"--value-size={VALUE_SIZE}",
            f"--read-percent={READ_PERCENT}",
            f"--alpha={alpha}",
            f"--seed={SEED}",
            f"--cache-mb={CACHE_MB}",
            f"--checkpoint-sec={CHECKPOINT_SEC}",
            f"--progress-sec={PROGRESS_SEC}",
        ]

        started = time.time()
        timeout = total_duration + 180
        with benchmark_log.open("w") as log:
            bench_proc = subprocess.Popen(benchmark_cmd, stdout=log, stderr=subprocess.STDOUT)
            try:
                bench_proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                bench_proc.kill()
                bench_proc.wait()
                stop_file.touch()
                telemetry_proc.wait(timeout=15)
                raise RuntimeError(f"Benchmark timed out: {run_id}")
        finished = time.time()

        stop_file.touch()
        try:
            telemetry_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            telemetry_proc.terminate()
            telemetry_proc.wait()
        if bench_proc.returncode != 0:
            raise RuntimeError(f"Benchmark failed with exit code {bench_proc.returncode}: {run_id}")

    manifest = {
        "run_id": run_id,
        "mode": mode,
        "trial": trial,
        "order_index": order_index,
        "engine": engine,
        "skew_name": skew_name,
        "zipf_alpha": alpha,
        "warmup_sec": warmup_sec,
        "duration_sec": duration,
        "total_duration_sec": total_duration,
        "key_count": KEY_COUNT,
        "value_size": VALUE_SIZE,
        "threads": THREADS,
        "read_percent": READ_PERCENT,
        "update_percent": 100 - READ_PERCENT,
        "cache_mb": CACHE_MB,
        "checkpoint_sec": CHECKPOINT_SEC,
        "seed": SEED,
        "nvme_device": NVME_DEVICE,
        "bench_data_root": str(DATA_ROOT),
        "started_wall": started,
        "finished_wall": finished,
        "rocksdb_revision": git_revision(ROOT / "rocksdb"),
        "wiredtiger_revision": git_revision(ROOT / "wiredtiger"),
    }

    for src in [metrics_path, progress_path, telemetry_path, telemetry_err, benchmark_log]:
        if src.exists():
            shutil.copy2(src, result_dir / src.name)

    (result_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Completed {run_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the common RocksDB/WiredTiger benchmark suite.")
    parser.add_argument("--mode", choices=["pilot", "final"], default="pilot")
    parser.add_argument(
        "--duration",
        type=int,
        default=1800,
        help="Measured workload duration per configuration.",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=None,
        help="Warm-up duration before measurement. Defaults to 0 for pilot and 900 s for final.",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="Number of repeated trials. Pilot normally uses 1.",
    )
    args = parser.parse_args()

    require_environment()

    warmup_sec = (
        900
        if args.warmup is None and args.mode == "final"
        else 0
        if args.warmup is None
        else args.warmup
    )

    configs = PILOT_CONFIGS if args.mode == "pilot" else FINAL_CONFIGS
    if warmup_sec < 0:
        raise SystemExit("--warmup cannot be negative.")
    if args.duration < 60:
        raise SystemExit("Use at least 60 seconds of measurement per configuration.")
    if args.trials < 1:
        raise SystemExit("--trials must be at least 1.")

    order_rng = random.Random(SEED)
    print(f"Mode: {args.mode}")
    print(
        f"Configurations: {len(configs)} | Trials: {args.trials} | "
        f"Warm-up: {warmup_sec}s | Measurement: {args.duration}s"
    )
    print(f"NVMe device: {NVME_DEVICE}")
    print(f"Database root: {DATA_ROOT}")

    for trial in range(1, args.trials + 1):
        ordered = configs[:]
        order_rng.seed(SEED + trial)
        order_rng.shuffle(ordered)
        for index, (engine, skew_name, alpha) in enumerate(ordered, start=1):
            run_config(
                args.mode,
                trial,
                engine,
                skew_name,
                alpha,
                warmup_sec,
                args.duration,
                index,
            )

    print("Benchmark suite complete.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.")
        raise SystemExit(130)

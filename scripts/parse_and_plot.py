#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# IEEE standard typography and layout settings
plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["Times New Roman", "DejaVu Serif", "Times"],
    "font.size": 8,
    "axes.labelsize": 8,
    "axes.titlesize": 8,
    "xtick.labelsize": 7.5,
    "ytick.labelsize": 7.5,
    "legend.fontsize": 7.5,
    "figure.titlesize": 8,
    "lines.linewidth": 1.2,
    "lines.markersize": 4.5,
    "grid.linewidth": 0.5,
})

ROOT = Path(__file__).resolve().parents[1]
RESULTS_ROOT = Path(__import__("os").environ.get("RESULTS_ROOT", ROOT / "results"))
RUNS_ROOT = RESULTS_ROOT / "runs"
FIG_ROOT = RESULTS_ROOT / "figures"


def read_single_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


def host_write_delta(telemetry: pd.DataFrame, start_unix: float, end_unix: float):
    if telemetry.empty:
        return np.nan, np.nan, np.nan
    telemetry = telemetry.sort_values("unix_time")
    before = telemetry[telemetry["unix_time"] <= start_unix]
    after = telemetry[telemetry["unix_time"] >= end_unix]
    if before.empty:
        start_row = telemetry.iloc[0]
    else:
        start_row = before.iloc[-1]
    if after.empty:
        end_row = telemetry.iloc[-1]
    else:
        end_row = after.iloc[0]
    delta = float(end_row["host_bytes_written"] - start_row["host_bytes_written"])
    elapsed = float(end_row["unix_time"] - start_row["unix_time"])
    rate = delta / elapsed / (1024 * 1024) if elapsed > 0 else np.nan
    return delta, elapsed, rate


def summarize_run(run_dir: Path) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    metrics = read_single_csv(run_dir / "metrics.csv")
    telemetry = read_single_csv(run_dir / "telemetry.csv")
    if metrics.empty:
        raise RuntimeError(f"Missing metrics.csv: {run_dir}")

    row = metrics.iloc[0]
    physical_bytes, telemetry_span, physical_rate = host_write_delta(
        telemetry,
        float(row["start_unix"]),
        float(row["end_unix"]),
    )
    logical_bytes = float(row["logical_write_bytes"])
    waf = physical_bytes / logical_bytes if logical_bytes > 0 else np.nan

    return {
        "run_id": manifest["run_id"],
        "mode": manifest["mode"],
        "trial": manifest["trial"],
        "order_index": manifest["order_index"],
        "engine": manifest["engine"],
        "skew": manifest["skew_name"],
        "alpha": manifest["zipf_alpha"],
        "duration_sec": manifest["duration_sec"],
        "wall_sec": float(row["wall_sec"]),
        "ops": int(row["ops"]),
        "reads": int(row["reads"]),
        "updates": int(row["updates"]),
        "errors": int(row["errors"]),
        "logical_write_mb": logical_bytes / (1024 * 1024),
        "host_write_mb": physical_bytes / (1024 * 1024),
        "hd_waf": waf,
        "host_write_rate_mib_s": physical_rate,
        "telemetry_span_sec": telemetry_span,
        "ops_per_sec": float(row["ops"]) / float(row["wall_sec"]),
        "cpu_seconds": float(row["cpu_seconds"]),
        "process_cpu_util_percent": float(row["process_cpu_util_percent"]),
    }


def save_summary(rows: list[dict]) -> pd.DataFrame:
    summary = pd.DataFrame(rows)
    summary = summary.sort_values(["mode", "engine", "skew", "trial"])
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    summary.to_csv(RESULTS_ROOT / "summary.csv", index=False)
    return summary


def plot_pilot_run(run_dir: Path, manifest: dict) -> None:
    progress = read_single_csv(run_dir / "progress.csv")
    telemetry = read_single_csv(run_dir / "telemetry.csv")
    metrics = read_single_csv(run_dir / "metrics.csv")
    if progress.empty or telemetry.empty or metrics.empty:
        return

    row = metrics.iloc[0]
    start_unix = float(row["start_unix"])
    end_unix = float(row["end_unix"])
    p = progress.copy()
    p["dt"] = p["elapsed_sec"].diff()
    p["ops_rate"] = p["ops"].diff() / p["dt"]
    p["update_rate_mib_s"] = p["updates"].diff() * float(manifest["value_size"]) / p["dt"] / (1024 * 1024)
    p = p.replace([np.inf, -np.inf], np.nan)

    t = telemetry[(telemetry["unix_time"] >= start_unix - 5) & (telemetry["unix_time"] <= end_unix + 5)].copy()
    if not t.empty:
        t["elapsed_from_start"] = t["unix_time"] - start_unix

    fig, axes = plt.subplots(2, 1, figsize=(3.5, 3.2), sharex=True)

    axes[0].plot(p["elapsed_sec"], p["ops_rate"] / 1000.0, linewidth=1.0)
    axes[0].set_ylabel("Throughput (kops/s)")
    axes[0].grid(True, alpha=0.3)

    if not t.empty:
        axes[1].plot(t["elapsed_from_start"], t["write_rate_mib_s"], linewidth=1.0)
    axes[1].set_ylabel("Write Rate (MiB/s)")
    axes[1].set_xlabel("Elapsed Time (s)")
    axes[1].grid(True, alpha=0.3)

    fig.tight_layout(pad=0.4)
    FIG_ROOT.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIG_ROOT / f"{manifest['run_id']}_pilot.png", dpi=300, bbox_inches="tight")
    plt.close(fig)


def _prepare_final_summary(summary: pd.DataFrame) -> pd.DataFrame:
    final = summary[summary["mode"] == "final"].copy()

    order = ["uniform", "zipfian_standard", "zipfian_extreme"]
    final["skew"] = pd.Categorical(final["skew"], categories=order, ordered=True)

    return final.sort_values(["engine", "skew", "trial"])


def plot_final_summary_figures(summary: pd.DataFrame) -> None:
    final = _prepare_final_summary(summary)

    if final.empty:
        return

    FIG_ROOT.mkdir(parents=True, exist_ok=True)

    order = ["uniform", "zipfian_standard", "zipfian_extreme"]
    labels = ["Uniform", "Zipf 0.99", "Zipf 1.20"]
    engines = ["rocksdb", "wiredtiger"]
    engine_labels = {
        "rocksdb": "RocksDB",
        "wiredtiger": "WiredTiger",
    }

    positions = np.arange(len(order))
    offsets = {
        "rocksdb": -0.09,
        "wiredtiger": 0.09,
    }

    # ---------------------------------------------------------------
    # Figure 1: Throughput (Scaled to kops/s to eliminate 1e6 artifact)
    # ---------------------------------------------------------------
    fig, ax = plt.subplots(figsize=(3.5, 2.4))

    for engine in engines:
        data = final[final["engine"] == engine]
        means = []
        stds = []

        for skew in order:
            # Scale by 1e6 to express in Million ops/s
            values = (data.loc[data["skew"] == skew, "ops_per_sec"] / 1e6).dropna()
            means.append(values.mean() if not values.empty else np.nan)
            stds.append(values.std(ddof=1) if len(values) > 1 else 0.0)

        ax.errorbar(
            positions + offsets[engine],
            means,
            yerr=stds,
            marker="o",
            capsize=3,
            label=engine_labels[engine],
        )

    ax.set_xticks(positions)
    ax.set_xticklabels(labels)
    ax.set_xlim(-0.4, len(order) - 0.6)
    ax.set_ylabel("Throughput (Million ops/s)")
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(frameon=True, framealpha=0.85)

    fig.tight_layout(pad=0.3)
    fig.savefig(FIG_ROOT / "final_throughput.png", dpi=300, bbox_inches="tight")
    plt.close(fig)

    # ---------------------------------------------------------------
    # Figure 2: HD-WAF
    # ---------------------------------------------------------------
    fig, ax = plt.subplots(figsize=(3.5, 2.4))

    for engine in engines:
        data = final[final["engine"] == engine]
        means = []
        stds = []

        for skew in order:
            values = data.loc[data["skew"] == skew, "hd_waf"].dropna()
            means.append(values.mean() if not values.empty else np.nan)
            stds.append(values.std(ddof=1) if len(values) > 1 else 0.0)

        ax.errorbar(
            positions + offsets[engine],
            means,
            yerr=stds,
            marker="o",
            capsize=3,
            label=engine_labels[engine],
        )

    ax.set_xticks(positions)
    ax.set_xticklabels(labels)
    ax.set_xlim(-0.4, len(order) - 0.6)
    ax.set_ylabel("HD-WAF")
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(frameon=True, framealpha=0.85)

    fig.tight_layout(pad=0.3)
    fig.savefig(FIG_ROOT / "final_hd_waf.png", dpi=300, bbox_inches="tight")
    plt.close(fig)

    # ---------------------------------------------------------------
    # Figure 3: CPU utilization
    # ---------------------------------------------------------------
    fig, ax = plt.subplots(figsize=(3.5, 2.4))

    for engine in engines:
        data = final[final["engine"] == engine]
        means = []
        stds = []

        for skew in order:
            values = data.loc[
                data["skew"] == skew,
                "process_cpu_util_percent"
            ].dropna()
            means.append(values.mean() if not values.empty else np.nan)
            stds.append(values.std(ddof=1) if len(values) > 1 else 0.0)

        ax.errorbar(
            positions + offsets[engine],
            means,
            yerr=stds,
            marker="o",
            capsize=3,
            label=engine_labels[engine],
        )

    ax.set_xticks(positions)
    ax.set_xticklabels(labels)
    ax.set_xlim(-0.4, len(order) - 0.6)
    ax.set_ylabel("CPU Utilization (%)")
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend(frameon=True, framealpha=0.85)

    fig.tight_layout(pad=0.3)
    fig.savefig(FIG_ROOT / "final_cpu_utilization.png", dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_final_host_write_rate(summary: pd.DataFrame) -> None:
    final = summary[summary["mode"] == "final"].copy()

    if final.empty:
        return

    FIG_ROOT.mkdir(parents=True, exist_ok=True)

    order = ["uniform", "zipfian_standard", "zipfian_extreme"]
    titles = {
        "uniform": "Uniform",
        "zipfian_standard": "Zipf 0.99",
        "zipfian_extreme": "Zipf 1.20",
    }
    engine_labels = {
        "rocksdb": "RocksDB",
        "wiredtiger": "WiredTiger",
    }

    # Fits standard 2-column IEEE width (7.0 in) or compact single-column (3.5 in)
    fig, axes = plt.subplots(
        3,
        1,
        figsize=(7.0, 4.2),
        sharex=True,
    )

    for ax, skew in zip(axes, order):
        skew_runs = final[final["skew"] == skew]

        for engine in ["rocksdb", "wiredtiger"]:
            engine_runs = skew_runs[skew_runs["engine"] == engine]

            traces = []

            for _, row in engine_runs.iterrows():
                run_dir = RUNS_ROOT / row["run_id"]
                telemetry = read_single_csv(run_dir / "telemetry.csv")

                if telemetry.empty:
                    continue

                metrics = read_single_csv(run_dir / "metrics.csv")

                if metrics.empty:
                    continue

                metrics_row = metrics.iloc[0]
                start_unix = float(metrics_row["start_unix"])
                end_unix = float(metrics_row["end_unix"])

                trace = telemetry[
                    (telemetry["unix_time"] >= start_unix)
                    & (telemetry["unix_time"] <= end_unix)
                ].copy()

                if trace.empty:
                    continue

                trace["elapsed_sec"] = (
                    trace["unix_time"] - start_unix
                )

                traces.append(trace[["elapsed_sec", "write_rate_mib_s"]])

            if not traces:
                continue

            common_end = int(
                min(trace["elapsed_sec"].max() for trace in traces)
            )

            grid = np.arange(0, common_end + 1, 10)
            interpolated = []

            for trace in traces:
                trace = trace.drop_duplicates("elapsed_sec")
                values = np.interp(
                    grid,
                    trace["elapsed_sec"],
                    trace["write_rate_mib_s"],
                )
                interpolated.append(values)

            values = np.vstack(interpolated)

            mean_rate = values.mean(axis=0)
            std_rate = (
                values.std(axis=0, ddof=1)
                if values.shape[0] > 1
                else np.zeros_like(mean_rate)
            )

            ax.plot(
                grid,
                mean_rate,
                linewidth=1.2,
                label=engine_labels[engine],
            )

            ax.fill_between(
                grid,
                mean_rate - std_rate,
                mean_rate + std_rate,
                alpha=0.15,
            )

        ax.set_ylabel("Rate (MiB/s)")
        # Standardize the vertical range across all subplots and leave headroom for the legend
        ax.set_ylim(0, 320)
        # Inset subplot label to avoid consuming vertical margin
        ax.text(
            0.02, 0.85, titles[skew],
            transform=ax.transAxes,
            weight="bold",
            fontsize=7.5,
        )
        ax.grid(True, alpha=0.3)

    # Single shared legend in the top axis headroom
    axes[0].legend(loc="upper right", ncol=2, framealpha=0.85)
    axes[-1].set_xlabel("Elapsed Time (s)")

    fig.tight_layout(pad=0.5)
    fig.savefig(
        FIG_ROOT / "final_host_write_rate.png",
        dpi=300,
        bbox_inches="tight",
    )
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse benchmark runs and generate pilot/final summaries.")
    parser.add_argument("--mode", choices=["pilot", "final", "all"], default="all")
    args = parser.parse_args()

    run_dirs = sorted(p for p in RUNS_ROOT.iterdir() if p.is_dir()) if RUNS_ROOT.exists() else []
    rows = []

    for run_dir in run_dirs:
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text())
        if args.mode != "all" and manifest["mode"] != args.mode:
            continue
        try:
            rows.append(summarize_run(run_dir))
            if manifest["mode"] == "pilot":
                plot_pilot_run(run_dir, manifest)
        except Exception as exc:
            print(f"Skipping {run_dir.name}: {exc}")

    if not rows:
        print("No completed runs found.")
        return 1

    summary = save_summary(rows)
    modes = ["pilot", "final"] if args.mode == "all" else [args.mode]
    for mode in modes:
        if mode == "pilot":
            for run_dir in run_dirs:
                manifest_path = run_dir / "manifest.json"
                if not manifest_path.exists():
                    continue

                manifest = json.loads(manifest_path.read_text())

                if manifest["mode"] == "pilot":
                    plot_pilot_run(run_dir, manifest)

        elif mode == "final":
            plot_final_summary_figures(summary)
            plot_final_host_write_rate(summary)

    columns = [
        "mode", "trial", "engine", "skew", "ops_per_sec", "updates",
        "logical_write_mb", "host_write_mb", "hd_waf", "host_write_rate_mib_s",
        "errors", "process_cpu_util_percent"
    ]
    print(summary[columns].to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print(f"\nSaved summary to {RESULTS_ROOT / 'summary.csv'}")
    print(f"Saved figures to {FIG_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

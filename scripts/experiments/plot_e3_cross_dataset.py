#!/usr/bin/env python3
"""
E3 cross-dataset plot: grouped bars, one group per dataset, one bar per system.

Reads the summary.csv written by run_e3_cross_dataset.py and renders the fixed
hop-count comparison across datasets for the three canonical systems, using the
presentation names from CLAUDE.md ("Experiment Comparison Systems"):

  nebuladb           -> Graphite
  obliviator_chained -> Obliviator chained
  full_mwj_no_filter -> Full MWJ

Failed cells are drawn honestly, never as a fake latency:
  TIMEOUT -> hatched outline bar at the cell's timeout budget (a true lower
             bound: the run exceeded it), labeled "> <budget>".
  OOM     -> an x marker at the baseline labeled "OOM" (no time bound exists).

Usage:
  python3 scripts/experiments/plot_e3_cross_dataset.py [summary.csv]
  # default input: <project>/results/e3_cross_dataset/summary.csv
  # output: e3_cross_dataset.png + .pdf next to the input CSV
"""

import csv
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SUMMARY = PROJECT_DIR / "results" / "e3_cross_dataset" / "summary.csv"

# Fixed system order and categorical colors (validated palette slots 1-3;
# aqua/yellow are sub-3:1 on white, mitigated by direct value labels on bars).
SYSTEMS = [
    ("nebuladb", "Graphite", "#2a78d6"),
    ("obliviator_chained", "Obliviator chained", "#1baf7a"),
    ("full_mwj_no_filter", "Full MWJ", "#eda100"),
]

DATASET_LABELS = {
    "banking_10k": "Banking-10k\n(50k edges)",
    "banking_1M": "Banking\n(5M edges)",
    "hi_small": "AML HI-Small\n(5.1M edges)",
    "snb_sf30": "LDBC SNB\n(12M edges)",
    "patents": "SNAP Patents\n(16.5M edges)",
    "hi_medium": "AML HI-Medium\n(31.9M edges)",
    "hi_large": "AML HI-Large\n(180M edges)",
}

INK = "#0b0b0b"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"


def fmt_seconds(sec: float) -> str:
    if sec >= 3600:
        return f"{sec/3600:.1f}h"
    if sec >= 60:
        return f"{sec/60:.1f}m"
    if sec >= 10:
        return f"{sec:.0f}s"
    if sec >= 1:
        return f"{sec:.1f}s"
    return f"{sec*1000:.0f}ms"


def main():
    summary_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SUMMARY
    if not summary_path.is_file():
        sys.exit(f"summary not found: {summary_path}")

    with open(summary_path) as f:
        cells = list(csv.DictReader(f))
    if not cells:
        sys.exit(f"{summary_path} is empty")

    # Timeout budgets (for the TIMEOUT lower-bound bar) from run metadata.
    cell_timeout = obl_timeout = None
    meta_path = summary_path.parent / "run_metadata.json"
    if meta_path.is_file():
        margs = json.loads(meta_path.read_text()).get("args", {})
        cell_timeout = margs.get("cell_timeout") or None
        obl_timeout = margs.get("obliviator_timeout") or None

    hop = next((c["query"].split("_")[1] for c in cells if c.get("query")), "?")

    # Datasets in ascending edge order.
    ds = {}
    for c in cells:
        ds[c["dataset"]] = int(c["edges"])
    datasets = [d for d, _ in sorted(ds.items(), key=lambda kv: kv[1])]

    by_cell = {(c["system"], c["dataset"]): c for c in cells}

    fig, ax = plt.subplots(figsize=(7.2, 3.6), dpi=200)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    n_sys = len(SYSTEMS)
    group_w = 0.78
    bar_w = group_w / n_sys
    floor_s = 1e-1  # log-axis floor

    max_val = floor_s
    for key, _, _ in SYSTEMS:
        for d in datasets:
            c = by_cell.get((key, d))
            if c is None:
                continue
            if c["median_ms"]:
                max_val = max(max_val, float(c["median_ms"]) / 1000.0)
            elif c["output_rows"] == "TIMEOUT":
                budget = (obl_timeout if key == "obliviator_chained"
                          else cell_timeout)
                if budget:
                    max_val = max(max_val, float(budget))
    top = max_val * 4  # headroom for value labels on the log axis

    for si, (key, disp, color) in enumerate(SYSTEMS):
        for di, d in enumerate(datasets):
            x = di - group_w / 2 + bar_w * (si + 0.5)
            c = by_cell.get((key, d))
            if c is None:
                continue
            if c["median_ms"]:
                sec = float(c["median_ms"]) / 1000.0
                ax.bar(x, sec, width=bar_w * 0.9, bottom=None, color=color,
                       edgecolor="white", linewidth=0.6, zorder=3)
                ax.annotate(fmt_seconds(sec), (x, sec), xytext=(0, 3),
                            textcoords="offset points", ha="center",
                            va="bottom", fontsize=7, color=INK, zorder=4)
            elif c["output_rows"] == "TIMEOUT":
                budget = obl_timeout if key == "obliviator_chained" else cell_timeout
                bound = float(budget) if budget else max_val
                ax.bar(x, bound, width=bar_w * 0.9, facecolor="none",
                       edgecolor=color, linewidth=1.0, hatch="///", zorder=3)
                ax.annotate(f"> {fmt_seconds(bound)}\n(T/O)", (x, bound),
                            xytext=(0, 3), textcoords="offset points",
                            ha="center", va="bottom", fontsize=6.5,
                            color=INK, zorder=4)
            else:  # OOM / crash: no time bound exists — mark the baseline.
                ax.plot([x], [floor_s * 1.35], marker="x", markersize=7,
                        markeredgewidth=1.8, color=color, zorder=4)
                ax.annotate("OOM", (x, floor_s * 1.35), xytext=(0, 5),
                            textcoords="offset points", ha="center",
                            va="bottom", fontsize=6.5, color=INK, zorder=4)

    ax.set_yscale("log")
    ax.set_ylim(floor_s, top)
    ax.set_ylabel("Latency (s, log scale)", fontsize=9, color=INK)
    ax.set_xticks(range(len(datasets)))
    ax.set_xticklabels([DATASET_LABELS.get(d, d) for d in datasets],
                       fontsize=8.5, color=INK)
    ax.set_title(f"{hop.rstrip('hop')}-hop chain query across datasets",
                 fontsize=10, color=INK)

    ax.yaxis.grid(True, color=GRID, linewidth=0.7, zorder=0)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(BASELINE)
    ax.tick_params(colors=MUTED, labelsize=8)
    for lbl in ax.get_xticklabels() + ax.get_yticklabels():
        lbl.set_color(INK)

    handles = [Patch(facecolor=color, label=disp) for _, disp, color in SYSTEMS]
    handles.append(Patch(facecolor="none", edgecolor=MUTED, hatch="///",
                         label="exceeded budget (T/O)"))
    ax.legend(handles=handles, fontsize=7.5, frameon=False, ncol=4,
              loc="upper center", bbox_to_anchor=(0.5, -0.22))

    fig.tight_layout()
    for ext in ("png", "pdf"):
        out = summary_path.parent / f"e3_cross_dataset.{ext}"
        fig.savefig(out, bbox_inches="tight")
        print(f"wrote {out}")


if __name__ == "__main__":
    main()

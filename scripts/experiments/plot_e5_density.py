#!/usr/bin/env python3
"""
E5 output-sensitivity plot (docs/e5_output_sensitivity.md).

Reads the summary.csv written by run_e5_density.py and renders two panels:

  (a) Setup — per density variant, unfiltered vs anchor-filtered 2-hop output
      rows (log scale, grouped bars). Input size is identical everywhere; only
      the outputs diverge.
  (b) Result — latency vs unfiltered 2-hop output rows, log-log, one line per
      system. Baselines are expected to track the unfiltered output (slope ~1
      guide shown); Graphite stays flat.

System presentation names per CLAUDE.md ("Experiment Comparison Systems"):
nebuladb -> Graphite, obliviator_chained -> Obliviator chained,
full_mwj_no_filter -> Full MWJ. Failed cells are drawn honestly (TIMEOUT =
open marker at the budget, a true lower bound; OOM = x marker at the floor),
never as a fake latency.

Usage:
  python3 scripts/experiments/plot_e5_density.py [summary.csv]
  # default input: <project>/results/e5_density/summary.csv
  # output: e5_density.png + .pdf next to the input CSV
"""

import csv
import json
import math
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SUMMARY = PROJECT_DIR / "results" / "e5_density" / "summary.csv"

# Same categorical assignment as the E1/E3 figures (color follows the system
# across the whole paper). Yellow's sub-3:1 contrast on white is mitigated by
# direct value labels, as in E3.
SYSTEMS = [
    ("nebuladb", "Graphite", "#2a78d6"),
    ("obliviator_chained", "Obliviator chained", "#1baf7a"),
    ("full_mwj_no_filter", "Full MWJ", "#eda100"),
]

UNFILTERED_BAR = "#b3b1a7"  # neutral: the outputs panel is setup, not identity

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


def fmt_rows(n: float) -> str:
    if n >= 1e6:
        return f"{n/1e6:.0f}M" if n >= 10e6 else f"{n/1e6:.1f}M"
    if n >= 1e3:
        return f"{n/1e3:.0f}k"
    return f"{n:.0f}"


def main():
    summary_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SUMMARY
    if not summary_path.is_file():
        sys.exit(f"summary not found: {summary_path}")

    with open(summary_path) as f:
        cells = list(csv.DictReader(f))
    if not cells:
        sys.exit(f"{summary_path} is empty")

    cell_timeout = obl_timeout = None
    meta_path = summary_path.parent / "run_metadata.json"
    if meta_path.is_file():
        margs = json.loads(meta_path.read_text()).get("args", {})
        cell_timeout = margs.get("cell_timeout") or None
        obl_timeout = margs.get("obliviator_timeout") or None

    # Variants in ascending density (hub_fraction) order.
    variants = {}
    for c in cells:
        variants[c["dataset"]] = {
            "p": float(c["hub_fraction"]),
            "unfiltered": int(c["unfiltered_2hop_rows"]),
            "filtered": int(c["filtered_2hop_rows"]),
            "edges": int(c["edges"]),
        }
    order = sorted(variants, key=lambda d: variants[d]["p"])
    by_cell = {(c["system"], c["dataset"]): c for c in cells}

    fig, (ax_a, ax_b) = plt.subplots(
        1, 2, figsize=(9.6, 3.6), dpi=200, gridspec_kw={"width_ratios": [1, 1.35]})
    fig.patch.set_facecolor("white")

    # ------------------------------------------------------------------ (a)
    ax = ax_a
    ax.set_facecolor("white")
    bar_w = 0.38
    for di, d in enumerate(order):
        v = variants[d]
        for off, val, color in ((-bar_w / 2, v["unfiltered"], UNFILTERED_BAR),
                                (bar_w / 2, max(v["filtered"], 1), "#2a78d6")):
            ax.bar(di + off, val, width=bar_w * 0.92, color=color,
                   edgecolor="white", linewidth=0.6, zorder=3)
            ax.annotate(fmt_rows(val), (di + off, val), xytext=(0, 3),
                        textcoords="offset points", ha="center", va="bottom",
                        fontsize=7, color=INK, zorder=4)
    ax.set_yscale("log")
    ymax = max(v["unfiltered"] for v in variants.values())
    ax.set_ylim(1, ymax * 8)
    ax.set_ylabel("2-hop output rows (log scale)", fontsize=9, color=INK)
    ax.set_xticks(range(len(order)))
    ax.set_xticklabels([f"{d}\np={variants[d]['p']:g}" for d in order],
                       fontsize=8, color=INK)
    edges = variants[order[0]]["edges"]
    ax.set_title(f"(a) Same input ({fmt_rows(edges)} edges), diverging outputs",
                 fontsize=9.5, color=INK)
    ax.legend(handles=[Patch(facecolor=UNFILTERED_BAR, label="unfiltered join"),
                       Patch(facecolor="#2a78d6", label="after filter")],
              fontsize=7.5, frameon=False, ncol=2, loc="upper center",
              bbox_to_anchor=(0.5, -0.18))

    # ------------------------------------------------------------------ (b)
    ax = ax_b
    ax.set_facecolor("white")
    max_val = 0.0

    series = {}
    for key, disp, color in SYSTEMS:
        xs, ys, fails = [], [], []
        for d in order:
            c = by_cell.get((key, d))
            if c is None:
                continue
            x = variants[d]["unfiltered"]
            if c["median_ms"]:
                sec = float(c["median_ms"]) / 1000.0
                xs.append(x)
                ys.append(sec)
                max_val = max(max_val, sec)
            else:
                kind = c["output_rows"]
                budget = (obl_timeout if key == "obliviator_chained"
                          else cell_timeout)
                fails.append((x, kind, budget))
                if kind == "TIMEOUT" and budget:
                    max_val = max(max_val, float(budget))
        series[key] = (xs, ys, fails)

    # Log-axis floor one decade below the fastest completed cell; failure
    # markers sit just above it.
    completed = [y for xs, ys, _ in series.values() for y in ys]
    floor_s = (10.0 ** (math.floor(math.log10(min(completed))) - 1)
               if completed else 1e-1)
    max_val = max(max_val, floor_s)

    fail_texts_drawn = set()  # one "OOM"/bound label per (variant, kind)

    # Slope-1 guide (cost proportional to unfiltered output), anchored to the
    # Full MWJ series when present.
    guide_key = ("full_mwj_no_filter"
                 if series.get("full_mwj_no_filter", ([],))[0]
                 else "obliviator_chained")
    gx, gy = series.get(guide_key, ([], [], []))[:2]
    if len(gx) >= 2:
        x0, x1 = gx[0], gx[-1]
        y0 = gy[0]
        ax.plot([x0, x1], [y0, y0 * x1 / x0], linestyle=(0, (4, 3)),
                color=BASELINE, linewidth=1.2, zorder=1)
        xm = (x0 * x1) ** 0.5
        ax.annotate("slope 1 (∝ output)", (xm, y0 * xm / x0),
                    xytext=(6, -14), textcoords="offset points",
                    ha="left", fontsize=7, color=MUTED)

    for si, (key, disp, color) in enumerate(SYSTEMS):
        xs, ys, fails = series[key]
        if xs:
            ax.plot(xs, ys, marker="o", markersize=5, linewidth=2,
                    color=color, markeredgecolor="white",
                    markeredgewidth=0.8, zorder=3)
            ax.annotate(disp, (xs[-1], ys[-1]), xytext=(6, 0),
                        textcoords="offset points", ha="left", va="center",
                        fontsize=7.5, color=INK, zorder=4)
        # Small per-system x-offset so coincident failure markers (two systems
        # failing on the same variant) stay individually visible.
        jitter = (0.92, 1.0, 1.09)[si]
        for x, kind, budget in fails:
            if kind == "TIMEOUT" and budget:
                b = float(budget)
                ax.plot([x * jitter], [b], marker="^", markersize=7,
                        color="white", markeredgecolor=color,
                        markeredgewidth=1.6, zorder=4)
                ax.annotate(f"> {fmt_seconds(b)}", (x * jitter, b),
                            xytext=(0, 6), textcoords="offset points",
                            ha="center", fontsize=6.5, color=INK, zorder=4)
            else:
                ax.plot([x * jitter], [floor_s * 1.35], marker="x",
                        markersize=7, markeredgewidth=1.8, color=color,
                        zorder=4)
                if (x, kind) not in fail_texts_drawn:
                    fail_texts_drawn.add((x, kind))
                    ax.annotate(kind, (x, floor_s * 1.35), xytext=(0, 6),
                                textcoords="offset points", ha="center",
                                fontsize=6.5, color=INK, zorder=4)

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_ylim(floor_s, max_val * 6)
    # One tick per variant; suppress minor log ticks that collide at this span.
    variant_xs = [variants[d]["unfiltered"] for d in order]
    ax.set_xticks(variant_xs)
    ax.set_xticklabels([fmt_rows(x) for x in variant_xs])
    ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    ax.set_xlabel("Unfiltered 2-hop output rows (log scale)", fontsize=9,
                  color=INK)
    ax.set_ylabel("Latency (s, log scale)", fontsize=9, color=INK)
    ax.set_title("(b) Latency vs unfiltered output size", fontsize=9.5,
                 color=INK)
    ax.legend(handles=[Line2D([], [], color=c, marker="o", markersize=5,
                              linewidth=2, markeredgecolor="white", label=n)
                       for _, n, c in SYSTEMS],
              fontsize=7.5, frameon=False, loc="lower left")

    for ax in (ax_a, ax_b):
        ax.yaxis.grid(True, color=GRID, linewidth=0.7, zorder=0)
        ax.set_axisbelow(True)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            ax.spines[spine].set_color(BASELINE)
        ax.tick_params(colors=MUTED, labelsize=8)
        for lbl in ax.get_xticklabels() + ax.get_yticklabels():
            lbl.set_color(INK)

    fig.tight_layout()
    for ext in ("png", "pdf"):
        out = summary_path.parent / f"e5_density.{ext}"
        fig.savefig(out, bbox_inches="tight")
        print(f"wrote {out}")


if __name__ == "__main__":
    main()

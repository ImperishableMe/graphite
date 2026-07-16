#!/usr/bin/env python3
"""
E7 predicate-selectivity plot.

Reads the summary.csv written by run_e7_selectivity.py and renders two panels:

  (a) Setup — filtered 2-hop output rows per per-edge selectivity point
      (log scale bars): the predicate threshold theta is the only knob, and it
      sweeps the filtered output across five orders of magnitude on a fixed
      input.
  (b) Result — latency vs MEASURED filtered output rows, log-log. Graphite is
      the swept series (cost tracks the filtered output). Obliviator chained
      has no filter support, so it is a flat theta-invariant line at its
      unfiltered-chain latency. Full MWJ runs --no-filter (identical at every
      theta) and is drawn as a flat annotation — expected OOM.

System presentation names per CLAUDE.md: nebuladb -> Graphite,
obliviator_chained -> Obliviator chained, full_mwj_no_filter -> Full MWJ.
Failed cells are drawn honestly (TIMEOUT = open marker at the budget; OOM =
x marker at the floor), never as a fake latency. Cells whose measured row
count contradicts the oracle (rows_match=0) are INVALID: drawn with a red
ring and label, and reported on stderr.

Usage:
  python3 scripts/experiments/plot_e7_selectivity.py [summary.csv]
  # default input: <project>/results/e7_selectivity/summary.csv
  # output: e7_selectivity.png + .pdf next to the input CSV
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

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SUMMARY = PROJECT_DIR / "results" / "e7_selectivity" / "summary.csv"

# Same categorical assignment as the E1/E3/E5 figures.
GRAPHITE = ("nebuladb", "Graphite", "#2a78d6")
OBLIVIATOR = ("obliviator_chained", "Obliviator chained", "#1baf7a")
FULL_MWJ = ("full_mwj_no_filter", "Full MWJ", "#eda100")
SYSTEMS = [GRAPHITE, OBLIVIATOR, FULL_MWJ]

INK = "#0b0b0b"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
INVALID_RED = "#d63b2a"


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


def fmt_sel(s: float) -> str:
    return "no filter" if s >= 1.0 else f"s={s:g}"


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

    graphite = sorted((c for c in cells if c["system"] == GRAPHITE[0]),
                      key=lambda c: int(c["expected_rows"]))
    if not graphite:
        sys.exit("no nebuladb cells in summary")
    obliviator = next((c for c in cells if c["system"] == OBLIVIATOR[0]), None)
    full_mwj = next((c for c in cells if c["system"] == FULL_MWJ[0]), None)

    fig, (ax_a, ax_b) = plt.subplots(
        1, 2, figsize=(9.6, 3.6), dpi=200, gridspec_kw={"width_ratios": [1, 1.35]})
    fig.patch.set_facecolor("white")

    # ------------------------------------------------------------------ (a)
    # The knob: per-edge selectivity -> filtered 2-hop output rows (oracle).
    ax = ax_a
    ax.set_facecolor("white")
    for i, c in enumerate(graphite):
        val = int(c["expected_rows"])
        ax.bar(i, val, width=0.62, color=GRAPHITE[2], edgecolor="white",
               linewidth=0.6, zorder=3)
        ax.annotate(fmt_rows(val), (i, val), xytext=(0, 3),
                    textcoords="offset points", ha="center", va="bottom",
                    fontsize=7, color=INK, zorder=4)
    ax.set_yscale("log")
    ymax = max(int(c["expected_rows"]) for c in graphite)
    ax.set_ylim(1, ymax * 8)
    ax.set_ylabel("Filtered 2-hop output rows (log scale)", fontsize=9,
                  color=INK)
    ax.set_xticks(range(len(graphite)))
    ax.set_xticklabels([fmt_sel(float(c["s_target"])) for c in graphite],
                       fontsize=7.5, color=INK, rotation=30, ha="right")
    ax.set_title("(a) Same input, predicate sweeps the output",
                 fontsize=9.5, color=INK)

    # ------------------------------------------------------------------ (b)
    ax = ax_b
    ax.set_facecolor("white")
    max_val = 0.0

    xs, ys, sels, invalid_pts, fails = [], [], [], [], []
    for c in graphite:
        expected = int(c["expected_rows"])
        s = float(c["s_target"])
        out = c["output_rows"]
        if c["median_ms"]:
            x = int(out) if out.isdigit() else expected  # measured rows
            sec = float(c["median_ms"]) / 1000.0
            xs.append(x)
            ys.append(sec)
            sels.append(s)
            max_val = max(max_val, sec)
            if c["rows_match"] == "0":
                invalid_pts.append((x, sec, s))
                print(f"WARNING: INVALID cell s={s}: measured {out} rows, "
                      f"expected {expected}", file=sys.stderr)
        elif out in ("TIMEOUT", "OOM"):
            fails.append((expected, out, s))
            if out == "TIMEOUT" and cell_timeout:
                max_val = max(max_val, float(cell_timeout))
        # SKIPPED cells (consequence of an earlier failure) are not drawn.

    obl_sec = None
    if obliviator and obliviator["median_ms"]:
        obl_sec = float(obliviator["median_ms"]) / 1000.0
        max_val = max(max_val, obl_sec)
    mwj_sec = None
    mwj_fail = None
    if full_mwj:
        if full_mwj["median_ms"]:
            mwj_sec = float(full_mwj["median_ms"]) / 1000.0
            max_val = max(max_val, mwj_sec)
        else:
            mwj_fail = full_mwj["output_rows"]  # OOM / TIMEOUT

    floor_s = (10.0 ** (math.floor(math.log10(min(ys))) - 1) if ys else 1e-1)
    max_val = max(max_val, floor_s)
    x_lo = min([x for x in xs] + [f[0] for f in fails] or [1])
    x_hi = max([x for x in xs] + [f[0] for f in fails] or [1])

    # Slope-1 guide (cost proportional to filtered output), anchored to the
    # largest completed Graphite point.
    if len(xs) >= 2:
        x1, y1 = xs[-1], ys[-1]
        x0 = x_lo
        ax.plot([x0, x1], [y1 * x0 / x1, y1], linestyle=(0, (4, 3)),
                color=BASELINE, linewidth=1.2, zorder=1)
        xm = (x0 * x1) ** 0.5
        ax.annotate("slope 1 (∝ filtered output)", (xm, y1 * xm / x1),
                    xytext=(6, -14), textcoords="offset points",
                    ha="left", fontsize=7, color=MUTED)

    # Theta-invariant baselines span the whole x-range as flat lines.
    if obl_sec is not None:
        ax.axhline(obl_sec, color=OBLIVIATOR[2], linewidth=2, zorder=2)
        ax.annotate(f"{OBLIVIATOR[1]} — no filter support ({fmt_seconds(obl_sec)})",
                    (x_lo, obl_sec), xytext=(2, 5), textcoords="offset points",
                    ha="left", fontsize=7.5, color=INK, zorder=4)
    if mwj_sec is not None:
        ax.axhline(mwj_sec, color=FULL_MWJ[2], linewidth=2, zorder=2)
        ax.annotate(f"{FULL_MWJ[1]} ({fmt_seconds(mwj_sec)})",
                    (x_lo, mwj_sec), xytext=(2, 5), textcoords="offset points",
                    ha="left", fontsize=7.5, color=INK, zorder=4)

    # Graphite series with per-point selectivity labels.
    if xs:
        ax.plot(xs, ys, marker="o", markersize=5, linewidth=2,
                color=GRAPHITE[2], markeredgecolor="white",
                markeredgewidth=0.8, zorder=3)
        ax.annotate(GRAPHITE[1], (xs[-1], ys[-1]), xytext=(6, 0),
                    textcoords="offset points", ha="left", va="center",
                    fontsize=7.5, color=INK, zorder=4)
        for x, y, s in zip(xs, ys, sels):
            ax.annotate(fmt_sel(s), (x, y), xytext=(0, -11),
                        textcoords="offset points", ha="center",
                        fontsize=6, color=MUTED, zorder=4)
    for x, y, s in invalid_pts:
        ax.plot([x], [y], marker="o", markersize=11, markerfacecolor="none",
                markeredgecolor=INVALID_RED, markeredgewidth=1.6, zorder=5)
        ax.annotate("INVALID", (x, y), xytext=(0, 8),
                    textcoords="offset points", ha="center", fontsize=6.5,
                    color=INVALID_RED, zorder=5)

    # Graphite failure markers (at the point's expected rows).
    for x, kind, s in fails:
        if kind == "TIMEOUT" and cell_timeout:
            b = float(cell_timeout)
            ax.plot([x], [b], marker="^", markersize=7, color="white",
                    markeredgecolor=GRAPHITE[2], markeredgewidth=1.6, zorder=4)
            ax.annotate(f"> {fmt_seconds(b)} ({fmt_sel(s)})", (x, b),
                        xytext=(0, 6), textcoords="offset points",
                        ha="center", fontsize=6.5, color=INK, zorder=4)
        else:
            ax.plot([x], [floor_s * 1.35], marker="x", markersize=7,
                    markeredgewidth=1.8, color=GRAPHITE[2], zorder=4)
            ax.annotate(f"{kind} ({fmt_sel(s)})", (x, floor_s * 1.35),
                        xytext=(0, 6), textcoords="offset points",
                        ha="center", fontsize=6.5, color=INK, zorder=4)

    # Full MWJ failure: identical at every theta -> band annotation at the top.
    if mwj_fail:
        y_band = max_val * 3
        ax.axhline(y_band, color=FULL_MWJ[2], linewidth=1.6,
                   linestyle=(0, (4, 3)), zorder=2)
        budget = f" > {fmt_seconds(float(cell_timeout))}" if (
            mwj_fail == "TIMEOUT" and cell_timeout) else ""
        ax.annotate(
            f"{FULL_MWJ[1]}: {mwj_fail}{budget} at every θ "
            f"(unfiltered join, {fmt_rows(int(full_mwj['expected_rows']))} rows)",
            (x_hi, y_band), xytext=(0, 5), textcoords="offset points",
            ha="right", fontsize=7.5, color=INK, zorder=4)
        max_val = y_band

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_ylim(floor_s, max_val * 6)
    tick_xs = sorted(set(xs + [f[0] for f in fails]))
    ax.set_xticks(tick_xs)
    ax.set_xticklabels([fmt_rows(x) for x in tick_xs], fontsize=7)
    ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    ax.set_xlabel("Measured filtered 2-hop output rows (log scale)",
                  fontsize=9, color=INK)
    ax.set_ylabel("Latency (s, log scale)", fontsize=9, color=INK)
    ax.set_title("(b) Latency vs filtered output size", fontsize=9.5,
                 color=INK)
    ax.legend(handles=[Line2D([], [], color=c, marker="o", markersize=5,
                              linewidth=2, markeredgecolor="white", label=n)
                       for _, n, c in SYSTEMS],
              fontsize=7.5, frameon=False, loc="upper left")

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
        out = summary_path.parent / f"e7_selectivity.{ext}"
        fig.savefig(out, bbox_inches="tight")
        print(f"wrote {out}")


if __name__ == "__main__":
    main()

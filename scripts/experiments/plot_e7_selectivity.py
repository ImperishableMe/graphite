#!/usr/bin/env python3
"""
E7 predicate-selectivity plot.

Reads the summary.csv written by run_e7_selectivity.py and renders two panels:

  (a) Setup — filtered 2-hop output rows per per-edge selectivity point
      (log scale bars): the predicate threshold theta is the only knob, and it
      sweeps the filtered output across five orders of magnitude on a fixed
      input.
  (b) Result — latency vs MEASURED filtered output rows, log-log, one swept
      line per system. Both systems apply the SAME filters, so the only
      differentiating factor is the decomposition: Graphite = decomposed
      one-hop + hop-table MWJ; Full MWJ = the same sgx_app engine run
      monolithically on the original 5-table query (filtered — a deliberate
      E7-only exception to the "Full MWJ = no-filter" presentation rule).

System presentation names per CLAUDE.md: nebuladb -> Graphite; full_mwj is
labeled "Full MWJ (filtered)" to distinguish it from the no-filter variant
used in other figures. Failed cells are drawn honestly (TIMEOUT = open marker
at the budget; OOM = x marker at the floor), never as a fake latency. Cells
whose measured row count contradicts the oracle (rows_match=0) are INVALID:
drawn with a red ring and label, and reported on stderr.

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

# Same categorical color assignment as the E1/E3/E5 figures (color follows
# the system across the paper; Full MWJ keeps its yellow even though this
# figure uses the filtered variant).
SYSTEMS = [
    ("nebuladb", "Graphite", "#2a78d6"),
    ("full_mwj", "Full MWJ (filtered)", "#eda100"),
]

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

    cell_timeout = None
    meta_path = summary_path.parent / "run_metadata.json"
    if meta_path.is_file():
        margs = json.loads(meta_path.read_text()).get("args", {})
        cell_timeout = margs.get("cell_timeout") or None

    by_system = {}
    for c in cells:
        by_system.setdefault(c["system"], []).append(c)
    for rows in by_system.values():
        rows.sort(key=lambda c: int(c["expected_rows"]))
    if "nebuladb" not in by_system:
        sys.exit("no nebuladb cells in summary")
    points = by_system["nebuladb"]  # panel (a) uses the oracle sweep itself

    fig, (ax_a, ax_b) = plt.subplots(
        1, 2, figsize=(9.6, 3.6), dpi=200, gridspec_kw={"width_ratios": [1, 1.35]})
    fig.patch.set_facecolor("white")

    # ------------------------------------------------------------------ (a)
    # The knob: per-edge selectivity -> filtered 2-hop output rows (oracle).
    ax = ax_a
    ax.set_facecolor("white")
    for i, c in enumerate(points):
        val = int(c["expected_rows"])
        ax.bar(i, val, width=0.62, color="#2a78d6", edgecolor="white",
               linewidth=0.6, zorder=3)
        ax.annotate(fmt_rows(val), (i, val), xytext=(0, 3),
                    textcoords="offset points", ha="center", va="bottom",
                    fontsize=7, color=INK, zorder=4)
    ax.set_yscale("log")
    ymax = max(int(c["expected_rows"]) for c in points)
    ax.set_ylim(1, ymax * 8)
    ax.set_ylabel("Filtered 2-hop output rows (log scale)", fontsize=9,
                  color=INK)
    ax.set_xticks(range(len(points)))
    ax.set_xticklabels([fmt_sel(float(c["s_target"])) for c in points],
                       fontsize=7.5, color=INK, rotation=30, ha="right")
    ax.set_title("(a) Same input, predicate sweeps the output",
                 fontsize=9.5, color=INK)

    # ------------------------------------------------------------------ (b)
    ax = ax_b
    ax.set_facecolor("white")
    max_val = 0.0

    series = {}
    for key, disp, color in SYSTEMS:
        xs, ys, sels, invalid_pts, fails = [], [], [], [], []
        for c in by_system.get(key, []):
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
                    invalid_pts.append((x, sec))
                    print(f"WARNING: INVALID cell {key} s={s}: measured {out} "
                          f"rows, expected {expected}", file=sys.stderr)
            elif out in ("TIMEOUT", "OOM"):
                fails.append((expected, out, s))
                if out == "TIMEOUT" and cell_timeout:
                    max_val = max(max_val, float(cell_timeout))
            # SKIPPED cells (consequence of an earlier failure) are not drawn.
        series[key] = (xs, ys, sels, invalid_pts, fails)

    completed = [y for xs, ys, *_ in series.values() for y in ys]
    floor_s = (10.0 ** (math.floor(math.log10(min(completed))) - 1)
               if completed else 1e-1)
    max_val = max(max_val, floor_s)
    all_x = ([x for xs, *_ in series.values() for x in xs]
             + [f[0] for *_, fails in series.values() for f in fails])
    x_lo = min(all_x or [1])

    # Slope-1 guide (cost proportional to filtered output), anchored to the
    # largest completed Graphite point.
    gxs, gys = series["nebuladb"][:2]
    if len(gxs) >= 2:
        x1, y1 = gxs[-1], gys[-1]
        ax.plot([x_lo, x1], [y1 * x_lo / x1, y1], linestyle=(0, (4, 3)),
                color=BASELINE, linewidth=1.2, zorder=1)
        xm = (x_lo * x1) ** 0.5
        ax.annotate("slope 1 (∝ filtered output)", (xm, y1 * xm / x1),
                    xytext=(6, -14), textcoords="offset points",
                    ha="left", fontsize=7, color=MUTED)

    for si, (key, disp, color) in enumerate(SYSTEMS):
        xs, ys, sels, invalid_pts, fails = series[key]
        if xs:
            ax.plot(xs, ys, marker="o", markersize=5, linewidth=2,
                    color=color, markeredgecolor="white",
                    markeredgewidth=0.8, zorder=3)
            ax.annotate(disp, (xs[-1], ys[-1]), xytext=(6, 0),
                        textcoords="offset points", ha="left", va="center",
                        fontsize=7.5, color=INK, zorder=4)
        if key == "nebuladb":
            # Selectivity labels once, under the Graphite series. The probe
            # points (0.55-0.7) sit too close together on the log axis to
            # label individually — annotate only the sparse canonical points
            # plus the topmost completed one.
            labeled = {0.001, 0.01, 0.05, 0.1, 0.25, 0.5}
            if sels:
                labeled.add(sels[-1])
            for x, y, s in zip(xs, ys, sels):
                if s in labeled:
                    ax.annotate(fmt_sel(s), (x, y), xytext=(0, -11),
                                textcoords="offset points", ha="center",
                                fontsize=6, color=MUTED, zorder=4)
        for x, y in invalid_pts:
            ax.plot([x], [y], marker="o", markersize=11,
                    markerfacecolor="none", markeredgecolor=INVALID_RED,
                    markeredgewidth=1.6, zorder=5)
            ax.annotate("INVALID", (x, y), xytext=(0, 8),
                        textcoords="offset points", ha="center",
                        fontsize=6.5, color=INVALID_RED, zorder=5)
        # Small per-system x-offset so coincident failure markers (both
        # systems failing on the same point) stay individually visible;
        # label heights stagger so neighboring failures don't overlap.
        jitter = (0.95, 1.06)[si]
        for fi, (x, kind, s) in enumerate(fails):
            y_off = 6 + 11 * ((2 * fi + si) % 3)
            if kind == "TIMEOUT" and cell_timeout:
                b = float(cell_timeout)
                ax.plot([x * jitter], [b], marker="^", markersize=7,
                        color="white", markeredgecolor=color,
                        markeredgewidth=1.6, zorder=4)
                ax.annotate(f"> {fmt_seconds(b)} ({fmt_sel(s)})",
                            (x * jitter, b), xytext=(0, y_off),
                            textcoords="offset points", ha="center",
                            fontsize=6.5, color=INK, zorder=4)
            else:
                ax.plot([x * jitter], [floor_s * 1.35], marker="x",
                        markersize=7, markeredgewidth=1.8, color=color,
                        zorder=4)
                ax.annotate(f"{kind} ({fmt_sel(s)})",
                            (x * jitter, floor_s * 1.35), xytext=(0, y_off),
                            textcoords="offset points", ha="center",
                            fontsize=6.5, color=color, zorder=4)

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_ylim(floor_s, max_val * 6)
    # One tick per point would crowd the dense top end of the sweep; keep a
    # tick only when it is at least ~0.2 decades from the previous one.
    tick_xs = []
    for x in sorted({int(c["expected_rows"]) for c in points}):
        if not tick_xs or math.log10(x / tick_xs[-1]) >= 0.2:
            tick_xs.append(x)
    ax.set_xticks(tick_xs)
    ax.set_xticklabels([fmt_rows(x) for x in tick_xs], fontsize=6.5,
                       rotation=40, ha="right")
    ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    ax.set_xlabel("Measured filtered 2-hop output rows (log scale)",
                  fontsize=9, color=INK)
    ax.set_ylabel("Latency (s, log scale)", fontsize=9, color=INK)
    ax.set_title("(b) Same filters — decomposition is the only difference",
                 fontsize=9.5, color=INK)
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

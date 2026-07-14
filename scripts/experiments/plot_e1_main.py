#!/usr/bin/env python3
"""
Plot an E1 main-result chain sweep (system comparison, one dataset).

Reads results/e1_main/<dataset>_chain_sweep/summary.csv (produced by
run_e1_main.py) and writes a grouped bar chart of end-to-end latency by chain
length (hops), one bar per system (NebulaDB, Full MWJ, ...). Linear y-axis,
seconds, with value labels.

By default only hops >= 2 are plotted: at 1-hop NebulaDB's "cell" is the
unfiltered hop-table materialization (its 1-hop join output), which is a
different quantity from the filtered full MWJ, so a 1-hop bar-to-bar comparison
would mislead. Pass --include-1hop to show it anyway.

Systems with no numeric cell at any plotted hop (e.g. an obliviator baseline
that OOM'd) are dropped from the bars and noted in a caption instead.

Usage:
  python3 scripts/experiments/plot_e1_main.py                       # dataset=snap_patents
  python3 scripts/experiments/plot_e1_main.py --dataset ibm_aml_hi_small
  python3 scripts/experiments/plot_e1_main.py --summary <path> --out <png>
"""

import argparse
import csv
import re
from pathlib import Path

import matplotlib.pyplot as plt

PROJECT_DIR = Path(__file__).resolve().parents[2]
E1_DIR = PROJECT_DIR / "results" / "e1_main"

# System -> (display label, color). Fixed order = draw/legend order; color
# follows the system, never its rank. Tableau-10 subset: blue/orange is the
# canonical CVD-safe pair, purple is distinct from both under deutan/protan.
# "Full MWJ" here is the *unfiltered* multiway join (full_mwj_no_filter): the
# honest full oblivious join with no filter pushdown. The filtered full_mwj is
# intentionally not plotted.
SYSTEMS = [
    ("nebuladb", "Graphite (one-hop + filtered MWJ)", "#4E79A7"),
    ("obliviator_chained", "Obliviator multiway", "#B07AA1"),
    ("full_mwj_no_filter", "Full MWJ", "#F28E2B"),
]

# Sentinels that appear in output_rows for a failed/skipped cell.
FAIL_TOKENS = {"OOM", "TIMEOUT", "SKIPPED"}


def hop_of(query):
    m = re.search(r"(\d+)hop", query)
    return int(m.group(1)) if m else None


def load_summary(path):
    """Return ({system: {hop: seconds}}, {system: {hop: fail_token}})."""
    times, status = {}, {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            sysname = r["system"]
            hop = hop_of(r["query"])
            val = (r.get("median_ms") or "").strip()
            rows = (r.get("output_rows") or "").strip()
            if val:
                try:
                    times.setdefault(sysname, {})[hop] = float(val) / 1000.0
                    continue
                except ValueError:
                    pass
            if rows in FAIL_TOKENS:
                status.setdefault(sysname, {})[hop] = rows
    return times, status


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default="snap_patents",
                    help="dataset name; reads results/e1_main/<dataset>_chain_sweep/"
                         "summary.csv (default: snap_patents)")
    ap.add_argument("--summary", type=Path, default=None,
                    help="explicit summary.csv path (overrides --dataset)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output PNG (default: alongside summary.csv, e1_<dataset>.png)")
    ap.add_argument("--include-1hop", action="store_true",
                    help="also plot 1-hop. NebulaDB's 1-hop is the unfiltered "
                         "hop-table build, which equals the unfiltered 'Full "
                         "MWJ' 1-hop result — a fair (and striking) comparison.")
    ap.add_argument("--title", default=None, help="override the chart title")
    args = ap.parse_args()

    summary = args.summary or (E1_DIR / f"{args.dataset}_chain_sweep" / "summary.csv")
    if not summary.exists():
        raise SystemExit(f"missing: {summary}")
    out_path = args.out or summary.parent / f"e1_{args.dataset}.png"

    times, status = load_summary(summary)
    if not times and not status:
        raise SystemExit("no cells in summary.csv")

    min_hop = 1 if args.include_1hop else 2

    def cells(s):  # all hops (numeric or failed) for a system, >= min_hop
        return {h for h in set(times.get(s, {})) | set(status.get(s, {}))
                if h >= min_hop}

    # A system gets a slot if it has ANY cell (numeric or failed) in range, so
    # a baseline that OOM'd everywhere still shows a reserved column of OOM marks.
    drawn = [(s, lbl, c) for (s, lbl, c) in SYSTEMS if cells(s)]
    hops = sorted({h for (s, _, _) in drawn for h in cells(s)})
    if not drawn or not hops:
        raise SystemExit("no cells to plot at the requested range")

    n_sets = len(drawn)
    bar_w = 0.8 / max(n_sets, 1)
    x = list(range(len(hops)))

    fig, ax = plt.subplots(figsize=(9.0, 5.2))

    # Scale to the drawn systems / plotted hops only — the summary may also hold
    # systems we don't plot (e.g. the filtered full_mwj), whose larger times
    # would otherwise inflate the axis and dwarf the bars we do show.
    ymax = max((times.get(s, {}).get(h, 0.0)
                for (s, _, _) in drawn for h in hops), default=1.0)

    for i, (sysname, label, color) in enumerate(drawn):
        offsets = [xi + (i - (n_sets - 1) / 2) * bar_w for xi in x]
        heights = [times.get(sysname, {}).get(h) for h in hops]
        # One bar() call per system so the legend gets exactly one handle
        # (zero-height where the cell is missing/failed).
        ax.bar(offsets, [h if h is not None else 0.0 for h in heights],
               width=bar_w, label=label, color=color, zorder=3)
        for xpos, h, hop in zip(offsets, heights, hops):
            if h is not None:
                ax.annotate(f"{h:.0f}", (xpos, h), textcoords="offset points",
                            xytext=(0, 3), ha="center", fontsize=8, color=color)
            else:
                tok = status.get(sysname, {}).get(hop)
                if tok:  # OOM / TIMEOUT / SKIPPED — mark the empty slot
                    ax.annotate(tok, (xpos, 0.0), textcoords="offset points",
                                xytext=(0, 4), ha="center", va="bottom",
                                fontsize=7.5, color=color, rotation=90,
                                fontweight="bold")

    ax.set_xticks(x)
    ax.set_xticklabels([f"{h}-hop" for h in hops])
    ax.set_xlabel("Chain length")
    ax.set_ylabel("End-to-end latency (s)")
    ax.set_ylim(0, ymax * 1.15)
    title = args.title or f"E1: system comparison on {args.dataset} — latency by chain length"
    ax.set_title(title)
    ax.grid(True, axis="y", alpha=0.3, zorder=0)
    ax.legend()

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"[done] wrote {out_path}")


if __name__ == "__main__":
    main()

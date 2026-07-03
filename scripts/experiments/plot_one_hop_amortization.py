#!/usr/bin/env python3
"""
Plot the one-hop build-once / serve-N amortization experiment (E3).

Reads results/one_hop_amortization/{amortization.csv, measured.csv} and emits
results/one_hop_amortization/amortization.png with two panels:

  (left)  amortized per-query cost vs N (log-x). Each dataset's curve falls from
          build_once + per_query (N=1) toward its per_query asymptote (dotted).
          The within-10% breakeven N is starred.
  (right) total serving cost build_once + N*per_query vs N (log-log).

build_once and per_query are MEASURED by the onehop binary's --serve mode
(physical index reuse), not projected. See run_one_hop_amortization.py.

Usage:
  python3 scripts/experiments/plot_one_hop_amortization.py
"""

import csv
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
RESULTS_DIR = PROJECT_DIR / "results" / "one_hop_amortization"


def load_csv(path: Path):
    with open(path) as f:
        return list(csv.DictReader(f))


def main():
    amort_path = RESULTS_DIR / "amortization.csv"
    meas_path = RESULTS_DIR / "measured.csv"
    if not amort_path.exists() or not meas_path.exists():
        sys.exit(f"missing results; run run_one_hop_amortization.py first ({RESULTS_DIR})")

    amort = load_csv(amort_path)
    measured = {m["dataset"]: m for m in load_csv(meas_path)}

    by_ds = {}
    for r in amort:
        by_ds.setdefault(r["dataset"], []).append(r)

    fig, (ax_amz, ax_tot) = plt.subplots(1, 2, figsize=(13, 5.2))
    cmap = plt.get_cmap("tab10")

    for i, (ds, rows) in enumerate(by_ds.items()):
        rows = sorted(rows, key=lambda r: int(r["n_queries"]))
        ns = [int(r["n_queries"]) for r in rows]
        amz = [float(r["amortized_per_query_ms"]) for r in rows]
        tot = [float(r["total_ms"]) for r in rows]
        per_query = float(measured[ds]["per_query_ms"])
        build_once = float(measured[ds]["build_once_ms"])
        be = int(float(measured[ds]["breakeven_n_within10pct"]))
        color = cmap(i)
        label = f"{ds} (build={build_once:.0f}ms, per-q={per_query:.0f}ms)"

        ax_amz.plot(ns, amz, "o-", color=color, label=label)
        ax_amz.axhline(per_query, color=color, ls=":", alpha=0.6)
        if min(ns) <= be <= max(ns):
            ax_amz.plot([be], [build_once / be + per_query], marker="*", color=color,
                        markersize=15, markeredgecolor="black", zorder=5)

        ax_tot.plot(ns, tot, "o-", color=color, label=ds)

    ax_amz.set_xscale("log")
    ax_amz.set_xlabel("number of one-hop queries  N  (log scale)")
    ax_amz.set_ylabel("amortized cost per query (ms)")
    ax_amz.set_title("Amortized per-query cost -> per_query asymptote\n"
                     "(* = within-10% breakeven; dotted = per_query floor)")
    ax_amz.grid(True, which="both", alpha=0.3)
    ax_amz.legend(fontsize=8)

    ax_tot.set_xscale("log")
    ax_tot.set_yscale("log")
    ax_tot.set_xlabel("number of one-hop queries  N  (log scale)")
    ax_tot.set_ylabel("total cost  build_once + N*per_query  (ms, log scale)")
    ax_tot.set_title("Total serving cost vs N")
    ax_tot.grid(True, which="both", alpha=0.3)
    ax_tot.legend(fontsize=8)

    fig.suptitle("One-hop build-once / serve-N amortization (E3, measured reuse)", fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    out_png = RESULTS_DIR / "amortization.png"
    fig.savefig(out_png, dpi=150)
    print(f"[plot] wrote {out_png}")


if __name__ == "__main__":
    main()

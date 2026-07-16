#!/usr/bin/env python3
"""
E7 predicate-selectivity sweep: calibration + query generation.

For each target per-edge selectivity s, computes:
  - theta: the integer amount threshold (amount quantile at 1-s) such that
    a fraction ~s of transactions satisfy `amount > theta`
  - s_achieved: the exact fraction of transactions passing (integer amounts
    have ties, so s_achieved differs slightly from the target)
  - expected_rows: the exact 2-hop output row count under the both-edge
    filter `t1.amount > theta AND t2.amount > theta`, computed as
    sum over accounts of filtered-in-degree x filtered-out-degree.
    This is the row-count oracle every measured Graphite run is checked
    against (self-loop edges count in both degrees, matching SQL semantics
    where t1 and t2 range independently).

Outputs:
  - <out-dir>/calibration.json — the oracle table consumed by the E7 runner
  - <queries-dir>/aml_2hop_sel_<s>.sql — one 2-hop no-anchor query per point
    (filtered points get the both-edge amount predicate; s=1.0 has no filter)

Usage:
  python3 scripts/experiments/calibrate_e7_selectivity.py \
      [--dataset input/plaintext/ibm_aml_hi_small] \
      [--out-dir results/e7_selectivity] \
      [--queries-dir input/queries]
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Target per-edge selectivities; 1.0 = unfiltered.
TARGET_SELECTIVITIES = [0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0]

QUERY_TEMPLATE_FILTERED = """\
SELECT * FROM account AS a1, txn AS t1, account AS a2, txn AS t2, account AS a3
WHERE a1.account_id = t1.acc_from
  AND a2.account_id = t1.acc_to
  AND a2.account_id = t2.acc_from
  AND a3.account_id = t2.acc_to
  AND t1.amount > {theta}
  AND t2.amount > {theta};
"""

QUERY_TEMPLATE_UNFILTERED = """\
SELECT * FROM account AS a1, txn AS t1, account AS a2, txn AS t2, account AS a3
WHERE a1.account_id = t1.acc_from
  AND a2.account_id = t1.acc_to
  AND a2.account_id = t2.acc_from
  AND a3.account_id = t2.acc_to;
"""


def sel_tag(s: float) -> str:
    """0.001 -> '0p001', 1.0 -> '1p000' (filename-safe selectivity label)."""
    return f"{s:.3f}".replace(".", "p")


def two_hop_rows(edges: pd.DataFrame) -> int:
    """Exact 2-hop path count: sum over accounts of indeg x outdeg."""
    indeg = edges.groupby("acc_to").size()
    outdeg = edges.groupby("acc_from").size()
    joined = pd.concat([indeg.rename("indeg"), outdeg.rename("outdeg")],
                       axis=1, join="inner")
    return int((joined["indeg"] * joined["outdeg"]).sum())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path,
                    default=PROJECT_ROOT / "input/plaintext/ibm_aml_hi_small")
    ap.add_argument("--out-dir", type=Path,
                    default=PROJECT_ROOT / "results/e7_selectivity")
    ap.add_argument("--queries-dir", type=Path,
                    default=PROJECT_ROOT / "input/queries")
    args = ap.parse_args()

    txn_csv = args.dataset / "txn.csv"
    print(f"Loading {txn_csv} ...", file=sys.stderr)
    txn = pd.read_csv(txn_csv, usecols=["acc_from", "acc_to", "amount"])
    n_txn = len(txn)
    print(f"  {n_txn} transactions", file=sys.stderr)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.queries_dir.mkdir(parents=True, exist_ok=True)

    points = []
    for s in TARGET_SELECTIVITIES:
        if s >= 1.0:
            theta = None
            passing = txn
        else:
            theta = int(txn["amount"].quantile(1.0 - s))
            passing = txn[txn["amount"] > theta]

        n_pass = len(passing)
        s_achieved = n_pass / n_txn
        expected = two_hop_rows(passing)

        tag = sel_tag(s)
        qname = f"aml_2hop_sel_{tag}.sql"
        qpath = args.queries_dir / qname
        if theta is None:
            qpath.write_text(QUERY_TEMPLATE_UNFILTERED)
        else:
            qpath.write_text(QUERY_TEMPLATE_FILTERED.format(theta=theta))

        points.append({
            "s_target": s,
            "s_achieved": s_achieved,
            "theta": theta,
            "passing_edges": n_pass,
            "expected_rows": expected,
            "query_file": qname,
        })
        theta_str = "-" if theta is None else str(theta)
        print(f"  s={s:<6} theta={theta_str:<12} passing={n_pass:<9} "
              f"s_achieved={s_achieved:.5f} expected_rows={expected}",
              file=sys.stderr)

    calib = {
        "dataset": str(args.dataset),
        "num_txns": n_txn,
        "query_shape": "2-hop chain, no anchor, amount > theta on both edges",
        "points": points,
    }
    calib_path = args.out_dir / "calibration.json"
    with open(calib_path, "w") as f:
        json.dump(calib, f, indent=2)
    print(f"Wrote {calib_path} and {len(points)} query files "
          f"to {args.queries_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
E7 double-oracle check: run the ORIGINAL 2-hop SQL through SQLite and compare
its COUNT(*) against the pandas oracle in calibration.json.

Two independent code paths (SQLite join vs pandas degree arithmetic) agreeing
on the expected row count is the precondition for trusting the oracle that
every measured Graphite run is checked against. Intended for the small points
only (s=0.001, s=0.01) — larger points make the SQLite join itself expensive.

Usage:
  python3 scripts/experiments/verify_e7_sqlite.py \
      [--dataset input/plaintext/ibm_aml_hi_small] \
      [--calibration results/e7_selectivity/calibration.json] \
      [--points 0.001 0.01]
"""

import argparse
import csv
import json
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_table(conn, name, csv_path, columns):
    cur = conn.cursor()
    col_defs = ", ".join(f"{c} INTEGER" for c in columns)
    cur.execute(f"CREATE TABLE {name} ({col_defs})")
    placeholders = ", ".join("?" for _ in columns)
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        rows = ([int(r[c]) for c in columns] for r in reader)
        cur.executemany(f"INSERT INTO {name} VALUES ({placeholders})", rows)
    conn.commit()


def count_query(sql: str) -> str:
    """Wrap SELECT * ... as SELECT COUNT(*) ... (counts only, per E7 scope)."""
    return "SELECT COUNT(*)" + sql.split("SELECT *", 1)[1].rstrip().rstrip(";")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path,
                    default=PROJECT_ROOT / "input/plaintext/ibm_aml_hi_small")
    ap.add_argument("--calibration", type=Path,
                    default=PROJECT_ROOT / "results/e7_selectivity/calibration.json")
    ap.add_argument("--points", type=float, nargs="+", default=[0.001, 0.01])
    args = ap.parse_args()

    with open(args.calibration) as f:
        calib = json.load(f)
    points = [p for p in calib["points"] if p["s_target"] in args.points]
    if len(points) != len(args.points):
        raise SystemExit(f"Points {args.points} not all present in {args.calibration}")

    print("Loading dataset into in-memory SQLite ...", file=sys.stderr)
    conn = sqlite3.connect(":memory:")
    load_table(conn, "account", args.dataset / "account.csv",
               ["account_id", "bank_id"])
    load_table(conn, "txn", args.dataset / "txn.csv",
               ["txn_id", "acc_from", "acc_to", "amount"])
    cur = conn.cursor()
    cur.execute("CREATE INDEX idx_acc ON account(account_id)")
    cur.execute("CREATE INDEX idx_from ON txn(acc_from)")
    cur.execute("CREATE INDEX idx_to ON txn(acc_to)")
    conn.commit()

    all_ok = True
    for p in points:
        qpath = PROJECT_ROOT / "input/queries" / p["query_file"]
        sql = count_query(qpath.read_text())
        t0 = time.time()
        (n,) = cur.execute(sql).fetchone()
        dt = time.time() - t0
        ok = (n == p["expected_rows"])
        all_ok &= ok
        status = "MATCH" if ok else "MISMATCH"
        print(f"s={p['s_target']}: sqlite={n} oracle={p['expected_rows']} "
              f"[{status}] ({dt:.1f}s)")

    if not all_ok:
        raise SystemExit("ORACLE VERIFICATION FAILED")
    print("Double-oracle check passed: SQLite agrees with pandas oracle.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
E7 predicate-selectivity runner: fixed input (ibm_aml_hi_small), fixed 2-hop
no-anchor chain, varying per-edge predicate selectivity.

Complement of E5: E5 varied the UNFILTERED output at fixed input; E7 fixes
input and structure and varies the FILTERED output via
`t1.amount > theta AND t2.amount > theta` (both edges, so the output sweeps
~quadratically with the per-edge selectivity).

The experiment isolates the impact of DECOMPOSITION. Both systems run the
same query with the same filters pushed down; the only differentiating
factor is decomposed one-hop + hop-table MWJ vs monolithic MWJ:

  1. Graphite  (runner key `nebuladb`)  decomposed: one-hop -> rewritten
     hop query through sgx_app
  2. Full MWJ  (runner key `full_mwj`)  monolithic: sgx_app on the original
     5-table query, filters applied (NO --no-filter). Deliberate E7-only
     exception to the "Full MWJ = no-filter" presentation rule, by explicit
     decision 2026-07-16: an unfiltered or filter-less baseline would be
     bounded by the 355M-row unfiltered join (OOM) at every point and shows
     nothing about decomposition. (Obliviator chained was dropped from E7
     for the same reason: it has no filter support at all.)

Row-count verification is first-class: every cell of BOTH systems is checked
against the pandas oracle in calibration.json (produced by
calibrate_e7_selectivity.py, double-checked against SQLite on the small
points by verify_e7_sqlite.py). A mismatch marks the cell INVALID — loudly
in stdout and via rows_match=0 in the CSVs — and is never silently averaged.

Points run smallest expected output first. Once a system fails (OOM/TIMEOUT)
at a point, larger points in the same rep and the same point in later reps
are recorded SKIPPED for that system rather than re-failed.

Usage:
  python3 scripts/experiments/run_e7_selectivity.py
  python3 scripts/experiments/run_e7_selectivity.py --points 0.001,0.01 --skip-build
  python3 scripts/experiments/run_e7_selectivity.py --systems nebuladb
"""

import argparse
import csv
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_e3_cross_dataset as e3

PROJECT_DIR = e3.PROJECT_DIR
QUERY_DIR = e3.QUERY_DIR
SGX_APP = e3.SGX_APP

AML = e3.WORKLOADS["aml"]
# E7 compares decomposed vs monolithic with IDENTICAL filters (see module
# docstring): full_mwj here is the filtered variant, not full_mwj_no_filter.
ALL_SYSTEMS = ["nebuladb", "full_mwj"]
K = 2  # E7 is a 2-hop experiment by design; the oracle is 2-hop.

RAW_FIELDS = ["system", "s_target", "s_achieved", "theta", "query",
              "passing_edges", "expected_rows", "run_id", "is_warmup",
              "total_ms", "onehop_ms", "mwj_ms", "output_rows", "rows_match"]


def load_calibration(path: Path) -> dict:
    with open(path) as f:
        calib = json.load(f)
    # Smallest expected output first: cheap cells surface failures early and
    # a Graphite OOM at one point lets us skip everything larger.
    calib["points"].sort(key=lambda p: p["expected_rows"])
    return calib


def write_raw_csv(rows, path: Path):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RAW_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_summary_csv(rows, path: Path):
    by_cell = {}
    for r in rows:
        if r["is_warmup"]:
            continue
        by_cell.setdefault((r["system"], r["s_target"]), []).append(r)
    fields = ["system", "s_target", "s_achieved", "theta", "query",
              "passing_edges", "expected_rows", "n_runs", "median_ms",
              "min_ms", "max_ms", "stddev_ms", "output_rows", "rows_match"]
    with open(path, "w", newline="") as f:
        sw = csv.DictWriter(f, fieldnames=fields)
        sw.writeheader()
        for (system, s_target), cell in sorted(by_cell.items()):
            base = {k: cell[0][k] for k in
                    ["system", "s_target", "s_achieved", "theta", "query",
                     "passing_edges", "expected_rows", "output_rows",
                     "rows_match"]}
            totals = [c["total_ms"] for c in cell if c["total_ms"] is not None]
            if not totals:
                sw.writerow({**base, "n_runs": 0, "median_ms": "", "min_ms": "",
                             "max_ms": "", "stddev_ms": ""})
                continue
            sw.writerow({**base, "n_runs": len(totals),
                         "median_ms": statistics.median(totals),
                         "min_ms": min(totals), "max_ms": max(totals),
                         "stddev_ms": (statistics.stdev(totals)
                                       if len(totals) >= 2 else 0.0)})


def collect_metadata(args, calib) -> dict:
    def sh(cmd):
        try:
            return subprocess.check_output(cmd, cwd=PROJECT_DIR, text=True).strip()
        except Exception:
            return ""
    return {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git_commit": sh(["git", "rev-parse", "HEAD"]),
        "git_branch": sh(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "hostname": platform.node(),
        "nproc": os.cpu_count(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "args": vars(args),
        "calibration_dataset": calib["dataset"],
        "note": (
            "E7 predicate-selectivity sweep: fixed ibm_aml_hi_small input, "
            "2-hop no-anchor chain, amount > theta on BOTH edges at 7 "
            "quantile thresholds (per-edge selectivity 0.001..1.0). Isolates "
            "the impact of decomposition: BOTH systems apply the same "
            "filters — Graphite via the decomposed one-hop + rewritten hop "
            "query, Full MWJ (filtered; E7-only exception to the no-filter "
            "presentation rule, decided 2026-07-16) via sgx_app on the "
            "original query. Obliviator chained is excluded (no filter "
            "support). Expected row counts come from the pandas oracle in "
            "calibration.json (SQLite-verified on the small points) and are "
            "cross-checked against every cell of both systems (rows_match; "
            "a 0 marks the cell INVALID). Graphite latency = onehop_ms + "
            "mwj_ms of the same rep; the hop table is filter-independent "
            "and built once per rep."
        ),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--points", default=None,
                   help="Comma-separated s_target values to run "
                        "(default: all points in calibration.json)")
    p.add_argument("--systems", default=",".join(ALL_SYSTEMS),
                   help=f"Comma-separated systems (default and allowed: "
                        f"{','.join(ALL_SYSTEMS)})")
    p.add_argument("--measurement-runs", type=int, default=3,
                   help="Recorded measurement runs per cell (default: 3)")
    p.add_argument("--warmup-runs", type=int, default=1,
                   help="Discarded warm-up runs per cell (default: 1)")
    p.add_argument("--onehop-threads", type=int, default=64,
                   help="Threads for the one-hop driver (default: 64)")
    p.add_argument("--mwj-threads", type=int, default=64,
                   help="OBL_MWJ_SORT_THREADS for sgx_app (default: 64; 0 = unset)")
    p.add_argument("--cell-timeout", type=int, default=7200,
                   help="Per-cell budget in seconds for MWJ cells "
                        "(default: 7200; 0 = no limit)")
    p.add_argument("--data-root", default=str(PROJECT_DIR / "input" / "plaintext"),
                   help="Directory holding ibm_aml_hi_small (default: "
                        "<project>/input/plaintext)")
    p.add_argument("--output-dir",
                   default=str(PROJECT_DIR / "results" / "e7_selectivity"),
                   help="Output directory (default: <project>/results/"
                        "e7_selectivity; must hold calibration.json)")
    p.add_argument("--skip-build", action="store_true",
                   help="Skip the binary rebuild step")
    args = p.parse_args()

    out_dir = Path(args.output_dir)
    calib_path = out_dir / "calibration.json"
    if not calib_path.is_file():
        sys.exit(f"calibration.json not found in {out_dir} — run "
                 f"scripts/experiments/calibrate_e7_selectivity.py first")
    calib = load_calibration(calib_path)

    points = calib["points"]
    if args.points:
        wanted = {float(s) for s in args.points.split(",") if s.strip()}
        known = {pt["s_target"] for pt in points}
        unknown = wanted - known
        if unknown:
            sys.exit(f"unknown points: {sorted(unknown)} (allowed: {sorted(known)})")
        points = [pt for pt in points if pt["s_target"] in wanted]

    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    for s in systems:
        if s not in ALL_SYSTEMS:
            sys.exit(f"unknown system: {s} (allowed: {ALL_SYSTEMS})")

    cell_timeout = args.cell_timeout or None
    data_dir = Path(args.data_root) / "ibm_aml_hi_small"
    if not (data_dir / "txn.csv").is_file():
        sys.exit(f"dataset not found: {data_dir}")

    needs_onehop = "nebuladb" in systems

    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "binary_stdout.log"
    raw_csv_path = out_dir / "raw_runs.csv"
    summary_csv_path = out_dir / "summary.csv"
    meta_path = out_dir / "run_metadata.json"
    meta = collect_metadata(args, calib)

    print(f"E7 selectivity runner: hop={K} (fixed), dataset={data_dir.name}")
    print(f"  points  : {[pt['s_target'] for pt in points]}")
    print(f"  systems : {systems} (full_mwj = FILTERED monolithic MWJ)")
    print(f"  output  : {out_dir}")
    print(f"  timeout : cell={cell_timeout}s")
    print()

    rows = []
    total_reps = args.warmup_runs + args.measurement_runs

    def record(system, pt, run_id, is_warmup, total_ms, onehop_ms, mwj_ms,
               output_rows, rows_match):
        rows.append({
            "system": system,
            "s_target": pt["s_target"] if pt else "",
            "s_achieved": pt["s_achieved"] if pt else "",
            "theta": (pt["theta"] if pt and pt["theta"] is not None else ""),
            "query": pt["query_file"] if pt else "",
            "passing_edges": pt["passing_edges"] if pt else "",
            "expected_rows": pt["expected_rows"] if pt else "",
            "run_id": run_id,
            "is_warmup": int(is_warmup),
            "total_ms": total_ms,
            "onehop_ms": onehop_ms,
            "mwj_ms": mwj_ms,
            "output_rows": output_rows,
            "rows_match": rows_match,
        })
        write_raw_csv(rows, raw_csv_path)
        write_summary_csv(rows, summary_csv_path)

    def check_rows(system, pt, cell_rows, expected, log_file):
        """Returns rows_match ('' when not applicable). Mismatch is loud."""
        if not isinstance(cell_rows, int):
            return ""
        match = int(cell_rows == expected)
        if not match:
            msg = (f"!!! ROW-COUNT MISMATCH — CELL INVALID: {system} "
                   f"s={pt['s_target'] if pt else '-'}: got {cell_rows}, "
                   f"expected {expected}")
            print(f"  {msg}", flush=True)
            log_file.write(f"\n{msg}\n")
            log_file.flush()
        return match

    with open(log_path, "w") as log_file:
        if not args.skip_build:
            e3.build_binaries({AML["onehop_target"]}, need_slim=False,
                              need_obliviator=False, log_file=log_file)
        else:
            required = [(SGX_APP, "sgx_app")]
            if needs_onehop:
                required.append((AML["onehop_bin"], AML["onehop_target"]))
            for bin_path, lbl in required:
                if not bin_path.exists():
                    sys.exit(f"--skip-build but {lbl} missing: {bin_path}")

        # Rewrite every point's query once (cached under decomposed/).
        decomposed_dir = out_dir / "decomposed"
        decomposed_dir.mkdir(exist_ok=True)
        rewritten = {}
        if needs_onehop:
            for pt in points:
                src = QUERY_DIR / pt["query_file"]
                if not src.is_file():
                    sys.exit(f"query not found: {src} — re-run calibration")
                dst = decomposed_dir / pt["query_file"].replace(
                    ".sql", "_rewritten.sql")
                e3.run_rewrite(src, dst, log_file)
                rewritten[pt["s_target"]] = dst

        with tempfile.TemporaryDirectory(dir=out_dir, prefix="_tmp_") as tmp_root:
            tmp_root = Path(tmp_root)
            hop_dir = tmp_root / "hop"
            hop_dir.mkdir()

            # Per-system failure ratchet: once a system fails at a point,
            # skip everything with an expected output at least as large
            # (all reps).
            failed_at_rows = {system: None for system in systems}

            for rep_idx in range(total_reps):
                is_warmup = rep_idx < args.warmup_runs
                run_id = rep_idx - args.warmup_runs + 1
                label = "warm" if is_warmup else f"run{run_id}"
                print(f"--- rep {rep_idx+1}/{total_reps} ({label}) ---", flush=True)

                onehop_ms, onehop_rows = (None, None)
                if needs_onehop:
                    print("  one-hop ...", end="", flush=True)
                    t0 = time.time()
                    onehop_ms, onehop_rows = e3.run_onehop(
                        AML["onehop_bin"], data_dir, hop_dir,
                        args.onehop_threads, log_file)
                    print(f" total={onehop_ms:.1f}ms rows={onehop_rows} "
                          f"({time.time()-t0:.1f}s wall)", flush=True)

                for pt in points:
                    for system in systems:
                        if (failed_at_rows[system] is not None
                                and pt["expected_rows"] >= failed_at_rows[system]):
                            print(f"  [s={pt['s_target']}] {system} {label} "
                                  f"-> SKIPPED (failed at "
                                  f"{failed_at_rows[system]:,} expected rows)",
                                  flush=True)
                            record(system, pt, run_id, is_warmup, None,
                                   "", "", "SKIPPED", "")
                            continue
                        cell_total_ms = None
                        cell_onehop_ms = ""
                        cell_mwj_ms = ""
                        try:
                            if system == "nebuladb":
                                # Decomposed: rewritten hop query over the
                                # per-rep hop table.
                                mwj_ms, mwj_rows = e3.run_mwj(
                                    rewritten[pt["s_target"]], hop_dir,
                                    args.mwj_threads, log_file,
                                    timeout=cell_timeout)
                                cell_total_ms = onehop_ms + mwj_ms
                                cell_onehop_ms = onehop_ms
                            else:  # full_mwj
                                # Monolithic: the ORIGINAL query with its
                                # filters on the raw tables (no --no-filter).
                                mwj_ms, mwj_rows = e3.run_mwj(
                                    QUERY_DIR / pt["query_file"], data_dir,
                                    args.mwj_threads, log_file,
                                    timeout=cell_timeout)
                                cell_total_ms = mwj_ms
                            cell_mwj_ms = mwj_ms
                            cell_rows = mwj_rows
                        except RuntimeError as e:
                            kind = ("TIMEOUT" if isinstance(e, e3.CellTimeout)
                                    else "OOM")
                            log_file.write(f"\n!!! {system} {kind} at "
                                           f"s={pt['s_target']}: {e}\n")
                            log_file.flush()
                            cell_rows = kind
                            failed_at_rows[system] = pt["expected_rows"]
                        rows_match = check_rows(system, pt, cell_rows,
                                                pt["expected_rows"], log_file)
                        total_str = (f"{cell_total_ms:.1f}ms"
                                     if cell_total_ms is not None
                                     else str(cell_rows))
                        print(f"  [s={pt['s_target']}] {system} {label} -> "
                              f"total={total_str} rows={cell_rows} "
                              f"match={rows_match}", flush=True)
                        record(system, pt, run_id, is_warmup,
                               cell_total_ms, cell_onehop_ms, cell_mwj_ms,
                               cell_rows, rows_match)

    write_raw_csv(rows, raw_csv_path)
    write_summary_csv(rows, summary_csv_path)

    meta["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    print()
    print(f"raw runs    -> {raw_csv_path}")
    print(f"summary     -> {summary_csv_path}")
    print(f"metadata    -> {meta_path}")
    print(f"stdout log  -> {log_path}")


if __name__ == "__main__":
    main()

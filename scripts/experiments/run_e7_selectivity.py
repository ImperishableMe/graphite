#!/usr/bin/env python3
"""
E7 predicate-selectivity runner: fixed input (ibm_aml_hi_small), fixed 2-hop
no-anchor chain, varying per-edge predicate selectivity.

Complement of E5: E5 varied the UNFILTERED output at fixed input (baselines
blow up, Graphite flat); E7 fixes input and structure and varies the FILTERED
output via `t1.amount > theta AND t2.amount > theta` (both edges, so the
output sweeps ~quadratically with the per-edge selectivity):

  1. Graphite           (runner key `nebuladb`)           cost ~ filtered output
  2. Obliviator chained (runner key `obliviator_chained`) no filter support ->
     always computes the unfiltered chain; theta-invariant, run once per rep
  3. Full MWJ           (runner key `full_mwj_no_filter`) --no-filter strips
     the predicate, so its execution is IDENTICAL at every theta -> attempted
     exactly once per invocation (expected OOM at the 355M-row output)

Row-count verification is first-class: every Graphite cell's measured output
rows are checked against the pandas oracle in calibration.json (produced by
calibrate_e7_selectivity.py, double-checked against SQLite on the small
points by verify_e7_sqlite.py). A mismatch marks the cell INVALID — loudly in
stdout and via rows_match=0 in the CSVs — and is never silently averaged.
Obliviator is exempt (perf-only: its converter collapses multi-edges, so it
reports a different unfiltered row count by design). Full MWJ, if it ever
completes, is checked against the unfiltered oracle count.

Points run smallest expected output first. Once Graphite fails (OOM/TIMEOUT)
at a point, larger points in the same rep and the same point in later reps
are recorded SKIPPED rather than re-failed.

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
ALL_SYSTEMS = e3.ALL_SYSTEMS
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
            "quantile thresholds (per-edge selectivity 0.001..1.0). Expected "
            "row counts come from the pandas oracle in calibration.json "
            "(SQLite-verified on the small points) and are cross-checked "
            "against every Graphite cell (rows_match; a 0 marks the cell "
            "INVALID). Obliviator has no filter support -> theta-invariant, "
            "run once per rep, exempt from rows_match (multi-edge collapse). "
            "Full MWJ runs --no-filter, identical at every theta -> attempted "
            "once per invocation. Graphite latency = onehop_ms + mwj_ms of "
            "the same rep; the hop table is filter-independent and built "
            "once per rep."
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
    p.add_argument("--obliviator-threads", type=int, default=64,
                   help="Threads for obliviator_khop_chained (default: 64)")
    p.add_argument("--cell-timeout", type=int, default=7200,
                   help="Per-cell budget in seconds for MWJ cells "
                        "(default: 7200; 0 = no limit)")
    p.add_argument("--obliviator-timeout", type=int, default=3600,
                   help="Per-cell budget for obliviator cells (default: 3600; "
                        "0 = no limit)")
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
    obl_timeout = args.obliviator_timeout or None
    data_dir = Path(args.data_root) / "ibm_aml_hi_small"
    if not (data_dir / "txn.csv").is_file():
        sys.exit(f"dataset not found: {data_dir}")

    unfiltered_pt = next((pt for pt in calib["points"] if pt["s_target"] >= 1.0),
                         None)

    needs_onehop = "nebuladb" in systems
    needs_obliviator = "obliviator_chained" in systems

    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "binary_stdout.log"
    raw_csv_path = out_dir / "raw_runs.csv"
    summary_csv_path = out_dir / "summary.csv"
    meta_path = out_dir / "run_metadata.json"
    meta = collect_metadata(args, calib)

    print(f"E7 selectivity runner: hop={K} (fixed), dataset={data_dir.name}")
    print(f"  points  : {[pt['s_target'] for pt in points]}")
    print(f"  systems : {systems}")
    print(f"  output  : {out_dir}")
    print(f"  timeouts: cell={cell_timeout}s obliviator={obl_timeout}s")
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
        if system == "obliviator_chained":
            return ""  # perf-only: converter collapses multi-edges by design
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
                              need_obliviator=needs_obliviator, log_file=log_file)
        else:
            required = [(SGX_APP, "sgx_app")]
            if needs_onehop:
                required.append((AML["onehop_bin"], AML["onehop_target"]))
            if needs_obliviator:
                required.append((e3.OBL_KHOP_BIN, "obliviator_khop_chained"))
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

            obl_src_txt = None
            if needs_obliviator:
                obl_dir = tmp_root / "obl"
                obl_dir.mkdir()
                print("[setup] obliviator src.txt ...", flush=True)
                t0 = time.time()
                obl_src_txt = e3.generate_obliviator_src_txt(
                    AML["obl_converter"], data_dir, obl_dir, log_file)
                print(f"  -> done ({time.time()-t0:.1f}s wall)")

            hop_dir = tmp_root / "hop"
            hop_dir.mkdir()

            # Graphite failure ratchet: once a point fails, skip everything
            # with an expected output at least as large (all reps).
            failed_at_rows = None

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

                if needs_onehop:
                    for pt in points:
                        if (failed_at_rows is not None
                                and pt["expected_rows"] >= failed_at_rows):
                            print(f"  [s={pt['s_target']}] nebuladb {label} "
                                  f"-> SKIPPED (failed at "
                                  f"{failed_at_rows:,} expected rows)",
                                  flush=True)
                            record("nebuladb", pt, run_id, is_warmup, None,
                                   "", "", "SKIPPED", "")
                            continue
                        cell_total_ms = None
                        cell_mwj_ms = ""
                        try:
                            mwj_ms, mwj_rows = e3.run_mwj(
                                rewritten[pt["s_target"]], hop_dir,
                                args.mwj_threads, log_file,
                                timeout=cell_timeout)
                            cell_total_ms = onehop_ms + mwj_ms
                            cell_mwj_ms = mwj_ms
                            cell_rows = mwj_rows
                        except RuntimeError as e:
                            kind = ("TIMEOUT" if isinstance(e, e3.CellTimeout)
                                    else "OOM")
                            log_file.write(f"\n!!! nebuladb {kind} at "
                                           f"s={pt['s_target']}: {e}\n")
                            log_file.flush()
                            cell_rows = kind
                            failed_at_rows = pt["expected_rows"]
                        rows_match = check_rows("nebuladb", pt, cell_rows,
                                                pt["expected_rows"], log_file)
                        total_str = (f"{cell_total_ms:.1f}ms"
                                     if cell_total_ms is not None
                                     else str(cell_rows))
                        print(f"  [s={pt['s_target']}] nebuladb {label} -> "
                              f"total={total_str} rows={cell_rows} "
                              f"match={rows_match}", flush=True)
                        record("nebuladb", pt, run_id, is_warmup,
                               cell_total_ms, onehop_ms, cell_mwj_ms,
                               cell_rows, rows_match)

                if needs_obliviator:
                    cell_total_ms = None
                    try:
                        obl_ms, obl_rows = e3.run_obliviator_khop(
                            K, obl_src_txt, args.obliviator_threads,
                            AML["obl_shape"], log_file, timeout=obl_timeout)
                        cell_total_ms = obl_ms
                        cell_rows = obl_rows
                    except RuntimeError as e:
                        kind = ("TIMEOUT" if isinstance(e, e3.CellTimeout)
                                else "OOM")
                        log_file.write(f"\n!!! obliviator_chained {kind}: {e}\n")
                        log_file.flush()
                        cell_rows = kind
                    total_str = (f"{cell_total_ms:.1f}ms"
                                 if cell_total_ms is not None
                                 else str(cell_rows))
                    print(f"  [theta-invariant] obliviator_chained {label} -> "
                          f"total={total_str} rows={cell_rows}", flush=True)
                    record("obliviator_chained", unfiltered_pt, run_id,
                           is_warmup, cell_total_ms, "", "", cell_rows, "")

            # Full MWJ: --no-filter strips the predicate, so its execution is
            # identical at every theta. One attempt for the whole invocation,
            # after the sweep (expected OOM at the 355M-row output).
            if "full_mwj_no_filter" in systems:
                if unfiltered_pt is None:
                    sys.exit("full_mwj_no_filter needs the s=1.0 point in "
                             "calibration.json")
                base_sql = QUERY_DIR / unfiltered_pt["query_file"]
                print("--- full_mwj_no_filter (single attempt; identical at "
                      "every theta) ---", flush=True)
                cell_total_ms = None
                cell_mwj_ms = ""
                try:
                    mwj_ms, mwj_rows = e3.run_mwj(
                        base_sql, data_dir, args.mwj_threads, log_file,
                        no_filter=True, timeout=cell_timeout)
                    cell_total_ms = mwj_ms
                    cell_mwj_ms = mwj_ms
                    cell_rows = mwj_rows
                except RuntimeError as e:
                    kind = ("TIMEOUT" if isinstance(e, e3.CellTimeout)
                            else "OOM")
                    log_file.write(f"\n!!! full_mwj_no_filter {kind}: {e}\n")
                    log_file.flush()
                    cell_rows = kind
                rows_match = check_rows("full_mwj_no_filter", unfiltered_pt,
                                        cell_rows,
                                        unfiltered_pt["expected_rows"],
                                        log_file)
                total_str = (f"{cell_total_ms:.1f}ms"
                             if cell_total_ms is not None else str(cell_rows))
                print(f"  full_mwj_no_filter -> total={total_str} "
                      f"rows={cell_rows} match={rows_match}", flush=True)
                record("full_mwj_no_filter", unfiltered_pt, 1, False,
                       cell_total_ms, "", cell_mwj_ms, cell_rows, rows_match)

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

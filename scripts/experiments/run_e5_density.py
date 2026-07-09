#!/usr/bin/env python3
"""
E5 output-sensitivity runner: fixed input size, varying density, one 2-hop query.

Design: docs/e5_output_sensitivity.md. Four Banking W1 variants with identical
row counts (200k accounts / 1M txns, seed 42) whose degree concentration — and
therefore unfiltered 2-hop output size — varies via the generator's hub knob
(--hub-fraction p, 1000 hubs). The anchored/filtered output stays small, so
the sweep isolates output-size sensitivity:

  1. Graphite            (runner key `nebuladb`)      cost ~ input + filtered output
  2. Obliviator chained  (runner key `obliviator_chained`)  cost ~ unfiltered output
  3. Full MWJ            (runner key `full_mwj_no_filter`)  cost ~ unfiltered output

The query is the fixed 2-hop banking chain (`banking_2hop.sql`, anchor 46 —
the generator plants account 46 with out-degree 5). Each variant's generator
emits stats.json with the exact unfiltered and filtered 2-hop row counts;
these are carried into the CSVs as ground truth and cross-checked against
every cell's output_rows.

Machinery (subprocess helpers, stdout parsers, stage runners, build step,
per-cell OOM/TIMEOUT capture) is imported from run_e3_cross_dataset.

Usage:
  python3 scripts/experiments/run_e5_density.py
  python3 scripts/experiments/run_e5_density.py --datasets smoke --skip-build
  python3 scripts/experiments/run_e5_density.py --systems nebuladb,obliviator_chained
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
GENERATOR = e3.GENERATOR
QUERY_DIR = e3.QUERY_DIR
SGX_APP = e3.SGX_APP

SEED = 42
ANCHOR = 46           # matches the a1.account_id literal in banking_2hop.sql
ANCHOR_OUT_DEGREE = 5
EDGE_RATIO = 5

BANKING = e3.WORKLOADS["banking"]

# Equal-input density variants (docs/e5_output_sensitivity.md), in two size
# tiers selected via --tier. The hub count scales with the edge count (hub
# term ~ p^2 M^2 / H), keeping the unfiltered-output spread of the p sweep the
# same relative size in both tiers. `smoke` (1M tier) is a 10k-account
# smoke-test entry, excluded from the default sweep.
TIERS = {
    "1M": {  # 200k accounts / 1M txns per variant
        "accounts": 200_000, "hub_count": 1_000,
        "dir_prefix": "banking_e5",
        "default_output_dir": "e5_density",
    },
    "5M": {  # 1M accounts / 5M txns per variant
        "accounts": 1_000_000, "hub_count": 5_000,
        "dir_prefix": "banking_e5_5M",
        "default_output_dir": "e5_density_5M",
    },
    "10M": {  # 2M accounts / 10M txns per variant
        "accounts": 2_000_000, "hub_count": 10_000,
        "dir_prefix": "banking_e5_10M",
        "default_output_dir": "e5_density_10M",
    },
}

VARIANTS = [
    ("uniform", 0.00),
    ("low", 0.10),
    ("medium", 0.20),
    ("high", 0.35),
]


def tier_datasets(tier_name):
    tier = TIERS[tier_name]
    datasets = []
    if tier_name == "1M":
        datasets.append({"label": "smoke", "dir_name": "banking_e5_smoke",
                         "accounts": 10_000, "hub_count": 1_000,
                         "hub_fraction": 0.20, "default": False})
    for label, p in VARIANTS:
        datasets.append({"label": label,
                         "dir_name": f"{tier['dir_prefix']}_{label}",
                         "accounts": tier["accounts"],
                         "hub_count": tier["hub_count"],
                         "hub_fraction": p})
    return datasets

ALL_SYSTEMS = e3.ALL_SYSTEMS
K = 2  # E5 is a 2-hop experiment by design; stats.json ground truth is 2-hop.


# ---------------------------------------------------------------------------
# Dataset generation / validation
# ---------------------------------------------------------------------------

def e5_dataset_is_valid(data_dir: Path, spec) -> bool:
    """Row counts + stats.json parameters must match the variant spec."""
    stats_path = data_dir / "stats.json"
    if not stats_path.is_file():
        return False
    try:
        with open(stats_path) as f:
            stats = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    if (stats.get("num_accounts") != spec["accounts"]
            or stats.get("hub_fraction") != spec["hub_fraction"]
            or stats.get("hub_count") != spec["hub_count"]
            or stats.get("anchor") != ANCHOR
            or stats.get("seed") != SEED):
        return False
    return e3.banking_dataset_is_valid(data_dir, spec["accounts"])


def generate_e5_dataset(spec, data_root: Path, log_file):
    import shutil
    data_dir = data_root / spec["dir_name"]
    if e5_dataset_is_valid(data_dir, spec):
        print(f"[data] {spec['dir_name']} present with expected parameters")
        return
    if data_dir.exists():
        print(f"[data] {spec['dir_name']} stale/wrong parameters -> regenerating")
        shutil.rmtree(data_dir)
    print(f"[data] generating {spec['dir_name']} ({spec['accounts']:,} accounts, "
          f"p={spec['hub_fraction']}, seed {SEED})...", flush=True)
    t0 = time.time()
    e3.run_capture(
        ["python3", GENERATOR, str(spec["accounts"]), data_dir,
         "--seed", str(SEED), "--quiet",
         "--plant-anchor", str(ANCHOR),
         "--anchor-out-degree", str(ANCHOR_OUT_DEGREE),
         "--hub-fraction", str(spec["hub_fraction"]),
         "--hub-count", str(spec["hub_count"])],
        log_file=log_file,
    )
    print(f"  -> done ({time.time()-t0:.1f}s)")


def load_ground_truth(data_dir: Path) -> dict:
    with open(data_dir / "stats.json") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Output writers (flushed after every cell, like E3)
# ---------------------------------------------------------------------------

RAW_FIELDS = ["system", "dataset", "hub_fraction", "query", "accounts", "edges",
              "unfiltered_2hop_rows", "filtered_2hop_rows", "run_id",
              "is_warmup", "total_ms", "onehop_ms", "mwj_ms", "output_rows",
              "rows_match"]


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
        by_cell.setdefault((r["system"], r["dataset"]), []).append(r)
    fields = ["system", "dataset", "hub_fraction", "query", "accounts", "edges",
              "unfiltered_2hop_rows", "filtered_2hop_rows", "n_runs",
              "median_ms", "min_ms", "max_ms", "stddev_ms", "output_rows",
              "rows_match"]
    with open(path, "w", newline="") as f:
        sw = csv.DictWriter(f, fieldnames=fields)
        sw.writeheader()
        for (system, dataset), cell in sorted(by_cell.items()):
            base = {k: cell[0][k] for k in
                    ["system", "dataset", "hub_fraction", "query", "accounts",
                     "edges", "unfiltered_2hop_rows", "filtered_2hop_rows",
                     "output_rows", "rows_match"]}
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


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def collect_metadata(args) -> dict:
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
        "note": (
            "E5 output-sensitivity: four equal-input banking variants "
            "(200k accounts / 1M txns, seed 42) with hub concentration "
            "p in {0, 0.1, 0.2, 0.35} (1000 hubs), fixed 2-hop chain anchored "
            "at planted account 46 (out-degree 5). Ground-truth unfiltered/"
            "filtered 2-hop row counts come from each variant's stats.json "
            "and are cross-checked against every cell's output_rows "
            "(rows_match column; obliviator is perf-only). Graphite latency "
            "= onehop_ms + mwj_ms of the same rep. See "
            "docs/e5_output_sensitivity.md."
        ),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tier", default="1M", choices=sorted(TIERS),
                   help="Input-size tier: 1M (200k accounts / 1M txns, 1000 "
                        "hubs) or 10M (2M accounts / 10M txns, 10000 hubs). "
                        "Default: 1M")
    p.add_argument("--datasets", default=None,
                   help="Comma-separated variant labels (default: "
                        f"{','.join(l for l, _ in VARIANTS)}; "
                        "smoke is available in the 1M tier)")
    p.add_argument("--systems", default=",".join(ALL_SYSTEMS),
                   help=f"Comma-separated systems (default and allowed: "
                        f"{','.join(ALL_SYSTEMS)})")
    p.add_argument("--measurement-runs", type=int, default=1,
                   help="Recorded measurement runs per cell (default: 1)")
    p.add_argument("--warmup-runs", type=int, default=0,
                   help="Discarded warm-up runs per cell (default: 0)")
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
                   help="Directory holding the dataset dirs (default: "
                        "<project>/input/plaintext)")
    p.add_argument("--output-dir", default=None,
                   help="Output directory (default: <project>/results/"
                        "e5_density for --tier 1M, e5_density_10M for 10M)")
    p.add_argument("--skip-build", action="store_true",
                   help="Skip the binary rebuild step")
    args = p.parse_args()

    if args.output_dir is None:
        args.output_dir = str(
            PROJECT_DIR / "results" / TIERS[args.tier]["default_output_dir"])

    all_datasets = tier_datasets(args.tier)
    if args.datasets:
        known = {d["label"]: d for d in all_datasets}
        wanted = [s.strip() for s in args.datasets.split(",") if s.strip()]
        unknown = [l for l in wanted if l not in known]
        if unknown:
            sys.exit(f"unknown dataset labels: {unknown} (allowed: {list(known)})")
        datasets = [known[l] for l in wanted]
    else:
        datasets = [d for d in all_datasets if d.get("default", True)]

    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    for s in systems:
        if s not in ALL_SYSTEMS:
            sys.exit(f"unknown system: {s} (allowed: {ALL_SYSTEMS})")

    cell_timeout = args.cell_timeout or None
    obl_timeout = args.obliviator_timeout or None
    data_root = Path(args.data_root)

    needs_onehop = "nebuladb" in systems
    needs_obliviator = "obliviator_chained" in systems

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "binary_stdout.log"
    raw_csv_path = out_dir / "raw_runs.csv"
    summary_csv_path = out_dir / "summary.csv"
    meta_path = out_dir / "run_metadata.json"
    meta = collect_metadata(args)

    print(f"E5 output-sensitivity runner: hop={K} (fixed), tier={args.tier}")
    print(f"  datasets: {[d['label'] for d in datasets]}")
    print(f"  systems : {systems}")
    print(f"  data    : {data_root}")
    print(f"  output  : {out_dir}")
    print(f"  timeouts: cell={cell_timeout}s obliviator={obl_timeout}s")
    print()

    rows = []
    total_reps = args.warmup_runs + args.measurement_runs

    with open(log_path, "w") as log_file:
        if not args.skip_build:
            e3.build_binaries({BANKING["onehop_target"]}, need_slim=False,
                              need_obliviator=needs_obliviator, log_file=log_file)
        else:
            required = [(SGX_APP, "sgx_app"),
                        (BANKING["onehop_bin"], BANKING["onehop_target"])]
            if needs_obliviator:
                required.append((e3.OBL_KHOP_BIN, "obliviator_khop_chained"))
            for bin_path, lbl in required:
                if not bin_path.exists():
                    sys.exit(f"--skip-build but {lbl} missing: {bin_path}")

        # Datasets: generate (or validate) every variant, then load ground truth.
        truth = {}
        for spec in datasets:
            generate_e5_dataset(spec, data_root, log_file)
            truth[spec["label"]] = load_ground_truth(data_root / spec["dir_name"])
            t = truth[spec["label"]]
            print(f"  [{spec['label']}] ground truth: "
                  f"unfiltered={t['unfiltered_2hop_rows']:,} "
                  f"filtered={t['filtered_2hop_rows']:,}")

        # One shared query pair: every variant uses banking_2hop.sql (anchor 46
        # is the planted account, already the query's literal).
        qname = f"banking_{K}hop"
        base_src = QUERY_DIR / f"{qname}.sql"
        if not base_src.is_file():
            sys.exit(f"query not found: {base_src}")
        decomposed_dir = out_dir / "decomposed"
        decomposed_dir.mkdir(exist_ok=True)
        base_sql = decomposed_dir / f"{qname}.sql"
        e3.substitute_anchor(base_src, None, base_sql)
        rewritten_sql = None
        if needs_onehop:
            rewritten_sql = decomposed_dir / f"{qname}_rewritten.sql"
            e3.run_rewrite(base_sql, rewritten_sql, log_file)

        with tempfile.TemporaryDirectory(dir=out_dir, prefix="_tmp_") as tmp_root:
            tmp_root = Path(tmp_root)

            obl_src_txt = {}
            if needs_obliviator:
                for spec in datasets:
                    obl_dir = tmp_root / f"obl_{spec['label']}"
                    obl_dir.mkdir()
                    print(f"[setup] obliviator src.txt for {spec['label']}...",
                          flush=True)
                    t0 = time.time()
                    obl_src_txt[spec["label"]] = e3.generate_obliviator_src_txt(
                        BANKING["obl_converter"], data_root / spec["dir_name"],
                        obl_dir, log_file)
                    print(f"  -> done ({time.time()-t0:.1f}s wall)")

            for rep_idx in range(total_reps):
                is_warmup = rep_idx < args.warmup_runs
                run_id = rep_idx - args.warmup_runs + 1
                label = "warm" if is_warmup else f"run{run_id}"
                print(f"--- rep {rep_idx+1}/{total_reps} ({label}) ---", flush=True)

                for spec in datasets:
                    data_dir = data_root / spec["dir_name"]
                    t = truth[spec["label"]]

                    onehop_ms, onehop_rows = (None, None)
                    hop_dir = tmp_root / f"hop_{spec['label']}"
                    if needs_onehop:
                        hop_dir.mkdir(exist_ok=True)
                        print(f"  [{spec['label']}] one-hop ...", end="", flush=True)
                        t0 = time.time()
                        onehop_ms, onehop_rows = e3.run_onehop(
                            BANKING["onehop_bin"], data_dir, hop_dir,
                            args.onehop_threads, log_file)
                        print(f" total={onehop_ms:.1f}ms rows={onehop_rows} "
                              f"({time.time()-t0:.1f}s wall)", flush=True)

                    for system in systems:
                        cell_total_ms = None
                        cell_onehop_ms = ""
                        cell_mwj_ms = ""
                        cell_rows = None
                        try:
                            if system == "nebuladb":
                                mwj_ms, mwj_rows = e3.run_mwj(
                                    rewritten_sql, hop_dir,
                                    args.mwj_threads, log_file,
                                    timeout=cell_timeout)
                                cell_total_ms = onehop_ms + mwj_ms
                                cell_onehop_ms = onehop_ms
                                cell_mwj_ms = mwj_ms
                                cell_rows = mwj_rows
                            elif system == "full_mwj_no_filter":
                                mwj_ms, mwj_rows = e3.run_mwj(
                                    base_sql, data_dir,
                                    args.mwj_threads, log_file,
                                    no_filter=True, timeout=cell_timeout)
                                cell_total_ms = mwj_ms
                                cell_mwj_ms = mwj_ms
                                cell_rows = mwj_rows
                            elif system == "obliviator_chained":
                                obl_ms, obl_rows = e3.run_obliviator_khop(
                                    K, obl_src_txt[spec["label"]],
                                    args.obliviator_threads,
                                    BANKING["obl_shape"],
                                    log_file, timeout=obl_timeout)
                                cell_total_ms = obl_ms
                                cell_rows = obl_rows
                        except RuntimeError as e:
                            kind = ("TIMEOUT" if isinstance(e, e3.CellTimeout)
                                    else "OOM")
                            log_file.write(f"\n!!! {system} {kind} at "
                                           f"{spec['label']}: {e}\n")
                            log_file.flush()
                            cell_rows = kind
                            cell_total_ms = None

                        # Ground-truth cross-check (obliviator is perf-only,
                        # but its banking row counts have matched exactly).
                        expected = (t["filtered_2hop_rows"]
                                    if system == "nebuladb"
                                    else t["unfiltered_2hop_rows"])
                        rows_match = ""
                        if isinstance(cell_rows, int):
                            rows_match = int(cell_rows == expected)
                            if not rows_match:
                                log_file.write(
                                    f"\n!!! {system} row-count mismatch at "
                                    f"{spec['label']}: got {cell_rows}, "
                                    f"expected {expected}\n")
                                log_file.flush()

                        total_str = (f"{cell_total_ms:.1f}ms"
                                     if cell_total_ms is not None
                                     else str(cell_rows))
                        print(f"  [{spec['label']}] {system:20s} {label} -> "
                              f"total={total_str} rows={cell_rows} "
                              f"match={rows_match}", flush=True)
                        rows.append({
                            "system": system,
                            "dataset": spec["label"],
                            "hub_fraction": spec["hub_fraction"],
                            "query": qname,
                            "accounts": spec["accounts"],
                            "edges": EDGE_RATIO * spec["accounts"],
                            "unfiltered_2hop_rows": t["unfiltered_2hop_rows"],
                            "filtered_2hop_rows": t["filtered_2hop_rows"],
                            "run_id": run_id,
                            "is_warmup": int(is_warmup),
                            "total_ms": cell_total_ms,
                            "onehop_ms": cell_onehop_ms,
                            "mwj_ms": cell_mwj_ms,
                            "output_rows": cell_rows,
                            "rows_match": rows_match,
                        })
                        write_raw_csv(rows, raw_csv_path)
                        write_summary_csv(rows, summary_csv_path)

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

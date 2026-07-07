#!/usr/bin/env python3
"""
One-hop OFFLINE/ONLINE amortization experiment (E3) -- MEASURED reuse.

Story
-----
The one-hop pipeline splits into a shared, build-once cost and a per-query cost:

  build_once  = buildNodeIndex
                The oblivious index over the node table. Built ONCE and reused
                across every one-hop query on the same graph.
  per_query   = 2x index copy  +  initProbeSide  +  online probe/sort/union
                Paid PER query. The two index copies are unavoidable because
                probing is DESTRUCTIVE (entries are consumed), so the shared
                index must stay immutable and each query works on fresh copies.

Serving N one-hop queries on a fixed graph therefore costs

      total(N)            = build_once + N * per_query
      amortized_per_query = build_once / N + per_query   --->  per_query  as N -> inf

Unlike a projected split, this experiment PHYSICALLY reuses the index: the
onehop binary is run with `--serve N`, which builds the index once and then
serves N queries against fresh copies, timing the shared build and each
per-query phase (see serveAmortized() / reportAmortize() in oneHop.cpp). The
binary emits an "AMORTIZE ..." line we parse directly -- the per-query number
is measured, not modelled.

Breakeven points reported:
  * crossover N  = build_once / per_query      (setup paid back)
  * within-10% N = ceil(10 * build_once / per_query)  (amortized within 10% of floor)

Datasets (real IBM AML W4 + synthetic Banking W1):
  aml_hi_small   ibm_aml_hi_small   (ibm_aml_onehop)
  aml_hi_medium  ibm_aml_hi_medium  (ibm_aml_onehop)
  banking_1M     banking_1M         (banking_onehop, generated seed=42 if missing)

Usage
-----
  python3 scripts/experiments/run_one_hop_amortization.py
  python3 scripts/experiments/run_one_hop_amortization.py --datasets aml_hi_small --serve 20
  python3 scripts/experiments/run_one_hop_amortization.py --skip-build --reps 3 --serve 30
"""

import argparse
import csv
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from statistics import median

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
GENERATOR = PROJECT_DIR / "scripts" / "generate_banking_scaled.py"
BUILD_DIR = PROJECT_DIR / "obligraph" / "build"
DATA_ROOT = PROJECT_DIR / "input" / "plaintext"
RESULTS_DIR = PROJECT_DIR / "results" / "one_hop_amortization"

SEED = 42

# Amortization grid: # of one-hop queries to plot the curve at (analytic extension
# of the measured build_once / per_query numbers -- the physics is measured, the
# curve is just build_once/N + per_query evaluated at these N).
DEFAULT_N_GRID = [1, 2, 3, 5, 10, 20, 50, 100, 200, 500, 1000]

DATASETS = [
    {"label": "aml_hi_small",  "dir_name": "ibm_aml_hi_small",  "target": "ibm_aml_onehop", "bin": BUILD_DIR / "ibm_aml_onehop", "generate": None},
    {"label": "aml_hi_medium", "dir_name": "ibm_aml_hi_medium", "target": "ibm_aml_onehop", "bin": BUILD_DIR / "ibm_aml_onehop", "generate": None},
    {"label": "banking_1M",    "dir_name": "banking_1M",        "target": "banking_onehop", "bin": BUILD_DIR / "banking_onehop", "generate": 1_000_000},
]

AMORTIZE_RE = re.compile(
    r"AMORTIZE\s+serve_n=(\d+)\s+build_once_ms=([\d.eE+-]+)\s+"
    r"per_query_total_median_ms=([\d.eE+-]+)\s+per_query_total_mean_ms=([\d.eE+-]+)\s+"
    r"per_query_prep_median_ms=([\d.eE+-]+)\s+per_query_serve_median_ms=([\d.eE+-]+)\s+"
    r"result_rows=(\d+)"
)


def parse_amortize(stdout: str):
    m = AMORTIZE_RE.search(stdout)
    if not m:
        return None
    return {
        "serve_n": int(m.group(1)),
        "build_once_ms": float(m.group(2)),
        "per_query_total_median_ms": float(m.group(3)),
        "per_query_total_mean_ms": float(m.group(4)),
        "per_query_prep_median_ms": float(m.group(5)),
        "per_query_serve_median_ms": float(m.group(6)),
        "result_rows": int(m.group(7)),
    }


def count_rows(csv_path: Path) -> int:
    if not csv_path.exists():
        return -1
    with open(csv_path) as f:
        return sum(1 for _ in f) - 1


def dataset_dims(data_dir: Path):
    return count_rows(data_dir / "account.csv"), count_rows(data_dir / "txn.csv")


def build_target(target: str):
    print(f"[build] cmake --build {BUILD_DIR} --target {target} (Release)")
    subprocess.run(["cmake", "--build", str(BUILD_DIR), "--target", target, "--config", "Release"],
                   check=True, cwd=PROJECT_DIR)


def generate_banking(data_dir: Path, accounts: int):
    if (data_dir / "account.csv").exists():
        n = count_rows(data_dir / "account.csv")
        if n == accounts:
            print(f"[gen]  {data_dir.name}: existing dataset matches ({n} accounts) -- skipping")
            return
        print(f"[gen]  {data_dir.name}: wrong size ({n} != {accounts}) -- regenerating")
        shutil.rmtree(data_dir)
    print(f"[gen]  {data_dir.name}: generating {accounts:,} accounts ...")
    subprocess.run(["python3", str(GENERATOR), str(accounts), str(data_dir), "--seed", str(SEED)],
                   check=True, cwd=PROJECT_DIR)


def run_serve(bin_path: Path, data_dir: Path, threads: int, serve_n: int):
    """Run the onehop binary in --serve mode; return (parsed AMORTIZE dict, stdout)."""
    # output_csv is a required positional but ignored in serve mode.
    proc = subprocess.run(
        [str(bin_path), str(data_dir), "/dev/null",
         "--serve", str(serve_n), "--threads", str(threads)],
        check=True, cwd=PROJECT_DIR, capture_output=True, text=True)
    parsed = parse_amortize(proc.stdout)
    if parsed is None:
        sys.stderr.write(proc.stdout[-4000:])
        sys.exit(f"[run]  FAILED -- no AMORTIZE line for {data_dir.name}")
    return parsed, proc.stdout


def write_metadata(path: Path, datasets_run, threads, reps, serve_n, n_grid):
    def git(*a):
        return subprocess.run(["git", *a], cwd=PROJECT_DIR, capture_output=True, text=True).stdout.strip()
    path.write_text(json.dumps({
        "experiment": "one_hop_amortization (E3, measured reuse)",
        "commit": git("rev-parse", "HEAD"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "hostname": platform.node(),
        "uname": platform.platform(),
        "nproc": os.cpu_count(),
        "build_type": "Release",
        "threads": threads,
        "reps_measured": reps,
        "serve_n_per_run": serve_n,
        "warmup_runs_discarded": 1,
        "mode": "physical reuse via onehop --serve N (build index once, serve N queries)",
        "seed": SEED,
        "n_grid": n_grid,
        "datasets": datasets_run,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--datasets", nargs="+", default=None,
                   help=f"subset of {[d['label'] for d in DATASETS]}")
    p.add_argument("--threads", type=int, default=64)
    p.add_argument("--reps", type=int, default=3, help="measured runs per dataset (median across runs)")
    p.add_argument("--serve", type=int, default=30, help="# queries served per run (per-query sample size)")
    p.add_argument("--skip-build", action="store_true")
    p.add_argument("--n-grid", type=int, nargs="+", default=DEFAULT_N_GRID)
    args = p.parse_args()

    datasets = DATASETS
    if args.datasets:
        wanted = set(args.datasets)
        datasets = [d for d in DATASETS if d["label"] in wanted]
        unknown = wanted - {d["label"] for d in datasets}
        if unknown:
            sys.exit(f"unknown dataset labels: {sorted(unknown)}")
    if args.serve < 2:
        sys.exit("--serve must be >= 2")

    if not args.skip_build:
        for tgt in sorted({d["target"] for d in datasets}):
            build_target(tgt)
    for d in datasets:
        if not Path(d["bin"]).exists():
            sys.exit(f"binary missing: {d['bin']} (drop --skip-build to build it)")

    for d in datasets:
        data_dir = DATA_ROOT / d["dir_name"]
        if d["generate"] is not None:
            generate_banking(data_dir, d["generate"])
        elif not data_dir.is_dir():
            sys.exit(f"dataset not found (and not auto-generated): {data_dir}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    log_fh = open(RESULTS_DIR / "binary_stdout.log", "w")

    raw_rows, measured = [], []
    for d in datasets:
        data_dir = DATA_ROOT / d["dir_name"]
        n_nodes, n_edges = dataset_dims(data_dir)
        print(f"\n[run]  {d['label']}: warm-up + {args.reps} measured "
              f"(serve={args.serve}; {n_nodes:,} nodes, {n_edges:,} edges)")

        # Warm-up (small serve) discarded.
        _, warm_out = run_serve(Path(d["bin"]), data_dir, args.threads, min(args.serve, 2))
        log_fh.write(f"\n\n===== {d['label']} warmup =====\n{warm_out}")

        build_samples, pq_samples, prep_samples, serve_samples = [], [], [], []
        rows_seen = set()
        for rep in range(1, args.reps + 1):
            a, out = run_serve(Path(d["bin"]), data_dir, args.threads, args.serve)
            log_fh.write(f"\n\n===== {d['label']} measured rep {rep} =====\n{out}")
            log_fh.flush()
            build_samples.append(a["build_once_ms"])
            pq_samples.append(a["per_query_total_median_ms"])
            prep_samples.append(a["per_query_prep_median_ms"])
            serve_samples.append(a["per_query_serve_median_ms"])
            rows_seen.add(a["result_rows"])
            print(f"[run]  {d['label']}: rep {rep} build_once={a['build_once_ms']:8.2f}ms  "
                  f"per_query={a['per_query_total_median_ms']:7.2f}ms  "
                  f"(prep={a['per_query_prep_median_ms']:.2f} + serve={a['per_query_serve_median_ms']:.2f})  "
                  f"rows={a['result_rows']}")
            raw_rows.append({
                "dataset": d["label"], "num_nodes": n_nodes, "num_edges": n_edges, "rep": rep,
                "build_once_ms": a["build_once_ms"],
                "per_query_total_median_ms": a["per_query_total_median_ms"],
                "per_query_prep_median_ms": a["per_query_prep_median_ms"],
                "per_query_serve_median_ms": a["per_query_serve_median_ms"],
                "result_rows": a["result_rows"],
            })
        if len(rows_seen) != 1:
            print(f"[warn] {d['label']}: result_rows varied across queries: {sorted(rows_seen)}")

        build_med = median(build_samples)
        pq_med = median(pq_samples)
        cross = build_med / pq_med if pq_med else float("inf")
        within10 = math.ceil(10 * cross) if pq_med else float("inf")
        measured.append({
            "dataset": d["label"], "num_nodes": n_nodes, "num_edges": n_edges,
            "build_once_ms": build_med,
            "per_query_ms": pq_med,
            "per_query_prep_ms": median(prep_samples),
            "per_query_serve_ms": median(serve_samples),
            "build_per_query_ratio": cross,
            "breakeven_n_crossover": cross,
            "breakeven_n_within10pct": within10,
            "result_rows": sorted(rows_seen)[0],
        })
        print(f"[res]  {d['label']}: build_once(med)={build_med:.2f}ms  per_query(med)={pq_med:.2f}ms  "
              f"ratio={cross:.2f}  crossover N={cross:.2f}  within-10% N={within10}")

    log_fh.close()

    with open(RESULTS_DIR / "raw_runs.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(raw_rows[0].keys()))
        w.writeheader()
        for r in raw_rows:
            w.writerow({k: (f"{v:.3f}" if isinstance(v, float) else v) for k, v in r.items()})

    with open(RESULTS_DIR / "measured.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(measured[0].keys()))
        w.writeheader()
        for r in measured:
            w.writerow({k: (f"{v:.3f}" if isinstance(v, float) else v) for k, v in r.items()})

    with open(RESULTS_DIR / "amortization.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["dataset", "num_edges", "n_queries", "build_once_ms", "per_query_ms",
                    "total_ms", "amortized_per_query_ms"])
        for m in measured:
            b, q = m["build_once_ms"], m["per_query_ms"]
            for n in args.n_grid:
                total = b + n * q
                w.writerow([m["dataset"], m["num_edges"], n,
                            f"{b:.3f}", f"{q:.3f}", f"{total:.3f}", f"{total / n:.3f}"])

    write_metadata(RESULTS_DIR / "run_metadata.json",
                   [d["label"] for d in datasets], args.threads, args.reps, args.serve, args.n_grid)

    print(f"\n[done] raw         : {RESULTS_DIR / 'raw_runs.csv'}")
    print(f"[done] measured    : {RESULTS_DIR / 'measured.csv'}")
    print(f"[done] amortization: {RESULTS_DIR / 'amortization.csv'}")
    print(f"[done] metadata    : {RESULTS_DIR / 'run_metadata.json'}")
    print(f"[done] stdout log  : {RESULTS_DIR / 'binary_stdout.log'}")
    print(f"\nNext: python3 scripts/experiments/plot_one_hop_amortization.py")


if __name__ == "__main__":
    main()

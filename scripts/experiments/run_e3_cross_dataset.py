#!/usr/bin/env python3
"""
E3 cross-dataset runner: one fixed chain query, every dataset, three systems.

Complements E1 (fixed dataset, hop sweep) and E2 (fixed workload, size sweep):
E3 fixes the query shape (default: the 2-hop chain) and sweeps across the
workload datasets, comparing the three canonical systems on each (see
CLAUDE.md "Experiment Comparison Systems"):

  1. Graphite            (runner key `nebuladb`, our system)
       one-hop driver -> hop table -> sgx_app on the rewritten chain query.
       Per-cell latency = onehop_ms + mwj_ms (the one-hop run of the same rep).
  2. Obliviator chained  (runner key `obliviator_chained`)
       `obliviator_khop_chained` (NFK kernel, 2K pairwise joins) on the
       workload's converted src.txt. The kernel takes the workload's payload
       shape (acct_cols, txn_cols) as CLI args — banking 3 4, AML 2 4,
       patents 2 2 — so the next-hop key is extracted from the right column
       for every workload.
  3. Full MWJ            (runner key `full_mwj_no_filter`)
       `sgx_app --no-filter` on the anchored chain query: the full unfiltered
       multi-way join. Per the canonical system set, the unfiltered mode IS
       Full MWJ; the filtered variant is not run.

Datasets (smallest edge count first; all four run by default):
  banking_1M  (W1, generated, seed 42)   1M accounts /   5M edges
  hi_small    (IBM AML W4)             515k accounts / 5.1M edges
  patents     (SNAP cit-Patents W3)    3.8M accounts /16.5M edges
  hi_large    (IBM AML W4)             2.1M accounts / 180M edges
      hi_large always uses the column-trimmed slim dataset
      (ibm_aml_hi_large_slim) + the reduced-capacity ./sgx_app_slim binary for
      the Graphite and Full MWJ cells (the only configuration where the hop
      table fits in RAM); the obliviator converter reads the FULL hi_large
      txn.csv because the slim table drops txn_time, which the converter
      requires.

A binary OOM/crash/timeout is caught per cell (recorded as output_rows =
OOM/TIMEOUT, total_ms empty) and never aborts the sweep — failed cells are
annotated in the E3 plot rather than hidden. Datasets are independent: a
failure on one never skips another.

The script's repo provides the binaries and query/converter scripts
(PROJECT_DIR = two levels above this file); --data-root and --output-dir may
point elsewhere (e.g. at the primary checkout's input/plaintext and results/)
so experiments read the canonical datasets without duplicating them.

Outputs (under <output-dir>):
  raw_runs.csv          every run, including warm-ups
  summary.csv           measurement runs only, per cell: n, median, min, max, stddev, output_rows
  decomposed/           anchored base + rewritten queries (inspectable, untimed)
  run_metadata.json     commit, host, nproc, settings
  binary_stdout.log     full stdout from every invocation

Usage:
  python3 scripts/experiments/run_e3_cross_dataset.py
  python3 scripts/experiments/run_e3_cross_dataset.py --datasets banking_1M,hi_small
  python3 scripts/experiments/run_e3_cross_dataset.py --hop 3 --skip-build
  python3 scripts/experiments/run_e3_cross_dataset.py \\
      --data-root /path/to/main/checkout/input/plaintext \\
      --output-dir /path/to/main/checkout/results/e3_cross_dataset
"""

import argparse
import csv
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]

SGX_APP = PROJECT_DIR / "sgx_app"
SGX_APP_SLIM = PROJECT_DIR / "sgx_app_slim"  # make sgx_app_slim (MAX_ATTRIBUTES=40)
OBLIGRAPH_SRC = PROJECT_DIR / "obligraph"
OBLIGRAPH_BUILD = PROJECT_DIR / "obligraph" / "build"
REWRITER = PROJECT_DIR / "scripts" / "rewrite_chain_query.py"
GENERATOR = PROJECT_DIR / "scripts" / "generate_banking_scaled.py"
QUERY_DIR = PROJECT_DIR / "input" / "queries"

OBL_FK_DIR = PROJECT_DIR / "obl-radix" / "baselines" / "obliviatorFK-TDX"
OBL_NFK_DIR = PROJECT_DIR / "obl-radix" / "baselines" / "obliviatorNFK-TDX"
OBL_KHOP_BIN = OBL_NFK_DIR / "obliviator_khop_chained"

SEED = 42
EDGE_RATIO = 5  # txns per account; fixed by generate_banking_scaled.py

# Per-workload configuration: one-hop driver + CMake target, chain-query name
# prefix, obliviator converter, and the obliviator payload shape (comma-column
# counts of the account/txn .data in src.txt, passed to obliviator_khop_chained
# so it extracts the next-hop key from the right column).
WORKLOADS = {
    "banking": {
        "onehop_bin": OBLIGRAPH_BUILD / "banking_onehop",
        "onehop_target": "banking_onehop",
        "query_prefix": "banking",
        "obl_converter": OBL_FK_DIR / "convert_banking_1hop.py",
        "obl_shape": (3, 4),  # "id,balance,owner" / "txn_id,acc_to,amount,txn_time"
    },
    "aml": {
        "onehop_bin": OBLIGRAPH_BUILD / "ibm_aml_onehop",
        "onehop_target": "ibm_aml_onehop",
        "query_prefix": "aml",
        "obl_converter": OBL_FK_DIR / "convert_aml_1hop.py",
        "obl_shape": (2, 4),  # "id,bank" / "txn_id,acc_to,amount,txn_time"
    },
    "snap": {
        "onehop_bin": OBLIGRAPH_BUILD / "snap_patents_onehop",
        "onehop_target": "snap_patents_onehop",
        "query_prefix": "snap",
        "obl_converter": OBL_FK_DIR / "convert_patents_1hop.py",
        "obl_shape": (2, 2),  # "id,<gyear cat claims>" / "txn_id,acc_to"
    },
}

# Dataset table, smallest edge count first so failures surface fast.
# `anchor` is substituted for the a1.bank_id / a1.account_id literal in the
# base query (None = the query file's own anchor is already valid).
# `dir_name_slim` + `sgx_app_slim` mark the memory-reduced path (hi_large).
DATASETS = [
    # Smoke-test entry; excluded from the default sweep ("default": False).
    {"label": "banking_10k", "workload": "banking", "dir_name": "banking_10k",
     "accounts": 10_000, "edges": 50_000, "anchor": None,
     "generate": True, "default": False},
    {"label": "banking_1M", "workload": "banking", "dir_name": "banking_1M",
     "accounts": 1_000_000, "edges": 5_000_000, "anchor": None,
     "generate": True},
    {"label": "hi_small", "workload": "aml", "dir_name": "ibm_aml_hi_small",
     "accounts": 515_088, "edges": 5_078_345, "anchor": 224866},
    {"label": "patents", "workload": "snap", "dir_name": "snap_patents",
     "accounts": 3_774_768, "edges": 16_518_948, "anchor": 3569342},
    {"label": "hi_medium", "workload": "aml", "dir_name": "ibm_aml_hi_medium",
     "accounts": 2_077_023, "edges": 31_898_238, "anchor": 118871},
    {"label": "hi_large", "workload": "aml", "dir_name": "ibm_aml_hi_large_slim",
     "obl_dir_name": "ibm_aml_hi_large",  # converter needs full txn.csv (txn_time)
     "accounts": 2_116_168, "edges": 179_702_229, "anchor": 111545,
     "slim": True},
]

# The three canonical systems (CLAUDE.md "Experiment Comparison Systems").
# Internal keys match E1/E2; presentation names (Graphite / Obliviator
# chained / Full MWJ) are applied by plot_e3_cross_dataset.py.
ALL_SYSTEMS = ["nebuladb", "obliviator_chained", "full_mwj_no_filter"]

# `a1.bank_id = <N>` (AML) or `a1.account_id = <N>` (banking/patents) anchor.
ANCHOR_RE = re.compile(r"(a1\.(?:bank_id|account_id)\s*=\s*)(\d+)")

# ---------------------------------------------------------------------------
# Stdout parsers (same regexes as the E1/E2 runners)
# ---------------------------------------------------------------------------

ONEHOP_TIMING_RE = re.compile(r"TIMING_REPORTED\s+categories=\S+\s+total=([\d.]+)ms")
MWJ_TIMING_RE = re.compile(r"PHASE_TIMING:[^\n]*Total=([\d.]+)")
RESULT_ROWS_RE = re.compile(r"Result:\s+(\d+)\s+rows")
OBL_KHOP_TIME_RE = re.compile(r"^\s*online_sec\s+\(sum on-clock\)\s+:\s+([\d.]+)", re.MULTILINE)
OBL_KHOP_ROWS_RE = re.compile(r"^\s*final_rows\s+:\s+(\d+)", re.MULTILINE)


def parse_onehop_total_ms(stdout: str) -> float:
    m = ONEHOP_TIMING_RE.search(stdout)
    if not m:
        raise RuntimeError("could not find TIMING_REPORTED line in one-hop output")
    return float(m.group(1))


def parse_mwj_total_ms(stdout: str) -> float:
    m = MWJ_TIMING_RE.search(stdout)
    if not m:
        raise RuntimeError("could not find PHASE_TIMING line in sgx_app output")
    return float(m.group(1)) * 1000.0


def parse_result_rows(stdout: str) -> int:
    matches = RESULT_ROWS_RE.findall(stdout)
    if not matches:
        raise RuntimeError("could not find 'Result: N rows' line")
    return int(matches[-1])


def parse_obliviator_khop(stdout: str) -> tuple:
    tm = OBL_KHOP_TIME_RE.search(stdout)
    rm = OBL_KHOP_ROWS_RE.search(stdout)
    if not tm or not rm:
        raise RuntimeError("could not find online_sec / final_rows in khop output")
    return float(tm.group(1)) * 1000.0, int(rm.group(1))


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

class CellTimeout(RuntimeError):
    """Cell exceeded its wall-clock budget. RuntimeError subclass so the
    OOM/crash handler catches it too, while staying distinguishable."""


def run_capture(cmd, *, log_file, env_extra=None, timeout=None) -> str:
    log_file.write(f"\n+++ {' '.join(str(c) for c in cmd)}\n")
    if env_extra:
        log_file.write(f"    env_extra: {env_extra}\n")
    if timeout:
        log_file.write(f"    timeout: {timeout}s\n")
    log_file.flush()
    proc_env = None
    if env_extra:
        proc_env = os.environ.copy()
        proc_env.update({str(k): str(v) for k, v in env_extra.items()})
    try:
        proc = subprocess.run(
            [str(c) for c in cmd], capture_output=True, text=True, env=proc_env,
            timeout=timeout)
    except subprocess.TimeoutExpired as e:
        partial = e.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        log_file.write(partial)
        log_file.write(f"\n!!! TIMEOUT after {timeout}s: "
                       f"{' '.join(str(c) for c in cmd)}\n")
        log_file.flush()
        raise CellTimeout(f"timed out after {timeout}s") from None
    log_file.write(proc.stdout)
    if proc.stderr:
        log_file.write("\n--- stderr ---\n" + proc.stderr)
    log_file.flush()
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed (exit {proc.returncode}): {' '.join(str(c) for c in cmd)}\n"
            f"--- stderr ---\n{proc.stderr}"
        )
    return proc.stdout


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------

def run_onehop(onehop_bin: Path, data_dir: Path, out_dir: Path, threads: int,
               log_file) -> tuple:
    """Run the workload's one-hop driver; hop.csv lands in out_dir.
    Returns (onehop_ms, rows)."""
    hop_csv = out_dir / "hop.csv"
    stdout = run_capture(
        [onehop_bin, data_dir, hop_csv, "--threads", str(threads)],
        log_file=log_file,
    )
    return parse_onehop_total_ms(stdout), parse_result_rows(stdout)


def substitute_anchor(src_sql: Path, anchor, dst_sql: Path):
    """Copy src_sql to dst_sql, replacing the a1 anchor literal when anchor is
    not None (each AML variant has its own valid anchor bank)."""
    text = src_sql.read_text()
    if anchor is not None:
        text, n = ANCHOR_RE.subn(rf"\g<1>{anchor}", text)
        if n == 0:
            raise RuntimeError(f"{src_sql}: no a1 anchor literal to substitute")
    dst_sql.write_text(text)


def run_rewrite(query_path: Path, out_path: Path, log_file):
    """Untimed: rewrite the chain query against the hop table."""
    run_capture(["python3", REWRITER, query_path, out_path], log_file=log_file)


def run_mwj(query_path: Path, data_dir: Path, mwj_threads: int, log_file,
            no_filter: bool = False, timeout=None, sgx_app_bin: Path = SGX_APP) -> tuple:
    """Run sgx_app (or sgx_app_slim) once; returns (mwj_ms, output_rows).
    OBL_MWJ_SORT_THREADS sizes the shared bitonic parallel_sort pool (0 leaves
    it unset -> hardware_concurrency). no_filter appends --no-filter, making
    sgx_app compute the full unfiltered multi-way join."""
    env_extra = None
    if mwj_threads and mwj_threads > 0:
        env_extra = {"OBL_MWJ_SORT_THREADS": str(mwj_threads)}
    with tempfile.TemporaryDirectory() as tmp:
        out_csv = Path(tmp) / "out.csv"
        cmd = [sgx_app_bin, query_path, data_dir, out_csv]
        if no_filter:
            cmd.append("--no-filter")
        stdout = run_capture(cmd, log_file=log_file, env_extra=env_extra,
                             timeout=timeout)
    return parse_mwj_total_ms(stdout), parse_result_rows(stdout)


def generate_obliviator_src_txt(converter: Path, data_dir: Path, out_dir: Path,
                                log_file) -> Path:
    """Run the workload's converter once; returns the src.txt path. The dst.txt
    it also writes is only used by the join-sort 1-hop variant and is discarded
    with the tempdir."""
    src_path = out_dir / "src.txt"
    dst_path = out_dir / "dst.txt"
    run_capture(
        ["python3", converter,
         data_dir / "account.csv", data_dir / "txn.csv", src_path, dst_path],
        log_file=log_file,
    )
    return src_path


def run_obliviator_khop(K: int, src_txt: Path, threads: int, shape: tuple,
                        log_file, timeout=None) -> tuple:
    """Run obliviator_khop_chained (K>=2) with the workload's payload shape.
    Returns (online_ms, output_rows)."""
    acct_cols, txn_cols = shape
    with tempfile.TemporaryDirectory() as tmp:
        out_csv = Path(tmp) / "obl_out.csv"
        cmd = [OBL_KHOP_BIN, str(threads), str(K), src_txt, out_csv,
               str(acct_cols), str(txn_cols)]
        stdout = run_capture(cmd, log_file=log_file, timeout=timeout)
    return parse_obliviator_khop(stdout)


# ---------------------------------------------------------------------------
# Build + dataset prep
# ---------------------------------------------------------------------------

def build_binaries(onehop_targets, need_slim: bool, need_obliviator: bool,
                   log_file):
    if not (OBLIGRAPH_BUILD / "CMakeCache.txt").exists():
        print("[build] cmake configure (Release)...", flush=True)
        run_capture(
            ["cmake", "-S", OBLIGRAPH_SRC, "-B", OBLIGRAPH_BUILD,
             "-DCMAKE_BUILD_TYPE=Release"],
            log_file=log_file,
        )
    for target in sorted(onehop_targets):
        print(f"[build] obligraph/{target} (Release)...", flush=True)
        run_capture(
            ["cmake", "--build", OBLIGRAPH_BUILD,
             "--target", target, "--config", "Release"],
            log_file=log_file,
        )
    targets = [(["make"], "sgx_app")]
    if need_slim:
        targets.append((["make", "sgx_app_slim"], "sgx_app_slim"))
    for cmd, name in targets:
        print(f"[build] {name} (make)...", flush=True)
        proc = subprocess.run(cmd, cwd=PROJECT_DIR, capture_output=True, text=True)
        log_file.write(f"\n+++ {' '.join(cmd)} ({name})\n" + proc.stdout)
        if proc.stderr:
            log_file.write("\n--- stderr ---\n" + proc.stderr)
        log_file.flush()
        if proc.returncode != 0:
            raise RuntimeError(f"{name} build failed:\n{proc.stderr}")
    if need_obliviator:
        print("[build] obliviator_khop_chained (NFK)...", flush=True)
        run_capture(
            ["make", "-C", OBL_NFK_DIR, "-f", "Makefile.standalone",
             "obliviator_khop_chained"],
            log_file=log_file,
        )


def banking_dataset_is_valid(data_dir: Path, accounts: int) -> bool:
    """Cheap row-count check (content trusted via seed=42)."""
    acc_csv = data_dir / "account.csv"
    txn_csv = data_dir / "txn.csv"
    if not acc_csv.is_file() or not txn_csv.is_file():
        return False

    def rows(p):
        n = 0
        with open(p) as f:
            for _ in f:
                n += 1
        return n - 1  # header

    return rows(acc_csv) == accounts and rows(txn_csv) == EDGE_RATIO * accounts


def generate_banking_dataset(spec, data_root: Path, log_file):
    data_dir = data_root / spec["dir_name"]
    if banking_dataset_is_valid(data_dir, spec["accounts"]):
        print(f"[data] {spec['dir_name']} present with expected row counts")
        return
    if data_dir.exists():
        print(f"[data] {spec['dir_name']} wrong size -> regenerating")
        shutil.rmtree(data_dir)
    print(f"[data] generating {spec['dir_name']} "
          f"({spec['accounts']} accounts, seed {SEED})...", flush=True)
    t0 = time.time()
    run_capture(
        ["python3", GENERATOR, str(spec["accounts"]), data_dir,
         "--seed", str(SEED)],
        log_file=log_file,
    )
    print(f"  -> done ({time.time()-t0:.1f}s)")


# ---------------------------------------------------------------------------
# Output writers (called after every cell so partial results are always on
# disk — a sweep interrupted mid-way still leaves a plottable summary.csv)
# ---------------------------------------------------------------------------

RAW_FIELDS = ["system", "dataset", "workload", "query", "edges", "run_id",
              "is_warmup", "total_ms", "onehop_ms", "mwj_ms", "output_rows"]


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
    with open(path, "w", newline="") as f:
        sw = csv.DictWriter(f, fieldnames=[
            "system", "dataset", "workload", "query", "edges", "n_runs",
            "median_ms", "min_ms", "max_ms", "stddev_ms", "output_rows",
        ])
        sw.writeheader()
        for (system, dataset), cell in sorted(by_cell.items()):
            base = {"system": system, "dataset": dataset,
                    "workload": cell[0]["workload"], "query": cell[0]["query"],
                    "edges": cell[0]["edges"]}
            totals = [c["total_ms"] for c in cell if c["total_ms"] is not None]
            if not totals:
                sw.writerow({**base, "n_runs": 0, "median_ms": "", "min_ms": "",
                             "max_ms": "", "stddev_ms": "",
                             "output_rows": cell[0]["output_rows"]})
                continue
            sw.writerow({**base, "n_runs": len(totals),
                         "median_ms": statistics.median(totals),
                         "min_ms": min(totals), "max_ms": max(totals),
                         "stddev_ms": (statistics.stdev(totals)
                                       if len(totals) >= 2 else 0.0),
                         "output_rows": cell[0]["output_rows"]})


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
            "E3 cross-dataset: one fixed chain query, all datasets, the three "
            "canonical systems (Graphite=nebuladb, Obliviator chained, Full "
            "MWJ=full_mwj_no_filter; sgx_app --no-filter). Graphite >=2-hop "
            "latency = onehop_ms + mwj_ms of the same rep. hi_large uses the "
            "slim txn table + sgx_app_slim for the Graphite/Full MWJ cells; "
            "the obliviator converter reads the full hi_large txn.csv (slim "
            "drops txn_time). obliviator_khop_chained receives the workload "
            "payload shape (banking 3 4, AML 2 4, patents 2 2) so the "
            "next-hop key column is correct on every workload. Any binary "
            "OOM/crash/timeout is recorded per cell (output_rows=OOM/TIMEOUT, "
            "total_ms empty) and never aborts the sweep; datasets are "
            "independent, no cross-dataset skipping."
        ),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hop", type=int, default=2,
                   help="Chain hop count K for the fixed query (default: 2). "
                        "Obliviator requires 2 <= K <= 5.")
    p.add_argument("--datasets", default=None,
                   help="Comma-separated dataset labels (default: the four "
                        "paper datasets, smallest first: "
                        f"{','.join(d['label'] for d in DATASETS if d.get('default', True))}; "
                        "banking_10k is available for smoke tests)")
    p.add_argument("--systems", default=",".join(ALL_SYSTEMS),
                   help=f"Comma-separated systems (default and allowed: "
                        f"{','.join(ALL_SYSTEMS)})")
    p.add_argument("--measurement-runs", type=int, default=1,
                   help="Recorded measurement runs per cell (default: 1)")
    p.add_argument("--warmup-runs", type=int, default=0,
                   help="Discarded warm-up runs per cell (default: 0 — cells "
                        "are minutes-to-hours long at 2-hop)")
    p.add_argument("--onehop-threads", type=int, default=64,
                   help="Threads for the one-hop driver (default: 64)")
    p.add_argument("--mwj-threads", type=int, default=64,
                   help="OBL_MWJ_SORT_THREADS for sgx_app (default: 64; 0 = unset)")
    p.add_argument("--obliviator-threads", type=int, default=64,
                   help="Threads for obliviator_khop_chained (default: 64)")
    p.add_argument("--cell-timeout", type=int, default=7200,
                   help="Per-cell wall-clock budget in seconds for MWJ cells "
                        "(default: 7200; 0 = no limit). Exceeding it records "
                        "the cell as TIMEOUT.")
    p.add_argument("--obliviator-timeout", type=int, default=3600,
                   help="Per-cell budget for obliviator cells (default: 3600; "
                        "0 = no limit). The unfiltered chain join grows "
                        "unboundedly on the big graphs, so a tighter budget "
                        "records TIMEOUT quickly.")
    p.add_argument("--data-root", default=str(PROJECT_DIR / "input" / "plaintext"),
                   help="Directory holding the dataset dirs (default: "
                        "<project>/input/plaintext)")
    p.add_argument("--output-dir",
                   default=str(PROJECT_DIR / "results" / "e3_cross_dataset"),
                   help="Output directory (default: <project>/results/e3_cross_dataset)")
    p.add_argument("--skip-build", action="store_true",
                   help="Skip the binary rebuild step")
    args = p.parse_args()

    if args.datasets:
        known = {d["label"]: d for d in DATASETS}
        wanted = [s.strip() for s in args.datasets.split(",") if s.strip()]
        unknown = [l for l in wanted if l not in known]
        if unknown:
            sys.exit(f"unknown dataset labels: {unknown} (allowed: {list(known)})")
        datasets = [known[l] for l in wanted]
    else:
        datasets = [d for d in DATASETS if d.get("default", True)]

    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    for s in systems:
        if s not in ALL_SYSTEMS:
            sys.exit(f"unknown system: {s} (allowed: {ALL_SYSTEMS})")

    K = args.hop
    if "obliviator_chained" in systems and not (2 <= K <= 5):
        sys.exit(f"obliviator_chained requires 2 <= hop <= 5 (got {K}); "
                 f"drop it from --systems for other hop counts")

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

    print(f"E3 cross-dataset runner: hop={K}")
    print(f"  datasets: {[d['label'] for d in datasets]}")
    print(f"  systems : {systems}")
    print(f"  data    : {data_root}")
    print(f"  output  : {out_dir}")
    print(f"  timeouts: cell={cell_timeout}s obliviator={obl_timeout}s")
    print()

    rows = []
    total_reps = args.warmup_runs + args.measurement_runs

    with open(log_path, "w") as log_file:
        onehop_targets = {WORKLOADS[d["workload"]]["onehop_target"]
                          for d in datasets}
        need_slim = any(d.get("slim") for d in datasets)
        if not args.skip_build:
            build_binaries(onehop_targets, need_slim, needs_obliviator, log_file)
        else:
            required = [(SGX_APP, "sgx_app")]
            if need_slim:
                required.append((SGX_APP_SLIM, "sgx_app_slim"))
            if needs_obliviator:
                required.append((OBL_KHOP_BIN, "obliviator_khop_chained"))
            for d in datasets:
                wf = WORKLOADS[d["workload"]]
                required.append((wf["onehop_bin"], wf["onehop_target"]))
            for bin_path, lbl in required:
                if not bin_path.exists():
                    sys.exit(f"--skip-build but {lbl} missing: {bin_path}")

        # Dataset presence / generation, up front.
        for spec in datasets:
            if spec.get("generate"):
                generate_banking_dataset(spec, data_root, log_file)
            else:
                d = data_root / spec["dir_name"]
                if not (d / "account.csv").is_file() or not (d / "txn.csv").is_file():
                    sys.exit(f"dataset not found: {d}")

        # Anchored base + rewritten queries, untimed, cached under decomposed/.
        decomposed_dir = out_dir / "decomposed"
        decomposed_dir.mkdir(exist_ok=True)
        prepared = {}
        for spec in datasets:
            wf = WORKLOADS[spec["workload"]]
            qname = f"{wf['query_prefix']}_{K}hop"
            base_src = QUERY_DIR / f"{qname}.sql"
            if not base_src.is_file():
                sys.exit(f"query not found: {base_src}")
            ds_dir = decomposed_dir / spec["label"]
            ds_dir.mkdir(exist_ok=True)
            base_sql = ds_dir / f"{qname}.sql"
            substitute_anchor(base_src, spec["anchor"], base_sql)
            rewritten_sql = None
            if needs_onehop:
                rewritten_sql = ds_dir / f"{qname}_rewritten.sql"
                run_rewrite(base_sql, rewritten_sql, log_file)
            prepared[spec["label"]] = {"query_name": qname, "base": base_sql,
                                       "rewritten": rewritten_sql}

        # Big scratch files (hop.csv, src.txt) live under the output dir, not
        # /tmp — the hi_large src.txt alone is ~8 GB.
        with tempfile.TemporaryDirectory(dir=out_dir, prefix="_tmp_") as tmp_root:
            tmp_root = Path(tmp_root)

            # Obliviator src.txt once per dataset (deterministic, reused
            # across reps).
            obl_src_txt = {}
            if needs_obliviator:
                for spec in datasets:
                    wf = WORKLOADS[spec["workload"]]
                    obl_data_dir = data_root / spec.get("obl_dir_name",
                                                        spec["dir_name"])
                    obl_dir = tmp_root / f"obl_{spec['label']}"
                    obl_dir.mkdir()
                    print(f"[setup] obliviator src.txt for {spec['label']} "
                          f"(from {obl_data_dir.name})...", flush=True)
                    t0 = time.time()
                    obl_src_txt[spec["label"]] = generate_obliviator_src_txt(
                        wf["obl_converter"], obl_data_dir, obl_dir, log_file)
                    print(f"  -> done ({time.time()-t0:.1f}s wall)")

            for rep_idx in range(total_reps):
                is_warmup = rep_idx < args.warmup_runs
                run_id = rep_idx - args.warmup_runs + 1
                label = "warm" if is_warmup else f"run{run_id}"
                print(f"--- rep {rep_idx+1}/{total_reps} ({label}) ---", flush=True)

                for spec in datasets:
                    wf = WORKLOADS[spec["workload"]]
                    prep = prepared[spec["label"]]
                    data_dir = data_root / spec["dir_name"]
                    sgx_bin = SGX_APP_SLIM if spec.get("slim") else SGX_APP

                    # One-hop once per (dataset, rep); hop.csv feeds the
                    # Graphite MWJ stage of the same rep.
                    onehop_ms, onehop_rows = (None, None)
                    hop_dir = tmp_root / f"hop_{spec['label']}"
                    if needs_onehop:
                        hop_dir.mkdir(exist_ok=True)
                        print(f"  [{spec['label']}] one-hop ...", end="", flush=True)
                        t0 = time.time()
                        onehop_ms, onehop_rows = run_onehop(
                            wf["onehop_bin"], data_dir, hop_dir,
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
                                mwj_ms, mwj_rows = run_mwj(
                                    prep["rewritten"], hop_dir,
                                    args.mwj_threads, log_file,
                                    timeout=cell_timeout, sgx_app_bin=sgx_bin)
                                cell_total_ms = onehop_ms + mwj_ms
                                cell_onehop_ms = onehop_ms
                                cell_mwj_ms = mwj_ms
                                cell_rows = mwj_rows
                            elif system == "full_mwj_no_filter":
                                mwj_ms, mwj_rows = run_mwj(
                                    prep["base"], data_dir,
                                    args.mwj_threads, log_file,
                                    no_filter=True, timeout=cell_timeout,
                                    sgx_app_bin=sgx_bin)
                                cell_total_ms = mwj_ms
                                cell_mwj_ms = mwj_ms
                                cell_rows = mwj_rows
                            elif system == "obliviator_chained":
                                obl_ms, obl_rows = run_obliviator_khop(
                                    K, obl_src_txt[spec["label"]],
                                    args.obliviator_threads, wf["obl_shape"],
                                    log_file, timeout=obl_timeout)
                                cell_total_ms = obl_ms
                                cell_rows = obl_rows
                        except RuntimeError as e:
                            kind = "TIMEOUT" if isinstance(e, CellTimeout) else "OOM"
                            log_file.write(f"\n!!! {system} {kind} at "
                                           f"{spec['label']}: {e}\n")
                            log_file.flush()
                            cell_rows = kind
                            cell_total_ms = None

                        total_str = (f"{cell_total_ms:.1f}ms"
                                     if cell_total_ms is not None
                                     else str(cell_rows))
                        print(f"  [{spec['label']}] {system:20s} {label} -> "
                              f"total={total_str} rows={cell_rows}", flush=True)
                        rows.append({
                            "system": system,
                            "dataset": spec["label"],
                            "workload": spec["workload"],
                            "query": prep["query_name"],
                            "edges": spec["edges"],
                            "run_id": run_id,
                            "is_warmup": int(is_warmup),
                            "total_ms": cell_total_ms,
                            "onehop_ms": cell_onehop_ms,
                            "mwj_ms": cell_mwj_ms,
                            "output_rows": cell_rows,
                        })
                        # Keep partial results on disk after every cell.
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

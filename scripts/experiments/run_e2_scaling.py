#!/usr/bin/env python3
"""
E2 data-scaling runner (Banking W1, IBM AML W4, or SNAP cit-Patents W3).

Sweeps how latency scales with dataset size. Three workloads via --workload:
`banking` (W1, default), `aml` (IBM AML-Data W4: HI-Small/Medium/Large), and
`snap` (SNAP cit-Patents W3: a single full-graph scale, latency-vs-hop).
The workload selects the one-hop binary, the size table, the chain query set,
whether datasets are auto-generated (banking) or downloaded+converted (aml,
snap), and the default systems (banking: nebuladb+full_mwj; aml/snap: nebuladb
only).

Queries: one or more chain hops (--queries, default per workload). Hops are
looped *within* each dataset; on OOM/timeout at hop K for a dataset, that
dataset's higher hops are SKIPPED (other datasets are independent). For AML
each dataset substitutes its own `a1.bank_id` anchor into the query (the size
table carries it) since the anchor differs per variant.

Systems:

  1. NebulaDB
       - 1-hop  : `banking_onehop` alone (its output IS the 1-hop join).
       - >= 2-hop : rewrite the chain query against the pre-built hop
                    table -> `sgx_app` on the rewritten query.
     Per-cell latency = `mwj_ms` for the rewritten-query run, plus the
     amortized one-hop cost (inherited from the per-rep one-hop run).
  2. Full MWJ
       - `sgx_app` directly on the chain query (no decomposition).
     Per-cell latency = `mwj_ms`.

Full MWJ is skipped at sizes whose edge count exceeds `--full-mwj-max-edges`
(default 1M). NebulaDB runs at every requested size.

The one-hop step is run **once per (size, rep)**, NOT per cell: its hop.csv
is the per-dataset constant fed to the NebulaDB MWJ stage, and its timing is
the NebulaDB 1-hop cell value if `banking_1hop` is ever swapped in.

Per (system, size) cell: `--warmup-runs` discarded + `--measurement-runs`
recorded. Loop order: outer = repetition, inner = size, innermost = system.
Strictly sequential; full machine for every run. Cells from the same rep
are run back-to-back to keep one-hop matched run-by-run with the MWJ
measurement it feeds.

Per-run query results (hop.csv, sgx_app output) live in a temporary
directory and are discarded after each rep. We do NOT persist query
results from experiment runs.

sgx_app threading: sgx_app reads OBL_MWJ_SORT_THREADS at first sort call
(magic-static initializer in app/data_structures/table.cpp). The value
sizes the shared thread pool used by the bitonic parallel_sort over
entry_t. Defaults to std::thread::hardware_concurrency() when unset, so
parallelism is on by default; the env var only narrows the worker count.
This runner sets it to --mwj-threads (default 64). Pass 0 to leave the
env unset and let sgx_app pick hardware_concurrency.

Outputs (under results/e2_scaling/):
  raw_runs.csv          every run, including warm-ups
  summary.csv           measurement runs only, per cell:
                        n_runs, median_total_ms, median_onehop_ms,
                        median_mwj_ms, min/max/stddev_total_ms, output_rows
  decomposed/*.sql      cached rewriter output (untimed)
  run_metadata.json     commit, host, nproc, build flags, settings
  binary_stdout.log     full stdout from every invocation

Usage:
  python3 scripts/experiments/run_e2_scaling.py
  python3 scripts/experiments/run_e2_scaling.py --query banking_2hop
  python3 scripts/experiments/run_e2_scaling.py --sizes 10k,200k --skip-build
  # NebulaDB scaling on IBM AML W4, hops 2-5, HI-Small + HI-Medium:
  python3 scripts/experiments/run_e2_scaling.py --workload aml --systems nebuladb \\
      --queries aml_2hop,aml_3hop,aml_4hop,aml_5hop --sizes hi_small,hi_medium \\
      --warmup-runs 0 --measurement-runs 1 --cell-timeout 3600
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
RESULTS_DIR = PROJECT_DIR / "results" / "e2_scaling"

SGX_APP = PROJECT_DIR / "sgx_app"
# Memory-reduced MWJ binary (MAX_ATTRIBUTES=40), built by `make sgx_app_slim`.
# Used only for the slim HI-Large path when --slim-hi-large is set.
SGX_APP_SLIM = PROJECT_DIR / "sgx_app_slim"
OBLIGRAPH_BUILD = PROJECT_DIR / "obligraph" / "build"
REWRITER = PROJECT_DIR / "scripts" / "rewrite_chain_query.py"
GENERATOR = PROJECT_DIR / "scripts" / "generate_banking_scaled.py"
QUERY_DIR = PROJECT_DIR / "input" / "queries"
DATA_ROOT = PROJECT_DIR / "input" / "plaintext"

SEED = 42
EDGE_RATIO = 5  # txns per account; fixed by generate_banking_scaled.py

# --- Banking W1 size table (auto-generated; edges = EDGE_RATIO * accounts) ---
# Order matters: smallest to largest so failures surface fast.
BANKING_SIZES = [
    {"label":  "10k",  "accounts":    10_000, "dir_name": "banking_10k"},
    {"label":  "20k",  "accounts":    20_000, "dir_name": "banking_20k"},
    {"label": "100k",  "accounts":   100_000, "dir_name": "banking_100k"},
    {"label": "200k",  "accounts":   200_000, "dir_name": "banking_200k"},
    {"label":   "1M",  "accounts": 1_000_000, "dir_name": "banking_1M"},
    {"label":   "2M",  "accounts": 2_000_000, "dir_name": "banking_2M"},
]

# --- IBM AML-Data W4 size table (downloaded + converted, NOT auto-generated) ---
# Each variant carries an explicit account/edge count (for the scaling x-axis)
# and a per-dataset anchor bank. The anchor only needs to exist in that dataset
# and keep the output bounded; NebulaDB's latency is dominated by oblivious
# sorts over the full hop table (sized by edge count) and is ~anchor-independent.
# hi_medium / hi_large entries are filled in once their datasets are converted.
AML_SIZES = [
    {"label": "hi_small",  "dir_name": "ibm_aml_hi_small",
     "accounts":   515_088, "edges":  5_078_345, "anchor_bank": 224866},
    {"label": "hi_medium", "dir_name": "ibm_aml_hi_medium",
     "accounts": 2_077_023, "edges": 31_898_238, "anchor_bank": 118871},
    {"label": "hi_large",  "dir_name": "ibm_aml_hi_large",
     "accounts": 2_116_168, "edges": 179_702_229, "anchor_bank": 111545,
     # Opt-in (--slim-hi-large) memory-reduced variant: a column-trimmed txn
     # table (slim dir) joined by the reduced-capacity ./sgx_app_slim binary, so
     # the >=2-hop chain fits in RAM. account.csv is unchanged (symlinked).
     "dir_name_slim": "ibm_aml_hi_large_slim"},
]

# --- SNAP cit-Patents W3 size table (downloaded + converted, NOT auto-generated;
# single full-graph scale — SNAP is not subsampled). `anchor_bank` here holds the
# a1.account_id start node (patents anchor on a node id, not bank_id; ANCHOR_RE
# matches both column names). NebulaDB latency is dominated by the oblivious sort
# over the full 16.5M-edge hop table, so this is a latency-vs-hop point at one
# scale rather than a multi-size sweep. ---
SNAP_SIZES = [
    {"label": "patents", "dir_name": "snap_patents",
     "accounts": 3_774_768, "edges": 16_518_948, "anchor_bank": 3569342},
]

# DEFAULT_SYSTEMS run on a plain invocation. full_mwj_no_filter (unfiltered
# full MWJ via `sgx_app --no-filter`) is opt-in: its output explodes with both
# hop count and dataset size and OOMs at the larger sizes by design.
DEFAULT_SYSTEMS = ["nebuladb", "full_mwj"]
ALL_SYSTEMS = DEFAULT_SYSTEMS + ["full_mwj_no_filter"]
# Both are full-MWJ variants gated by the --full-mwj-max-edges cap.
FULL_MWJ_SYSTEMS = {"full_mwj", "full_mwj_no_filter"}

# Per-workload configuration. A workload selects the one-hop binary and its
# build target, the size table, the chain query set, whether datasets are
# auto-generated, and the default system set. Everything else is shared:
# sgx_app reads each table's schema from its CSV header, and the rewriter keys
# on the generic account/txn/account_id/acc_from/acc_to names both query sets
# share. Mirrors the WORKLOADS pattern in run_e1_main.py.
WORKLOADS = {
    "banking": {
        "onehop_bin": OBLIGRAPH_BUILD / "banking_onehop",
        "onehop_target": "banking_onehop",
        "sizes": BANKING_SIZES,
        "queries": ["banking_1hop", "banking_2hop", "banking_3hop",
                    "banking_4hop", "banking_5hop"],
        "default_queries": ["banking_3hop"],
        "default_systems": DEFAULT_SYSTEMS,
        "generate": True,
    },
    "aml": {
        "onehop_bin": OBLIGRAPH_BUILD / "ibm_aml_onehop",
        "onehop_target": "ibm_aml_onehop",
        "sizes": AML_SIZES,
        "queries": ["aml_1hop", "aml_2hop", "aml_3hop", "aml_4hop", "aml_5hop"],
        "default_queries": ["aml_2hop", "aml_3hop", "aml_4hop", "aml_5hop"],
        "default_systems": ["nebuladb"],
        "generate": False,
    },
    "snap": {
        "onehop_bin": OBLIGRAPH_BUILD / "snap_patents_onehop",
        "onehop_target": "snap_patents_onehop",
        "sizes": SNAP_SIZES,
        "queries": ["snap_1hop", "snap_2hop", "snap_3hop", "snap_4hop", "snap_5hop"],
        "default_queries": ["snap_2hop", "snap_3hop", "snap_4hop", "snap_5hop"],
        "default_systems": ["nebuladb"],
        "generate": False,
    },
}

# `a1.bank_id = <N>` (AML) or `a1.account_id = <N>` (banking) anchor literal.
ANCHOR_RE = re.compile(r"(a1\.(?:bank_id|account_id)\s*=\s*)(\d+)")


# ---------------------------------------------------------------------------
# Stdout parsers (same regexes as E1 runner)
# ---------------------------------------------------------------------------

ONEHOP_TIMING_RE = re.compile(r"TIMING_REPORTED\s+categories=\S+\s+total=([\d.]+)ms")
MWJ_TIMING_RE    = re.compile(r"PHASE_TIMING:[^\n]*Total=([\d.]+)")
RESULT_ROWS_RE   = re.compile(r"Result:\s+(\d+)\s+rows")


def parse_onehop_total_ms(stdout: str) -> float:
    m = ONEHOP_TIMING_RE.search(stdout)
    if not m:
        raise RuntimeError("could not find TIMING_REPORTED line in banking_onehop output")
    return float(m.group(1))


def parse_mwj_total_ms(stdout: str) -> float:
    """PHASE_TIMING Total (seconds) -> ms."""
    m = MWJ_TIMING_RE.search(stdout)
    if not m:
        raise RuntimeError("could not find PHASE_TIMING line in sgx_app output")
    return float(m.group(1)) * 1000.0


def parse_result_rows(stdout: str) -> int:
    matches = RESULT_ROWS_RE.findall(stdout)
    if not matches:
        raise RuntimeError("could not find 'Result: N rows' line")
    return int(matches[-1])


def hop_count(query_name: str) -> int:
    return int(query_name.split("_")[1].rstrip("hop"))


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

class CellTimeout(RuntimeError):
    """Raised when a cell's subprocess exceeds the per-cell wall-clock budget.
    A subclass of RuntimeError so the existing OOM/crash handler also catches it,
    while letting callers distinguish a timeout (recorded as TIMEOUT) from a
    crash/OOM (recorded as OOM)."""


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
    """Run the workload's one-hop driver and place hop.csv in out_dir
    (tempdir-owned). Returns (onehop_ms, rows). hop.csv is consumed by the
    NebulaDB MWJ stage in the same rep and then discarded with the tempdir."""
    hop_csv = out_dir / "hop.csv"
    stdout = run_capture(
        [onehop_bin, data_dir, hop_csv, "--threads", str(threads)],
        log_file=log_file,
    )
    return parse_onehop_total_ms(stdout), parse_result_rows(stdout)


def substitute_anchor(src_sql: Path, anchor_bank, dst_sql: Path):
    """Write src_sql to dst_sql with the `a1.bank_id = <N>` / `a1.account_id =
    <N>` anchor literal replaced by anchor_bank. A no-op copy when anchor_bank
    is None (banking) or already equal. Lets one base query file serve every
    dataset, since each AML variant has a different valid bank anchor."""
    text = src_sql.read_text()
    if anchor_bank is not None:
        text, n = ANCHOR_RE.subn(rf"\g<1>{anchor_bank}", text)
        if n == 0:
            raise RuntimeError(
                f"{src_sql}: no a1.bank_id/account_id anchor to substitute")
    dst_sql.write_text(text)


def run_rewrite(query_path: Path, out_path: Path, log_file):
    """Untimed: rewrite chain query against the hop table."""
    run_capture(["python3", REWRITER, query_path, out_path], log_file=log_file)


def run_mwj(query_path: Path, data_dir: Path, mwj_threads: int, log_file,
            no_filter: bool = False, timeout=None, sgx_app_bin: Path = SGX_APP) -> tuple:
    """Run sgx_app once; tempdir-owned output csv is discarded after parse.
    Returns (mwj_ms, output_rows). Sets OBL_MWJ_SORT_THREADS=mwj_threads in
    the subprocess env to size sgx_app's shared bitonic parallel_sort thread
    pool (read once via magic static in app/data_structures/table.cpp). Pass
    0 to leave the env unset; sgx_app then defaults to hardware_concurrency.

    When no_filter=True, appends --no-filter so sgx_app skips the WHERE-clause
    selection pushdown and computes the full unfiltered multi-way join.

    sgx_app_bin selects the MWJ binary (default ./sgx_app); the slim HI-Large
    path passes ./sgx_app_slim instead."""
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


def prepare_query(spec, query, decomposed_dir, log_file) -> dict:
    """Produce the (anchored) base query and, for hop>=2, its rewritten form
    against the hop table — both cached on disk under decomposed_dir. Untimed.

    For AML each dataset substitutes its own a1.bank_id anchor into the base
    query (files go under decomposed_dir/<dataset>/); for Banking (no anchor)
    the original query file is used directly and the rewrite lands in
    decomposed_dir/<query>.sql (the original layout). full_mwj runs `base`;
    NebulaDB's >=2-hop MWJ runs `decomposed`. Returns {"base", "decomposed"}."""
    base_src = QUERY_DIR / f"{query}.sql"
    anchor = spec.get("anchor_bank")
    if anchor is not None:
        ddir = decomposed_dir / spec["dir_name"]
        ddir.mkdir(parents=True, exist_ok=True)
        base = ddir / f"{query}.base.sql"
        substitute_anchor(base_src, anchor, base)
    else:
        ddir = decomposed_dir
        base = base_src
    decomposed = None
    if hop_count(query) >= 2:
        decomposed = ddir / f"{query}.sql"
        run_rewrite(base, decomposed, log_file)
    return {"base": base, "decomposed": decomposed}


# ---------------------------------------------------------------------------
# Build + dataset prep
# ---------------------------------------------------------------------------

def build_binaries(onehop_target: str, log_file, build_slim: bool = False):
    print(f"[build] obligraph/{onehop_target} (Release)...", flush=True)
    run_capture(
        ["cmake", "--build", OBLIGRAPH_BUILD,
         "--target", onehop_target, "--config", "Release"],
        log_file=log_file,
    )
    targets = [(["make"], "sgx_app")]
    if build_slim:
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


def edge_count(spec) -> int:
    """Edges for a size spec: explicit `edges` (AML) or EDGE_RATIO*accounts
    (Banking, where the generator fixes the ratio)."""
    return spec.get("edges", EDGE_RATIO * spec["accounts"])


def dataset_is_valid(data_dir: Path, accounts: int) -> bool:
    """Cheap row-count check for Banking (generated); trust seed=42 for content."""
    acc_csv = data_dir / "account.csv"
    txn_csv = data_dir / "txn.csv"
    own_csv = data_dir / "owner.csv"
    if not (acc_csv.exists() and txn_csv.exists() and own_csv.exists()):
        return False
    def lc(p): return sum(1 for _ in open(p))
    return (
        lc(acc_csv) == accounts + 1
        and lc(txn_csv) == EDGE_RATIO * accounts + 1
        and lc(own_csv) == accounts // EDGE_RATIO + 1
    )


def aml_dataset_present(data_dir: Path) -> bool:
    """AML datasets are downloaded+converted (not generated); just require the
    account.csv / txn.csv pair to exist. Row counts are recorded in the size
    table for the x-axis and not re-validated here."""
    return (data_dir / "account.csv").exists() and (data_dir / "txn.csv").exists()


def generate_dataset(spec, log_file):
    data_dir = DATA_ROOT / spec["dir_name"]
    if dataset_is_valid(data_dir, spec["accounts"]):
        print(f"[gen]  {spec['label']:>5}: existing dataset matches expected sizes — skipping")
        return
    print(f"[gen]  {spec['label']:>5}: generating {spec['accounts']:,} accounts -> {data_dir}")
    if data_dir.exists():
        shutil.rmtree(data_dir)
    t0 = time.time()
    run_capture(
        ["python3", GENERATOR, str(spec["accounts"]), str(data_dir), "--seed", str(SEED)],
        log_file=log_file,
    )
    print(f"[gen]  {spec['label']:>5}: done in {time.time() - t0:.1f}s")


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def collect_metadata(args, wf, sizes_run) -> dict:
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
        "workload": args.workload,
        "args": vars(args),
        "sizes_run": sizes_run,
        "seed": SEED,
        "edge_ratio": EDGE_RATIO,
        "mwj_env": (
            {"OBL_MWJ_SORT_THREADS": str(args.mwj_threads)}
            if args.mwj_threads and args.mwj_threads > 0 else {}
        ),
        "binaries": {"sgx_app": str(SGX_APP), "onehop": str(wf["onehop_bin"])},
        "note": (
            "one-hop runs once per (size, rep), not per cell, and is reused "
            "across all chain queries (hops) at that size since the hop table is "
            "the same. Per-run query results (hop.csv, sgx_app output) are "
            "tempdir-owned and discarded after parsing. AML chain queries are "
            "anchored per dataset (a1.bank_id substituted from the size table). "
            "full_mwj_no_filter runs sgx_app with --no-filter and explodes with "
            "dataset size; both full-MWJ variants are gated by "
            "--full-mwj-max-edges. Any binary OOM/crash/timeout is caught per "
            "cell (recorded output_rows=OOM/TIMEOUT, total_ms empty) and never "
            "aborts the sweep; once a system fails at hop K for a dataset, that "
            "dataset's higher hops are SKIPPED, and once a full-MWJ system fails "
            "at E edges, larger sizes are SKIPPED."
        ),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workload", default="banking", choices=sorted(WORKLOADS),
                   help="Workload family selecting the one-hop binary, size "
                        "table, chain queries, dataset handling, and default "
                        "systems (default: banking). 'aml' = IBM AML-Data W4 "
                        "(HI-Small/Medium/Large); 'snap' = SNAP cit-Patents W3 "
                        "(single full-graph scale). Both downloaded/converted "
                        "and NebulaDB-focused.")
    p.add_argument("--queries", default=None,
                   help="Comma-separated chain queries; hops are looped within "
                        "each dataset (so an OOM at hop K skips that dataset's "
                        "higher hops). Default: the workload's set (banking: "
                        "banking_3hop; aml: aml_2hop..aml_5hop).")
    p.add_argument("--query", default=None,
                   help="Single-query convenience alias for --queries.")
    p.add_argument("--sizes", default=None,
                   help="Comma-separated size labels, smallest-first. "
                        "Default: every size in the workload's table.")
    p.add_argument("--systems", default=None,
                   help=f"Comma-separated systems (allowed: {','.join(ALL_SYSTEMS)}). "
                        f"Default: the workload's set (banking: "
                        f"{','.join(DEFAULT_SYSTEMS)}; aml: nebuladb). "
                        f"'full_mwj_no_filter' is the unfiltered baseline.")
    p.add_argument("--full-mwj-max-edges", type=int, default=1_000_000,
                   help="Skip the full-MWJ variants (full_mwj, full_mwj_no_filter) "
                        "for sizes with more than this many edges (default: "
                        "1,000,000). NebulaDB always runs at every size.")
    p.add_argument("--measurement-runs", type=int, default=1,
                   help="Recorded measurement runs per cell (default: 1)")
    p.add_argument("--warmup-runs", type=int, default=1,
                   help="Discarded warm-up runs per cell (default: 1)")
    p.add_argument("--onehop-threads", type=int, default=32,
                   help="Threads passed to the one-hop driver --threads (default: 32).")
    p.add_argument("--mwj-threads", type=int, default=64,
                   help="Workers in sgx_app's shared bitonic parallel_sort thread "
                        "pool, passed via the OBL_MWJ_SORT_THREADS env var "
                        "(default: 64). Set to 0 to leave the env unset; sgx_app "
                        "then defaults to std::thread::hardware_concurrency().")
    p.add_argument("--cell-timeout", type=int, default=0,
                   help="Per-cell wall-clock budget in seconds for each MWJ run "
                        "(default: 0 = no limit). A cell that exceeds it is "
                        "recorded as TIMEOUT and, like an OOM, skips that "
                        "dataset's higher hops. Use for the large AML datasets.")
    p.add_argument("--slim-hi-large", action="store_true",
                   help="For the AML hi_large size only: use the column-trimmed "
                        "slim dataset (ibm_aml_hi_large_slim) and the "
                        "reduced-capacity ./sgx_app_slim binary, so the >=2-hop "
                        "chain fits in RAM. Generate the slim dataset first with "
                        "scripts/make_slim_hi_large.py. No effect on other sizes.")
    p.add_argument("--skip-build", action="store_true",
                   help="Skip binary rebuild step")
    p.add_argument("--skip-generation", action="store_true",
                   help="Skip dataset (re)generation; fail if any dataset is missing/wrong-size")
    p.add_argument("--output-dir", default=str(RESULTS_DIR),
                   help=f"Output directory (default: {RESULTS_DIR})")
    args = p.parse_args()

    wf = WORKLOADS[args.workload]
    cell_timeout = args.cell_timeout or None  # 0 -> None (no limit)

    # Resolve sizes (workload-specific table)
    known = {s["label"]: s for s in wf["sizes"]}
    if args.sizes:
        wanted_labels = [s.strip() for s in args.sizes.split(",") if s.strip()]
        unknown = [l for l in wanted_labels if l not in known]
        if unknown:
            sys.exit(f"unknown size labels for workload {args.workload}: "
                     f"{unknown} (allowed: {list(known)})")
        sizes = [known[l] for l in wanted_labels]
    else:
        sizes = list(wf["sizes"])

    # --slim-hi-large: swap any selected spec that defines a slim variant onto
    # the trimmed dataset + reduced-capacity binary. Done on a per-spec copy so
    # the shared WORKLOADS size table is never mutated. Only specs carrying
    # "dir_name_slim" are affected (currently just AML hi_large).
    if args.slim_hi_large:
        slim_specs = []
        applied = []
        for spec in sizes:
            if "dir_name_slim" in spec:
                slim = dict(spec)
                slim["dir_name"] = spec["dir_name_slim"]
                slim["_sgx_app"] = SGX_APP_SLIM
                slim_specs.append(slim)
                applied.append(spec["label"])
            else:
                slim_specs.append(spec)
        if not applied:
            sys.exit("--slim-hi-large set but none of the selected sizes "
                     f"{[s['label'] for s in sizes]} has a slim variant "
                     "(expected e.g. hi_large)")
        sizes = slim_specs

    # Resolve systems
    if args.systems:
        systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    else:
        systems = list(wf["default_systems"])
    for s in systems:
        if s not in ALL_SYSTEMS:
            sys.exit(f"unknown system: {s} (allowed: {ALL_SYSTEMS})")

    # Resolve queries (sorted ascending by hop so the per-dataset OOM/timeout
    # short-circuit skips strictly higher hops).
    if args.queries:
        queries = [q.strip() for q in args.queries.split(",") if q.strip()]
    elif args.query:
        queries = [args.query]
    else:
        queries = list(wf["default_queries"])
    for q in queries:
        if q not in wf["queries"]:
            sys.exit(f"unknown query for workload {args.workload}: {q} "
                     f"(allowed: {wf['queries']})")
        if not (QUERY_DIR / f"{q}.sql").is_file():
            sys.exit(f"query not found: {QUERY_DIR / (q + '.sql')}")
    queries = sorted(queries, key=hop_count)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "binary_stdout.log"
    raw_csv_path = out_dir / "raw_runs.csv"
    summary_csv_path = out_dir / "summary.csv"
    meta_path = out_dir / "run_metadata.json"

    meta = collect_metadata(args, wf, [s["label"] for s in sizes])

    print(f"E2 runner: workload={args.workload}")
    print(f"  queries: {queries}")
    print(f"  sizes  : {[s['label'] for s in sizes]}")
    print(f"  systems: {systems}")
    print(f"  warm-up: {args.warmup_runs}   measurement: {args.measurement_runs}")
    print(f"  full_mwj cap (edges): {args.full_mwj_max_edges:,}")
    print(f"  cell timeout: {cell_timeout or '(none)'}")
    print(f"  mwj_env: {meta['mwj_env'] or '(unset — sgx_app uses hardware_concurrency)'}")
    print(f"  output : {out_dir}")
    print()

    rows = []
    total_reps = args.warmup_runs + args.measurement_runs
    needs_onehop = "nebuladb" in systems

    # Per-(system, query) smallest edge count at which a full-MWJ variant OOM'd:
    # sizes ascend, so it cannot succeed at any larger size for that query.
    failed_at_edges = {}
    # Per-(dataset, system) smallest hop that OOM'd / timed out: hops ascend
    # within a dataset, so strictly higher hops are skipped for that dataset
    # only (other datasets stay independent).
    failed_at_hop = {}

    with open(log_path, "w") as log_file:
        if not args.skip_build:
            build_binaries(wf["onehop_target"], log_file,
                           build_slim=args.slim_hi_large)
        else:
            required = [(wf["onehop_bin"], wf["onehop_target"]), (SGX_APP, "sgx_app")]
            if args.slim_hi_large:
                required.append((SGX_APP_SLIM, "sgx_app_slim"))
            for bin_path, lbl in required:
                if not bin_path.exists():
                    sys.exit(f"--skip-build but {lbl} missing: {bin_path}")

        # Dataset prep up-front, smallest-first.
        for spec in sizes:
            data_dir = DATA_ROOT / spec["dir_name"]
            if wf["generate"]:
                if args.skip_generation:
                    if not dataset_is_valid(data_dir, spec["accounts"]):
                        sys.exit(f"--skip-generation but {data_dir} is missing/wrong size")
                else:
                    generate_dataset(spec, log_file)
            elif not aml_dataset_present(data_dir):  # AML: downloaded+converted
                sys.exit(f"dataset not found: {data_dir} (download + convert it first)")

        # Anchored base + rewritten query per (dataset, query). Untimed, cached.
        decomposed_dir = out_dir / "decomposed"
        decomposed_dir.mkdir(exist_ok=True)
        prepared = {}
        for spec in sizes:
            for query in queries:
                prepared[(spec["dir_name"], query)] = prepare_query(
                    spec, query, decomposed_dir, log_file)

        # Sweep
        for rep_idx in range(total_reps):
            is_warmup = rep_idx < args.warmup_runs
            run_id = rep_idx - args.warmup_runs + 1  # 1..N when measured
            label = "warm" if is_warmup else f"run{run_id}"
            print(f"--- rep {rep_idx+1}/{total_reps} ({label}) ---", flush=True)

            for spec in sizes:
                dataset = spec["dir_name"]
                data_dir = DATA_ROOT / dataset
                edges = edge_count(spec)

                # One-hop once per (size, rep); reused across all hops at this
                # size (the hop table is the same). tempdir-owned hop.csv.
                with tempfile.TemporaryDirectory() as tmp_root:
                    hop_dir = Path(tmp_root)
                    onehop_ms, onehop_rows = (None, None)
                    if needs_onehop:
                        print(f"  [{spec['label']:>9}] one-hop ...", end="", flush=True)
                        t0 = time.time()
                        onehop_ms, onehop_rows = run_onehop(
                            wf["onehop_bin"], data_dir, hop_dir,
                            args.onehop_threads, log_file)
                        print(f" total={onehop_ms:.1f}ms rows={onehop_rows} "
                              f"({time.time()-t0:.1f}s wall)")

                    for query in queries:
                        hk = hop_count(query)
                        prep = prepared[(dataset, query)]
                        for system in systems:
                            cell_total_ms = None
                            cell_onehop_ms = ""
                            cell_mwj_ms = ""
                            cell_rows = None

                            # Skip reasons (recorded with empty total_ms):
                            #   cap  — full-MWJ variant above --full-mwj-max-edges
                            #   size — full-MWJ already OOM'd at a smaller size (this query)
                            #   hop  — this system already failed at a lower hop (this dataset)
                            cap_skip = (system in FULL_MWJ_SYSTEMS
                                        and edges > args.full_mwj_max_edges)
                            size_skip = ((system, query) in failed_at_edges
                                         and edges >= failed_at_edges[(system, query)])
                            hop_skip = ((dataset, system) in failed_at_hop
                                        and hk >= failed_at_hop[(dataset, system)])

                            if cap_skip or size_skip or hop_skip:
                                reason = ("cap" if cap_skip else
                                          "OOM at smaller size" if size_skip else
                                          "failed at lower hop")
                                cell_rows = "SKIPPED"
                                print(f"  [{spec['label']:>9}] {query:10s} {system:18s} "
                                      f"{label} -> SKIPPED ({reason})", flush=True)
                            else:
                                # Every binary call is OOM/timeout-tolerant: a crash,
                                # OOM-kill (non-zero exit -> RuntimeError) or timeout
                                # (CellTimeout) is recorded and the sweep continues.
                                try:
                                    if system == "nebuladb":
                                        if hk == 1:
                                            cell_total_ms = onehop_ms
                                            cell_onehop_ms = onehop_ms
                                            cell_rows = onehop_rows
                                        else:
                                            mwj_ms, mwj_rows = run_mwj(
                                                prep["decomposed"], hop_dir,
                                                args.mwj_threads, log_file,
                                                timeout=cell_timeout,
                                                sgx_app_bin=spec.get("_sgx_app", SGX_APP))
                                            cell_total_ms = onehop_ms + mwj_ms
                                            cell_onehop_ms = onehop_ms
                                            cell_mwj_ms = mwj_ms
                                            cell_rows = mwj_rows
                                    elif system == "full_mwj":
                                        mwj_ms, mwj_rows = run_mwj(
                                            prep["base"], data_dir,
                                            args.mwj_threads, log_file,
                                            timeout=cell_timeout,
                                            sgx_app_bin=spec.get("_sgx_app", SGX_APP))
                                        cell_total_ms = mwj_ms
                                        cell_mwj_ms = mwj_ms
                                        cell_rows = mwj_rows
                                    elif system == "full_mwj_no_filter":
                                        mwj_ms, mwj_rows = run_mwj(
                                            prep["base"], data_dir,
                                            args.mwj_threads, log_file,
                                            no_filter=True, timeout=cell_timeout,
                                            sgx_app_bin=spec.get("_sgx_app", SGX_APP))
                                        cell_total_ms = mwj_ms
                                        cell_mwj_ms = mwj_ms
                                        cell_rows = mwj_rows
                                except RuntimeError as e:
                                    kind = "TIMEOUT" if isinstance(e, CellTimeout) else "OOM"
                                    log_file.write(f"\n!!! {system} {kind} at "
                                                   f"{spec['label']} {query}: {e}\n")
                                    log_file.flush()
                                    failed_at_hop[(dataset, system)] = min(
                                        failed_at_hop.get((dataset, system), hk), hk)
                                    if system in FULL_MWJ_SYSTEMS:
                                        failed_at_edges[(system, query)] = min(
                                            failed_at_edges.get((system, query), edges), edges)
                                    cell_rows = kind

                                total_str = (f"{cell_total_ms:.1f}ms" if cell_total_ms
                                             is not None else str(cell_rows))
                                print(f"  [{spec['label']:>9}] {query:10s} {system:18s} "
                                      f"{label} -> total={total_str} rows={cell_rows}",
                                      flush=True)

                            rows.append({
                                "system": system,
                                "query": query,
                                "dataset": dataset,
                                "num_accounts": spec["accounts"],
                                "num_edges": edges,
                                "run_id": run_id,
                                "is_warmup": int(is_warmup),
                                "total_ms": cell_total_ms if cell_total_ms is not None else "",
                                "onehop_ms": cell_onehop_ms,
                                "mwj_ms": cell_mwj_ms,
                                "output_rows": cell_rows if cell_rows is not None else "",
                            })

    # raw_runs.csv (everything)
    fieldnames = ["system", "query", "dataset", "num_accounts", "num_edges",
                  "run_id", "is_warmup", "total_ms", "onehop_ms", "mwj_ms",
                  "output_rows"]
    with open(raw_csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # summary.csv (measurement runs only; skipped cells excluded)
    by_cell = {}
    for r in rows:
        if r["is_warmup"]:
            continue
        if r["total_ms"] == "" or r["total_ms"] is None:
            continue
        key = (r["system"], r["query"], r["dataset"],
               r["num_accounts"], r["num_edges"])
        by_cell.setdefault(key, []).append(r)

    def _med(vals):
        vals = [v for v in vals if v not in ("", None)]
        return statistics.median(vals) if vals else ""

    summary_fields = [
        "system", "query", "dataset", "num_accounts", "num_edges", "n_runs",
        "median_total_ms", "median_onehop_ms", "median_mwj_ms",
        "min_total_ms", "max_total_ms", "stddev_total_ms", "output_rows",
    ]
    with open(summary_csv_path, "w", newline="") as f:
        sw = csv.DictWriter(f, fieldnames=summary_fields)
        sw.writeheader()
        for key, cell in sorted(by_cell.items(),
                                key=lambda kv: (kv[0][3], kv[0][1], kv[0][0])):
            system, q, dataset, na, ne = key
            totals = [c["total_ms"] for c in cell]
            n = len(totals)
            sw.writerow({
                "system": system, "query": q, "dataset": dataset,
                "num_accounts": na, "num_edges": ne,
                "n_runs": n,
                "median_total_ms": statistics.median(totals),
                "median_onehop_ms": _med([c["onehop_ms"] for c in cell]),
                "median_mwj_ms":    _med([c["mwj_ms"]    for c in cell]),
                "min_total_ms": min(totals),
                "max_total_ms": max(totals),
                "stddev_total_ms": statistics.stdev(totals) if n >= 2 else 0.0,
                "output_rows": cell[0]["output_rows"],
            })

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

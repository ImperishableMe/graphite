#!/usr/bin/env python3
"""
E1 main-result runner (chain queries only, reduced scope).

Supports three workloads via --workload: `banking` (W1, default), `aml`
(IBM AML-Data W4), and `snap` (SNAP cit-Patents W3). The workload selects the
one-hop binary, the five chain queries, the obliviator src.txt converter, and
the default dataset; everything else is workload-agnostic (sgx_app reads schema
from CSV headers; the rewriter keys on the shared account/txn column names —
W3/W4 deliberately reuse W1's account/txn/acc_from/acc_to naming).

Compares three systems on the five chain queries at a single dataset scale:

  1. NebulaDB
       - 1-hop  : `banking_onehop` alone (its output IS the 1-hop join).
       - >= 2-hop : rewrite the chain query against the pre-built hop
                    table -> `sgx_app` on the rewritten query.
     Per-cell latency = `mwj_ms` for the rewritten-query run, plus the
     amortized one-hop cost (inherited from the per-rep one-hop run).
  2. Full MWJ
       - `sgx_app` directly on the chain query (no decomposition).
     Per-cell latency = `mwj_ms`.
  3. Obliviator chained
       - 1-hop  : `obliviator_1hop_chained` (FK kernel, two pairwise joins).
       - >= 2-hop : `obliviator_khop_chained` (NFK kernel, 2K pairwise joins).
     Per-cell latency = the binary's reported `OBLIVIOUS WORK` (1-hop) or
     `online_sec` (K-hop). Both binaries consume the same combined `src.txt`
     produced by `convert_banking_1hop.py`, generated once per dataset.
     Caveat: at threads>=2 the NFK kernel has a known pairing bug at
     genuine-NFK steps (K>=2) — rowcount correct, tuple identities scrambled.
     See docs/obliviator_baseline_status.md.

The one-hop step is run **once per measurement repetition**, NOT per cell:
the hop table is a per-dataset constant, identical regardless of which
chain query we run on top of it. Running it inside every cell would just
re-do the same FK join. The single per-rep run gives both:
  - the `onehop_ms` value inherited by every >= 2-hop NebulaDB cell, and
  - the latency reported for the 1-hop NebulaDB cell itself.

Per (system, query) cell: `--warmup-runs` discarded + `--measurement-runs`
recorded. Loop order: outer = repetition, inner = (query, system).
This keeps the one-hop matched run-by-run with the MWJ measurements it's
paired with. Strictly sequential; full machine for every run.

Correctness is checked separately by `tests/test_e1_chain_correctness.py`.

Outputs (under results/e1_main/):
  raw_runs.csv          every run, including warm-ups
  summary.csv           measurement runs only, per cell: n, median, min, max, stddev, output_rows
  run_metadata.json     commit, host, nproc, build flags, settings
  binary_stdout.log     full stdout from every invocation

Usage:
  python3 scripts/experiments/run_e1_main.py
  python3 scripts/experiments/run_e1_main.py --dataset banking_5k --skip-build
  python3 scripts/experiments/run_e1_main.py --measurement-runs 5
  python3 scripts/experiments/run_e1_main.py --queries banking_1hop,banking_3hop
  python3 scripts/experiments/run_e1_main.py --workload aml   # IBM AML-Data W4
  python3 scripts/experiments/run_e1_main.py --workload snap  # SNAP cit-Patents W3
"""

import argparse
import csv
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_DIR / "results" / "e1_main"

SGX_APP = PROJECT_DIR / "sgx_app"
OBLIGRAPH_BUILD = PROJECT_DIR / "obligraph" / "build"
REWRITER = PROJECT_DIR / "scripts" / "rewrite_chain_query.py"
QUERY_DIR = PROJECT_DIR / "input" / "queries"
DATA_ROOT = PROJECT_DIR / "input" / "plaintext"

OBL_FK_DIR  = PROJECT_DIR / "obl-radix" / "baselines" / "obliviatorFK-TDX"
OBL_NFK_DIR = PROJECT_DIR / "obl-radix" / "baselines" / "obliviatorNFK-TDX"
OBL_1HOP_BIN = OBL_FK_DIR  / "obliviator_1hop_chained"
OBL_KHOP_BIN = OBL_NFK_DIR / "obliviator_khop_chained"
CONVERT_BANKING_1HOP = OBL_FK_DIR / "convert_banking_1hop.py"
CONVERT_AML_1HOP = OBL_FK_DIR / "convert_aml_1hop.py"
CONVERT_PATENTS_1HOP = OBL_FK_DIR / "convert_patents_1hop.py"

# Per-workload configuration. A workload selects the one-hop binary (NebulaDB's
# decomposed-hop driver), its CMake build target, the chain query set, the
# obliviator src.txt converter, and a default dataset directory. Everything else
# (sgx_app, the rewriter, the obliviator chained binaries) is workload-agnostic:
# sgx_app reads each table's schema from its CSV header, and the rewriter keys on
# the generic account/txn/account_id/acc_from/acc_to names both query sets share.
WORKLOADS = {
    "banking": {
        "onehop_bin": OBLIGRAPH_BUILD / "banking_onehop",
        "onehop_target": "banking_onehop",
        "queries": ["banking_1hop", "banking_2hop", "banking_3hop",
                    "banking_4hop", "banking_5hop"],
        "obl_converter": CONVERT_BANKING_1HOP,
        "default_dataset": "banking_1M",
    },
    "aml": {
        "onehop_bin": OBLIGRAPH_BUILD / "ibm_aml_onehop",
        "onehop_target": "ibm_aml_onehop",
        "queries": ["aml_1hop", "aml_2hop", "aml_3hop", "aml_4hop", "aml_5hop"],
        "obl_converter": CONVERT_AML_1HOP,
        "default_dataset": "ibm_aml_hi_small",
    },
    "snap": {
        "onehop_bin": OBLIGRAPH_BUILD / "snap_patents_onehop",
        "onehop_target": "snap_patents_onehop",
        "queries": ["snap_1hop", "snap_2hop", "snap_3hop", "snap_4hop", "snap_5hop"],
        "obl_converter": CONVERT_PATENTS_1HOP,
        "default_dataset": "snap_patents",
    },
}

# DEFAULT_SYSTEMS run on a plain invocation. `full_mwj_no_filter` (the
# unfiltered full MWJ baseline) is opt-in only: its output explodes
# exponentially with hop count, so it is in the validation allowlist but NOT in
# the default set.
DEFAULT_SYSTEMS = ["nebuladb", "full_mwj", "obliviator_chained"]
ALL_SYSTEMS = DEFAULT_SYSTEMS + ["full_mwj_no_filter"]


# ---------------------------------------------------------------------------
# Stdout parsers
# ---------------------------------------------------------------------------

# banking_onehop:  "TIMING_REPORTED categories=ONLINE total=110.298ms"
ONEHOP_TIMING_RE = re.compile(r"TIMING_REPORTED\s+categories=\S+\s+total=([\d.]+)ms")
# sgx_app:         "PHASE_TIMING: Bottom-Up=... Total=0.047904"  (seconds)
MWJ_TIMING_RE    = re.compile(r"PHASE_TIMING:[^\n]*Total=([\d.]+)")
# both:            "Result: 2 rows" (sgx_app); "Result: 1000 rows, 11 columns" (banking_onehop)
RESULT_ROWS_RE   = re.compile(r"Result:\s+(\d+)\s+rows")
# obliviator_1hop_chained: "  OBLIVIOUS WORK         : 0.786334 s   [...]"
OBL_1HOP_TIME_RE = re.compile(r"^\s*OBLIVIOUS WORK\s+:\s+([\d.]+)\s+s", re.MULTILINE)
OBL_1HOP_ROWS_RE = re.compile(r"^\s*final rows\s+:\s+(\d+)", re.MULTILINE)
# obliviator_khop_chained: "  online_sec  (sum on-clock)  : 0.020014   [...]"
OBL_KHOP_TIME_RE = re.compile(r"^\s*online_sec\s+\(sum on-clock\)\s+:\s+([\d.]+)", re.MULTILINE)
OBL_KHOP_ROWS_RE = re.compile(r"^\s*final_rows\s+:\s+(\d+)", re.MULTILINE)


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


def parse_obliviator_total_ms(stdout: str, K: int) -> float:
    """Parse the obliviator chained binary's online-time line (seconds) -> ms.
    K=1 uses obliviator_1hop_chained's `OBLIVIOUS WORK` label; K>=2 uses
    obliviator_khop_chained's `online_sec  (sum on-clock)` label."""
    if K == 1:
        m = OBL_1HOP_TIME_RE.search(stdout)
        label = "OBLIVIOUS WORK"
    else:
        m = OBL_KHOP_TIME_RE.search(stdout)
        label = "online_sec  (sum on-clock)"
    if not m:
        raise RuntimeError(f"could not find '{label}' line in obliviator output")
    return float(m.group(1)) * 1000.0


def parse_obliviator_rows(stdout: str, K: int) -> int:
    if K == 1:
        m = OBL_1HOP_ROWS_RE.search(stdout)
        label = "final rows"
    else:
        m = OBL_KHOP_ROWS_RE.search(stdout)
        label = "final_rows"
    if not m:
        raise RuntimeError(f"could not find '{label}' line in obliviator output")
    return int(m.group(1))


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
# Stage runners (each returns just what its caller needs)
# ---------------------------------------------------------------------------

def run_onehop(onehop_bin: Path, data_dir: Path, out_dir: Path, threads: int,
               log_file) -> tuple:
    """Run the workload's one-hop driver and place hop.csv in out_dir.
    Returns (onehop_ms, rows)."""
    hop_csv = out_dir / "hop.csv"
    stdout = run_capture(
        [onehop_bin, data_dir, hop_csv, "--threads", str(threads)],
        log_file=log_file,
    )
    return parse_onehop_total_ms(stdout), parse_result_rows(stdout)


def run_rewrite(query_path: Path, out_path: Path, log_file):
    """Untimed: rewrite chain query against the hop table."""
    run_capture(["python3", REWRITER, query_path, out_path], log_file=log_file)


def run_mwj(query_path: Path, data_dir: Path, mwj_threads: int, log_file,
            no_filter: bool = False, timeout=None) -> tuple:
    """Run sgx_app once. Returns (mwj_ms, output_rows). Sets
    OBL_MWJ_SORT_THREADS=mwj_threads in the subprocess env to size sgx_app's
    shared bitonic parallel_sort thread pool (read once via magic static in
    app/data_structures/table.cpp). Pass 0 to leave the env unset; sgx_app
    then defaults to std::thread::hardware_concurrency().

    When no_filter=True, appends --no-filter so sgx_app skips the WHERE-clause
    selection pushdown and computes the full unfiltered multi-way join."""
    env_extra = None
    if mwj_threads and mwj_threads > 0:
        env_extra = {"OBL_MWJ_SORT_THREADS": str(mwj_threads)}
    with tempfile.TemporaryDirectory() as tmp:
        out_csv = Path(tmp) / "out.csv"
        cmd = [SGX_APP, query_path, data_dir, out_csv]
        if no_filter:
            cmd.append("--no-filter")
        stdout = run_capture(cmd, log_file=log_file, env_extra=env_extra,
                             timeout=timeout)
    return parse_mwj_total_ms(stdout), parse_result_rows(stdout)


def generate_obliviator_src_txt(converter: Path, data_dir: Path, out_dir: Path,
                                log_file) -> Path:
    """Run the workload's 1-hop converter once to produce src.txt for the
    obliviator chained binaries. The converter also writes a dst.txt (used by
    the join-sort 1-hop variant, not by chained); it lands in the same tmpdir
    and is discarded with it. Returns the path to src.txt."""
    src_path = out_dir / "src.txt"
    dst_path = out_dir / "dst.txt"
    run_capture(
        ["python3", converter,
         data_dir / "account.csv", data_dir / "txn.csv", src_path, dst_path],
        log_file=log_file,
    )
    return src_path


def run_obliviator(K: int, src_txt: Path, threads: int, log_file,
                   timeout=None) -> tuple:
    """Run obliviator_1hop_chained (K=1) or obliviator_khop_chained (K>=2).
    Returns (online_ms, output_rows). Output CSV is written to a tempdir that
    is discarded on exit."""
    with tempfile.TemporaryDirectory() as tmp:
        out_csv = Path(tmp) / "obl_out.csv"
        if K == 1:
            cmd = [OBL_1HOP_BIN, str(threads), src_txt, out_csv]
        else:
            cmd = [OBL_KHOP_BIN, str(threads), str(K), src_txt, out_csv]
        stdout = run_capture(cmd, log_file=log_file, timeout=timeout)
    return parse_obliviator_total_ms(stdout, K), parse_obliviator_rows(stdout, K)


# ---------------------------------------------------------------------------
# Build helpers
# ---------------------------------------------------------------------------

def build_binaries(onehop_target: str, log_file):
    print(f"[build] obligraph/{onehop_target} (Release)...", flush=True)
    run_capture(
        ["cmake", "--build", OBLIGRAPH_BUILD,
         "--target", onehop_target, "--config", "Release"],
        log_file=log_file,
    )
    print("[build] sgx_app (make)...", flush=True)
    proc = subprocess.run(["make"], cwd=PROJECT_DIR, capture_output=True, text=True)
    log_file.write("\n+++ make (sgx_app)\n" + proc.stdout)
    if proc.stderr:
        log_file.write("\n--- stderr ---\n" + proc.stderr)
    log_file.flush()
    if proc.returncode != 0:
        raise RuntimeError(f"sgx_app build failed:\n{proc.stderr}")

    print("[build] obliviator_1hop_chained (FK)...", flush=True)
    run_capture(
        ["make", "-C", OBL_FK_DIR, "-f", "Makefile.standalone",
         "obliviator_1hop_chained"],
        log_file=log_file,
    )
    print("[build] obliviator_khop_chained (NFK)...", flush=True)
    run_capture(
        ["make", "-C", OBL_NFK_DIR, "-f", "Makefile.standalone",
         "obliviator_khop_chained"],
        log_file=log_file,
    )


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def collect_metadata(args, wf: dict) -> dict:
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
        "mwj_env": (
            {"OBL_MWJ_SORT_THREADS": str(args.mwj_threads)}
            if args.mwj_threads and args.mwj_threads > 0 else {}
        ),
        "binaries": {
            "sgx_app": str(SGX_APP),
            "onehop": str(wf["onehop_bin"]),
            "obliviator_1hop_chained": str(OBL_1HOP_BIN),
            "obliviator_khop_chained": str(OBL_KHOP_BIN),
            "obliviator_converter": str(wf["obl_converter"]),
        },
        "note": (
            "one-hop runs once per repetition (not per cell); its result is "
            "reused for all >=2-hop NebulaDB cells in that rep and reported "
            "as the latency of the 1-hop NebulaDB cell. obliviator src.txt "
            "is generated once per dataset and reused across all reps and "
            "queries (deterministic). full_mwj_no_filter runs sgx_app with "
            "--no-filter (no WHERE-clause selection pushdown), computing the "
            "full unfiltered multi-way join; like obliviator_chained it computes "
            "the unfiltered chain join and explodes with hop count. Any binary "
            "OOM/crash is caught per cell (recorded as output_rows=OOM, "
            "total_ms empty) and never aborts the sweep; once a system fails at "
            "hop K its higher hops are recorded SKIPPED."
        ),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workload", default="banking", choices=sorted(WORKLOADS),
                   help="Workload family selecting the one-hop binary, chain "
                        "queries, obliviator converter, and default dataset "
                        "(default: banking). 'aml' = IBM AML-Data W4; "
                        "'snap' = SNAP cit-Patents W3.")
    p.add_argument("--dataset", default=None,
                   help="Dataset name under input/plaintext/ (default: the "
                        "workload's default dataset).")
    p.add_argument("--queries", default=None,
                   help="Comma-separated query names (default: the workload's "
                        "five chain queries).")
    p.add_argument("--systems", default=",".join(DEFAULT_SYSTEMS),
                   help=f"Comma-separated systems (default: {','.join(DEFAULT_SYSTEMS)}; "
                        f"allowed: {','.join(ALL_SYSTEMS)}). 'full_mwj_no_filter' is the "
                        f"unfiltered full MWJ baseline (opt-in; output explodes with hops).")
    p.add_argument("--measurement-runs", type=int, default=1,
                   help="Recorded measurement runs per cell (default: 1)")
    p.add_argument("--warmup-runs", type=int, default=1,
                   help="Discarded warm-up runs per cell (default: 1)")
    p.add_argument("--onehop-threads", type=int, default=64,
                   help="Threads passed to the workload's one-hop driver "
                        "--threads (default: 64).")
    p.add_argument("--mwj-threads", type=int, default=64,
                   help="Workers in sgx_app's shared bitonic parallel_sort thread "
                        "pool, passed via the OBL_MWJ_SORT_THREADS env var "
                        "(default: 64). Set to 0 to leave the env unset; sgx_app "
                        "then defaults to std::thread::hardware_concurrency().")
    p.add_argument("--obliviator-threads", type=int, default=64,
                   help="Threads passed to obliviator_1hop_chained / "
                        "obliviator_khop_chained (default: 64). Both binaries "
                        "take this as their first CLI arg. At threads>=2 the "
                        "NFK kernel (K>=2) has a documented pairing bug that "
                        "keeps the row count correct but scrambles tuple "
                        "identities at genuine-NFK steps — see "
                        "docs/obliviator_baseline_status.md.")
    p.add_argument("--cell-timeout", type=int, default=0,
                   help="Per-cell wall-clock budget in seconds for each MWJ / "
                        "obliviator binary run (default: 0 = no limit). A cell "
                        "that exceeds it is recorded as output_rows=TIMEOUT and, "
                        "like an OOM, causes the system's higher hops to be "
                        "SKIPPED.")
    p.add_argument("--obliviator-timeout", type=int, default=None,
                   help="Separate per-cell budget (seconds) for obliviator_chained "
                        "only (default: inherit --cell-timeout; 0 = no limit). "
                        "The obliviator baseline computes the *unfiltered* chain "
                        "join and runs unboundedly long (rather than OOM) at K>=2, "
                        "so a tight budget here lets it be recorded as TIMEOUT "
                        "quickly while the filtered systems (which complete) keep "
                        "the larger/uncapped --cell-timeout.")
    p.add_argument("--skip-build", action="store_true",
                   help="Skip binary rebuild step")
    p.add_argument("--output-dir", default=str(RESULTS_DIR),
                   help=f"Output directory (default: {RESULTS_DIR})")
    args = p.parse_args()

    wf = WORKLOADS[args.workload]
    if args.dataset is None:
        args.dataset = wf["default_dataset"]
    if args.queries is None:
        args.queries = ",".join(wf["queries"])

    queries = args.queries.split(",")
    systems = args.systems.split(",")
    cell_timeout = args.cell_timeout or None  # 0 -> None (no limit)
    obliviator_timeout = (cell_timeout if args.obliviator_timeout is None
                          else (args.obliviator_timeout or None))
    data_dir = DATA_ROOT / args.dataset
    if not data_dir.is_dir():
        sys.exit(f"dataset not found: {data_dir}")
    for q in queries:
        if not (QUERY_DIR / f"{q}.sql").is_file():
            sys.exit(f"query not found: {QUERY_DIR}/{q}.sql")
    for s in systems:
        if s not in ALL_SYSTEMS:
            sys.exit(f"unknown system: {s} (allowed: {ALL_SYSTEMS})")

    needs_onehop = "nebuladb" in systems
    needs_mwj = ("full_mwj" in systems) or ("full_mwj_no_filter" in systems) or ("nebuladb" in systems and any(hop_count(q) >= 2 for q in queries))
    needs_obliviator = "obliviator_chained" in systems

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "binary_stdout.log"
    raw_csv_path = out_dir / "raw_runs.csv"
    summary_csv_path = out_dir / "summary.csv"
    meta_path = out_dir / "run_metadata.json"

    meta = collect_metadata(args, wf)

    print(f"E1 runner: workload={args.workload} dataset={args.dataset}")
    print(f"  queries: {queries}")
    print(f"  systems: {systems}")
    print(f"  warm-up: {args.warmup_runs}   measurement: {args.measurement_runs}")
    print(f"  mwj_env: {meta['mwj_env'] or '(unset — sgx_app uses hardware_concurrency)'}")
    print(f"  output : {out_dir}")
    print()

    rows = []
    total_reps = args.warmup_runs + args.measurement_runs

    # Per-system lowest hop at which a binary OOM'd / crashed. Systems whose
    # output grows monotonically with hop count (obliviator_chained,
    # full_mwj_no_filter — both compute the *unfiltered* full chain join) cannot
    # succeed at a higher hop once they fail at a lower one, so we skip those
    # rather than re-grinding to the same kill. Persisted across reps/queries.
    failed_at_hop = {}

    with open(log_path, "w") as log_file:
        if not args.skip_build:
            build_binaries(wf["onehop_target"], log_file)

        # Pre-compute rewritten SQL once per query (rewriter is deterministic, untimed).
        # These live under out_dir/decomposed/ so they're inspectable after the run.
        decomposed_dir = out_dir / "decomposed"
        decomposed_dir.mkdir(exist_ok=True)
        decomposed_for = {}
        if "nebuladb" in systems:
            for q in queries:
                if hop_count(q) >= 2:
                    dpath = decomposed_dir / f"{q}.sql"
                    run_rewrite(QUERY_DIR / f"{q}.sql", dpath, log_file)
                    decomposed_for[q] = dpath

        with tempfile.TemporaryDirectory() as tmp_root:
            tmp_root = Path(tmp_root)
            hop_dir = tmp_root / "hop_data"
            hop_dir.mkdir()

            # Generate src.txt once per dataset (deterministic; reused across
            # all reps and queries for the obliviator chained binaries).
            obliviator_src_txt = None
            if needs_obliviator:
                obl_dir = tmp_root / "obliviator_input"
                obl_dir.mkdir()
                print(f"[setup] generating obliviator src.txt from {data_dir} ...",
                      flush=True)
                t0 = time.time()
                obliviator_src_txt = generate_obliviator_src_txt(
                    wf["obl_converter"], data_dir, obl_dir, log_file)
                print(f"  -> {obliviator_src_txt} ({time.time()-t0:.1f}s wall)")

            for rep_idx in range(total_reps):
                is_warmup = rep_idx < args.warmup_runs
                run_id = rep_idx - args.warmup_runs + 1  # 1..N when measured
                label = "warm" if is_warmup else f"run{run_id}"
                print(f"--- rep {rep_idx+1}/{total_reps} ({label}) ---", flush=True)

                # 1. one-hop, once per rep (gives onehop_ms; produces hop.csv reused below).
                onehop_ms, onehop_rows = (None, None)
                if needs_onehop:
                    print(f"  one-hop ...", end="", flush=True)
                    t0 = time.time()
                    onehop_ms, onehop_rows = run_onehop(
                        wf["onehop_bin"], data_dir, hop_dir, args.onehop_threads, log_file)
                    print(f" total={onehop_ms:.1f}ms rows={onehop_rows} "
                          f"({time.time()-t0:.1f}s wall)")

                # 2. Per-query cells.
                for query in queries:
                    qpath = QUERY_DIR / f"{query}.sql"
                    hk = hop_count(query)
                    for system in systems:
                        cell_total_ms = None
                        cell_onehop_ms = ""
                        cell_mwj_ms = ""
                        cell_rows = None

                        # Short-circuit: a system that already OOM'd at a lower
                        # hop will only OOM again here (output grows with hops),
                        # so skip without re-running.
                        if system in failed_at_hop and hk >= failed_at_hop[system]:
                            cell_rows = "SKIPPED"
                            cell_total_ms = None
                        else:
                            # Every binary invocation is OOM-tolerant: a crash /
                            # OOM-kill (non-zero exit -> RuntimeError) is recorded
                            # as a failed cell and the experiment keeps going,
                            # rather than aborting the whole sweep.
                            try:
                                if system == "nebuladb":
                                    if hk == 1:
                                        # 1-hop NebulaDB = the per-rep one-hop run.
                                        cell_total_ms = onehop_ms
                                        cell_onehop_ms = onehop_ms
                                        cell_rows = onehop_rows
                                    else:
                                        # rewrite already cached; only time the MWJ stage.
                                        mwj_ms, mwj_rows = run_mwj(
                                            decomposed_for[query], hop_dir,
                                            args.mwj_threads, log_file,
                                            timeout=cell_timeout)
                                        cell_total_ms = onehop_ms + mwj_ms
                                        cell_onehop_ms = onehop_ms  # inherited from this rep
                                        cell_mwj_ms = mwj_ms
                                        cell_rows = mwj_rows
                                elif system == "full_mwj":
                                    mwj_ms, mwj_rows = run_mwj(
                                        qpath, data_dir, args.mwj_threads, log_file,
                                        timeout=cell_timeout)
                                    cell_total_ms = mwj_ms
                                    cell_mwj_ms = mwj_ms
                                    cell_rows = mwj_rows
                                elif system == "full_mwj_no_filter":
                                    # Unfiltered full MWJ: same chain query, --no-filter.
                                    mwj_ms, mwj_rows = run_mwj(
                                        qpath, data_dir, args.mwj_threads,
                                        log_file, no_filter=True,
                                        timeout=cell_timeout)
                                    cell_total_ms = mwj_ms
                                    cell_mwj_ms = mwj_ms
                                    cell_rows = mwj_rows
                                elif system == "obliviator_chained":
                                    obl_ms, obl_rows = run_obliviator(
                                        hk, obliviator_src_txt,
                                        args.obliviator_threads, log_file,
                                        timeout=obliviator_timeout)
                                    cell_total_ms = obl_ms
                                    cell_rows = obl_rows
                            except RuntimeError as e:
                                # OOM / crash / timeout: record it, remember the
                                # hop so higher hops of this system are skipped
                                # (output grows monotonically with hops), move on.
                                # CellTimeout is a RuntimeError subclass, so it is
                                # caught here too but recorded distinctly.
                                kind = "TIMEOUT" if isinstance(e, CellTimeout) else "OOM"
                                log_file.write(f"\n!!! {system} {kind} at {query}: {e}\n")
                                log_file.flush()
                                failed_at_hop[system] = min(
                                    failed_at_hop.get(system, hk), hk)
                                cell_rows = kind
                                cell_total_ms = None

                        total_str = (f"{cell_total_ms:.1f}ms" if cell_total_ms is not None
                                     else str(cell_rows))
                        print(f"  [{query}] {system:18s} {label} -> total={total_str}"
                              f" rows={cell_rows}", flush=True)
                        rows.append({
                            "system": system,
                            "query": query,
                            "dataset": args.dataset,
                            "run_id": run_id,
                            "is_warmup": int(is_warmup),
                            "total_ms": cell_total_ms,
                            "onehop_ms": cell_onehop_ms,
                            "mwj_ms": cell_mwj_ms,
                            "output_rows": cell_rows,
                        })

    # raw_runs.csv (everything)
    fieldnames = ["system", "query", "dataset", "run_id", "is_warmup",
                  "total_ms", "onehop_ms", "mwj_ms", "output_rows"]
    with open(raw_csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # summary.csv (measurement runs only)
    by_cell = {}
    for r in rows:
        if r["is_warmup"]:
            continue
        by_cell.setdefault((r["system"], r["query"], r["dataset"]), []).append(r)

    with open(summary_csv_path, "w", newline="") as f:
        sw = csv.DictWriter(f, fieldnames=[
            "system", "query", "dataset", "n_runs",
            "median_ms", "min_ms", "max_ms", "stddev_ms", "output_rows",
        ])
        sw.writeheader()
        for (system, query, dataset), cell in sorted(by_cell.items()):
            # Drop failed/skipped runs (total_ms is None for OOM/SKIPPED cells).
            totals = [c["total_ms"] for c in cell if c["total_ms"] is not None]
            n = len(totals)
            if n == 0:
                # Every run for this cell failed/was skipped — emit the sentinel.
                sw.writerow({
                    "system": system, "query": query, "dataset": dataset,
                    "n_runs": 0,
                    "median_ms": "", "min_ms": "", "max_ms": "", "stddev_ms": "",
                    "output_rows": cell[0]["output_rows"],
                })
                continue
            sw.writerow({
                "system": system, "query": query, "dataset": dataset,
                "n_runs": n,
                "median_ms": statistics.median(totals),
                "min_ms": min(totals),
                "max_ms": max(totals),
                "stddev_ms": statistics.stdev(totals) if n >= 2 else 0.0,
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

# Graphite: An Oblivious Property Graph Database

A TDX-based implementation of an oblivious property graph database that answers
multi-hop graph queries without revealing anything about the data through memory
access patterns, beyond the public input table sizes and the size of the filtered
result.

## Overview

This project implements an oblivious graph query engine that:
- Decomposes multi-hop queries into independent one-hop operators plus a residual join
- Applies predicate filtering in place, so the unfiltered intermediate join is never materialized
- Maintains data-oblivious memory access patterns to prevent side-channel leaks
- Runs inside an Intel TDX trusted VM, which encrypts memory transparently
- Supports chain, fan-in, fan-out, and tree query patterns

Based on the research paper: "Graphite: An Oblivious Property Graph Database" (to appear)

## Features

- **Oblivious Execution**: Memory access patterns depend only on input sizes, output size, and the query
- **Hidden Intermediates**: Unlike prior oblivious joins, intermediate result sizes are never revealed
- **One-Hop Operator**: Indexed oblivious hop with parallel source/destination branches
- **Parallel Execution**: Thread counts and work partitioning derived only from public parameters
- **Two Baselines Included**: Full multi-way join and the Obliviator chained scheme, for comparison

## Prerequisites

- Linux x86-64 (Ubuntu 20.04 or later)
- GCC 9+ with C++17 support (C++20 for the one-hop operators)
- CMake 3.14 or later
- Make build system
- Python 3.8+ with `matplotlib` and `pandas` (for plots and calibration)

No SGX SDK is required. The system targets Intel TDX, which encrypts the entire guest
VM, so there is no enclave boundary and no application-level cryptography. The main
binary is still named `sgx_app` for historical reasons, but it builds and runs on any
ordinary Linux machine. A TDX guest is needed for the security guarantee in
deployment, not for reproducing performance results.

```bash
python3 -m pip install matplotlib pandas
```

## Usage

## SQL Query Format

Queries are standard SQL SELECT statements. All tables must use the `AS` keyword for
aliases:

```sql
-- Two-hop chain
SELECT * FROM account AS a1, txn AS t1, account AS a2, txn AS t2, account AS a3
WHERE a1.account_id = t1.acc_from AND t1.acc_to = a2.account_id
  AND a2.account_id = t2.acc_from AND t2.acc_to = a3.account_id
  AND a1.account_id = 46;
```

Graphite does not execute these directly. The decomposer rewrites each query into
one-hop instances plus a residual query; both are saved under `decomposed/` in the
results directory. The baselines execute the original query.

Every query used in the experiments is checked in under
[`input/queries/`](input/queries):
the 1- to 5-hop chains for each workload (`banking_*`, `aml_*`, `snb_*`, `snap_*`),
the non-chain query shapes (`aml_fanin`, `aml_fanout`, `aml_tree`), and the
13-point selectivity sweep (`aml_2hop_sel_*`).

## Data Format

Input data is plaintext CSV with:
- First row containing column names
- Integer values only (system limitation)
- Values within range [-1073741820, 1073741820]

No encryption step is needed — TDX encrypts memory transparently.

## Datasets

### Synthetic banking data

```bash
python3 scripts/generate_banking_scaled.py <num_accounts> <output_dir> [options]

# 1M accounts / 5M transactions
python3 scripts/generate_banking_scaled.py 1000000 input/plaintext/banking_1M --seed 42
```

### Real-world datasets

These must be downloaded, then converted to the CSV layout above using the conversion
scripts under `scripts/`:

| Dataset | Nodes | Edges |
|---------|-------|-------|
| IBM AML HI-Small | 515K | 5.1M |
| LDBC SNB SF30 | 165K | 12.0M |
| SNAP cit-Patents | 3.8M | 16.5M |
| IBM AML HI-Medium | 2.1M | 31.9M |
| IBM AML HI-Large | 2.1M | 179.7M |


See `docs/workloads.md` for download links and schemas.

## Experiments

Three experiments produce the measured results in the paper. Each script writes
`raw_runs.csv`, `summary.csv`, `run_metadata.json`, and `binary_stdout.log` to its
results directory, flushing after every cell so an interrupted run still leaves
usable output. Our own results are checked in under `results/`.

Three systems are compared. They differ in what they leak, which is why the two
baselines are given the benefit of the doubt:

| System | Intermediate sizes | Unfiltered output | Filtered output |
|--------|--------------------|-------------------|-----------------|
| Obliviator chained | revealed | revealed | revealed |
| Full MWJ | hidden | revealed | revealed |
| Graphite | hidden | hidden | revealed |

Results were measured inside a TDX guest with 512 GB of RAM, 64 threads, and a
one-hour budget per cell.
`OOM` and `TIMEOUT` entries in the CSVs are expected
results, not failures — they are the claim that baselines cannot complete these
workloads. On a smaller machine the baselines will fail earlier and Graphite's
latencies will rise, but the separation between them should hold.

### Comparison on real-world datasets

A 2-hop query across every dataset, comparing all three systems.

```bash
python3 scripts/experiments/run_e3_cross_dataset.py
```

```bash
python3 scripts/experiments/run_e3_cross_dataset.py --datasets banking_1M,hi_small
```

To redraw the figure from the checked-in numbers, use `summary_all6.csv` instead of
`summary.csv`.

Index construction times come from the one-hop drivers rather than this script:

```bash
./obligraph/build/ibm_aml_onehop input/plaintext/ibm_aml_hi_small hop.csv \
    --threads 64 --report OFFLINE
```

### Impact of graph density

Four banking graphs with identical input size (1M accounts, 5M edges) and a fixed
filtered output, varying only how edges concentrate on hubs. The graphs are generated
automatically on first run.

```bash
python3 scripts/experiments/run_e5_density.py --tier 5M
python3 scripts/experiments/plot_e5_density.py results/e5_density_5M/summary.csv
```
The plot is written as `e5_density.pdf` next to the summary you pass in.


### Impact of query decomposition

Full MWJ is given the same in-place filtering as Graphite, so the remaining gap is
attributable to decomposition alone. The predicate threshold is swept so the filtered
output spans 2.4K to 183M rows.

```bash
python3 scripts/experiments/run_e7_selectivity.py --measurement-runs 1 --warmup-runs 1
python3 scripts/experiments/plot_e7_selectivity.py
```


## Testing

```bash
# One-hop operator against a plaintext reference
python3 tests/test_onehop_correctness.py input/plaintext/banking_1k obligraph/build/banking_onehop

# Full decomposed pipeline against a plaintext reference
python3 tests/test_pipeline_correctness.py input/plaintext/banking_1k obligraph/build/banking_onehop ./sgx_app

# Large-scale regression, verified against SQLite
python3 scripts/generate_banking_scaled.py 200000 input/plaintext/banking_200k --seed 42
python3 tests/test_large_scale_regression.py input/plaintext/banking_200k obligraph/build/banking_onehop ./sgx_app
```

`make tests` builds the C++ test binaries, including `test_join`, which compares
engine output against a SQLite baseline.

## Sample Data

- `input/plaintext/banking_1k/` — 200 accounts, 1,000 transactions; used by the correctness tests
- `input/plaintext/banking/` — 10,000 accounts, 50,000 transactions
- `input/plaintext/data_0_001/` — TPC-H scale factor 0.001

## Limitations

- Integer data types only; values must fall within [-1073741820, 1073741820]

## License

[MIT License](LICENSE), covering the code authored in this repository.

## Contact

For questions or issues, please open a GitHub issue.

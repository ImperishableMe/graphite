# Claude AI Assistant Instructions - Oblivious Multi-Way Join Project

This document contains comprehensive instructions and context for AI assistants working with this codebase.

## ⚠️ IMPORTANT: TDX MIGRATION COMPLETED (October 2025)

**This codebase has been migrated from Intel SGX to Intel TDX architecture.**

Key changes:
- ❌ **No application-level encryption** - TDX encrypts entire VM
- ❌ **No enclave boundary** - Direct function calls instead of ecalls
- ✅ **Oblivious algorithms preserved** - All security properties maintained
- ✅ **Simplified architecture** - No crypto layer, unified codebase

See `TDX_MIGRATION_SUMMARY.md` for complete migration details.

## "The Obliviator One Hop" — Canonical Reference

When the user says **"the obliviator one hop"** (without further qualification),
they mean the **chained FK driver**, not the join-sort variant.

- **Source:** `obl-radix/baselines/obliviatorFK-TDX/obliviator_1hop_chained_main.c`
- **Build target:** `obliviator_1hop_chained` (via `Makefile.standalone`)
- **CLI:** `./obliviator_1hop_chained <num_threads> <src.txt> <output.csv>`
- **Pipeline:** `account ⋈ txn` on `acc_from` → repack as intermediate keyed by
  `acc_to` → `account ⋈ inter` on `acc_to`. No bitonic stitch.
- **Headline:** banking_1M @ 32 threads, OBLIVIOUS WORK ≈ 0.60 s.

The other variant (`obliviator_1hop_main.c`, the "join-sort" baseline at
commit `77e89e1`) does two independent FK joins followed by an oblivious
bitonic stitch by `txn_id`. It is kept as an alternative reference, but
is **not** the default. Only use it when the user explicitly says
"join-sort baseline" or names it directly.

## Experiment Comparison Systems — Canonical Naming (July 2026)

Exactly **three** systems are compared in the experiments (E1 chain sweep and
all derived plots/tables). No code rename was performed — the runner's internal
system keys stay as-is; the rules below apply to **presentation**: plot legends,
axis/series labels, table headers, and any reported output.

1. **Graphite** — our system (decomposed one-hop + rewritten-query pipeline).
   Internal runner key: `nebuladb`. In ALL plots and experiment outputs, always
   label it **Graphite**, never "NebulaDB".
2. **Obliviator chained** — the obliviator k-hop chained baseline
   (`obliviator_1hop_chained` for 1 hop, `obliviator_khop_chained` for ≥2 hops).
   Internal runner key: `obliviator_chained`.
3. **Full multiway oblivious join (Full MWJ)** — `sgx_app` on the full chain
   query **without the filter** (`--no-filter`). Internal runner key:
   `full_mwj_no_filter`. The unfiltered mode is the natural/default Full MWJ:
   label it plainly "Full MWJ" (no "no-filter" qualifier), and do NOT run or
   report the filtered `full_mwj` variant.

# ============== CRITICAL RULES (MUST FOLLOW) ==============

## Obliviousness Guarantee (NEVER BREAK)
The oblivious execution model of this codebase guarantees that all observable behavior
(memory access patterns, control flow, timing, allocation sizes, loop bounds) depends
ONLY on three public quantities:
  1. Input data sizes (row counts of the input tables).
  2. Output data size (row count of the result).
  3. The query itself (schema, predicates, projection columns, join plan).

Any decision — branch, loop bound, array index, allocation size — MUST be derivable
from those three alone. Values inside rows (keys, column payloads, dummy flags) and
any intermediate counts MUST NOT influence observable behavior.

When considering any optimization:
  - Does it branch or allocate based on row *values*?  If yes, it breaks obliviousness.
  - Does it branch or allocate based on row *counts* or schema?  Fine.
  - Does it leak information through timing (e.g. data-dependent early exit)?  Breaks it.

Parked optimization ideas (explicitly deferred):
  - Parallelizing `deduplicateRows` / `reduplicateRows`: a specific technique is
    planned for this; do not implement a naive chunked version.

## Code Modification Rules
- **NEVER modify code with scripts** - Always edit code manually using the Edit tool. No sed, awk, perl, or any script-based modifications.
- **NO temporary fixes or workarounds** - All issues must be addressed with proper, permanent solutions.
- **ALL compiler warnings must be fixed** - Treat warnings as errors. No code should be committed with warnings.

## Command Execution Rules
- **NEVER use pipes (|) in ANY command**:
  - NO pipes with make commands (no `make | grep`)
  - NO pipes when running tests (no `./test | head`)
  - NO grep, head, tail, sed, awk, or any other pipe operations
  - This is REQUIRED to avoid permission prompts and ensure all output is captured

## Testing Rules
- **NO PIPELINES IN TESTS**: Run test commands WITHOUT pipelines for proper debugging
- **All tests must go to test folder**, isolated from the main implementation
- **ALWAYS run both correctness tests after ANY optimization or refactoring** before considering the work done. These are the regression tests for the oneHop pipeline:
  1. oneHop test: `python3 tests/test_onehop_correctness.py input/plaintext/banking_1k obligraph/build/banking_onehop`
  2. Pipeline test: `python3 tests/test_pipeline_correctness.py input/plaintext/banking_1k obligraph/build/banking_onehop ./sgx_app`
  - This applies to ANY change touching `obligraph/src/`, `app/algorithms/`, `app/core_logic/`, or any other code in the oneHop or sgx_app execution path
  - Do not skip even if the change looks obviously safe
  - Note: `banking_onehop` is built separately via CMake — run `cmake --build obligraph/build --target banking_onehop` after changing `obligraph/src/`

## Large-Scale Regression Test (Performance Baseline)
- **Large dataset** (200k accounts, 1M transactions) is generated with a fixed seed for reproducibility:
  ```
  python3 scripts/generate_banking_scaled.py 200000 input/plaintext/banking_200k --seed 42
  ```
- **Run the performance regression test** (times oneHop + filtered 3-hop, verifies correctness against SQLite):
  ```
  python3 tests/test_large_scale_regression.py input/plaintext/banking_200k obligraph/build/banking_onehop ./sgx_app
  ```
- Current baseline (seed=42, 200k accounts / 1M txns): `oneHop ≈ 2.3s`, `sgx_filtered_3hop ≈ 167s`

## oneHop Timing Categories (banking_onehop binary)
`banking_onehop` emits a structured timing breakdown at the end of every run. Each stage is tagged with exactly one category:
- **`ONLINE`** — the per-query oblivious work (deduplicate, ORAM probe, reduplicate, sort, union, filter, project). **This is the default "oneHop time" to report** — it's what recurs per query.
- **`OFFLINE`** — query-independent setup: index build, index deep-copy, and per-side probe-scaffold initialization (`buildNodeIndex`, `index copy (src)`, `initProbeSide (wall)`, with `initProbeSide (src)` / `(dst)` as diagnostic per-side breakdowns). Report this alongside ONLINE if the setup cost is part of the comparison.
- **`IO`** — CSV read/write. Typically **excluded** from reported times.

### Selecting which categories to report
Use the `--report` flag on `banking_onehop`:
```
./obligraph/build/banking_onehop <data_dir> <output_csv> --report ONLINE              # default
./obligraph/build/banking_onehop <data_dir> <output_csv> --report ONLINE,OFFLINE      # include index build
./obligraph/build/banking_onehop <data_dir> <output_csv> --report IO,OFFLINE,ONLINE   # end-to-end
```
The binary prints a `TIMING_REPORTED categories=<cats> total=<ms>ms` line that the regression test / grep harness can parse. The full per-stage breakdown is always printed regardless of `--report`.

### Wall-clock vs diagnostic entries
Category totals and `TIMING_REPORTED` represent **true wall-clock time**. Stages that run inside the parallel block (both branches and everything nested inside them — dedup, probe, redup, unionWith per side, the dst-side oblivious sort, and the `src/dst branch (total)` wrappers) are marked with `*` in the breakdown and are **excluded from sums**. Including them would double-count the parallel work and defeat the point of running the two sides concurrently.

The stages that DO contribute to the ONLINE wall-clock sum are:
- `parallel branches (wall)` — the outer scope around both `std::async` branches (≈ max of src/dst branch totals).
- `unionWith (final)` — sequential stage after the parallel block.
- `filter` / `project` — sequential, only present when the query has predicates / non-identity projection.

### Notable stages in the ONLINE breakdown
- `src branch (total)` / `dst branch (total)` — wall-clock of each parallel branch **in isolation**. Reported for both so you can tell how the two sides would have cost if run sequentially. Marked `*` (diagnostic).
- `parallel branches (wall)` — actual concurrent wall-clock for both branches running together (≈ max of the two branch totals). This is what the ONLINE sum credits for the parallel phase.
- `probe src` / `probe dst` — the ORAM probe itself, the usual hotspot. Diagnostic.
- `parallel_sort (dst)` — oblivious sort on the dst side; historically large. Diagnostic.

### Notable stages in the OFFLINE breakdown
- `buildNodeIndex` — builds the oblivious cuckoo/bin index from the node table; sized by the public edge count.
- `index copy (src)` — deep-copies the index for the src side because probing is destructive.
- `initProbeSide (src)` / `initProbeSide (dst)` — populate the per-side probe scaffold (resize to `edgeCount` + parallel key-copy from the corresponding edge table). Marked `*` (diagnostic); they run concurrently inside `initProbeSide (wall)`.
- `initProbeSide (wall)` — actual concurrent wall-clock for both side scaffolds running together (≈ max of (src) and (dst)). This is what the OFFLINE sum credits for scaffold init.

## One-Hop Scaling Benchmark
Use `scripts/run_onehop_scaling.py` to measure how the per-stage breakdown of `banking_onehop` scales with dataset size. This is the canonical way to gather the timing data we report for one-hop scaling.

### What it does (in order)
1. Rebuilds `obligraph/build/banking_onehop` in Release mode (`-O3 -DNDEBUG`) — skip with `--skip-build`.
2. Generates Banking W1 datasets at **200K / 500K / 1M / 10M accounts** (5× txns each = 1M / 2.5M / 5M / 50M edges) under `input/plaintext/banking_{200k,500k,1M,10M}` using `scripts/generate_banking_scaled.py --seed 42`. A dataset is regenerated only if its row counts don't match what seed=42 would produce; skip generation entirely with `--skip-generation`.
3. Runs `banking_onehop` **twice per dataset, strictly sequentially** — run 1 is a cache warm-up and is discarded; run 2 is the recorded measurement. Each invocation gets the full machine; the binary picks its own thread count from available cores.
4. Parses the binary's `=== TIMING BREAKDOWN ===` block and emits the CSVs below.

### Run commands
```
python3 scripts/run_onehop_scaling.py                                  # full run, fresh build, regenerate any missing/wrong-sized datasets
python3 scripts/run_onehop_scaling.py --skip-build --skip-generation   # re-run experiment only
python3 scripts/run_onehop_scaling.py --sizes 200k                     # subset, e.g. for smoke tests
```

### Output files (in `results/onehop_scaling/`)
- `breakdown.csv` — raw per-stage timings, **both runs** (warm-up + measured). Columns: `dataset, num_accounts, num_edges, run_id, is_warmup, stage, category, time_ms, in_wall_clock`.
- `breakdown_summary.csv` — same as above but **run 2 only**, plus `timing_reported_categories` and `timing_reported_total_ms`. `run_id` and `is_warmup` are dropped (they would be constant).
- `breakdown_sorted.csv` — derived from `breakdown_summary.csv` with **IO category removed** and stages sorted by `time_ms` **descending within each dataset** (`rank` column = 1 is the most expensive stage). Use this to spot where time is going at each scale.
- `category_summary.csv` — top-level latency, **one row per dataset**, wide format: `dataset, num_accounts, num_edges, online_ms, offline_ms`. Both numbers are wall-clock-contributing totals (sum of stages where `in_wall_clock=1`).
- `run_metadata.json` — git commit, branch, hostname, `nproc`, build flags, seed, timestamp. Reproducibility context for the CSVs.
- `binary_stdout.log` — full stdout from every `banking_onehop` invocation.

### `in_wall_clock` flag
Every per-stage row carries `in_wall_clock` ∈ {0, 1}:
- `1` — the stage runs sequentially within its category and contributes to the category total. Summing `in_wall_clock=1` rows for a category gives the truthful wall-clock figure.
- `0` — diagnostic stages that run *inside* `parallel branches (wall)` (per-side dedup/probe/redup/union, plus `parallel_sort (dst)`, plus the `src/dst branch (total)` wrappers). These are reported only so you can see *how* the parallel block spent its time. They must NOT be summed alongside `in_wall_clock=1` rows or you'll double-count parallel work.

### Reproducibility notes
- All four datasets are generated under **seed 42** so the experiment is reproducible. The row-count check only verifies size, not content — if you suspect a directory was generated under a different seed, `rm -rf` it before running.
- The run is **strictly sequential** by design: dataset generation is one-at-a-time, and the binary is invoked one run at a time. This guarantees each invocation has the full machine and avoids contention that would distort per-stage timings.

## Compilation Rules
- **ALWAYS compile using separate commands from the project root**:
  - Main code: `make`
  - Test utilities: `make tests`
- **NEVER use combined commands** like `cd test && make && cd ..` 
- **Use absolute paths** when referencing files outside current directory

# ============== PROJECT OVERVIEW ==============

## System Architecture

### Core Design
- **Oblivious Multi-Way Join**: Implements data-oblivious join algorithms with constant memory overhead
- **TDX VM Protection**: Secure execution inside Intel TDX trusted VM (migrated from SGX)
- **Unified Processing**:
  - Single codebase - no enclave boundary
  - Direct function calls - no ecalls/ocalls
  - VM-level encryption - no application crypto
- **Memory Access Patterns**: All access patterns are data-independent to prevent side-channel attacks

### TDX Migration (October 2025)
- **Architecture**: Moved from SGX enclaves to TDX trusted VMs
- **Encryption**: Removed application-level encryption (TDX handles transparently)
- **Code Organization**: Merged `enclave/trusted/` → `app/core_logic/`
- **Performance**: Eliminated ecall overhead, faster execution
- **Security**: Maintained data-oblivious properties, VM-level protection

### Key Components
1. **Join Algorithms** (`app/algorithms/`):
   - Bottom-up phase: Builds join tree from leaves
   - Top-down phase: Propagates results down the tree
   - Distribute-Expand: Core oblivious distribution mechanism
   - Align-Concat: Oblivious data alignment and concatenation

2. **Batch Processing System** (`app/batch/`):
   - Reduces SGX ecall overhead by batching operations
   - Deduplicates entries using hash maps
   - Converts Entry pointers to indices for oblivious tracking

3. **Data Structures** (`app/data_structures/`):
   - Entry: Core data structure (fat mode: ~2256 bytes, slim mode: ~260 bytes)
   - Table: Collection of entries with schema
   - JoinAttributeSetter: Manages join attribute extraction

4. **Encryption** (`app/crypto/`):
   - AES-based encryption for data at rest
   - Secure key management inside enclave

## System Data Bounds and Constraints

**Core Design Principle:**
"We use int32_t throughout our system for simplicity. For attributes we define the bounds to [-1,073,741,820, 1,073,741,820], and we define -INF and INF to be -1,073,741,821 and 1,073,741,821 to handle join_attr±INF without overflow."

### Defined Constants (from enclave_types.h):
- **Valid Attribute Range**: `[JOIN_ATTR_MIN, JOIN_ATTR_MAX]` = `[-1,073,741,820, 1,073,741,820]`
- **Negative Infinity**: `JOIN_ATTR_NEG_INF = -1,073,741,821`
- **Positive Infinity**: `JOIN_ATTR_POS_INF = 1,073,741,821`
- **NULL Value**: `NULL_VALUE = INT32_MAX = 2,147,483,647`

### Important Implications:
- **ALL data values must be integers** within the valid range
- String values in CSV files will be parsed as 0 (with warnings)
- The system cannot handle actual string data
- Test data must be prepared with these bounds in mind

# ============== BUILD SYSTEM ==============

## Prerequisites
- Intel CPU with SGX support
- Ubuntu 20.04 or later
- Intel SGX SDK and PSW
- GCC 9+ with C++17 support

## Build Commands

```bash
# Standard build (TDX - no SGX SDK needed)
make clean && make

# Debug build (enables debug output to files)
DEBUG=1 make

# Slim entry mode (reduces memory overhead)
make SLIM_ENTRY=1

# Build test programs
make test_join        # Integration test (works)
# Note: sqlite_baseline requires libsqlite3-dev (optional)

# Run the application
./sgx_app <query.sql> <input_dir> <output.csv>
```

### TDX Build Notes
- ✅ No SGX SDK installation required
- ✅ No enclave signing needed
- ✅ Standard GCC/G++ compilation
- ✅ Direct linking of all components

## Build Modes
- **Fat Entry Mode** (default): Full entry structure (~2256 bytes per entry)
- **Slim Entry Mode**: Reduced entry size (~260 bytes) by moving metadata to shared structures
- **Debug Mode**: Enables detailed logging to debug files

**IMPORTANT**: Fat and slim modes are incompatible - data encrypted in one mode cannot be decrypted in the other

# ============== USAGE GUIDE ==============

## Basic Usage

### Running Joins
```bash
# Run a join query on encrypted data
./sgx_app <query.sql> <encrypted_data_dir> <output.csv>

# Example
./sgx_app input/queries/tpch_tb1.sql input/encrypted/data_0_001 output.csv
```

### Data Format (TDX)
**Note**: After TDX migration, no encryption tool is needed. Data files are used directly as CSV.
- Input: Plaintext CSV files
- Output: Plaintext CSV files
- Protection: TDX VM encrypts filesystem transparently

### Testing
```bash
# Compare SGX output with SQLite baseline
./test_join <query.sql> <encrypted_data_dir>

# Run all TPC-H tests
./scripts/run_tpch_tests.sh
```

## SQL Query Format

**IMPORTANT**: All queries must use the AS keyword for table aliases.

Standard SQL SELECT statements with joins:
```sql
-- Two-way join
SELECT * FROM T1 AS t1, T2 AS t2
WHERE t1.attr < t2.attr;

-- Three-way join
SELECT * FROM T1 AS t1, T2 AS t2, T3 AS t3
WHERE t1.attr = t2.attr AND t2.attr = t3.attr;

-- Self-join (same table with multiple aliases)
SELECT * FROM T1 AS a, T1 AS b, T1 AS c
WHERE a.attr < b.attr AND b.attr < c.attr;
```

**Table Aliasing**:
- ALL tables must use `AS` keyword to specify an alias
- Each alias creates a logical in-memory copy of the table
- Output columns are prefixed with the alias (e.g., `t1.attr`, `t2.attr`)
- Self-joins work by using the same table name with different aliases

## Data Format Requirements
- CSV files with header row containing column names
- Integer values only (system limitation)
- Values must be within [-1,073,741,820, 1,073,741,820]
- Last row should contain sentinel values (-10000)

# ============== TEST INFRASTRUCTURE ==============

## Test Tools

### test_join
- **Purpose**: Compares SGX output with SQLite baseline
- **Usage**: `./test_join <sql_file> <encrypted_data_dir>`
- **Build**: `make test_join` or `make tests` to build all tests
- **Note**: Both SGX and SQLite must be compiled in same mode (fat/slim)

### sqlite_baseline
- **Purpose**: Reference implementation using SQLite
- **Process**: Decrypts input → Runs SQL → Re-encrypts output
- **Usage**: `./sqlite_baseline <sql_file> <encrypted_data_dir> <output_file>`
- **Build**: `make sqlite_baseline` or `make tests` to build all tests

### Performance Tests
- `overhead_measurement`: Measures SGX overhead
- `overhead_crypto_breakdown`: Analyzes encryption costs

## Building and Running Tests

### Build all tests
```bash
make tests
```

### Build individual test programs
```bash
make test_join        # Build comparison test
make sqlite_baseline  # Build SQLite baseline
```

### Run tests
```bash
# Compare SGX with SQLite baseline
./test_join input/queries/tpch_tb1.sql input/encrypted/data_0_001

# Run SQLite baseline directly
./sqlite_baseline input/queries/tpch_tb1.sql input/encrypted/data_0_001 output.csv

# Run SGX join directly
./sgx_app input/queries/tpch_tb1.sql input/encrypted/data_0_001 output.csv

# Run all TPC-H tests with script
./scripts/run_tpch_tests.sh [scale]  # scale: 0_001 (default) or 0_01
```

## Test Data
- Scale 0.001: ~150 rows per table (included)
- Scale 0.01: ~1,500 rows per table (included)
- Scale 0.1: ~15,000 rows per table
- Scale 1.0: ~150,000 rows per table

# ============== DEBUG INFORMATION ==============

## Debug Output
- Debug output goes to files, not console
- Location: `debug/{date}_{time}_{test}/`
- Enable with: `DEBUG=1 make`
- Contains table dumps, operation traces, and execution logs

## Common Debug Scenarios
1. **Join result mismatch**: Check debug dumps for intermediate results
2. **Memory issues**: Enable Valgrind or use operation tracing
3. **Performance issues**: Use overhead measurement tools
4. **Encryption issues**: Verify data format matches compilation mode

# ============== RECENT DEVELOPMENT ==============

## Ecall Reduction (Completed)
- Reduced from 40+ individual ecalls to 4 essential ecalls
- Implemented batch processing system for operation bundling
- Significant performance improvement (>10x for some workloads)

## Memory Access Pattern Verification
- Added operation tracing to verify oblivious behavior
- Can compile with `TRACE_OPS=1` to enable operation logging
- Traces stored as JSON for analysis
- Verified identical operation sequences across different datasets

## Slim Mode Migration (In Progress)
- Goal: Reduce entry size from ~2256 to ~260 bytes
- Status: Core functionality complete, testing ongoing
- Benefits: Reduced memory usage and ecall overhead

# ============== PROJECT STRUCTURE ==============

```
.
├── app/                    # Main application code (non-enclave)
│   ├── algorithms/         # Join algorithm implementations
│   │   ├── oblivious_join.cpp    # Main orchestrator
│   │   ├── bottom_up_phase.cpp   # Build join tree
│   │   ├── top_down_phase.cpp    # Propagate results
│   │   ├── distribute_expand.cpp # Core oblivious operations
│   │   └── align_concat.cpp      # Data alignment
│   ├── batch/              # Ecall batching system
│   ├── core/               # Core data structures (Entry, Table, etc.)
│   ├── crypto/             # Encryption utilities
│   ├── debug/              # Debug utilities
│   ├── io/                 # File I/O operations
│   ├── query/              # SQL query parsing
│   └── utils/              # Helper utilities
├── enclave/                # SGX enclave code
│   ├── trusted/            # Trusted enclave code
│   └── untrusted/          # Generated untrusted edge routines
├── common/                 # Shared headers between app and enclave
│   ├── types_common.h      # Shared type definitions
│   ├── enclave_types.h     # Entry structure definition
│   ├── batch_types.h       # Batch operation types
│   └── debug_util.h        # Debug utilities
├── main/                   # Entry point programs
│   ├── sgx_join/           # Main SGX join application
│   └── tools/              # Standalone tools (encrypt_tables, etc.)
├── tests/                  # Test suite
│   ├── integration/        # Integration tests (test_join)
│   ├── baseline/           # SQLite baseline implementation
│   ├── performance/        # Performance tests
│   └── unit/               # Unit tests (organized by module)
├── scripts/                # Build and test scripts
├── input/                  # Test data
│   ├── queries/            # SQL test queries
│   ├── plaintext/          # Unencrypted test data
│   │   ├── data_0_001/    # Scale 0.001
│   │   └── data_0_01/     # Scale 0.01
│   └── encrypted/          # Encrypted test data
│       ├── data_0_001/    # Scale 0.001
│       └── data_0_01/     # Scale 0.01
└── output/                 # Test outputs and results
```

# ============== KEY ALGORITHMS ==============

## Oblivious Join Algorithm
1. **Input Processing**: Load and encrypt tables
2. **Bottom-Up Phase**: Build join tree from leaf nodes
3. **Distribute-Expand**: Obliviously distribute tuples
4. **Align-Concat**: Align data structures obliviously
5. **Top-Down Phase**: Propagate results to output
6. **Output Generation**: Decrypt and write results

## Batch Processing Flow
1. Operations added to batch collector with Entry pointers
2. Collector deduplicates entries, assigns indices
3. When batch full, flush to enclave
4. Enclave processes batch, returns results
5. Results written back to original Entry objects

## Memory Management
- Pre-allocated pools to avoid dynamic allocation
- Constant memory overhead: O(N) for N input tuples
- No memory allocation during join execution

# ============== PERFORMANCE NOTES ==============

## Optimization Strategies
1. **Batch Size**: Larger batches reduce ecall overhead
2. **Entry Mode**: Slim mode reduces memory transfer
3. **Debug Mode**: Disable for production (10x speedup)
4. **Data Locality**: Keep related data together

## Known Bottlenecks
1. SGX ecall transitions (mitigated by batching)
2. Encryption/decryption overhead
3. Oblivious operations add ~2-3x overhead
4. Memory bandwidth for large datasets

# ============== TROUBLESHOOTING ==============

## Common Issues

### "File not found" errors
- Check PATHS.md for correct file locations
- Use absolute paths when in doubt

### Compilation errors
- Ensure SGX SDK is properly installed
- Check that all dependencies are met
- Verify correct compilation mode (fat/slim)

### Test failures
- Verify data format matches compilation mode
- Check that test data is within valid bounds
- Ensure both tools compiled with same settings

### Performance issues
- Disable debug mode for production
- Increase batch size for large datasets
- Use slim mode for memory-constrained systems

# ============== SECURITY CONSIDERATIONS ==============

## Threat Model
- Untrusted cloud provider with full system access
- Adversary can observe all memory accesses
- Side-channel attacks through access patterns

## Security Properties
1. **Data Confidentiality**: All data encrypted outside enclave
2. **Oblivious Execution**: Access patterns independent of data
3. **Integrity**: SGX attestation ensures code integrity
4. **Constant Time**: Operations take same time regardless of data

## Security Guidelines
- Never log sensitive data values
- Maintain oblivious access patterns
- Use secure random number generation
- Verify all input bounds

# ============== ADDITIONAL NOTES ==============

## Git Branches
- `master`: Stable release
- `efficiency-test-working`: Performance testing branch
- `memory-trace-working`: Memory pattern verification
- `public-release`: Clean version for public sharing

## Contact and Support
- Report issues via GitHub issues
- Include debug logs when reporting problems
- Specify exact commands and data used

## Future Work
- Support for additional data types (strings, floats)
- GPU acceleration for larger datasets
- Distributed execution across multiple nodes
- Dynamic memory management
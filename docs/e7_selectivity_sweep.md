# E7: Predicate-Selectivity Sweep (fixed input, varying filtered output)

Status: designed, implemented, and run 2026-07-16; **redesigned to v2 the
same day** (see "V2 design change"). Results in `results/e7_selectivity/`
(summary.csv, e7_selectivity.png/.pdf).

## V2 design change (2026-07-16): isolate the decomposition

V1 compared Graphite against Obliviator chained and Full MWJ `--no-filter`.
Both baselines turned out to be pinned to the 355M-row *unfiltered* join at
every θ (flat 19.6 min / flat OOM) — informative about filter pushdown, but
they say nothing about Graphite's *decomposition*, and they can never track
the sweep. V2 therefore changes the comparison, by explicit decision:

- **Obliviator chained is removed** — it has no filter support at all, so in
  a selectivity sweep it is bounded by the full unfiltered join and is not a
  meaningful comparator here.
- **Full MWJ now runs the FILTERED queries** (sgx_app on the original
  5-table query, no `--no-filter`) — a deliberate E7-only exception to the
  "Full MWJ = no-filter" presentation rule, labeled "Full MWJ (filtered)" in
  the figure. Both systems now apply the *same* predicates on the *same*
  engine, so the only differentiating factor between the two curves is the
  decomposition (one-hop + hop-table MWJ vs monolithic MWJ). Full MWJ is
  θ-dependent in v2 and runs per point, subject to the same oracle
  row-count check as Graphite.

V1's measured numbers are kept in the "V1 results" section below for the
record.

## V2 results (2026-07-16, 1 run/cell, 64 threads, all cells oracle-matched)

| s (per edge) | filtered rows | Graphite | Full MWJ (filtered) | gap |
|--------------|---------------|-----------|---------------------|------|
| 0.001        | 2,404         | 43.8 s    | 85.3 s              | 1.95× |
| 0.01         | 82,596        | 43.9 s    | 85.7 s              | 1.95× |
| 0.05         | 753,013       | 48.5 s    | 97.9 s              | 2.02× |
| 0.1          | 2,478,413     | 61.5 s    | 136.1 s             | 2.21× |
| 0.25         | 12,272,345    | 130.7 s   | 334.9 s             | 2.56× |
| 0.5          | 39,282,354    | 332.7 s   | 944.6 s             | 2.84× |
| 0.55         | 49,322,515    | 414.2 s   | 1,173.6 s           | 2.83× |
| 0.6          | 63,353,620    | 537.3 s   | 1,514.7 s           | 2.82× |
| 0.65         | 95,761,288    | 796.6 s   | 2,274.5 s           | 2.86× |
| 0.7          | 134,317,360   | 1,140.3 s | **OOM**             | —    |
| 0.75         | 182,661,965   | 1,459.0 s | (beyond MWJ ceiling)| —    |
| 0.8          | 233,108,598   | **OOM**   | —                   | —    |
| 1.0 (none)   | 355,144,984   | **OOM**   | aborted (trajectory to OOM) | — |

Decomposition buys two things, both measured on identical filters and the
same engine:

1. **Speed**: a 1.95×→2.86× gap that widens with the filtered output. Both
   curves are near-linear in output rows at the top end; the gap is the
   monolithic plan's price at every feasible point.
2. **Reach**: Graphite completes 183M-row outputs (s=0.75, 24.3 min, peak
   ~398 GB of the 471 GB box); the monolithic plan OOMs at 134M (s=0.7).
   Graphite's feasible region extends ~1.9× further, and it finishes its
   183M point faster (1,459 s) than Full MWJ finishes its 96M point
   (2,274.5 s). Ceilings bracketed empirically: Graphite ∈ [183M, 233M),
   Full MWJ ∈ [96M, 134M).

Notes: the s=0.55–0.8 points were added as ceiling probes after the main
sweep (separate runner invocations, `probe*/` subdirs; merged into the main
CSVs). Full MWJ's unfiltered (s=1.0) attempt was aborted by decision after
39 min at ~334 GB on a clear OOM trajectory — recorded as an abort, not a
measurement; Graphite's own unfiltered attempt OOMed, so the convergence
point stands either way.

## V1 results (superseded by v2; 2026-07-16 sweep, 1 run/cell, 64 threads)

Every completed Graphite cell's output_rows matched the pandas oracle
exactly (rows_match=1; the two smallest points additionally SQLite-verified).

| s (per edge) | θ (amount)  | filtered rows | Graphite | vs Obliviator |
|--------------|-------------|---------------|----------|---------------|
| 0.001        | 416,225,565 | 2,404         | 42.6 s   | 27.5×         |
| 0.01         | 13,524,530  | 82,596        | 43.5 s   | 27.0×         |
| 0.05         | 623,757     | 753,013       | 46.9 s   | 25.0×         |
| 0.1          | 137,275     | 2,478,413     | 59.7 s   | 19.6×         |
| 0.25         | 12,298      | 12,272,345    | 130.3 s  | 9.0×          |
| 0.5          | 1,415       | 39,282,354    | 343.7 s  | 3.4×          |
| 1.0 (none)   | —           | 355,144,984   | OOM      | —             |

Baselines (θ-invariant, one run): **Obliviator chained** 1,173.1 s of online
oblivious work at every θ — and it reported exactly 355,144,984 rows, so its
count matches the oracle too (the multi-edge-collapse quirk noted at design
time did not manifest post the E3 k-hop payload fix). **Full MWJ**
(`--no-filter`): OOM, as in E1.

Shape: Graphite is flat (~43 s) while the filtered output is small — the
fixed input-proportional oblivious work dominates — then bends into
output-proportional growth (2.5M → 39.3M rows: 15.9× rows for 5.8× time),
and at the unfiltered point it OOMs exactly like Full MWJ: when the filter
does no work, nobody can win. Combined with E5 this pins the claim: Graphite
costs Θ(input + *filtered* output); the baselines cost Θ(*unfiltered*
output) regardless of the predicate.

Numbering note: E7 in the *implemented* experiment series (E1 main, E2
scaling, E3 cross-dataset, E4 query shapes, E5 output sensitivity). The
E-numbers in the older `docs/experiments.md` planning doc do not correspond
to this series ("E6" there is the one-hop thread-scaling experiment,
`results/one_hop_thread_scaling/`).

## Claim demonstrated (v2)

Complement of E5. E5 held the input fixed and grew the *unfiltered* output:
baselines blow up, Graphite stays flat. E7 holds input and query structure
perfectly constant and moves only the predicate threshold θ, sweeping the
*filtered* output across five orders of magnitude — and both systems apply
the **same filters on the same engine**, so the gap between the two curves
is attributable to exactly one thing: **the decomposition**.

- **Graphite** (decomposed): one-hop materializes the hop table once
  (filter-independent, input-proportional), then the rewritten 2-hop hop
  query runs with the predicates pushed onto the hop aliases.
- **Full MWJ (filtered)** (monolithic): sgx_app executes the original
  5-table query with the same predicates pushed onto the txn tables.

Expected shape: both curves rise with the filtered output at the loose end;
the vertical gap at every θ is the price of joining monolithically instead
of through the decomposed hop table.

## Query and sweep design

Dataset: `ibm_aml_hi_small` (515,088 accounts, 5,078,345 txns), unchanged
across all points. Query: 2-hop chain, **no anchor**, with the predicate on
**both** edges:

    SELECT * FROM account AS a1, txn AS t1, account AS a2, txn AS t2, account AS a3
    WHERE a1.account_id = t1.acc_from AND a2.account_id = t1.acc_to
      AND a2.account_id = t2.acc_from AND a3.account_id = t2.acc_to
      AND t1.amount > θ AND t2.amount > θ;

Both edges filtered means the output shrinks ~quadratically in the per-edge
selectivity s (a surviving path needs both legs to pass), so a modest s range
covers 2.4K → 355M output rows. θ is the amount quantile at 1−s, computed by
`scripts/experiments/calibrate_e7_selectivity.py`:

| s (per edge) | θ (amount) | passing edges | filtered 2-hop rows |
|--------------|------------|---------------|---------------------|
| 0.001        | 416,225,565 | 5,077        | 2,404               |
| 0.01         | 13,524,530  | 50,784       | 82,596              |
| 0.05         | 623,757     | 253,911      | 753,013             |
| 0.1          | 137,275     | 507,835      | 2,478,413           |
| 0.25         | 12,298      | 1,269,555    | 12,272,345          |
| 0.5          | 1,415       | 2,538,725    | 39,282,354          |
| 1.0 (none)   | —           | 5,078,345    | 355,144,984         |

Machine budget: 471 GB RAM. ≤12.3M rows is comfortable; 39.3M is the risky
top filtered point and 355M (the unfiltered Graphite point) is expected to
OOM like Full MWJ — both are kept as honest data points.

## Row-count verification (first-class)

The x-axis of the result plot is the row count Graphite *actually produced*,
never the calibration table taken on faith:

1. **Every cell, every rep**: the runner compares sgx_app's `Result: N rows`
   against the pandas oracle (Σ over middle accounts of filtered-in-degree ×
   filtered-out-degree, `calibration.json`). Mismatch → the cell is INVALID:
   loud in stdout, `rows_match=0` in the CSVs, never silently averaged, drawn
   with a red ring by the plot.
2. **Double oracle**: `scripts/experiments/verify_e7_sqlite.py` runs the
   original SQL through SQLite on the small points (s=0.001, 0.01) and its
   COUNT(*) must equal the pandas oracle. Verified 2026-07-16: 2,404 and
   82,596 match on both paths, and the end-to-end Graphite runs return
   exactly those counts (`match=1`).

Verification is count-based by explicit scope decision (2026-07-16);
row-content diffing is out of scope for E7. In v2 BOTH systems are subject
to `rows_match` at every point — the two engines must agree with the oracle
and hence with each other, which is itself part of the decomposition claim
(same answer, different plan).

## Rewriter fix (prerequisite, landed with this experiment)

`scripts/rewrite_chain_query.py` used to keep only *account*-alias filters;
predicates on txn aliases were silently dropped — Graphite would have run
unfiltered while the runner labeled the point "s=0.001" (the exact failure
mode the row-count check exists to catch; the four old `aml_sel_*.sql`
drafts, superseded and deleted, had this problem plus a stray anchor). The
rewriter now maps `tY.col op v` to the hop built from that edge — edge
payload columns are unprefixed in the hop table, so `t1.amount > θ` becomes
`h1.amount > θ` — and raises on filters over unknown aliases instead of
dropping them.

## Runner mechanics

`scripts/experiments/run_e7_selectivity.py` (machinery imported from
`run_e3_cross_dataset`, structure cloned from `run_e5_density`):

- Points run smallest expected output first; per-system failure ratchet —
  once a system fails (OOM or timeout) at a point, every point with an
  expected output at least as large is recorded SKIPPED for that system
  instead of re-failed (all reps).
- Both systems run per point: Graphite = rewritten hop query over the
  per-rep hop table (latency = onehop_ms + mwj_ms of the same rep; the hop
  table is filter-independent and shared by all points); Full MWJ = the
  original query with its filters on the raw tables (no `--no-filter`).
- Defaults: 1 warm-up + 3 measurement reps, 64 threads, 7200s cell budget.
- CSVs (`raw_runs.csv`, `summary.csv`) are rewritten after every cell.

Run:

    python3 scripts/experiments/calibrate_e7_selectivity.py   # once per dataset
    python3 scripts/experiments/verify_e7_sqlite.py           # double-oracle gate
    python3 scripts/experiments/run_e7_selectivity.py
    python3 scripts/experiments/plot_e7_selectivity.py

## Presentation

`plot_e7_selectivity.py`: a single log-log panel — latency vs *measured*
filtered output rows, one swept line per system, every completed point
annotated with its latency (the log y-axis hides magnitudes otherwise),
TIMEOUT as an open marker at the budget, OOM as an ✕ at the floor, INVALID
cells ringed in red. Presentation names: Graphite and "Full MWJ (filtered)"
— the explicit label marks the E7-only deviation from the canonical
no-filter Full MWJ.

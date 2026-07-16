# E7: Predicate-Selectivity Sweep (fixed input, varying filtered output)

Status: designed, implemented, and run 2026-07-16. Results in
`results/e7_selectivity/` (summary.csv, e7_selectivity.png/.pdf).

## Results (2026-07-16 sweep, 1 run/cell, 64 threads)

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

## Claim demonstrated

Complement of E5. E5 held the input fixed and grew the *unfiltered* output:
baselines blow up, Graphite stays flat. The skeptic's follow-up is "Graphite
looks flat only because the queries are highly selective — what happens when
the filter lets more through?" E7 answers it: input and query structure are
held perfectly constant and only the predicate threshold θ moves, sweeping
the *filtered* output across five orders of magnitude. Expected shape:

- **Graphite** tracks the filtered output — honestly Θ(input + filtered
  output), converging toward Full-MWJ behavior (and possibly OOM) as the
  filter approaches a no-op. When the filter does no work, nobody can win.
- **Obliviator chained** cannot apply the predicate at all (`amount` is
  opaque payload to it) — flat θ-invariant line at its unfiltered-chain cost.
- **Full MWJ** runs `--no-filter`, so its execution is *identical* at every
  θ — one attempt, expected OOM at the 355M-row unfiltered output (as in E1).

Together E5+E7 pin the claim: Graphite's cost tracks the filtered output and
nothing else; the baselines' cost tracks the unfiltered output and nothing
else.

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
row-content diffing is out of scope for E7. Obliviator is exempt from
`rows_match` (perf-only: its converter collapses multi-edges, so it reports
149,212,726 unfiltered 2-hop rows — a known data-model quirk, not a
mismatch). Full MWJ, if it ever completes, is checked against the unfiltered
355,144,984.

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

- Points run smallest expected output first; once Graphite fails (OOM or
  timeout) at a point, every point with an expected output at least as large
  is recorded SKIPPED instead of re-failed (all reps).
- The hop table is filter-independent: built once per rep and shared by all
  points. Graphite latency = onehop_ms + mwj_ms of the same rep.
- Obliviator chained is θ-invariant: converted once, run once per rep.
- Full MWJ is attempted exactly once per invocation (identical work at every
  θ), after the sweep, with the cell timeout.
- Defaults: 1 warm-up + 3 measurement reps, 64 threads, 7200s/3600s budgets.
- CSVs (`raw_runs.csv`, `summary.csv`) are rewritten after every cell.

Run:

    python3 scripts/experiments/calibrate_e7_selectivity.py   # once per dataset
    python3 scripts/experiments/verify_e7_sqlite.py           # double-oracle gate
    python3 scripts/experiments/run_e7_selectivity.py
    python3 scripts/experiments/plot_e7_selectivity.py

## Presentation

`plot_e7_selectivity.py`: panel (a) — filtered output rows per selectivity
point (the knob works); panel (b) — latency vs *measured* filtered output
rows, log-log, slope-1 guide anchored at Graphite's largest completed point,
flat lines for the θ-invariant baselines, TIMEOUT as an open marker at the
budget, OOM as an ✕ at the floor, INVALID cells ringed in red. Presentation
names per CLAUDE.md: Graphite / Obliviator chained / Full MWJ.

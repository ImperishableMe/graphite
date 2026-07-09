# E5: Output-Sensitivity Experiment (fixed input size, varying density)

Status: designed, implemented, and run 2026-07-08. Results in
`results/e5_density/` (summary.csv, e5_density.png/.pdf).

## Results (2026-07-08 sweep, 1 run/cell, 64 threads)

Every cell's output_rows matched the stats.json ground truth exactly.

| variant | unfiltered rows | filtered rows | Graphite | Obliviator chained | Full MWJ |
|---------|-----------------|---------------|----------|--------------------|----------|
| uniform | 3,895,691       | 5             | 7.5 s    | 6.4 s              | 94 s     |
| low     | 9,214,537       | 346           | 7.5 s    | 14.6 s             | 208 s    |
| medium  | 24,435,486      | 945           | 7.5 s    | 40.7 s             | 537 s    |
| high    | 70,222,811      | 1,830         | 7.5 s    | 238.8 s            | 1602 s   |

Graphite is flat (7.49–7.54 s; ~7.4 s of that is the input-size-bound one-hop
+ reduced MWJ on the tiny filtered result). Both baselines scale ≈ linearly
with the unfiltered output (Full MWJ 17.0× over an 18.0× output spread;
Obliviator 37× — super-linear at the top end). At `high`, Graphite is 32×
faster than Obliviator chained and 213× faster than Full MWJ, on inputs of
identical size.

Numbering note: this is E5 in the *implemented* experiment series (`run_e1_main`,
`run_e2_scaling`, `run_e3_cross_dataset`, E4 query shapes on branch
`e4-query-shapes`). The E-numbers in the older `docs/experiments.md` planning
doc do not correspond to this series.

## Claim demonstrated

With input size held **perfectly constant**, the cost of the baseline systems
is driven by the *unfiltered* join output size, while Graphite's cost is driven
by input size plus the *filtered* output size — because Graphite fuses the
filter (the anchor predicate) into the algorithm instead of materializing the
full multi-way join and filtering afterwards.

E3 already shows this correlation across datasets, but there it is confounded
with dataset size and workload. E5 isolates the variable: four datasets with
byte-identical row counts whose unfiltered 2-hop output spans ~22×.

## Why "density" means hub concentration, not edge count

The unfiltered 2-hop chain output is exactly

    |output| = Σ over accounts a of  in_deg(a) × out_deg(a)

(the number of (t1, t2) pairs sharing a middle account). Input sizes must stay
equal, so the edge count M cannot move; the only remaining knob is **degree
concentration**. A graph where in- and out-edges pile onto the same hub
accounts has a far larger 2-hop expansion than a uniform graph with identical
row counts.

## Dataset design

Four Banking W1 variants, all **200,000 accounts / 1,000,000 transactions /
40,000 owners**, seed 42. Scale rationale: at banking_1M the p=0 baseline is
already 23M output rows, so a 20× density sweep would push Full MWJ past its
timeout or into OOM (as on hi_small in E3); at 200k/1M edges every cell of the
sweep completes.

Generation scheme (extends `scripts/generate_banking_scaled.py` with new,
default-off flags — existing datasets are unaffected):

- Designate **H = 1,000 hub accounts** (the highest account ids,
  `199001..200000`).
- Every edge endpoint (source and destination **independently**) is redirected
  to a uniformly random hub with probability **p**; otherwise it follows the
  existing scheme (source ~ Zipf(1.5), destination ~ uniform).
- The `(acc_from, acc_to)` pair-uniqueness and no-self-loop constraints of the
  existing generator are kept.

The hub term contributes ≈ p²·M²/H to Σ in×out on top of the ≈ M·(M/N)
background. Realized values (seed 42, from each variant's `stats.json`;
design-time simulation estimates in parentheses):

| variant | p    | unfiltered 2-hop rows | filtered 2-hop rows | hub-hub pairs | dir name |
|---------|------|-----------------------|---------------------|---------------|----------|
| uniform | 0.00 | 3,895,691 (≈4.6M)     | 5                   | 0             | `banking_e5_uniform` |
| low     | 0.10 | 9,214,537 (≈11.1M)    | 346                 | 12,173        | `banking_e5_low`     |
| medium  | 0.20 | 24,435,486 (≈33.0M)   | 945                 | 45,432        | `banking_e5_medium`  |
| high    | 0.35 | 70,222,811 (≈104.0M)  | 1,830               | 125,761       | `banking_e5_high`    |

That is an 18× spread in unfiltered output at identical input row counts. The
realized values sit ~20–30% below the simulation because the generator retries
pair collisions by redrawing only the destination (the simulation redrew both
endpoints), which truncates the Zipf head differently. Uniqueness feasibility:
worst-case hub-hub occupancy is 12.6% of the H·(H−1) possible pairs, far from
saturation, so generation does not stall.

Generator caveat (fixed during implementation): the per-source exhaustion
threshold must count only *reachable* destinations — at p=0 the hubs are never
proposed as destinations, so counting them leaves the Zipf head account
spinning forever in the destination-rejection loop once it saturates.

### 10M-edge tier

A second size tier (`run_e5_density.py --tier 10M`) repeats the sweep at
**2M accounts / 10M txns** per variant (dirs `banking_e5_10M_*`), with the hub
count scaled to **H = 10,000** so the hub term (≈ p²·M²/H) keeps the same
relative spread over the ≈ 5·M background. Results in
`results/e5_density_10M/` (2026-07-08 sweep, 471 GB RAM, 64 threads; every
completed cell's output_rows matched stats.json exactly):

| variant | unfiltered rows | filtered rows | Graphite | Obliviator chained | Full MWJ |
|---------|-----------------|---------------|----------|--------------------|----------|
| uniform | 47,227,107      | 17            | 91.8 s   | 146.5 s            | 1233 s (20.5 m) |
| low     | 103,714,693     | 496           | 90.8 s   | TIMEOUT > 1 h      | 2629 s (43.8 m) |
| medium  | 242,493,946     | 1,137         | 92.0 s   | TIMEOUT > 1 h      | OOM (SIGKILL)   |
| high    | 720,806,166     | 1,963         | 91.2 s   | OOM (SIGKILL)      | OOM (SIGKILL)   |

Graphite stays flat at ~91 s (the input-size term: MWJ over the 10M-row hop
table; online one-hop is ~1.1 s, offline buildNodeIndex ~8 min cold and
excluded per the ONLINE reporting convention). Both baselines *collapse*
rather than merely slow down: Full MWJ is OOM-killed from `medium` up (its
distribute-expand working set at ≥242M output rows exceeds 471 GB), and
Obliviator degrades super-linearly much earlier than its per-row rate at
`uniform` predicts (~5 min projected for `low`, >1 h observed) before being
OOM-killed at `high` — so severely that Full MWJ, nominally the slowest
system, completes `low` while Obliviator does not. A re-measurement of the
Obliviator `low` cell with an extended budget ran **past 3 h** (64 threads at
full load, RSS steady at ~59 GB — compute-bound, not thrashing) before being
stopped, i.e. a **>36×** blow-up over the ~5 min linear projection from its
own `uniform` rate. Context from E3: the same kernel also TIMEOUTs on AML
hi_medium and hi_large 2-hop, so collapse at scale is not unique to the E5
datasets; the E5-specific trigger is the engineered skew (Zipf head with ~2M
out-edges + 10k hubs of ~100 in × ~100 out) appearing at the 10M-edge scale —
the identical hub structure at the 1M tier costs it at most 2×. Per-step
attribution would need a rerun with line-buffered stdout (the kernel's stdout
is lost on timeout); deferred, since Obliviator is perf-only in our
comparisons.

### 5M-edge tier

Middle tier (`--tier 5M`): 1M accounts / 5M txns, H = 5,000, dirs
`banking_e5_5M_*`, results `results/e5_density_5M/`. Motivation: at 10M edges
the baselines mostly fail (bounds, not curves); the 5M tier shows the
divergence with completed baseline cells. Results (2026-07-09 sweep; every
completed cell matched stats.json exactly):

| variant | unfiltered rows | filtered rows | Graphite | Obliviator chained | Full MWJ |
|---------|-----------------|---------------|----------|--------------------|----------|
| uniform | 26,113,069      | 8             | 42.8 s   | 54.8 s             | 629 s (10.5 m)  |
| low     | 49,466,164      | 407           | 42.7 s   | 142.3 s            | 1184 s (19.7 m) |
| medium  | 123,599,405     | 902           | 42.9 s   | 335.0 s            | OOM (SIGKILL)   |
| high    | 362,117,655     | 1,823         | 43.1 s   | 1194 s (19.9 m)    | OOM (SIGKILL)   |

**This is the headline tier.** Graphite is flat at ~43 s; Obliviator completes
everywhere with a full mildly-super-linear curve (21.8× time over a 13.9×
output spread; 2.1 → 3.3 s per M rows — its 10M-tier collapse sits between
these two scales); Full MWJ rides the slope-1 guide then is OOM-killed from
124M output rows. At `high`, Graphite is 27.7× faster than Obliviator and
unboundedly faster than Full MWJ, on byte-identical input sizes. Full MWJ's
memory ceiling brackets to 104M–124M output rows on this 471 GB machine
(completed 103.7M at the 10M tier, OOM at 123.6M here).

Both output columns grow with p, and the absolute gap between them explodes —
which is the story: same input, diverging outputs.

### Anchor planting

The filter is the anchor predicate `a1.account_id = 46` from the existing
`input/queries/banking_2hop.sql` (no query edit needed; account 46 is a
non-hub).

Decision: the anchor is **excluded from background draws** and **planted
last** with a fixed out-degree of 5, with destinations drawn from the
destination distribution (uniform/hub mix) **conditioned on out-degree ≥ 1**
(resample until the candidate has at least one out-edge; planting happens
after background generation so out-degrees are known).

Rationale: under Zipf(1.5) most accounts have out-degree 0, so unconditioned
destinations make the filtered 2-hop result empty at p=0 (confirmed in
simulation) — a degenerate Graphite cell. Drawing from the *source* (Zipf)
distribution instead was considered and rejected: the Zipf head accounts carry
~40% of all edges, so a single head hit would inflate the filtered output to
~10⁵ rows and make it noisy across variants. Conditioning the destination
distribution on out-degree ≥ 1 gives a filtered result that is non-empty at
p=0 (realized: 5 rows — a uniform draw over active accounts overwhelmingly
hits out-degree-1 accounts, which dominate the population) and grows
organically with p (anchor neighbors are hubs with probability p; realized:
5 → 346 → 945 → 1,830).

### Ground truth at generation time

The generator (under the E5 flags) emits `stats.json` next to the CSVs with:

- exact `unfiltered_2hop_rows` = Σ in×out (cheap degree pass),
- exact `filtered_2hop_rows` for anchor 46,
- degree distribution summary (max/mean in- and out-degree, hub occupancy).

These are the x-axis values, available before any oblivious binary runs, and a
cross-check: Full MWJ and Obliviator `output_rows` must equal
`unfiltered_2hop_rows` exactly; Graphite's must equal `filtered_2hop_rows`.

### Calibration note

The realized Σ in×out may drift ±20% from the simulation (the real generator's
collision-retry loop shifts mass off the Zipf head slightly differently). If
the spread compresses, nudge p for the top variant (0.35 → 0.40) and
regenerate only that dataset.

## Query, systems, protocol

- **Query:** 2-hop chain (`banking_2hop.sql`), anchor 46. 2-hop is the highest
  hop count all three systems run reliably; Obliviator's chained k-hop kernel
  supports it.
- **Systems:** the three canonical systems (see CLAUDE.md "Experiment
  Comparison Systems"): Graphite (runner key `nebuladb`), Obliviator chained
  (`obliviator_chained`, payload shape 3 4), Full MWJ (`full_mwj_no_filter`,
  `sgx_app --no-filter`). Obliviator is perf-only (its known correctness bug is
  accepted, as in E1/E3).
- **Runner:** `scripts/experiments/run_e5_density.py`, a clone of the E3
  runner with the dataset table replaced by the four variants (all
  `workload: banking`, generated on demand with seed 42, row-count validity
  check extended to verify `stats.json` matches the variant's p). Per-cell
  OOM/TIMEOUT capture, flush-after-every-cell CSVs, and thread defaults carry
  over unchanged. A `banking_e5_smoke` entry (10k accounts, default-off) is
  included for smoke tests.
- **Measurement:** 1 measurement run, 0 warm-ups per cell (cells are minutes
  long; same protocol as E3).

## Expected outcome and runtime budget

Scaling from measured E3 rates (Full MWJ ≈ 20–25 s per M output rows,
Obliviator ≈ 2–3.5 s/M, Graphite ≈ onehop on 1M edges + small anchored MWJ):

- **Graphite:** roughly constant across variants (order tens of seconds); the
  one-hop stage is input-size-bound (hop table = M rows regardless of p) and
  the reduced MWJ sees only the filtered rows.
- **Obliviator chained:** ~15 s → ~6 min, rising ≈ linearly with unfiltered
  rows.
- **Full MWJ:** ~2 min → ~40–60 min at `high`; completes within the 2 h cell
  timeout, so the plot has no failure holes.

Whole sweep ≈ 1.5–2.5 h.

## Presentation

`scripts/experiments/plot_e5_density.py`, following the E3 plot conventions
(system naming per CLAUDE.md: **Graphite**, never "NebulaDB"; **Full MWJ**
without a "no-filter" qualifier):

- **Panel A:** latency (log) vs unfiltered 2-hop output rows (log). Three
  lines; expected: baselines rise with slope ≈ 1, Graphite flat.
- **Panel B:** filtered vs unfiltered output rows per variant (log-scale
  bars) — makes the "same input, diverging outputs" setup visually explicit.

Outputs under `results/e5_density/`.

## Correctness validation

Before the timed sweep, validate the pipeline on the smoke variant: run
Graphite's decomposed pipeline and `sgx_app` (filtered) against the SQLite
baseline via the existing test infrastructure, and check the stats.json
cross-check rows on the smallest full variant. Obliviator is exempt
(perf-only).

## Implementation checklist

1. `scripts/generate_banking_scaled.py`: `--hub-fraction`, `--hub-count`,
   `--plant-anchor` flags (default off ⇒ byte-identical current behavior) +
   `stats.json` emission under the E5 flags.
2. Generate the four variants; verify realized Σ in×out spread; recalibrate p
   if needed.
3. `scripts/experiments/run_e5_density.py` (E3 clone, new dataset table).
4. `scripts/experiments/plot_e5_density.py`.
5. Smoke test end-to-end on `banking_e5_smoke`, then full sweep.

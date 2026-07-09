#!/usr/bin/env python3
"""
Banking Dataset Generator for Oblivious Multi-Way Join Testing

Generates realistic banking data with parameterized sizes:
- Owner table: num_accounts / 5 owners
- Account table: num_accounts accounts with balances and owner IDs
- Transaction table: 5 * num_accounts transactions between accounts

Uses Zipfian distribution for transaction counts to create realistic variance
(few very active accounts, most moderately active).

Usage: python3 generate_banking_scaled.py <num_accounts> <output_dir>

Example:
    python3 scripts/generate_banking_scaled.py 5000 input/plaintext/banking_5000
    python3 scripts/generate_banking_scaled.py 10000 input/plaintext/banking
"""

import argparse
import bisect
import csv
import json
import random
from collections import defaultdict
from pathlib import Path

# System bounds from enclave_types.h
JOIN_ATTR_MIN = -1_073_741_820
JOIN_ATTR_MAX = 1_073_741_820

# Data ranges
MIN_BALANCE = 0
MAX_BALANCE = 1_000_000
MIN_AMOUNT = 1
MAX_AMOUNT = 100_000
MIN_TIMESTAMP = 1
MAX_TIMESTAMP = 365000


class ZipfianSampler:
    """Pre-computed Zipfian distribution for fast O(log n) sampling."""

    def __init__(self, items, alpha=1.5):
        self.items = items
        n = len(items)
        # Pre-compute cumulative weights
        weights = [1.0 / pow(i + 1, alpha) for i in range(n)]
        self.cumulative = []
        total = 0.0
        for w in weights:
            total += w
            self.cumulative.append(total)
        self.total_weight = total

    def sample(self):
        """Sample an item using binary search - O(log n)."""
        rand_val = random.uniform(0, self.total_weight)
        idx = bisect.bisect_left(self.cumulative, rand_val)
        return self.items[min(idx, len(self.items) - 1)]


def zipfian_choice(items, alpha=1.5):
    """Select an item using Zipfian distribution (legacy, O(n) - use ZipfianSampler for repeated calls)."""
    n = len(items)
    weights = [1.0 / pow(i + 1, alpha) for i in range(n)]
    total_weight = sum(weights)

    rand_val = random.uniform(0, total_weight)
    cumulative = 0.0
    for i, weight in enumerate(weights):
        cumulative += weight
        if rand_val <= cumulative:
            return items[i]
    return items[-1]


def generate_owners(num_owners):
    """Generate owner table."""
    owners = []
    for owner_id in range(1, num_owners + 1):
        owners.append({
            'ow_id': owner_id,
            'name_placeholder': owner_id
        })
    return owners


def generate_accounts(num_accounts, num_owners, quiet=False):
    """Generate account table with realistic distribution."""
    accounts = []
    accounts_per_owner = defaultdict(int)

    for account_id in range(1, num_accounts + 1):
        rand_val = random.random()
        if rand_val < 0.7:
            owner_id = random.randint(1, num_owners)
        else:
            owner_id = random.randint(1, max(1, num_owners // 5))

        balance = random.randint(MIN_BALANCE, MAX_BALANCE)

        accounts.append({
            'account_id': account_id,
            'balance': balance,
            'owner_id': owner_id
        })
        accounts_per_owner[owner_id] += 1

    if not quiet:
        print(f"Generated {len(accounts)} accounts")
        print(f"Owners with accounts: {len(accounts_per_owner)}")
        print(f"Max accounts per owner: {max(accounts_per_owner.values())}")
        print(f"Avg accounts per owner: {sum(accounts_per_owner.values()) / len(accounts_per_owner):.2f}")

    return accounts


def generate_transactions(accounts, num_transactions, quiet=False):
    """Generate transaction table with Zipfian distribution and unique (acc_from, acc_to) pairs."""
    transactions = []
    account_ids = [acc['account_id'] for acc in accounts]
    txn_counts = defaultdict(int)
    seen_pairs = set()
    exhausted_sources = set()

    for _ in range(num_transactions):
        acc_from = zipfian_choice(account_ids, alpha=1.5)
        while acc_from in exhausted_sources:
            acc_from = zipfian_choice(account_ids, alpha=1.5)

        acc_to = acc_from
        while acc_to == acc_from or (acc_from, acc_to) in seen_pairs:
            acc_to = random.choice(account_ids)

        seen_pairs.add((acc_from, acc_to))
        txn_counts[acc_from] += 1
        if txn_counts[acc_from] >= len(account_ids) - 1:
            exhausted_sources.add(acc_from)

        amount = random.randint(MIN_AMOUNT, MAX_AMOUNT)
        timestamp = random.randint(MIN_TIMESTAMP, MAX_TIMESTAMP)

        transactions.append({
            'txn_id': len(transactions) + 1,
            'acc_from': acc_from,
            'acc_to': acc_to,
            'amount': amount,
            'txn_time': timestamp
        })

    if not quiet:
        txn_list = sorted(txn_counts.values(), reverse=True)
        print(f"\nGenerated {len(transactions)} transactions")
        print(f"Accounts with outgoing transactions: {len(txn_counts)}")
        print(f"Top 10 most active accounts: {txn_list[:10]}")
        print(f"Median transactions per account: {txn_list[len(txn_list)//2] if txn_list else 0}")
        print(f"Avg transactions per active account: {sum(txn_list) / len(txn_list):.2f}")
        mean = sum(txn_list) / len(txn_list) if txn_list else 0
        variance = sum((x - mean) ** 2 for x in txn_list) / len(txn_list) if txn_list else 0
        print(f"Variance in transaction counts: {variance:.2f}")

    return transactions


def write_csv(output_dir, filename, data, fieldnames):
    """Write data to CSV file with Unix line endings (required by the C++ CSV parser)."""
    filepath = output_dir / filename

    with open(filepath, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator='\n')
        writer.writeheader()
        writer.writerows(data)


def generate_transactions_streaming(account_ids, num_transactions, output_dir, batch_size=1_000_000, quiet=False):
    """Generate transactions in streaming mode with unique (acc_from, acc_to) pairs."""
    filepath = output_dir / 'txn.csv'
    fieldnames = ['txn_id', 'acc_from', 'acc_to', 'amount', 'txn_time']

    # Pre-compute Zipfian distribution for O(log n) sampling
    sampler = ZipfianSampler(account_ids, alpha=1.5)
    seen_pairs = set()
    source_counts = defaultdict(int)
    num_accounts = len(account_ids)
    exhausted_sources = set()

    with open(filepath, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator='\n')
        writer.writeheader()

        written = 0
        while written < num_transactions:
            batch_count = min(batch_size, num_transactions - written)

            for _ in range(batch_count):
                acc_from = sampler.sample()
                while acc_from in exhausted_sources:
                    acc_from = sampler.sample()

                acc_to = acc_from
                while acc_to == acc_from or (acc_from, acc_to) in seen_pairs:
                    acc_to = random.choice(account_ids)

                seen_pairs.add((acc_from, acc_to))
                source_counts[acc_from] += 1
                if source_counts[acc_from] >= num_accounts - 1:
                    exhausted_sources.add(acc_from)

                amount = random.randint(MIN_AMOUNT, MAX_AMOUNT)
                timestamp = random.randint(MIN_TIMESTAMP, MAX_TIMESTAMP)

                written += 1
                writer.writerow({
                    'txn_id': written,
                    'acc_from': acc_from,
                    'acc_to': acc_to,
                    'amount': amount,
                    'txn_time': timestamp
                })

            if not quiet:
                print(f"  Written {written:,}/{num_transactions:,} transactions ({100*written//num_transactions}%)")

    if not quiet:
        print(f"Generated {num_transactions:,} transactions (streaming mode)")


def generate_transactions_streaming_e5(num_accounts, num_transactions, output_dir,
                                       hub_fraction, hub_count, anchor,
                                       anchor_out_degree, quiet=False):
    """E5 density-variant transaction generator (streaming).

    See docs/e5_output_sensitivity.md. Holds row counts fixed while varying
    degree concentration:
      - The hub_count highest account ids are designated hubs.
      - Every background edge endpoint (source and destination independently)
        is redirected to a uniformly random hub with probability hub_fraction;
        otherwise source ~ Zipf(1.5) and destination ~ uniform, both over
        non-hub ids.
      - The anchor account is excluded from background draws and planted last
        with exactly anchor_out_degree out-edges whose destinations follow the
        destination distribution conditioned on out-degree >= 1 (guarantees a
        non-empty filtered 2-hop result at hub_fraction = 0).

    (acc_from, acc_to) uniqueness and no-self-loop constraints match the
    default generator. Returns a stats dict with the exact unfiltered and
    anchor-filtered 2-hop output sizes (ground truth for the E5 sweep).
    """
    first_hub = num_accounts - hub_count + 1
    hub_ids = list(range(first_hub, num_accounts + 1))
    background_ids = [i for i in range(1, first_hub) if i != anchor]

    sampler = ZipfianSampler(background_ids, alpha=1.5)
    seen_pairs = set()
    out_degree = defaultdict(int)
    in_degree = defaultdict(int)
    exhausted_sources = set()
    # Distinct destinations reachable per source: every background id except
    # itself, plus — only when hub_fraction > 0 — the hubs (at p=0 draw_dest
    # never proposes a hub, so counting hubs here would leave the Zipf head
    # account spinning forever in the destination-rejection loop once it has
    # consumed every reachable destination).
    if hub_fraction > 0:
        max_out = len(background_ids) + hub_count - 1
    else:
        max_out = len(background_ids) - 1

    def draw_source():
        if hub_fraction > 0 and random.random() < hub_fraction:
            return random.choice(hub_ids)
        return sampler.sample()

    def draw_dest():
        if hub_fraction > 0 and random.random() < hub_fraction:
            return random.choice(hub_ids)
        return random.choice(background_ids)

    num_background = num_transactions - anchor_out_degree
    filepath = output_dir / 'txn.csv'
    fieldnames = ['txn_id', 'acc_from', 'acc_to', 'amount', 'txn_time']

    with open(filepath, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator='\n')
        writer.writeheader()

        written = 0
        while written < num_background:
            acc_from = draw_source()
            while acc_from in exhausted_sources:
                acc_from = draw_source()

            acc_to = acc_from
            while acc_to == acc_from or (acc_from, acc_to) in seen_pairs:
                acc_to = draw_dest()

            seen_pairs.add((acc_from, acc_to))
            out_degree[acc_from] += 1
            in_degree[acc_to] += 1
            if out_degree[acc_from] >= max_out:
                exhausted_sources.add(acc_from)

            written += 1
            writer.writerow({
                'txn_id': written,
                'acc_from': acc_from,
                'acc_to': acc_to,
                'amount': random.randint(MIN_AMOUNT, MAX_AMOUNT),
                'txn_time': random.randint(MIN_TIMESTAMP, MAX_TIMESTAMP),
            })
            if not quiet and written % 1_000_000 == 0:
                print(f"  Written {written:,}/{num_background:,} background transactions")

        # Plant the anchor's out-edges: destinations from the destination
        # distribution conditioned on out_degree >= 1.
        anchor_dests = []
        while len(anchor_dests) < anchor_out_degree:
            d = draw_dest()
            if out_degree[d] == 0 or d == anchor or (anchor, d) in seen_pairs:
                continue
            seen_pairs.add((anchor, d))
            out_degree[anchor] += 1
            in_degree[d] += 1
            anchor_dests.append(d)
            written += 1
            writer.writerow({
                'txn_id': written,
                'acc_from': anchor,
                'acc_to': d,
                'amount': random.randint(MIN_AMOUNT, MAX_AMOUNT),
                'txn_time': random.randint(MIN_TIMESTAMP, MAX_TIMESTAMP),
            })

    # Exact 2-hop ground truth (chain query joins on the middle account).
    unfiltered_2hop = sum(in_degree[a] * out_degree.get(a, 0) for a in in_degree)
    filtered_2hop = sum(out_degree[d] for d in anchor_dests)
    hub_hub_pairs = sum(1 for (s, d) in seen_pairs if s >= first_hub and d >= first_hub)

    stats = {
        'num_accounts': num_accounts,
        'num_transactions': num_transactions,
        'hub_fraction': hub_fraction,
        'hub_count': hub_count,
        'first_hub_id': first_hub,
        'anchor': anchor,
        'anchor_out_degree': anchor_out_degree,
        'anchor_dests': sorted(anchor_dests),
        'anchor_dest_out_degrees': sorted(
            (out_degree[d] for d in anchor_dests), reverse=True),
        'unfiltered_2hop_rows': unfiltered_2hop,
        'filtered_2hop_rows': filtered_2hop,
        'max_out_degree': max(out_degree.values()),
        'max_in_degree': max(in_degree.values()),
        'max_hub_out_degree': max(
            (out_degree.get(h, 0) for h in hub_ids), default=0),
        'hub_hub_pairs': hub_hub_pairs,
    }

    if not quiet:
        print(f"Generated {num_transactions:,} transactions (E5 streaming mode)")
        print(f"  unfiltered 2-hop rows : {unfiltered_2hop:,}")
        print(f"  filtered  2-hop rows  : {filtered_2hop:,} (anchor {anchor})")
        print(f"  hub-hub pairs         : {hub_hub_pairs:,}")

    return stats


def validate_data(accounts, transactions):
    """Validate foreign key constraints and data bounds."""
    account_ids = {acc['account_id'] for acc in accounts}

    for txn in transactions:
        if txn['acc_from'] not in account_ids or txn['acc_to'] not in account_ids:
            raise ValueError("Foreign key validation failed!")

    for acc in accounts:
        if not (JOIN_ATTR_MIN <= acc['balance'] <= JOIN_ATTR_MAX):
            raise ValueError(f"Balance out of range: {acc['balance']}")

    for txn in transactions:
        if not (JOIN_ATTR_MIN <= txn['amount'] <= JOIN_ATTR_MAX):
            raise ValueError(f"Amount out of range: {txn['amount']}")


def main():
    parser = argparse.ArgumentParser(
        description='Banking Dataset Generator for Oblivious Multi-Way Join Testing'
    )
    parser.add_argument('num_accounts', type=int, help='Number of accounts to generate')
    parser.add_argument('output_dir', type=Path, help='Output directory path')
    parser.add_argument('--seed', type=int, default=42,
                        help='Random seed (default: 42)')
    parser.add_argument('--txn-ratio', type=int, default=5,
                        help='Transaction-to-account ratio (default: 5)')
    parser.add_argument('--streaming', action='store_true',
                        help='Use streaming mode for memory-efficient generation of large datasets')
    parser.add_argument('--quiet', '-q', action='store_true',
                        help='Suppress detailed output (for parallel execution)')
    # E5 density-variant flags (docs/e5_output_sensitivity.md). Default off:
    # without --plant-anchor the generator behaves exactly as before.
    parser.add_argument('--plant-anchor', type=int, default=None, metavar='ID',
                        help='E5 mode: plant this account id with a fixed '
                             'out-degree (--anchor-out-degree) and emit '
                             'stats.json with exact 2-hop output sizes')
    parser.add_argument('--anchor-out-degree', type=int, default=5,
                        help='Planted out-degree of the anchor account '
                             '(E5 mode, default: 5)')
    parser.add_argument('--hub-fraction', type=float, default=0.0,
                        help='E5 mode: probability that each edge endpoint is '
                             'redirected to a random hub (default: 0.0)')
    parser.add_argument('--hub-count', type=int, default=1000,
                        help='E5 mode: number of hub accounts, the highest '
                             'account ids (default: 1000)')
    args = parser.parse_args()

    e5_mode = args.plant_anchor is not None
    if not e5_mode and args.hub_fraction > 0:
        parser.error('--hub-fraction requires --plant-anchor (E5 mode)')
    if e5_mode:
        if not (0.0 <= args.hub_fraction < 1.0):
            parser.error('--hub-fraction must be in [0, 1)')
        if not (0 < args.hub_count < args.num_accounts):
            parser.error('--hub-count must be in (0, num_accounts)')
        first_hub = args.num_accounts - args.hub_count + 1
        if not (1 <= args.plant_anchor < first_hub):
            parser.error(f'--plant-anchor must be a non-hub account id '
                         f'(1 <= id < {first_hub})')

    num_accounts = args.num_accounts
    output_dir = args.output_dir
    quiet = args.quiet

    # Calculate dependent sizes
    num_transactions = args.txn_ratio * num_accounts
    num_owners = max(1, num_accounts // 5)

    # Feasibility check: can't have more unique (from, to) pairs than num_accounts*(num_accounts-1)
    max_unique_pairs = num_accounts * (num_accounts - 1)
    if num_transactions > max_unique_pairs:
        print(f"ERROR: num_transactions ({num_transactions:,}) exceeds maximum unique "
              f"(acc_from, acc_to) pairs ({max_unique_pairs:,}) for {num_accounts:,} accounts.")
        print("Reduce --txn-ratio or increase num_accounts.")
        raise SystemExit(1)

    # Auto-enable streaming for large datasets
    streaming = args.streaming or (num_transactions > 100_000)

    random.seed(args.seed)

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    if not quiet:
        print("=" * 60)
        print("Banking Dataset Generator")
        print("=" * 60)

    # Generate data
    if not quiet:
        print(f"\n1. Generating {num_owners:,} owners...")
    owners = generate_owners(num_owners)

    if not quiet:
        print(f"\n2. Generating {num_accounts:,} accounts...")
    accounts = generate_accounts(num_accounts, num_owners, quiet=quiet)

    # Write owner and account files first
    if not quiet:
        print("\n3. Writing owner and account CSV files...")
    write_csv(output_dir, 'owner.csv', owners, ['ow_id', 'name_placeholder'])
    write_csv(output_dir, 'account.csv', accounts, ['account_id', 'balance', 'owner_id'])

    # Generate transactions (streaming or in-memory)
    if not quiet:
        print(f"\n4. Generating {num_transactions:,} transactions...")

    account_ids = [acc['account_id'] for acc in accounts]

    if e5_mode:
        # E5 density variant: always streaming, emits stats.json ground truth.
        stats = generate_transactions_streaming_e5(
            num_accounts, num_transactions, output_dir,
            args.hub_fraction, args.hub_count, args.plant_anchor,
            args.anchor_out_degree, quiet=quiet)
        stats['seed'] = args.seed
        with open(output_dir / 'stats.json', 'w') as f:
            json.dump(stats, f, indent=2)
        if not quiet:
            print(f"E5 stats written to {output_dir / 'stats.json'}")
    elif streaming:
        # Streaming mode: write directly to file
        generate_transactions_streaming(account_ids, num_transactions, output_dir, quiet=quiet)
    else:
        # In-memory mode: generate all then write
        transactions = generate_transactions(accounts, num_transactions, quiet=quiet)
        validate_data(accounts, transactions)
        if not quiet:
            print("\n✓ All data validated successfully!")
        write_csv(output_dir, 'txn.csv', transactions, ['txn_id', 'acc_from', 'acc_to', 'amount', 'txn_time'])

    if not quiet:
        print("\n" + "=" * 60)
        print("Dataset generation complete!")
        print(f"Output directory: {output_dir.absolute()}")
        print("=" * 60)
    else:
        print(f"Generated: {output_dir} ({num_accounts:,} accounts, {num_transactions:,} txns)")


if __name__ == '__main__':
    main()

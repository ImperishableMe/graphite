"""Convert LDBC SNB (W2) CSV files to Obliviator FK input format for 1-hop.

SNB variant of convert_patents_1hop.py. The differences from the patents
converter:
  - account (person) schema is (account_id, city_id, gender, birthday), so the
    account payload written to src.txt is three columns (city_id gender
    birthday).
  - txn (mirrored knows edge) schema is (txn_id, acc_from, acc_to, knows_day),
    so the txn payload carries knows_day after the endpoint:
    (txn_id, acc_to, knows_day) / (txn_id, acc_from, knows_day).
Columns are mapped by header name, so column order in the CSVs is irrelevant.

Produces two input files:
  - src file: joins txn.acc_from = account.account_id
  - dst file: joins txn.acc_to   = account.account_id

Each txn line carries txn_id as the first field of the data payload, so the
two FK kernel results can be later stitched together by txn_id (the unique
edge identifier) in the 1-hop driver.

Usage:
  python3 convert_snb_1hop.py <account.csv> <txn.csv> <out_src.txt> <out_dst.txt>
"""

import sys

def main():
    if len(sys.argv) != 5:
        print(f"usage: {sys.argv[0]} <account.csv> <txn.csv> <out_src.txt> <out_dst.txt>")
        sys.exit(1)

    account_file, txn_file, out_src, out_dst = sys.argv[1:]

    # Read accounts; map columns by header so schema column-order is irrelevant.
    accounts = []
    with open(account_file) as f:
        header = f.readline().strip().split(',')
        try:
            i_id, i_city, i_gen, i_bd = (
                header.index(c) for c in ("account_id", "city_id", "gender", "birthday")
            )
        except ValueError as e:
            sys.exit(f"{account_file}: missing column in header {header}: {e}")
        cols_used = (i_id, i_city, i_gen, i_bd)
        for line in f:
            parts = line.strip().split(',')
            if len(parts) <= max(cols_used):
                continue
            accounts.append((parts[i_id], parts[i_city], parts[i_gen], parts[i_bd]))

    # Read knows edges; map by header. txn_id is required (the stitch key in the
    # 1-hop driver to align the two FK-join results).
    txns = []
    with open(txn_file) as f:
        header = f.readline().strip().split(',')
        try:
            i_id, i_from, i_to, i_day = (
                header.index(c) for c in ("txn_id", "acc_from", "acc_to", "knows_day")
            )
        except ValueError as e:
            sys.exit(f"{txn_file}: missing column in header {header}: {e}")
        cols_used = (i_id, i_from, i_to, i_day)
        for line in f:
            parts = line.strip().split(',')
            if len(parts) <= max(cols_used):
                continue
            txns.append((parts[i_id], parts[i_from], parts[i_to], parts[i_day]))

    num_accounts = len(accounts)
    num_txns = len(txns)
    print(f"Accounts: {num_accounts}, Transactions: {num_txns}")

    # Source join: key=acc_from for txns, key=account_id for accounts.
    # account data: <city_id gender birthday>; txn data: <txn_id,acc_to,knows_day>.
    with open(out_src, 'w') as f:
        f.write(f"{num_accounts} {num_txns}\n\n")
        for aid, city, gen, bd in accounts:
            f.write(f"{aid} {city} {gen} {bd}\n")
        f.write("\n")
        for txn_id, acc_from, acc_to, day in txns:
            f.write(f"{acc_from} {txn_id},{acc_to},{day}\n")

    # Destination join: key=acc_to for txns, key=account_id for accounts.
    # account data: <city_id gender birthday>; txn data: <txn_id,acc_from,knows_day>.
    with open(out_dst, 'w') as f:
        f.write(f"{num_accounts} {num_txns}\n\n")
        for aid, city, gen, bd in accounts:
            f.write(f"{aid} {city} {gen} {bd}\n")
        f.write("\n")
        for txn_id, acc_from, acc_to, day in txns:
            f.write(f"{acc_to} {txn_id},{acc_from},{day}\n")

    print(f"Written: {out_src} (source join), {out_dst} (destination join)")

if __name__ == "__main__":
    main()

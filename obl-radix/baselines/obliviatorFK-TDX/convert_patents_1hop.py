"""Convert SNAP cit-Patents (W3) CSV files to Obliviator FK input format for 1-hop.

Patent variant of convert_aml_1hop.py. The differences from the AML converter:
  - account schema is (account_id, gyear, cat, claims), so the account payload
    written to src.txt is three columns (gyear cat claims) instead of one.
  - txn (citation) schema is (txn_id, acc_from, acc_to) with no edge attributes,
    so the txn payload is just (txn_id, acc_to)/(txn_id, acc_from).
Columns are mapped by header name, so column order in the CSVs is irrelevant.

Produces two input files:
  - src file: joins txn.acc_from = account.account_id
  - dst file: joins txn.acc_to   = account.account_id

Each txn line carries txn_id as the first field of the data payload, so the
two FK kernel results can be later stitched together by txn_id (the unique
edge identifier) in the 1-hop driver.

Usage:
  python3 convert_patents_1hop.py <account.csv> <txn.csv> <out_src.txt> <out_dst.txt>
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
            i_id, i_gy, i_cat, i_cl = (
                header.index(c) for c in ("account_id", "gyear", "cat", "claims")
            )
        except ValueError as e:
            sys.exit(f"{account_file}: missing column in header {header}: {e}")
        cols_used = (i_id, i_gy, i_cat, i_cl)
        for line in f:
            parts = line.strip().split(',')
            if len(parts) <= max(cols_used):
                continue
            accounts.append((parts[i_id], parts[i_gy], parts[i_cat], parts[i_cl]))

    # Read citations; map by header. txn_id is required (the stitch key in the
    # 1-hop driver to align the two FK-join results).
    txns = []
    with open(txn_file) as f:
        header = f.readline().strip().split(',')
        try:
            i_id, i_from, i_to = (
                header.index(c) for c in ("txn_id", "acc_from", "acc_to")
            )
        except ValueError as e:
            sys.exit(f"{txn_file}: missing column in header {header}: {e}")
        cols_used = (i_id, i_from, i_to)
        for line in f:
            parts = line.strip().split(',')
            if len(parts) <= max(cols_used):
                continue
            txns.append((parts[i_id], parts[i_from], parts[i_to]))

    num_accounts = len(accounts)
    num_txns = len(txns)
    print(f"Accounts: {num_accounts}, Transactions: {num_txns}")

    # Source join: key=acc_from for txns, key=account_id for accounts.
    # account data: <gyear cat claims>; txn data: <txn_id,acc_to>.
    with open(out_src, 'w') as f:
        f.write(f"{num_accounts} {num_txns}\n\n")
        for aid, gy, cat, cl in accounts:
            f.write(f"{aid} {gy} {cat} {cl}\n")
        f.write("\n")
        for txn_id, acc_from, acc_to in txns:
            f.write(f"{acc_from} {txn_id},{acc_to}\n")

    # Destination join: key=acc_to for txns, key=account_id for accounts.
    # account data: <gyear cat claims>; txn data: <txn_id,acc_from>.
    with open(out_dst, 'w') as f:
        f.write(f"{num_accounts} {num_txns}\n\n")
        for aid, gy, cat, cl in accounts:
            f.write(f"{aid} {gy} {cat} {cl}\n")
        f.write("\n")
        for txn_id, acc_from, acc_to in txns:
            f.write(f"{acc_to} {txn_id},{acc_from}\n")

    print(f"Written: {out_src} (source join), {out_dst} (destination join)")

if __name__ == "__main__":
    main()

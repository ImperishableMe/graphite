#!/usr/bin/env python3
"""
Convert the SNAP cit-Patents citation network to NebulaDB format (W3).

W3 is the US patent citation network (SNAP cit-Patents edge list + NBER
pat63_99 metadata). It is a directed acyclic graph (citing -> cited), sparse
(avg out-degree ~4.3) with real integer node attributes, complementing the
dense financial (W1/W4) and social (W2) graphs.

Inputs:
  - SNAP edge list  : cit-Patents.txt(.gz)  -- tab-separated "FromNodeId\\tToNodeId",
                      '#'-prefixed comment lines skipped. From = citing patent,
                      To = cited patent. We follow citations forward (citing ->
                      cited), so acc_from = citing, acc_to = cited.
  - NBER metadata   : pat63_99.zip / pat63_99.tpt -- a SAS XPORT transport file
                      with columns PATENT, GYEAR, CAT, CLAIMS (read via pandas).

To stay drop-in compatible with the rest of the E1 pipeline (the chain-query
rewriter, the obliviator FK converter, and sgx_app's schema-from-header import),
we emit the SAME table/column naming W1/W4 use -- patents map to `account`,
citations map to `txn`:

  account.csv: account_id, gyear, cat, claims
  txn.csv:     txn_id, acc_from, acc_to

Conversions performed:
  - Only patents that appear in the SNAP edge list (as a citing or cited node)
    become accounts; isolated NBER patents are dropped. Patent numbers are
    remapped to contiguous int [1, N], assigned in ascending patent-number order
    so the mapping is deterministic regardless of edge order.
  - gyear / cat / claims are taken from NBER. Patents with no NBER metadata (or
    a missing field) get 0. claims is NaN for pre-1975 grants -> 0.
  - All values are validated to fit within [-1_073_741_820, 1_073_741_820].
    Patent numbers (<= ~6M after 1963) and the contiguous ids are well within
    int32, so no saturation is needed.

No sentinel row is appended (matching the W4 converter: the obligraph one-hop
import keys on the CSV header and does not consume a sentinel).

Operates in two streaming passes over the edge list (pass 1: collect the node
set; pass 2: emit txn.csv) -- bounded memory beyond the node-set/metadata maps.

Usage:
    python3 scripts/convert_patents_to_nebuladb.py \\
        <cit-Patents.txt[.gz]> <pat63_99.zip|.tpt> <output_dir>
"""

import argparse
import csv
import gzip
import sys
import zipfile
from collections import Counter
from pathlib import Path

# System bounds from enclave_types.h
JOIN_ATTR_MIN = -1_073_741_820
JOIN_ATTR_MAX = 1_073_741_820

PROGRESS_EVERY = 2_000_000


def open_edges(path: Path):
    """Open the SNAP edge list transparently (.gz or plain text)."""
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt")
    return open(path, "r")


def check_bounds(val: int, col_name: str) -> None:
    if not (JOIN_ATTR_MIN <= val <= JOIN_ATTR_MAX):
        raise ValueError(f"{col_name} value {val} outside int32 range "
                         f"[{JOIN_ATTR_MIN}, {JOIN_ATTR_MAX}]")


def load_nber_metadata(meta_path: Path) -> dict:
    """Read PATENT -> (gyear, cat, claims) from the NBER pat63_99 SAS XPORT file.
    Accepts either the .zip (single .tpt member) or an extracted .tpt path."""
    try:
        import pandas as pd
    except ImportError:
        sys.exit("ERROR: pandas is required to read the NBER SAS XPORT file.\n"
                 "  pip install --user --break-system-packages pandas")

    # read_sas needs a real filesystem path; extract the .tpt from the zip.
    if str(meta_path).endswith(".zip"):
        with zipfile.ZipFile(meta_path) as z:
            members = [m for m in z.namelist() if m.lower().endswith(".tpt")]
            if not members:
                sys.exit(f"{meta_path}: no .tpt member found ({z.namelist()})")
            tpt = z.extract(members[0], meta_path.parent)
    else:
        tpt = str(meta_path)

    print(f"Reading NBER metadata from {tpt} (SAS XPORT) ...")
    meta: dict[int, tuple[int, int, int]] = {}
    reader = pd.read_sas(tpt, format="xport", chunksize=500_000)
    for chunk in reader:
        for patent, gyear, cat, claims in zip(
                chunk["PATENT"], chunk["GYEAR"], chunk["CAT"], chunk["CLAIMS"]):
            if patent != patent:  # NaN patent number -> skip
                continue
            pid = int(patent)
            gy = 0 if gyear != gyear else int(gyear)
            ct = 0 if cat != cat else int(cat)
            cl = 0 if claims != claims else int(claims)
            meta[pid] = (gy, ct, cl)
        print(f"  ... {len(meta):,} patents with metadata", flush=True)
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("edge_list", type=Path,
                        help="SNAP cit-Patents edge list (.txt or .txt.gz)")
    parser.add_argument("metadata", type=Path,
                        help="NBER pat63_99 metadata (.zip or .tpt)")
    parser.add_argument("output_dir", type=Path,
                        help="Output directory (will be created)")
    args = parser.parse_args()

    if not args.edge_list.is_file():
        print(f"ERROR: edge list not found: {args.edge_list}", file=sys.stderr)
        return 1
    if not args.metadata.is_file():
        print(f"ERROR: metadata not found: {args.metadata}", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    account_csv_path = args.output_dir / "account.csv"
    txn_csv_path = args.output_dir / "txn.csv"

    print("=" * 70)
    print("SNAP cit-Patents -> NebulaDB W3 converter")
    print("=" * 70)
    print(f"Edges   : {args.edge_list}")
    print(f"Metadata: {args.metadata}")
    print(f"Output  : {args.output_dir}/")
    print()

    # ---- Pass 1: collect the node set (patents appearing in any edge). ----
    print("Pass 1: scanning edge list for the node set ...")
    nodes: set[int] = set()
    edge_count = 0
    with open_edges(args.edge_list) as f:
        for line in f:
            if not line or line[0] == "#":
                continue
            a, b = line.split()
            nodes.add(int(a))
            nodes.add(int(b))
            edge_count += 1
            if edge_count % PROGRESS_EVERY == 0:
                print(f"  ... {edge_count:,} edges, {len(nodes):,} nodes", flush=True)
    print(f"  edges: {edge_count:,}   distinct patents: {len(nodes):,}")

    # Deterministic contiguous remap in ascending patent-number order.
    sorted_patents = sorted(nodes)
    remap = {pid: i for i, pid in enumerate(sorted_patents, start=1)}

    # ---- Metadata ----
    meta = load_nber_metadata(args.metadata)
    with_meta = sum(1 for p in sorted_patents if p in meta)

    # ---- Write account.csv (one row per patent, in remapped-id order). ----
    print(f"\nWriting account.csv ({len(sorted_patents):,} accounts) ...")
    gyear_hist: Counter = Counter()
    with open(account_csv_path, "w", newline="") as facc:
        acc_writer = csv.writer(facc, lineterminator="\n")
        acc_writer.writerow(["account_id", "gyear", "cat", "claims"])
        for pid in sorted_patents:
            gy, ct, cl = meta.get(pid, (0, 0, 0))
            check_bounds(gy, "gyear")
            check_bounds(ct, "cat")
            check_bounds(cl, "claims")
            gyear_hist[gy] += 1
            acc_writer.writerow([remap[pid], gy, ct, cl])

    # ---- Pass 2: write txn.csv, remapping endpoints; track out-degree. ----
    print(f"Writing txn.csv ({edge_count:,} citations) ...")
    out_degree: Counter = Counter()
    with open(txn_csv_path, "w", newline="") as ftxn, \
         open_edges(args.edge_list) as f:
        txn_writer = csv.writer(ftxn, lineterminator="\n")
        txn_writer.writerow(["txn_id", "acc_from", "acc_to"])
        txn_id = 0
        for line in f:
            if not line or line[0] == "#":
                continue
            a, b = line.split()
            acc_from = remap[int(a)]  # citing patent
            acc_to = remap[int(b)]    # cited patent
            txn_id += 1
            txn_writer.writerow([txn_id, acc_from, acc_to])
            out_degree[acc_from] += 1
            if txn_id % PROGRESS_EVERY == 0:
                print(f"  ... {txn_id:,} citations written", flush=True)

    # ---- Stats ----
    print("\n" + "=" * 70)
    print("Conversion summary")
    print("=" * 70)
    print(f"  Patents (accounts): {len(sorted_patents):,}")
    print(f"  Citations (txns):   {edge_count:,}")
    print(f"  Patents with NBER metadata: {with_meta:,} "
          f"({100.0 * with_meta / max(len(sorted_patents), 1):.1f}%)")
    print(f"  Avg out-degree: {edge_count / max(len(sorted_patents), 1):.2f}")
    print(f"  Max out-degree: {max(out_degree.values()) if out_degree else 0}")
    print(f"  Patent-number range: [{sorted_patents[0]}, {sorted_patents[-1]}]")

    print("\n  Grant-year distribution (top 10, for snap_sel_* gyear thresholds):")
    for gy, cnt in sorted(gyear_hist.most_common(10)):
        if gy == 0:
            continue
        print(f"    gyear {gy}: {cnt:,} patents")

    # Suggested start accounts: high out-degree citing patents make the chain
    # non-trivial at every hop. The anchor is filter-independent for NebulaDB
    # latency (the hop table is sorted in full regardless), so this only needs
    # to keep the chain output bounded but non-empty across all 5 hops.
    print("\n  Suggested a1.account_id start anchors (highest out-degree):")
    print(f"    {'rank':>4}  {'account_id':>11}  {'out_deg':>8}")
    for rank, (acc, deg) in enumerate(out_degree.most_common(10), start=1):
        print(f"    {rank:>4}  {acc:>11}  {deg:>8}")

    # A mid-range out-degree node is a safer default than the absolute max
    # (which may sit at the very top of a citation hub and explode at 5-hop).
    by_deg = out_degree.most_common()
    if by_deg:
        # pick a node around the 95th percentile of out-degree
        idx = max(0, int(len(by_deg) * 0.05))
        suggested, sdeg = by_deg[idx]
        print(f"\n  Default suggested start: account_id {suggested} "
              f"(out-degree {sdeg}, ~95th pct) -- bake into snap_*hop.sql.")

    print(f"\nDone. Outputs:")
    print(f"  {account_csv_path}")
    print(f"  {txn_csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

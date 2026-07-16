#!/usr/bin/env python3
"""
Query rewriter for chain/branch queries using filter-independent hop decomposition.

DECOMPOSITION PRINCIPLE:
- Every transaction edge becomes a one-hop join
- Consecutive hops share overlapping account nodes (dest of left = src of right)
- All hops are aliases over a single pre-computed hop table
- Filters are distributed to hops that contain the filtered account

Example for chain4 (a1 --t1-- a2 --t2-- a3 --t3-- a4):
  Decomposition (always 3 hops regardless of filters):
    h1: a1 -> t1 -> a2
    h2: a2 -> t2 -> a3  (overlaps: h1.dest = h2.src at a2)
    h3: a3 -> t3 -> a4  (overlaps: h2.dest = h3.src at a3)

  Rewritten query:
    SELECT * FROM hop AS h1, hop AS h2, hop AS h3
    WHERE h1.account_dest_account_id = h2.account_src_account_id
      AND h2.account_dest_account_id = h3.account_src_account_id
      AND <filters mapped to appropriate hops>
"""

import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import List, Dict, Tuple, Set, Optional


@dataclass
class AccountInfo:
    """Information about an account alias in the query."""
    alias: str


@dataclass
class TxnInfo:
    """Information about a transaction edge."""
    alias: str
    src_account: str  # acc_from
    dest_account: str  # acc_to


@dataclass
class FilterInfo:
    """Filter condition on an account."""
    account_alias: str
    column: str
    operator: str
    value: str


@dataclass
class TxnFilterInfo:
    """Filter condition on a transaction edge."""
    txn_alias: str
    column: str
    operator: str
    value: str


@dataclass
class QueryElement:
    """An element in the decomposed query (always a hop)."""
    alias: str          # h1, h2, h3, ...
    src_account: str    # Original account alias (e.g., "a1")
    txn: str            # Original txn alias (e.g., "t1")
    dest_account: str   # Original account alias (e.g., "a2")


def _extract_number(s: str) -> int:
    """Extract numeric suffix from alias (e.g., 't1' -> 1, 'a2' -> 2)."""
    match = re.search(r'\d+', s)
    return int(match.group()) if match else 0


def parse_banking_query(sql: str) -> Tuple[List[Tuple[str, str]], List[str], List[str]]:
    """
    Parse a banking query to extract tables, join conditions, and filters.

    Returns:
        (tables, join_conditions, filter_conditions)
    """
    sql = ' '.join(sql.split())

    # Extract FROM clause tables
    from_match = re.search(r'FROM\s+(.+?)\s+WHERE', sql, re.IGNORECASE)
    if not from_match:
        raise ValueError("Could not parse FROM clause")

    from_clause = from_match.group(1)
    table_pattern = re.compile(r'(\w+)\s+AS\s+(\w+)', re.IGNORECASE)
    tables = table_pattern.findall(from_clause)

    # Extract WHERE clause
    where_match = re.search(r'WHERE\s+(.+?)(?:;|$)', sql, re.IGNORECASE)
    if not where_match:
        raise ValueError("Could not parse WHERE clause")

    where_clause = where_match.group(1)
    conditions = [c.strip() for c in re.split(r'\s+AND\s+', where_clause, flags=re.IGNORECASE)]

    join_conditions = []
    filter_conditions = []

    for cond in conditions:
        dot_count = cond.count('.')
        if dot_count >= 2:
            join_conditions.append(cond)
        else:
            filter_conditions.append(cond)

    return tables, join_conditions, filter_conditions


def build_graph(tables: List[Tuple[str, str]], join_conditions: List[str]) -> Tuple[Dict[str, TxnInfo], Dict[str, AccountInfo]]:
    """
    Build the join graph from tables and join conditions.

    Returns:
        (txn_info_map, account_info_map)
    """
    accounts: Dict[str, AccountInfo] = {}
    txns: Dict[str, TxnInfo] = {}

    # Initialize accounts
    for table_name, alias in tables:
        if table_name.lower() == 'account':
            accounts[alias] = AccountInfo(alias=alias)

    # Build txn edges
    for cond in join_conditions:
        # Match: aX.account_id = tY.acc_from
        from_match = re.match(r'(\w+)\.account_id\s*=\s*(\w+)\.acc_from', cond)
        if from_match:
            account, txn = from_match.group(1), from_match.group(2)
            if txn not in txns:
                txns[txn] = TxnInfo(alias=txn, src_account='', dest_account='')
            txns[txn].src_account = account
            continue

        # Match: aX.account_id = tY.acc_to
        to_match = re.match(r'(\w+)\.account_id\s*=\s*(\w+)\.acc_to', cond)
        if to_match:
            account, txn = to_match.group(1), to_match.group(2)
            if txn not in txns:
                txns[txn] = TxnInfo(alias=txn, src_account='', dest_account='')
            txns[txn].dest_account = account

    return txns, accounts


def parse_filters(
    filter_conditions: List[str],
    accounts: Dict[str, AccountInfo],
    txns: Dict[str, TxnInfo]
) -> Tuple[Dict[str, FilterInfo], Dict[str, List[TxnFilterInfo]]]:
    """
    Parse filter conditions into FilterInfo / TxnFilterInfo objects.

    Returns:
        (account alias -> FilterInfo, txn alias -> list of TxnFilterInfo)
    """
    filters: Dict[str, FilterInfo] = {}
    txn_filters: Dict[str, List[TxnFilterInfo]] = defaultdict(list)

    for cond in filter_conditions:
        # Match: alias.column op value (e.g., a1.owner_id = 52, t1.amount > 10000)
        match = re.match(r'(\w+)\.(\w+)\s*(=|<|>|<=|>=|<>|!=)\s*(.+)', cond)
        if not match:
            raise ValueError(f"Could not parse filter condition: {cond}")

        alias, column, operator, value = match.groups()
        if alias in accounts:
            filters[alias] = FilterInfo(
                account_alias=alias,
                column=column,
                operator=operator,
                value=value.strip()
            )
        elif alias in txns:
            txn_filters[alias].append(TxnFilterInfo(
                txn_alias=alias,
                column=column,
                operator=operator,
                value=value.strip()
            ))
        else:
            raise ValueError(
                f"Filter references unknown alias '{alias}' "
                f"(not an account or txn alias): {cond}"
            )

    return filters, dict(txn_filters)


def build_filter_independent_decomposition(
    txns: Dict[str, TxnInfo],
    accounts: Dict[str, AccountInfo]
) -> List[QueryElement]:
    """
    Build filter-independent decomposition: one hop per transaction edge.

    Algorithm:
    1. Build graph of account->txn connections
    2. Find starting accounts (only appear as source, not dest)
    3. BFS traversal from start, creating one hop per edge
    4. For branches: shared nodes naturally connect multiple hops

    Returns:
        List of QueryElement representing hops in traversal order
    """
    elements: List[QueryElement] = []
    hop_counter = 1
    visited_txns: Set[str] = set()

    # Build adjacency: account -> list of (txn, dest_account)
    outgoing: Dict[str, List[Tuple[str, str]]] = defaultdict(list)

    for txn_alias, txn_info in txns.items():
        if txn_info.src_account and txn_info.dest_account:
            outgoing[txn_info.src_account].append((txn_alias, txn_info.dest_account))

    # Find starting accounts (have outgoing but no incoming)
    dest_accounts = {t.dest_account for t in txns.values() if t.dest_account}
    src_accounts = {t.src_account for t in txns.values() if t.src_account}
    starting_accounts = src_accounts - dest_accounts

    # If no clear starting point (cycle or isolated), start from first txn's source
    if not starting_accounts:
        first_txn = min(txns.keys(), key=_extract_number)
        starting_accounts = {txns[first_txn].src_account}

    def traverse_from(start_account: str):
        """BFS traversal from a starting account, creating hops."""
        nonlocal hop_counter
        queue = [start_account]

        while queue:
            current = queue.pop(0)

            # Sort outgoing edges by txn number for deterministic ordering
            edges = sorted(outgoing[current], key=lambda x: _extract_number(x[0]))

            for txn_alias, dest_account in edges:
                if txn_alias in visited_txns:
                    continue

                visited_txns.add(txn_alias)

                # Create hop element
                hop = QueryElement(
                    alias=f"h{hop_counter}",
                    src_account=current,
                    txn=txn_alias,
                    dest_account=dest_account
                )
                elements.append(hop)
                hop_counter += 1

                # Continue traversal from dest
                queue.append(dest_account)

    # Traverse from each starting account (handles disconnected components)
    for start in sorted(starting_accounts, key=_extract_number):
        traverse_from(start)

    return elements


def distribute_filters_to_hops(
    elements: List[QueryElement],
    filters: Dict[str, FilterInfo]
) -> Dict[str, List[Tuple[str, FilterInfo]]]:
    """
    Distribute filter conditions to hops that contain the filtered account.

    A filter on account 'aX' goes to the first hop where aX appears
    as either src or dest.

    Returns:
        Mapping from hop_alias to list of (position, filter) tuples
        where position is "src" or "dest"
    """
    hop_filters: Dict[str, List[Tuple[str, FilterInfo]]] = defaultdict(list)
    assigned_filters: Set[str] = set()

    for elem in elements:
        # Check if src_account has an unassigned filter
        if elem.src_account in filters and elem.src_account not in assigned_filters:
            hop_filters[elem.alias].append(("src", filters[elem.src_account]))
            assigned_filters.add(elem.src_account)

        # Check if dest_account has an unassigned filter
        if elem.dest_account in filters and elem.dest_account not in assigned_filters:
            hop_filters[elem.alias].append(("dest", filters[elem.dest_account]))
            assigned_filters.add(elem.dest_account)

    return hop_filters


def generate_join_conditions(elements: List[QueryElement]) -> List[str]:
    """
    Generate join conditions between hops.

    For overlapping hops, join on shared account:
      - h_prev.account_dest_account_id = h_next.account_src_account_id

    For branch points (same account in multiple hops):
      - Chain the references appropriately
    """
    conditions = []
    generated = set()

    # Build mapping: account -> list of (hop_alias, position)
    account_references: Dict[str, List[Tuple[str, str]]] = defaultdict(list)

    for elem in elements:
        account_references[elem.src_account].append((elem.alias, "src"))
        account_references[elem.dest_account].append((elem.alias, "dest"))

    # Generate equi-join for each shared account
    for account, refs in account_references.items():
        if len(refs) < 2:
            continue

        # Sort refs by hop number for deterministic output
        refs = sorted(refs, key=lambda x: _extract_number(x[0]))

        # Chain the references
        for i in range(len(refs) - 1):
            hop1, pos1 = refs[i]
            hop2, pos2 = refs[i + 1]

            col1 = f"{hop1}.account_{pos1}_account_id"
            col2 = f"{hop2}.account_{pos2}_account_id"

            # Avoid duplicates
            join_key = tuple(sorted([col1, col2]))
            if join_key not in generated:
                conditions.append(f"{col1} = {col2}")
                generated.add(join_key)

    return conditions


def generate_filter_conditions(
    elements: List[QueryElement],
    filters: Dict[str, FilterInfo]
) -> List[str]:
    """Generate filter conditions mapped to hop column names."""
    conditions = []
    hop_filters = distribute_filters_to_hops(elements, filters)

    for hop_alias, filter_list in sorted(hop_filters.items(), key=lambda x: _extract_number(x[0])):
        for position, finfo in filter_list:
            # Map account column to hop column
            # e.g., a1.owner_id -> h1.account_src_owner_id
            col = f"{hop_alias}.account_{position}_{finfo.column}"
            conditions.append(f"{col} {finfo.operator} {finfo.value}")

    return conditions


def generate_txn_filter_conditions(
    elements: List[QueryElement],
    txn_filters: Dict[str, List[TxnFilterInfo]]
) -> List[str]:
    """
    Generate filter conditions on transaction edges, mapped to hop column names.

    Each txn alias corresponds to exactly one hop (the hop built from that
    edge). Edge payload columns appear UNPREFIXED in the hop table (e.g.
    txn column `amount` is hop column `amount`), so only the alias changes:
      t1.amount > 10000  ->  h1.amount > 10000
    """
    conditions = []
    txn_to_hop = {elem.txn: elem.alias for elem in elements}

    for txn_alias in sorted(txn_filters.keys(), key=_extract_number):
        if txn_alias not in txn_to_hop:
            raise ValueError(
                f"Filter on txn alias '{txn_alias}' but no hop was built "
                f"from that edge (txn not in any join condition?)"
            )
        hop_alias = txn_to_hop[txn_alias]
        for finfo in txn_filters[txn_alias]:
            conditions.append(f"{hop_alias}.{finfo.column} {finfo.operator} {finfo.value}")

    return conditions


def generate_optimized_query(
    elements: List[QueryElement],
    filters: Dict[str, FilterInfo],
    txn_filters: Dict[str, List[TxnFilterInfo]]
) -> str:
    """Generate the optimized SQL query."""

    # FROM clause: all elements are hops
    from_parts = [f"hop AS {elem.alias}" for elem in elements]
    from_clause = "SELECT * FROM " + ", ".join(from_parts)

    # WHERE clause
    join_conditions = generate_join_conditions(elements)
    filter_conditions = generate_filter_conditions(elements, filters)
    txn_filter_conditions = generate_txn_filter_conditions(elements, txn_filters)

    all_conditions = join_conditions + filter_conditions + txn_filter_conditions

    if all_conditions:
        where_clause = "WHERE " + "\n  AND ".join(all_conditions)
        return f"{from_clause}\n{where_clause};"
    else:
        return f"{from_clause};"


def rewrite_query(input_sql: str) -> str:
    """Main function to rewrite a banking query with filter-independent decomposition."""
    # Step 1: Parse the query
    tables, join_conditions, filter_conditions = parse_banking_query(input_sql)

    # Step 2: Build the graph
    txns, accounts = build_graph(tables, join_conditions)

    # Step 3: Parse filters
    filters, txn_filters = parse_filters(filter_conditions, accounts, txns)

    # Step 4: Build filter-independent decomposition
    elements = build_filter_independent_decomposition(txns, accounts)

    # Debug output
    print(f"Decomposition ({len(elements)} hops):", file=sys.stderr)
    for elem in elements:
        print(f"  {elem.alias}: {elem.src_account} -> {elem.txn} -> {elem.dest_account}", file=sys.stderr)

    if filters:
        print(f"Account filters: {list(filters.keys())}", file=sys.stderr)
    if txn_filters:
        print(f"Txn filters: {list(txn_filters.keys())}", file=sys.stderr)

    # Step 5: Generate the optimized query
    return generate_optimized_query(elements, filters, txn_filters)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <input.sql> [output.sql]")
        print("  If output.sql is not specified, prints to stdout")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None

    with open(input_path, 'r') as f:
        input_sql = f.read()

    output_sql = rewrite_query(input_sql)

    if output_path:
        with open(output_path, 'w') as f:
            f.write(output_sql)
        print(f"Wrote optimized query to {output_path}", file=sys.stderr)
    else:
        print(output_sql)


if __name__ == "__main__":
    main()

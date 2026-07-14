#!/usr/bin/env python3
"""
Convert the LDBC SNB Interactive v1 social network to NebulaDB format (W2).

W2 is the LDBC Social Network Benchmark person-knows-person graph (Interactive
v1, Datagen CsvBasic & LongDateFormatter layout). It is a dense social graph
(avg degree ~40 undirected at SF30) with a power-law-ish degree distribution,
complementing the financial (W1/W4) and citation (W3) graphs.

Inputs (from the extracted social_network/ directory, '|'-separated):
  - dynamic/person_0_0.csv                    id|firstName|lastName|gender|
                                              birthday|creationDate|locationIP|
                                              browserUsed
  - dynamic/person_knows_person_0_0.csv       Person.id|Person.id|creationDate
  - dynamic/person_isLocatedIn_place_0_0.csv  Person.id|Place.id
With LongDateFormatter, birthday and creationDate are epoch milliseconds; a
date-string fallback ("1984-05-02", "2010-03-12T05:20:33.123+0000") is kept in
case a differently-formatted dump is fed in.

To stay drop-in compatible with the rest of the E1 pipeline (the chain-query
rewriter, the obliviator FK converter, and sgx_app's schema-from-header
import), we emit the SAME table/column naming W1/W3/W4 use -- persons map to
`account`, knows edges map to `txn`:

  account.csv: account_id, city_id, gender, birthday
  txn.csv:     txn_id, acc_from, acc_to, knows_day

Column-count note: the one-hop combined row is account + txn + account =
4 + 4 + 4 = 12 int32 columns = 48 bytes, exactly ROW_DATA_MAX_SIZE. Do not add
columns to either table without shrinking the other.

Conversions performed:
  - Person ids (sparse 64-bit) are remapped to contiguous int [1, N], assigned
    in ascending person-id order so the mapping is deterministic.
  - `knows` is an undirected relationship. Edges are canonicalized to unordered
    pairs, deduplicated, and then BOTH directions are emitted so the directed
    chain queries (acc_from -> acc_to) traverse friendships both ways. txn.csv
    therefore has 2x the unique-friendship count. knows_day (the friendship
    creationDate, epoch days) is shared by the two mirrored rows.
  - gender is mapped to an integer code in first-seen order (mapping printed).
  - birthday / knows creationDate are converted to epoch days (Unix ms // 86400000).
  - city_id is the LDBC Place id from person_isLocatedIn_place (already a small
    integer; used verbatim). Persons with no city row get 0.
  - All values are validated to fit within [-1_073_741_820, 1_073_741_820].

No sentinel row is appended (matching the W3/W4 converters: the obligraph
one-hop import keys on the CSV header and does not consume a sentinel).

Everything is held in memory (persons + a packed-int edge set); at SF30 that
is ~180k persons and ~7M unique friendships, well within a few GB.

Usage:
    python3 scripts/convert_ldbc_snb_to_nebuladb.py \\
        <person_0_0.csv> <person_knows_person_0_0.csv> \\
        <person_isLocatedIn_place_0_0.csv> <output_dir>
"""

import argparse
import csv
import sys
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path

# System bounds from enclave_types.h
JOIN_ATTR_MIN = -1_073_741_820
JOIN_ATTR_MAX = 1_073_741_820

PROGRESS_EVERY = 2_000_000

MS_PER_DAY = 86_400_000


def check_bounds(val: int, col_name: str) -> None:
    if not (JOIN_ATTR_MIN <= val <= JOIN_ATTR_MAX):
        raise ValueError(f"{col_name} value {val} outside int32 range "
                         f"[{JOIN_ATTR_MIN}, {JOIN_ATTR_MAX}]")


def parse_epoch_days(raw: str, col_name: str) -> int:
    """LongDateFormatter epoch-millis -> epoch days, with a date-string
    fallback for other datagen formatter settings."""
    try:
        return int(raw) // MS_PER_DAY
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp()) // 86_400
        except ValueError:
            continue
    raise ValueError(f"cannot parse {col_name} value {raw!r} as epoch millis "
                     f"or date")


def open_pipe_csv(path: Path, expected_first_cols: list):
    """Open a '|'-separated SNB CSV, validate the header prefix, and return
    (file_handle, reader). Caller is responsible for closing the handle."""
    f = open(path, "r", newline="")
    reader = csv.reader(f, delimiter="|")
    header = next(reader)
    got = [h.strip() for h in header[:len(expected_first_cols)]]
    if got != expected_first_cols:
        f.close()
        raise ValueError(f"{path}: unexpected header {header} "
                         f"(expected it to start with {expected_first_cols})")
    return f, reader, header


def load_persons(person_path: Path):
    """Read person_0_0.csv -> OrderedDict person_id -> (gender_code, birthday_days),
    plus the gender-code mapping used."""
    gender_codes = OrderedDict()
    persons = {}
    f, reader, header = open_pipe_csv(
        person_path,
        ["id", "firstName", "lastName", "gender", "birthday", "creationDate"])
    i_gender = header.index("gender")
    i_birthday = header.index("birthday")
    with f:
        for row in reader:
            if not row:
                continue
            pid = int(row[0])
            gender = row[i_gender]
            if gender not in gender_codes:
                gender_codes[gender] = len(gender_codes)
            birthday_days = parse_epoch_days(row[i_birthday], "birthday")
            check_bounds(birthday_days, "birthday")
            persons[pid] = (gender_codes[gender], birthday_days)
    return persons, gender_codes


def load_cities(located_path: Path, persons: dict):
    """Read person_isLocatedIn_place_0_0.csv -> dict person_id -> place_id."""
    cities = {}
    f, reader, _ = open_pipe_csv(located_path, ["Person.id", "Place.id"])
    with f:
        for row in reader:
            if not row:
                continue
            pid, place = int(row[0]), int(row[1])
            if pid not in persons:
                raise ValueError(f"{located_path}: person {pid} not in person table")
            check_bounds(place, "city_id")
            cities[pid] = place
    return cities


def load_knows(knows_path: Path, id_map: dict):
    """Read person_knows_person_0_0.csv, canonicalize to unordered pairs of
    remapped ids, dedupe. Returns (sorted unique pairs, per-pair knows_day,
    raw row count). Pairs are packed as u*(N+1)+v (u < v) to keep the set
    lean at millions of edges."""
    n = len(id_map)
    packed_days = {}
    raw_rows = 0
    f, reader, _ = open_pipe_csv(knows_path, ["Person.id", "Person.id"])
    with f:
        for row in reader:
            if not row:
                continue
            raw_rows += 1
            p1, p2 = int(row[0]), int(row[1])
            if p1 == p2:
                raise ValueError(f"{knows_path}: self-loop at person {p1}")
            try:
                a, b = id_map[p1], id_map[p2]
            except KeyError as e:
                raise ValueError(f"{knows_path}: person {e} not in person table")
            if a > b:
                a, b = b, a
            key = a * (n + 1) + b
            if key not in packed_days:
                day = parse_epoch_days(row[2], "knows.creationDate") if len(row) > 2 else 0
                check_bounds(day, "knows_day")
                packed_days[key] = day
            if raw_rows % PROGRESS_EVERY == 0:
                print(f"  ... {raw_rows:,} knows rows read", flush=True)
    pairs = sorted(packed_days)
    return pairs, packed_days, raw_rows, n


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("person_csv", type=Path)
    ap.add_argument("knows_csv", type=Path)
    ap.add_argument("located_csv", type=Path)
    ap.add_argument("output_dir", type=Path)
    args = ap.parse_args()

    for p in (args.person_csv, args.knows_csv, args.located_csv):
        if not p.is_file():
            sys.exit(f"input file not found: {p}")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Reading persons from {args.person_csv} ...", flush=True)
    persons, gender_codes = load_persons(args.person_csv)
    print(f"  {len(persons):,} persons; gender codes: {dict(gender_codes)}")

    print(f"Reading cities from {args.located_csv} ...", flush=True)
    cities = load_cities(args.located_csv, persons)
    print(f"  {len(cities):,} person->city rows; "
          f"{len(persons) - len(cities):,} persons without a city (city_id=0)")

    # Contiguous ids [1, N] in ascending person-id order (deterministic).
    sorted_pids = sorted(persons)
    id_map = {pid: i + 1 for i, pid in enumerate(sorted_pids)}
    check_bounds(len(id_map), "account_id")

    print(f"Reading knows edges from {args.knows_csv} ...", flush=True)
    pairs, packed_days, raw_rows, n = load_knows(args.knows_csv, id_map)
    print(f"  {raw_rows:,} rows -> {len(pairs):,} unique friendships "
          f"({raw_rows - len(pairs):,} duplicate/reciprocal rows dropped); "
          f"emitting {2 * len(pairs):,} directed edges")

    account_path = args.output_dir / "account.csv"
    print(f"Writing {account_path} ...", flush=True)
    with open(account_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["account_id", "city_id", "gender", "birthday"])
        for pid in sorted_pids:
            gender_code, birthday_days = persons[pid]
            w.writerow([id_map[pid], cities.get(pid, 0), gender_code,
                        birthday_days])

    txn_path = args.output_dir / "txn.csv"
    print(f"Writing {txn_path} ...", flush=True)
    degree = Counter()
    with open(txn_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["txn_id", "acc_from", "acc_to", "knows_day"])
        txn_id = 0
        for key in pairs:
            u, v = divmod(key, n + 1)
            day = packed_days[key]
            txn_id += 1
            w.writerow([txn_id, u, v, day])
            txn_id += 1
            w.writerow([txn_id, v, u, day])
            degree[u] += 1
            degree[v] += 1
        check_bounds(txn_id, "txn_id")

    print(f"\nDone: {len(id_map):,} accounts, {2 * len(pairs):,} txns")
    avg_deg = 2 * len(pairs) / len(id_map) if id_map else 0
    print(f"Avg undirected degree: {avg_deg:.1f}")
    print("Top-10 degree persons (account_id: degree):")
    for aid, d in degree.most_common(10):
        print(f"  {aid}: {d}")
    city_counts = Counter(cities.values())
    print("Top-5 cities (city_id: persons):")
    for cid, c in city_counts.most_common(5):
        print(f"  {cid}: {c}")
    print("Bottom-5 cities (city_id: persons):")
    for cid, c in city_counts.most_common()[-5:]:
        print(f"  {cid}: {c}")


if __name__ == "__main__":
    main()

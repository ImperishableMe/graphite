#include <set>
#include <unordered_map>
#include <algorithm>
#include <iostream>
#include <future>
#include <thread>
#include <cassert>
#include <bit>
#include <cstring>
#include <chrono>
#include <random>

#include "xxhash.h"
#include "node_index.h"
#include "obl_primitives.h"
#include "obl_building_blocks.h"
#include "obl_row_ops.h"
#include "slice_utils.h"
#include "timer.h"
#include "config.h"

// ObliviousBin headers
#include "ohash_bin.hpp"
#include "hash_planner.hpp"
#include "ohash_tiers.hpp"

using namespace std;

namespace obligraph {
    std::unique_ptr<NodeIndex> buildNodeIndex(const Table& table, size_t op_num) {
        TimedScope ts("buildNodeIndex", "OFFLINE");

        std::cout << "[INFO] Row size: " << sizeof(Row)
                  << " bytes, RowBlock size: " << ROW_BLOCK_SIZE
                  << " bytes" << std::endl;

        size_t n = std::bit_ceil(static_cast<size_t>(table.rowCount));
        std::vector<RowBlock> blocks(n);

        for (size_t i = 0; i < table.rowCount; i++) {
            blocks[i].id = triple32(table.rows[i].key.first) & ~DUMMY_KEY_MSB;
            std::memcpy(blocks[i].value, &table.rows[i], sizeof(Row));
        }
        for (size_t i = table.rowCount; i < n; i++) {
            blocks[i].id = static_cast<key_t>(i) | DUMMY_KEY_MSB;
        }

        auto index = std::make_unique<NodeIndex>(n, op_num);
        index->build(blocks.data());
        return index;
    }

    // Resize `side` to n rows and copy each edge row's key in parallel.
    // Replaces the serial loop `for (i=0..n) { addRow(Row()); rows[i].key = edge[i].key; }`,
    // which on banking_10M ran 50M single-threaded push_backs before dedup could start.
    // rows.resize(n) is a single bulk zero-init (~memset); the key copy is O(n/p).
    // Row payload is overwritten later by probe, so zero-init of data[48] is wasted work
    // — tolerated here; elimination would require a custom allocator.
    static void initProbeSide(Table& side, const Table& edge, ThreadPool& pool) {
        const size_t n = edge.rowCount;
        side.rows.resize(n);
        side.rowCount = n;

        auto chunk = [&](int start, int end) {
            for (int i = start; i < end; i++)
                side.rows[i].key = edge.rows[i].key;
        };

        int num_threads = obligraph::number_of_threads.load();
        std::vector<std::future<void>> futures;
        for (int t = 0; t < num_threads; t++) {
            auto c = obligraph::get_cutoffs_for_thread(t, static_cast<int>(n), num_threads);
            if (c.first == c.second) continue;
            if (t == num_threads - 1) chunk(c.first, c.second);
            else futures.push_back(pool.submit(chunk, c.first, c.second));
        }
        for (auto& f : futures) f.get();
    }

    void probe_with_index(NodeIndex& obin, Table& probeT, ThreadPool& pool,
                          const std::string& label) {
        TimedScope ts("probe " + label, "ONLINE", /*contributes_to_total=*/false);

        key_t d = 2 * probeT.rowCount;
        int num_threads = std::max(1, obligraph::number_of_threads.load() / 2);

        auto thread_chunk = [&](int thread_id, int start, int end) {
            using TwoTier = ORAM::OTwoTierHash<key_t, ROW_BLOCK_SIZE>;
            TwoTier::init_dummy_range(key_t(0) - key_t(1) - key_t(thread_id) * d);

            Row dummyRow;
            dummyRow.setDummy(true);

            for (int i = start; i < end; i++) {
                key_t srcId = triple32(probeT.rows[i].key.first) & ~DUMMY_KEY_MSB;
                bool dummy = probeT.rows[i].isDummy();

                RowBlock result = obin[srcId];

                Row matchedRow;
                std::memcpy(&matchedRow, result.value, sizeof(Row));

                // obl_row_select: dst = cond ? t_val : f_val.
                // cond true  -> dummyRow (this is a dummy probe or ORAM entry was empty)
                // cond false -> matchedRow
                // Single AVX-512 blend + store; avoids the ObliviousChoose return-by-value temporary.
                obl_row_select(probeT.rows[i],
                               /*t_val=*/dummyRow,
                               /*f_val=*/matchedRow,
                               /*cond =*/dummy || result.dummy());
            }
        };

        std::vector<std::future<void>> futures;
        for (int i = 0; i < num_threads; i++) {
            auto chunks = obligraph::get_cutoffs_for_thread(i, probeT.rowCount, num_threads);
            if (chunks.first == chunks.second) continue;
            if (i == num_threads - 1)
                thread_chunk(i, chunks.first, chunks.second);
            else
                futures.push_back(pool.submit(thread_chunk, i, chunks.first, chunks.second));
        }
        for (auto& fut : futures) fut.get();
    }

    // Legacy overload (no label) — used by build_and_probe.
    void probe_with_index(NodeIndex& obin, Table& probeT, ThreadPool& pool) {
        probe_with_index(obin, probeT, pool, "");
    }

    void build_and_probe(const Table& buildT, Table &probeT, ThreadPool& pool) {
        TimedScope ts("build_and_probe", "ONLINE", /*contributes_to_total=*/false);
        auto index = buildNodeIndex(buildT, probeT.rowCount);
        probe_with_index(*index, probeT, pool, "");
    }

    void deduplicateRows(Table& table) {
        key_t lastKey = -1;
        key_t dummy = 1e9;

        for (size_t i = 0; i < table.rows.size(); ++i) {
            key_t currentKey = table.rows[i].key.first;
            // Generate dummy key without MSB set (MSB reserved for ObliviousBin dummy marking).
            // Offset by 2^31 to ensure lower 32 bits are in [2^31, 2^32-1], which is safely
            // above any valid account ID (small positive integers), preventing accidental
            // ORAM entry consumption for real account IDs.
            dummy = (static_cast<key_t>(random()) + (key_t(1) << 31)) & ~DUMMY_KEY_MSB;
            table.rows[i].key.first = ObliviousChoose(lastKey == currentKey, dummy, currentKey);
            table.rows[i].setDummy(lastKey == currentKey);
            lastKey = currentKey;
        }
    }

    void reduplicateRows(Table& table) {
        Row lastRow;
        for (size_t i = 0; i < table.rows.size(); ++i) {
            auto secKey = table.rows[i].key.second;
            table.rows[i] = ObliviousChoose(table.rows[i].isDummy(), lastRow, table.rows[i]);
            table.rows[i].key.second = secKey;
            lastRow = table.rows[i];
        }
    }

    // Parallel dedup: 1 pre-pass (O(P), serial, boundary reads) + 1 parallel scan.
    // The pre-pass captures each slice's entering lastKey BEFORE any writes, so each
    // worker can run the original loop independently on its slice with the correct seed.
    void deduplicateRowsParallel(Table& table, ThreadPool& pool) {
        const size_t N = table.rows.size();
        if (N == 0) return;
        const size_t P = std::min<size_t>(pool.size(), N);
        const auto slices = buildSlices(N, P);
        if (slices.empty()) return;

        // Pre-pass: seed[t] = key entering slice t (pre-dedup). Must run before any writes.
        std::vector<key_t> seed(slices.size());
        seed[0] = static_cast<key_t>(-1);  // sentinel: idx 0 is always real
        for (size_t t = 1; t < slices.size(); ++t) {
            seed[t] = table.rows[slices[t-1].end - 1].key.first;
        }

        // Phase 3: original loop, per slice, with correct seed.
        std::vector<std::future<void>> fs;
        fs.reserve(slices.size());
        for (size_t t = 0; t < slices.size(); ++t) {
            fs.push_back(pool.submit([&, t] {
                // random() is not thread-safe; use a thread-local PRNG instead.
                // Mask rng() to 31 bits so dummy lands in [2^31, 2^32-1]; serial
                // random() returns a 31-bit positive long, giving that same range.
                // This matters because triple32() hashes the low 32 bits of key.first:
                // if dummy's low-32-bits collide with a real account id (small positive
                // int), the dummy probe consumes the real ORAM entry and the later real
                // probe returns a dummy, corrupting results (off-by-one neighbor rows).
                thread_local std::mt19937_64 rng{std::random_device{}()};
                key_t lastKey = seed[t];
                for (size_t i = slices[t].begin; i < slices[t].end; ++i) {
                    key_t currentKey = table.rows[i].key.first;
                    key_t dummy = ((static_cast<key_t>(rng()) & 0x7FFFFFFFULL)
                                   + (key_t(1) << 31));
                    table.rows[i].key.first =
                        ObliviousChoose(lastKey == currentKey, dummy, currentKey);
                    table.rows[i].setDummy(lastKey == currentKey);
                    lastKey = currentKey;
                }
            }));
        }
        for (auto& f : fs) f.get();
    }

    // Parallel redup: canonical 3-phase carry-forward scan.
    // Phase 1 (parallel): each thread records its slice's tail real row.
    // Phase 2 (serial,   O(P)): compute seed[t] = real row in effect entering slice t.
    // Phase 3 (parallel): original redup loop per slice, seeded.
    void reduplicateRowsParallel(Table& table, ThreadPool& pool) {
        const size_t N = table.rows.size();
        if (N == 0) return;
        const size_t P = std::min<size_t>(pool.size(), N);
        const auto slices = buildSlices(N, P);
        if (slices.empty()) return;

        // Phase 1: find "tail real row" per slice.
        std::vector<Row>     tail(slices.size());
        std::vector<uint8_t> tailReal(slices.size(), 0);

        std::vector<std::future<void>> fs;
        fs.reserve(slices.size());
        for (size_t t = 0; t < slices.size(); ++t) {
            fs.push_back(pool.submit([&, t] {
                Row cur;
                uint8_t have = 0;
                for (size_t i = slices[t].begin; i < slices[t].end; ++i) {
                    bool real = !table.rows[i].isDummy();
                    cur  = ObliviousChoose(real, table.rows[i], cur);
                    have = ObliviousChoose(real, uint8_t{1}, have);
                }
                tail[t]     = cur;
                tailReal[t] = have;
            }));
        }
        for (auto& f : fs) f.get();
        fs.clear();

        // Phase 2: serial walk, seed[t] = running, then fold tail[t] into running.
        std::vector<Row> seed(slices.size());
        Row running;  // default-constructed; matches serial reduplicateRows's initial state
        for (size_t t = 0; t < slices.size(); ++t) {
            seed[t] = running;
            running = ObliviousChoose(tailReal[t] != 0, tail[t], running);
        }

        // Phase 3: original redup loop, per slice, with seeded lastRow.
        for (size_t t = 0; t < slices.size(); ++t) {
            fs.push_back(pool.submit([&, t] {
                Row lastRow = seed[t];
                for (size_t i = slices[t].begin; i < slices[t].end; ++i) {
                    auto secKey = table.rows[i].key.second;
                    table.rows[i] = ObliviousChoose(
                        table.rows[i].isDummy(), lastRow, table.rows[i]);
                    table.rows[i].key.second = secKey;
                    lastRow = table.rows[i];
                }
            }));
        }
        for (auto& f : fs) f.get();
    }

    Table buildSourceAndEdgeTables(Catalog& catalog, const OneHopQuery& query,
                                    ThreadPool &pool, NodeIndex* srcIndex,
                                    Table srcSide) {
        TimedScope ts_total("src branch (total)", "ONLINE", /*contributes_to_total=*/false);

        string srcPrefix = query.sourceNodeTableName;
        if (query.sourceNodeTableName == query.destNodeTableName)
            srcPrefix += "_src";

        if (query.projectionColumns.empty()) {
            const Table& srcRef = catalog.getTable(query.sourceNodeTableName);
            // Move out of the catalog: oneHop's driver is single-use, the fwd edge table
            // is not read again after this call. Avoids a 50M-row copy on banking_10M.
            Table edgeTableFwd = std::move(catalog.getTable(query.edgeTableName + "_fwd"));

            // srcSide arrives initialized (full node schema, edge keys copied) — see oneHop().
            {
                TimedScope ts("deduplicateRows (src)", "ONLINE", /*contributes_to_total=*/false);
                deduplicateRowsParallel(srcSide, pool);
            }

            if (srcIndex)
                probe_with_index(*srcIndex, srcSide, pool, "src");
            else
                build_and_probe(srcRef, srcSide, pool);

            {
                TimedScope ts("reduplicateRows (src)", "ONLINE", /*contributes_to_total=*/false);
                reduplicateRowsParallel(srcSide, pool);
            }
            {
                TimedScope ts("unionWith (src)", "ONLINE", /*contributes_to_total=*/false);
                edgeTableFwd.unionWith(srcSide, pool, srcPrefix);
            }
            return edgeTableFwd;
        }

        set<string> edgeColumns;

        // srcTable is only read by build_and_probe (the no-srcIndex fallback), so bind
        // by reference — avoids copying the full node table (10M rows on banking_10M).
        const Table& srcTable = catalog.getTable(query.sourceNodeTableName);
        // Move out of the catalog: single-use driver, fwd edge table not read again.
        Table edgeTableFwd = std::move(catalog.getTable(query.edgeTableName + "_fwd"));

        for (const auto& col : query.projectionColumns) {
            if (col.first == query.edgeTableName)
                edgeColumns.insert(col.second);
        }
        for (const auto& tablePred : query.tablePredicates) {
            if (tablePred.first == query.edgeTableName)
                for (const auto& pred : tablePred.second) edgeColumns.insert(pred.column);
        }

        // srcSide carries the full node schema (built in oneHop OFFLINE phase). Per-query
        // narrowing of node columns is deferred to applyFilterAndProject.
        Table edgeProjectedFwd = edgeTableFwd.project(vector<string>(edgeColumns.begin(), edgeColumns.end()), pool);

        {
            TimedScope ts("deduplicateRows (src)", "ONLINE", /*contributes_to_total=*/false);
            deduplicateRowsParallel(srcSide, pool);
        }

        if (srcIndex)
            probe_with_index(*srcIndex, srcSide, pool, "src");
        else
            build_and_probe(srcTable, srcSide, pool);

        {
            TimedScope ts("reduplicateRows (src)", "ONLINE", /*contributes_to_total=*/false);
            reduplicateRowsParallel(srcSide, pool);
        }
        {
            TimedScope ts("unionWith (src)", "ONLINE", /*contributes_to_total=*/false);
            edgeProjectedFwd.unionWith(srcSide, pool, srcPrefix);
        }
        return edgeProjectedFwd;
    }

    Table buildDestinationTable(Catalog& catalog, const OneHopQuery& query,
                                ThreadPool& pool, NodeIndex* dstIndex,
                                Table dstSide) {
        TimedScope ts_total("dst branch (total)", "ONLINE", /*contributes_to_total=*/false);

        string dstPrefix = query.destNodeTableName;
        if (query.sourceNodeTableName == query.destNodeTableName)
            dstPrefix += "_dest";

        // Move out of the catalog: single-use driver, rev edge table not read again.
        Table edgeTableRev = std::move(catalog.getTable(query.edgeTableName + "_rev"));

        auto doSortAndReturn = [&]() -> Table {
            TimedScope ts("parallel_sort (dst)", "ONLINE", /*contributes_to_total=*/false);
            parallel_sort(edgeTableRev.rows.begin(), edgeTableRev.rows.end(),
                pool,
                [](const Row& a, const Row& b) {
                    bool eq  = (a.key.second == b.key.second);
                    bool lt2 = (a.key.second <  b.key.second);
                    bool lt1 = (a.key.first  <  b.key.first);
                    return ObliviousChoose(eq, lt1, lt2);
                },
                pool.size()
            );
            // edgeTableRev is captured by reference; return-by-value would copy. Move.
            return std::move(edgeTableRev);
        };

        if (query.projectionColumns.empty()) {
            const Table& dstRef = catalog.getTable(query.destNodeTableName);

            // dstSide arrives initialized (full node schema, edge keys copied) — see oneHop().
            {
                TimedScope ts("deduplicateRows (dst)", "ONLINE", /*contributes_to_total=*/false);
                deduplicateRowsParallel(dstSide, pool);
            }

            if (dstIndex)
                probe_with_index(*dstIndex, dstSide, pool, "dst");
            else
                build_and_probe(dstRef, dstSide, pool);

            {
                TimedScope ts("reduplicateRows (dst)", "ONLINE", /*contributes_to_total=*/false);
                reduplicateRowsParallel(dstSide, pool);
            }
            {
                TimedScope ts("unionWith (dst)", "ONLINE", /*contributes_to_total=*/false);
                edgeTableRev.unionWith(dstSide, pool, dstPrefix);
            }

            return doSortAndReturn();
        }

        // dstTable is only read by build_and_probe (the no-dstIndex fallback), so bind
        // by reference — avoids copying the full node table.
        const Table& dstTable = catalog.getTable(query.destNodeTableName);

        // dstSide carries the full node schema (built in oneHop OFFLINE phase). Per-query
        // narrowing of node columns is deferred to applyFilterAndProject.
        {
            TimedScope ts("deduplicateRows (dst)", "ONLINE", /*contributes_to_total=*/false);
            deduplicateRowsParallel(dstSide, pool);
        }

        if (dstIndex)
            probe_with_index(*dstIndex, dstSide, pool, "dst");
        else
            build_and_probe(dstTable, dstSide, pool);

        {
            TimedScope ts("reduplicateRows (dst)", "ONLINE", /*contributes_to_total=*/false);
            reduplicateRowsParallel(dstSide, pool);
        }
        {
            TimedScope ts("unionWith (dst)", "ONLINE", /*contributes_to_total=*/false);
            edgeTableRev.unionWith(dstSide, pool, dstPrefix);
        }

        return doSortAndReturn();
    }

    // ---------------------------------------------------------------------------
    // Shared filter + project logic after the parallel branches complete.
    // ---------------------------------------------------------------------------
    static void applyFilterAndProject(Table& result, OneHopQuery& query, ThreadPool& pool) {
        bool isSelfReferential = (query.sourceNodeTableName == query.destNodeTableName);

        vector<Predicate> allPredicates;
        for (const auto& tablePred : query.tablePredicates) {
            for (const auto& pred : tablePred.second) {
                Predicate qualifiedPred = pred;
                string tablePrefix = tablePred.first;
                if (isSelfReferential && tablePred.first == query.sourceNodeTableName)
                    tablePrefix += "_src";
                qualifiedPred.column = tablePrefix + "_" + pred.column;
                allPredicates.push_back(qualifiedPred);
            }
        }
        if (!allPredicates.empty()) {
            TimedScope ts("filter", "ONLINE");
            result.filter(allPredicates, pool);
        }

        if (query.projectionColumns.empty())
            return;

        vector<string> projectionColumns;
        for (const auto& col : query.projectionColumns) {
            string columnName;
            if (col.first == query.edgeTableName) {
                columnName = col.second;
            } else {
                string tablePrefix = col.first;
                if (isSelfReferential) {
                    if (col.first.length() > 4 && col.first.substr(col.first.length() - 4) == "_src")
                        tablePrefix = col.first;
                    else if (col.first.length() > 5 && col.first.substr(col.first.length() - 5) == "_dest")
                        tablePrefix = col.first;
                    else if (col.first == query.destNodeTableName)
                        tablePrefix += "_dest";
                }
                columnName = tablePrefix + "_" + col.second;
            }
            projectionColumns.push_back(columnName);
        }
        TimedScope ts("project", "ONLINE");
        result = result.project(projectionColumns, pool);
    }

    // Build the two probe-side scaffolds: full node schema + per-edge key copy. The
    // result depends only on the graph (node table + edge keys + edge count), never on
    // query particulars, so this whole block is OFFLINE — query-independent setup that
    // can be amortized across queries against the same graph.
    static void initProbeSidesOffline(Catalog& catalog, const OneHopQuery& query,
                                       ThreadPool& pool, Table& srcSide, Table& dstSide) {
        srcSide.init(catalog.getTable(query.sourceNodeTableName));
        dstSide.init(catalog.getTable(query.destNodeTableName));
        TimedScope ts_wall("initProbeSide (wall)", "OFFLINE");
        auto fSrc = std::async(std::launch::async, [&] {
            TimedScope ts("initProbeSide (src)", "OFFLINE", /*contributes_to_total=*/false);
            initProbeSide(srcSide, catalog.getTable(query.edgeTableName + "_fwd"), pool);
        });
        {
            TimedScope ts("initProbeSide (dst)", "OFFLINE", /*contributes_to_total=*/false);
            initProbeSide(dstSide, catalog.getTable(query.edgeTableName + "_rev"), pool);
        }
        fSrc.get();
    }

    Table oneHop(Catalog& catalog, OneHopQuery& query, ThreadPool& pool) {
        Table srcSide, dstSide;
        initProbeSidesOffline(catalog, query, pool, srcSide, dstSide);

        Table edgeProjectedFwd, edgeProjectedRev;
        {
            TimedScope ts("parallel branches (wall)", "ONLINE");
            auto futureFwd = std::async(std::launch::async, buildSourceAndEdgeTables,
                                        std::ref(catalog), std::ref(query), std::ref(pool),
                                        nullptr, std::move(srcSide));
            auto futureRev = std::async(std::launch::async, buildDestinationTable,
                                        std::ref(catalog), std::ref(query), std::ref(pool),
                                        nullptr, std::move(dstSide));
            edgeProjectedFwd = futureFwd.get();
            edgeProjectedRev = futureRev.get();
        }
        {
            TimedScope ts("unionWith (final)", "ONLINE");
            edgeProjectedFwd.unionWith(edgeProjectedRev, pool);
        }
        applyFilterAndProject(edgeProjectedFwd, query, pool);
        return edgeProjectedFwd;
    }

    Table oneHop(Catalog& catalog, OneHopQuery& query, ThreadPool& pool,
                 std::unique_ptr<NodeIndex> srcIndex,
                 std::unique_ptr<NodeIndex> dstIndex) {
        NodeIndex* srcPtr = srcIndex.release();
        NodeIndex* dstPtr = dstIndex.release();

        Table srcSide, dstSide;
        initProbeSidesOffline(catalog, query, pool, srcSide, dstSide);

        Table edgeProjectedFwd, edgeProjectedRev;
        {
            TimedScope ts("parallel branches (wall)", "ONLINE");
            auto futureFwd = std::async(std::launch::async, buildSourceAndEdgeTables,
                                        std::ref(catalog), std::ref(query), std::ref(pool),
                                        srcPtr, std::move(srcSide));
            auto futureRev = std::async(std::launch::async, buildDestinationTable,
                                        std::ref(catalog), std::ref(query), std::ref(pool),
                                        dstPtr, std::move(dstSide));
            edgeProjectedFwd = futureFwd.get();
            delete srcPtr;
            edgeProjectedRev = futureRev.get();
            delete dstPtr;
        }
        {
            TimedScope ts("unionWith (final)", "ONLINE");
            edgeProjectedFwd.unionWith(edgeProjectedRev, pool);
        }
        applyFilterAndProject(edgeProjectedFwd, query, pool);
        return edgeProjectedFwd;
    }

    AmortizeTiming serveAmortized(Catalog& catalog, OneHopQuery& query,
                                  ThreadPool& pool, const Table& nodeTable,
                                  size_t edgeCount, int n) {
        using clk = std::chrono::steady_clock;
        auto ms = [](clk::time_point a, clk::time_point b) {
            return std::chrono::duration<double, std::milli>(b - a).count();
        };
        AmortizeTiming t;

        // --- Shared, ONCE: build the node index (the amortizable offline cost). ---
        auto b0 = clk::now();
        std::unique_ptr<NodeIndex> nodeIndex = buildNodeIndex(nodeTable, edgeCount);
        t.build_once_ms = ms(b0, clk::now());

        // The online path MOVES the forward/reverse edge tables out of the catalog
        // (std::move in buildSourceAndEdgeTables / buildDestinationTable), consuming
        // them. Keep pristine copies so every query starts from identical inputs, and
        // restore them by vector index each query — a moved-from table loses its name,
        // so it can no longer be found via getTable(). Node tables are only read.
        auto indexOf = [&](const std::string& nm) -> size_t {
            for (size_t i = 0; i < catalog.tables.size(); ++i)
                if (catalog.tables[i].name == nm) return i;
            throw std::runtime_error("serveAmortized: table not found: " + nm);
        };
        const size_t fwdIdx = indexOf(query.edgeTableName + "_fwd");
        const size_t revIdx = indexOf(query.edgeTableName + "_rev");
        const Table fwdPristine = catalog.tables[fwdIdx];   // deep copy, offline (once)
        const Table revPristine = catalog.tables[revIdx];

        // --- Per query: restore consumed inputs + fresh index copies (prep), then probe.
        //     nodeIndex stays immutable so it is reused across every query. Loop bound
        //     n is public — work per iteration is data-independent. ---
        t.prep_ms.reserve(n);
        t.serve_ms.reserve(n);
        for (int i = 0; i < n; ++i) {
            auto c0 = clk::now();
            catalog.tables[fwdIdx] = fwdPristine;   // restore forward edges
            catalog.tables[revIdx] = revPristine;   // restore reverse edges
            auto srcIndex = std::make_unique<NodeIndex>(*nodeIndex);
            auto dstIndex = std::make_unique<NodeIndex>(*nodeIndex);
            auto c1 = clk::now();
            Table result = oneHop(catalog, query, pool,
                                  std::move(srcIndex), std::move(dstIndex));
            auto c2 = clk::now();
            t.prep_ms.push_back(ms(c0, c1));
            t.serve_ms.push_back(ms(c1, c2));
            t.result_rows = result.rowCount;
        }
        return t;
    }

    void reportAmortize(const AmortizeTiming& t) {
        const size_t n = t.serve_ms.size();
        auto median = [](std::vector<double> v) {
            if (v.empty()) return 0.0;
            std::sort(v.begin(), v.end());
            size_t m = v.size() / 2;
            return (v.size() % 2) ? v[m] : 0.5 * (v[m - 1] + v[m]);
        };
        auto mean = [](const std::vector<double>& v) {
            if (v.empty()) return 0.0;
            double s = 0.0;
            for (double x : v) s += x;
            return s / static_cast<double>(v.size());
        };
        std::vector<double> total(n);
        for (size_t i = 0; i < n; ++i) total[i] = t.prep_ms[i] + t.serve_ms[i];

        const double prepMed = median(t.prep_ms);
        const double serveMed = median(t.serve_ms);
        const double totalMed = median(total);
        const double totalMean = mean(total);

        std::cout << "\n=== AMORTIZE (build-once / serve-N) ===\n"
                  << "  build_once (buildNodeIndex, SHARED across all queries): "
                  << t.build_once_ms << " ms\n"
                  << "  per-query median of " << n << ": prep(edge restore + index copy x2) = "
                  << prepMed << " ms + serve(initProbeSide+online) = " << serveMed
                  << " ms = " << totalMed << " ms\n"
                  << "  result rows (constant per query): " << t.result_rows << "\n";
        // Machine-parseable summary line.
        std::cout << "AMORTIZE serve_n=" << n
                  << " build_once_ms=" << t.build_once_ms
                  << " per_query_total_median_ms=" << totalMed
                  << " per_query_total_mean_ms=" << totalMean
                  << " per_query_prep_median_ms=" << prepMed
                  << " per_query_serve_median_ms=" << serveMed
                  << " result_rows=" << t.result_rows << "\n";
    }
} // namespace obligraph

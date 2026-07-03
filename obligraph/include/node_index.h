#pragma once

#include <memory>
#include <cstring>
#include <bit>
#include <vector>

#include "definitions.h"
#include "ohash_bin.hpp"

namespace obligraph {

    // Block size for ObliviousBin: key_t (8 bytes) + Row payload
    constexpr size_t ROW_BLOCK_SIZE = sizeof(key_t) + sizeof(Row);
    using RowBlock = ORAM::Block<key_t, ROW_BLOCK_SIZE>;

    // DUMMY_KEY_MSB is defined in definitions.h

    // Pre-built oblivious hash index over a node table
    using NodeIndex = ORAM::ObliviousBin<key_t, ROW_BLOCK_SIZE>;

    // triple32 hash function: https://github.com/skeeto/hash-prospector
    // exact bias: 0.020888578919738908
    inline uint32_t triple32(uint32_t x) {
        x ^= x >> 17;
        x *= 0xed5ad4bb;
        x ^= x >> 11;
        x *= 0xac4c1b51;
        x ^= x >> 15;
        x *= 0x31848bab;
        x ^= x >> 14;
        return x;
    }

    // Build an ObliviousBin index from a node table (offline build phase).
    // Returns a unique_ptr because ObliviousBin has no move constructor.
    std::unique_ptr<NodeIndex> buildNodeIndex(const Table& table, size_t op_num = 1);

    // oneHop overload that accepts pre-built indexes for probe-only execution.
    // Both indexes are consumed (probing is destructive).
    Table oneHop(Catalog& catalog, OneHopQuery& query, ThreadPool& pool,
                 std::unique_ptr<NodeIndex> srcIndex,
                 std::unique_ptr<NodeIndex> dstIndex);

    // Per-phase timings from a build-once / serve-N amortization run.
    struct AmortizeTiming {
        double build_once_ms = 0.0;     // buildNodeIndex — shared across ALL queries
        std::vector<double> prep_ms;    // per-query: restore consumed edge tables + 2 fresh index copies
        std::vector<double> serve_ms;   // per-query: initProbeSide + online probe/sort/union
        size_t result_rows = 0;         // rows from the last served query (constant across queries)
    };

    // Build the node index ONCE, then serve `n` one-hop queries reusing it. The online
    // path consumes (moves out) the forward/reverse edge tables and destroys the index
    // copies it is handed, so each query must restore the edge tables from pristine
    // copies and hand oneHop two fresh index copies (the shared index stays immutable).
    // The shared build and each per-query phase are timed separately, physically
    // demonstrating reuse of the offline index across many online queries. `n` is
    // public, so the fixed-bound loop preserves obliviousness.
    AmortizeTiming serveAmortized(Catalog& catalog, OneHopQuery& query,
                                  ThreadPool& pool, const Table& nodeTable,
                                  size_t edgeCount, int n);

    // Print a human-readable breakdown plus a machine-parseable "AMORTIZE ..." line
    // (build_once_ms, per-query median/mean copy/serve/total) for an amortization run.
    void reportAmortize(const AmortizeTiming& t);

} // namespace obligraph

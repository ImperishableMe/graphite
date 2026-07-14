/**
 * ldbc_snb_onehop.cpp
 *
 * Driver program to run one-hop join on the LDBC SNB workload (W2).
 * Mirror of snap_patents_onehop.cpp with schema adjusted for the SNB social
 * network: the account table (persons) has (account_id, city_id, gender,
 * birthday) and txn (mirrored knows edges) has (txn_id, acc_from, acc_to,
 * knows_day). Persons map to `account`, friendships map to `txn` (both
 * directions of each undirected knows edge are materialized by the converter)
 * so the rest of the E1 pipeline (rewriter, sgx_app schema-from-header import)
 * is reused unchanged.
 *
 * Usage:
 *   ./ldbc_snb_onehop <data_dir> <output_csv> [--report CATS] [--threads N]
 *
 *   --report <cats>   Comma-separated list of timing categories to sum for
 *                     the TIMING_REPORTED line (default: ONLINE).
 *                     Categories: IO, OFFLINE, ONLINE
 *   --threads <N>     Override the thread count (default: std::thread::hardware_concurrency()).
 */

#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "definitions.h"
#include "node_index.h"
#include "timer.h"
#include "config.h"

using namespace std;
using namespace obligraph;

static void writeTableToCSV(const Table& table, const string& filePath) {
    TimedScope ts("CSV write", "IO");
    ofstream file(filePath);
    if (!file.is_open())
        throw runtime_error("Cannot open output file: " + filePath);

    for (size_t i = 0; i < table.schema.columnMetas.size(); i++) {
        file << table.schema.columnMetas[i].name;
        if (i < table.schema.columnMetas.size() - 1) file << ",";
    }
    file << "\n";

    for (const auto& row : table.rows) {
        if (row.isDummy()) continue;
        for (size_t i = 0; i < table.schema.columnMetas.size(); i++) {
            ColumnValue val = row.getColumnValue(
                table.schema.columnMetas[i].name, table.schema);
            visit([&file](const auto& v) { file << v; }, val);
            if (i < table.schema.columnMetas.size() - 1) file << ",";
        }
        file << "\n";
    }
}

static vector<string> splitComma(const string& s) {
    vector<string> out;
    size_t start = 0;
    while (true) {
        size_t pos = s.find(',', start);
        out.push_back(s.substr(start, pos == string::npos ? string::npos : pos - start));
        if (pos == string::npos) break;
        start = pos + 1;
    }
    return out;
}

int main(int argc, char* argv[]) {
    if (argc < 3) {
        cerr << "Usage: " << argv[0] << " <data_dir> <output_csv> [--report CATS] [--threads N]\n"
             << "  CATS: comma-separated categories to report (default: ONLINE)\n"
             << "  Categories: IO, OFFLINE, ONLINE\n"
             << "  N:     thread count (default: hardware_concurrency())\n";
        return 1;
    }

    string dataDir    = argv[1];
    string outputPath = argv[2];
    vector<string> reportCats = {"ONLINE"};
    int nthreadsOverride = 0;  // 0 = unset, fall back to hardware_concurrency()

    for (int i = 3; i < argc; i++) {
        string arg = argv[i];
        if (arg == "--report" && i + 1 < argc) {
            reportCats = splitComma(argv[++i]);
        } else if (arg.rfind("--report=", 0) == 0) {
            reportCats = splitComma(arg.substr(9));
        } else if (arg == "--threads" && i + 1 < argc) {
            nthreadsOverride = std::stoi(argv[++i]);
        } else if (arg.rfind("--threads=", 0) == 0) {
            nthreadsOverride = std::stoi(arg.substr(10));
        }
    }
    if (nthreadsOverride < 0) {
        cerr << "--threads must be > 0 (got " << nthreadsOverride << ")\n";
        return 1;
    }

    try {
        int nthreads = nthreadsOverride > 0
                       ? nthreadsOverride
                       : static_cast<int>(std::thread::hardware_concurrency());
        obligraph::number_of_threads.store(nthreads);
        cout << "Using " << nthreads << " threads\n";

        // --- I/O: import ---
        cout << "Importing data from " << dataDir << "...\n";
        Catalog catalog;
        {
            TimedScope ts("CSV read (account)", "IO");
            catalog.importNodeFromCSV(
                dataDir + "/account.csv", ',',
                {{"account_id", "int32"}, {"city_id", "int32"},
                 {"gender", "int32"}, {"birthday", "int32"}},
                "account_id"
            );
        }
        {
            TimedScope ts("CSV read (txn)", "IO");
            // 4 int32 columns = 16 bytes; combined hop row (account 4 + txn 4 +
            // account 4 = 12 cols = 48 bytes) is exactly ROW_DATA_MAX_SIZE (48).
            catalog.importEdgeFromCSV(
                dataDir + "/txn.csv", ',',
                {{"txn_id", "int32"}, {"acc_from", "int32"},
                 {"acc_to", "int32"}, {"knows_day", "int32"}},
                "account", "txn", "account",
                "acc_from", "acc_to"
            );
        }
        cout << "Imported " << catalog.tables.size() << " tables\n";

        // --- OFFLINE: build index (done once, reused for both src and dst probes) ---
        ThreadPool pool(nthreads);
        Table& accountTable = catalog.getTable("account");
        size_t edgeCount = catalog.getTable("txn_fwd").rowCount;

        unique_ptr<NodeIndex> nodeIndex;
        {
            nodeIndex = buildNodeIndex(accountTable, edgeCount);
        }
        unique_ptr<NodeIndex> srcIndex, dstIndex;
        {
            TimedScope ts("index copy (src)", "OFFLINE");
            srcIndex = std::make_unique<NodeIndex>(*nodeIndex);
            dstIndex = std::move(nodeIndex);
        }

        // --- ONLINE: probe phase ---
        cout << "Executing one-hop join...\n";
        OneHopQuery query(
            "account", "txn", "account",
            vector<pair<string, vector<Predicate>>>{}
        );
        Table result = oneHop(catalog, query, pool,
                              std::move(srcIndex), std::move(dstIndex));

        cout << "Result: " << result.rowCount << " rows, "
             << result.schema.columnMetas.size() << " columns\n";

        cout << "Schema: ";
        for (size_t i = 0; i < result.schema.columnMetas.size(); i++) {
            cout << result.schema.columnMetas[i].name;
            if (i < result.schema.columnMetas.size() - 1) cout << ", ";
        }
        cout << "\n";

        cout << "Writing result to " << outputPath << "...\n";
        writeTableToCSV(result, outputPath);

        TimingCollector::get().report(reportCats);

    } catch (const exception& e) {
        cerr << "Error: " << e.what() << "\n";
        return 1;
    }

    return 0;
}

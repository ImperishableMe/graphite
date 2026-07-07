/* obliviator_khop_chained_main.c — Obliviator NFK-based K-hop, chained.
 *
 * Banking K-hop: account a1 ⋈ txn t1 ⋈ account a2 ⋈ … ⋈ txn tK ⋈ account a_{K+1}
 *   K ∈ {2, 3, 4, 5}.  K=1 is handled by obliviator_1hop_chained (FK kernel).
 *
 * Sequential chain of 2K pairwise joins, all on the NFK kernel:
 *   step 0 : a1   ⋈ t1   on a1.account_id = t1.acc_from   (FK-shape, NFK kernel)
 *   step 1 : prev ⋈ a2   on prev.acc_to   = a2.account_id (FK-shape, NFK kernel)
 *   step 2 : prev ⋈ t2   on prev.a2.id    = t2.acc_from   (true NFK)
 *   step 3 : prev ⋈ a3   on prev.acc_to   = a3.account_id (FK-shape, NFK kernel)
 *   step 4 : prev ⋈ t3   on prev.a3.id    = t3.acc_from   (true NFK)
 *   …
 *   step 2K-1 (last) : prev ⋈ a_{K+1}     (FK-shape, NFK kernel)
 *
 * NFK is correct on FK-shaped joins (cross-product degenerates to 1:N when one
 * side's keys are unique). Using NFK everywhere lets us write one driver that
 * covers all 2K steps uniformly.
 *
 * Schema convention (carried in .data of every elem_t):
 *   - account .data = "<account_id>[,<attr>…]"  (ACCT_COLS cols total, key
 *     DUPLICATED as the first col; banking: "<account_id>,<balance>,<owner_id>")
 *   - txn     .data = "<txn_id>,<acc_to>[,<attr>…]"  (TXN_COLS cols total,
 *     acc_to ALWAYS the 2nd col; key acc_from NOT in data — carried in .key
 *     only; banking: "<txn_id>,<acc_to>,<amount>,<txn_time>")
 *   - intermediate .data = prev.data + "," + next_base.data   (uniform L.data + R.data concat)
 *
 * Pre-loading the account_id into accounts' .data buys us a uniform concat rule
 * for every step. The txn's acc_from is always equal to the prior account's id,
 * which is already in the prev intermediate's .data, so we never need to inject
 * txn's .key into the concat.
 *
 * ACCT_COLS / TXN_COLS are the workload's payload widths, taken from optional
 * CLI args (defaults 3 and 4 = the banking W1 shape; AML W4 is 2,4; SNAP
 * patents W3 is 2,2). The next-join-key column indices into the cumulative
 * concat are computed from them at startup:
 *   even step (just joined txn t_k)      → col = total_cols - TXN_COLS + 1
 *                                          (t_k.acc_to, 2nd col of the txn block)
 *   odd  step (just joined account a_j)  → col = total_cols - ACCT_COLS
 *                                          (a_j.account_id, 1st col of the block)
 *   last step → -1 (no next-key needed, the kernel output goes to emit)
 * For the banking defaults this reproduces the historical table
 * 4,7,11,14,18,21,25,28,32.
 *
 * Final output schema (column count = ACCT_COLS*(K+1) + TXN_COLS*K).
 *
 * Caveat — NFK parallel correctness:
 *   At --threads ≥ 2, the upstream NFK cross-product path produces wrong
 *   pairings whenever a step has duplicate keys on both sides (m0>1 ∧ m1>1).
 *   Total row count is correct; specific tuples get duplicated and equally
 *   many valid tuples are dropped. See docs/obliviator_baseline_status.md.
 *   Single-thread runs are exact; scaling measurements at threads≥2 are
 *   defensible for performance numbers only (row count matches; pairings
 *   leak).
 *
 * CLI:
 *   ./obliviator_khop_chained <num_threads> <K> <src.txt> <output.csv> \
 *                             [<acct_cols> <txn_cols>]
 *
 * <src.txt> is the same combined account+txn file consumed by
 * obliviator_1hop_chained (produced by the workload's convert_*_1hop.py):
 * header "<N_acc> <N_txn>\n", then N_acc lines "<account_id> <payload…>"
 * (payload = acct_cols-1 comma-separated cols), then N_txn lines
 * "<acc_from> <txn_id>,<acc_to>[,<payload…>]" (txn_cols comma-separated cols).
 */

#include <ctype.h>
#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "common/elem_t.h"
#include "enclave/threading.h"

extern size_t total_num_threads;

extern int  scalable_oblivious_join_init(int nthreads);
extern void scalable_oblivious_join_free(void);
extern double scalable_oblivious_join_to_array(elem_t *arr, int length1, int length2,
                                                elem_t **arr1_out, elem_t **arr2_out,
                                                int *result_len,
                                                double *sort_time_out);

#define MAX_HOPS 5
#define MIN_HOPS 2

/* Column index of the next-join-key in the cumulative concat after each step,
 * indexed by step number (0..2*MAX_HOPS-1). Filled at startup from the
 * workload's payload widths (acct_cols, txn_cols); the last step uses -1.
 * For the banking defaults (3,4) this reproduces the historical table
 * 4,7,11,14,18,21,25,28,32,-1. */
static int NEXT_KEY_COL[2 * MAX_HOPS];

static void fill_next_key_cols(int num_steps, int acct_cols, int txn_cols) {
    int total = acct_cols; /* the chain starts as a1's columns */
    for (int step = 0; step < num_steps; step++) {
        if (step % 2 == 0) {
            total += txn_cols;                        /* joined txn t_k     */
            NEXT_KEY_COL[step] = total - txn_cols + 1; /* t_k.acc_to (2nd)  */
        } else {
            total += acct_cols;                       /* joined account a_j */
            NEXT_KEY_COL[step] = total - acct_cols;    /* a_j.account_id    */
        }
    }
    NEXT_KEY_COL[num_steps - 1] = -1; /* last step: output goes to emit */
}

static void *start_thread_work(void *arg) { (void)arg; thread_start_work(); return NULL; }
static void die(const char *msg) { fprintf(stderr, "%s\n", msg); exit(2); }

static double now_sec(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec + tv.tv_usec / 1e6;
}

/* ----- src.txt loader (off-clock) ---------------------------------------
 * Reads the combined account+txn file produced by convert_banking_1hop.py.
 * Account rows are stored with .data = "<account_id>,<balance>,<owner_id>"
 * (the account_id is duplicated into .data as the first column so that the
 * uniform L.data + R.data concat rule works at every subsequent step).
 * Txn rows retain the original .data = "<txn_id>,<acc_to>,<amount>,<txn_time>"
 * — the acc_from is held in .key only, never in .data.
 *
 * Returns one big buffer of length N_acc + N_txn:
 *   arr[0..N_acc)        : accounts (table_0=true), .data = "id,bal,own"
 *   arr[N_acc..N_acc+N_txn) : txns   (table_0=false), .data = "txn_id,acc_to,amount,txn_time"
 */
static elem_t *load_file(const char *path, int *n_acc_out, int *n_txn_out) {
    FILE *fp = fopen(path, "r");
    if (!fp) { perror(path); exit(2); }
    long long n_acc_ll, n_txn_ll;
    if (fscanf(fp, "%lld %lld\n", &n_acc_ll, &n_txn_ll) != 2) {
        fprintf(stderr, "%s: bad header\n", path); exit(2);
    }
    if (n_acc_ll < 0 || n_txn_ll < 0
        || n_acc_ll > (long long)((1U << 31) - 1)
        || n_txn_ll > (long long)((1U << 31) - 1)) {
        fprintf(stderr, "%s: row counts out of int range (n_acc=%lld n_txn=%lld)\n",
                path, n_acc_ll, n_txn_ll); exit(2);
    }
    int n_acc = (int)n_acc_ll;
    int n_txn = (int)n_txn_ll;
    long long total = (long long)n_acc + (long long)n_txn;

    elem_t *arr = calloc((size_t)total, sizeof(*arr));
    if (!arr) { fprintf(stderr, "%s: calloc(%lld elem_t) failed\n", path, total); exit(2); }

    /* Accounts: prepend account_id into .data so it carries through the chain. */
    char tmp[DATA_LENGTH];
    for (int i = 0; i < n_acc; i++) {
        long long key;
        if (fscanf(fp, "%lld %[^\n]\n", &key, tmp) != 2) {
            fprintf(stderr, "%s: parse error at account row %d\n", path, i); exit(2);
        }
        tmp[DATA_LENGTH - 1] = '\0';
        int w = snprintf(arr[i].data, DATA_LENGTH, "%lld,%s", key, tmp);
        if (w < 0 || w >= DATA_LENGTH) {
            fprintf(stderr, "%s: account row %d overflows DATA_LENGTH=%d (need %d)\n",
                    path, i, DATA_LENGTH, w); exit(2);
        }
        arr[i].key = (int)key;
        arr[i].table_0 = true;
    }
    /* Txns: keep .data as-is; .key holds acc_from. */
    for (int i = 0; i < n_txn; i++) {
        long long key;
        if (fscanf(fp, "%lld %[^\n]\n", &key, arr[n_acc + i].data) != 2) {
            fprintf(stderr, "%s: parse error at txn row %d\n", path, i); exit(2);
        }
        arr[n_acc + i].data[DATA_LENGTH - 1] = '\0';
        arr[n_acc + i].key = (int)key;
        arr[n_acc + i].table_0 = false;
    }
    fclose(fp);
    *n_acc_out = n_acc;
    *n_txn_out = n_txn;
    return arr;
}

/* ----- generic chunked-parallel dispatch (same as 1-hop chained) ---------- */
static void dispatch(void (*worker)(void *), void *args_base, size_t arg_stride,
                     size_t num_threads) {
    struct thread_work *works = (num_threads > 1)
        ? calloc(num_threads - 1, sizeof(*works)) : NULL;
    if (num_threads > 1 && !works) die("dispatch: thread_work alloc failed");
    char *base = (char *)args_base;
    for (size_t t = 0; t < num_threads - 1; t++) {
        works[t].type = THREAD_WORK_SINGLE;
        works[t].single.func = worker;
        works[t].single.arg = base + t * arg_stride;
        thread_work_push(&works[t]);
    }
    worker(base + (num_threads - 1) * arg_stride);
    for (size_t t = 0; t + 1 < num_threads; t++) thread_wait(&works[t]);
    free(works);
}

/* ----- intermediate builder (parallel) -----------------------------------
 *
 * After each NFK kernel call:
 *   arr1[i] : matched table_0=true row, .data = prior cumulative concat
 *   arr2[i] : matched table_0=false row, .data = new base table's columns
 *
 * For all steps except the last, we build a new intermediate row:
 *   out[i].data    = arr1[i].data + "," + arr2[i].data
 *   out[i].table_0 = true
 *   out[i].key     = (int)strtoll at column NEXT_KEY_COL[step] of out[i].data
 *
 * The concat width never depends on row values — only on the step number — so
 * obliviousness is preserved as long as the kernel calls themselves are.
 */
struct inter_args {
    int lo, hi;
    elem_t *arr1;
    elem_t *arr2;
    elem_t *out;
    int next_key_col;
};

static void inter_worker(void *voidargs) {
    struct inter_args *a = (struct inter_args *)voidargs;
    for (int i = a->lo; i < a->hi; i++) {
        int w = snprintf(a->out[i].data, DATA_LENGTH, "%s,%s",
                         a->arr1[i].data, a->arr2[i].data);
        if (w < 0 || w >= DATA_LENGTH) {
            fprintf(stderr, "inter row %d: data overflow (need %d, have %d)\n",
                    i, w, DATA_LENGTH);
            exit(3);
        }
        a->out[i].table_0 = true;

        /* Walk to the start of column next_key_col. */
        const char *p = a->out[i].data;
        int col = 0;
        while (*p && col < a->next_key_col) {
            if (*p == ',') col++;
            p++;
        }
        if (col != a->next_key_col) {
            fprintf(stderr, "inter row %d: next_key_col=%d past end of data\n",
                    i, a->next_key_col);
            exit(3);
        }
        a->out[i].key = (int)strtoll(p, NULL, 10);
    }
}

/* ----- emit worker (parallel format) -------------------------------------
 *
 * After the last kernel call, write one CSV row per (arr1[i], arr2[i]) pair:
 *   "<arr1[i].data>,<arr2[i].data>\n"
 * Each thread writes into a private buffer; main concatenates them serially
 * to disk (off-clock).
 */
struct emit_args {
    int lo, hi;
    elem_t *arr1;
    elem_t *arr2;
    char *buf;
    size_t buf_cap;
    size_t bytes_used;
};

static void emit_worker(void *voidargs) {
    struct emit_args *a = (struct emit_args *)voidargs;
    char *p = a->buf;
    for (int i = a->lo; i < a->hi; i++) {
        p += sprintf(p, "%s,%s\n", a->arr1[i].data, a->arr2[i].data);
    }
    a->bytes_used = (size_t)(p - a->buf);
}

/* ----- header builder ----------------------------------------------------
 * Constructs the output CSV header for a K-hop chain:
 *   a1.account_id,a1.balance,a1.owner_id,
 *   t1.txn_id,t1.acc_to,t1.amount,t1.txn_time,
 *   a2.account_id,a2.balance,a2.owner_id,
 *   …
 *   a_{K+1}.account_id,a_{K+1}.balance,a_{K+1}.owner_id\n
 */
static void build_header(char *buf, size_t cap, int K) {
    char *p = buf;
    size_t left = cap;
    for (int hop = 1; hop <= K + 1; hop++) {
        if (hop > 1) {
            int tidx = hop - 1;
            int w = snprintf(p, left, "t%d.txn_id,t%d.acc_to,t%d.amount,t%d.txn_time,",
                             tidx, tidx, tidx, tidx);
            if (w < 0 || (size_t)w >= left) die("header buffer too small");
            p += w; left -= w;
        }
        int w = snprintf(p, left, "a%d.account_id,a%d.balance,a%d.owner_id",
                         hop, hop, hop);
        if (w < 0 || (size_t)w >= left) die("header buffer too small");
        p += w; left -= w;
        if (hop < K + 1) {
            if (left < 2) die("header buffer too small");
            *p++ = ','; left--;
        }
    }
    if (left < 2) die("header buffer too small");
    *p++ = '\n';
    *p = '\0';
}

/* ----- main -------------------------------------------------------------- */
int main(int argc, char **argv) {
    if (argc != 5 && argc != 7) {
        fprintf(stderr,
                "usage: %s <num_threads> <K> <src.txt> <output.csv> [<acct_cols> <txn_cols>]\n"
                "  K is the hop count (number of transactions in the chain).\n"
                "  Valid range: %d..%d. For K=1 use obliviator_1hop_chained.\n"
                "  acct_cols/txn_cols are the comma-column counts of the account\n"
                "  and txn .data payloads (defaults 3 and 4 = banking W1;\n"
                "  AML W4: 2 4; SNAP patents W3: 2 2). acc_to must be the txn\n"
                "  payload's 2nd column.\n",
                argv[0], MIN_HOPS, MAX_HOPS);
        return 1;
    }
    size_t num_threads = (size_t)atoi(argv[1]);
    int K = atoi(argv[2]);
    const char *src_path = argv[3];
    const char *out_csv  = argv[4];
    int acct_cols = 3, txn_cols = 4; /* banking W1 shape */
    if (argc == 7) {
        acct_cols = atoi(argv[5]);
        txn_cols  = atoi(argv[6]);
        if (acct_cols < 1 || txn_cols < 2) {
            fprintf(stderr, "error: acct_cols must be >= 1 and txn_cols >= 2 "
                            "(got %d, %d)\n", acct_cols, txn_cols);
            return 1;
        }
    }

    if (K == 1) {
        fprintf(stderr,
                "error: K=1 is handled by obliviator_1hop_chained (FK kernel, faster).\n"
                "       Use that binary for the 1-hop banking chain.\n");
        return 1;
    }
    if (K < MIN_HOPS || K > MAX_HOPS) {
        fprintf(stderr, "error: K=%d out of range; valid: %d..%d\n",
                K, MIN_HOPS, MAX_HOPS);
        return 1;
    }
    if (num_threads < 1) {
        fprintf(stderr, "error: num_threads must be >= 1\n");
        return 1;
    }
    total_num_threads = num_threads;

    int num_steps = 2 * K;
    fill_next_key_cols(num_steps, acct_cols, txn_cols);

    if (scalable_oblivious_join_init((int)num_threads) != 0) die("join init failed");
    thread_system_init();

    pthread_t *threads = NULL;
    if (num_threads > 1) {
        threads = malloc((num_threads - 1) * sizeof(pthread_t));
        if (!threads) die("thread array alloc failed");
        for (size_t i = 0; i < num_threads - 1; i++) {
            int rc = pthread_create(&threads[i], NULL, start_thread_work, NULL);
            if (rc != 0) { fprintf(stderr, "pthread_create: %s\n", strerror(rc)); return 1; }
        }
        printf("Threads: %zu  (1 main + %zu workers)\n", num_threads, num_threads - 1);
    } else {
        printf("Threads: 1\n");
    }
    printf("Kernel:  NFK  (non-foreign-key, general equi-join)\n");
    printf("Hops:    K=%d  (%d pairwise steps)\n", K, num_steps);
    printf("Payload: acct_cols=%d txn_cols=%d\n", acct_cols, txn_cols);

    /* ============================================================
     * Off-clock: load src.txt and split into master copies.
     * Each base table is reused multiple times (accounts K+1 times,
     * txns K times); we keep one canonical copy of each.
     * ============================================================ */
    double load_t_s = now_sec();
    int n_acc = 0, n_txn = 0;
    elem_t *loaded = load_file(src_path, &n_acc, &n_txn);
    double load_t = now_sec() - load_t_s;
    printf("Loaded src: %d accts + %d txns  (%.3f s, off-clock)\n",
           n_acc, n_txn, load_t);

    elem_t *accounts_master = malloc((size_t)n_acc * sizeof(*accounts_master));
    elem_t *txns_master = malloc((size_t)n_txn * sizeof(*txns_master));
    if (!accounts_master || !txns_master) die("master alloc failed");
    memcpy(accounts_master, loaded, (size_t)n_acc * sizeof(*accounts_master));
    memcpy(txns_master, loaded + n_acc, (size_t)n_txn * sizeof(*txns_master));
    free(loaded);

    /* ============================================================
     * On-clock: 2K pairwise NFK joins, chained.
     *
     * prev_inter[] is the table_0=true side handed to the next step:
     *   step 0 → side0 = fresh accounts_master copy
     *   step i>0 → side0 = prev_inter from step i-1
     *
     * side1 alternates between txns and accounts:
     *   step 0 → side1 = txns
     *   step 1 → side1 = accounts
     *   step 2 → side1 = txns
     *   step 3 → side1 = accounts
     *   …
     * I.e. side1 = txns iff step is even.
     * ============================================================ */
    elem_t *prev_inter = NULL;
    int prev_len = 0;

    elem_t *last_arr1 = NULL;
    elem_t *last_arr2 = NULL;
    int last_len = 0;

    double total_sort_sec = 0.0;
    double total_join_sec = 0.0;
    double total_merge_sec = 0.0;
    double total_build_sec = 0.0;

    for (int step = 0; step < num_steps; step++) {
        bool side1_is_txn = (step % 2 == 0);
        int len0, len1;

        /* ---- merge: build kernel input buffer ---- */
        double merge_s = now_sec();
        elem_t *side1_master = side1_is_txn ? txns_master   : accounts_master;
        int     side1_count = side1_is_txn ? n_txn          : n_acc;

        if (step == 0) {
            len0 = n_acc;
            len1 = n_txn;
        } else {
            len0 = prev_len;
            len1 = side1_count;
        }

        long long total_ll = (long long)len0 + (long long)len1;
        if (total_ll > (long long)((1U << 31) - 1)) {
            fprintf(stderr, "step %d: input size %lld exceeds NFK int range\n",
                    step, total_ll);
            exit(2);
        }
        int total = (int)total_ll;

        elem_t *arr = malloc((size_t)total * sizeof(*arr));
        if (!arr) die("kernel input buffer alloc failed");

        if (step == 0) {
            memcpy(arr, accounts_master, (size_t)n_acc * sizeof(*arr));
            memcpy(arr + n_acc, txns_master, (size_t)n_txn * sizeof(*arr));
        } else {
            memcpy(arr, prev_inter, (size_t)prev_len * sizeof(*arr));
            memcpy(arr + prev_len, side1_master, (size_t)side1_count * sizeof(*arr));
            free(prev_inter);
            prev_inter = NULL;
        }
        /* Force table_0 flags — masters have arbitrary table_0; the kernel
         * requires table_0=true for arr[0..len0) and table_0=false for the rest. */
        for (int j = 0; j < len0; j++) arr[j].table_0 = true;
        for (int j = 0; j < len1; j++) arr[len0 + j].table_0 = false;
        double merge_t = now_sec() - merge_s;

        /* ---- kernel ---- */
        elem_t *arr1_out = NULL;
        elem_t *arr2_out = NULL;
        int result_len = 0;
        double sort_t = 0.0;
        double join_t = scalable_oblivious_join_to_array(
            arr, len0, len1, &arr1_out, &arr2_out, &result_len, &sort_t);
        free(arr);

        printf("step %d: input=%d+%d=%d  matched=%d  "
               "sort=%.6f  join_total=%.6f  merge=%.6f",
               step, len0, len1, total, result_len, sort_t, join_t, merge_t);

        total_sort_sec  += sort_t;
        total_join_sec  += join_t;
        total_merge_sec += merge_t;

        /* ---- build next intermediate (or stash for emit) ---- */
        if (step < num_steps - 1) {
            int nkc = NEXT_KEY_COL[step];
            if (nkc < 0) die("internal: non-last step has NEXT_KEY_COL=-1");

            double build_s = now_sec();
            elem_t *new_inter = NULL;
            if (result_len > 0) {
                new_inter = malloc((size_t)result_len * sizeof(*new_inter));
                if (!new_inter) die("intermediate buffer alloc failed");

                struct inter_args *iargs = calloc(num_threads, sizeof(*iargs));
                if (!iargs) die("inter args calloc failed");
                int rows_per   = result_len / (int)num_threads;
                int rows_extra = result_len % (int)num_threads;
                int cursor = 0;
                for (size_t t = 0; t < num_threads; t++) {
                    int chunk = rows_per + (t < (size_t)rows_extra ? 1 : 0);
                    iargs[t].lo = cursor;
                    iargs[t].hi = cursor + chunk;
                    iargs[t].arr1 = arr1_out;
                    iargs[t].arr2 = arr2_out;
                    iargs[t].out = new_inter;
                    iargs[t].next_key_col = nkc;
                    cursor += chunk;
                }
                dispatch(inter_worker, iargs, sizeof(*iargs), num_threads);
                free(iargs);
            }
            double build_t = now_sec() - build_s;
            total_build_sec += build_t;

            printf("  build_intermediate=%.6f\n", build_t);
            free(arr1_out);
            free(arr2_out);
            prev_inter = new_inter;
            prev_len = result_len;
        } else {
            printf("  (last step — output retained for emit)\n");
            last_arr1 = arr1_out;
            last_arr2 = arr2_out;
            last_len = result_len;
        }
    }

    /* ============================================================
     * Emit (parallel format → off-clock disk write).
     * ============================================================ */
    char header[2048];
    build_header(header, sizeof(header), K);

    const size_t row_cap = 2 * DATA_LENGTH + 4;  /* "<arr1.data>,<arr2.data>\n" */

    double emit_s = now_sec();
    struct emit_args *eargs = calloc(num_threads, sizeof(*eargs));
    if (!eargs) die("emit args calloc failed");
    int erows_per   = (last_len > 0) ? last_len / (int)num_threads : 0;
    int erows_extra = (last_len > 0) ? last_len % (int)num_threads : 0;
    int ecursor = 0;
    for (size_t t = 0; t < num_threads; t++) {
        int chunk = erows_per + (t < (size_t)erows_extra ? 1 : 0);
        eargs[t].lo = ecursor;
        eargs[t].hi = ecursor + chunk;
        eargs[t].arr1 = last_arr1;
        eargs[t].arr2 = last_arr2;
        eargs[t].buf_cap = (size_t)chunk * row_cap;
        eargs[t].buf = malloc(eargs[t].buf_cap > 0 ? eargs[t].buf_cap : 1);
        if (!eargs[t].buf) die("emit per-thread buf alloc failed");
        eargs[t].bytes_used = 0;
        ecursor += chunk;
    }
    if (last_len > 0) {
        dispatch(emit_worker, eargs, sizeof(*eargs), num_threads);
    }
    double emit_t = now_sec() - emit_s;

    /* Off-clock disk write. */
    double write_s = now_sec();
    FILE *out = fopen(out_csv, "w");
    if (!out) { perror(out_csv); exit(2); }
    fwrite(header, 1, strlen(header), out);
    for (size_t t = 0; t < num_threads; t++) {
        if (eargs[t].bytes_used > 0) {
            fwrite(eargs[t].buf, 1, eargs[t].bytes_used, out);
        }
        free(eargs[t].buf);
    }
    fclose(out);
    double write_t = now_sec() - write_s;
    free(eargs);
    free(last_arr1);
    free(last_arr2);
    free(accounts_master);
    free(txns_master);

    /* ============================================================
     * Summary.
     * ============================================================ */
    double oblivious_total = total_join_sec + total_merge_sec + total_build_sec;

    printf("\n=== %d-Hop Summary (NFK, chained) ===\n", K);
    printf("  total_sort_sec              : %.6f\n", total_sort_sec);
    printf("  total_join_sec              : %.6f   [includes sort]\n", total_join_sec);
    printf("  total_merge_sec             : %.6f   [build kernel input]\n", total_merge_sec);
    printf("  total_build_intermediate_sec: %.6f\n", total_build_sec);
    printf("  emit_sec                    : %.6f\n", emit_t);
    printf("  ----------------------------------------\n");
    printf("  online_sec  (sum on-clock)  : %.6f   [join+merge+build]\n", oblivious_total);
    printf("  online_plus_emit_sec        : %.6f   [...plus in-memory CSV format]\n",
           oblivious_total + emit_t);
    printf("  final_rows                  : %d\n", last_len);
    printf("  output_csv                  : %s\n", out_csv);
    printf("  off-clock CSV I/O           : load %.3f, write %.3f = %.3f s total\n",
           load_t, write_t, load_t + write_t);

    if (num_threads > 1 && threads) {
        thread_release_all();
        for (size_t i = 0; i < num_threads - 1; i++) pthread_join(threads[i], NULL);
        free(threads);
    }
    thread_system_cleanup();
    /* scalable_oblivious_join_free() intentionally not called — kernel
     * core invokes aggregation_tree_free() per step; a trailing call would
     * double-free. Matches multiway_main.c and upstream standalone_main.c. */
    return 0;
}

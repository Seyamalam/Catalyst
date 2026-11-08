//! Rust-only Criterion benchmarks for the synchronous SQLite core.

#[allow(dead_code, unused_imports)]
// This includes pool.rs verbatim: it must stay self-contained (rusqlite +
// std only, no crate-internal references) or the bench compilation will
// diverge from the lib. Split into a shared internal crate if it grows deps.
#[path = "../src/pool.rs"]
mod pool;

use criterion::{BenchmarkId, Criterion, black_box, criterion_group, criterion_main};
use pool::Pool;
use rusqlite::named_params;
use std::time::Duration;

fn benchmark_pool_checkout(c: &mut Criterion) {
    let path = std::env::temp_dir().join(format!("catalyst-bench-{}.sqlite", std::process::id()));
    let pool = Pool::new_with_options(
        path.to_str().expect("UTF-8 temp path"),
        5,
        0,
        Duration::from_secs(1),
        Duration::from_secs(5),
    )
    .expect("create benchmark pool");

    c.bench_function("pool/checkout_return", |b| {
        b.iter(|| {
            let connection = pool.checkout().expect("checkout");
            black_box(connection);
        });
    });
    drop(pool);
    let _ = std::fs::remove_file(path);
}

fn benchmark_sqlite_core(c: &mut Criterion) {
    let path =
        std::env::temp_dir().join(format!("catalyst-sql-bench-{}.sqlite", std::process::id()));
    let pool = Pool::new_with_options(
        path.to_str().expect("UTF-8 temp path"),
        1,
        0,
        Duration::from_secs(1),
        Duration::from_secs(5),
    )
    .expect("create benchmark pool");
    let checkout = pool.checkout().expect("initial checkout");
    let connection = checkout.connection_handle().expect("connection handle");
    {
        let connection = connection.lock().expect("connection lock");
        connection
            .execute_batch(
                "CREATE TABLE rows (value INTEGER NOT NULL);\
                 WITH RECURSIVE numbers(value) AS (\
                   SELECT 1 UNION ALL SELECT value + 1 FROM numbers WHERE value < 1000\
                 ) INSERT INTO rows SELECT value FROM numbers;",
            )
            .expect("seed benchmark rows");
    }
    drop(checkout);

    let mut group = c.benchmark_group("sqlite");
    for row_count in [10, 100, 1000] {
        group.bench_with_input(
            BenchmarkId::new("buffered_select_rows", row_count),
            &row_count,
            |b, &row_count| {
                b.iter(|| {
                    let checkout = pool.checkout().expect("checkout");
                    let connection = checkout.connection_handle().expect("connection handle");
                    let connection = connection.lock().expect("connection lock");
                    let mut statement = connection
                        .prepare("SELECT value FROM rows LIMIT ?1")
                        .expect("prepare SELECT");
                    let mut rows = statement.query([row_count]).expect("execute SELECT");
                    let mut buffered = Vec::with_capacity(row_count as usize);
                    while let Some(row) = rows.next().expect("read row") {
                        buffered.push(row.get::<_, i64>(0).expect("read value"));
                    }
                    black_box(buffered);
                });
            },
        );
    }
    group.bench_function("named_bind_parse_execute", |b| {
        b.iter(|| {
            let checkout = pool.checkout().expect("checkout");
            let connection = checkout.connection_handle().expect("connection handle");
            let connection = connection.lock().expect("connection lock");
            let value: i64 = connection
                .query_row(
                    "SELECT :value",
                    named_params![":value": black_box(42_i64)],
                    |row| row.get(0),
                )
                .expect("execute named-bind query");
            black_box(value);
        });
    });
    group.bench_function("transaction_begin_commit", |b| {
        b.iter(|| {
            let checkout = pool.checkout().expect("checkout");
            let connection = checkout.connection_handle().expect("connection handle");
            let connection = connection.lock().expect("connection lock");
            connection.execute_batch("BEGIN").expect("begin");
            connection.execute_batch("COMMIT").expect("commit");
        });
    });
    group.finish();

    drop(pool);
    let _ = std::fs::remove_file(path);
}

fn benchmark_statement_cache(c: &mut Criterion) {
    let connection = rusqlite::Connection::open_in_memory().expect("open memory connection");
    connection
        .execute_batch("CREATE TABLE t (v INTEGER NOT NULL); INSERT INTO t VALUES (7);")
        .expect("seed");

    let mut group = c.benchmark_group("statement_cache");
    group.bench_function("prepare_each_execute", |b| {
        b.iter(|| {
            let mut statement = connection
                .prepare("SELECT v FROM t WHERE v > ?")
                .expect("prepare");
            let value: i64 = statement
                .query_row([black_box(1i64)], |row| row.get(0))
                .expect("query");
            black_box(value);
        });
    });
    group.bench_function("prepare_cached_execute", |b| {
        b.iter(|| {
            let mut statement = connection
                .prepare_cached("SELECT v FROM t WHERE v > ?")
                .expect("prepare_cached");
            let value: i64 = statement
                .query_row([black_box(1i64)], |row| row.get(0))
                .expect("query");
            black_box(value);
        });
    });
    group.finish();
}

criterion_group!(
    benches,
    benchmark_pool_checkout,
    benchmark_sqlite_core,
    benchmark_statement_cache
);
criterion_main!(benches);

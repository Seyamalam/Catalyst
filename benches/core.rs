//! Rust-only Criterion benchmarks for the synchronous SQLite core.
//!
//! Coverage: the bounded pool (single-threaded and contended checkout), the
//! dialect compiler over hand-built ASTs (the GIL-bound Python-object parse
//! in `compile_expression` is excluded — it cannot run without Python
//! objects), the MySQL/PostgreSQL named-bind scanners, the MySQL executemany
//! `VALUES`-collapse splitter, and the PostgreSQL NUMERIC binary codec.
//! `NativeConnection.execute_flush` is benchmarked end-to-end from Python
//! instead (`benches/bench_orm.py`): its ops list crosses the FFI boundary as
//! `PyList`, so there is no GIL-free entry point to time here.

#[allow(dead_code, unused_imports)]
// This includes pool.rs verbatim: it must stay self-contained (rusqlite +
// std only, no crate-internal references) or the bench compilation will
// diverge from the lib. Split into a shared internal crate if it grows deps.
#[path = "../src/pool.rs"]
mod pool;

use _catalyst::bench::{
    ColumnDdl, Compiler, Dialect, Expr, JoinSpec, Statement, decode_numeric, encode_numeric,
    mysql_scan_named_binds, postgres_scan_named_binds, split_insert_values,
};
use criterion::{BenchmarkId, Criterion, black_box, criterion_group, criterion_main};
use pool::Pool;
use rusqlite::named_params;
use rusqlite::types::Value;
use std::time::{Duration, Instant};

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

// ---------------------------------------------------------------------------
// Dialect compiler benches: AST -> SQL + bind list, no Python objects.
// ---------------------------------------------------------------------------

fn column(table: &str, name: &str, tag: &str) -> Expr {
    Expr::Column(table.to_owned(), name.to_owned(), Some(tag.to_owned()))
}

fn literal_int(value: i64, name: &str) -> Expr {
    Expr::Literal(
        Value::Integer(value),
        name.to_owned(),
        Some("integer".to_owned()),
    )
}

fn literal_text(value: &str, name: &str) -> Expr {
    Expr::Literal(
        Value::Text(value.to_owned()),
        name.to_owned(),
        Some("string".to_owned()),
    )
}

/// A selectinload/eager-load shaped SELECT: entity columns, one aliased LEFT
/// OUTER JOIN and one plain JOIN, an expanding `IN` predicate nested in an
/// AND group with BETWEEN and LIKE, ORDER BY, and explicit LIMIT/OFFSET binds.
fn select_join_statement(in_size: usize) -> Statement {
    let members = (0..in_size as i64)
        .map(|value| literal_int(value, "parent_id"))
        .collect();
    Statement::Select {
        columns: vec![
            column("parent", "id", "integer"),
            column("parent", "name", "string"),
            Expr::Function("count".to_owned(), vec![column("child_1", "id", "integer")]),
        ],
        table: Some("parent".to_owned()),
        predicate: Some(Box::new(Expr::Group(
            "AND".to_owned(),
            vec![
                Expr::In(Box::new(column("parent", "id", "integer")), members),
                Expr::Between(
                    Box::new(column("parent", "score", "float")),
                    Box::new(Expr::Literal(
                        Value::Real(0.0),
                        "score_1".to_owned(),
                        Some("float".to_owned()),
                    )),
                    Box::new(Expr::Literal(
                        Value::Real(10.0),
                        "score_2".to_owned(),
                        Some("float".to_owned()),
                    )),
                ),
                Expr::Binary(
                    Box::new(column("parent", "name", "string")),
                    "LIKE".to_owned(),
                    Box::new(literal_text("a%", "name_1")),
                ),
            ],
        ))),
        order: vec![Expr::Order(
            Box::new(column("parent", "id", "integer")),
            false,
        )],
        limit: Some(Box::new(Expr::Literal(
            Value::Integer(50),
            "=param_1".to_owned(),
            None,
        ))),
        offset: Some(Box::new(Expr::Literal(
            Value::Integer(0),
            "=param_2".to_owned(),
            None,
        ))),
        joins: vec![
            JoinSpec::LeftOuter {
                table: "child".to_owned(),
                alias: "child_1".to_owned(),
                on: Expr::Binary(
                    Box::new(column("parent", "id", "integer")),
                    "=".to_owned(),
                    Box::new(column("child_1", "parent_id", "integer")),
                ),
            },
            JoinSpec::Inner {
                table: "assoc".to_owned(),
                alias: None,
                on: Expr::Binary(
                    Box::new(column("assoc", "parent_id", "integer")),
                    "=".to_owned(),
                    Box::new(column("parent", "id", "integer")),
                ),
            },
        ],
    }
}

/// ORM-flush shaped INSERT: one `"=column"`-keyed literal per value.
fn insert_statement() -> Statement {
    Statement::Insert {
        table: "child".to_owned(),
        values: vec![
            (
                "id".to_owned(),
                Expr::Literal(Value::Integer(1), "=id".to_owned(), None),
            ),
            (
                "name".to_owned(),
                Expr::Literal(
                    Value::Text("ada".to_owned()),
                    "=name".to_owned(),
                    Some("string".to_owned()),
                ),
            ),
            (
                "score".to_owned(),
                Expr::Literal(Value::Real(9.5), "=score".to_owned(), None),
            ),
            (
                "active".to_owned(),
                Expr::Literal(
                    Value::Integer(1),
                    "=active".to_owned(),
                    Some("boolean".to_owned()),
                ),
            ),
            (
                "parent_id".to_owned(),
                Expr::Literal(Value::Integer(7), "=parent_id".to_owned(), None),
            ),
            (
                "created".to_owned(),
                Expr::Literal(
                    Value::Text("2024-01-01 12:30:45.000000".to_owned()),
                    "=created".to_owned(),
                    Some("datetime".to_owned()),
                ),
            ),
        ],
    }
}

/// ORM-flush shaped UPDATE: SET assignments plus a PK `"=id_1"` predicate.
fn update_statement() -> Statement {
    Statement::Update {
        table: "child".to_owned(),
        values: vec![
            (
                "name".to_owned(),
                Expr::Literal(
                    Value::Text("grace".to_owned()),
                    "=name".to_owned(),
                    Some("string".to_owned()),
                ),
            ),
            (
                "score".to_owned(),
                Expr::Literal(Value::Real(8.25), "=score".to_owned(), None),
            ),
        ],
        predicate: Some(Expr::Binary(
            Box::new(column("child", "id", "integer")),
            "=".to_owned(),
            Box::new(Expr::Literal(
                Value::Integer(1),
                "=id_1".to_owned(),
                Some("integer".to_owned()),
            )),
        )),
    }
}

/// Bulk-delete shaped DELETE with an expanding `IN` predicate.
fn delete_statement(in_size: usize) -> Statement {
    Statement::Delete {
        table: "child".to_owned(),
        predicate: Some(Expr::In(
            Box::new(column("child", "id", "integer")),
            (0..in_size as i64)
                .map(|value| literal_int(value, "id"))
                .collect(),
        )),
    }
}

/// `create_all` shaped DDL: six columns, a PRIMARY KEY, a UNIQUE column, and
/// three table-level FOREIGN KEY clauses.
fn create_table_statement() -> Statement {
    let ddl = |name: &str, type_name: &str, not_null: bool, unique: bool| ColumnDdl {
        name: name.to_owned(),
        type_name: type_name.to_owned(),
        not_null,
        unique,
        default: None,
    };
    Statement::CreateTable {
        table: "bench_child".to_owned(),
        columns: vec![
            ddl("id", "INTEGER", false, false),
            ddl("name", "VARCHAR(30)", true, true),
            ddl("score", "FLOAT", true, false),
            ddl("payload", "BLOB", false, false),
            ddl("amount", "NUMERIC(10, 2)", true, false),
            ddl("parent_id", "INTEGER", true, false),
        ],
        primary_key: vec!["id".to_owned()],
        foreign_keys: vec![
            (
                "parent_id".to_owned(),
                "bench_parent".to_owned(),
                "id".to_owned(),
            ),
            (
                "name".to_owned(),
                "bench_labels".to_owned(),
                "name".to_owned(),
            ),
            (
                "id".to_owned(),
                "bench_audit".to_owned(),
                "row_id".to_owned(),
            ),
        ],
    }
}

fn benchmark_dialect_compile(c: &mut Criterion) {
    let dialects = [
        ("sqlite", Dialect::Sqlite),
        ("postgres", Dialect::Postgres),
        ("mysql", Dialect::Mysql),
    ];
    let mut group = c.benchmark_group("compile");
    for (name, dialect) in dialects {
        for in_size in [10_usize, 100] {
            let statement = select_join_statement(in_size);
            group.bench_with_input(
                BenchmarkId::new(format!("select_join_in/{name}"), in_size),
                &in_size,
                |b, _| {
                    b.iter(|| black_box(Compiler::new(dialect).statement(black_box(&statement))));
                },
            );
        }
        for (label, statement) in [
            ("insert", insert_statement()),
            ("update", update_statement()),
            ("delete_in", delete_statement(25)),
            ("create_table_fk", create_table_statement()),
        ] {
            group.bench_function(BenchmarkId::new(label, name), |b| {
                b.iter(|| black_box(Compiler::new(dialect).statement(black_box(&statement))));
            });
        }
    }
    group.finish();
}

// ---------------------------------------------------------------------------
// Pool checkout under multi-threaded contention (Pool is Send + Sync).
// ---------------------------------------------------------------------------

fn benchmark_pool_contention(c: &mut Criterion) {
    let path = std::env::temp_dir().join(format!("catalyst-contend-{}.sqlite", std::process::id()));
    let pool = Pool::new_with_options(
        path.to_str().expect("UTF-8 temp path"),
        5,
        0,
        Duration::from_secs(30),
        Duration::from_secs(5),
    )
    .expect("create benchmark pool");

    let mut group = c.benchmark_group("pool");
    for threads in [1_usize, 4, 8] {
        group.bench_function(BenchmarkId::new("contended_checkout", threads), |b| {
            b.iter_custom(|iters| {
                // Each worker performs `iters` checkout/drop cycles; the
                // reported per-iter time is the contended checkout latency a
                // single worker observes while racing `threads - 1` peers.
                let started = Instant::now();
                std::thread::scope(|scope| {
                    for _ in 0..threads {
                        scope.spawn(|| {
                            for _ in 0..iters {
                                let connection = pool.checkout().expect("checkout");
                                black_box(&connection);
                                drop(connection);
                            }
                        });
                    }
                });
                started.elapsed()
            });
        });
    }
    group.finish();
    drop(pool);
    let _ = std::fs::remove_file(path);
}

// ---------------------------------------------------------------------------
// Named-bind scanners and the MySQL executemany VALUES-collapse splitter.
// ---------------------------------------------------------------------------

/// ORM-ish SELECT text with `count` named binds plus quote/comment/cast traps.
fn scan_sample_sql(dialect: &str, binds: usize) -> String {
    let mut sql = String::from(
        "SELECT parent.id, parent.name, ':not_a_bind' AS trap \
         /* :comment_bind */ FROM parent -- :line_bind\n \
         WHERE parent.id IN (",
    );
    for index in 0..binds {
        if index > 0 {
            sql.push_str(", ");
        }
        sql.push_str(&format!(":id_{index}"));
    }
    sql.push_str(") AND parent.name LIKE :name_pattern");
    if dialect == "postgres" {
        sql.push_str(" AND parent.name::text <> $$dollar :bind$$ AND parent.id::int8 > :floor");
    } else {
        sql.push_str(" AND `parent`.`name` <> \"ANSI :bind\" # :tail_bind\nAND parent.id > :floor");
    }
    sql
}

fn benchmark_bind_scanners(c: &mut Criterion) {
    let mut group = c.benchmark_group("named_binds");
    for binds in [10_usize, 100] {
        let sql = scan_sample_sql("postgres", binds);
        group.bench_with_input(BenchmarkId::new("postgres", binds), &binds, |b, _| {
            b.iter(|| black_box(postgres_scan_named_binds(black_box(&sql)).expect("scan")));
        });
        let sql = scan_sample_sql("mysql", binds);
        group.bench_with_input(BenchmarkId::new("mysql", binds), &binds, |b, _| {
            b.iter(|| black_box(mysql_scan_named_binds(black_box(&sql)).expect("scan")));
        });
    }
    group.finish();

    let mut group = c.benchmark_group("mysql_executemany_collapse");
    for columns in [10_usize, 50] {
        let placeholders = vec!["?"; columns].join(", ");
        let sql = format!(
            "INSERT INTO batch_rows ({}) VALUES ({placeholders}) \
             ON DUPLICATE KEY UPDATE col_0 = VALUES(col_0)",
            (0..columns)
                .map(|index| format!("col_{index}"))
                .collect::<Vec<_>>()
                .join(", "),
        );
        group.bench_with_input(
            BenchmarkId::new("split_insert_values", columns),
            &columns,
            |b, _| {
                b.iter(|| black_box(split_insert_values(black_box(&sql))));
            },
        );
    }
    group.finish();
}

// ---------------------------------------------------------------------------
// PostgreSQL NUMERIC binary codec.
// ---------------------------------------------------------------------------

fn benchmark_numeric_codec(c: &mut Criterion) {
    let cases: &[(&str, &str)] = &[
        ("two_places", "12345.67"),
        ("scale_38", "1234567890123456789012345678.9012345678"),
        ("exponent", "-1.23E-7"),
        ("integer", "1000000"),
    ];
    let mut group = c.benchmark_group("numeric_codec");
    for (label, text) in cases {
        group.bench_function(BenchmarkId::new("encode", label), |b| {
            b.iter(|| black_box(encode_numeric(black_box(text)).expect("encode")));
        });
        let raw = encode_numeric(text).expect("encode for decode input");
        group.bench_function(BenchmarkId::new("decode", label), |b| {
            b.iter(|| black_box(decode_numeric(black_box(&raw)).expect("decode")));
        });
    }
    group.finish();
}

criterion_group!(
    benches,
    benchmark_pool_checkout,
    benchmark_sqlite_core,
    benchmark_statement_cache,
    benchmark_dialect_compile,
    benchmark_pool_contention,
    benchmark_bind_scanners,
    benchmark_numeric_codec
);
criterion_main!(benches);

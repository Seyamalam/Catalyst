# Benchmarks

Measured performance of Catalyst's Rust internals (Criterion) and of the
end-to-end ORM path against SQLAlchemy 2.1.8. Numbers below come from real
runs, not estimates; re-run them with the commands in each section.

## Environment

- Hardware/OS: macOS 26.6.2 (Darwin 25.6.0), Apple M5 Pro, `arm64`
- Toolchain: rustc 1.96.0, criterion 0.5, Python 3.13.13, SQLAlchemy 2.1.8
- Build: `cargo bench` (release, `--bench core`); `maturin develop --release`

## Rust microbenchmarks — `benches/core.rs`

```bash
cargo bench --bench core            # full criterion run (results below)
scripts/bench.sh -- --quick         # smoke pass
```

The suite times only paths that run without a Python interpreter:

- `pool/checkout_return` — the existing single-threaded checkout/return.
- `pool/contended_checkout` — `Pool` is `Send + Sync`, so 1/4/8 scoped
  threads race a `pool_size=5, max_overflow=0` pool; the reported time is
  the per-checkout latency a worker sees while contending with its peers
  (checkout timeout 30s so waits never exhaust).
- `sqlite/*` — the existing buffered SELECT, named-bind, and transaction
  round-trips over the bundled rusqlite build.
- `statement_cache/*` — the existing `prepare` vs `prepare_cached` pair.
- `compile/*` — the dialect compiler (`Compiler::statement`) over
  hand-built ASTs matching what Python serializes: an eager-load-shaped
  SELECT (entity columns, `LEFT OUTER JOIN` + `JOIN`, expanding `IN`,
  `BETWEEN`, `LIKE`, `ORDER BY`, `LIMIT`/`OFFSET`) at `IN` sizes 10/100,
  an ORM-flush-shaped INSERT/UPDATE, a bulk `DELETE ... IN (25)`, and a
  `CREATE TABLE` with `PRIMARY KEY`, `UNIQUE`, and three `FOREIGN KEY`
  clauses — each on the SQLite, PostgreSQL, and MySQL dialects.
- `named_binds/*` — the quote/comment/cast-aware named-bind scanners
  (`:name` → `$N` for PostgreSQL, `:name` → `?` for MySQL) on SQL with 10
  and 100 binds plus string/comment traps.
- `mysql_executemany_collapse/*` — `split_insert_values`, the scanner that
  decides whether an `INSERT ... VALUES (?,...)` batch collapses into one
  multi-row statement (asyncmy parity), at 10 and 50 columns.
- `numeric_codec/*` — the PostgreSQL binary NUMERIC `encode_numeric`/
  `decode_numeric` pair over four value shapes.

Not covered here (and why): `NativeConnection.execute_flush` takes a
`PyList` of serialized ops, and `NativeResult.hydrate` needs Python
objects — both are GIL-bound by design, so they are measured end-to-end in
`benches/bench_orm.py` instead. The same applies to the Python-object
`parse_statement` half of `compile_expression`; the `compile/*` numbers
measure the pure AST→SQL render only.

### Results (criterion means)

| benchmark | parameter | time |
|---|---|---|
| `pool/checkout_return` | — | 31.9 ns |
| `pool/contended_checkout` | 1 thread | 32.1 ns |
| `pool/contended_checkout` | 4 threads | 498.2 ns |
| `pool/contended_checkout` | 8 threads | 1.97 µs |
| `sqlite/buffered_select_rows` | 10 / 100 / 1000 rows | 4.32 / 6.56 / 24.0 µs |
| `sqlite/named_bind_parse_execute` | — | 369.5 ns |
| `sqlite/transaction_begin_commit` | — | 240.7 ns |
| `statement_cache/prepare_each_execute` | — | 599.1 ns |
| `statement_cache/prepare_cached_execute` | — | 140.4 ns (4.3×) |
| `compile/select_join_in` (IN 10 → 100) | sqlite | 4.87 → 9.77 µs |
| `compile/select_join_in` (IN 10 → 100) | postgres | 4.28 → 10.6 µs |
| `compile/select_join_in` (IN 10 → 100) | mysql | 5.98 → 10.6 µs |
| `compile/insert` | sqlite / postgres / mysql | 1.23 / 1.20 / 1.61 µs |
| `compile/update` | sqlite / postgres / mysql | 0.82 / 0.81 / 1.08 µs |
| `compile/delete_in` (IN 25) | sqlite / postgres / mysql | 2.04 / 2.36 / 2.14 µs |
| `compile/create_table_fk` | sqlite / postgres / mysql | 2.67 / 2.11 / 3.45 µs |
| `named_binds` (10 → 100 binds) | postgres | 1.41 → 8.88 µs |
| `named_binds` (10 → 100 binds) | mysql | 1.14 → 7.15 µs |
| `mysql_executemany_collapse` | 10 / 50 columns | 0.30 / 0.93 µs |
| `numeric_codec/encode` | two_places / scale_38 / exponent / integer | 82.9 / 162.7 / 79.9 / 80.0 ns |
| `numeric_codec/decode` | two_places / scale_38 / exponent / integer | 143.9 / 408.4 / 140.8 / 64.2 ns |

## End-to-end ORM — `benches/bench_orm.py`

```bash
maturin develop --release   # build the extension into the venv first
python benches/bench_orm.py # median of 7 runs after 2 warm-ups
```

Plain-`time.perf_counter` harness; no benchmark dependencies. Catalyst runs
`create_engine(":memory:")`, SQLAlchemy runs
`create_engine("sqlite:///:memory:")` — both resolve to a single shared
sqlite3-grade connection. Each case performs identical work on both sides:
same model (`bench_user` with int/str/float/bool/nullable/`date`/`bytes`
columns), same rows, same statements, sessions rebuilt per repetition.
`session_get_identity` is the exception — it warms 100 ids, then times
2000 `session.get` calls that all hit the identity map.

| benchmark | size | Catalyst | SQLAlchemy 2.1.8 | speedup |
|---|---|---|---|---|
| `insert_add_all_flush` | 1,000 rows | 37.4 ms | 7.5 ms | **0.20×** |
| `insert_add_all_flush` | 10,000 rows | 372 ms | 83.6 ms | **0.22×** |
| `select_hydrate` | 10,000 rows | 25.6 ms | 17.9 ms | 0.70× |
| `executemany_insert` | 10,000 rows | 19.3 ms | 18.0 ms | 0.93× |
| `selectinload` | 100 parents × 10 children | 2.47 ms | 2.32 ms | 0.94× |
| `session_get_identity` | 2,000 calls / 100 ids | 308 µs | 63.5 ms | **206×** |

## Interpretation

- **`session.get` identity-map hits — big win (≈200×).** Catalyst resolves a
  hit as one `NativeUow.identity_get` hashmap lookup in Rust
  (~0.15 µs/call). SQLAlchemy performs per-call Python bookkeeping
  (~32 µs/call) even on hits. This is where Rust ownership of the identity
  map pays off most clearly.
- **`INSERT` via `add_all`+`flush` — real loss (≈0.2×), and measured.** The
  batched `execute_flush` crossing works — all N INSERTs execute in one FFI
  call inside one wire transaction — but the flush first compiles
  `Insert(table).values(row).compile()` *per object*. Each compile pays the
  Python→tuple serialization plus a `compile_expression` FFI call that
  spawns a dedicated 64 MiB-stack thread (~29 µs/op measured). SQLAlchemy
  reuses a cached per-table INSERT and only re-binds parameters. The
  bottleneck is per-object compilation, not the FFI crossing: the fix is
  statement-template caching in the flush path (compile once per table,
  rebind per row), which is a known follow-up, not a benchmark artifact.
- **`SELECT`+hydrate — modest loss (≈0.7×).** Batched Rust hydration
  (`__new__` + per-column setattr in one crossing) lands in the same
  ballpark as SQLAlchemy's mature hydration path but doesn't beat it;
  identity-map reconciliation and the Python-side result wrappers cost the
  difference.
- **`executemany` and `selectinload` — comparable (≈0.93–0.94×).** Both
  sides ultimately funnel to sqlite executemany / two-SELECT loads;
  Catalyst's one-crossing batch shows no penalty here.
- **Rust internals.** Dialect compilation costs 0.8–10.6 µs per statement
  (PostgreSQL's `$N` bind numbering is slightly cheaper than `?`-style on
  the complex SELECT; MySQL's identifier quoting scan makes it the
  priciest). Named-bind scanning is 1.1–8.9 µs at 10–100 binds. The NUMERIC
  codec is 64–408 ns. Pool checkout is 32 ns uncontended and ≈2 µs per
  worker under an 8-thread race on 5 slots — the condvar wake path keeps
  starvation bounded. The statement cache retains its ≈4.3× advantage.

### Gap noticed while benchmarking

`session.flush()` currently raises `StatementError` on `DateTime` columns:
flush ops carry compile-time-rendered datetime text, and the second bind
pass inside `execute_flush` re-validates it via `is_formatted_datetime`,
which rejects the `-` separators at positions 4/7 that its own renderer
emits (the sibling `is_formatted_date` skips those positions correctly).
The e2e model therefore uses a `Date` column. Reported for a follow-up
fix; no flush-parity test previously exercised a `datetime` column.

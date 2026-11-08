//! Small synchronous SQLite connection pool used by native engines.

use rusqlite::Connection;
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

pub(crate) const DEFAULT_POOL_SIZE: usize = 5;

struct PoolState {
    idle: Vec<PoolSlot>,
    memory_slot: Option<PoolSlot>,
    total: usize,
    closed: bool,
}

struct PoolInner {
    path: String,
    pool_size: usize,
    max_overflow: usize,
    timeout: Duration,
    sqlite_timeout: Duration,
    state: Mutex<PoolState>,
    available: Condvar,
}

/// Transaction bookkeeping belongs to the physical pool slot, not a Python checkout.
#[derive(Default)]
pub(crate) struct TransactionState {
    pub(crate) active: bool,
    pub(crate) savepoints: Vec<String>,
    pub(crate) next_savepoint: u64,
}

struct PoolSlot {
    connection: Arc<Mutex<Connection>>,
    transaction: Arc<Mutex<TransactionState>>,
}

/// A bounded SQLite connection pool. In-memory engines intentionally use one connection.
#[derive(Clone)]
pub(crate) struct Pool {
    inner: Arc<PoolInner>,
}

/// An exclusive checkout. Dropping it returns the connection to its pool.
pub(crate) struct PooledConnection {
    slot: Option<PoolSlot>,
    pool: Arc<PoolInner>,
    shared: bool,
}

#[derive(Debug)]
pub(crate) enum PoolError {
    Exhausted,
    Closed,
    Sqlite(rusqlite::Error),
    Poisoned,
}

impl Pool {
    #[cfg(test)]
    pub(crate) fn new(path: &str) -> Result<Self, PoolError> {
        Self::new_with_options(
            path,
            DEFAULT_POOL_SIZE,
            10,
            Duration::from_secs(30),
            Duration::from_secs(5),
        )
    }

    pub(crate) fn new_with_options(
        path: &str,
        pool_size: usize,
        max_overflow: usize,
        timeout: Duration,
        sqlite_timeout: Duration,
    ) -> Result<Self, PoolError> {
        let memory_slot = if path == ":memory:" {
            Some(PoolSlot {
                connection: Arc::new(Mutex::new(
                    Connection::open_in_memory().map_err(PoolError::Sqlite)?,
                )),
                transaction: Arc::new(Mutex::new(TransactionState::default())),
            })
        } else {
            None
        };
        let pool_size = if memory_slot.is_some() { 1 } else { pool_size };
        let max_overflow = if memory_slot.is_some() {
            0
        } else {
            max_overflow
        };
        Ok(Self {
            inner: Arc::new(PoolInner {
                path: path.to_owned(),
                pool_size,
                max_overflow,
                timeout,
                sqlite_timeout,
                available: Condvar::new(),
                state: Mutex::new(PoolState {
                    idle: Vec::new(),
                    memory_slot,
                    total: 0,
                    closed: false,
                }),
            }),
        })
    }

    pub(crate) fn checkout(&self) -> Result<PooledConnection, PoolError> {
        let started = Instant::now();
        let mut state = self.inner.state.lock().map_err(|_| PoolError::Poisoned)?;
        loop {
            if state.closed {
                return Err(PoolError::Closed);
            }
            if let Some(slot) = &state.memory_slot {
                return Ok(PooledConnection {
                    slot: Some(PoolSlot {
                        connection: Arc::clone(&slot.connection),
                        transaction: Arc::clone(&slot.transaction),
                    }),
                    pool: Arc::clone(&self.inner),
                    shared: true,
                });
            }
            if let Some(slot) = state.idle.pop() {
                return Ok(PooledConnection {
                    slot: Some(slot),
                    pool: Arc::clone(&self.inner),
                    shared: false,
                });
            }
            if self.inner.pool_size == 0
                || state.total < self.inner.pool_size.saturating_add(self.inner.max_overflow)
            {
                // Reserve before releasing the mutex; connection opens run outside it.
                state.total += 1;
                break;
            }
            let remaining = self.inner.timeout.saturating_sub(started.elapsed());
            if remaining.is_zero() {
                return Err(PoolError::Exhausted);
            }
            let (next, result) = self
                .inner
                .available
                .wait_timeout(state, remaining)
                .map_err(|_| PoolError::Poisoned)?;
            state = next;
            if result.timed_out() && state.idle.is_empty() {
                return Err(PoolError::Exhausted);
            }
        }
        drop(state);

        let opened = (|| {
            let connection = Connection::open(&self.inner.path).map_err(PoolError::Sqlite)?;
            connection
                .busy_timeout(self.inner.sqlite_timeout)
                .map_err(PoolError::Sqlite)?;
            // Connection-scoped prepared-statement cache, per ADR 0003.
            connection.set_prepared_statement_cache_capacity(128);
            Ok(connection)
        })();
        match opened {
            Ok(connection) => Ok(PooledConnection {
                slot: Some(PoolSlot {
                    connection: Arc::new(Mutex::new(connection)),
                    transaction: Arc::new(Mutex::new(TransactionState::default())),
                }),
                pool: Arc::clone(&self.inner),
                shared: false,
            }),
            Err(error) => {
                if let Ok(mut state) = self.inner.state.lock() {
                    state.total = state.total.saturating_sub(1);
                }
                self.inner.available.notify_one();
                Err(error)
            }
        }
    }

    pub(crate) fn close(&self) -> Result<(), PoolError> {
        let mut state = self.inner.state.lock().map_err(|_| PoolError::Poisoned)?;
        state.closed = true;
        state.total = state.total.saturating_sub(state.idle.len());
        state.idle.clear();
        state.memory_slot.take();
        self.inner.available.notify_all();
        Ok(())
    }
}

impl PooledConnection {
    pub(crate) fn connection_handle(&self) -> Result<Arc<Mutex<Connection>>, PoolError> {
        self.slot
            .as_ref()
            .map(|slot| Arc::clone(&slot.connection))
            .ok_or(PoolError::Closed)
    }

    pub(crate) fn transaction_handle(&self) -> Result<Arc<Mutex<TransactionState>>, PoolError> {
        self.slot
            .as_ref()
            .map(|slot| Arc::clone(&slot.transaction))
            .ok_or(PoolError::Closed)
    }
}

impl Drop for PooledConnection {
    fn drop(&mut self) {
        let Some(slot) = self.slot.take() else {
            return;
        };
        if self.shared {
            // The in-memory singleton is shared by every holder; a single
            // holder returning must not roll back transactions other holders
            // opened (SQLAlchemy's SingletonThreadPool behaves the same).
            return;
        }
        // Reset-on-return: roll back any transaction the previous holder left
        // open so no dirty state survives the checkout boundary. Acquire locks
        // in the same transaction-then-connection order as native operations.
        if let Ok(mut transaction) = slot.transaction.lock() {
            if let Ok(guard) = slot.connection.lock()
                && !guard.is_autocommit()
            {
                let _ = guard.execute_batch("ROLLBACK");
            }
            transaction.active = false;
            transaction.savepoints.clear();
        }
        let connection = slot.connection;
        let Ok(mut state) = self.pool.state.lock() else {
            return;
        };
        if state.closed || state.total > self.pool.pool_size {
            state.total = state.total.saturating_sub(1);
        } else {
            state.idle.push(PoolSlot {
                connection,
                transaction: slot.transaction,
            });
        }
        self.pool.available.notify_one();
    }
}

#[cfg(test)]
mod tests {
    use super::{Pool, PoolError};
    use std::sync::Arc;
    use std::thread;
    use std::time::{Duration, Instant};

    #[test]
    fn checkout_and_return_reuses_connection_and_state() {
        let path =
            std::env::temp_dir().join(format!("catalyst-reuse-{}.sqlite", std::process::id()));
        let pool = Pool::new(path.to_str().expect("UTF-8 temp path")).expect("pool");
        let first = pool.checkout().expect("initial checkout");
        first
            .connection_handle()
            .expect("connection")
            .lock()
            .expect("connection lock")
            .execute_batch("CREATE TABLE items (value INTEGER); INSERT INTO items VALUES (42);")
            .expect("create and insert");
        drop(first);

        // A TEMP table is per-connection, so surviving this checkout boundary
        // proves physical reuse rather than mere file durability.
        let reused = pool.checkout().expect("checkout for temp table");
        reused
            .connection_handle()
            .expect("connection")
            .lock()
            .expect("connection lock")
            .execute_batch(
                "CREATE TEMP TABLE marker (value INTEGER); INSERT INTO marker VALUES (7)",
            )
            .expect("create temp table");
        // Leave an open transaction on the slot; reset-on-return must roll the
        // wire back and clear the bookkeeping so the next checkout is clean.
        reused
            .connection_handle()
            .expect("connection")
            .lock()
            .expect("connection lock")
            .execute_batch("BEGIN")
            .expect("begin transaction");
        drop(reused);

        let second = pool.checkout().expect("checkout after return");
        second
            .connection_handle()
            .expect("connection")
            .lock()
            .expect("connection lock")
            .execute_batch("BEGIN")
            .expect("wire was reset on return; a fresh BEGIN must succeed");
        let value: i64 = second
            .connection_handle()
            .expect("connection")
            .lock()
            .expect("connection lock")
            .query_row("SELECT value FROM items", [], |row| row.get(0))
            .expect("persisted file state");
        assert_eq!(value, 42);
        let marker: i64 = second
            .connection_handle()
            .expect("connection")
            .lock()
            .expect("connection lock")
            .query_row("SELECT COUNT(*) FROM marker", [], |row| row.get(0))
            .expect("temp table survives physical connection reuse");
        assert_eq!(marker, 1);
        drop(second);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn checkout_is_exclusive_and_exhaustion_is_reported() {
        let path =
            std::env::temp_dir().join(format!("catalyst-exhaust-{}.sqlite", std::process::id()));
        let pool = Pool::new_with_options(
            path.to_str().expect("UTF-8 temp path"),
            5,
            0,
            Duration::ZERO,
            Duration::from_secs(5),
        )
        .expect("pool");
        let held: Vec<_> = (0..5)
            .map(|_| pool.checkout().expect("pool slot"))
            .collect();
        assert!(matches!(pool.checkout(), Err(PoolError::Exhausted)));
        drop(held);
        assert!(pool.checkout().is_ok());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn overflow_connections_are_created_beyond_pool_size() {
        let path =
            std::env::temp_dir().join(format!("catalyst-overflow-{}.sqlite", std::process::id()));
        let pool = Pool::new_with_options(
            path.to_str().expect("UTF-8 temp path"),
            1,
            2,
            Duration::ZERO,
            Duration::from_secs(5),
        )
        .expect("pool");
        let held: Vec<_> = (0..3)
            .map(|_| pool.checkout().expect("overflow slot"))
            .collect();
        assert!(matches!(pool.checkout(), Err(PoolError::Exhausted)));
        drop(held);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn timed_wait_wakes_when_a_connection_is_returned() {
        let path =
            std::env::temp_dir().join(format!("catalyst-wait-{}.sqlite", std::process::id()));
        let pool = Pool::new_with_options(
            path.to_str().expect("UTF-8 temp path"),
            1,
            0,
            Duration::from_secs(1),
            Duration::from_secs(5),
        )
        .expect("pool");
        let held = pool.checkout().expect("initial slot");
        let waiter_pool = Pool {
            inner: Arc::clone(&pool.inner),
        };
        let waiter = thread::spawn(move || waiter_pool.checkout());
        thread::sleep(Duration::from_millis(30));
        drop(held);
        assert!(waiter.join().expect("waiter thread").is_ok());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn timed_wait_reports_exhaustion_after_timeout() {
        let path =
            std::env::temp_dir().join(format!("catalyst-timeout-{}.sqlite", std::process::id()));
        let pool = Pool::new_with_options(
            path.to_str().expect("UTF-8 temp path"),
            1,
            0,
            Duration::from_millis(40),
            Duration::from_secs(5),
        )
        .expect("pool");
        let _held = pool.checkout().expect("initial slot");
        let started = Instant::now();
        assert!(matches!(pool.checkout(), Err(PoolError::Exhausted)));
        assert!(started.elapsed() >= Duration::from_millis(30));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn file_pool_allows_multiple_connections() {
        let path =
            std::env::temp_dir().join(format!("catalyst-pool-{}.sqlite", std::process::id()));
        let pool = Pool::new(path.to_str().expect("UTF-8 temp path")).expect("pool");
        let first = pool.checkout().expect("first connection");
        let second = pool.checkout().expect("second connection");
        let third = pool.checkout().expect("third connection");
        drop((first, second, third));
        let _ = std::fs::remove_file(path);
    }
}

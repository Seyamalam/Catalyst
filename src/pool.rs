//! Small synchronous SQLite connection pool used by native engines.

use rusqlite::Connection;
use std::sync::{Arc, Mutex};
use std::time::Duration;

const DEFAULT_MAX_CONNECTIONS: usize = 5;

struct PoolState {
    idle: Vec<Arc<Mutex<Connection>>>,
    memory_connection: Option<Arc<Mutex<Connection>>>,
    total: usize,
    closed: bool,
}

struct PoolInner {
    path: String,
    max_connections: usize,
    state: Mutex<PoolState>,
}

/// A bounded SQLite connection pool. In-memory engines intentionally use one connection.
pub(crate) struct Pool {
    inner: Arc<PoolInner>,
}

/// An exclusive checkout. Dropping it returns the connection to its pool.
pub(crate) struct PooledConnection {
    connection: Option<Arc<Mutex<Connection>>>,
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
    pub(crate) fn new(path: &str) -> Result<Self, PoolError> {
        let memory_connection = if path == ":memory:" {
            Some(Arc::new(Mutex::new(
                Connection::open_in_memory().map_err(PoolError::Sqlite)?,
            )))
        } else {
            None
        };
        let max_connections = if memory_connection.is_some() {
            1
        } else {
            DEFAULT_MAX_CONNECTIONS
        };
        Ok(Self {
            inner: Arc::new(PoolInner {
                path: path.to_owned(),
                max_connections,
                state: Mutex::new(PoolState {
                    idle: Vec::new(),
                    memory_connection,
                    total: 0,
                    closed: false,
                }),
            }),
        })
    }

    pub(crate) fn checkout(&self) -> Result<PooledConnection, PoolError> {
        let mut state = self.inner.state.lock().map_err(|_| PoolError::Poisoned)?;
        if state.closed {
            return Err(PoolError::Closed);
        }
        if let Some(connection) = &state.memory_connection {
            return Ok(PooledConnection {
                connection: Some(Arc::clone(connection)),
                pool: Arc::clone(&self.inner),
                shared: true,
            });
        }
        if let Some(connection) = state.idle.pop() {
            return Ok(PooledConnection {
                connection: Some(connection),
                pool: Arc::clone(&self.inner),
                shared: false,
            });
        }
        if state.total >= self.inner.max_connections {
            return Err(PoolError::Exhausted);
        }
        // Reserve the slot before releasing the pool mutex so the bound holds
        // while the (potentially slow) file open runs outside the lock.
        state.total += 1;
        drop(state);

        let opened = (|| {
            let connection = Connection::open(&self.inner.path).map_err(PoolError::Sqlite)?;
            connection
                .busy_timeout(Duration::from_secs(5))
                .map_err(PoolError::Sqlite)?;
            Ok(connection)
        })();
        match opened {
            Ok(connection) => Ok(PooledConnection {
                connection: Some(Arc::new(Mutex::new(connection))),
                pool: Arc::clone(&self.inner),
                shared: false,
            }),
            Err(error) => {
                if let Ok(mut state) = self.inner.state.lock() {
                    state.total = state.total.saturating_sub(1);
                }
                Err(error)
            }
        }
    }

    pub(crate) fn close(&self) -> Result<(), PoolError> {
        let mut state = self.inner.state.lock().map_err(|_| PoolError::Poisoned)?;
        state.closed = true;
        state.total = state.total.saturating_sub(state.idle.len());
        state.idle.clear();
        state.memory_connection.take();
        Ok(())
    }
}

impl PooledConnection {
    pub(crate) fn connection_handle(&self) -> Result<Arc<Mutex<Connection>>, PoolError> {
        self.connection.as_ref().cloned().ok_or(PoolError::Closed)
    }
}

impl Drop for PooledConnection {
    fn drop(&mut self) {
        let Some(connection) = self.connection.take() else {
            return;
        };
        if self.shared {
            return;
        }
        // Reset-on-return: roll back any transaction the previous holder left
        // open so no dirty state survives the checkout boundary. This must live
        // here (not on the pyobject) because holders may be dropped without
        // calling close(); the pool slot is the single source of truth.
        if let Ok(guard) = connection.lock()
            && !guard.is_autocommit()
        {
            let _ = guard.execute_batch("ROLLBACK");
        }
        let Ok(mut state) = self.pool.state.lock() else {
            return;
        };
        if state.closed {
            state.total = state.total.saturating_sub(1);
        } else {
            state.idle.push(connection);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{Pool, PoolError};

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
        drop(reused);

        let second = pool.checkout().expect("checkout after return");
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
        let pool = Pool::new(path.to_str().expect("UTF-8 temp path")).expect("pool");
        let held: Vec<_> = (0..5)
            .map(|_| pool.checkout().expect("pool slot"))
            .collect();
        assert!(matches!(pool.checkout(), Err(PoolError::Exhausted)));
        drop(held);
        assert!(pool.checkout().is_ok());
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

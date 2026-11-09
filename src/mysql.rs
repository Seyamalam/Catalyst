use std::collections::HashMap;
use std::str::FromStr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use chrono::{Datelike, Timelike};
use mysql_async::consts::ColumnType;
use mysql_async::prelude::Queryable;
use mysql_async::{Conn, Opts, OptsBuilder, Row, SslOpts, Value};
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDate, PyDateTime, PyDict, PyFloat, PyInt, PyList, PyString};

use crate::{IntegrityError, NativeResult, OperationalError, ProgrammingError, StatementError};

type MysqlTypeMetadata = (Vec<Option<String>>, Vec<String>, Vec<Option<String>>);

/// QueuePool-style shared state for a MySQL engine: a bounded slot
/// semaphore, an idle-connection stack, and a closed flag set on dispose.
struct MysqlPoolInner {
    opts: Opts,
    idle: Mutex<Vec<Conn>>,
    capacity: Arc<tokio::sync::Semaphore>,
    checkout_timeout: std::time::Duration,
    pool_size: usize,
    max_overflow: i64,
    closed: AtomicBool,
}

/// Native Tokio MySQL engine with a bounded async connection pool.
#[pyclass(module = "catalyst._catalyst")]
pub(crate) struct NativeMysqlEngine {
    inner: Arc<MysqlPoolInner>,
}

/// One async MySQL connection checked out from the pool.
#[pyclass(module = "catalyst._catalyst")]
pub(crate) struct NativeMysqlConnection {
    connection: Mutex<Option<Conn>>,
    active: AtomicBool,
    pool: Mutex<Option<(Arc<MysqlPoolInner>, tokio::sync::OwnedSemaphorePermit)>>,
}

#[derive(Clone, Debug)]
enum MysqlValue {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
    Timestamp(chrono::NaiveDateTime),
    Date(chrono::NaiveDate),
    Decimal(String),
}

impl From<MysqlValue> for Value {
    fn from(value: MysqlValue) -> Self {
        match value {
            MysqlValue::Null => Value::NULL,
            MysqlValue::Bool(flag) => Value::Int(i64::from(flag)),
            MysqlValue::Int(number) => Value::Int(number),
            MysqlValue::Float(number) => Value::Double(number),
            MysqlValue::Text(text) | MysqlValue::Decimal(text) => Value::Bytes(text.into_bytes()),
            MysqlValue::Bytes(bytes) => Value::Bytes(bytes),
            MysqlValue::Timestamp(value) => Value::Date(
                u16::try_from(value.year()).unwrap_or(0),
                value.month() as u8,
                value.day() as u8,
                value.hour() as u8,
                value.minute() as u8,
                value.second() as u8,
                value.and_utc().timestamp_subsec_micros(),
            ),
            MysqlValue::Date(value) => Value::Date(
                u16::try_from(value.year()).unwrap_or(0),
                value.month() as u8,
                value.day() as u8,
                0,
                0,
                0,
                0,
            ),
        }
    }
}

#[pymethods]
impl NativeMysqlEngine {
    #[new]
    #[pyo3(signature = (url, pool_size=5, max_overflow=10, pool_timeout=30.0))]
    fn new(url: &str, pool_size: usize, max_overflow: i64, pool_timeout: f64) -> PyResult<Self> {
        // `max_overflow` of -1 means unlimited, matching SQLAlchemy QueuePool.
        let permits = if max_overflow < 0 {
            tokio::sync::Semaphore::MAX_PERMITS
        } else {
            pool_size.saturating_add(max_overflow as usize)
        };
        Ok(Self {
            inner: Arc::new(MysqlPoolInner {
                opts: parse_mysql_opts(url)?,
                idle: Mutex::new(Vec::new()),
                capacity: Arc::new(tokio::sync::Semaphore::new(permits)),
                checkout_timeout: std::time::Duration::from_secs_f64(pool_timeout.max(0.0)),
                pool_size,
                max_overflow,
                closed: AtomicBool::new(false),
            }),
        })
    }

    fn async_connect<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        if self.inner.closed.load(Ordering::Acquire) {
            return Err(OperationalError::new_err("Engine is disposed"));
        }
        let inner = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let permit = match tokio::time::timeout(
                inner.checkout_timeout,
                inner.capacity.clone().acquire_owned(),
            )
            .await
            {
                Ok(Ok(permit)) => permit,
                Ok(Err(_)) => {
                    return Err(OperationalError::new_err("MySQL connection pool is closed"));
                }
                Err(_) => {
                    return Err(crate::TimeoutError::new_err(format!(
                        "QueuePool limit of size {} overflow {} reached, connection timed \
                         out, timeout {:.2}",
                        inner.pool_size,
                        inner.max_overflow,
                        inner.checkout_timeout.as_secs_f64(),
                    )));
                }
            };
            let connection = loop {
                let candidate = inner
                    .idle
                    .lock()
                    .map_err(|_| OperationalError::new_err("MySQL pool lock was poisoned"))?
                    .pop();
                match candidate {
                    Some(conn) if conn.is_disconnected() => continue,
                    other => break other,
                }
            };
            let connection = match connection {
                Some(conn) => conn,
                None => Conn::new(inner.opts.clone())
                    .await
                    .map_err(|_| OperationalError::new_err("Could not connect to MySQL"))?,
            };
            Python::attach(|py| {
                Py::new(
                    py,
                    NativeMysqlConnection {
                        connection: Mutex::new(Some(connection)),
                        active: AtomicBool::new(false),
                        pool: Mutex::new(Some((inner, permit))),
                    },
                )
            })
        })
    }

    fn async_close<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            inner.closed.store(true, Ordering::Release);
            let drained = if let Ok(mut idle) = inner.idle.lock() {
                idle.drain(..).collect::<Vec<_>>()
            } else {
                Vec::new()
            };
            for conn in drained {
                let _ = conn.disconnect().await;
            }
            Ok(())
        })
    }
}

#[pymethods]
impl NativeMysqlConnection {
    /// Execute buffered text SQL using safe named or positional MySQL parameters.
    #[pyo3(signature = (sql, parameters, text_mode, statement_kind=None, type_metadata=None))]
    fn async_execute<'py>(
        slf: Py<Self>,
        py: Python<'py>,
        sql: String,
        parameters: Py<PyAny>,
        text_mode: bool,
        statement_kind: Option<String>,
        type_metadata: Option<MysqlTypeMetadata>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let _ = (statement_kind, type_metadata);
        let (sql, values) = extract_parameters(py, &sql, &parameters, text_mode)?;
        let (mut connection, active) = {
            let borrowed = slf.borrow(py);
            let connection = borrowed
                .connection
                .lock()
                .map_err(|_| OperationalError::new_err("MySQL connection lock was poisoned"))?
                .take()
                .ok_or_else(|| OperationalError::new_err("Connection is closed"))?;
            (connection, borrowed.active.load(Ordering::Acquire))
        };
        let owner = slf.clone_ref(py);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let result = execute_buffered(&mut connection, &sql, values, active).await;
            let (outcome, began) = match result {
                Ok((result, began)) => (Ok(result), began),
                Err((error, began)) => (Err(error), began),
            };
            Python::attach(|py| {
                let borrowed = owner.borrow(py);
                if began {
                    borrowed.active.store(true, Ordering::Release);
                }
                if let Ok(mut guard) = borrowed.connection.lock() {
                    *guard = Some(connection);
                }
            });
            match outcome {
                Ok(result) => Python::attach(|py| make_native_result(py, result)),
                Err(error) => Err(error),
            }
        })
    }

    /// Existence probe matching SQLAlchemy's MySQL ``has_table``: a
    /// current-database ``information_schema.TABLES`` lookup.
    fn async_has_table<'py>(
        slf: Py<Self>,
        py: Python<'py>,
        name: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let mut connection = {
            let borrowed = slf.borrow(py);
            borrowed
                .connection
                .lock()
                .map_err(|_| OperationalError::new_err("MySQL connection lock was poisoned"))?
                .take()
                .ok_or_else(|| OperationalError::new_err("Connection is closed"))?
        };
        let owner = slf.clone_ref(py);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let result = connection
                .exec::<Row, _, _>(
                    "SELECT COUNT(*) FROM information_schema.TABLES \
                     WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?",
                    mysql_async::Params::Positional(vec![Value::Bytes(name.into_bytes())]),
                )
                .await;
            Python::attach(|py| {
                let borrowed = owner.borrow(py);
                if let Ok(mut guard) = borrowed.connection.lock() {
                    *guard = Some(connection);
                }
            });
            match result {
                Ok(rows) => {
                    let count = rows
                        .first()
                        .and_then(|row| row.get::<Option<i64>, _>(0))
                        .flatten()
                        .unwrap_or(0);
                    Ok(count > 0)
                }
                Err(error) => Err(map_mysql_error(error)),
            }
        })
    }

    fn async_transaction<'py>(
        slf: Py<Self>,
        py: Python<'py>,
        operation: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let operation = operation.to_owned();
        let connection = slf.clone_ref(py);
        let mut conn = {
            let borrowed = slf.borrow(py);
            borrowed
                .connection
                .lock()
                .map_err(|_| OperationalError::new_err("MySQL connection lock was poisoned"))?
                .take()
                .ok_or_else(|| OperationalError::new_err("Connection is closed"))?
        };
        let active = slf.borrow(py).active.load(Ordering::Acquire);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let outcome = match operation.as_str() {
                "begin" if active => Err(crate::InvalidRequestError::new_err(
                    "This connection already has an active transaction",
                )),
                "begin" => conn
                    .query_drop("BEGIN")
                    .await
                    .map(|_| true)
                    .map_err(map_mysql_error),
                "commit" if active => conn
                    .query_drop("COMMIT")
                    .await
                    .map(|_| false)
                    .map_err(map_mysql_error),
                "rollback" if active => conn
                    .query_drop("ROLLBACK")
                    .await
                    .map(|_| false)
                    .map_err(map_mysql_error),
                "commit" | "rollback" => Ok(active),
                "close" => Ok(active),
                _ => Err(crate::InvalidRequestError::new_err(
                    "Unknown async operation",
                )),
            };
            Python::attach(|py| {
                let borrowed = connection.borrow(py);
                if let Ok(new_active) = &outcome {
                    borrowed.active.store(*new_active, Ordering::Release);
                }
                if let Ok(mut guard) = borrowed.connection.lock() {
                    *guard = Some(conn);
                }
            });
            outcome.map(|_| ())
        })
    }

    /// Return the connection to the pool: roll back an open transaction first
    /// (reset-on-return), then release the slot permit. Dead connections and
    /// returns after engine dispose are dropped instead of idled.
    fn async_close<'py>(slf: Py<Self>, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let borrowed = slf.borrow(py);
        let connection = borrowed
            .connection
            .lock()
            .map_err(|_| OperationalError::new_err("MySQL connection lock was poisoned"))?
            .take();
        let pool_entry = borrowed
            .pool
            .lock()
            .map_err(|_| OperationalError::new_err("MySQL pool lock was poisoned"))?
            .take();
        let active = borrowed.active.load(Ordering::Acquire);
        drop(borrowed);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let Some(mut conn) = connection else {
                return Ok(());
            };
            let Some((inner, permit)) = pool_entry else {
                let _ = conn.disconnect().await;
                return Ok(());
            };
            let mut healthy = !conn.is_disconnected();
            if healthy && active {
                healthy = conn.query_drop("ROLLBACK").await.is_ok();
                Python::attach(|py| slf.borrow(py).active.store(false, Ordering::Release));
            }
            if healthy && !inner.closed.load(Ordering::Acquire) {
                if let Ok(mut idle) = inner.idle.lock() {
                    idle.push(conn);
                } else {
                    let _ = conn.disconnect().await;
                }
            } else {
                let _ = conn.disconnect().await;
            }
            drop(permit);
            Ok(())
        })
    }
}

/// Map SQLAlchemy-style URL options onto `mysql_async` connection options.
/// `sslmode=disable` selects plaintext (required for local development);
/// everything else keeps rustls-verified TLS via `SslOpts::default()`.
fn parse_mysql_opts(url: &str) -> PyResult<Opts> {
    let (base, params) = match url.split_once('?') {
        Some((base, query)) => (base, query),
        None => (url, ""),
    };
    let disable_tls = params
        .split('&')
        .any(|pair| pair.eq_ignore_ascii_case("sslmode=disable"));
    let remaining: Vec<&str> = params
        .split('&')
        .filter(|pair| !pair.eq_ignore_ascii_case("sslmode=disable"))
        .collect();
    let normalized = if remaining.iter().any(|pair| !pair.is_empty()) {
        format!("{}?{}", base, remaining.join("&"))
    } else {
        base.to_owned()
    };
    let mut builder = OptsBuilder::from_opts(
        Opts::from_str(&normalized)
            .map_err(|_| crate::ArgumentError::new_err("Invalid MySQL connection URL"))?,
    );
    builder = builder.ssl_opts(if disable_tls {
        None
    } else {
        // mysql_async's verified rustls path uses the vendored WebPKI roots.
        Some(SslOpts::default())
    });
    Ok(Opts::from(builder))
}

async fn execute_buffered(
    connection: &mut Conn,
    sql: &str,
    values: Vec<MysqlValue>,
    active: bool,
) -> Result<(MysqlResult, bool), (PyErr, bool)> {
    let mut began = false;
    if !active {
        connection
            .query_drop("BEGIN")
            .await
            .map_err(|error| (map_mysql_error(error), false))?;
        began = true;
    }
    let params: Vec<Value> = values.into_iter().map(Value::from).collect();
    // Binary protocol gives typed result values; statements the server cannot
    // prepare (ER_UNSUPPORTED_PS) fall back to the text protocol. The iterator
    // forms are used so column metadata survives empty result sets.
    let execution = match connection
        .exec_iter(sql, mysql_async::Params::Positional(params))
        .await
    {
        Ok(result) => collect_result(result).await,
        Err(mysql_async::Error::Server(server)) if server.code == 1295 => {
            match connection.query_iter(sql).await {
                Ok(result) => collect_result(result).await,
                Err(error) => Err(error),
            }
        }
        Err(error) => Err(error),
    };
    match execution {
        Ok((columns, rows)) => {
            let affected = connection.affected_rows();
            let buffered =
                buffer_rows(&columns, &rows, affected).map_err(|error| (error, began))?;
            Ok((buffered, began))
        }
        Err(error) => Err((map_mysql_error(error), began)),
    }
}

/// Drain a query result into its column metadata and row list in one shot.
async fn collect_result<P: mysql_async::prelude::Protocol>(
    result: mysql_async::QueryResult<'_, '_, P>,
) -> Result<(Vec<mysql_async::Column>, Vec<Row>), mysql_async::Error> {
    let columns = result.columns_ref().to_vec();
    let rows = result.collect_and_drop::<Row>().await?;
    Ok((columns, rows))
}

struct MysqlResult {
    columns: Vec<String>,
    rows: Vec<Vec<MysqlValue>>,
    rowcount: i64,
}

fn buffer_rows(
    columns: &[mysql_async::Column],
    rows: &[Row],
    affected: u64,
) -> PyResult<MysqlResult> {
    let names = columns
        .iter()
        .map(|column| column.name_str().into_owned())
        .collect::<Vec<_>>();
    let buffered = rows
        .iter()
        .map(|row| {
            columns
                .iter()
                .enumerate()
                .map(|(index, column)| {
                    row_to_value(row, index, column.column_type(), column.character_set())
                })
                .collect::<PyResult<Vec<_>>>()
        })
        .collect::<PyResult<Vec<_>>>()?;
    // Result sets report their row count; statements without one report the
    // server's affected-row count.
    let rowcount = if names.is_empty() {
        affected as i64
    } else {
        buffered.len() as i64
    };
    Ok(MysqlResult {
        columns: names,
        rowcount,
        rows: buffered,
    })
}

fn row_to_value(row: &Row, index: usize, ty: ColumnType, charset: u16) -> PyResult<MysqlValue> {
    let Some(value) = row.as_ref(index) else {
        return Ok(MysqlValue::Null);
    };
    match (value, ty) {
        (Value::NULL, _) => Ok(MysqlValue::Null),
        (Value::Int(number), _) => Ok(MysqlValue::Int(*number)),
        (Value::UInt(number), _) => Ok(MysqlValue::Int(i64::try_from(*number).unwrap_or(i64::MAX))),
        (Value::Float(number), _) => Ok(MysqlValue::Float(f64::from(*number))),
        (Value::Double(number), _) => Ok(MysqlValue::Float(*number)),
        (
            Value::Date(year, month, day, ..),
            ColumnType::MYSQL_TYPE_DATE | ColumnType::MYSQL_TYPE_NEWDATE,
        ) => chrono::NaiveDate::from_ymd_opt(i32::from(*year), u32::from(*month), u32::from(*day))
            .map(MysqlValue::Date)
            .ok_or_else(|| OperationalError::new_err("Could not decode MySQL date")),
        (
            Value::Date(year, month, day, hour, minute, second, micros),
            ColumnType::MYSQL_TYPE_DATETIME | ColumnType::MYSQL_TYPE_TIMESTAMP,
        ) => chrono::NaiveDate::from_ymd_opt(i32::from(*year), u32::from(*month), u32::from(*day))
            .and_then(|date| {
                date.and_hms_micro_opt(
                    u32::from(*hour),
                    u32::from(*minute),
                    u32::from(*second),
                    *micros,
                )
            })
            .map(MysqlValue::Timestamp)
            .ok_or_else(|| OperationalError::new_err("Could not decode MySQL datetime")),
        // The text protocol reports every value as bytes; the column type tag
        // still identifies the logical type for decoding.
        (
            Value::Bytes(bytes),
            ColumnType::MYSQL_TYPE_TINY
            | ColumnType::MYSQL_TYPE_SHORT
            | ColumnType::MYSQL_TYPE_INT24
            | ColumnType::MYSQL_TYPE_LONG
            | ColumnType::MYSQL_TYPE_LONGLONG
            | ColumnType::MYSQL_TYPE_YEAR,
        ) => std::str::from_utf8(bytes)
            .ok()
            .and_then(|text| text.trim().parse::<i64>().ok())
            .map(MysqlValue::Int)
            .ok_or_else(|| OperationalError::new_err("Could not decode MySQL integer")),
        (Value::Bytes(bytes), ColumnType::MYSQL_TYPE_FLOAT | ColumnType::MYSQL_TYPE_DOUBLE) => {
            std::str::from_utf8(bytes)
                .ok()
                .and_then(|text| text.trim().parse::<f64>().ok())
                .map(MysqlValue::Float)
                .ok_or_else(|| OperationalError::new_err("Could not decode MySQL float"))
        }
        (Value::Bytes(bytes), ColumnType::MYSQL_TYPE_DATE | ColumnType::MYSQL_TYPE_NEWDATE) => {
            std::str::from_utf8(bytes)
                .ok()
                .and_then(|text| chrono::NaiveDate::parse_from_str(text.trim(), "%Y-%m-%d").ok())
                .map(MysqlValue::Date)
                .ok_or_else(|| OperationalError::new_err("Could not decode MySQL date"))
        }
        (
            Value::Bytes(bytes),
            ColumnType::MYSQL_TYPE_DATETIME | ColumnType::MYSQL_TYPE_TIMESTAMP,
        ) => std::str::from_utf8(bytes)
            .ok()
            .and_then(|text| {
                chrono::NaiveDateTime::parse_from_str(text.trim(), "%Y-%m-%d %H:%M:%S%.f").ok()
            })
            .map(MysqlValue::Timestamp)
            .ok_or_else(|| OperationalError::new_err("Could not decode MySQL datetime")),
        (
            Value::Bytes(bytes),
            ColumnType::MYSQL_TYPE_DECIMAL | ColumnType::MYSQL_TYPE_NEWDECIMAL,
        ) => String::from_utf8(bytes.clone())
            .map(MysqlValue::Decimal)
            .map_err(|_| OperationalError::new_err("Could not decode MySQL decimal")),
        (Value::Bytes(bytes), _) if charset == 63 => Ok(MysqlValue::Bytes(bytes.clone())),
        (Value::Bytes(bytes), _) => String::from_utf8(bytes.clone())
            .map(MysqlValue::Text)
            .map_err(|_| OperationalError::new_err("Could not decode MySQL text")),
        (Value::Time(..), _) => Err(OperationalError::new_err(
            "Unsupported MySQL TIME result type in this driver slice",
        )),
        _ => Err(OperationalError::new_err(
            "Unsupported MySQL result type in this driver slice",
        )),
    }
}

fn make_native_result(py: Python<'_>, result: MysqlResult) -> PyResult<NativeResult> {
    let rows = result
        .rows
        .into_iter()
        .map(|row| {
            row.into_iter()
                .map(|value| mysql_value_to_python(py, value))
                .collect::<PyResult<Vec<_>>>()
        })
        .collect::<PyResult<Vec<_>>>()?;
    Ok(NativeResult {
        columns: result.columns,
        rows,
        next_row: 0,
        rowcount: result.rowcount,
    })
}

fn mysql_value_to_python(py: Python<'_>, value: MysqlValue) -> PyResult<Py<PyAny>> {
    match value {
        MysqlValue::Null => Ok(py.None()),
        MysqlValue::Bool(value) => Ok(value.into_pyobject(py)?.to_owned().unbind().into_any()),
        MysqlValue::Int(value) => Ok(value.into_pyobject(py)?.to_owned().unbind().into_any()),
        MysqlValue::Float(value) => Ok(value.into_pyobject(py)?.to_owned().unbind().into_any()),
        MysqlValue::Text(value) => Ok(value.into_pyobject(py)?.to_owned().unbind().into_any()),
        MysqlValue::Bytes(value) => Ok(PyBytes::new(py, &value)
            .into_pyobject(py)?
            .to_owned()
            .unbind()
            .into_any()),
        MysqlValue::Timestamp(value) => Ok(value.into_pyobject(py)?.into_any().unbind()),
        MysqlValue::Date(value) => Ok(value.into_pyobject(py)?.into_any().unbind()),
        MysqlValue::Decimal(value) => Ok(py
            .import("decimal")?
            .getattr("Decimal")?
            .call1((value,))?
            .unbind()),
    }
}

fn extract_parameters(
    py: Python<'_>,
    sql: &str,
    parameters: &Py<PyAny>,
    text_mode: bool,
) -> PyResult<(String, Vec<MysqlValue>)> {
    let value = parameters.bind(py);
    if let Ok(mapping) = value.cast::<PyDict>() {
        let mut entries = HashMap::new();
        for (key, item) in mapping.iter() {
            let name = key
                .extract::<String>()
                .map_err(|_| StatementError::new_err("MySQL named bind keys must be strings"))?;
            entries.insert(name, python_to_mysql_value(&item)?);
        }
        let (rendered, names) = if text_mode {
            scan_named_binds(sql)?
        } else {
            (sql.to_owned(), Vec::new())
        };
        let ordered = names
            .iter()
            .map(|name| {
                entries.get(name).cloned().ok_or_else(|| {
                    StatementError::new_err(format!(
                        "A value is required for bind parameter '{name}'"
                    ))
                })
            })
            .collect::<PyResult<Vec<_>>>()?;
        Ok((rendered, ordered))
    } else if let Ok(sequence) = value.cast::<PyList>() {
        let values = sequence
            .iter()
            .map(|item| python_to_mysql_value(&item))
            .collect::<PyResult<Vec<_>>>()?;
        Ok((sql.to_owned(), values))
    } else {
        Err(PyTypeError::new_err(
            "MySQL parameters must be a mapping or list",
        ))
    }
}

fn python_to_mysql_value(value: &Bound<'_, PyAny>) -> PyResult<MysqlValue> {
    if value.is_none() {
        Ok(MysqlValue::Null)
    } else if value.is_instance_of::<PyBool>() {
        Ok(MysqlValue::Bool(value.extract()?))
    } else if value.is_instance_of::<PyInt>() {
        Ok(MysqlValue::Int(value.extract()?))
    } else if value.is_instance_of::<PyFloat>() {
        Ok(MysqlValue::Float(value.extract()?))
    } else if value.is_instance_of::<PyString>() {
        Ok(MysqlValue::Text(value.extract()?))
    } else if value.is_instance_of::<PyBytes>() {
        Ok(MysqlValue::Bytes(value.extract()?))
    } else if value.is_instance_of::<PyDateTime>() {
        // Timezone-aware values bind their wall-clock fields; the offset is
        // dropped like the SQLite/PostgreSQL slices.
        Ok(MysqlValue::Timestamp(value.extract()?))
    } else if value.is_instance_of::<PyDate>() {
        Ok(MysqlValue::Date(value.extract()?))
    } else if value.get_type().name()? == "Decimal" {
        Ok(MysqlValue::Decimal(value.str()?.extract()?))
    } else {
        Err(PyTypeError::new_err("Unsupported MySQL bind value type"))
    }
}

/// Replace named binds outside SQL strings/comments and return first-seen names.
/// MySQL renders positional `?` parameters and additionally skips backtick
/// quoted identifiers and `#` comments (dollar-quoting does not exist).
fn scan_named_binds(sql: &str) -> PyResult<(String, Vec<String>)> {
    let bytes = sql.as_bytes();
    let mut output = String::with_capacity(sql.len());
    let mut names = Vec::<String>::new();
    let mut indexes = HashMap::<String, usize>::new();
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'\'' || bytes[index] == b'"' || bytes[index] == b'`' {
            let quote = bytes[index];
            let start = index;
            index += 1;
            while index < bytes.len() {
                if bytes[index] == quote {
                    index += 1;
                    if index < bytes.len() && bytes[index] == quote {
                        index += 1;
                        continue;
                    }
                    break;
                }
                index += 1;
            }
            output.push_str(&sql[start..index]);
        } else if bytes[index..].starts_with(b"--") || bytes[index] == b'#' {
            let start = index;
            while index < bytes.len() && bytes[index] != b'\n' {
                index += 1;
            }
            output.push_str(&sql[start..index]);
        } else if bytes[index..].starts_with(b"/*") {
            let start = index;
            index += 2;
            let mut depth = 1_u32;
            while index < bytes.len() && depth > 0 {
                if bytes[index..].starts_with(b"/*") {
                    depth += 1;
                    index += 2;
                } else if bytes[index..].starts_with(b"*/") {
                    depth -= 1;
                    index += 2;
                } else {
                    index += 1;
                }
            }
            output.push_str(&sql[start..index]);
        } else if bytes[index] == b'\\' && bytes.get(index + 1) == Some(&b':') {
            output.push(':');
            index += 2;
        } else if bytes[index] == b':'
            && bytes.get(index.wrapping_sub(1)) != Some(&b':')
            && bytes
                .get(index + 1)
                .is_some_and(|byte| byte.is_ascii_alphabetic() || *byte == b'_')
        {
            let start = index + 1;
            index = start;
            while index < bytes.len()
                && (bytes[index].is_ascii_alphanumeric() || bytes[index] == b'_')
            {
                index += 1;
            }
            let name = &sql[start..index];
            if !indexes.contains_key(name) {
                names.push(name.to_owned());
                indexes.insert(name.to_owned(), names.len());
            }
            output.push('?');
        } else {
            let character = sql[index..]
                .chars()
                .next()
                .ok_or_else(|| StatementError::new_err("Invalid SQL text"))?;
            output.push(character);
            index += character.len_utf8();
        }
    }
    Ok((output, names))
}

fn map_mysql_error(error: mysql_async::Error) -> PyErr {
    if let mysql_async::Error::Server(server) = &error {
        if matches!(
            server.code,
            1048 | 1062 | 1216 | 1217 | 1364 | 1451 | 1452 | 3819 | 4025
        ) {
            return IntegrityError::new_err("MySQL integrity constraint failed");
        }
        if matches!(server.code, 1046 | 1054 | 1064 | 1142 | 1146 | 1241) {
            return ProgrammingError::new_err("MySQL statement is invalid");
        }
    }
    OperationalError::new_err("MySQL operation failed")
}

#[cfg(test)]
mod tests {
    use super::scan_named_binds;

    #[test]
    fn named_scanner_skips_literals_comments_backticks_and_hash() {
        let sql = "SELECT :value, ':ignored', `:also_ignored`, col -- :comment\n# :hash\n/* :block /* :nested */ */";
        let (rendered, names) = scan_named_binds(sql).expect("valid binds");
        assert_eq!(names, ["value"]);
        assert!(rendered.contains("SELECT ?"));
        assert!(rendered.contains("`:also_ignored`"));
    }

    #[test]
    fn named_scanner_reuses_repeated_bind_position() {
        let (rendered, names) = scan_named_binds("SELECT :v + :v").expect("valid binds");
        assert_eq!(names, ["v"]);
        assert_eq!(rendered, "SELECT ? + ?");
    }
}

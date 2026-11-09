use std::collections::HashMap;
use std::str::FromStr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use bytes::BytesMut;
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDate, PyDateTime, PyDict, PyFloat, PyInt, PyList, PyString};
use tokio_postgres::config::SslMode;
use tokio_postgres::types::{IsNull, ToSql, Type};
use tokio_postgres::{Client, Config, Row};
use tokio_postgres_rustls::MakeRustlsConnect;

use crate::{IntegrityError, NativeResult, OperationalError, ProgrammingError, StatementError};

type PgTypeMetadata = (Vec<Option<String>>, Vec<String>, Vec<Option<String>>);

/// QueuePool-style shared state for a PostgreSQL engine: a bounded slot
/// semaphore, an idle-client stack, and a closed flag set on dispose.
struct PgPoolInner {
    url: String,
    idle: Mutex<Vec<Arc<Client>>>,
    capacity: Arc<tokio::sync::Semaphore>,
    checkout_timeout: std::time::Duration,
    pool_size: usize,
    max_overflow: i64,
    closed: AtomicBool,
}

/// Native Tokio PostgreSQL engine with a bounded async connection pool.
#[pyclass(module = "catalyst._catalyst")]
pub(crate) struct NativePgEngine {
    inner: Arc<PgPoolInner>,
}

/// One async PostgreSQL client connection checked out from the pool.
#[pyclass(module = "catalyst._catalyst")]
pub(crate) struct NativePgConnection {
    client: Mutex<Option<Arc<Client>>>,
    active: AtomicBool,
    next_savepoint: std::sync::atomic::AtomicU64,
    pool: Mutex<Option<(Arc<PgPoolInner>, tokio::sync::OwnedSemaphorePermit)>>,
}

#[derive(Clone, Debug)]
enum PgValue {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
    Timestamp(chrono::NaiveDateTime),
    TimestampTz(chrono::DateTime<chrono::Utc>),
    Date(chrono::NaiveDate),
}

impl ToSql for PgValue {
    fn to_sql(
        &self,
        ty: &Type,
        output: &mut BytesMut,
    ) -> Result<IsNull, Box<dyn std::error::Error + Sync + Send>> {
        if matches!(self, Self::Null) {
            return Ok(IsNull::Yes);
        }
        match (self, ty.clone()) {
            (Self::Bool(value), Type::BOOL) => output.extend_from_slice(&[u8::from(*value)]),
            (Self::Int(value), Type::INT2) => {
                let value = i16::try_from(*value)?;
                output.extend_from_slice(&value.to_be_bytes());
            }
            (Self::Int(value), Type::INT4) => {
                let value = i32::try_from(*value)?;
                output.extend_from_slice(&value.to_be_bytes());
            }
            (Self::Int(value), Type::INT8) => output.extend_from_slice(&value.to_be_bytes()),
            (Self::Float(value), Type::FLOAT4) => {
                output.extend_from_slice(&(*value as f32).to_bits().to_be_bytes());
            }
            (Self::Float(value), Type::FLOAT8) => {
                output.extend_from_slice(&value.to_bits().to_be_bytes())
            }
            (Self::Text(value), Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME) => {
                output.extend_from_slice(value.as_bytes());
            }
            (Self::Bytes(value), Type::BYTEA) => output.extend_from_slice(value),
            (Self::Timestamp(value), Type::TIMESTAMP) => return value.to_sql(ty, output),
            (Self::TimestampTz(value), Type::TIMESTAMPTZ) => return value.to_sql(ty, output),
            (Self::Date(value), Type::DATE) => return value.to_sql(ty, output),
            _ => {
                return Err(
                    "PostgreSQL bind value does not match the inferred parameter type".into(),
                );
            }
        }
        Ok(IsNull::No)
    }

    fn accepts(_ty: &Type) -> bool {
        true
    }

    tokio_postgres::types::to_sql_checked!();
}

impl PgValue {
    fn pg_type(&self) -> Type {
        match self {
            Self::Null => Type::TEXT,
            Self::Bool(_) => Type::BOOL,
            Self::Int(_) => Type::INT8,
            Self::Float(_) => Type::FLOAT8,
            Self::Text(_) => Type::TEXT,
            Self::Bytes(_) => Type::BYTEA,
            Self::Timestamp(_) => Type::TIMESTAMP,
            Self::TimestampTz(_) => Type::TIMESTAMPTZ,
            Self::Date(_) => Type::DATE,
        }
    }
}

#[pymethods]
impl NativePgEngine {
    #[new]
    #[pyo3(signature = (url, pool_size=5, max_overflow=10, pool_timeout=30.0))]
    fn new(url: &str, pool_size: usize, max_overflow: i64, pool_timeout: f64) -> Self {
        // `max_overflow` of -1 means unlimited, matching SQLAlchemy QueuePool.
        let permits = if max_overflow < 0 {
            tokio::sync::Semaphore::MAX_PERMITS
        } else {
            pool_size.saturating_add(max_overflow as usize)
        };
        Self {
            inner: Arc::new(PgPoolInner {
                url: url.to_owned(),
                idle: Mutex::new(Vec::new()),
                capacity: Arc::new(tokio::sync::Semaphore::new(permits)),
                checkout_timeout: std::time::Duration::from_secs_f64(pool_timeout.max(0.0)),
                pool_size,
                max_overflow,
                closed: AtomicBool::new(false),
            }),
        }
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
                    return Err(OperationalError::new_err(
                        "PostgreSQL connection pool is closed",
                    ));
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
            let client = loop {
                let candidate = inner
                    .idle
                    .lock()
                    .map_err(|_| OperationalError::new_err("PostgreSQL pool lock was poisoned"))?
                    .pop();
                match candidate {
                    Some(client) if client.is_closed() => continue,
                    other => break other,
                }
            };
            let client = match client {
                Some(client) => client,
                None => connect_client(&inner.url).await?,
            };
            Python::attach(|py| {
                Py::new(
                    py,
                    NativePgConnection {
                        client: Mutex::new(Some(client)),
                        active: AtomicBool::new(false),
                        next_savepoint: std::sync::atomic::AtomicU64::new(0),
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
            if let Ok(mut idle) = inner.idle.lock() {
                idle.clear();
            }
            Ok(())
        })
    }
}

async fn connect_client(url: &str) -> PyResult<Arc<Client>> {
    let mut config = Config::from_str(url)
        .map_err(|_| crate::ArgumentError::new_err("Invalid PostgreSQL connection URL"))?;
    let disable_tls = config.get_ssl_mode() == SslMode::Disable;
    // PostgreSQL's client default is `prefer`; Catalyst intentionally upgrades
    // every other setting to mandatory TLS with certificate/hostname checks.
    config.ssl_mode(if disable_tls {
        SslMode::Disable
    } else {
        SslMode::Require
    });
    let tls = verified_tls_connector()?;
    let (client, connection) = config.connect(tls).await.map_err(|_| {
        if disable_tls {
            OperationalError::new_err("Could not connect to PostgreSQL")
        } else {
            OperationalError::new_err("Could not establish a verified PostgreSQL TLS connection")
        }
    })?;
    tokio::spawn(async move {
        // The task owns protocol I/O. Driver details are intentionally not logged.
        let _ = connection.await;
    });
    Ok(Arc::new(client))
}

#[pymethods]
impl NativePgConnection {
    /// Execute buffered text SQL using safe named or positional PostgreSQL parameters.
    #[pyo3(signature = (sql, parameters, text_mode, statement_kind=None, type_metadata=None))]
    fn async_execute<'py>(
        slf: Py<Self>,
        py: Python<'py>,
        sql: String,
        parameters: Py<PyAny>,
        text_mode: bool,
        statement_kind: Option<String>,
        type_metadata: Option<PgTypeMetadata>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let _ = statement_kind;
        let (bind_types, _bind_names, _result_types) = type_metadata.unwrap_or_default();
        let (sql, values) = extract_parameters(py, &sql, &parameters, text_mode)
            .map_err(|error| crate::wrap_statement_error(py, error, &sql, parameters.bind(py)))?;
        let values = values
            .into_iter()
            .enumerate()
            .map(|(index, value)| {
                coerce_pg_value(value, bind_types.get(index).and_then(|tag| tag.as_deref()))
            })
            .collect::<PyResult<Vec<_>>>()
            .map_err(|error| crate::wrap_statement_error(py, error, &sql, parameters.bind(py)))?;
        let (client, active) = {
            let borrowed = slf.borrow(py);
            let client = borrowed
                .client
                .lock()
                .map_err(|_| OperationalError::new_err("PostgreSQL connection lock was poisoned"))?
                .clone()
                .ok_or_else(|| OperationalError::new_err("Connection is closed"))?;
            (client, borrowed.active.load(Ordering::Acquire))
        };
        let connection = slf.clone_ref(py);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            if !active {
                client.batch_execute("BEGIN").await.map_err(map_pg_error)?;
                Python::attach(|py| connection.borrow(py).active.store(true, Ordering::Release));
            }
            let statement = match client.prepare(&sql).await {
                Ok(statement) => statement,
                Err(_) => {
                    let typed_values = values
                        .iter()
                        .map(|value| (value as &(dyn ToSql + Sync), value.pg_type()))
                        .collect::<Vec<_>>();
                    let rows = client
                        .query_typed(&sql, &typed_values)
                        .await
                        .map_err(map_pg_error)?;
                    let result = buffer_rows_inferred(&rows)?;
                    return Python::attach(|py| make_native_result(py, result));
                }
            };
            let param_types = statement.params();
            if values.len() != param_types.len() {
                return Err(StatementError::new_err(
                    "Incorrect number of PostgreSQL bind parameters",
                ));
            }
            let typed_values = values
                .iter()
                .zip(param_types)
                .map(|(value, server_type)| {
                    let parameter_type = if matches!(value, PgValue::Null) {
                        server_type.clone()
                    } else if server_type == &Type::TEXT {
                        value.pg_type()
                    } else {
                        server_type.clone()
                    };
                    (value as &(dyn ToSql + Sync), parameter_type)
                })
                .collect::<Vec<_>>();
            let result = if statement.columns().is_empty() {
                let count = client
                    .execute_typed(&sql, &typed_values)
                    .await
                    .map_err(map_pg_error)?;
                PgResult {
                    columns: Vec::new(),
                    rows: Vec::new(),
                    rowcount: count as i64,
                }
            } else {
                let rows = client
                    .query_typed(&sql, &typed_values)
                    .await
                    .map_err(map_pg_error)?;
                buffer_rows(&rows, &statement)?
            };
            Python::attach(|py| make_native_result(py, result))
        })
    }

    /// Existence probe matching SQLAlchemy 2.x's PostgreSQL ``has_table``:
    /// search-path-visible table-like relations outside pg_catalog.
    fn async_has_table<'py>(&self, py: Python<'py>, name: String) -> PyResult<Bound<'py, PyAny>> {
        let client = self
            .client
            .lock()
            .map_err(|_| OperationalError::new_err("PostgreSQL connection lock was poisoned"))?
            .clone()
            .ok_or_else(|| OperationalError::new_err("Connection is closed"))?;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let row = client
                .query_one(
                    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c \
                     JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace \
                     WHERE c.relname = $1 AND c.relkind IN ('r', 'p', 'f', 'v', 'm') \
                     AND pg_catalog.pg_table_is_visible(c.oid) \
                     AND n.nspname != 'pg_catalog')",
                    &[&name],
                )
                .await
                .map_err(map_pg_error)?;
            Ok(row.get::<_, bool>(0))
        })
    }

    fn async_transaction<'py>(
        slf: Py<Self>,
        py: Python<'py>,
        operation: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let operation = operation.to_owned();
        let connection = slf.clone_ref(py);
        let client = {
            let borrowed = slf.borrow(py);
            borrowed
                .client
                .lock()
                .map_err(|_| OperationalError::new_err("PostgreSQL connection lock was poisoned"))?
                .clone()
                .ok_or_else(|| OperationalError::new_err("Connection is closed"))?
        };
        let active = slf.borrow(py).active.load(Ordering::Acquire);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            match operation.as_str() {
                "begin" if active => Err(crate::InvalidRequestError::new_err(
                    "This connection already has an active transaction",
                )),
                "begin" => {
                    client.batch_execute("BEGIN").await.map_err(map_pg_error)?;
                    Python::attach(|py| {
                        connection.borrow(py).active.store(true, Ordering::Release)
                    });
                    Ok(())
                }
                "commit" if active => {
                    client.batch_execute("COMMIT").await.map_err(map_pg_error)?;
                    Python::attach(|py| {
                        connection.borrow(py).active.store(false, Ordering::Release)
                    });
                    Ok(())
                }
                "rollback" if active => {
                    client
                        .batch_execute("ROLLBACK")
                        .await
                        .map_err(map_pg_error)?;
                    Python::attach(|py| {
                        connection.borrow(py).active.store(false, Ordering::Release)
                    });
                    Ok(())
                }
                "commit" | "rollback" => Ok(()),
                "close" => Ok(()),
                _ => Err(crate::InvalidRequestError::new_err(
                    "Unknown async operation",
                )),
            }
        })
    }

    /// Create a uniquely named savepoint, autobeginning a root if needed.
    fn async_begin_savepoint<'py>(slf: Py<Self>, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = {
            let borrowed = slf.borrow(py);
            borrowed
                .client
                .lock()
                .map_err(|_| OperationalError::new_err("PostgreSQL connection lock was poisoned"))?
                .clone()
                .ok_or_else(|| OperationalError::new_err("Connection is closed"))?
        };
        let owner = slf.clone_ref(py);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let (active, serial) = Python::attach(|py| {
                let borrowed = owner.borrow(py);
                (
                    borrowed.active.load(Ordering::Acquire),
                    borrowed.next_savepoint.fetch_add(1, Ordering::Relaxed),
                )
            });
            if !active {
                client.batch_execute("BEGIN").await.map_err(map_pg_error)?;
            }
            let name = format!("catalyst_sp_{serial}");
            client
                .batch_execute(&format!("SAVEPOINT {name}"))
                .await
                .map_err(map_pg_error)?;
            Python::attach(|py| owner.borrow(py).active.store(true, Ordering::Release));
            Ok(name)
        })
    }

    /// Release a savepoint and any nested savepoints beneath it.
    fn async_release_savepoint<'py>(
        slf: Py<Self>,
        py: Python<'py>,
        name: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = take_savepoint_client(slf, py, &name)?;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            client
                .batch_execute(&format!("RELEASE SAVEPOINT {name}"))
                .await
                .map_err(map_pg_error)
        })
    }

    /// Roll back to and release a savepoint.
    fn async_rollback_savepoint<'py>(
        slf: Py<Self>,
        py: Python<'py>,
        name: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = take_savepoint_client(slf, py, &name)?;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            client
                .batch_execute(&format!("ROLLBACK TO SAVEPOINT {name}"))
                .await
                .map_err(map_pg_error)?;
            client
                .batch_execute(&format!("RELEASE SAVEPOINT {name}"))
                .await
                .map_err(map_pg_error)
        })
    }

    /// Whether this holder has an open transaction (explicit or autobegin).
    fn async_is_active<'py>(slf: Py<Self>, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let active = slf.borrow(py).active.load(Ordering::Acquire);
        pyo3_async_runtimes::tokio::future_into_py(py, async move { Ok(active) })
    }

    /// Return the client to the pool: roll back an open transaction first
    /// (reset-on-return), then release the slot permit. Dead clients and
    /// returns after engine dispose are dropped instead of idled.
    fn async_close<'py>(slf: Py<Self>, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let borrowed = slf.borrow(py);
        let client = borrowed
            .client
            .lock()
            .map_err(|_| OperationalError::new_err("PostgreSQL connection lock was poisoned"))?
            .take();
        let pool_entry = borrowed
            .pool
            .lock()
            .map_err(|_| OperationalError::new_err("PostgreSQL pool lock was poisoned"))?
            .take();
        let active = borrowed.active.load(Ordering::Acquire);
        drop(borrowed);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let Some(client) = client else {
                return Ok(());
            };
            let Some((inner, permit)) = pool_entry else {
                drop(client);
                return Ok(());
            };
            let mut healthy = !client.is_closed();
            if healthy && active {
                healthy = client.batch_execute("ROLLBACK").await.is_ok();
            }
            if healthy
                && !inner.closed.load(Ordering::Acquire)
                && let Ok(mut idle) = inner.idle.lock()
            {
                idle.push(client);
            }
            drop(permit);
            Ok(())
        })
    }
}

/// Validate a generated savepoint name and clone the live client for it.
fn take_savepoint_client(
    slf: Py<NativePgConnection>,
    py: Python<'_>,
    name: &str,
) -> PyResult<Arc<Client>> {
    crate::validate_savepoint_name(name)?;
    let borrowed = slf.borrow(py);
    borrowed
        .client
        .lock()
        .map_err(|_| OperationalError::new_err("PostgreSQL connection lock was poisoned"))?
        .clone()
        .ok_or_else(|| OperationalError::new_err("Connection is closed"))
}

fn verified_tls_connector() -> PyResult<MakeRustlsConnect> {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = rustls::ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map_err(|_| OperationalError::new_err("Could not configure PostgreSQL TLS"))?
        .with_root_certificates(rustls::RootCertStore {
            roots: webpki_roots::TLS_SERVER_ROOTS.to_vec(),
        })
        .with_no_client_auth();
    Ok(MakeRustlsConnect::new(config))
}

fn extract_parameters(
    py: Python<'_>,
    sql: &str,
    parameters: &Py<PyAny>,
    text_mode: bool,
) -> PyResult<(String, Vec<PgValue>)> {
    let value = parameters.bind(py);
    if let Ok(mapping) = value.cast::<PyDict>() {
        let mut entries = HashMap::new();
        for (key, item) in mapping.iter() {
            let name = key.extract::<String>().map_err(|_| {
                StatementError::new_err("PostgreSQL named bind keys must be strings")
            })?;
            entries.insert(name, python_to_pg_value(&item)?);
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
            .map(|item| python_to_pg_value(&item))
            .collect::<PyResult<Vec<_>>>()?;
        Ok((sql.to_owned(), values))
    } else {
        Err(PyTypeError::new_err(
            "PostgreSQL parameters must be a mapping or list",
        ))
    }
}

/// Reinterpret a bind value using the compiled statement's declared type tag.
/// Compile-time bind processing formats some values for SQLite storage (for
/// example datetimes as `YYYY-MM-DD HH:MM:SS.ffffff` strings); PostgreSQL has
/// native typed parameters, so the driver coerces processed and user-supplied
/// raw values alike into the `PgValue` variant the tag requires.
fn coerce_pg_value(value: PgValue, tag: Option<&str>) -> PyResult<PgValue> {
    let Some(tag) = tag else {
        return Ok(value);
    };
    match (tag, value) {
        (_, PgValue::Null) => Ok(PgValue::Null),
        ("datetime", PgValue::Text(ref text)) => parse_pg_datetime(text),
        ("datetime", PgValue::Date(day)) => {
            Ok(PgValue::Timestamp(day.and_time(chrono::NaiveTime::MIN)))
        }
        ("date", PgValue::Text(ref text)) => parse_pg_date(text),
        ("date", PgValue::Timestamp(timestamp)) => Ok(PgValue::Date(timestamp.date())),
        ("binary", PgValue::Text(_)) => Err(PyTypeError::new_err(
            "Binary columns require bytes bind values",
        )),
        ("boolean", PgValue::Int(flag)) => Ok(PgValue::Bool(flag != 0)),
        ("integer", PgValue::Text(ref text)) => text
            .trim()
            .parse::<i64>()
            .map(PgValue::Int)
            .map_err(|_| PyTypeError::new_err("Invalid integer bind value")),
        ("float" | "double", PgValue::Int(number)) => Ok(PgValue::Float(number as f64)),
        (_, value) => Ok(value),
    }
}

fn parse_pg_datetime(text: &str) -> PyResult<PgValue> {
    for format in ["%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S%.f"] {
        if let Ok(parsed) = chrono::NaiveDateTime::parse_from_str(text, format) {
            return Ok(PgValue::Timestamp(parsed));
        }
    }
    Err(PyTypeError::new_err("Invalid datetime bind value"))
}

fn parse_pg_date(text: &str) -> PyResult<PgValue> {
    chrono::NaiveDate::parse_from_str(text, "%Y-%m-%d")
        .map(PgValue::Date)
        .map_err(|_| PyTypeError::new_err("Invalid date bind value"))
}

fn python_to_pg_value(value: &Bound<'_, PyAny>) -> PyResult<PgValue> {
    if value.is_none() {
        Ok(PgValue::Null)
    } else if value.is_instance_of::<PyBool>() {
        Ok(PgValue::Bool(value.extract()?))
    } else if value.is_instance_of::<PyInt>() {
        Ok(PgValue::Int(value.extract()?))
    } else if value.is_instance_of::<PyFloat>() {
        Ok(PgValue::Float(value.extract()?))
    } else if value.is_instance_of::<PyString>() {
        Ok(PgValue::Text(value.extract()?))
    } else if value.is_instance_of::<PyBytes>() {
        Ok(PgValue::Bytes(value.extract()?))
    } else if value.is_instance_of::<PyDateTime>() {
        // Timezone-aware values bind their wall-clock fields; the offset is
        // dropped like the SQLite slice (TIMESTAMP WITHOUT TIME ZONE target).
        Ok(PgValue::Timestamp(value.extract()?))
    } else if value.is_instance_of::<PyDate>() {
        Ok(PgValue::Date(value.extract()?))
    } else {
        let name = value
            .get_type()
            .qualname()
            .map(|name| name.to_string())
            .unwrap_or_else(|_| "unknown".to_owned());
        Err(ProgrammingError::new_err(format!(
            "Error binding parameter: type '{name}' is not supported"
        )))
    }
}

/// Replace named binds outside SQL strings/comments and return first-seen names.
fn scan_named_binds(sql: &str) -> PyResult<(String, Vec<String>)> {
    let bytes = sql.as_bytes();
    let mut output = String::with_capacity(sql.len());
    let mut names = Vec::<String>::new();
    let mut indexes = HashMap::<String, usize>::new();
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'\'' || bytes[index] == b'"' {
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
        } else if bytes[index..].starts_with(b"--") {
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
        } else if bytes[index] == b'$' {
            if let Some((delimiter, end)) = dollar_delimiter(bytes, index) {
                if let Some(relative) = sql[end..].find(delimiter) {
                    let finish = end + relative + delimiter.len();
                    output.push_str(&sql[index..finish]);
                    index = finish;
                } else {
                    output.push_str(&sql[index..]);
                    break;
                }
            } else {
                output.push('$');
                index += 1;
            }
        } else if bytes[index] == b'\\' && bytes.get(index + 1) == Some(&b':') {
            output.push(':');
            index += 2;
        } else if bytes[index] == b':'
            && bytes.get(index.wrapping_sub(1)) != Some(&b':')
            && bytes.get(index + 1) != Some(&b':')
            && bytes
                .get(index + 1)
                .is_some_and(|byte| byte.is_ascii_alphabetic() || *byte == b'_')
        {
            let start = index + 1;
            index = start + 1;
            while index < bytes.len()
                && (bytes[index].is_ascii_alphanumeric() || bytes[index] == b'_')
            {
                index += 1;
            }
            let name = &sql[start..index];
            let param_index = if let Some(existing) = indexes.get(name) {
                *existing
            } else {
                let next = names.len() + 1;
                names.push(name.to_owned());
                indexes.insert(name.to_owned(), next);
                next
            };
            output.push('$');
            output.push_str(&param_index.to_string());
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

fn dollar_delimiter(bytes: &[u8], start: usize) -> Option<(&str, usize)> {
    let mut end = start + 1;
    while end < bytes.len() && (bytes[end].is_ascii_alphanumeric() || bytes[end] == b'_') {
        end += 1;
    }
    if bytes.get(end) != Some(&b'$') {
        return None;
    }
    std::str::from_utf8(&bytes[start..=end])
        .ok()
        .map(|delimiter| (delimiter, end + 1))
}

struct PgResult {
    columns: Vec<String>,
    rows: Vec<Vec<PgValue>>,
    rowcount: i64,
}

fn buffer_rows(rows: &[Row], statement: &tokio_postgres::Statement) -> PyResult<PgResult> {
    let columns = statement
        .columns()
        .iter()
        .map(|column| column.name().to_owned())
        .collect::<Vec<_>>();
    let buffered = rows
        .iter()
        .map(|row| {
            row.columns()
                .iter()
                .enumerate()
                .map(|(index, column)| row_to_value(row, index, column.type_()))
                .collect::<PyResult<Vec<_>>>()
        })
        .collect::<PyResult<Vec<_>>>()?;
    Ok(PgResult {
        columns,
        rows: buffered,
        rowcount: -1,
    })
}

fn buffer_rows_inferred(rows: &[Row]) -> PyResult<PgResult> {
    let Some(first) = rows.first() else {
        return Ok(PgResult {
            columns: Vec::new(),
            rows: Vec::new(),
            rowcount: -1,
        });
    };
    let columns = first
        .columns()
        .iter()
        .map(|column| column.name().to_owned())
        .collect::<Vec<_>>();
    let buffered = rows
        .iter()
        .map(|row| {
            row.columns()
                .iter()
                .enumerate()
                .map(|(index, column)| row_to_value(row, index, column.type_()))
                .collect::<PyResult<Vec<_>>>()
        })
        .collect::<PyResult<Vec<_>>>()?;
    Ok(PgResult {
        columns,
        rows: buffered,
        rowcount: -1,
    })
}

fn row_to_value(row: &Row, index: usize, ty: &Type) -> PyResult<PgValue> {
    let value = if *ty == Type::BOOL {
        row.try_get::<_, Option<bool>>(index)
            .map(|v| v.map(PgValue::Bool))
    } else if *ty == Type::INT2 {
        row.try_get::<_, Option<i16>>(index)
            .map(|v| v.map(|v| PgValue::Int(v.into())))
    } else if *ty == Type::INT4 {
        row.try_get::<_, Option<i32>>(index)
            .map(|v| v.map(|v| PgValue::Int(v.into())))
    } else if *ty == Type::INT8 {
        row.try_get::<_, Option<i64>>(index)
            .map(|v| v.map(PgValue::Int))
    } else if *ty == Type::FLOAT4 {
        row.try_get::<_, Option<f32>>(index)
            .map(|v| v.map(|v| PgValue::Float(v.into())))
    } else if *ty == Type::FLOAT8 {
        row.try_get::<_, Option<f64>>(index)
            .map(|v| v.map(PgValue::Float))
    } else if matches!(*ty, Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME) {
        row.try_get::<_, Option<String>>(index)
            .map(|v| v.map(PgValue::Text))
    } else if *ty == Type::BYTEA {
        row.try_get::<_, Option<Vec<u8>>>(index)
            .map(|v| v.map(PgValue::Bytes))
    } else if *ty == Type::TIMESTAMP {
        row.try_get::<_, Option<chrono::NaiveDateTime>>(index)
            .map(|v| v.map(PgValue::Timestamp))
    } else if *ty == Type::TIMESTAMPTZ {
        row.try_get::<_, Option<chrono::DateTime<chrono::Utc>>>(index)
            .map(|v| v.map(PgValue::TimestampTz))
    } else if *ty == Type::DATE {
        row.try_get::<_, Option<chrono::NaiveDate>>(index)
            .map(|v| v.map(PgValue::Date))
    } else {
        return Err(OperationalError::new_err(
            "Unsupported PostgreSQL result type in this driver slice",
        ));
    }
    .map_err(|_| OperationalError::new_err("Could not decode PostgreSQL result"))?;
    Ok(value.unwrap_or(PgValue::Null))
}

fn make_native_result(py: Python<'_>, result: PgResult) -> PyResult<NativeResult> {
    let rows = result
        .rows
        .into_iter()
        .map(|row| {
            row.into_iter()
                .map(|value| pg_value_to_python(py, value))
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

fn pg_value_to_python(py: Python<'_>, value: PgValue) -> PyResult<Py<PyAny>> {
    match value {
        PgValue::Null => Ok(py.None()),
        PgValue::Bool(value) => Ok(value.into_pyobject(py)?.to_owned().unbind().into_any()),
        PgValue::Int(value) => Ok(value.into_pyobject(py)?.to_owned().unbind().into_any()),
        PgValue::Float(value) => Ok(value.into_pyobject(py)?.to_owned().unbind().into_any()),
        PgValue::Text(value) => Ok(value.into_pyobject(py)?.to_owned().unbind().into_any()),
        PgValue::Bytes(value) => Ok(PyBytes::new(py, &value)
            .into_pyobject(py)?
            .to_owned()
            .unbind()
            .into_any()),
        PgValue::Timestamp(value) => Ok(value.into_pyobject(py)?.into_any().unbind()),
        PgValue::TimestampTz(value) => Ok(value.into_pyobject(py)?.into_any().unbind()),
        PgValue::Date(value) => Ok(value.into_pyobject(py)?.into_any().unbind()),
    }
}

fn map_pg_error(error: tokio_postgres::Error) -> PyErr {
    if let Some(database_error) = error.as_db_error() {
        let code = database_error.code().code();
        if code.starts_with("23") {
            return IntegrityError::new_err("PostgreSQL integrity constraint failed");
        }
        if code.starts_with("42") {
            return ProgrammingError::new_err("PostgreSQL statement is invalid");
        }
    }
    OperationalError::new_err("PostgreSQL operation failed")
}

#[cfg(test)]
mod tests {
    use super::scan_named_binds;

    #[test]
    fn named_scanner_skips_literals_comments_casts_and_dollar_quotes() {
        let sql = "SELECT :value, ':ignored', \" :also_ignored \", col::text -- :comment\n/* :block /* :nested */ */ $$:body$$";
        let (rendered, names) = scan_named_binds(sql).expect("valid binds");
        assert_eq!(names, ["value"]);
        assert!(rendered.contains("SELECT $1"));
        assert!(rendered.contains("col::text"));
        assert!(rendered.contains("$$:body$$"));
    }

    #[test]
    fn named_scanner_reuses_repeated_bind_index() {
        let (rendered, names) = scan_named_binds("SELECT :value + :value").expect("valid binds");
        assert_eq!(names, ["value"]);
        assert_eq!(rendered, "SELECT $1 + $1");
    }
}

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList, PyTuple};
use rusqlite::types::Value;
use std::collections::HashMap;

use crate::{ArgumentError, OperationalError, python_to_sqlite};

type CompiledExpression = (
    String,
    Py<PyDict>,
    String,
    Vec<Py<PyAny>>,
    Vec<Option<String>>,
    Vec<String>,
    Vec<Option<String>>,
);
type CompiledStatement = (
    String,
    Vec<(String, Value, Option<String>, bool)>,
    &'static str,
    Vec<Option<String>>,
);

/// A DDL column definition serialized from Python `Column` metadata. The
/// SQL type name is rendered in Rust at parse time.
#[derive(Clone)]
struct ColumnDdl {
    name: String,
    type_name: String,
    not_null: bool,
    unique: bool,
    default: Option<String>,
}

#[derive(Clone)]
enum Expr {
    Column(String, String, Option<String>),
    Literal(Value, String, Option<String>),
    Boolean(bool),
    BooleanWord(bool),
    Binary(Box<Expr>, String, Box<Expr>),
    Group(String, Vec<Expr>),
    Not(Box<Expr>),
    In(Box<Expr>, Vec<Expr>),
    NotIn(Box<Expr>, Vec<Expr>),
    IsNull(Box<Expr>, bool),
    IsScalar(Box<Expr>, Box<Expr>),
    IsNotScalar(Box<Expr>, Box<Expr>),
    Between(Box<Expr>, Box<Expr>, Box<Expr>),
    NotBetween(Box<Expr>, Box<Expr>, Box<Expr>),
    LikeEscape(Box<Expr>, Box<Expr>, String),
    NotLikeEscape(Box<Expr>, Box<Expr>, String),
    Function(String, Vec<Expr>),
    Order(Box<Expr>, bool),
    Neg(Box<Expr>),
}

#[derive(Clone)]
enum Statement {
    Select {
        columns: Vec<Expr>,
        table: Option<String>,
        predicate: Option<Expr>,
        order: Vec<Expr>,
        limit: Option<Expr>,
        offset: Option<Expr>,
    },
    Insert {
        table: String,
        values: Vec<(String, Expr)>,
    },
    Update {
        table: String,
        values: Vec<(String, Expr)>,
        predicate: Option<Expr>,
    },
    Delete {
        table: String,
        predicate: Option<Expr>,
    },
    CreateTable {
        table: String,
        columns: Vec<ColumnDdl>,
        primary_key: Vec<String>,
    },
    DropTable {
        table: String,
    },
}

/// SQLAlchemy operator precedences (``sqlalchemy.sql.operators._PRECEDENCE``).
fn precedence(operator: &str) -> u8 {
    match operator {
        "*" | "/" => 8,
        "+" | "-" => 7,
        "AND" => 3,
        "OR" => 2,
        // Comparisons, IS, IN, BETWEEN, LIKE, and NOT all bind at 5.
        _ => 5,
    }
}

/// Operators that do not parenthesize a same-operator child (SA's
/// ``_natural_self_precedent``: the associative set).
fn natural_self_precedent(operator: &str) -> bool {
    matches!(operator, "+" | "*" | "AND" | "OR")
}

/// SQLAlchemy ``operators.is_precedent``: whether a child operator needs
/// parenthesization against its parent operator. Equal precedence groups
/// unless the child is its own natural-self-precedent operator.
fn is_precedent(child: &str, parent: &str) -> bool {
    if child == parent && natural_self_precedent(child) {
        false
    } else {
        precedence(child) <= precedence(parent)
    }
}

/// Whether rendering ``expr`` produces nothing (an empty and_/or_ group).
fn renders_empty(expr: &Expr) -> bool {
    match expr {
        Expr::Group(_, items) => items.iter().all(renders_empty),
        _ => false,
    }
}

/// SQLAlchemy concatenates string-typed operands with ``||`` instead of ``+``.
fn is_string_operand(expr: &Expr) -> bool {
    match expr {
        Expr::Column(_, _, Some(tag)) | Expr::Literal(_, _, Some(tag)) => {
            matches!(tag.as_str(), "string" | "text")
        }
        _ => false,
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum Dialect {
    Sqlite,
    Postgres,
    Mysql,
}

impl Dialect {
    fn parse(name: &str) -> PyResult<Self> {
        match name {
            "sqlite" => Ok(Self::Sqlite),
            "postgresql" => Ok(Self::Postgres),
            "mysql" => Ok(Self::Mysql),
            other => Err(ArgumentError::new_err(format!(
                "Unsupported compilation dialect '{other}'"
            ))),
        }
    }

    fn is_postgres(self) -> bool {
        matches!(self, Self::Postgres)
    }

    fn is_mysql(self) -> bool {
        matches!(self, Self::Mysql)
    }
}

/// SQLAlchemy's PG dialect cast rules: integer/string/text/date/datetime
/// binds carry explicit casts; boolean, float, and binary binds do not.
fn bind_cast(type_tag: Option<&str>) -> Option<&'static str> {
    match type_tag {
        Some("integer") => Some("::INTEGER"),
        Some("string") | Some("text") => Some("::VARCHAR"),
        Some("datetime") => Some("::TIMESTAMP WITHOUT TIME ZONE"),
        Some("date") => Some("::DATE"),
        _ => None,
    }
}

/// SA infers a cast for untyped PG binds from the value itself.
fn inferred_cast(value: &Value) -> Option<&'static str> {
    match value {
        Value::Integer(_) => Some("::INTEGER"),
        Value::Text(_) => Some("::VARCHAR"),
        _ => None,
    }
}

struct Compiler {
    dialect: Dialect,
    sql: String,
    params: Vec<(String, Value, Option<String>, bool)>,
    result_types: Vec<Option<String>>,
    bind_counts: HashMap<String, usize>,
    function_counts: HashMap<String, usize>,
    column_labels: HashMap<String, usize>,
    anon_count: usize,
}

impl Compiler {
    fn new(dialect: Dialect) -> Self {
        Self {
            dialect,
            sql: String::new(),
            params: Vec::new(),
            result_types: Vec::new(),
            bind_counts: HashMap::new(),
            function_counts: HashMap::new(),
            column_labels: HashMap::new(),
            anon_count: 0,
        }
    }

    fn push_placeholder(&mut self) {
        if self.dialect.is_postgres() {
            self.sql.push('$');
            self.sql.push_str(&self.params.len().to_string());
        } else {
            self.sql.push('?');
        }
    }

    fn next_bind_name(&mut self, base: &str) -> String {
        let next = self.bind_counts.entry(base.to_owned()).or_insert(0);
        *next += 1;
        format!("{base}_{}", *next)
    }

    fn bind(&mut self, value: &Expr) {
        let (base, value, type_tag) = match value {
            Expr::Literal(value, name, type_tag) => (name.clone(), value.clone(), type_tag.clone()),
            _ => ("param".to_owned(), Value::Null, None),
        };
        let key = if let Some(name) = base.strip_prefix('=') {
            // DML values use their explicit column key.
            name.to_owned()
        } else {
            self.next_bind_name(&base)
        };
        let is_bool = type_tag.as_deref() == Some("boolean");
        let cast_suffix = if self.dialect.is_postgres() {
            bind_cast(type_tag.as_deref()).or_else(|| {
                if type_tag.is_none() {
                    inferred_cast(&value)
                } else {
                    None
                }
            })
        } else {
            None
        };
        self.params.push((key, value, type_tag, is_bool));
        self.push_placeholder();
        if let Some(cast) = cast_suffix {
            self.sql.push_str(cast);
        }
    }

    fn bind_in_values(&mut self, inner: &Expr, values: &[Expr]) {
        let base = match inner {
            Expr::Column(_, column, _) => column.as_str(),
            _ => "param",
        };
        // SQLAlchemy allocates one name for the expanding bind, then uses it
        // as the prefix for each post-compile member.
        let group = self.next_bind_name(base);
        for (index, value) in values.iter().enumerate() {
            if index > 0 {
                self.sql.push_str(", ");
            }
            if let Expr::Literal(value, _, type_tag) = value {
                let is_bool = type_tag.as_deref() == Some("boolean");
                let cast_suffix = if self.dialect.is_postgres() {
                    bind_cast(type_tag.as_deref()).or_else(|| {
                        if type_tag.is_none() {
                            inferred_cast(value)
                        } else {
                            None
                        }
                    })
                } else {
                    None
                };
                self.params.push((
                    format!("{group}_{}", index + 1),
                    value.clone(),
                    type_tag.clone(),
                    is_bool,
                ));
                self.push_placeholder();
                if let Some(cast) = cast_suffix {
                    self.sql.push_str(cast);
                }
            } else if let Expr::Boolean(value) = value {
                let type_tag = match inner {
                    Expr::Column(_, _, type_tag) => type_tag.clone(),
                    _ => None,
                };
                let cast_suffix = if self.dialect.is_postgres() {
                    bind_cast(type_tag.as_deref())
                } else {
                    None
                };
                self.params.push((
                    format!("{group}_{}", index + 1),
                    Value::Integer(i64::from(*value)),
                    type_tag,
                    true,
                ));
                self.push_placeholder();
                if let Some(cast) = cast_suffix {
                    self.sql.push_str(cast);
                }
            } else {
                self.expr(value, None);
            }
        }
    }

    fn render_escape(&mut self, escape: &str) {
        self.sql.push_str(" ESCAPE '");
        self.sql.push_str(&escape.replace('\'', "''"));
        self.sql.push('\'');
    }

    /// Render one expression. ``against`` is the parent operator (``None`` at
    /// statement level); a child self-groups like SQLAlchemy's ``self_group``.
    fn expr(&mut self, expr: &Expr, against: Option<&str>) {
        match expr {
            Expr::Column(table, column, _) => {
                if !table.is_empty() {
                    self.ident(table);
                    self.sql.push('.');
                }
                self.ident(column);
            }
            Expr::Literal(_, _, _) => self.bind(expr),
            Expr::Boolean(value) => {
                if self.dialect.is_postgres() || self.dialect.is_mysql() {
                    self.sql.push_str(if *value { "true" } else { "false" });
                } else {
                    self.sql.push_str(if *value { "1" } else { "0" });
                }
            }
            // SQLAlchemy renders a bare Python bool predicate as 1 = 1 / 0 = 1
            // on SQLite, as true/false words on PostgreSQL, and as true = 1 /
            // false = 1 on MySQL.
            Expr::BooleanWord(value) => {
                if self.dialect.is_postgres() {
                    self.sql.push_str(if *value { "true" } else { "false" });
                } else if self.dialect.is_mysql() {
                    self.sql
                        .push_str(if *value { "true = 1" } else { "false = 1" });
                } else {
                    self.sql.push_str(if *value { "1 = 1" } else { "0 = 1" });
                }
            }
            Expr::Binary(left, op, right) => {
                if op == "/" && matches!(self.dialect, Dialect::Sqlite) {
                    // SQLAlchemy's SQLite dialect rewrites true division as
                    // ``left / (right + 0.0)``; the right operand renders
                    // inside its own grouping.
                    self.render_child(left, Some(op.as_str()));
                    self.sql.push_str(" / (");
                    self.render_child(right, Some(op.as_str()));
                    self.sql.push_str(" + 0.0)");
                } else if op == "/" && self.dialect.is_postgres() {
                    // SQLAlchemy's PG dialect casts the divisor: NUMERIC for
                    // integer operands, FLOAT otherwise.
                    self.render_child(left, Some(op.as_str()));
                    self.sql.push_str(" / CAST(");
                    self.render_child(right, Some(op.as_str()));
                    let cast = match right.as_ref() {
                        Expr::Column(_, _, Some(tag)) if tag == "integer" => "NUMERIC",
                        _ => "FLOAT",
                    };
                    self.sql.push_str(" AS ");
                    self.sql.push_str(cast);
                    self.sql.push(')');
                } else if op == "+"
                    && self.dialect.is_mysql()
                    && is_string_operand(left)
                    && is_string_operand(right)
                {
                    // MySQL concatenates with the CONCAT function, not an
                    // operator.
                    self.sql.push_str("concat(");
                    self.expr(left, None);
                    self.sql.push_str(", ");
                    self.expr(right, None);
                    self.sql.push(')');
                } else {
                    let rendered_op =
                        if op == "+" && is_string_operand(left) && is_string_operand(right) {
                            "||"
                        } else {
                            op.as_str()
                        };
                    self.render_child(left, Some(op.as_str()));
                    self.sql.push(' ');
                    self.sql.push_str(rendered_op);
                    self.sql.push(' ');
                    self.render_child(right, Some(op.as_str()));
                }
            }
            Expr::Group(op, items) => {
                let parens = against.is_some_and(|parent| is_precedent(op, parent));
                if parens {
                    self.sql.push('(');
                }
                let mut emitted = false;
                for item in items {
                    // Empty and_/or_ groups flatten away like SQLAlchemy's
                    // BooleanClauseList.
                    if renders_empty(item) {
                        continue;
                    }
                    if emitted {
                        self.sql
                            .push_str(if op == "AND" { " AND " } else { " OR " });
                    }
                    self.render_child(item, Some(op.as_str()));
                    emitted = true;
                }
                if parens {
                    self.sql.push(')');
                }
            }
            Expr::Not(inner) => {
                self.sql.push_str("NOT ");
                // SQLAlchemy wraps the negated element in a Grouping whenever
                // it is not a plain column/parameter/function operand.
                let parens = matches!(
                    inner.as_ref(),
                    Expr::Group(_, _) | Expr::Binary(_, _, _) | Expr::Not(_)
                );
                if parens {
                    self.sql.push('(');
                    self.expr(inner, None);
                    self.sql.push(')');
                } else {
                    self.expr(inner, None);
                }
            }
            Expr::In(inner, values) | Expr::NotIn(inner, values) => {
                let negated = matches!(expr, Expr::NotIn(_, _));
                if negated {
                    self.sql.push('(');
                }
                self.render_child(inner, Some("IN"));
                if values.is_empty() {
                    if self.dialect.is_postgres() || self.dialect.is_mysql() {
                        // SQLAlchemy's expanding-bind empty-set rendering for
                        // PostgreSQL and MySQL.
                        if negated {
                            self.sql.push_str(" NOT IN (NULL) OR (1 = 1)");
                        } else {
                            self.sql.push_str(" IN (NULL) AND (1 != 1)");
                        }
                    } else {
                        self.sql.push_str(if negated {
                            " NOT IN (SELECT 1 FROM (SELECT 1) WHERE 1!=1)"
                        } else {
                            " IN (SELECT 1 FROM (SELECT 1) WHERE 1!=1)"
                        });
                    }
                } else {
                    self.sql
                        .push_str(if negated { " NOT IN (" } else { " IN (" });
                    self.bind_in_values(inner, values);
                    self.sql.push(')');
                }
                if negated {
                    self.sql.push(')');
                }
            }
            Expr::IsScalar(inner, value) | Expr::IsNotScalar(inner, value) => {
                self.render_child(inner, Some("IS"));
                self.sql
                    .push_str(if matches!(expr, Expr::IsNotScalar(_, _)) {
                        " IS NOT "
                    } else {
                        " IS "
                    });
                self.expr(value, None);
            }
            Expr::IsNull(inner, negated) => {
                self.render_child(inner, Some("IS"));
                self.sql
                    .push_str(if *negated { " IS NOT NULL" } else { " IS NULL" });
            }
            Expr::Between(inner, low, high) | Expr::NotBetween(inner, low, high) => {
                self.render_child(inner, Some("BETWEEN"));
                self.sql
                    .push_str(if matches!(expr, Expr::NotBetween(_, _, _)) {
                        " NOT BETWEEN "
                    } else {
                        " BETWEEN "
                    });
                self.expr(low, None);
                self.sql.push_str(" AND ");
                self.expr(high, None);
            }
            Expr::LikeEscape(inner, value, escape) | Expr::NotLikeEscape(inner, value, escape) => {
                self.render_child(inner, Some("LIKE"));
                self.sql
                    .push_str(if matches!(expr, Expr::NotLikeEscape(_, _, _)) {
                        " NOT LIKE "
                    } else {
                        " LIKE "
                    });
                self.expr(value, None);
                self.render_escape(escape);
            }
            Expr::Function(name, args) => {
                self.sql.push_str(name);
                self.sql.push('(');
                if name == "count" && args.is_empty() {
                    self.sql.push('*');
                }
                for (i, arg) in args.iter().enumerate() {
                    if i > 0 {
                        self.sql.push_str(", ");
                    }
                    self.expr(arg, None);
                }
                self.sql.push(')');
            }
            Expr::Order(inner, ascending) => {
                self.expr(inner, None);
                self.sql.push_str(if *ascending { " ASC" } else { " DESC" });
            }
            Expr::Neg(inner) => {
                self.sql.push('-');
                match inner.as_ref() {
                    Expr::Column(_, _, _) => self.expr(inner, None),
                    _ => {
                        self.sql.push('(');
                        self.expr(inner, None);
                        self.sql.push(')');
                    }
                }
            }
        }
    }

    /// Render ``expr`` as a child of a parent operator, parenthesizing it when
    /// its own operator is precedent against the parent (SQLAlchemy's
    /// ``self_group(against=...)`` behavior).
    fn render_child(&mut self, expr: &Expr, against: Option<&str>) {
        let parens = match (expr, against) {
            (Expr::Binary(_, child_op, _), Some(parent))
            | (Expr::Group(child_op, _), Some(parent)) => is_precedent(child_op, parent),
            (Expr::Not(_), Some(parent)) => is_precedent("NOT", parent),
            _ => false,
        };
        if parens {
            self.sql.push('(');
            self.expr(expr, None);
            self.sql.push(')');
        } else {
            self.expr(expr, against);
        }
    }

    fn ident(&mut self, value: &str) {
        self.sql.push_str(&quote_ident(value, self.dialect));
    }

    fn select_expr(&mut self, expr: &Expr) {
        self.expr(expr, None);
        match expr {
            // A negated plain column stays column-like: SQLAlchemy emits no label.
            Expr::Neg(inner) if matches!(inner.as_ref(), Expr::Column(_, _, _)) => {
                self.result_types.push(None);
            }
            Expr::Column(_, label, type_tag) => {
                self.result_types.push(type_tag.clone());
                let duplicate = {
                    let count = self.column_labels.entry(label.clone()).or_insert(0);
                    let duplicate = (*count > 0).then_some(*count);
                    *count += 1;
                    duplicate
                };
                if let Some(count) = duplicate {
                    self.sql.push_str(" AS ");
                    self.ident(&format!("{label}__{count}"));
                }
            }
            Expr::Function(name, _) => {
                self.result_types.push(None);
                let count = {
                    let count = self.function_counts.entry(name.clone()).or_insert(0);
                    *count += 1;
                    *count
                };
                self.sql.push_str(" AS ");
                self.ident(&format!("{name}_{count}"));
            }
            // Unlabeled non-column select items receive SQLAlchemy's anon_N
            // series with its own per-compile counter.
            _ => {
                self.result_types.push(None);
                self.anon_count += 1;
                self.sql.push_str(" AS ");
                self.ident(&format!("anon_{}", self.anon_count));
            }
        }
    }

    fn statement(mut self, statement: &Statement) -> CompiledStatement {
        let kind = match statement {
            Statement::Select {
                columns,
                table,
                predicate,
                order,
                limit,
                offset,
            } => {
                self.sql.push_str("SELECT");
                if !columns.is_empty() {
                    self.sql.push(' ');
                    for (i, column) in columns.iter().enumerate() {
                        if i > 0 {
                            self.sql.push_str(", ");
                        }
                        self.select_expr(column);
                    }
                }
                if let Some(table) = table {
                    self.sql.push_str(" \nFROM ");
                    self.ident(table);
                }
                if let Some(predicate) = predicate
                    && !renders_empty(predicate)
                {
                    self.sql.push_str(" \nWHERE ");
                    self.expr(predicate, None);
                }
                if !order.is_empty() {
                    self.sql.push_str(" ORDER BY ");
                    for (i, value) in order.iter().enumerate() {
                        if i > 0 {
                            self.sql.push_str(", ");
                        }
                        self.expr(value, None);
                    }
                }
                if limit.is_some() || offset.is_some() {
                    if self.dialect.is_postgres() {
                        // SA PG: " \nLIMIT x" with limit, "\n LIMIT ALL" when
                        // only offset is present; OFFSET joins on the same line.
                        if let Some(limit) = limit {
                            self.sql.push_str(" \n LIMIT ");
                            self.expr(limit, None);
                        } else {
                            self.sql.push_str("\n LIMIT ALL");
                        }
                        if let Some(offset) = offset {
                            self.sql.push_str(" OFFSET ");
                            self.expr(offset, None);
                        }
                    } else if self.dialect.is_mysql() {
                        // SA MySQL: "LIMIT offset, count"; an offset without a
                        // limit uses the maximum unsigned BIGINT count.
                        self.sql.push_str(" \n LIMIT ");
                        if let Some(offset) = offset {
                            self.expr(offset, None);
                            self.sql.push_str(", ");
                            if let Some(limit) = limit {
                                self.expr(limit, None);
                            } else {
                                self.sql.push_str("18446744073709551615");
                            }
                        } else if let Some(limit) = limit {
                            self.expr(limit, None);
                        }
                    } else {
                        self.sql.push_str("\n LIMIT ");
                        if let Some(limit) = limit {
                            self.expr(limit, None);
                        } else {
                            self.sql.push_str("-1");
                        }
                        self.sql.push_str(" OFFSET ");
                        if let Some(offset) = offset {
                            self.expr(offset, None);
                        } else {
                            self.sql.push('0');
                        }
                    }
                }
                "select"
            }
            Statement::Insert { table, values } => {
                self.sql.push_str("INSERT INTO ");
                self.ident(table);
                if values.is_empty() {
                    if self.dialect.is_mysql() {
                        // MySQL has no DEFAULT VALUES syntax; Python expands
                        // empty inserts to NULL binds, so this only guards a
                        // directly-serialized empty values map.
                        self.sql.push_str(" () VALUES ()");
                    } else {
                        self.sql.push_str(" DEFAULT VALUES");
                    }
                } else {
                    self.sql.push_str(" (");
                    for (i, (name, _)) in values.iter().enumerate() {
                        if i > 0 {
                            self.sql.push_str(", ");
                        }
                        self.ident(name);
                    }
                    self.sql.push_str(") VALUES (");
                    for (i, (_, value)) in values.iter().enumerate() {
                        if i > 0 {
                            self.sql.push_str(", ");
                        }
                        self.expr(value, None);
                    }
                    self.sql.push(')');
                }
                "insert"
            }
            Statement::Update {
                table,
                values,
                predicate,
            } => {
                self.sql.push_str("UPDATE ");
                self.ident(table);
                self.sql.push_str(" SET ");
                for (i, (name, value)) in values.iter().enumerate() {
                    if i > 0 {
                        self.sql.push_str(", ");
                    }
                    self.ident(name);
                    self.sql.push('=');
                    self.expr(value, None);
                }
                if let Some(predicate) = predicate {
                    self.sql.push_str(" WHERE ");
                    self.expr(predicate, None);
                }
                "update"
            }
            Statement::Delete { table, predicate } => {
                self.sql.push_str("DELETE FROM ");
                self.ident(table);
                if let Some(predicate) = predicate {
                    self.sql.push_str(" WHERE ");
                    self.expr(predicate, None);
                }
                "delete"
            }
            Statement::CreateTable {
                table,
                columns,
                primary_key,
            } => {
                let dialect = self.dialect;
                let quote = |value: &str| quote_ident(value, dialect);
                self.sql.push('\n');
                self.sql.push_str("CREATE TABLE ");
                self.ident(table);
                if columns.is_empty() {
                    self.sql.push_str(" (\n");
                } else {
                    self.sql.push_str(" (\n\t");
                }
                let mut lines: Vec<String> = columns
                    .iter()
                    .map(|column| {
                        let mut line = quote(&column.name);
                        line.push(' ');
                        // SQLAlchemy's PG dialect renders a single integer
                        // autoincrement primary key column as SERIAL.
                        if dialect.is_postgres()
                            && primary_key.len() == 1
                            && primary_key[0] == column.name
                            && column.type_name == "INTEGER"
                        {
                            line.push_str("SERIAL");
                        } else {
                            line.push_str(&column.type_name);
                        }
                        if let Some(default) = &column.default {
                            line.push_str(" DEFAULT '");
                            line.push_str(&default.replace('\'', "''"));
                            line.push('\'');
                        }
                        // MySQL renders the single integer primary key with
                        // NOT NULL AUTO_INCREMENT; the PRIMARY KEY clause is
                        // still emitted below.
                        if dialect.is_mysql()
                            && primary_key.len() == 1
                            && primary_key[0] == column.name
                            && column.type_name == "INTEGER"
                        {
                            line.push_str(" NOT NULL AUTO_INCREMENT");
                        } else if column.not_null {
                            line.push_str(" NOT NULL");
                        }
                        line
                    })
                    .collect();
                if !primary_key.is_empty() {
                    let keys = primary_key
                        .iter()
                        .map(|key| quote(key))
                        .collect::<Vec<_>>()
                        .join(", ");
                    lines.push(format!("PRIMARY KEY ({keys})"));
                }
                for column in columns.iter().filter(|column| column.unique) {
                    lines.push(format!("UNIQUE ({})", quote(&column.name)));
                }
                self.sql.push_str(&lines.join(", \n\t"));
                if lines.is_empty() {
                    self.sql.push_str(")\n\n");
                } else {
                    self.sql.push_str("\n)\n\n");
                }
                "create_table"
            }
            Statement::DropTable { table } => {
                self.sql.push('\n');
                self.sql.push_str("DROP TABLE ");
                self.ident(table);
                "drop_table"
            }
        };
        (self.sql, self.params, kind, self.result_types)
    }
}

#[cfg(test)]
fn needs_quote(value: &str) -> bool {
    needs_quote_in(value, Dialect::Sqlite)
}

/// Quote a possibly-reserved identifier: double quotes for SQLite/PostgreSQL,
/// backticks for MySQL, with doubled-quote escapes inside.
fn quote_ident(value: &str, dialect: Dialect) -> String {
    if !needs_quote_in(value, dialect) {
        return value.to_owned();
    }
    if dialect.is_mysql() {
        format!("`{}`", value.replace('`', "``"))
    } else {
        format!("\"{}\"", value.replace('"', "\"\""))
    }
}

fn needs_quote_in(value: &str, dialect: Dialect) -> bool {
    const PG_RESERVED: &[&str] = &[
        "all",
        "analyse",
        "analyze",
        "and",
        "any",
        "array",
        "as",
        "asc",
        "asymmetric",
        "authorization",
        "binary",
        "both",
        "case",
        "cast",
        "check",
        "collate",
        "collation",
        "column",
        "concurrently",
        "constraint",
        "create",
        "cross",
        "current_catalog",
        "current_date",
        "current_role",
        "current_schema",
        "current_time",
        "current_timestamp",
        "current_user",
        "default",
        "deferrable",
        "desc",
        "distinct",
        "do",
        "else",
        "end",
        "except",
        "false",
        "fetch",
        "for",
        "foreign",
        "freeze",
        "from",
        "full",
        "grant",
        "group",
        "having",
        "ilike",
        "in",
        "initially",
        "inner",
        "intersect",
        "into",
        "is",
        "isnull",
        "join",
        "lateral",
        "leading",
        "left",
        "like",
        "limit",
        "localtime",
        "localtimestamp",
        "natural",
        "not",
        "notnull",
        "null",
        "offset",
        "on",
        "only",
        "or",
        "order",
        "outer",
        "overlaps",
        "placing",
        "primary",
        "references",
        "returning",
        "right",
        "select",
        "session_user",
        "similar",
        "some",
        "symmetric",
        "table",
        "tablesample",
        "then",
        "to",
        "trailing",
        "true",
        "union",
        "unique",
        "user",
        "using",
        "variadic",
        "verbose",
        "when",
        "where",
        "window",
        "with",
    ];
    const RESERVED: &[&str] = &[
        "abort",
        "action",
        "add",
        "after",
        "all",
        "alter",
        "always",
        "analyze",
        "and",
        "as",
        "asc",
        "attach",
        "autoincrement",
        "before",
        "begin",
        "between",
        "by",
        "cascade",
        "case",
        "cast",
        "check",
        "collate",
        "column",
        "commit",
        "conflict",
        "constraint",
        "create",
        "cross",
        "current",
        "current_date",
        "current_time",
        "current_timestamp",
        "database",
        "default",
        "deferrable",
        "deferred",
        "delete",
        "desc",
        "detach",
        "distinct",
        "do",
        "drop",
        "each",
        "else",
        "end",
        "escape",
        "except",
        "exclude",
        "exclusive",
        "exists",
        "explain",
        "fail",
        "filter",
        "first",
        "following",
        "for",
        "foreign",
        "from",
        "full",
        "generated",
        "glob",
        "group",
        "groups",
        "having",
        "if",
        "ignore",
        "in",
        "index",
        "indexed",
        "initially",
        "inner",
        "insert",
        "instead",
        "intersect",
        "into",
        "is",
        "isnull",
        "join",
        "key",
        "last",
        "left",
        "like",
        "limit",
        "match",
        "materialized",
        "natural",
        "no",
        "not",
        "nothing",
        "notnull",
        "null",
        "of",
        "offset",
        "on",
        "or",
        "order",
        "others",
        "outer",
        "over",
        "partition",
        "plan",
        "pragma",
        "preceding",
        "primary",
        "query",
        "raise",
        "range",
        "recursive",
        "references",
        "regexp",
        "reindex",
        "release",
        "rename",
        "replace",
        "restrict",
        "returning",
        "right",
        "rollback",
        "row",
        "rows",
        "savepoint",
        "select",
        "set",
        "table",
        "temp",
        "temporary",
        "then",
        "ties",
        "to",
        "transaction",
        "trigger",
        "unbounded",
        "union",
        "unique",
        "update",
        "using",
        "vacuum",
        "values",
        "view",
        "virtual",
        "when",
        "where",
        "window",
        "with",
        "without",
    ];
    // SQLAlchemy's ``RESERVED_WORDS_MYSQL`` (MySQL 8.x reserved words).
    const MYSQL_RESERVED: &[&str] = &[
        "accessible",
        "add",
        "admin",
        "all",
        "alter",
        "analyze",
        "and",
        "array",
        "as",
        "asc",
        "asensitive",
        "before",
        "between",
        "bigint",
        "binary",
        "blob",
        "both",
        "by",
        "call",
        "cascade",
        "case",
        "change",
        "char",
        "character",
        "check",
        "collate",
        "column",
        "condition",
        "constraint",
        "continue",
        "convert",
        "create",
        "cross",
        "cube",
        "cume_dist",
        "current_date",
        "current_time",
        "current_timestamp",
        "current_user",
        "cursor",
        "database",
        "databases",
        "day_hour",
        "day_microsecond",
        "day_minute",
        "day_second",
        "dec",
        "decimal",
        "declare",
        "default",
        "delayed",
        "delete",
        "dense_rank",
        "desc",
        "describe",
        "deterministic",
        "distinct",
        "distinctrow",
        "div",
        "double",
        "drop",
        "dual",
        "each",
        "else",
        "elseif",
        "empty",
        "enclosed",
        "escaped",
        "except",
        "exists",
        "exit",
        "explain",
        "false",
        "fetch",
        "first_value",
        "float",
        "float4",
        "float8",
        "for",
        "force",
        "foreign",
        "from",
        "fulltext",
        "function",
        "general",
        "generated",
        "get",
        "get_master_public_key",
        "grant",
        "group",
        "grouping",
        "groups",
        "having",
        "high_priority",
        "hour_microsecond",
        "hour_minute",
        "hour_second",
        "if",
        "ignore",
        "ignore_server_ids",
        "in",
        "index",
        "infile",
        "inner",
        "inout",
        "insensitive",
        "insert",
        "int",
        "int1",
        "int2",
        "int3",
        "int4",
        "int8",
        "integer",
        "intersect",
        "interval",
        "into",
        "io_after_gtids",
        "io_before_gtids",
        "is",
        "iterate",
        "join",
        "json_table",
        "key",
        "keys",
        "kill",
        "lag",
        "last_value",
        "lateral",
        "lead",
        "leading",
        "leave",
        "left",
        "like",
        "limit",
        "linear",
        "lines",
        "load",
        "localtime",
        "localtimestamp",
        "lock",
        "long",
        "longblob",
        "longtext",
        "loop",
        "low_priority",
        "master_bind",
        "master_heartbeat_period",
        "master_ssl_verify_server_cert",
        "match",
        "maxvalue",
        "mediumblob",
        "mediumint",
        "mediumtext",
        "member",
        "middleint",
        "minute_microsecond",
        "minute_second",
        "mod",
        "modifies",
        "natural",
        "no_write_to_binlog",
        "not",
        "nth_value",
        "ntile",
        "null",
        "numeric",
        "of",
        "on",
        "optimize",
        "optimizer_costs",
        "option",
        "optionally",
        "or",
        "order",
        "out",
        "outer",
        "outfile",
        "over",
        "parallel",
        "parse_gcol_expr",
        "partition",
        "percent_rank",
        "persist",
        "persist_only",
        "precision",
        "primary",
        "procedure",
        "purge",
        "qualify",
        "range",
        "rank",
        "read",
        "read_write",
        "reads",
        "real",
        "recursive",
        "references",
        "regexp",
        "release",
        "rename",
        "repeat",
        "replace",
        "require",
        "resignal",
        "restrict",
        "return",
        "revoke",
        "right",
        "rlike",
        "role",
        "row",
        "row_number",
        "rows",
        "schema",
        "schemas",
        "second_microsecond",
        "select",
        "sensitive",
        "separator",
        "set",
        "show",
        "signal",
        "slow",
        "smallint",
        "spatial",
        "specific",
        "sql",
        "sql_after_gtids",
        "sql_before_gtids",
        "sql_big_result",
        "sql_calc_found_rows",
        "sql_small_result",
        "sqlexception",
        "sqlstate",
        "sqlwarning",
        "ssl",
        "starting",
        "stored",
        "straight_join",
        "system",
        "table",
        "terminated",
        "then",
        "tinyblob",
        "tinyint",
        "tinytext",
        "to",
        "trailing",
        "trigger",
        "true",
        "undo",
        "union",
        "unique",
        "unlock",
        "unsigned",
        "update",
        "usage",
        "use",
        "using",
        "utc_date",
        "utc_time",
        "utc_timestamp",
        "values",
        "varbinary",
        "varchar",
        "varcharacter",
        "varying",
        "virtual",
        "when",
        "where",
        "while",
        "window",
        "with",
        "write",
        "xor",
        "year_month",
        "zerofill",
    ];
    let mut chars = value.chars();
    let Some(first) = chars.next() else {
        return true;
    };
    !(first == '_' || first.is_ascii_lowercase())
        || !chars.all(|ch| ch == '_' || ch.is_ascii_lowercase() || ch.is_ascii_digit())
        || match dialect {
            Dialect::Sqlite => RESERVED.contains(&value),
            Dialect::Postgres => PG_RESERVED.contains(&value),
            Dialect::Mysql => MYSQL_RESERVED.contains(&value),
        }
}

fn seq<'py>(value: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    if let Ok(list) = value.cast::<PyList>() {
        Ok(list.iter().collect())
    } else if let Ok(tuple) = value.cast::<PyTuple>() {
        Ok(tuple.iter().collect())
    } else {
        Err(PyTypeError::new_err(
            "Expression serialization must use lists or tuples",
        ))
    }
}

fn item<'py>(value: &Bound<'py, PyAny>, index: usize) -> PyResult<Bound<'py, PyAny>> {
    value.get_item(index)
}
fn tag(value: &Bound<'_, PyAny>) -> PyResult<String> {
    item(value, 0)?.extract()
}
fn string_at(value: &Bound<'_, PyAny>, index: usize) -> PyResult<String> {
    item(value, index)?.extract()
}

fn optional_string_at(value: &Bound<'_, PyAny>, index: usize) -> PyResult<Option<String>> {
    let items = seq(value)?;
    match items.get(index) {
        Some(item) if !item.is_none() => item.extract().map(Some),
        _ => Ok(None),
    }
}

fn parse_expr(value: &Bound<'_, PyAny>) -> PyResult<Expr> {
    parse_expr_at_depth(value, 0)
}

fn parse_expr_at_depth(value: &Bound<'_, PyAny>, depth: usize) -> PyResult<Expr> {
    if depth >= 2000 {
        return Err(ArgumentError::new_err("expression nesting too deep"));
    }
    let child_depth = depth + 1;
    let values = seq(value)?;
    let name: String = values
        .first()
        .ok_or_else(|| ArgumentError::new_err("Empty expression node"))?
        .extract()?;
    match name.as_str() {
        "column" => Ok(Expr::Column(
            string_at(value, 1)?,
            string_at(value, 2)?,
            optional_string_at(value, 3)?,
        )),
        "literal" => {
            let literal = item(value, 1)?;
            let type_tag = optional_string_at(value, 3)?;
            if literal.is_instance_of::<PyBool>() && type_tag.is_none() {
                Ok(Expr::Boolean(literal.extract()?))
            } else {
                let converted = match type_tag.as_deref() {
                    Some(tag) => crate::python_to_sqlite_typed(&literal, Some(tag))?,
                    None => python_to_sqlite(&literal)?,
                };
                Ok(Expr::Literal(converted, string_at(value, 2)?, type_tag))
            }
        }
        "boolean_word" => Ok(Expr::BooleanWord(item(value, 1)?.extract()?)),
        "binary" => {
            let left = parse_expr_at_depth(&item(value, 1)?, child_depth)?;
            let operator = string_at(value, 2)?;
            let right = parse_expr_at_depth(&item(value, 3)?, child_depth)?;
            // SQLAlchemy's SQLite Boolean comparator renders typed boolean
            // constants inline as 1/0 rather than allocating a bind.
            let right = match right {
                Expr::Literal(Value::Integer(value), _, Some(tag))
                    if tag == "boolean" && matches!(value, 0 | 1) =>
                {
                    Expr::Boolean(value != 0)
                }
                other => other,
            };
            Ok(Expr::Binary(Box::new(left), operator, Box::new(right)))
        }
        "and" | "or" => Ok(Expr::Group(
            if name == "and" { "AND" } else { "OR" }.to_owned(),
            seq(&item(value, 1)?)?
                .iter()
                .map(|child| parse_expr_at_depth(child, child_depth))
                .collect::<PyResult<Vec<_>>>()?,
        )),
        "not" => Ok(Expr::Not(Box::new(parse_expr_at_depth(
            &item(value, 1)?,
            child_depth,
        )?))),
        "in" | "not_in" => {
            let left = Box::new(parse_expr_at_depth(&item(value, 1)?, child_depth)?);
            let members = seq(&item(value, 2)?)?
                .iter()
                .map(|child| parse_expr_at_depth(child, child_depth))
                .collect::<PyResult<Vec<_>>>()?;
            if name == "in" {
                Ok(Expr::In(left, members))
            } else {
                Ok(Expr::NotIn(left, members))
            }
        }
        "is" | "is_not" => Ok(Expr::IsNull(
            Box::new(parse_expr_at_depth(&item(value, 1)?, child_depth)?),
            name == "is_not",
        )),
        "is_scalar" | "is_not_scalar" => {
            let left = Box::new(parse_expr_at_depth(&item(value, 1)?, child_depth)?);
            let right = Box::new(parse_expr_at_depth(&item(value, 2)?, child_depth)?);
            // IS TRUE/FALSE is a grammar production, not a bind: typed boolean
            // literals render inline like the binary-operator arm.
            let right = Box::new(match *right {
                Expr::Literal(Value::Integer(value), _, Some(tag))
                    if tag == "boolean" && matches!(value, 0 | 1) =>
                {
                    Expr::Boolean(value != 0)
                }
                other => other,
            });
            if name == "is_scalar" {
                Ok(Expr::IsScalar(left, right))
            } else {
                Ok(Expr::IsNotScalar(left, right))
            }
        }
        "like_escape" | "not_like_escape" => {
            let left = Box::new(parse_expr_at_depth(&item(value, 1)?, child_depth)?);
            let right = Box::new(parse_expr_at_depth(&item(value, 2)?, child_depth)?);
            let escape = string_at(value, 3)?;
            if name == "like_escape" {
                Ok(Expr::LikeEscape(left, right, escape))
            } else {
                Ok(Expr::NotLikeEscape(left, right, escape))
            }
        }
        "between" | "not_between" => {
            let left = Box::new(parse_expr_at_depth(&item(value, 1)?, child_depth)?);
            let low = Box::new(parse_expr_at_depth(&item(value, 2)?, child_depth)?);
            let high = Box::new(parse_expr_at_depth(&item(value, 3)?, child_depth)?);
            if name == "between" {
                Ok(Expr::Between(left, low, high))
            } else {
                Ok(Expr::NotBetween(left, low, high))
            }
        }
        "function" => Ok(Expr::Function(
            string_at(value, 1)?,
            seq(&item(value, 2)?)?
                .iter()
                .map(|child| parse_expr_at_depth(child, child_depth))
                .collect::<PyResult<Vec<_>>>()?,
        )),
        "order" => Ok(Expr::Order(
            Box::new(parse_expr_at_depth(&item(value, 1)?, child_depth)?),
            item(value, 2)?.extract()?,
        )),
        "neg" => Ok(Expr::Neg(Box::new(parse_expr_at_depth(
            &item(value, 1)?,
            child_depth,
        )?))),
        _ => Err(ArgumentError::new_err(format!(
            "Unknown expression node '{name}'"
        ))),
    }
}

fn parse_values(value: &Bound<'_, PyAny>) -> PyResult<Vec<(String, Expr)>> {
    let dict = value.cast::<PyDict>()?;
    let mut result = Vec::new();
    for (key, value) in dict.iter() {
        result.push((key.extract()?, parse_expr(&value)?));
    }
    Ok(result)
}

/// Map a Python type-marker tag to its DDL type name (SQLAlchemy's type
/// compiler rendering for the equivalent type in the target dialect).
fn render_type_name(tag: &str, length: Option<u64>, dialect: Dialect) -> PyResult<String> {
    if dialect.is_postgres() {
        return match tag {
            "integer" => Ok("INTEGER".to_owned()),
            "string" => {
                Ok(length.map_or_else(|| "VARCHAR".to_owned(), |size| format!("VARCHAR({size})")))
            }
            "text" => Ok("TEXT".to_owned()),
            "boolean" => Ok("BOOLEAN".to_owned()),
            "float" => Ok("FLOAT".to_owned()),
            "double" => Ok("DOUBLE PRECISION".to_owned()),
            "datetime" => Ok("TIMESTAMP WITHOUT TIME ZONE".to_owned()),
            "date" => Ok("DATE".to_owned()),
            "binary" => Ok("BYTEA".to_owned()),
            other => Err(ArgumentError::new_err(format!(
                "Unsupported DDL column type '{other}'"
            ))),
        };
    }
    if dialect.is_mysql() {
        return match tag {
            "integer" => Ok("INTEGER".to_owned()),
            "string" => {
                Ok(length.map_or_else(|| "VARCHAR".to_owned(), |size| format!("VARCHAR({size})")))
            }
            "text" => Ok("TEXT".to_owned()),
            "boolean" => Ok("BOOL".to_owned()),
            "float" => Ok("FLOAT".to_owned()),
            "double" => Ok("DOUBLE".to_owned()),
            "datetime" => Ok("DATETIME".to_owned()),
            "date" => Ok("DATE".to_owned()),
            "binary" => Ok("BLOB".to_owned()),
            other => Err(ArgumentError::new_err(format!(
                "Unsupported DDL column type '{other}'"
            ))),
        };
    }
    match tag {
        "integer" => Ok("INTEGER".to_owned()),
        "string" => {
            Ok(length.map_or_else(|| "VARCHAR".to_owned(), |size| format!("VARCHAR({size})")))
        }
        "text" => Ok("TEXT".to_owned()),
        "boolean" => Ok("BOOLEAN".to_owned()),
        "float" => Ok("FLOAT".to_owned()),
        "double" => Ok("DOUBLE".to_owned()),
        "datetime" => Ok("DATETIME".to_owned()),
        "date" => Ok("DATE".to_owned()),
        "binary" => Ok("BLOB".to_owned()),
        other => Err(ArgumentError::new_err(format!(
            "Unsupported DDL column type '{other}'"
        ))),
    }
}

fn parse_column_def(value: &Bound<'_, PyAny>, dialect: Dialect) -> PyResult<ColumnDdl> {
    let name = string_at(value, 0)?;
    let type_tag = string_at(value, 1)?;
    let length_value = item(value, 2)?;
    let length = if length_value.is_none() {
        None
    } else {
        Some(length_value.extract::<u64>()?)
    };
    let not_null = item(value, 3)?.extract()?;
    let unique = item(value, 4)?.extract()?;
    let default_value = item(value, 5)?;
    let default = if default_value.is_none() {
        None
    } else {
        Some(default_value.extract::<String>()?)
    };
    Ok(ColumnDdl {
        name,
        type_name: render_type_name(&type_tag, length, dialect)?,
        not_null,
        unique,
        default,
    })
}

fn parse_statement(value: &Bound<'_, PyAny>, dialect: Dialect) -> PyResult<Statement> {
    let name = tag(value)?;
    match name.as_str() {
        "select" => {
            let columns = seq(&item(value, 1)?)?
                .iter()
                .map(parse_expr)
                .collect::<PyResult<Vec<_>>>()?;
            let table_value = item(value, 2)?;
            let table = if table_value.is_none() {
                None
            } else {
                Some(table_value.extract()?)
            };
            let predicate_value = item(value, 3)?;
            let predicate = if predicate_value.is_none() {
                None
            } else {
                Some(parse_expr(&predicate_value)?)
            };
            let order = seq(&item(value, 4)?)?
                .iter()
                .map(parse_expr)
                .collect::<PyResult<Vec<_>>>()?;
            let limit_value = item(value, 5)?;
            let limit = if limit_value.is_none() {
                None
            } else {
                Some(parse_expr(&limit_value)?)
            };
            let offset_value = item(value, 6)?;
            let offset = if offset_value.is_none() {
                None
            } else {
                Some(parse_expr(&offset_value)?)
            };
            Ok(Statement::Select {
                columns,
                table,
                predicate,
                order,
                limit,
                offset,
            })
        }
        "insert" => Ok(Statement::Insert {
            table: string_at(value, 1)?,
            values: parse_values(&item(value, 2)?)?,
        }),
        "update" => {
            let predicate = item(value, 3)?;
            Ok(Statement::Update {
                table: string_at(value, 1)?,
                values: parse_values(&item(value, 2)?)?,
                predicate: if predicate.is_none() {
                    None
                } else {
                    Some(parse_expr(&predicate)?)
                },
            })
        }
        "delete" => {
            let predicate = item(value, 2)?;
            Ok(Statement::Delete {
                table: string_at(value, 1)?,
                predicate: if predicate.is_none() {
                    None
                } else {
                    Some(parse_expr(&predicate)?)
                },
            })
        }
        "create_table" => {
            let columns = seq(&item(value, 2)?)?
                .iter()
                .map(|column| parse_column_def(column, dialect))
                .collect::<PyResult<Vec<_>>>()?;
            let primary_key = seq(&item(value, 3)?)?
                .iter()
                .map(|key| key.extract())
                .collect::<PyResult<Vec<_>>>()?;
            Ok(Statement::CreateTable {
                table: string_at(value, 1)?,
                columns,
                primary_key,
            })
        }
        "drop_table" => Ok(Statement::DropTable {
            table: string_at(value, 1)?,
        }),
        _ => Err(ArgumentError::new_err(format!(
            "Unknown statement '{name}'"
        ))),
    }
}

#[pyfunction]
#[pyo3(signature = (value, dialect="sqlite"))]
pub(crate) fn compile_expression(
    value: &Bound<'_, PyAny>,
    dialect: &str,
    py: Python<'_>,
) -> PyResult<CompiledExpression> {
    let dialect = Dialect::parse(dialect)?;
    // Parsing recurses once per serialized node and each debug frame is
    // large; on the default stack the depth guard cannot fire before the
    // stack is exhausted. Parse and render on a dedicated thread whose stack
    // is sized well beyond the guard's worst case.
    let owned = value.clone().unbind();
    let (sql, values, kind, result_types) = py.detach(move || -> PyResult<CompiledStatement> {
        // The GIL is released by detach, so the worker can re-attach for
        // the Python-side parse and render outside it.
        std::thread::Builder::new()
            .stack_size(64 * 1024 * 1024)
            .spawn(move || -> PyResult<CompiledStatement> {
                let statement = Python::attach(|py| parse_statement(owned.bind(py), dialect))?;
                Ok(Compiler::new(dialect).statement(&statement))
            })
            .map_err(|error| {
                OperationalError::new_err(format!("Expression compiler thread failed: {error}"))
            })?
            .join()
            .map_err(|_| OperationalError::new_err("Expression compiler thread panicked"))?
    })?;
    let params = PyDict::new(py);
    let mut ordered = Vec::with_capacity(values.len());
    let mut bind_types = Vec::with_capacity(values.len());
    let mut bind_names = Vec::with_capacity(values.len());
    for (key, value, type_tag, is_bool) in values {
        let converted = if dialect.is_postgres() && is_bool && matches!(value, Value::Integer(_)) {
            // PG keeps boolean bind values as Python bools (SQLite stores 0/1).
            let flag = matches!(value, Value::Integer(1));
            flag.into_pyobject(py)?.to_owned().unbind().into_any()
        } else {
            crate::sqlite_to_python(py, value)?
        };
        params.set_item(&key, converted.clone_ref(py))?;
        ordered.push(converted);
        bind_names.push(key);
        bind_types.push(type_tag);
    }
    Ok((
        sql,
        params.unbind(),
        kind.to_owned(),
        ordered,
        bind_types,
        bind_names,
        result_types,
    ))
}

#[cfg(test)]
mod tests {
    use super::{is_precedent, natural_self_precedent, needs_quote, precedence, renders_empty};

    #[test]
    fn identifier_rules_quote_reserved_and_mixed_case() {
        assert!(!needs_quote("simple_name2"));
        assert!(needs_quote("MixedCase"));
        assert!(needs_quote("order"));
        assert!(needs_quote("has-dash"));
    }

    #[test]
    fn precedence_matches_sqlalchemy_operator_table() {
        assert_eq!(precedence("*"), 8);
        assert_eq!(precedence("/"), 8);
        assert_eq!(precedence("+"), 7);
        assert_eq!(precedence("-"), 7);
        assert_eq!(precedence("="), 5);
        assert_eq!(precedence("AND"), 3);
        assert_eq!(precedence("OR"), 2);
    }

    #[test]
    fn self_precedent_matches_sqlalchemy() {
        // Associative operators do not parenthesize same-operator children.
        assert!(!is_precedent("+", "+"));
        assert!(!is_precedent("*", "*"));
        assert!(!is_precedent("AND", "AND"));
        // Subtraction and division do (SQLAlchemy left-associativity).
        assert!(is_precedent("-", "-"));
        assert!(is_precedent("/", "/"));
        // Equal precedence with a different operator still groups.
        assert!(is_precedent("*", "/"));
        assert!(is_precedent("+", "-"));
        // Tighter children of looser parents group; the reverse does not.
        assert!(is_precedent("OR", "AND"));
        assert!(!is_precedent("AND", "OR"));
        assert!(!is_precedent("+", "="));
        assert!(!is_precedent("*", "+"));
    }

    #[test]
    fn natural_self_precedent_covers_associative_operators() {
        assert!(natural_self_precedent("+"));
        assert!(natural_self_precedent("*"));
        assert!(natural_self_precedent("AND"));
        assert!(natural_self_precedent("OR"));
        assert!(!natural_self_precedent("-"));
        assert!(!natural_self_precedent("/"));
    }

    #[test]
    fn empty_boolean_groups_render_empty() {
        assert!(renders_empty(&super::Expr::Group(
            "AND".to_owned(),
            Vec::new()
        )));
        assert!(renders_empty(&super::Expr::Group(
            "OR".to_owned(),
            vec![super::Expr::Group("AND".to_owned(), Vec::new())]
        )));
        assert!(!renders_empty(&super::Expr::BooleanWord(true)));
    }
}

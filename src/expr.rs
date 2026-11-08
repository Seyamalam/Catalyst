use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList, PyTuple};
use rusqlite::types::Value;
use std::collections::HashMap;

use crate::{ArgumentError, python_to_sqlite};

type CompiledExpression = (String, Py<PyDict>, String, Vec<Py<PyAny>>);

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
    Column(String, String),
    Literal(Value, String),
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

/// Quote an identifier with double quotes when it is not a bare lowercase
/// SQLite identifier.
pub(crate) fn quote_identifier(value: &str) -> String {
    if needs_quote(value) {
        format!("\"{}\"", value.replace('"', "\"\""))
    } else {
        value.to_owned()
    }
}

struct Compiler {
    sql: String,
    params: Vec<(String, Value)>,
    bind_counts: HashMap<String, usize>,
    function_counts: HashMap<String, usize>,
    column_labels: HashMap<String, usize>,
    anon_count: usize,
}

impl Compiler {
    fn new() -> Self {
        Self {
            sql: String::new(),
            params: Vec::new(),
            bind_counts: HashMap::new(),
            function_counts: HashMap::new(),
            column_labels: HashMap::new(),
            anon_count: 0,
        }
    }

    fn next_bind_name(&mut self, base: &str) -> String {
        let next = self.bind_counts.entry(base.to_owned()).or_insert(0);
        *next += 1;
        format!("{base}_{}", *next)
    }

    fn bind(&mut self, value: &Expr) {
        let (base, value) = match value {
            Expr::Literal(value, name) => (name.clone(), value.clone()),
            _ => ("param".to_owned(), Value::Null),
        };
        let key = if let Some(name) = base.strip_prefix('=') {
            // DML values use their explicit column key.
            name.to_owned()
        } else {
            self.next_bind_name(&base)
        };
        self.params.push((key, value));
        self.sql.push('?');
    }

    fn bind_in_values(&mut self, inner: &Expr, values: &[Expr]) {
        let base = match inner {
            Expr::Column(_, column) => column.as_str(),
            _ => "param",
        };
        // SQLAlchemy allocates one name for the expanding bind, then uses it
        // as the prefix for each post-compile member.
        let group = self.next_bind_name(base);
        for (index, value) in values.iter().enumerate() {
            if index > 0 {
                self.sql.push_str(", ");
            }
            if let Expr::Literal(value, _) = value {
                self.params
                    .push((format!("{group}_{}", index + 1), value.clone()));
                self.sql.push('?');
            } else if let Expr::Boolean(value) = value {
                self.params.push((
                    format!("{group}_{}", index + 1),
                    Value::Integer(i64::from(*value)),
                ));
                self.sql.push('?');
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
            Expr::Column(table, column) => {
                if !table.is_empty() {
                    self.ident(table);
                    self.sql.push('.');
                }
                self.ident(column);
            }
            Expr::Literal(_, _) => self.bind(expr),
            Expr::Boolean(value) => self.sql.push_str(if *value { "1" } else { "0" }),
            // SQLAlchemy renders a bare Python bool predicate as 1 = 1 / 0 = 1.
            Expr::BooleanWord(value) => {
                self.sql.push_str(if *value { "1 = 1" } else { "0 = 1" });
            }
            Expr::Binary(left, op, right) => {
                if op == "/" {
                    // SQLAlchemy's SQLite dialect rewrites true division as
                    // ``left / (right + 0.0)``; the right operand renders
                    // inside its own grouping.
                    self.render_child(left, Some(op.as_str()));
                    self.sql.push_str(" / (");
                    self.render_child(right, Some(op.as_str()));
                    self.sql.push_str(" + 0.0)");
                } else {
                    self.render_child(left, Some(op.as_str()));
                    self.sql.push(' ');
                    self.sql.push_str(op);
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
                    self.sql.push_str(if negated {
                        " NOT IN (SELECT 1 FROM (SELECT 1) WHERE 1!=1)"
                    } else {
                        " IN (SELECT 1 FROM (SELECT 1) WHERE 1!=1)"
                    });
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
                    Expr::Column(_, _) => self.expr(inner, None),
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
        if needs_quote(value) {
            self.sql.push('"');
            self.sql.push_str(&value.replace('"', "\"\""));
            self.sql.push('"');
        } else {
            self.sql.push_str(value);
        }
    }

    fn select_expr(&mut self, expr: &Expr) {
        self.expr(expr, None);
        match expr {
            // A negated plain column stays column-like: SQLAlchemy emits no label.
            Expr::Neg(inner) if matches!(inner.as_ref(), Expr::Column(_, _)) => {}
            Expr::Column(_, label) => {
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
                self.anon_count += 1;
                self.sql.push_str(" AS ");
                self.ident(&format!("anon_{}", self.anon_count));
            }
        }
    }

    fn statement(mut self, statement: &Statement) -> (String, Vec<(String, Value)>, &'static str) {
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
                "select"
            }
            Statement::Insert { table, values } => {
                self.sql.push_str("INSERT INTO ");
                self.ident(table);
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
                        let mut line = quote_identifier(&column.name);
                        line.push(' ');
                        line.push_str(&column.type_name);
                        if let Some(default) = &column.default {
                            line.push_str(" DEFAULT '");
                            line.push_str(&default.replace('\'', "''"));
                            line.push('\'');
                        }
                        if column.not_null {
                            line.push_str(" NOT NULL");
                        }
                        line
                    })
                    .collect();
                if !primary_key.is_empty() {
                    let keys = primary_key
                        .iter()
                        .map(|key| quote_identifier(key))
                        .collect::<Vec<_>>()
                        .join(", ");
                    lines.push(format!("PRIMARY KEY ({keys})"));
                }
                for column in columns.iter().filter(|column| column.unique) {
                    lines.push(format!("UNIQUE ({})", quote_identifier(&column.name)));
                }
                self.sql.push_str(&lines.join(", \n\t"));
                self.sql.push_str("\n)\n\n");
                "create_table"
            }
            Statement::DropTable { table } => {
                self.sql.push('\n');
                self.sql.push_str("DROP TABLE ");
                self.ident(table);
                "drop_table"
            }
        };
        (self.sql, self.params, kind)
    }
}

fn needs_quote(value: &str) -> bool {
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
    let mut chars = value.chars();
    let Some(first) = chars.next() else {
        return true;
    };
    !(first == '_' || first.is_ascii_lowercase())
        || !chars.all(|ch| ch == '_' || ch.is_ascii_lowercase() || ch.is_ascii_digit())
        || RESERVED.contains(&value)
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
        "column" => Ok(Expr::Column(string_at(value, 1)?, string_at(value, 2)?)),
        "literal" => {
            let literal = item(value, 1)?;
            if literal.is_instance_of::<PyBool>() {
                Ok(Expr::Boolean(literal.extract()?))
            } else {
                Ok(Expr::Literal(
                    python_to_sqlite(&literal)?,
                    string_at(value, 2)?,
                ))
            }
        }
        "boolean_word" => Ok(Expr::BooleanWord(item(value, 1)?.extract()?)),
        "binary" => Ok(Expr::Binary(
            Box::new(parse_expr_at_depth(&item(value, 1)?, child_depth)?),
            string_at(value, 2)?,
            Box::new(parse_expr_at_depth(&item(value, 3)?, child_depth)?),
        )),
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

/// Map a Python type-marker tag to its SQLite DDL type name (SQLAlchemy's
/// SQLite type compiler rendering for the equivalent type).
fn render_type_name(tag: &str, length: Option<u64>) -> PyResult<String> {
    match tag {
        "integer" => Ok("INTEGER".to_owned()),
        "string" => {
            Ok(length.map_or_else(|| "VARCHAR".to_owned(), |size| format!("VARCHAR({size})")))
        }
        "text" => Ok("TEXT".to_owned()),
        "boolean" => Ok("BOOLEAN".to_owned()),
        "float" => Ok("FLOAT".to_owned()),
        "datetime" => Ok("DATETIME".to_owned()),
        other => Err(ArgumentError::new_err(format!(
            "Unsupported DDL column type '{other}'"
        ))),
    }
}

fn parse_column_def(value: &Bound<'_, PyAny>) -> PyResult<ColumnDdl> {
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
        type_name: render_type_name(&type_tag, length)?,
        not_null,
        unique,
        default,
    })
}

fn parse_statement(value: &Bound<'_, PyAny>) -> PyResult<Statement> {
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
                .map(parse_column_def)
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
pub(crate) fn compile_expression(
    value: &Bound<'_, PyAny>,
    py: Python<'_>,
) -> PyResult<CompiledExpression> {
    let statement = parse_statement(value)?;
    let (sql, values, kind) = py.detach(move || Compiler::new().statement(&statement));
    let params = PyDict::new(py);
    let mut ordered = Vec::with_capacity(values.len());
    for (key, value) in values {
        let converted = crate::sqlite_to_python(py, value)?;
        params.set_item(key, converted.clone_ref(py))?;
        ordered.push(converted);
    }
    Ok((sql, params.unbind(), kind.to_owned(), ordered))
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

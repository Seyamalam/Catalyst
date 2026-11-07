# Parity status

A `done` status requires a passing test that compares Catalyst with SQLAlchemy behavior. Unimplemented items remain `not started`; partial support must describe the uncovered cases. Status is not an implementation estimate.

## Initial status

At the foundation baseline, exception names and SQLite text execution plus a minimal synchronous engine facade are partial, tested slices; no pooling, transactions, query AST, dialect compilation, or ORM parity is claimed. As the suite grows, generate status from explicit test metadata/results to prevent stale claims.

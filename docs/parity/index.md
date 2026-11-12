# Parity status

Catalyst targets SQLAlchemy 2.0 compatibility. These matrices are the source of truth for feature coverage; `MEMORY.md` tracks milestone history and `docs/*-ground-truth.md` records the empirically verified SQLAlchemy 2.1.8 behavior each slice was built against.

## Rules

- `done` requires a passing proving test that compares Catalyst with SQLAlchemy (SQL text, bind values, results, rowcounts, or exception classes/attributes). Implementation alone never earns `done`.
- `partial` must name the uncovered cases in the Notes column — which clauses, drivers, options, or edge behaviors remain unproven or divergent.
- `not started` and `won't support` rows state the reason (missing API surface, deferred slice, deliberate divergence).
- Rows whose only evidence is implementation (no test) are `partial` at best, with the gap named.
- Live-database rows cite the suite that runs against `CATALYST_POSTGRES_URL`/`CATALYST_MYSQL_URL` (see `scripts/integration.sh`); sqlite-only rows run in the default suite.
- Driver/dialect differences that are verified but intentionally not reproduced are documented in each file's "Known gaps and documented differences" section.

## Summary

| Domain | File | done | partial | not started | won't support |
| --- | --- | --- | --- | --- | --- |
| Core engine, expressions, DDL, transactions, exceptions | [core.md](core.md) | 0 | 12 | 6 | 0 |
| Dialects (SQLite/PostgreSQL/MySQL drivers and compilers) | [dialects.md](dialects.md) | 0 | 11 | 3 | 0 |
| Session, unit of work, bulk operations | [session.md](session.md) | 5 | 2 | 2 | 0 |
| ORM: declarative, relationships, inheritance, `MappedAsDataclass` | [orm.md](orm.md) | 5 | 3 | 12 | 0 |
| Async engine/connections/savepoints | [async.md](async.md) | 0 | 5 | 3 | 0 |
| Events (`SessionEvents`) | [events.md](events.md) | 1 | 0 | 3 | 0 |
| Types and typing (`Numeric`, PEP 681, stubs) | [types.md](types.md) | 3 | 4 | 2 | 0 |
| **Total** | | **14** | **37** | **31** | **0** |

## Headline gaps

- The entire ORM/Session surface is **SQLite-only** — no `AsyncSession`, no sync PostgreSQL/MySQL engines.
- Core expressions compile single-table statements only: no public JOIN/alias/CTE/set-op/subquery surface (ORM loaders use internal join specs).
- Relationships cover FK-inferred one-to-many/many-to-one/many-to-many with `lazy="select"`, `joinedload`, `selectinload`; custom joins, other lazy modes, other loader options, `order_by`/`collection_class`, and the association-object pattern are unimplemented.
- Inheritance covers STI and JTI; concrete-table inheritance and `with_polymorphic` are not started.
- Events cover the `SessionEvents` firing matrix only; `MapperEvents`/`InstanceEvents`/`ConnectionEvents` and the never-dispatched bulk/`do_orm_execute` hooks are gaps.

# ORM parity matrix

| SQLAlchemy feature | Status | Proving test | Notes |
| --- | --- | --- | --- |
| Declarative mappings and `Mapped` | partial | `tests/test_orm.py` | `DeclarativeBase`/`declarative_base()`, `Mapped[T]` annotations, `mapped_column`, `__table__`/`__mapper__`/`Mapper`, kwargs `__init__` with SQLAlchemy's invalid-kwarg `TypeError`, and class-level `Column` attribute access are implemented and proven. `Session.scalars`/`Session.execute` hydrate `select(Entity)` results into instances in one batched Rust call (`NativeResult.hydrate` allocates via `cls.__new__` and assigns attributes per column, matching SQLAlchemy instrumentation), returning `[(entity,)]` rows and `keys() == (Entity,)`. Core `Connection.execute(select(Entity))` returns raw tuples and `scalars()` returns first-column values, matching SQLAlchemy Core. Relationships, inheritance, `MappedAsDataclass`, `registry`, and `relationship()` are not implemented; hydration is eager over the buffered result rather than per-row lazy. |
| Relationships and eager/lazy loading | not started | — | No ORM or loader API. |
| Unit of Work and identity map | not started | — | No session tracking. |

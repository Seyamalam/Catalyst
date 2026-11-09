# Session parity matrix

| SQLAlchemy feature | Status | Proving test | Notes |
| --- | --- | --- | --- |
| `Session` query and persistence behavior | partial | `tests/test_orm.py` | `Session(bind)` wraps an `Engine`/`Connection` and exposes `execute`, `scalars`, `close`, and context-manager use. `scalars(select(Entity))` returns hydrated mapped instances matching SQLAlchemy's `Session.scalars` attribute values; `execute` returns `(entity,)` rows with `keys() == (Entity,)`; `one`/`one_or_none`/`first` raise or return like SQLAlchemy with `NoResultFound`/`MultipleResultsFound`. `add`/`add_all`, `flush`, `commit`, `rollback`, `get`, `refresh`, `delete`, identity map, and dirty tracking are not implemented. |
| Flush, commit, and rollback | not started | — | Transactions are not implemented. |
| Savepoints | not started | — | `Connection.begin_nested` covers connection-level savepoints (see core/async matrices); `Session.begin_nested` is not implemented. |

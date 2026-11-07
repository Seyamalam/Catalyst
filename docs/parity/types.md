# Type parity matrix

| SQLAlchemy feature | Status | Proving test | Notes |
| --- | --- | --- | --- |
| `Mapped[T]` and typed declarative columns | not started | — | Mapping API and inference tests are not implemented. |
| Accurate stubs for the native extension error API | partial | `python -m mypy.stubtest catalyst` | Error classes pass stubtest and strict type checks; no mapping or expression typing API exists. |
| PEP 681 `dataclass_transform` | not started | — | Declarative mapping is not implemented. |

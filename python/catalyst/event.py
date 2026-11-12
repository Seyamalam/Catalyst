"""Session event registry — mirrors ``sqlalchemy.event`` for ``SessionEvents``.

Listeners attach to the :class:`Session` class (firing for every session) or
to a single session instance, exactly like SQLAlchemy's ``event.listen``:
``event.listen(Session, "after_commit", fn)``.  ``SessionEvents`` itself is
also accepted as a target and registers against the ``Session`` base class,
mirroring SQLAlchemy where the interface class stands in for ``Session``.

Registration lives in a weak-keyed registry keyed by class, plus a per-session
dict for instance-level listeners; dispatch is Python-side at the unit-of-work
seams (the Rust ``NativeUow`` stays pure bookkeeping).
"""

from __future__ import annotations

import weakref
from collections.abc import Callable
from typing import Any, cast

from ._catalyst import InvalidRequestError

# Dispatch intentionally reaches ``Session`` internals (``_transaction``,
# ``_closed``) — same-module friend access, mirroring how SQLAlchemy wires
# ``SessionEvents`` into ``Session``.
# pyright: reportPrivateUsage=false

_Listener = Callable[..., Any]

# Every SessionEvents hook SQLAlchemy 2.1 accepts for a Session target.
# ``after_bulk_update``/``after_bulk_delete`` are legacy ``Query.update()`` /
# ``Query.delete()`` hooks — accepted here for parity but never dispatched
# (Catalyst has no legacy Query API, and 2.0-style ORM DML does not fire them
# in SQLAlchemy either).  ``do_orm_execute`` is likewise accepted but not yet
# dispatched.
_SESSION_EVENT_NAMES = frozenset(
    {
        # attach
        "before_attach",
        "after_attach",
        # flush
        "before_flush",
        "after_flush",
        "after_flush_postexec",
        # commit / rollback
        "before_commit",
        "after_commit",
        "after_rollback",
        "after_soft_rollback",
        # transaction lifecycle
        "after_begin",
        "after_transaction_create",
        "after_transaction_end",
        # state transitions
        "transient_to_pending",
        "pending_to_transient",
        "pending_to_persistent",
        "detached_to_persistent",
        "persistent_to_transient",
        "persistent_to_detached",
        "persistent_to_deleted",
        "deleted_to_persistent",
        "deleted_to_detached",
        "loaded_as_persistent",
        # bulk / execution (accepted, never dispatched — see module docstring)
        "after_bulk_update",
        "after_bulk_delete",
        "do_orm_execute",
    }
)


class SessionEvents:
    """Interface class naming every ``Session`` event hook.

    Used purely as an ``event.listen`` target (it registers against the
    ``Session`` base class like SQLAlchemy); the hooks themselves are
    dispatched by :class:`Session` internals.
    """


# Class-level listener registry: mapped class -> {event name: [listeners]}.
# Weak keys so subclass-targeted registrations die with the class.
_CLASS_LISTENERS: weakref.WeakKeyDictionary[type[object], dict[str, list[_Listener]]] = (
    weakref.WeakKeyDictionary()
)


def _resolve_target(target: object) -> tuple[type[object] | None, object | None]:
    """Split ``target`` into ``(class, instance)`` — exactly one is non-None."""
    from .orm import Session

    if target is SessionEvents:
        return Session, None
    if isinstance(target, type) and issubclass(target, Session):
        return target, None
    if isinstance(target, Session):
        return None, target
    return None, None


def _check_target_and_name(target: object, name: str) -> None:
    if _resolve_target(target) == (None, None):
        raise InvalidRequestError(f"No such event '{name}' for target '{target}'")
    if name not in _SESSION_EVENT_NAMES:
        raise InvalidRequestError(f"No such event '{name}' for target '{target}'")


def _registry_for(target: object) -> dict[str, list[_Listener]]:
    cls, instance = _resolve_target(target)
    if cls is not None:
        return _CLASS_LISTENERS.setdefault(cls, {})
    listeners: dict[str, list[_Listener]] | None = getattr(instance, "_event_listeners", None)
    if listeners is None:
        listeners = {}
        setattr(instance, "_event_listeners", listeners)  # noqa: B010
    return listeners


def listen(target: object, name: str, fn: _Listener) -> None:
    """Register ``fn`` for session event ``name`` on class or instance."""
    _check_target_and_name(target, name)
    _registry_for(target).setdefault(name, []).append(fn)


def listens_for(target: object, name: str) -> Callable[[_Listener], _Listener]:
    """Decorator form of :func:`listen`."""

    def _decorator(fn: _Listener) -> _Listener:
        listen(target, name, fn)
        return fn

    return _decorator


def contains(target: object, name: str, fn: _Listener) -> bool:
    """Whether ``fn`` is registered for ``name`` on ``target``."""
    _check_target_and_name(target, name)
    return fn in _registry_for(target).get(name, ())


def remove(target: object, name: str, fn: _Listener) -> None:
    """Unregister ``fn``; raises like SQLAlchemy when it was not registered."""
    _check_target_and_name(target, name)
    registry = _registry_for(target)
    listeners = registry.get(name)
    if not listeners or fn not in listeners:
        raise InvalidRequestError(f"No listeners found for event {target} / '{name}' / {fn}")
    listeners.remove(fn)


def _listeners_for(session: object, name: str) -> list[_Listener]:
    """All listeners for ``name`` on ``session`` — class-level, then instance.

    SQLAlchemy propagates class-level listeners down the hierarchy at
    registration time, so a listener on ``Session`` lands *before* listeners
    later registered on a subclass: dispatch walks the MRO base-first.
    """
    fns: list[_Listener] = []
    for cls in reversed(type(session).__mro__):
        registry = _CLASS_LISTENERS.get(cls)
        if registry is not None:
            fns.extend(registry.get(name, ()))
    instance_registry = getattr(session, "_event_listeners", None)
    if isinstance(instance_registry, dict):
        fns.extend(cast("dict[str, list[_Listener]]", instance_registry).get(name, ()))
    return fns


def _dispatch_for(  # pyright: ignore[reportUnusedFunction]
    session: object, name: str, *args: object
) -> None:
    """Invoke every ``name`` listener on ``session`` in registration order."""
    for fn in _listeners_for(session, name):
        fn(*args)

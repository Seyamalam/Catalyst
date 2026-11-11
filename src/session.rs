//! Session unit-of-work and identity map, owned natively per `AGENTS.md`.
//!
//! [`NativeUow`] is the per-Session registry: it tracks every managed
//! instance's status (pending/persistent/deleted), the set-on-write dirty
//! attribute set, the expired attribute set, the stored primary key used for
//! refreshes, and the identity map keyed by `(class pointer, primary key)`.
//! Python orchestrates SQLAlchemy API semantics (autoflush, expire flags,
//! exception types) on top; all bookkeeping state lives here so a single FFI
//! call can transition or reconcile whole batches.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::{HashMap, HashSet};

pub(crate) const PENDING: u8 = 1;
pub(crate) const PERSISTENT: u8 = 2;
pub(crate) const DELETED: u8 = 3;

/// Identity-map bucket key: `(type(obj) pointer, hash(pk))`.
type IdentityKey = (usize, isize);
/// One collision bucket entry: the stored primary key and the instance.
type IdentityEntry = (Py<PyAny>, Py<PyAny>);

/// Per-instance bookkeeping keyed by the object's pointer identity.
struct ObjectState {
    /// Strong handle to the managed instance (kept so pointer keys stay valid
    /// and `session.new`/`dirty`/`deleted` can return live objects).
    obj: Py<PyAny>,
    /// `type(obj)` pointer, used to key the identity map.
    class_ptr: usize,
    /// Mapped attribute names in table order (expire sets copy this).
    attrs: Vec<String>,
    /// Persisted primary key used for refresh/expiry loads.
    pk: Option<Py<PyAny>>,
    /// Insertion sequence for deterministic collection ordering.
    seq: usize,
    status: u8,
    /// Inserted during the current transaction: rollback detaches rather than
    /// expires these objects (they keep their Python-side values).
    new_in_txn: bool,
    /// Set-on-write dirty attributes (reverting a value stays dirty).
    dirty_attrs: HashSet<String>,
    /// Attributes expired by commit/rollback/`expire`; loaded on next access.
    expired_attrs: HashSet<String>,
}

/// The unit-of-work registry backing one Python `Session`.
#[pyclass(module = "catalyst._catalyst")]
pub(crate) struct NativeUow {
    /// `weakref.ref(session)` — the Session owns this registry, so the
    /// back-reference must not keep it alive.
    session: Py<PyAny>,
    /// Managed objects keyed by pointer identity (`id(obj)`).
    states: HashMap<usize, ObjectState>,
    /// Identity map: `(class pointer, hash(pk))` -> collision bucket of
    /// `(pk, instance)` pairs compared with `==`.
    identity: HashMap<IdentityKey, Vec<IdentityEntry>>,
    next_seq: usize,
}

fn object_id(obj: &Bound<'_, PyAny>) -> usize {
    obj.as_ptr() as usize
}

#[pymethods]
impl NativeUow {
    #[new]
    fn new(session: &Bound<'_, PyAny>, py: Python<'_>) -> PyResult<Self> {
        let weakref = py
            .import("weakref")?
            .getattr("ref")?
            .call1((session,))?
            .unbind();
        Ok(Self {
            session: weakref,
            states: HashMap::new(),
            identity: HashMap::new(),
            next_seq: 0,
        })
    }

    /// The owning `Session` (through the weak reference), or `None`.
    #[getter]
    fn session(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(self.session.bind(py).call0()?.unbind())
    }

    fn attach_pending(
        &mut self,
        obj: &Bound<'_, PyAny>,
        cls: &Bound<'_, PyAny>,
        attrs: Vec<String>,
    ) {
        self.next_seq += 1;
        self.states.insert(
            object_id(obj),
            ObjectState {
                obj: obj.clone().unbind(),
                class_ptr: cls.as_ptr() as usize,
                attrs,
                pk: None,
                seq: self.next_seq,
                status: PENDING,
                new_in_txn: true,
                dirty_attrs: HashSet::new(),
                expired_attrs: HashSet::new(),
            },
        );
    }

    /// Register a freshly loaded (persistent) instance and its identity entry.
    fn attach_persistent(
        &mut self,
        obj: &Bound<'_, PyAny>,
        cls: &Bound<'_, PyAny>,
        attrs: Vec<String>,
        pk: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        self.next_seq += 1;
        self.states.insert(
            object_id(obj),
            ObjectState {
                obj: obj.clone().unbind(),
                class_ptr: cls.as_ptr() as usize,
                attrs,
                pk: Some(pk.clone().unbind()),
                seq: self.next_seq,
                status: PERSISTENT,
                new_in_txn: false,
                dirty_attrs: HashSet::new(),
                expired_attrs: HashSet::new(),
            },
        );
        self.identity
            .entry((cls.as_ptr() as usize, pk.hash()?))
            .or_default()
            .push((pk.clone().unbind(), obj.clone().unbind()));
        Ok(())
    }

    /// Object status: 0 unmanaged, 1 pending, 2 persistent, 3 deleted.
    fn status(&self, obj: &Bound<'_, PyAny>) -> i64 {
        self.states
            .get(&object_id(obj))
            .map(|state| i64::from(state.status))
            .unwrap_or(0)
    }

    /// Whether the object is tracked by this session (any non-detached state).
    fn is_managed(&self, obj: &Bound<'_, PyAny>) -> bool {
        self.states.contains_key(&object_id(obj))
    }

    /// Set-on-write interception: persistent objects record the attribute in
    /// the dirty set (even when the value reverts); any recorded expiry for
    /// that attribute is cleared because the write re-loads it.
    fn attr_set(&mut self, obj: &Bound<'_, PyAny>, name: &str) {
        if let Some(state) = self.states.get_mut(&object_id(obj)) {
            if state.status == PERSISTENT {
                state.dirty_attrs.insert(name.to_owned());
            }
            state.expired_attrs.remove(name);
        }
    }

    /// Whether `name` on `obj` is currently expired (lazy-load pending).
    fn attr_expired(&self, obj: &Bound<'_, PyAny>, name: &str) -> bool {
        self.states
            .get(&object_id(obj))
            .map(|state| state.expired_attrs.contains(name))
            .unwrap_or(false)
    }

    /// The persisted primary key stored for this instance, if any.
    fn pk_of(&self, obj: &Bound<'_, PyAny>, py: Python<'_>) -> Option<Py<PyAny>> {
        self.states
            .get(&object_id(obj))
            .and_then(|state| state.pk.as_ref().map(|pk| pk.clone_ref(py)))
    }

    /// SQLAlchemy `Session.is_modified`: pending objects count as modified;
    /// persistent objects only when the dirty set is non-empty.
    fn is_modified(&self, obj: &Bound<'_, PyAny>) -> bool {
        match self.states.get(&object_id(obj)) {
            Some(state) if state.status == PENDING => true,
            Some(state) if state.status == PERSISTENT => !state.dirty_attrs.is_empty(),
            _ => false,
        }
    }

    /// Pending instances in add order.
    fn new_objects(&self, py: Python<'_>) -> Vec<Py<PyAny>> {
        self.ordered(py, |state| state.status == PENDING)
    }

    /// Persistent instances with a non-empty dirty set, in add order.
    fn dirty_objects(&self, py: Python<'_>) -> Vec<Py<PyAny>> {
        self.ordered(py, |state| {
            state.status == PERSISTENT && !state.dirty_attrs.is_empty()
        })
    }

    /// Instances marked for deletion, in add order.
    fn deleted_objects(&self, py: Python<'_>) -> Vec<Py<PyAny>> {
        self.ordered(py, |state| state.status == DELETED)
    }

    /// Attribute names recorded dirty for `obj` (drives UPDATE column lists).
    fn dirty_attrs_of(&self, obj: &Bound<'_, PyAny>) -> Vec<String> {
        self.states
            .get(&object_id(obj))
            .map(|state| state.dirty_attrs.iter().cloned().collect())
            .unwrap_or_default()
    }

    /// Attribute names currently expired for `obj`.
    fn expired_attrs_of(&self, obj: &Bound<'_, PyAny>) -> Vec<String> {
        self.states
            .get(&object_id(obj))
            .map(|state| state.expired_attrs.iter().cloned().collect())
            .unwrap_or_default()
    }

    /// Identity-map lookup: same `(class, pk)` returns the same instance.
    /// Entries marked deleted are skipped — SQLAlchemy drops them from the
    /// identity map when the delete is registered.
    fn identity_get(
        &self,
        cls: &Bound<'_, PyAny>,
        pk: &Bound<'_, PyAny>,
        py: Python<'_>,
    ) -> PyResult<Option<Py<PyAny>>> {
        let key = (cls.as_ptr() as usize, pk.hash()?);
        if let Some(entries) = self.identity.get(&key) {
            for (stored_pk, obj) in entries {
                let deleted = self
                    .states
                    .get(&(obj.as_ptr() as usize))
                    .map(|state| state.status == DELETED)
                    .unwrap_or(false);
                if !deleted && stored_pk.bind(py).eq(pk)? {
                    return Ok(Some(obj.clone_ref(py)));
                }
            }
        }
        Ok(None)
    }

    /// Whether a flush would emit any statements.
    fn has_changes(&self) -> bool {
        self.states.values().any(|state| {
            state.status == PENDING
                || state.status == DELETED
                || (state.status == PERSISTENT && !state.dirty_attrs.is_empty())
        })
    }

    /// Expire every attribute of `obj` (next access reloads by stored pk).
    fn expire(&mut self, obj: &Bound<'_, PyAny>) {
        if let Some(state) = self.states.get_mut(&object_id(obj)) {
            state.expired_attrs = state.attrs.iter().cloned().collect();
            state.dirty_attrs.clear();
        }
    }

    /// Expire all persistent instances; returns them so Python can drop the
    /// now-stale `__dict__` entries that drive lazy reload.
    fn expire_all(&mut self, py: Python<'_>) -> Vec<Py<PyAny>> {
        let mut expired = Vec::new();
        for state in self.states.values_mut() {
            if state.status == PERSISTENT {
                state.expired_attrs = state.attrs.iter().cloned().collect();
                state.dirty_attrs.clear();
                expired.push(state.obj.clone_ref(py));
            }
        }
        expired
    }

    /// Clear the expired set after a successful load.
    fn mark_loaded(&mut self, obj: &Bound<'_, PyAny>) {
        if let Some(state) = self.states.get_mut(&object_id(obj)) {
            state.expired_attrs.clear();
        }
    }

    /// Clear one attribute's expired flag after a lazy load populated it.
    fn attr_loaded(&mut self, obj: &Bound<'_, PyAny>, name: &str) {
        if let Some(state) = self.states.get_mut(&object_id(obj)) {
            state.expired_attrs.remove(name);
        }
    }

    /// Clear dirty and expired sets (post-flush clean, post-refresh).
    fn mark_clean(&mut self, obj: &Bound<'_, PyAny>) {
        if let Some(state) = self.states.get_mut(&object_id(obj)) {
            state.dirty_attrs.clear();
            state.expired_attrs.clear();
        }
    }

    /// Mark a persistent instance for deletion.
    fn mark_deleted(&mut self, obj: &Bound<'_, PyAny>) {
        if let Some(state) = self.states.get_mut(&object_id(obj)) {
            state.status = DELETED;
            state.dirty_attrs.clear();
        }
    }

    /// Re-`add` of an object marked deleted cancels the pending delete.
    fn un_delete(&mut self, obj: &Bound<'_, PyAny>) {
        if let Some(state) = self.states.get_mut(&object_id(obj))
            && state.status == DELETED
        {
            state.status = PERSISTENT;
        }
    }

    /// Successful commit: flushed inserts are no longer new-in-transaction.
    fn commit_transition(&mut self) {
        for state in self.states.values_mut() {
            state.new_in_txn = false;
        }
    }

    /// Rollback semantics: objects inserted this transaction are detached
    /// entirely (keeping their Python-side attribute values like SQLAlchemy);
    /// everything else is expired so the next access reloads committed state.
    /// Returns the expired objects for `__dict__` clearing.
    fn rollback(&mut self, py: Python<'_>) -> PyResult<Vec<Py<PyAny>>> {
        let mut detached = Vec::new();
        let mut expired = Vec::new();
        for (id, state) in self.states.iter_mut() {
            if state.status == PENDING || state.new_in_txn {
                detached.push(*id);
            } else {
                state.expired_attrs = state.attrs.iter().cloned().collect();
                state.dirty_attrs.clear();
                state.status = PERSISTENT;
                expired.push(state.obj.clone_ref(py));
            }
        }
        for id in detached {
            if let Some(state) = self.states.remove(&id) {
                self.identity_remove(py, state.class_ptr, state.pk.as_ref())?;
            }
        }
        Ok(expired)
    }

    /// Set the expired set to exactly `names` (used when re-attaching a
    /// detached object whose expired attribute set must be preserved).
    fn expire_attrs(&mut self, obj: &Bound<'_, PyAny>, names: Vec<String>) {
        if let Some(state) = self.states.get_mut(&object_id(obj)) {
            state.expired_attrs = names.into_iter().collect();
        }
    }

    /// Drop one object's state and identity entry entirely (SQLAlchemy
    /// detach/expunge semantics); returns `(pk, status, dirty, expired)` so
    /// Python can stamp the detached-object marker that preserves
    /// `state.modified`/`state.key` semantics, or `None` when unmanaged.
    #[allow(clippy::type_complexity)]
    fn detach(
        &mut self,
        obj: &Bound<'_, PyAny>,
        py: Python<'_>,
    ) -> PyResult<Option<(Option<Py<PyAny>>, u8, Vec<String>, Vec<String>)>> {
        match self.states.remove(&object_id(obj)) {
            Some(state) => {
                self.identity_remove(py, state.class_ptr, state.pk.as_ref())?;
                let dirty = state.dirty_attrs.iter().cloned().collect();
                let expired = state.expired_attrs.iter().cloned().collect();
                Ok(Some((state.pk, state.status, dirty, expired)))
            }
            None => Ok(None),
        }
    }

    /// Detach every managed object in one crossing (expunge_all/close):
    /// returns `(obj, pk, status, dirty, expired)` per object so Python can
    /// stamp detached markers.
    #[allow(clippy::type_complexity)]
    fn detach_all(&mut self) -> Vec<(Py<PyAny>, Option<Py<PyAny>>, u8, Vec<String>, Vec<String>)> {
        let states = self
            .states
            .drain()
            .map(|(_, state)| state)
            .collect::<Vec<_>>();
        self.identity.clear();
        states
            .into_iter()
            .map(|state| {
                (
                    state.obj,
                    state.pk,
                    state.status,
                    state.dirty_attrs.into_iter().collect(),
                    state.expired_attrs.into_iter().collect(),
                )
            })
            .collect()
    }

    /// Managed persistent instances of `cls`, sequence ordered — the candidate
    /// set ORM DML `synchronize_session` evaluates against.
    fn persistent_objects(&self, py: Python<'_>, cls: &Bound<'_, PyAny>) -> Vec<Py<PyAny>> {
        let class_ptr = cls.as_ptr() as usize;
        self.ordered(py, |state| {
            state.class_ptr == class_ptr && state.status == PERSISTENT
        })
    }

    /// Drop all tracking (Session.close releases the whole registry).
    fn clear(&mut self) {
        self.states.clear();
        self.identity.clear();
    }

    /// Apply post-flush transitions in one call: `(obj, pk)` pending inserts
    /// become persistent identity entries, dirty objects become clean, and
    /// flushed deletes drop their state and identity entry.
    fn flush_transition(
        &mut self,
        persisted: Vec<(Bound<'_, PyAny>, Bound<'_, PyAny>)>,
        cleaned: Vec<Bound<'_, PyAny>>,
        dropped: Vec<Bound<'_, PyAny>>,
        py: Python<'_>,
    ) -> PyResult<()> {
        for (obj, pk) in &persisted {
            let Some(state) = self.states.get_mut(&object_id(obj)) else {
                continue;
            };
            state.status = PERSISTENT;
            state.dirty_attrs.clear();
            state.expired_attrs.clear();
            if pk.is_none() {
                state.pk = None;
                continue;
            }
            state.pk = Some(pk.clone().unbind());
            let key = (state.class_ptr, pk.hash()?);
            self.identity
                .entry(key)
                .or_default()
                .push((pk.clone().unbind(), state.obj.clone_ref(py)));
        }
        for obj in &cleaned {
            if let Some(state) = self.states.get_mut(&object_id(obj)) {
                // Only the flushed write is reconciled: attributes that were
                // expired before the flush stay expired so the next access
                // still lazy-reloads committed state.
                state.dirty_attrs.clear();
            }
        }
        for obj in &dropped {
            if let Some(state) = self.states.remove(&object_id(obj)) {
                self.identity_remove(py, state.class_ptr, state.pk.as_ref())?;
            }
        }
        Ok(())
    }

    /// Reconcile a batch of freshly hydrated instances with the identity map:
    /// already-mapped primary keys return the existing instance (SQLAlchemy
    /// identity semantics); new ones are registered persistent and receive the
    /// `_catalyst_state` handle, all in one FFI crossing.
    fn reconcile(
        slf: Py<Self>,
        py: Python<'_>,
        cls: &Bound<'_, PyAny>,
        pk_attr: &str,
        attrs: Vec<String>,
        instances: &Bound<'_, PyList>,
    ) -> PyResult<Vec<Py<PyAny>>> {
        let _ = cls; // identity keys derive from each instance's own class
        let mut out = Vec::with_capacity(instances.len());
        for instance in instances.iter() {
            let pk = instance.getattr(pk_attr)?;
            // Key the identity map by the *resolved* class — polymorphic
            // hydration can return subclasses of the queried entity (STI).
            let class_ptr = instance.get_type().as_ptr() as usize;
            let key = (class_ptr, pk.hash()?);
            let existing = {
                let uow = slf.borrow(py);
                let mut found = None;
                if let Some(entries) = uow.identity.get(&key) {
                    for (stored_pk, obj) in entries {
                        if stored_pk.bind(py).eq(&pk)? {
                            found = Some(obj.clone_ref(py));
                            break;
                        }
                    }
                }
                found
            };
            match existing {
                Some(existing) => {
                    // A row load populates expired attributes on the
                    // already-mapped instance — SQLAlchemy refreshes expired
                    // state from the row; unexpired (possibly dirty) values
                    // are left untouched. ``__dict__`` is written directly so
                    // mapped-class ``__setattr__`` dirty tracking doesn't fire
                    // (a refresh isn't a user mutation) and no re-borrow is
                    // needed mid-write.
                    let names: Vec<String> = {
                        let uow = slf.borrow(py);
                        uow.states
                            .get(&(existing.as_ptr() as usize))
                            .map(|state| state.expired_attrs.iter().cloned().collect())
                            .unwrap_or_default()
                    };
                    if !names.is_empty() {
                        let dict = existing.bind(py).getattr("__dict__")?;
                        let dict = dict.cast::<PyDict>()?;
                        let mut refreshed: Vec<String> = Vec::new();
                        for name in names {
                            if let Ok(value) = instance.getattr(name.as_str()) {
                                dict.set_item(name.as_str(), value)?;
                                refreshed.push(name);
                            }
                        }
                        if !refreshed.is_empty() {
                            let mut uow = slf.borrow_mut(py);
                            if let Some(state) = uow.states.get_mut(&(existing.as_ptr() as usize)) {
                                for name in refreshed {
                                    state.expired_attrs.remove(&name);
                                }
                            }
                        }
                    }
                    out.push(existing)
                }
                None => {
                    {
                        let mut uow = slf.borrow_mut(py);
                        uow.next_seq += 1;
                        let seq = uow.next_seq;
                        uow.states.insert(
                            instance.as_ptr() as usize,
                            ObjectState {
                                obj: instance.clone().unbind(),
                                class_ptr,
                                attrs: attrs.clone(),
                                pk: Some(pk.clone().unbind()),
                                seq,
                                status: PERSISTENT,
                                new_in_txn: false,
                                dirty_attrs: HashSet::new(),
                                expired_attrs: HashSet::new(),
                            },
                        );
                        uow.identity
                            .entry(key)
                            .or_default()
                            .push((pk.clone().unbind(), instance.clone().unbind()));
                    }
                    instance.setattr("_catalyst_state", slf.clone_ref(py))?;
                    out.push(instance.unbind());
                }
            }
        }
        Ok(out)
    }
}

impl NativeUow {
    fn ordered(&self, py: Python<'_>, predicate: impl Fn(&ObjectState) -> bool) -> Vec<Py<PyAny>> {
        let mut states = self
            .states
            .values()
            .filter(|state| predicate(state))
            .collect::<Vec<_>>();
        states.sort_by_key(|state| state.seq);
        states.iter().map(|state| state.obj.clone_ref(py)).collect()
    }

    fn identity_remove(
        &mut self,
        py: Python<'_>,
        class_ptr: usize,
        pk: Option<&Py<PyAny>>,
    ) -> PyResult<()> {
        let Some(pk) = pk else {
            return Ok(());
        };
        let key = (class_ptr, pk.bind(py).hash()?);
        if let Some(entries) = self.identity.get_mut(&key) {
            entries.retain(|(stored_pk, _)| stored_pk.bind(py).ne(pk).unwrap_or(true));
            if entries.is_empty() {
                self.identity.remove(&key);
            }
        }
        Ok(())
    }
}

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

/// Session-transaction snapshot frame (SQLAlchemy `SessionTransaction._new`/
/// `_dirty`/`_deleted` equivalents). One frame per boundary transaction —
/// the session root transaction and every savepoint — pushed at creation
/// and merged upward on `RELEASE` or restored on `ROLLBACK TO`.
struct UowSnapshot {
    /// Objects attached pending (or flushed persistent) inside the span —
    /// `ROLLBACK TO` expunges them to transient rather than expiring.
    new_ids: HashSet<usize>,
    /// Persistent objects mutated inside the span — `ROLLBACK TO` expires
    /// them so the next access reloads pre-savepoint values.
    dirty_ids: HashSet<usize>,
    /// Objects marked deleted inside the span (`id` -> instance) — restored
    /// to persistent on rollback, and reported on commit so Python can fire
    /// `deleted_to_detached`.
    deleted: HashMap<usize, Py<PyAny>>,
    /// States dropped by a flushed delete inside the span, kept so a
    /// savepoint rollback can resurrect the object (status + identity).
    dropped: HashMap<usize, ObjectState>,
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
    /// Savepoint/session-transaction snapshot stack, innermost last.
    snapshots: Vec<UowSnapshot>,
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
            snapshots: Vec::new(),
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
        let id = object_id(obj);
        self.states.insert(
            id,
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
        if let Some(frame) = self.snapshots.last_mut() {
            frame.new_ids.insert(id);
        }
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
        let id = object_id(obj);
        if let Some(state) = self.states.get_mut(&id) {
            if state.status == PERSISTENT {
                state.dirty_attrs.insert(name.to_owned());
                if let Some(frame) = self.snapshots.last_mut() {
                    frame.dirty_ids.insert(id);
                }
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
        let id = object_id(obj);
        if let Some(state) = self.states.get_mut(&id) {
            state.status = DELETED;
            state.dirty_attrs.clear();
            if let Some(frame) = self.snapshots.last_mut() {
                frame.deleted.insert(id, obj.clone().unbind());
            }
        }
    }

    /// Re-`add` of an object marked deleted cancels the pending delete.
    fn un_delete(&mut self, obj: &Bound<'_, PyAny>) {
        let id = object_id(obj);
        if let Some(state) = self.states.get_mut(&id)
            && state.status == DELETED
        {
            state.status = PERSISTENT;
            for frame in self.snapshots.iter_mut() {
                frame.deleted.remove(&id);
            }
        }
    }

    /// Successful commit: flushed inserts are no longer new-in-transaction.
    /// Committed deletes leave the registry (SQLAlchemy detaches them after
    /// `after_commit`); returns `(obj, pk)` pairs so Python can stamp the
    /// detached marker and fire `deleted_to_detached` for each.
    #[allow(clippy::type_complexity)]
    fn commit_transition(
        &mut self,
        py: Python<'_>,
    ) -> PyResult<Vec<(Py<PyAny>, Option<Py<PyAny>>)>> {
        for state in self.states.values_mut() {
            state.new_in_txn = false;
        }
        let mut deleted: HashMap<usize, (Py<PyAny>, Option<Py<PyAny>>)> = HashMap::new();
        let frames: Vec<UowSnapshot> = self.snapshots.drain(..).collect();
        for frame in frames {
            for (id, obj) in frame.deleted {
                if let Some(state) = self.states.remove(&id) {
                    self.identity_remove(py, state.class_ptr, state.pk.as_ref())?;
                    deleted.entry(id).or_insert((obj, state.pk));
                }
            }
            for (id, state) in frame.dropped {
                deleted.entry(id).or_insert((state.obj, state.pk));
            }
        }
        Ok(deleted.into_values().collect())
    }

    /// Rollback semantics: objects inserted this transaction are detached
    /// entirely (keeping their Python-side attribute values like SQLAlchemy);
    /// flushed deletes are resurrected; everything else is expired so the
    /// next access reloads committed state.
    /// Returns `(expired, expunged, resurrected)` — expunged pairs each
    /// object with its pre-detach status so Python can fire the correct
    /// `pending_to_transient`/`persistent_to_transient` event, and
    /// resurrected objects fire `deleted_to_persistent`.
    #[allow(clippy::type_complexity)]
    fn rollback(
        &mut self,
        py: Python<'_>,
    ) -> PyResult<(Vec<Py<PyAny>>, Vec<(Py<PyAny>, u8)>, Vec<Py<PyAny>>)> {
        // Savepoint-dropped states (flushed deletes) live in the frames;
        // resurrecting them undoes the delete like SQLAlchemy.
        let mut dropped: HashMap<usize, ObjectState> = HashMap::new();
        let frames: Vec<UowSnapshot> = self.snapshots.drain(..).collect();
        for frame in frames {
            for (id, state) in frame.dropped {
                dropped.entry(id).or_insert(state);
            }
        }
        let mut detached: Vec<(usize, usize)> = Vec::new();
        let mut expired = Vec::new();
        let mut resurrected = Vec::new();
        for (id, state) in self.states.iter_mut() {
            if state.status == PENDING || state.new_in_txn {
                detached.push((state.seq, *id));
            } else {
                if state.status == DELETED {
                    resurrected.push(state.obj.clone_ref(py));
                }
                state.expired_attrs = state.attrs.iter().cloned().collect();
                state.dirty_attrs.clear();
                state.status = PERSISTENT;
                expired.push(state.obj.clone_ref(py));
            }
        }
        detached.sort_unstable();
        let mut expunged = Vec::with_capacity(detached.len());
        for (_, id) in detached {
            if let Some(state) = self.states.remove(&id) {
                self.identity_remove(py, state.class_ptr, state.pk.as_ref())?;
                expunged.push((state.obj.clone_ref(py), state.status));
            }
        }
        let mut resurrect_dropped: Vec<(usize, usize)> =
            dropped.iter().map(|(id, state)| (state.seq, *id)).collect();
        resurrect_dropped.sort_unstable();
        for (_, id) in resurrect_dropped {
            let Some(mut state) = dropped.remove(&id) else {
                continue;
            };
            state.status = PERSISTENT;
            state.dirty_attrs.clear();
            if let Some(pk) = state.pk.as_ref() {
                let key = (state.class_ptr, pk.bind(py).hash()?);
                self.identity
                    .entry(key)
                    .or_default()
                    .push((pk.clone_ref(py), state.obj.clone_ref(py)));
            }
            resurrected.push(state.obj.clone_ref(py));
            expired.push(state.obj.clone_ref(py));
            self.states.insert(id, state);
        }
        Ok((expired, expunged, resurrected))
    }

    /// Push a session-transaction snapshot frame (`Session.begin`,
    /// `begin_nested`, autobegin). Recording is piggy-backed onto the
    /// existing bookkeeping seams: `attach_pending`/`flush_transition`
    /// fill `new_ids`, `attr_set` fills `dirty_ids`, `mark_deleted` fills
    /// `deleted`, and flushed deletes preserve their state in `dropped`.
    fn snapshot_begin(&mut self) {
        self.snapshots.push(UowSnapshot {
            new_ids: HashSet::new(),
            dirty_ids: HashSet::new(),
            deleted: HashMap::new(),
            dropped: HashMap::new(),
        });
    }

    /// `RELEASE SAVEPOINT`: fold the top frame into its parent so an outer
    /// rollback still sees the inner span's mutations. Returns the deleted
    /// objects only when the root frame was popped (transaction commit) so
    /// Python can fire `deleted_to_detached`.
    fn snapshot_commit(&mut self) -> Vec<Py<PyAny>> {
        let Some(frame) = self.snapshots.pop() else {
            return Vec::new();
        };
        if let Some(parent) = self.snapshots.last_mut() {
            parent.new_ids.extend(frame.new_ids);
            parent.dirty_ids.extend(frame.dirty_ids);
            parent.deleted.extend(frame.deleted);
            parent.dropped.extend(frame.dropped);
            Vec::new()
        } else {
            frame.deleted.into_values().collect()
        }
    }

    /// `ROLLBACK TO SAVEPOINT`: restore the top frame. Objects attached or
    /// flushed inside the span are expunged to transient; flushed deletes
    /// and marked deletes are resurrected to persistent; persistent objects
    /// mutated inside the span are expired (reload restores pre-savepoint
    /// values). Returns `(expunged, resurrected, expired)`; expunged pairs
    /// each object with its pre-detach status for event dispatch.
    #[allow(clippy::type_complexity)]
    fn snapshot_rollback(
        &mut self,
        py: Python<'_>,
    ) -> PyResult<(Vec<(Py<PyAny>, u8)>, Vec<Py<PyAny>>, Vec<Py<PyAny>>)> {
        let Some(mut frame) = self.snapshots.pop() else {
            return Ok((Vec::new(), Vec::new(), Vec::new()));
        };
        // `trans._new ∪ session._new`: attached/flushed inside the span, or
        // still pending now.
        let mut to_expunge = std::mem::take(&mut frame.new_ids);
        for (id, state) in self.states.iter() {
            if state.status == PENDING {
                to_expunge.insert(*id);
            }
        }
        let mut ordered: Vec<(usize, usize)> = to_expunge
            .iter()
            .map(|id| {
                (
                    self.states
                        .get(id)
                        .map(|state| state.seq)
                        .unwrap_or(usize::MAX),
                    *id,
                )
            })
            .collect();
        ordered.sort_unstable();
        let mut expunged = Vec::new();
        for (_, id) in ordered {
            if let Some(state) = self.states.remove(&id) {
                self.identity_remove(py, state.class_ptr, state.pk.as_ref())?;
                expunged.push((state.obj.clone_ref(py), state.status));
            }
        }
        // `trans._deleted ∪ session._deleted`: marked deletes revert in
        // place; flushed deletes resurrect from the captured state.
        let mut resurrect: Vec<(usize, usize)> = Vec::new();
        for id in frame.deleted.keys() {
            if let Some(state) = self.states.get(id)
                && state.status == DELETED
            {
                resurrect.push((state.seq, *id));
            }
        }
        for (id, state) in self.states.iter() {
            if state.status == DELETED {
                resurrect.push((state.seq, *id));
            }
        }
        for (id, state) in frame.dropped.iter() {
            resurrect.push((state.seq, *id));
        }
        resurrect.sort_unstable();
        resurrect.dedup_by_key(|(_, id)| *id);
        let mut resurrected = Vec::new();
        for (_, id) in resurrect {
            if let Some(state) = self.states.get_mut(&id) {
                if state.status == DELETED {
                    state.status = PERSISTENT;
                    state.dirty_attrs.clear();
                    resurrected.push(state.obj.clone_ref(py));
                }
            } else if let Some(mut state) = frame.dropped.remove(&id) {
                state.status = PERSISTENT;
                state.dirty_attrs.clear();
                if let Some(pk) = state.pk.as_ref() {
                    let key = (state.class_ptr, pk.bind(py).hash()?);
                    self.identity
                        .entry(key)
                        .or_default()
                        .push((pk.clone_ref(py), state.obj.clone_ref(py)));
                }
                resurrected.push(state.obj.clone_ref(py));
                self.states.insert(id, state);
            }
        }
        let mut ordered_dirty: Vec<(usize, usize)> = frame
            .dirty_ids
            .iter()
            .map(|id| {
                (
                    self.states
                        .get(id)
                        .map(|state| state.seq)
                        .unwrap_or(usize::MAX),
                    *id,
                )
            })
            .collect();
        ordered_dirty.sort_unstable();
        let mut expired = Vec::new();
        for (_, id) in ordered_dirty {
            if let Some(state) = self.states.get_mut(&id)
                && state.status == PERSISTENT
            {
                state.expired_attrs = state.attrs.iter().cloned().collect();
                state.dirty_attrs.clear();
                expired.push(state.obj.clone_ref(py));
            }
        }
        Ok((expunged, resurrected, expired))
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
        let id = object_id(obj);
        match self.states.remove(&id) {
            Some(state) => {
                self.identity_remove(py, state.class_ptr, state.pk.as_ref())?;
                // An expunged marked-delete leaves the snapshot deleted set
                // (SQLAlchemy pops it from `session._deleted`/`trans._deleted`
                // too, so the commit event never sees it).
                for frame in self.snapshots.iter_mut() {
                    frame.deleted.remove(&id);
                }
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
        self.snapshots.clear();
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
        self.snapshots.clear();
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
            let id = object_id(obj);
            let Some(state) = self.states.get_mut(&id) else {
                continue;
            };
            state.status = PERSISTENT;
            state.dirty_attrs.clear();
            state.expired_attrs.clear();
            if let Some(frame) = self.snapshots.last_mut() {
                frame.new_ids.insert(id);
            }
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
            let id = object_id(obj);
            if let Some(state) = self.states.remove(&id) {
                self.identity_remove(py, state.class_ptr, state.pk.as_ref())?;
                if let Some(frame) = self.snapshots.last_mut() {
                    frame.dropped.insert(id, state);
                }
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
                        // Only refresh attributes the fresh row actually
                        // carried: a parent-table row resolved to a subclass
                        // leaves subclass-table columns unfetched, and writing
                        // their `None` getattr fallback would clear the pending
                        // deferred load with a wrong value.
                        let fresh = instance.getattr("__dict__")?;
                        let fresh = fresh.cast::<PyDict>()?;
                        let mut refreshed: Vec<String> = Vec::new();
                        for name in names {
                            if let Some(value) = fresh.get_item(name.as_str())? {
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

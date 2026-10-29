//! The decision flow against fakes: no network, no MySQL.
//!
//! All three fakes write into one shared event log, so the tests can check
//! ORDER (ids saved before they are used, row deleted after the copies), not
//! just which calls happened.

use std::cell::{Cell, RefCell};
use std::collections::{HashMap, VecDeque};
use std::rc::Rc;

use chrono::{NaiveDate, NaiveDateTime};

use super::*;
use crate::google::error::Scope;

type Log = Rc<RefCell<Vec<String>>>;

const SHA_A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const SHA_B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

// ---------------------------------------------------------------- fakes ----

struct FakePanel {
    log: Log,
    answers: HashMap<i32, PanelAnswer>,
}

impl Panel for FakePanel {
    async fn fetch_copy(&self, user_id: i32) -> PanelAnswer {
        self.log.borrow_mut().push(format!("panel {user_id}"));
        self.answers
            .get(&user_id)
            .cloned()
            .unwrap_or(PanelAnswer::Error("panel: HTTP 500".into()))
    }
}

struct FakeGoogle {
    log: Log,
    /// What `drive_generate_id` hands out, in order.
    ids: RefCell<VecDeque<String>>,
    /// File ids whose upload answers 404.
    gone_files: RefCell<Vec<String>>,
    /// File ids whose create/upload answer says `"trashed": true`.
    trashed_files: RefCell<Vec<String>>,
    /// "op" or "op id" → the failure it returns ("op id" wins).
    failures: RefCell<HashMap<String, Failure>>,
}

impl FakeGoogle {
    fn call(&self, op: &'static str, arg: &str) -> Result<(), Failure> {
        self.log.borrow_mut().push(format!("{op} {arg}"));
        let failures = self.failures.borrow();
        match failures
            .get(&format!("{op} {arg}"))
            .or_else(|| failures.get(op))
        {
            Some(f) => Err(f.clone()),
            None => Ok(()),
        }
    }
}

/// The single-instance lock: held for `allow` more checks (`None`: always).
struct FakeLock {
    log: Log,
    allow: Cell<Option<usize>>,
}

impl Lock for FakeLock {
    async fn held(&self) -> bool {
        self.log.borrow_mut().push("lock?".into());
        match self.allow.get() {
            None => true,
            Some(0) => false,
            Some(n) => {
                self.allow.set(Some(n - 1));
                true
            }
        }
    }
}

impl Google for FakeGoogle {
    async fn firestore_write(&self, doc_id: &str, _blob: &[u8]) -> Result<(), Failure> {
        self.call("fs_write", doc_id)
    }
    async fn firestore_delete(&self, doc_id: &str) -> Result<(), Failure> {
        self.call("fs_delete", doc_id)
    }
    async fn drive_generate_id(&self) -> Result<String, Failure> {
        self.call("drive_gen", "")?;
        Ok(self
            .ids
            .borrow_mut()
            .pop_front()
            .unwrap_or_else(|| "fileX".to_string()))
    }
    async fn drive_create(&self, file_id: &str) -> Result<Upload, Failure> {
        self.call("drive_create", file_id)?;
        if self.trashed_files.borrow().iter().any(|f| f == file_id) {
            Ok(Upload::Trashed)
        } else {
            Ok(Upload::Done)
        }
    }
    async fn drive_upload(&self, file_id: &str, _blob: &[u8]) -> Result<Upload, Failure> {
        self.call("drive_upload", file_id)?;
        if self.gone_files.borrow().iter().any(|f| f == file_id) {
            Ok(Upload::NotFound)
        } else if self.trashed_files.borrow().iter().any(|f| f == file_id) {
            Ok(Upload::Trashed)
        } else {
            Ok(Upload::Done)
        }
    }
    async fn drive_share(&self, file_id: &str) -> Result<(), Failure> {
        self.call("drive_share", file_id)
    }
    async fn drive_delete(&self, file_id: &str) -> Result<(), Failure> {
        self.call("drive_delete", file_id)
    }
}

#[derive(Debug, Clone, Default)]
struct Extra {
    last_error: Option<String>,
}

struct FakeStore {
    log: Log,
    rows: RefCell<Vec<(CopyRow, Extra)>>,
    /// When set, `due_rows` fails with this message.
    fail_due: RefCell<Option<String>>,
    /// Simulates a stop (SIGTERM, crash) right after the store writes: the
    /// row's final UPDATE (mark_synced / defer / mark_failed) never runs.
    stopped: Cell<bool>,
    /// Simulates the user coming back while the copies are being deleted:
    /// a successful `clear_sync_state` also bumps `dirty_at`.
    bump_dirty_on_clear: Cell<bool>,
}

impl FakeStore {
    fn row(&self, id: i32) -> Option<CopyRow> {
        self.rows
            .borrow()
            .iter()
            .find(|(r, _)| r.id == id)
            .map(|(r, _)| r.clone())
    }
    fn extra(&self, id: i32) -> Extra {
        self.rows
            .borrow()
            .iter()
            .find(|(r, _)| r.id == id)
            .map(|(_, e)| e.clone())
            .unwrap_or_default()
    }
    fn with_row<T>(&self, id: i32, f: impl FnOnce(&mut CopyRow, &mut Extra) -> T) -> Option<T> {
        let mut rows = self.rows.borrow_mut();
        rows.iter_mut()
            .find(|(r, _)| r.id == id)
            .map(|(r, e)| f(r, e))
    }
}

impl Store for FakeStore {
    async fn due_rows(&self, now: NaiveDateTime, limit: u32) -> Result<Vec<CopyRow>, String> {
        if let Some(e) = self.fail_due.borrow().clone() {
            return Err(e);
        }
        let mut due: Vec<CopyRow> = self
            .rows
            .borrow()
            .iter()
            .map(|(r, _)| r.clone())
            .filter(|r| is_due(r, now))
            .collect();
        due.sort_by_key(|r| (!is_dirty(r), r.dirty_at));
        due.truncate(limit as usize);
        Ok(due)
    }

    async fn save_id(
        &self,
        row_id: i32,
        column: IdColumn,
        old: Option<&str>,
        new: &str,
    ) -> Result<bool, String> {
        self.log.borrow_mut().push(format!(
            "save {column:?} {} -> {new}",
            old.unwrap_or("NULL")
        ));
        Ok(self
            .with_row(row_id, |r, _| {
                let slot = match column {
                    IdColumn::Firestore => &mut r.firestore_doc_id,
                    IdColumn::Drive => &mut r.drive_file_id,
                };
                if slot.as_deref() == old {
                    *slot = Some(new.to_string());
                    // Same statement as the id, like the real store.
                    r.synced_at = None;
                    true
                } else {
                    false
                }
            })
            .unwrap_or(false))
    }

    async fn mark_synced(
        &self,
        row_id: i32,
        synced_at: NaiveDateTime,
        now: NaiveDateTime,
        sha: &str,
        last_upload: Option<NaiveDateTime>,
        doc_id: &str,
        file_id: &str,
    ) -> Result<bool, String> {
        self.log.borrow_mut().push("mark_synced".into());
        if self.stopped.get() {
            return Ok(true);
        }
        Ok(self
            .with_row(row_id, |r, e| {
                if r.firestore_doc_id.as_deref() != Some(doc_id)
                    || r.drive_file_id.as_deref() != Some(file_id)
                {
                    return false;
                }
                r.synced_at = Some(synced_at);
                r.checked_at = Some(now);
                r.content_sha = Some(sha.to_string());
                if last_upload.is_some() {
                    r.last_upload_at = last_upload;
                }
                r.attempts = 0;
                r.next_attempt_at = None;
                e.last_error = None;
                true
            })
            .unwrap_or(false))
    }

    async fn clear_sync_state(
        &self,
        row_id: i32,
        seen_dirty: NaiveDateTime,
    ) -> Result<bool, String> {
        self.log.borrow_mut().push("clear_sync".into());
        Ok(self
            .with_row(row_id, |r, _| {
                if r.dirty_at != seen_dirty {
                    return false;
                }
                r.synced_at = None;
                r.content_sha = None;
                r.last_upload_at = None;
                if self.bump_dirty_on_clear.get() {
                    r.dirty_at += chrono::Duration::seconds(1);
                }
                true
            })
            .unwrap_or(false))
    }

    async fn clear_sha(&self, row_id: i32) -> Result<(), String> {
        self.log.borrow_mut().push("clear_sha".into());
        self.with_row(row_id, |r, _| r.content_sha = None);
        Ok(())
    }

    async fn defer(
        &self,
        row_id: i32,
        next_attempt_at: NaiveDateTime,
        clear_sha: bool,
    ) -> Result<(), String> {
        self.log.borrow_mut().push("defer".into());
        if self.stopped.get() {
            return Ok(());
        }
        self.with_row(row_id, |r, _| {
            r.next_attempt_at = Some(next_attempt_at);
            if clear_sha {
                r.content_sha = None;
            }
        });
        Ok(())
    }

    async fn release_ids(
        &self,
        row_id: i32,
        deleted_doc: Option<&str>,
        deleted_file: Option<&str>,
    ) -> Result<(), String> {
        self.log.borrow_mut().push("release_ids".into());
        self.with_row(row_id, |r, _| {
            if r.firestore_doc_id.as_deref() == deleted_doc {
                r.firestore_doc_id = None;
            }
            if r.drive_file_id.as_deref() == deleted_file {
                r.drive_file_id = None;
            }
            r.synced_at = None;
        });
        Ok(())
    }

    async fn mark_failed(
        &self,
        row_id: i32,
        message: &str,
        next_attempt_at: NaiveDateTime,
        clear_sha: bool,
    ) -> Result<(), String> {
        self.log.borrow_mut().push("mark_failed".into());
        if self.stopped.get() {
            return Ok(());
        }
        self.with_row(row_id, |r, e| {
            r.attempts += 1;
            r.next_attempt_at = Some(next_attempt_at);
            if clear_sha {
                r.content_sha = None;
            }
            e.last_error = Some(message.to_string());
        });
        Ok(())
    }

    async fn delete_row(&self, row_id: i32, seen_dirty: NaiveDateTime) -> Result<bool, String> {
        self.log.borrow_mut().push("delete_row".into());
        let mut rows = self.rows.borrow_mut();
        let before = rows.len();
        rows.retain(|(r, _)| !(r.id == row_id && r.dirty_at == seen_dirty));
        Ok(rows.len() < before)
    }

    async fn backlog(&self) -> Result<Backlog, String> {
        Ok(Backlog::default())
    }
}

// -------------------------------------------------------------- helpers ----

struct World {
    log: Log,
    panel: FakePanel,
    google: FakeGoogle,
    store: FakeStore,
    lock: FakeLock,
    /// Loop state carried across `process` calls (pauses, partial writes).
    state: RefCell<LoopState>,
}

impl World {
    fn new(rows: Vec<CopyRow>, answers: Vec<(i32, PanelAnswer)>) -> World {
        let log: Log = Rc::new(RefCell::new(Vec::new()));
        World {
            panel: FakePanel {
                log: log.clone(),
                answers: answers.into_iter().collect(),
            },
            google: FakeGoogle {
                log: log.clone(),
                ids: RefCell::new(VecDeque::from(["file2".to_string(), "file3".to_string()])),
                gone_files: RefCell::new(Vec::new()),
                trashed_files: RefCell::new(Vec::new()),
                failures: RefCell::new(HashMap::new()),
            },
            store: FakeStore {
                log: log.clone(),
                rows: RefCell::new(rows.into_iter().map(|r| (r, Extra::default())).collect()),
                fail_due: RefCell::new(None),
                stopped: Cell::new(false),
                bump_dirty_on_clear: Cell::new(false),
            },
            lock: FakeLock {
                log: log.clone(),
                allow: Cell::new(None),
            },
            state: RefCell::new(LoopState::default()),
            log,
        }
    }

    /// `op` ("drive_upload") or `op id` ("drive_upload file1") fails with `f`.
    fn fail(&self, op: &str, f: Failure) {
        self.google.failures.borrow_mut().insert(op.to_string(), f);
    }

    async fn process(&self, row: &CopyRow, now: NaiveDateTime) -> Outcome {
        // Taken out and put back so no RefCell borrow is held across awaits.
        let mut state = self.state.take();
        let out = process_row(&self.panel, &self.google, &self.store, &mut state, row, now).await;
        self.state.replace(state);
        out
    }

    async fn batch(&self, state: &mut LoopState) -> BatchEnd {
        run_batch(&self.panel, &self.google, &self.store, &self.lock, state).await
    }

    fn events(&self) -> Vec<String> {
        self.log.borrow().clone()
    }

    /// Everything but the per-row lock checks.
    fn work(&self) -> Vec<String> {
        self.events().into_iter().filter(|e| e != "lock?").collect()
    }

    /// Google calls only (no panel, no store), for "what was written".
    fn google_calls(&self) -> Vec<String> {
        self.events()
            .into_iter()
            .filter(|e| e.starts_with("fs_") || e.starts_with("drive_"))
            .collect()
    }
}

/// 2026-10-09 h:m:s.123456 — microseconds on purpose (DATETIME(6) columns).
fn at(h: u32, m: u32, s: u32) -> NaiveDateTime {
    NaiveDate::from_ymd_opt(2026, 10, 9)
        .and_then(|d| d.and_hms_micro_opt(h, m, s, 123_456))
        .unwrap()
}

/// A healthy row: synced at 10:00, checked 40 min before NOW, uploaded 1 h
/// before NOW with SHA_A.
const NOW: (u32, u32, u32) = (12, 0, 0);

fn now() -> NaiveDateTime {
    at(NOW.0, NOW.1, NOW.2)
}

fn synced_row() -> CopyRow {
    CopyRow {
        id: 1,
        user_id: 7,
        key_changed_at: at(9, 0, 0),
        firestore_doc_id: Some("doc1".into()),
        drive_file_id: Some("file1".into()),
        content_sha: Some(SHA_A.into()),
        dirty_at: at(10, 0, 0),
        synced_at: Some(at(10, 0, 0)),
        checked_at: Some(at(11, 20, 0)),
        last_upload_at: Some(at(11, 0, 0)),
        attempts: 0,
        next_attempt_at: None,
    }
}

fn new_row() -> CopyRow {
    CopyRow {
        firestore_doc_id: None,
        drive_file_id: None,
        content_sha: None,
        synced_at: None,
        checked_at: None,
        last_upload_at: None,
        ..synced_row()
    }
}

fn copy(sha: &str) -> PanelAnswer {
    let mut blob = vec![0x01];
    blob.extend_from_slice(&[0x55; 60]);
    PanelAnswer::Copy(Copy {
        blob,
        sha: sha.to_string(),
    })
}

// ------------------------------------------------- selection / decisions ----

#[test]
fn dirty_rule_matches_the_sql() {
    assert!(is_dirty(&new_row()));
    assert!(!is_dirty(&synced_row()));
    let r = CopyRow {
        dirty_at: at(10, 0, 1),
        ..synced_row()
    };
    assert!(is_dirty(&r), "dirty_at > synced_at");
    let r = CopyRow {
        key_changed_at: at(10, 0, 1),
        ..synced_row()
    };
    assert!(is_dirty(&r), "synced_at < key_changed_at");
}

#[test]
fn dirty_rule_sees_microseconds() {
    let mut r = synced_row();
    r.dirty_at += chrono::Duration::microseconds(1);
    assert!(is_dirty(&r));
}

#[test]
fn clean_rows_are_rechecked_every_30_minutes() {
    let now = now();
    let mut r = synced_row();
    r.checked_at = Some(now - chrono::Duration::minutes(29));
    assert!(!is_due(&r, now));
    r.checked_at = Some(now - chrono::Duration::minutes(31));
    assert!(is_due(&r, now));
    r.checked_at = None;
    assert!(is_due(&r, now));
}

#[test]
fn backoff_holds_a_row_back_even_when_dirty() {
    let now = now();
    let mut r = new_row();
    r.next_attempt_at = Some(now + chrono::Duration::seconds(1));
    assert!(!is_due(&r, now));
    r.next_attempt_at = Some(now);
    assert!(is_due(&r, now));
}

#[test]
fn plan_first_time() {
    let p = plan(&new_row(), SHA_A, now());
    assert_eq!(
        p,
        Plan {
            upload: true,
            first_time: true,
            create_drive: true,
            refresh: true
        }
    );
}

#[test]
fn plan_unchanged_and_recent_uploads_nothing() {
    let p = plan(&synced_row(), SHA_A, now());
    assert_eq!(
        p,
        Plan {
            upload: false,
            first_time: false,
            create_drive: false,
            refresh: false
        }
    );
}

#[test]
fn plan_uploads_on_sha_change_key_change_age_or_missing_id() {
    let now = now();
    assert!(plan(&synced_row(), SHA_B, now).upload, "sha changed");

    let r = CopyRow {
        key_changed_at: at(10, 30, 0),
        ..synced_row()
    };
    assert!(plan(&r, SHA_A, now).upload, "key changed after last sync");

    let r = CopyRow {
        last_upload_at: Some(now - chrono::Duration::minutes(5 * 60 + 31)),
        ..synced_row()
    };
    assert!(plan(&r, SHA_A, now).upload, "older than 5 h 30 min");
    let r = CopyRow {
        last_upload_at: Some(now - chrono::Duration::minutes(5 * 60 + 29)),
        ..synced_row()
    };
    assert!(!plan(&r, SHA_A, now).upload, "5 h 29 min is fine");

    let r = CopyRow {
        last_upload_at: None,
        ..synced_row()
    };
    assert!(plan(&r, SHA_A, now).upload, "never uploaded");

    let r = CopyRow {
        drive_file_id: None,
        ..synced_row()
    };
    let p = plan(&r, SHA_A, now);
    assert!(
        p.upload && p.create_drive && !p.first_time,
        "drive id missing"
    );

    let r = CopyRow {
        firestore_doc_id: None,
        ..synced_row()
    };
    let p = plan(&r, SHA_A, now);
    assert!(p.upload && !p.create_drive, "firestore id missing");
}

#[test]
fn synced_value_is_the_dirty_at_read_to_the_microsecond() {
    let r = synced_row();
    assert_eq!(synced_value(&r), r.dirty_at);
    assert_eq!(
        synced_value(&r).and_utc().timestamp_subsec_micros(),
        123_456
    );
}

#[test]
fn synced_value_covers_a_key_stamped_after_the_dirty_mark() {
    // A revoke whose key_changed_at landed a few µs after its dirty mark must
    // not leave the row "key changed" forever after a successful upload.
    let mut r = new_row();
    r.dirty_at = at(10, 0, 0);
    r.key_changed_at = r.dirty_at + chrono::Duration::microseconds(7);
    let after = CopyRow {
        synced_at: Some(synced_value(&r)),
        ..r.clone()
    };
    assert!(!is_dirty(&after));
}

// ------------------------------------------------------------ the flows ----

#[tokio::test]
async fn panel_404_deletes_both_copies_then_the_row() {
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, PanelAnswer::Gone)]);
    assert_eq!(w.process(&row, now()).await, Outcome::Deleted);
    assert_eq!(
        w.events(),
        vec![
            "panel 7",
            "clear_sync",
            "fs_delete doc1",
            "drive_delete file1",
            "delete_row"
        ]
    );
    assert!(w.store.row(1).is_none());
}

#[tokio::test]
async fn bare_404_never_deletes_and_backs_off() {
    // An older panel without the route, a proxied or mistyped PANEL_URL: a
    // 404 without x-copy-gone is NOT "user gone".
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, PanelAnswer::UnmarkedNotFound)]);
    let out = w.process(&row, now()).await;
    assert!(
        matches!(out, Outcome::UnmarkedNotFound(ref f) if f.scope == Scope::Row),
        "{out:?}"
    );
    assert!(w.google_calls().is_empty(), "nothing deleted at Google");
    assert!(!w.events().contains(&"delete_row".to_string()));
    let kept = w.store.row(1).unwrap();
    assert_eq!(kept.firestore_doc_id.as_deref(), Some("doc1"));
    assert_eq!(kept.drive_file_id.as_deref(), Some("file1"));
    assert_eq!(kept.attempts, 1);
    assert_eq!(
        kept.next_attempt_at,
        Some(now() + chrono::Duration::seconds(30))
    );
    assert_eq!(
        w.store.extra(1).last_error.as_deref(),
        Some("panel: HTTP 404 without x-copy-gone")
    );
}

#[tokio::test]
async fn bare_404s_warn_once_per_batch_and_the_batch_goes_on() {
    let rows = vec![dirty_row(1, 7, 5), dirty_row(2, 8, 3), dirty_row(3, 9, 1)];
    let w = World::new(
        rows,
        vec![
            (7, PanelAnswer::UnmarkedNotFound),
            (8, PanelAnswer::UnmarkedNotFound),
            (9, copy(SHA_B)),
        ],
    );
    let mut state = LoopState::default();
    assert_eq!(w.batch(&mut state).await, BatchEnd::Done);
    assert!(state.warned_unmarked_404);
    // Nothing deleted anywhere; the healthy row still synced.
    assert!(w.store.row(1).is_some() && w.store.row(2).is_some());
    assert!(!w.events().iter().any(|e| e.contains("delete")));
    assert_eq!(w.store.row(1).unwrap().attempts, 1);
    assert_eq!(w.store.row(2).unwrap().attempts, 1);
    assert!(!is_dirty(&w.store.row(3).unwrap()));

    // The once-per-batch flag resets at the start of the next batch.
    let w2 = World::new(vec![], vec![]);
    w2.batch(&mut state).await;
    assert!(!state.warned_unmarked_404);
}

#[tokio::test]
async fn panel_404_without_ids_only_deletes_the_row() {
    let row = new_row();
    let w = World::new(vec![row.clone()], vec![(7, PanelAnswer::Gone)]);
    assert_eq!(w.process(&row, now()).await, Outcome::Deleted);
    assert!(w.google_calls().is_empty());
    assert!(w.store.row(1).is_none());
}

#[tokio::test]
async fn panel_404_keeps_the_row_when_a_delete_fails() {
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, PanelAnswer::Gone)]);
    w.fail(
        "drive_delete",
        Failure::from_status("drive delete", 500, "backend"),
    );
    // A 500 is account-wide: Drive pauses, the row waits for it uncharged.
    assert_eq!(
        w.process(&row, now()).await,
        Outcome::Deferred(now() + chrono::Duration::seconds(300))
    );
    let kept = w.store.row(1).unwrap();
    assert_eq!(kept.attempts, 0);
    assert_eq!(
        kept.drive_file_id.as_deref(),
        Some("file1"),
        "id kept for the retry"
    );
    assert!(!w.events().contains(&"delete_row".to_string()));
    // Cleared BEFORE the deletes: the row no longer claims a synced copy.
    assert_eq!(kept.synced_at, None);
    assert_eq!(kept.content_sha, None);
    assert_eq!(kept.last_upload_at, None);

    // The user comes back to Micky before the retry: the same sha must still
    // be UPLOADED (G1 was already deleted), not merely marked synced.
    let w2 = World::new(vec![kept.clone()], vec![(7, copy(SHA_A))]);
    assert_eq!(
        w2.process(&kept, now()).await,
        Outcome::Synced { uploaded: true }
    );
    assert_eq!(
        w2.google_calls(),
        vec![
            "fs_write doc1",
            "drive_create file1",
            "drive_upload file1",
            "drive_share file1"
        ]
    );
}

#[tokio::test]
async fn panel_404_does_not_delete_a_row_the_panel_touched_meanwhile() {
    let seen = synced_row();
    let mut current = seen.clone();
    current.dirty_at = at(11, 59, 0); // the user came back to Micky
    let w = World::new(vec![current], vec![(7, PanelAnswer::Gone)]);
    assert_eq!(w.process(&seen, now()).await, Outcome::RowChanged);
    assert!(w.store.row(1).is_some());
    assert_eq!(w.events(), vec!["panel 7", "clear_sync"]);
    assert!(w.google_calls().is_empty(), "nothing deleted at Google");
    assert_eq!(
        w.store.row(1).unwrap().synced_at,
        seen.synced_at,
        "untouched"
    );
}

#[tokio::test]
async fn drive_file_in_the_trash_is_replaced_and_purged() {
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_B))]);
    w.google.trashed_files.borrow_mut().push("file1".into());
    assert_eq!(
        w.process(&row, now()).await,
        Outcome::Synced { uploaded: true }
    );
    assert_eq!(
        w.events(),
        vec![
            "panel 7",
            "clear_sha",
            "fs_write doc1",
            "drive_upload file1",
            "drive_gen ",
            "save Drive file1 -> file2",
            "drive_create file2",
            "drive_upload file2",
            "drive_share file2",
            "drive_delete file1",
            "mark_synced",
        ]
    );
    let after = w.store.row(1).unwrap();
    assert_eq!(after.drive_file_id.as_deref(), Some("file2"));
    assert!(!is_dirty(&after));
}

#[tokio::test]
async fn failing_to_purge_a_trashed_file_does_not_fail_the_row() {
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_B))]);
    w.google.trashed_files.borrow_mut().push("file1".into());
    w.fail(
        "drive_delete",
        Failure::from_status("drive delete", 500, "backend"),
    );
    assert_eq!(
        w.process(&row, now()).await,
        Outcome::Synced { uploaded: true }
    );
    assert_eq!(w.store.row(1).unwrap().attempts, 0);
}

#[tokio::test]
async fn a_trashed_file_found_by_create_is_replaced_without_uploading_to_it() {
    // Interrupted first attempt: ids saved, file1 since trashed by hand.
    let row = CopyRow {
        firestore_doc_id: Some("doc1".into()),
        drive_file_id: Some("file1".into()),
        ..new_row()
    };
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_A))]);
    w.google.trashed_files.borrow_mut().push("file1".into());
    assert_eq!(
        w.process(&row, now()).await,
        Outcome::Synced { uploaded: true }
    );
    assert_eq!(
        w.google_calls(),
        vec![
            "fs_write doc1",
            "drive_create file1",
            "drive_gen ",
            "drive_create file2",
            "drive_upload file2",
            "drive_share file2",
            "drive_delete file1",
        ]
    );
}

#[tokio::test]
async fn success_is_not_recorded_when_the_ids_changed_underneath() {
    // We wrote under doc1/file1; meanwhile the row's Drive id became fileZ.
    let seen = synced_row();
    let current = CopyRow {
        drive_file_id: Some("fileZ".into()),
        ..seen.clone()
    };
    let w = World::new(vec![current.clone()], vec![(7, copy(SHA_B))]);
    assert_eq!(w.process(&seen, now()).await, Outcome::RowChanged);
    let after = w.store.row(1).unwrap();
    assert_eq!(after.synced_at, current.synced_at, "not marked synced");
    assert_eq!(after.attempts, 0);
    // Only the write-ahead clear happened: the stores were written, so the
    // stored sha no longer vouches for them.
    assert_eq!(after.content_sha, None);
}

#[tokio::test]
async fn first_time_saves_ids_before_use_then_creates_uploads_shares() {
    let row = new_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_A))]);
    assert_eq!(
        w.process(&row, now()).await,
        Outcome::Synced { uploaded: true }
    );

    let after = w.store.row(1).unwrap();
    let doc = after.firestore_doc_id.clone().unwrap();
    assert_eq!(doc.len(), 32);
    assert_eq!(
        w.events(),
        vec![
            "panel 7".to_string(),
            format!("save Firestore NULL -> {doc}"),
            "drive_gen ".to_string(),
            "save Drive NULL -> file2".to_string(),
            "clear_sha".to_string(),
            format!("fs_write {doc}"),
            "drive_create file2".to_string(),
            "drive_upload file2".to_string(),
            "drive_share file2".to_string(),
            "mark_synced".to_string(),
        ]
    );
    assert_eq!(after.drive_file_id.as_deref(), Some("file2"));
    assert_eq!(after.synced_at, Some(row.dirty_at));
    assert_eq!(after.content_sha.as_deref(), Some(SHA_A));
    assert_eq!(after.last_upload_at, Some(now()));
    assert_eq!(after.checked_at, Some(now()));
    assert!(!is_dirty(&after));
}

#[tokio::test]
async fn first_time_resumes_with_ids_from_an_interrupted_attempt() {
    // Ids were saved, then the process died. No new ids; create (409 at
    // Google = fine) + upload + share under the saved ones.
    let row = CopyRow {
        firestore_doc_id: Some("doc1".into()),
        drive_file_id: Some("file1".into()),
        ..new_row()
    };
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_A))]);
    assert_eq!(
        w.process(&row, now()).await,
        Outcome::Synced { uploaded: true }
    );
    assert_eq!(
        w.google_calls(),
        vec![
            "fs_write doc1",
            "drive_create file1",
            "drive_upload file1",
            "drive_share file1"
        ]
    );
}

#[tokio::test]
async fn unchanged_sha_within_5h30_uploads_nothing_but_marks_checked() {
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_A))]);
    assert_eq!(
        w.process(&row, now()).await,
        Outcome::Synced { uploaded: false }
    );
    assert!(w.google_calls().is_empty());
    let after = w.store.row(1).unwrap();
    assert_eq!(after.checked_at, Some(now()));
    assert_eq!(after.last_upload_at, row.last_upload_at, "untouched");
}

#[tokio::test]
async fn changed_sha_updates_both_stores_without_create_or_share() {
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_B))]);
    assert_eq!(
        w.process(&row, now()).await,
        Outcome::Synced { uploaded: true }
    );
    assert_eq!(
        w.google_calls(),
        vec!["fs_write doc1", "drive_upload file1"]
    );
    assert_eq!(w.store.row(1).unwrap().content_sha.as_deref(), Some(SHA_B));
}

#[tokio::test]
async fn unchanged_sha_older_than_5h30_is_reuploaded() {
    let row = CopyRow {
        last_upload_at: Some(at(5, 0, 0)),
        ..synced_row()
    };
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_A))]);
    assert_eq!(
        w.process(&row, now()).await,
        Outcome::Synced { uploaded: true }
    );
    assert_eq!(
        w.google_calls(),
        vec!["fs_write doc1", "drive_upload file1", "drive_share file1"],
        "N5: the refresh re-applies the share"
    );
}

#[tokio::test]
async fn key_change_uploads_even_with_the_same_sha() {
    // Revoke: new key → re-encrypted copy; the plaintext sha is unchanged.
    let row = CopyRow {
        key_changed_at: at(11, 30, 0),
        dirty_at: at(11, 30, 0),
        ..synced_row()
    };
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_A))]);
    assert_eq!(
        w.process(&row, now()).await,
        Outcome::Synced { uploaded: true }
    );
    assert_eq!(
        w.google_calls(),
        vec!["fs_write doc1", "drive_upload file1"]
    );
    let after = w.store.row(1).unwrap();
    assert!(after.synced_at >= Some(after.key_changed_at));
    assert!(!is_dirty(&after));
}

#[tokio::test]
async fn drive_file_deleted_by_hand_gets_a_new_id_first() {
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_B))]);
    w.google.gone_files.borrow_mut().push("file1".into());
    assert_eq!(
        w.process(&row, now()).await,
        Outcome::Synced { uploaded: true }
    );
    assert_eq!(
        w.events(),
        vec![
            "panel 7",
            "clear_sha",
            "fs_write doc1",
            "drive_upload file1",
            "drive_gen ",
            "save Drive file1 -> file2",
            "drive_create file2",
            "drive_upload file2",
            "drive_share file2",
            "mark_synced",
        ]
    );
    assert_eq!(
        w.store.row(1).unwrap().drive_file_id.as_deref(),
        Some("file2")
    );
}

#[tokio::test]
async fn replacing_a_drive_id_clears_synced_at_until_the_new_file_is_complete() {
    // The upload 404s, a new id is reserved, then sharing it fails: the row
    // must carry the new id with synced_at = NULL (the panel then withholds
    // the G headers), not the new id with the old synced_at.
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_A))]);
    w.google.gone_files.borrow_mut().push("file1".into());
    w.fail(
        "drive_share",
        Failure::from_status("drive share", 400, "Bad Request"),
    );
    // Same sha and a recent upload: the 404 is only discovered because the
    // 5 h 30 min refresh is due.
    let row = CopyRow {
        last_upload_at: Some(at(5, 0, 0)),
        ..row
    };
    let out = w.process(&row, now()).await;
    assert!(matches!(out, Outcome::Failed(_)), "{out:?}");
    let mid = w.store.row(1).unwrap();
    assert_eq!(mid.drive_file_id.as_deref(), Some("file2"));
    assert_eq!(
        mid.synced_at, None,
        "headers withheld while file2 is incomplete"
    );
    assert_eq!(mid.attempts, 1);

    // Next attempt (after the backoff): first-time path under the saved id —
    // no new id, create (409 at Google = fine), upload, share, synced again.
    // G1 already holds this exact copy from the first pass: not rewritten.
    w.google.failures.borrow_mut().clear();
    w.log.borrow_mut().clear();
    let later = mid.next_attempt_at.unwrap();
    assert_eq!(
        w.process(&mid, later).await,
        Outcome::Synced { uploaded: true }
    );
    assert_eq!(
        w.google_calls(),
        vec![
            "drive_create file2",
            "drive_upload file2",
            "drive_share file2"
        ]
    );
    let done = w.store.row(1).unwrap();
    assert_eq!(done.synced_at, Some(row.dirty_at));
    assert_eq!(done.attempts, 0);
}

#[tokio::test]
async fn first_ids_are_saved_with_synced_at_cleared() {
    // A synced row that somehow lost its Drive id (cleared by hand): the new
    // id must not be advertised before the file exists.
    let row = CopyRow {
        drive_file_id: None,
        ..synced_row()
    };
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_A))]);
    w.fail(
        "drive_create",
        Failure::from_status("drive create", 400, "Bad Request"),
    );
    assert!(matches!(w.process(&row, now()).await, Outcome::Failed(_)));
    let mid = w.store.row(1).unwrap();
    assert_eq!(mid.drive_file_id.as_deref(), Some("file2"));
    assert_eq!(mid.synced_at, None);
}

#[tokio::test]
async fn a_change_during_the_upload_keeps_the_row_dirty() {
    let seen = synced_row(); // what the loop read
    let mut current = CopyRow {
        dirty_at: at(11, 0, 0),
        ..seen.clone()
    };
    let seen = CopyRow {
        dirty_at: at(10, 30, 0),
        ..seen
    };
    current.content_sha = seen.content_sha.clone();
    // The panel bumped dirty_at to 11:00 after we read 10:30.
    let w = World::new(vec![current], vec![(7, copy(SHA_B))]);
    assert_eq!(
        w.process(&seen, now()).await,
        Outcome::Synced { uploaded: true }
    );
    let after = w.store.row(1).unwrap();
    assert_eq!(after.synced_at, Some(at(10, 30, 0)));
    assert!(
        is_dirty(&after),
        "the 11:00 change must be picked up next tick"
    );
}

#[tokio::test]
async fn per_row_failure_backs_off_and_is_recorded() {
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_B))]);
    w.fail(
        "fs_write",
        Failure::from_status("firestore write", 400, "Invalid argument"),
    );
    let out = w.process(&row, now()).await;
    assert!(
        matches!(out, Outcome::Failed(ref f) if f.scope == Scope::Row),
        "{out:?}"
    );
    let after = w.store.row(1).unwrap();
    assert_eq!(after.attempts, 1);
    assert_eq!(
        after.next_attempt_at,
        Some(now() + chrono::Duration::seconds(30))
    );
    assert_eq!(
        w.store.extra(1).last_error.as_deref(),
        Some("firestore write: HTTP 400 Invalid argument")
    );
    assert_eq!(after.synced_at, row.synced_at, "not marked synced");
}

#[tokio::test]
async fn repeated_failures_back_off_further() {
    let row = CopyRow {
        attempts: 3,
        ..synced_row()
    };
    let w = World::new(
        vec![row.clone()],
        vec![(7, PanelAnswer::Error("panel: HTTP 500".into()))],
    );
    let out = w.process(&row, now()).await;
    assert!(matches!(out, Outcome::Failed(ref f) if f.scope == Scope::Row));
    let after = w.store.row(1).unwrap();
    assert_eq!(after.attempts, 4);
    assert_eq!(
        after.next_attempt_at,
        Some(now() + chrono::Duration::seconds(240))
    );
}

#[tokio::test]
async fn a_lost_id_race_stops_the_row_without_writing() {
    // The row's id column is no longer what we read (row deleted/replaced).
    let row = new_row();
    let w = World::new(vec![], vec![(7, copy(SHA_A))]);
    assert_eq!(w.process(&row, now()).await, Outcome::RowChanged);
    assert!(w.google_calls().is_empty());
}

#[tokio::test]
async fn panel_off_and_bad_secret_never_touch_the_row() {
    for answer in [PanelAnswer::Disabled, PanelAnswer::Unauthorized(401)] {
        let row = synced_row();
        let w = World::new(vec![row.clone()], vec![(7, answer.clone())]);
        let out = w.process(&row, now()).await;
        assert!(
            matches!(
                out,
                Outcome::PanelDisabled | Outcome::PanelUnauthorized(401)
            ),
            "{out:?}"
        );
        assert_eq!(w.store.row(1).unwrap(), row);
        assert!(w.google_calls().is_empty());
    }
}

#[tokio::test]
async fn a_user_back_mid_delete_gets_fresh_ids() {
    // Marked 404, clear succeeds, both copies are deleted, then the user is
    // back (dirty_at moved) so the guarded row DELETE misses: the ids of the
    // deleted objects are released, and the next pass reserves new ones.
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, PanelAnswer::Gone)]);
    w.store.bump_dirty_on_clear.set(true);
    assert_eq!(w.process(&row, now()).await, Outcome::RowChanged);
    assert_eq!(
        w.events(),
        vec![
            "panel 7",
            "clear_sync",
            "fs_delete doc1",
            "drive_delete file1",
            "delete_row",
            "release_ids"
        ]
    );
    let back = w.store.row(1).unwrap();
    assert_eq!(back.firestore_doc_id, None);
    assert_eq!(back.drive_file_id, None);
    assert_eq!(back.synced_at, None);

    let w2 = World::new(vec![back.clone()], vec![(7, copy(SHA_A))]);
    assert_eq!(
        w2.process(&back, now()).await,
        Outcome::Synced { uploaded: true }
    );
    let after = w2.store.row(1).unwrap();
    assert_ne!(after.firestore_doc_id.as_deref(), Some("doc1"));
    assert_eq!(after.drive_file_id.as_deref(), Some("file2"));
}

#[tokio::test]
async fn a_file_level_403_replaces_the_drive_file() {
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_B))]);
    let body = br#"{"error":{"code":403,"message":"no","errors":[{"reason":"insufficientFilePermissions"}]}}"#;
    w.fail(
        "drive_upload file1",
        Failure::from_google("drive upload", 403, body),
    );
    assert_eq!(
        w.process(&row, now()).await,
        Outcome::Synced { uploaded: true }
    );
    assert_eq!(
        w.work(),
        vec![
            "panel 7",
            "clear_sha",
            "fs_write doc1",
            "drive_upload file1",
            "drive_gen ",
            "save Drive file1 -> file2",
            "drive_create file2",
            "drive_upload file2",
            "drive_share file2",
            "drive_delete file1",
            "mark_synced",
        ]
    );
    assert_eq!(
        w.store.row(1).unwrap().drive_file_id.as_deref(),
        Some("file2")
    );
    assert_eq!(w.store.row(1).unwrap().attempts, 0);
}

#[tokio::test]
async fn a_file_level_403_on_share_replaces_the_drive_file_too() {
    // The 5 h 30 refresh re-shares; a file whose permissions were taken from
    // us answers 403 there.
    let row = CopyRow {
        last_upload_at: Some(at(5, 0, 0)),
        ..synced_row()
    };
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_A))]);
    let body = br#"{"error":{"code":403,"message":"no","errors":[{"reason":"cannotModifyViewersCanCopyContent"}]}}"#;
    w.fail(
        "drive_share file1",
        Failure::from_google("drive share", 403, body),
    );
    assert_eq!(
        w.process(&row, now()).await,
        Outcome::Synced { uploaded: true }
    );
    assert_eq!(
        w.store.row(1).unwrap().drive_file_id.as_deref(),
        Some("file2")
    );
}

#[test]
fn ids_are_logged_as_6_char_prefixes() {
    assert_eq!(id6("1WZCyO0EVB1HBHObkBOydxklo9Xm02_Yg"), "1WZCyO");
    assert_eq!(id6("207226496fdd1630aee76aa8d3075c30"), "207226");
    assert_eq!(id6("abc"), "abc");
}

#[tokio::test]
async fn content_back_to_an_older_sha_is_still_written_to_both_stores() {
    // Synced with SHA_A. Pass 1 renders SHA_B: G1 gets B, Drive is paused.
    // Before the retry the content goes back to A. The row must not look
    // "unchanged": both stores are written with A.
    let row = synced_row(); // content_sha = SHA_A, recent upload
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_B))]);
    w.state.borrow_mut().drive_pause = Some(StorePause {
        until: now() + chrono::Duration::seconds(60),
        reason: "x".into(),
    });
    assert!(matches!(w.process(&row, now()).await, Outcome::Deferred(_)));
    let mid = w.store.row(1).unwrap();
    assert_eq!(
        mid.content_sha, None,
        "cleared in the same UPDATE as the defer"
    );

    for restarted in [false, true] {
        let w2 = World::new(vec![mid.clone()], vec![(7, copy(SHA_A))]);
        if !restarted {
            // Same process: carries pass 1's memory (G1 holds B).
            *w2.state.borrow_mut() = w.state.take();
            w2.state.borrow_mut().drive_pause = None;
        }
        let later = now() + chrono::Duration::seconds(61);
        assert_eq!(
            w2.process(&mid, later).await,
            Outcome::Synced { uploaded: true },
            "restarted={restarted}"
        );
        assert_eq!(
            w2.google_calls(),
            vec!["fs_write doc1", "drive_upload file1"],
            "restarted={restarted}: both stores get A"
        );
    }
}

#[tokio::test]
async fn a_failed_write_also_clears_the_sha() {
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_B))]);
    w.fail(
        "drive_upload",
        Failure::from_status("drive upload", 400, "Bad Request"),
    );
    assert!(matches!(w.process(&row, now()).await, Outcome::Failed(_)));
    let after = w.store.row(1).unwrap();
    assert_eq!(after.attempts, 1);
    assert_eq!(
        after.content_sha, None,
        "cleared in the same UPDATE as the failure"
    );
}

#[tokio::test]
async fn a_failure_before_any_write_keeps_the_sha() {
    let row = synced_row();
    let w = World::new(
        vec![row.clone()],
        vec![(7, PanelAnswer::Error("panel: HTTP 500".into()))],
    );
    w.process(&row, now()).await;
    assert_eq!(w.store.row(1).unwrap().content_sha, row.content_sha);
}

#[tokio::test]
async fn a_failed_write_is_never_trusted_from_memory() {
    // Pass 1: G1 written with B (remembered), Drive paused.
    // Pass 2: content C; the G1 write times out — it may have landed.
    // Pass 3: content B again. Memory of "G1 holds B" must be gone.
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_B))]);
    w.state.borrow_mut().drive_pause = Some(StorePause {
        until: now() + chrono::Duration::hours(1),
        reason: "x".into(),
    });
    w.process(&row, now()).await;

    let sha_c = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
    let w2 = World::new(vec![w.store.row(1).unwrap()], vec![(7, copy(sha_c))]);
    *w2.state.borrow_mut() = w.state.take();
    w2.fail(
        "fs_write",
        Failure::row("firestore write: no answer (timeout)"),
    );
    let r2 = w2.store.row(1).unwrap();
    w2.process(&r2, now() + chrono::Duration::seconds(1)).await;

    let w3 = World::new(vec![w2.store.row(1).unwrap()], vec![(7, copy(SHA_B))]);
    *w3.state.borrow_mut() = w2.state.take();
    let r3 = w3.store.row(1).unwrap();
    w3.process(&r3, now() + chrono::Duration::seconds(2)).await;
    assert_eq!(
        w3.google_calls(),
        vec!["fs_write doc1"],
        "G1 rewritten with B, not trusted from pass 1"
    );
}

#[tokio::test]
async fn the_lock_is_checked_every_pass_even_with_nothing_due() {
    let w = World::new(vec![], vec![]);
    w.lock.allow.set(Some(0));
    // Returns at once: the check comes before any SELECT or row.
    tokio::time::timeout(
        Duration::from_secs(2),
        run(&w.panel, &w.google, &w.store, &w.lock),
    )
    .await
    .expect("run returns as soon as the lock is gone");
    assert_eq!(w.events(), vec!["lock?"]);
}

#[tokio::test]
async fn a_stop_right_after_the_first_store_write_still_rewrites_both() {
    // Synced with SHA_A. Pass 1 renders SHA_B, writes G1, and the process
    // stops before ANY final UPDATE (no defer, no mark_failed, no
    // mark_synced). After a restart the content is back at A: both stores
    // must be written, because G1 holds B.
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_B))]);
    w.state.borrow_mut().drive_pause = Some(StorePause {
        until: now() + chrono::Duration::hours(1),
        reason: "x".into(),
    });
    w.store.stopped.set(true);
    w.process(&row, now()).await;
    let ev = w.work();
    let cleared = ev
        .iter()
        .position(|e| e == "clear_sha")
        .expect("write-ahead clear");
    let wrote = ev
        .iter()
        .position(|e| e == "fs_write doc1")
        .expect("G1 written");
    assert!(
        cleared < wrote,
        "content_sha cleared BEFORE the first write: {ev:?}"
    );
    let left = w.store.row(1).unwrap();
    assert_eq!(left.content_sha, None);

    // Restart: no memory, Drive no longer paused, content back at A.
    let w2 = World::new(vec![left.clone()], vec![(7, copy(SHA_A))]);
    assert_eq!(
        w2.process(&left, now() + chrono::Duration::seconds(5))
            .await,
        Outcome::Synced { uploaded: true }
    );
    assert_eq!(
        w2.google_calls(),
        vec!["fs_write doc1", "drive_upload file1"]
    );
    assert_eq!(w2.store.row(1).unwrap().content_sha.as_deref(), Some(SHA_A));
}

#[tokio::test]
async fn the_write_ahead_clear_runs_once_and_only_when_writing() {
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_B))]);
    w.process(&row, now()).await;
    assert_eq!(w.work().iter().filter(|e| *e == "clear_sha").count(), 1);

    let w2 = World::new(vec![row.clone()], vec![(7, copy(SHA_A))]);
    assert_eq!(
        w2.process(&row, now()).await,
        Outcome::Synced { uploaded: false }
    );
    assert!(
        !w2.work().contains(&"clear_sha".to_string()),
        "nothing written, sha kept"
    );
}

#[tokio::test]
async fn no_write_when_the_write_ahead_clear_fails() {
    struct FailingClear(FakeStore);
    impl Store for FailingClear {
        async fn due_rows(&self, now: NaiveDateTime, l: u32) -> Result<Vec<CopyRow>, String> {
            self.0.due_rows(now, l).await
        }
        async fn save_id(
            &self,
            r: i32,
            c: IdColumn,
            o: Option<&str>,
            n: &str,
        ) -> Result<bool, String> {
            self.0.save_id(r, c, o, n).await
        }
        async fn mark_synced(
            &self,
            r: i32,
            s: NaiveDateTime,
            n: NaiveDateTime,
            h: &str,
            u: Option<NaiveDateTime>,
            d: &str,
            f: &str,
        ) -> Result<bool, String> {
            self.0.mark_synced(r, s, n, h, u, d, f).await
        }
        async fn mark_failed(
            &self,
            r: i32,
            m: &str,
            n: NaiveDateTime,
            c: bool,
        ) -> Result<(), String> {
            self.0.mark_failed(r, m, n, c).await
        }
        async fn defer(&self, r: i32, n: NaiveDateTime, c: bool) -> Result<(), String> {
            self.0.defer(r, n, c).await
        }
        async fn clear_sha(&self, _r: i32) -> Result<(), String> {
            Err("database down".into())
        }
        async fn clear_sync_state(&self, r: i32, s: NaiveDateTime) -> Result<bool, String> {
            self.0.clear_sync_state(r, s).await
        }
        async fn delete_row(&self, r: i32, s: NaiveDateTime) -> Result<bool, String> {
            self.0.delete_row(r, s).await
        }
        async fn release_ids(
            &self,
            r: i32,
            d: Option<&str>,
            f: Option<&str>,
        ) -> Result<(), String> {
            self.0.release_ids(r, d, f).await
        }
        async fn backlog(&self) -> Result<Backlog, String> {
            self.0.backlog().await
        }
    }
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_B))]);
    let store = FailingClear(FakeStore {
        log: w.log.clone(),
        rows: RefCell::new(vec![(row.clone(), Extra::default())]),
        fail_due: RefCell::new(None),
        stopped: Cell::new(false),
        bump_dirty_on_clear: Cell::new(false),
    });
    let mut state = LoopState::default();
    let out = process_row(&w.panel, &w.google, &store, &mut state, &row, now()).await;
    assert_eq!(out, Outcome::StoreError("database down".into()));
    assert!(w.google_calls().is_empty(), "nothing sent to Google");
}

// ---------------------------------------------------------- batch level ----

/// Rows relative to the real clock, because run_batch reads it. Each row
/// gets its own ids (doc<id>, file<id>).
fn dirty_row(id: i32, user_id: i32, minutes_ago: i64) -> CopyRow {
    let now = chrono::Utc::now().naive_utc();
    CopyRow {
        id,
        user_id,
        dirty_at: now - chrono::Duration::minutes(minutes_ago),
        key_changed_at: now - chrono::Duration::hours(1),
        synced_at: Some(now - chrono::Duration::hours(1)),
        checked_at: Some(now - chrono::Duration::minutes(1)),
        last_upload_at: Some(now - chrono::Duration::hours(1)),
        firestore_doc_id: Some(format!("doc{id}")),
        drive_file_id: Some(format!("file{id}")),
        ..synced_row()
    }
}

fn pause_of(end: BatchEnd) -> Pause {
    match end {
        BatchEnd::Pause(p) => p,
        other => panic!("expected a pause, got {other:?}"),
    }
}

#[tokio::test]
async fn batch_runs_oldest_dirty_first_and_survives_row_failures() {
    let rows = vec![dirty_row(1, 7, 1), dirty_row(2, 8, 5), dirty_row(3, 9, 3)];
    let w = World::new(
        rows,
        vec![
            (7, copy(SHA_B)),
            (8, PanelAnswer::Error("panel: HTTP 500".into())),
            (9, copy(SHA_B)),
        ],
    );
    let mut state = LoopState::default();
    assert_eq!(w.batch(&mut state).await, BatchEnd::Done);
    let panels: Vec<String> = w
        .events()
        .into_iter()
        .filter(|e| e.starts_with("panel"))
        .collect();
    assert_eq!(panels, vec!["panel 8", "panel 9", "panel 7"]);
    assert_eq!(w.store.row(2).unwrap().attempts, 1);
    assert!(!is_dirty(&w.store.row(1).unwrap()));
    assert!(!is_dirty(&w.store.row(3).unwrap()));
    assert!(state.last_error.is_some());
}

#[tokio::test]
async fn firestore_quota_pauses_firestore_only_and_drive_carries_on() {
    // The free tier's 20k writes/day are used up: 429 RESOURCE_EXHAUSTED.
    let rows = vec![dirty_row(1, 7, 5), dirty_row(2, 8, 1)];
    let w = World::new(rows, vec![(7, copy(SHA_B)), (8, copy(SHA_B))]);
    let body =
        br#"{"error":{"code":429,"message":"Quota exceeded.","status":"RESOURCE_EXHAUSTED"}}"#;
    w.fail(
        "fs_write",
        Failure::from_google("firestore write", 429, body),
    );
    let mut state = LoopState::default();
    let before = chrono::Utc::now().naive_utc();
    assert_eq!(
        w.batch(&mut state).await,
        BatchEnd::Done,
        "the batch goes on"
    );

    let fs_writes = w
        .events()
        .iter()
        .filter(|e| e.starts_with("fs_write"))
        .count();
    assert_eq!(fs_writes, 1, "Firestore tried once, then paused");
    assert!(w.events().contains(&"drive_upload file1".to_string()));
    assert!(w.events().contains(&"drive_upload file2".to_string()));
    assert!(state.firestore_pause.is_some());
    assert!(state.drive_pause.is_none());
    for id in [1, 2] {
        let r = w.store.row(id).unwrap();
        assert_eq!(
            r.attempts, 0,
            "row {id}: a paused store is not the row's fault"
        );
        assert!(is_dirty(&r), "row {id}: not marked synced");
        let next = r.next_attempt_at.unwrap();
        assert!(
            next >= before + chrono::Duration::seconds(299),
            "row {id}: waits for Firestore's pause end"
        );
    }
}

#[tokio::test]
async fn drive_failure_pauses_drive_only() {
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_B))]);
    w.fail(
        "drive_upload",
        Failure::from_status("drive upload", 503, "backend"),
    );
    assert_eq!(
        w.process(&row, now()).await,
        Outcome::Deferred(now() + chrono::Duration::seconds(300))
    );
    assert_eq!(
        w.google_calls(),
        vec!["fs_write doc1", "drive_upload file1"]
    );
    let after = w.store.row(1).unwrap();
    assert_eq!(after.attempts, 0, "N8: no attempt counted");
    assert_eq!(
        after.next_attempt_at,
        Some(now() + chrono::Duration::seconds(300))
    );
    assert_eq!(after.synced_at, row.synced_at, "not marked synced");
    let st = w.state.borrow();
    assert!(st.drive_pause.is_some() && st.firestore_pause.is_none());
}

#[tokio::test]
async fn after_the_pause_only_the_missing_store_is_written() {
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_B))]);
    w.fail(
        "drive_upload",
        Failure::from_status("drive upload", 503, "backend"),
    );
    w.process(&row, now()).await;
    w.google.failures.borrow_mut().clear();
    w.log.borrow_mut().clear();

    let later = now() + chrono::Duration::seconds(301);
    let mid = w.store.row(1).unwrap();
    assert_eq!(
        w.process(&mid, later).await,
        Outcome::Synced { uploaded: true }
    );
    assert_eq!(
        w.google_calls(),
        vec!["drive_upload file1"],
        "G1 not rewritten"
    );
    let done = w.store.row(1).unwrap();
    assert!(!is_dirty(&done));
    assert_eq!(
        done.last_upload_at,
        Some(now()),
        "the older of the two writes"
    );
}

#[tokio::test]
async fn a_row_failure_in_one_store_is_charged_while_the_other_pauses() {
    let row = synced_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_B))]);
    w.fail(
        "fs_write",
        Failure::from_status("firestore write", 400, "Invalid argument"),
    );
    w.fail(
        "drive_upload",
        Failure::from_status("drive upload", 500, "backend"),
    );
    let out = w.process(&row, now()).await;
    assert!(
        matches!(out, Outcome::Failed(ref f) if f.scope == Scope::Row),
        "{out:?}"
    );
    assert_eq!(w.store.row(1).unwrap().attempts, 1);
    assert!(w.state.borrow().drive_pause.is_some());
}

#[tokio::test]
async fn paused_drive_and_no_file_id_still_writes_firestore() {
    let row = new_row();
    let w = World::new(vec![row.clone()], vec![(7, copy(SHA_A))]);
    w.state.borrow_mut().drive_pause = Some(StorePause {
        until: now() + chrono::Duration::seconds(120),
        reason: "x".into(),
    });
    assert_eq!(
        w.process(&row, now()).await,
        Outcome::Deferred(now() + chrono::Duration::seconds(120))
    );
    let calls = w.google_calls();
    assert_eq!(calls.len(), 1, "{calls:?}");
    assert!(
        calls[0].starts_with("fs_write "),
        "no generateIds while Drive is paused"
    );
    let r = w.store.row(1).unwrap();
    assert!(r.firestore_doc_id.is_some() && r.drive_file_id.is_none());
    assert_eq!(r.attempts, 0);
}

#[tokio::test]
async fn both_stores_paused_means_no_work_at_all() {
    let w = World::new(vec![dirty_row(1, 7, 5)], vec![(7, copy(SHA_B))]);
    let now = chrono::Utc::now().naive_utc();
    let mut state = LoopState {
        firestore_pause: Some(StorePause {
            until: now + chrono::Duration::seconds(200),
            reason: "a".into(),
        }),
        drive_pause: Some(StorePause {
            until: now + chrono::Duration::seconds(100),
            reason: "b".into(),
        }),
        ..LoopState::default()
    };
    let p = pause_of(w.batch(&mut state).await);
    assert!(p.length <= Duration::from_secs(100) && p.length >= Duration::from_secs(98));
    assert!(w.events().is_empty(), "not even the panel is asked");
}

#[tokio::test]
async fn invalid_grant_pauses_only_its_store() {
    let w = World::new(vec![dirty_row(1, 7, 1)], vec![(7, copy(SHA_B))]);
    let mut f = Failure::account("drive token refused: HTTP 400 invalid_grant");
    f.invalid_grant = true;
    w.fail("drive_upload", f);
    let mut state = LoopState::default();
    assert_eq!(w.batch(&mut state).await, BatchEnd::Done);
    assert!(state.drive_pause.is_some() && state.firestore_pause.is_none());
    assert!(w.events().contains(&"fs_write doc1".to_string()));
}

#[tokio::test]
async fn the_lock_is_checked_before_every_row() {
    let rows = vec![dirty_row(1, 7, 5), dirty_row(2, 8, 1)];
    let w = World::new(rows, vec![(7, copy(SHA_B)), (8, copy(SHA_B))]);
    w.lock.allow.set(Some(1)); // held for the first row only
    let mut state = LoopState::default();
    assert_eq!(w.batch(&mut state).await, BatchEnd::LockLost);
    let ev = w.events();
    assert_eq!(ev.iter().filter(|e| *e == "lock?").count(), 2);
    assert!(ev.contains(&"panel 7".to_string()));
    assert!(
        !ev.contains(&"panel 8".to_string()),
        "no work after the lock is gone"
    );
    assert_eq!(ev[0], "lock?", "checked before the first row too");
}

#[tokio::test]
async fn panel_503_stops_the_batch_for_a_minute() {
    let rows = vec![dirty_row(1, 7, 5), dirty_row(2, 8, 1)];
    let w = World::new(rows, vec![(7, PanelAnswer::Disabled), (8, copy(SHA_B))]);
    let mut state = LoopState::default();
    let pause = pause_of(w.batch(&mut state).await);
    assert_eq!(pause.length, PANEL_DISABLED_PAUSE);
    assert!(state.panel_disabled);
    assert_eq!(w.work(), vec!["panel 7"]);
    assert_eq!(w.store.row(1).unwrap().attempts, 0);
}

#[tokio::test]
async fn panel_401_pauses_five_minutes() {
    let w = World::new(
        vec![dirty_row(1, 7, 5)],
        vec![(7, PanelAnswer::Unauthorized(403))],
    );
    let mut state = LoopState::default();
    let pause = pause_of(w.batch(&mut state).await);
    assert_eq!(pause.length, PANEL_AUTH_PAUSE);
    assert_eq!(w.store.row(1).unwrap().attempts, 0);
}

#[tokio::test]
async fn unreachable_panel_pauses_15s_and_charges_no_row() {
    for why in [
        "panel: no answer (connect failed)",
        "panel: HTTP 502",
        "panel: HTTP 504",
    ] {
        let rows = vec![dirty_row(1, 7, 5), dirty_row(2, 8, 1)];
        let w = World::new(
            rows.clone(),
            vec![(7, PanelAnswer::Unreachable(why.into())), (8, copy(SHA_B))],
        );
        let mut state = LoopState::default();
        let pause = pause_of(w.batch(&mut state).await);
        assert_eq!(pause.length, PANEL_UNREACHABLE_PAUSE, "{why}");
        assert!(state.panel_unreachable);
        assert_eq!(w.work(), vec!["panel 7"], "batch stopped at the first row");
        assert_eq!(w.store.row(1).unwrap(), rows[0], "row 1 not charged");
        assert_eq!(w.store.row(2).unwrap(), rows[1]);
    }
}

#[tokio::test]
async fn a_panel_timeout_is_charged_and_the_batch_goes_on() {
    let rows = vec![dirty_row(1, 7, 5), dirty_row(2, 8, 1)];
    let w = World::new(
        rows,
        vec![
            (7, PanelAnswer::Error("panel: no answer (timeout)".into())),
            (8, copy(SHA_B)),
        ],
    );
    let mut state = LoopState::default();
    assert_eq!(w.batch(&mut state).await, BatchEnd::Done);
    assert_eq!(
        w.store.row(1).unwrap().attempts,
        1,
        "the slow row backs off"
    );
    assert!(
        !is_dirty(&w.store.row(2).unwrap()),
        "the next row is not stalled"
    );
}

#[tokio::test]
async fn a_failing_select_warns_at_most_once_a_minute() {
    let w = World::new(vec![], vec![]);
    *w.store.fail_due.borrow_mut() =
        Some("micky_copies does not exist yet (the panel's migration has not run)".into());
    let mut state = LoopState::default();
    assert_eq!(w.batch(&mut state).await, BatchEnd::Done);
    let first = state.last_db_warn.expect("first failure is logged");
    w.batch(&mut state).await;
    assert_eq!(
        state.last_db_warn,
        Some(first),
        "second one within a minute is not"
    );
    // A minute later it is logged again.
    state.last_db_warn = Some(first - DB_WARN_EVERY);
    w.batch(&mut state).await;
    assert!(state.last_db_warn.unwrap() > first);
}

#[tokio::test]
async fn rows_in_backoff_are_not_selected() {
    let now = chrono::Utc::now().naive_utc();
    let mut r = dirty_row(1, 7, 1);
    r.next_attempt_at = Some(now + chrono::Duration::minutes(1));
    let w = World::new(vec![r], vec![(7, copy(SHA_B))]);
    let mut state = LoopState::default();
    assert_eq!(w.batch(&mut state).await, BatchEnd::Done);
    assert!(w.events().is_empty());
}

// ------------------------------------------------------------- the logs ----

#[test]
fn ages_are_compact() {
    assert_eq!(fmt_age(-5), "0s");
    assert_eq!(fmt_age(42), "42s");
    assert_eq!(fmt_age(185), "3m05s");
    assert_eq!(fmt_age(7620), "2h07m");
}

#[test]
fn backlog_line_reads_well() {
    let now_i = Instant::now();
    let b = Backlog {
        dirty: 3,
        oldest_dirty: Some(now() - chrono::Duration::seconds(75)),
        failing: 1,
    };
    let state = LoopState {
        last_error: Some((now_i, "user 7: drive upload: HTTP 500".into())),
        ..LoopState::default()
    };
    let paused = (
        now_i + Duration::from_secs(120),
        "Google account-wide failure: x".to_string(),
    );
    assert_eq!(
        backlog_line(&b, now(), &state, Some(&paused), now_i),
        "backlog: 3 need sync, oldest dirty 1m15s, 1 failing; PAUSED 2m00s more \
         (Google account-wide failure: x); last error 0s ago: user 7: drive upload: HTTP 500"
    );
    assert_eq!(
        backlog_line(
            &Backlog::default(),
            now(),
            &LoopState::default(),
            None,
            now_i
        ),
        "backlog: 0 need sync, oldest dirty -, 0 failing; no errors since start"
    );
}

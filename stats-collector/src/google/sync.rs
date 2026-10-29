//! The sync loop and the per-row decision flow.
//!
//! ## One tick (every [`TICK`])
//!
//! ```text
//!   SELECT ≤ 50 due rows: dirty first, then oldest dirty_at
//!   for each row, ONE AT A TIME, after confirming the single-instance lock:
//!       GET panel copy ──404 + x-copy-gone: 1──► clear synced_at/sha/last_upload
//!                                 (only if dirty_at is unchanged), then delete
//!                                 G1, G2, and the row
//!                     ──bare 404──► this row backs off; NOTHING is deleted
//!                     ──connect failure / 502 / 504──► stop; panel restarting;
//!                                 15 s; no row is charged a failure
//!                     ──503──► stop; the feature is off; look again in 60 s
//!                     ─401/3─► stop; wrong secret; ERROR, pause 5 min
//!                     ─other (incl. timeout)─► this row backs off
//!       reserve ids that are missing (saved on the row BEFORE first use,
//!           with synced_at = NULL in the same UPDATE)
//!       upload needed?  (first time, key changed, sha changed, or 5.5 h old)
//!           G1: Firestore PATCH                      } each skipped while its
//!           G2: create+upload+share, or upload       } own store is paused
//!               (+share on the 5.5 h refresh; a missing, trashed or
//!               forbidden file → new id, recreate the file)
//!       every needed step done → synced_at = the dirty_at READ at the start,
//!                 only if the row still has the ids we wrote
//!       a step failed for this row → attempts+1, backoff
//!       a store paused / failed account-wide → pause THAT store 5 min; the
//!                 row waits for it (next_attempt_at, no attempt counted)
//!       before the first store write of a pass → content_sha = NULL
//!                 (write-ahead), so a stop or a failure after any write
//!                 leaves the next pass writing both stores
//! ```
//!
//! ## Why `synced_at` is the value READ, not now
//!
//! The panel bumps `dirty_at` on every change, in the change's own
//! transaction. Writing back the `dirty_at` we read (bound exactly, all six
//! fractional digits) means a change that lands while we upload leaves
//! `dirty_at > synced_at`, so the row is picked up again on the next tick.
//! Nothing is lost and nothing is locked.
//!
//! Strictly, `synced_at` gets the later of the `dirty_at` and the
//! `key_changed_at` read — both values read, never "now". The panel's revoke
//! sets both to the same instant, so this IS the `dirty_at` read; the `max` is
//! only a guard: were the key ever stamped even a microsecond after its dirty
//! mark, `synced_at < key_changed_at` would stay true after every successful
//! upload and the row would be re-uploaded every 5 s forever.
//!
//! ## Why a new id clears `synced_at`
//!
//! The panel sends the G headers (ids + key) only while `synced_at` is set and
//! not older than `key_changed_at`. A freshly reserved id names a copy that
//! does not exist yet — above all when a Drive file deleted by hand is
//! replaced — so the UPDATE that saves it also sets `synced_at = NULL`, and
//! phones are not told about the new id until the success path has created,
//! uploaded and shared it and written `synced_at` again.
//!
//! ## Why a marked 404 clears the sync state first
//!
//! Before anything is deleted at Google, `synced_at`, `content_sha` and
//! `last_upload_at` are cleared — guarded by the `dirty_at` read, so a user
//! who came back to Micky in the meantime is left alone (nothing deleted).
//! If a delete then fails, or the user comes back after the copies are gone,
//! the row is no longer "synced with this sha": the panel withholds the G
//! headers and the next pass uploads again, instead of marking it synced
//! without an upload and handing phones the ids of deleted copies. When the
//! user comes back between the deletes and the row DELETE, the ids of the
//! objects already deleted are released so fresh ones are reserved.
//!
//! ## Each store pauses on its own
//!
//! Firestore and Drive have separate quotas (Firestore's free tier stops at
//! 20k writes a day with a 429), so an account-wide failure pauses only the
//! store it came from. A row whose copies are written to one store while the
//! other is paused is NOT marked synced; it waits for the paused store, and
//! [`LoopState`] remembers what was already written, so the retry writes only
//! the missing store instead of re-uploading both every 5 minutes.
//!
//! ## The seams
//!
//! [`Panel`], [`Google`], [`Store`] and [`Lock`] are small traits so the
//! decision flow is tested with fakes: no network, no MySQL.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use chrono::NaiveDateTime;

use super::drive::Upload;
use super::error::{backoff, Failure, Scope, ACCOUNT_PAUSE};
use super::firestore::new_doc_id;
use super::panel::{Copy, PanelAnswer};

/// How often due rows are selected.
pub const TICK: Duration = Duration::from_secs(5);

/// Rows per tick.
pub const BATCH_SIZE: u32 = 50;

/// A clean row is re-checked against the panel this often. The check is a
/// render on the panel plus a sha compare, so it catches anything no panel
/// hook marks dirty (hosts mirrored from other panels, a lost mark) for the
/// cost of one local HTTP call.
pub const CHECK_EVERY: chrono::Duration = chrono::Duration::minutes(30);

/// An unchanged copy is still re-uploaded this often, so the usage line in it
/// (excluded from the sha) stays current. The contract says "at least every
/// 6 hours"; the half hour of margin covers the backlog and a retry.
pub const REUPLOAD_EVERY: chrono::Duration = chrono::Duration::minutes(5 * 60 + 30);

/// Pause while the panel answers 503 (feature switched off).
pub const PANEL_DISABLED_PAUSE: Duration = Duration::from_secs(60);

/// Pause after the panel refuses our secret (401/403).
pub const PANEL_AUTH_PAUSE: Duration = Duration::from_secs(5 * 60);

/// Pause while the panel does not answer (restarting during a deploy). Short:
/// the panel is usually back within seconds, and no row is charged for it.
pub const PANEL_UNREACHABLE_PAUSE: Duration = Duration::from_secs(15);

/// A failing row SELECT (no database, or no `micky_copies` table yet on a
/// first deploy before the panel's migration) is logged at most this often.
pub const DB_WARN_EVERY: Duration = Duration::from_secs(60);

/// One backlog line this often.
pub const BACKLOG_EVERY: Duration = Duration::from_secs(60);

/// The columns of `micky_copies` this service reads. `content_key` is NOT
/// among them, on purpose: the key never leaves the panel.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CopyRow {
    pub id: i32,
    pub user_id: i32,
    pub key_changed_at: NaiveDateTime,
    pub firestore_doc_id: Option<String>,
    pub drive_file_id: Option<String>,
    pub content_sha: Option<String>,
    pub dirty_at: NaiveDateTime,
    pub synced_at: Option<NaiveDateTime>,
    pub checked_at: Option<NaiveDateTime>,
    pub last_upload_at: Option<NaiveDateTime>,
    pub attempts: i32,
    pub next_attempt_at: Option<NaiveDateTime>,
}

/// The panel changed something this row's copies do not have yet. Mirrors the
/// first half of the selection SQL in `store.rs`.
pub fn is_dirty(row: &CopyRow) -> bool {
    match row.synced_at {
        None => true,
        Some(synced) => row.dirty_at > synced || synced < row.key_changed_at,
    }
}

/// The row should be worked on now. Mirrors the whole selection SQL in
/// `store.rs`; rows the SQL returns are filtered through this as well.
pub fn is_due(row: &CopyRow, now: NaiveDateTime) -> bool {
    let wanted = is_dirty(row)
        || match row.checked_at {
            None => true,
            Some(checked) => checked < now - CHECK_EVERY,
        };
    let allowed = match row.next_attempt_at {
        None => true,
        Some(next) => next <= now,
    };
    wanted && allowed
}

/// What one row needs, given the copy the panel just rendered.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Plan {
    /// Write the blob to both stores.
    pub upload: bool,
    /// Never synced: Drive gets create + upload + share.
    pub first_time: bool,
    /// Drive needs create (+ share) before the upload: first time, or the row
    /// has no file id yet.
    pub create_drive: bool,
    /// The periodic re-upload is due. Drive re-applies the anyone/reader
    /// permission then too (idempotent), so a hand-removed share heals.
    pub refresh: bool,
}

pub fn plan(row: &CopyRow, sha: &str, now: NaiveDateTime) -> Plan {
    let first_time = row.synced_at.is_none();
    let key_changed = row.synced_at.is_some_and(|s| s < row.key_changed_at);
    let sha_changed = row.content_sha.as_deref() != Some(sha);
    let stale = match row.last_upload_at {
        None => true,
        Some(t) => t < now - REUPLOAD_EVERY,
    };
    let missing_id = row.firestore_doc_id.is_none() || row.drive_file_id.is_none();
    Plan {
        upload: first_time || key_changed || sha_changed || stale || missing_id,
        first_time,
        create_drive: first_time || row.drive_file_id.is_none(),
        refresh: stale,
    }
}

/// The `synced_at` written after a successful pass: values READ from the row,
/// never the clock. Revoke sets `dirty_at` and `key_changed_at` to the same
/// instant, so this is the `dirty_at` read; the `max` only guards against the
/// key ever being stamped after its dirty mark (see the module docs).
pub fn synced_value(row: &CopyRow) -> NaiveDateTime {
    row.dirty_at.max(row.key_changed_at)
}

/// The first 6 characters of a doc or file id, for logs: enough to tell
/// copies apart, not enough to fetch one.
pub fn id6(id: &str) -> &str {
    id.get(..6).unwrap_or(id)
}

/// The panel endpoint.
#[allow(async_fn_in_trait)] // only ever awaited on the main task; no Send needed
pub trait Panel {
    async fn fetch_copy(&self, user_id: i32) -> PanelAnswer;
}

/// The two Google stores.
#[allow(async_fn_in_trait)]
pub trait Google {
    /// Create or replace the Firestore document.
    async fn firestore_write(&self, doc_id: &str, blob: &[u8]) -> Result<(), Failure>;
    /// 404 counts as success.
    async fn firestore_delete(&self, doc_id: &str) -> Result<(), Failure>;
    async fn drive_generate_id(&self) -> Result<String, Failure>;
    /// 409 (exists) counts as success (`Done`). `Trashed` when the created
    /// file reports `"trashed": true`. Never `NotFound`.
    async fn drive_create(&self, file_id: &str) -> Result<Upload, Failure>;
    async fn drive_upload(&self, file_id: &str, blob: &[u8]) -> Result<Upload, Failure>;
    async fn drive_share(&self, file_id: &str) -> Result<(), Failure>;
    /// 404 counts as success.
    async fn drive_delete(&self, file_id: &str) -> Result<(), Failure>;
}

/// The single-instance lock, confirmed before every row.
#[allow(async_fn_in_trait)]
pub trait Lock {
    async fn held(&self) -> bool;
}

/// Which id column an update touches.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IdColumn {
    Firestore,
    Drive,
}

/// Numbers for the backlog line.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Backlog {
    /// Rows whose copies are behind the panel.
    pub dirty: i64,
    /// `dirty_at` of the oldest of those.
    pub oldest_dirty: Option<NaiveDateTime>,
    /// Rows whose last attempt failed.
    pub failing: i64,
}

/// `micky_copies`. Errors are strings: a DB problem ends the batch, whatever
/// it was.
#[allow(async_fn_in_trait)]
pub trait Store {
    async fn due_rows(&self, now: NaiveDateTime, limit: u32) -> Result<Vec<CopyRow>, String>;
    /// Set an id column from `old` (NULL when `None`) to `new`, and
    /// `synced_at` to NULL in the SAME statement, so the panel withholds the G
    /// headers until a copy exists under the new id. `Ok(false)`: the row is
    /// gone or its id is no longer `old`.
    async fn save_id(
        &self,
        row_id: i32,
        column: IdColumn,
        old: Option<&str>,
        new: &str,
    ) -> Result<bool, String>;
    /// Record success — only if the row still holds the ids we wrote under.
    /// `last_upload`: when the copies now in the stores were written (`None`:
    /// nothing was uploaded, keep the column). `Ok(false)`: the ids changed
    /// under us; nothing was written.
    #[allow(clippy::too_many_arguments)]
    async fn mark_synced(
        &self,
        row_id: i32,
        synced_at: NaiveDateTime,
        now: NaiveDateTime,
        sha: &str,
        last_upload: Option<NaiveDateTime>,
        doc_id: &str,
        file_id: &str,
    ) -> Result<bool, String>;
    /// A failure of this row: attempts + 1, last_error, next_attempt_at.
    /// `clear_sha`: a write to some store was attempted this pass, so the
    /// stores may no longer match `content_sha` — NULL it in the same UPDATE.
    async fn mark_failed(
        &self,
        row_id: i32,
        message: &str,
        next_attempt_at: NaiveDateTime,
        clear_sha: bool,
    ) -> Result<(), String>;
    /// Not this row's fault (a paused store): only `next_attempt_at`; the
    /// attempt is not counted. `clear_sha` as for `mark_failed`.
    async fn defer(
        &self,
        row_id: i32,
        next_attempt_at: NaiveDateTime,
        clear_sha: bool,
    ) -> Result<(), String>;
    /// Write-ahead, before the first store write of a pass: `content_sha =
    /// NULL`, so a stop between that write and the row's final UPDATE never
    /// leaves the old sha claiming copies that changed.
    async fn clear_sha(&self, row_id: i32) -> Result<(), String>;
    /// Before a marked-404 delete: set `synced_at`, `content_sha` and
    /// `last_upload_at` to NULL if `dirty_at` is still `seen_dirty`.
    /// `Ok(false)`: the panel touched the row since (the user may be back).
    async fn clear_sync_state(
        &self,
        row_id: i32,
        seen_dirty: NaiveDateTime,
    ) -> Result<bool, String>;
    /// Delete the row if its `dirty_at` is still `seen_dirty`. `Ok(false)`:
    /// the panel touched it in the meantime (the user may be back).
    async fn delete_row(&self, row_id: i32, seen_dirty: NaiveDateTime) -> Result<bool, String>;
    /// The user came back after these objects were deleted: NULL each id
    /// column that still names a deleted object (`None` = leave it), plus
    /// `synced_at`, so fresh ids are reserved.
    async fn release_ids(
        &self,
        row_id: i32,
        deleted_doc: Option<&str>,
        deleted_file: Option<&str>,
    ) -> Result<(), String>;
    async fn backlog(&self) -> Result<Backlog, String>;
}

/// One of the two Google stores, for per-store pauses.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Target {
    Firestore,
    Drive,
}

impl Target {
    pub fn name(self) -> &'static str {
        match self {
            Target::Firestore => "Firestore",
            Target::Drive => "Drive",
        }
    }
}

/// One store's pause.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StorePause {
    pub until: NaiveDateTime,
    pub reason: String,
}

/// A write to one store not yet covered by `synced_at` (the other store was
/// paused or failed).
#[derive(Debug, Clone, PartialEq, Eq)]
struct Mark {
    sha: String,
    key_changed_at: NaiveDateTime,
    id: String,
    at: NaiveDateTime,
}

/// What happened to one row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    /// Both copies are current. `uploaded` = bytes were written this time.
    Synced { uploaded: bool },
    /// The user is gone: both copies and the row were deleted.
    Deleted,
    /// The row changed or vanished under us; the next tick re-reads it.
    RowChanged,
    /// Waiting for a paused store; `next_attempt_at` set, no attempt counted.
    Deferred(NaiveDateTime),
    /// Failure recorded on the row (attempts, last_error, next_attempt_at).
    Failed(Failure),
    /// The panel answered 404 WITHOUT `x-copy-gone: 1`. Recorded on the row
    /// like any per-row failure; nothing was deleted.
    UnmarkedNotFound(Failure),
    /// Panel 503: stop the batch.
    PanelDisabled,
    /// Connect failure, 502 or 504 from the panel: stop the batch, charge no
    /// row.
    PanelUnreachable(String),
    /// Panel 401/403: stop the batch.
    PanelUnauthorized(u16),
    /// MySQL failed: stop the batch.
    StoreError(String),
}

/// Why the loop is pausing, and for how long.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pause {
    pub length: Duration,
    pub reason: String,
}

/// Loop state that outlives one batch.
#[derive(Debug, Default)]
pub struct LoopState {
    /// Last failure seen, any kind, with when it happened.
    pub last_error: Option<(Instant, String)>,
    /// The panel answered 503 last time (logged once per transition).
    pub panel_disabled: bool,
    /// The bare-404 WARN was logged in this batch (once per batch).
    pub warned_unmarked_404: bool,
    /// The panel did not answer last time (logged once per transition).
    pub panel_unreachable: bool,
    /// When a failing row SELECT was last logged (see [`DB_WARN_EVERY`]).
    pub last_db_warn: Option<Instant>,
    pub firestore_pause: Option<StorePause>,
    pub drive_pause: Option<StorePause>,
    /// Partial writes, by (row id, store). In memory only: a restart costs
    /// one extra write.
    written: HashMap<(i32, Target), Mark>,
}

impl LoopState {
    /// The end of `t`'s pause, if it is paused at `now`.
    pub fn paused_until(&self, t: Target, now: NaiveDateTime) -> Option<NaiveDateTime> {
        let p = match t {
            Target::Firestore => &self.firestore_pause,
            Target::Drive => &self.drive_pause,
        };
        p.as_ref().filter(|p| p.until > now).map(|p| p.until)
    }

    /// Pause `t` after an account-wide failure. Returns the pause end.
    fn pause(&mut self, t: Target, now: NaiveDateTime, user: i32, f: &Failure) -> NaiveDateTime {
        let secs = ACCOUNT_PAUSE.as_secs();
        let until = now + chrono::Duration::seconds(secs as i64);
        let name = t.name();
        if f.invalid_grant {
            tracing::error!("user {user}: {}; pausing {name} for {secs}s", f.message);
        } else {
            tracing::warn!(
                "user {user}: {} — account-wide; pausing {name} for {secs}s (the other store carries on)",
                f.message
            );
        }
        self.last_error = Some((Instant::now(), format!("user {user}: {}", f.message)));
        let p = Some(StorePause {
            until,
            reason: f.message.clone(),
        });
        match t {
            Target::Firestore => self.firestore_pause = p,
            Target::Drive => self.drive_pause = p,
        }
        until
    }

    /// When this exact copy (same sha, same key, same id) was already written
    /// to `t` and is still fresh, the time it was written.
    fn written_at(
        &self,
        row: &CopyRow,
        t: Target,
        sha: &str,
        id: &str,
        now: NaiveDateTime,
    ) -> Option<NaiveDateTime> {
        self.written
            .get(&(row.id, t))
            .filter(|m| {
                m.sha == sha
                    && m.key_changed_at == row.key_changed_at
                    && m.id == id
                    && now - m.at < REUPLOAD_EVERY
            })
            .map(|m| m.at)
    }

    fn remember(&mut self, row: &CopyRow, t: Target, sha: &str, id: &str, now: NaiveDateTime) {
        self.written.insert(
            (row.id, t),
            Mark {
                sha: sha.to_string(),
                key_changed_at: row.key_changed_at,
                id: id.to_string(),
                at: now,
            },
        );
    }

    /// Drop what is remembered for (row, `t`). Called right before every
    /// write to `t`: a write that fails (a timeout may still have landed)
    /// leaves the store's content unknown, so the old record must not be
    /// trusted afterwards.
    fn unremember(&mut self, row_id: i32, t: Target) {
        self.written.remove(&(row_id, t));
    }

    fn forget(&mut self, row_id: i32) {
        self.written.remove(&(row_id, Target::Firestore));
        self.written.remove(&(row_id, Target::Drive));
    }

    /// Both stores paused: nothing can be done until the earlier one resumes.
    fn both_paused(&self, now: NaiveDateTime) -> Option<Pause> {
        let fs = self.paused_until(Target::Firestore, now)?;
        let dr = self.paused_until(Target::Drive, now)?;
        Some(Pause {
            length: (fs.min(dr) - now).to_std().unwrap_or(Duration::ZERO),
            reason: "Firestore and Drive are both paused".to_string(),
        })
    }
}

/// How one store's part of a row ended.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Part {
    /// Written now, written earlier (remembered), or nothing to do.
    Done,
    /// The store is paused (maybe by this very row): try again at this time.
    Later(NaiveDateTime),
    /// This row's own problem: charge it.
    Failed(Failure),
}

/// A Google failure becomes a store pause (account-wide) or this row's
/// failure.
fn part_for(state: &mut LoopState, t: Target, now: NaiveDateTime, user: i32, f: Failure) -> Part {
    match f.scope {
        Scope::Account => Part::Later(state.pause(t, now, user, &f)),
        Scope::Row => Part::Failed(f),
    }
}

enum StepError {
    Google(Failure),
    Store(String),
    RowChanged,
}

impl From<Failure> for StepError {
    fn from(f: Failure) -> Self {
        StepError::Google(f)
    }
}

/// Work one row as far as the stores allow. Never panics; every failure is
/// classified and, when it belongs to the row, recorded on it.
pub async fn process_row<P: Panel, G: Google, S: Store>(
    panel: &P,
    google: &G,
    store: &S,
    state: &mut LoopState,
    row: &CopyRow,
    now: NaiveDateTime,
) -> Outcome {
    let copy = match panel.fetch_copy(row.user_id).await {
        PanelAnswer::Copy(c) => c,
        PanelAnswer::Gone => return delete_copies(google, store, state, row, now).await,
        PanelAnswer::UnmarkedNotFound => {
            let f = Failure::row("panel: HTTP 404 without x-copy-gone");
            return match record_failure(store, row, now, f, false).await {
                Outcome::Failed(f) => Outcome::UnmarkedNotFound(f),
                other => other,
            };
        }
        PanelAnswer::Disabled => return Outcome::PanelDisabled,
        PanelAnswer::Unreachable(why) => return Outcome::PanelUnreachable(why),
        PanelAnswer::Unauthorized(s) => return Outcome::PanelUnauthorized(s),
        PanelAnswer::Error(msg) => {
            return record_failure(store, row, now, Failure::row(msg), false).await
        }
    };

    let progress = match write_copies(google, store, state, row, &copy, now).await {
        Ok(p) => p,
        // Not produced by write_copies today; if it ever is, a write may have
        // happened, so the sha is cleared.
        Err(StepError::Google(f)) => return record_failure(store, row, now, f, true).await,
        Err(StepError::Store(e)) => return Outcome::StoreError(e),
        Err(StepError::RowChanged) => {
            state.forget(row.id);
            return Outcome::RowChanged;
        }
    };

    // Not every needed step worked: the row stays unsynced, and if anything
    // was written (or may have been) its content_sha goes, so the next pass
    // writes BOTH stores — otherwise content going A→B→A before the retry
    // would look unchanged and leave G1 and G2 holding different copies.
    let parts = [&progress.firestore, &progress.drive];
    if let Some(outcome) = settle(store, row, now, parts, progress.attempted_write).await {
        return outcome;
    }
    let Some(file_id) = progress.file_id else {
        // Unreachable: a missing Drive id always leaves the Drive part
        // `Later` or `Failed`. Charged rather than trusted.
        let f = Failure::row("drive: no file id");
        return record_failure(store, row, now, f, progress.attempted_write).await;
    };
    let result = store
        .mark_synced(
            row.id,
            synced_value(row),
            now,
            &copy.sha,
            progress.uploaded_at,
            &progress.doc_id,
            &file_id,
        )
        .await;
    match result {
        Ok(true) => {
            state.forget(row.id);
            Outcome::Synced {
                uploaded: progress.uploaded_at.is_some(),
            }
        }
        Ok(false) => {
            state.forget(row.id);
            tracing::info!(
                "user {}: row ids changed while syncing; re-checking next tick",
                row.user_id
            );
            Outcome::RowChanged
        }
        Err(e) => Outcome::StoreError(e),
    }
}

/// Turn the store parts into an outcome unless every part is done: a row
/// failure is charged (it wins over a pause), otherwise the row waits for the
/// earliest paused store without an attempt counted.
async fn settle<S: Store>(
    store: &S,
    row: &CopyRow,
    now: NaiveDateTime,
    parts: [&Part; 2],
    clear_sha: bool,
) -> Option<Outcome> {
    if let Some(f) = parts.iter().find_map(|p| match p {
        Part::Failed(f) => Some(f.clone()),
        _ => None,
    }) {
        return Some(record_failure(store, row, now, f, clear_sha).await);
    }
    let later = parts
        .iter()
        .filter_map(|p| match p {
            Part::Later(t) => Some(*t),
            _ => None,
        })
        .min()?;
    Some(match store.defer(row.id, later, clear_sha).await {
        Ok(()) => Outcome::Deferred(later),
        Err(e) => Outcome::StoreError(e),
    })
}

/// What `write_copies` got done, and under which ids.
struct Progress {
    firestore: Part,
    drive: Part,
    doc_id: String,
    file_id: Option<String>,
    /// When the copies now in the stores were written: the older of the two
    /// writes (one may be remembered from an earlier pass). `None`: nothing
    /// needed uploading.
    uploaded_at: Option<NaiveDateTime>,
    /// A write to either store was sent this pass (it may have landed even
    /// if it failed).
    attempted_write: bool,
}

/// Reserve missing ids, then write each store that is not paused.
async fn write_copies<G: Google, S: Store>(
    google: &G,
    store: &S,
    state: &mut LoopState,
    row: &CopyRow,
    copy: &Copy,
    now: NaiveDateTime,
) -> Result<Progress, StepError> {
    let user = row.user_id;
    let plan = plan(row, &copy.sha, now);

    // Ids first, saved before anything is written under them, so a crash at
    // any later point leaves nothing at Google that the row does not name.
    // Firestore's is made here; Drive's needs Drive.
    let doc_id = match &row.firestore_doc_id {
        Some(id) => id.clone(),
        None => {
            let id = new_doc_id();
            save_id(store, row.id, IdColumn::Firestore, None, &id).await?;
            id
        }
    };
    let mut drive_part: Option<Part> = None;
    let mut file_id = match &row.drive_file_id {
        Some(id) => Some(id.clone()),
        None => match state.paused_until(Target::Drive, now) {
            Some(until) => {
                drive_part = Some(Part::Later(until));
                None
            }
            None => match google.drive_generate_id().await {
                Ok(id) => {
                    save_id(store, row.id, IdColumn::Drive, None, &id).await?;
                    Some(id)
                }
                Err(f) => {
                    drive_part = Some(part_for(state, Target::Drive, now, user, f));
                    None
                }
            },
        },
    };

    if !plan.upload {
        tracing::debug!(
            "user {user}: copy unchanged (sha {}); nothing to upload",
            short(&copy.sha)
        );
        return Ok(Progress {
            firestore: Part::Done,
            drive: drive_part.unwrap_or(Part::Done),
            doc_id,
            file_id,
            uploaded_at: None,
            attempted_write: false,
        });
    }
    let mut written_at = now;
    let mut attempted_write = false;
    // Write-ahead: the stored sha goes BEFORE the first byte is sent, once
    // per pass. A SIGTERM or crash after a store write (before defer,
    // mark_failed or mark_synced) then leaves content_sha NULL, so the next
    // pass writes both stores even if the content went back to the old sha.
    let mut sha_cleared = false;

    // G1. PATCH creates the document when it does not exist yet.
    let firestore =
        if let Some(at) = state.written_at(row, Target::Firestore, &copy.sha, &doc_id, now) {
            written_at = written_at.min(at);
            Part::Done
        } else if let Some(until) = state.paused_until(Target::Firestore, now) {
            Part::Later(until)
        } else {
            if !sha_cleared {
                store.clear_sha(row.id).await.map_err(StepError::Store)?;
                sha_cleared = true;
            }
            state.unremember(row.id, Target::Firestore);
            attempted_write = true;
            match google.firestore_write(&doc_id, &copy.blob).await {
                Ok(()) => {
                    let fresh = plan.first_time || row.firestore_doc_id.is_none();
                    tracing::info!(
                        "user {user}: G1 {} (doc {}…, {} bytes)",
                        if fresh { "created" } else { "updated" },
                        id6(&doc_id),
                        copy.blob.len()
                    );
                    state.remember(row, Target::Firestore, &copy.sha, &doc_id, now);
                    Part::Done
                }
                Err(f) => part_for(state, Target::Firestore, now, user, f),
            }
        };

    // G2.
    let drive = match (drive_part, file_id.clone()) {
        (Some(part), _) => part,
        (None, None) => Part::Failed(Failure::row("drive: no file id")),
        (None, Some(id)) => {
            if let Some(at) = state.written_at(row, Target::Drive, &copy.sha, &id, now) {
                written_at = written_at.min(at);
                Part::Done
            } else if let Some(until) = state.paused_until(Target::Drive, now) {
                Part::Later(until)
            } else {
                if !sha_cleared {
                    store.clear_sha(row.id).await.map_err(StepError::Store)?;
                }
                state.unremember(row.id, Target::Drive);
                attempted_write = true;
                match drive_write(google, store, row, &plan, &id, &copy.blob).await {
                    Ok(final_id) => {
                        state.remember(row, Target::Drive, &copy.sha, &final_id, now);
                        file_id = Some(final_id);
                        Part::Done
                    }
                    Err(StepError::Google(f)) => part_for(state, Target::Drive, now, user, f),
                    Err(e) => return Err(e),
                }
            }
        }
    };

    Ok(Progress {
        firestore,
        drive,
        doc_id,
        file_id,
        uploaded_at: Some(written_at),
        attempted_write,
    })
}

/// What a create/upload/share found out about the Drive file.
enum FileState {
    Live,
    Missing,
    Trashed,
    /// A 403 whose reasons are all about this one file.
    Forbidden(Failure),
}

fn file_state(r: Result<Upload, Failure>) -> Result<FileState, StepError> {
    match r {
        Ok(Upload::Done) => Ok(FileState::Live),
        Ok(Upload::NotFound) => Ok(FileState::Missing),
        Ok(Upload::Trashed) => Ok(FileState::Trashed),
        Err(f) if f.file_level => Ok(FileState::Forbidden(f)),
        Err(f) => Err(f.into()),
    }
}

/// The Drive part of a row under `file_id`. Returns the id the copy ended up
/// under: a file that is missing, trashed or forbidden to us is replaced.
async fn drive_write<G: Google, S: Store>(
    google: &G,
    store: &S,
    row: &CopyRow,
    plan: &Plan,
    file_id: &str,
    blob: &[u8],
) -> Result<String, StepError> {
    let user = row.user_id;
    let mut state = FileState::Live;
    if plan.create_drive {
        state = file_state(google.drive_create(file_id).await)?;
    }
    if matches!(state, FileState::Live) {
        state = file_state(google.drive_upload(file_id, blob).await)?;
    }
    if matches!(state, FileState::Live) && (plan.create_drive || plan.refresh) {
        match google.drive_share(file_id).await {
            Ok(()) => {}
            Err(f) if f.file_level => state = FileState::Forbidden(f),
            Err(f) => return Err(f.into()),
        }
    }
    if matches!(state, FileState::Live) {
        tracing::info!(
            "user {user}: G2 {} (file {}…, {} bytes)",
            if plan.create_drive {
                "created"
            } else {
                "updated"
            },
            id6(file_id),
            blob.len()
        );
        return Ok(file_id.to_string());
    }
    replace_drive_file(google, store, row, file_id, blob, state).await
}

/// Missing, trashed or forbidden: a new file under a new id, in the same
/// ids-first order as a first create. Saving the new id also clears
/// `synced_at`, so the panel withholds the headers until the file is complete.
async fn replace_drive_file<G: Google, S: Store>(
    google: &G,
    store: &S,
    row: &CopyRow,
    old: &str,
    blob: &[u8],
    why: FileState,
) -> Result<String, StepError> {
    let user = row.user_id;
    let new = google.drive_generate_id().await?;
    save_id(store, row.id, IdColumn::Drive, Some(old), &new).await?;
    if google.drive_create(&new).await? != Upload::Done
        || google.drive_upload(&new, blob).await? != Upload::Done
    {
        return Err(Failure::row("drive: new file missing or trashed right after create").into());
    }
    google.drive_share(&new).await?;
    let (reason, purge) = match &why {
        FileState::Missing => ("was gone".to_string(), false),
        FileState::Trashed => ("was in the trash".to_string(), true),
        FileState::Forbidden(f) => (format!("is forbidden to us ({})", f.message), true),
        FileState::Live => ("was replaced".to_string(), false),
    };
    tracing::info!(
        "user {user}: G2 file {}… {reason}; created {}… ({} bytes)",
        id6(old),
        id6(&new),
        blob.len()
    );
    if purge {
        // Best effort: the old file is still ours (or at least not usable by
        // us); a failure here is only logged.
        match google.drive_delete(old).await {
            Ok(()) => tracing::info!("user {user}: old G2 file {}… deleted", id6(old)),
            Err(f) => tracing::warn!(
                "user {user}: could not delete old G2 file {}…: {}",
                id6(old),
                f.message
            ),
        }
    }
    Ok(new)
}

async fn save_id<S: Store>(
    store: &S,
    row_id: i32,
    column: IdColumn,
    old: Option<&str>,
    new: &str,
) -> Result<(), StepError> {
    match store.save_id(row_id, column, old, new).await {
        Ok(true) => Ok(()),
        Ok(false) => Err(StepError::RowChanged),
        Err(e) => Err(StepError::Store(e)),
    }
}

/// Marked 404 from the panel: the user is gone or no longer on Micky. The
/// sync state is cleared first, then each copy goes unless its store is
/// paused (already gone counts as done), the row last, so a failure part-way
/// leaves the row and its ids for the retry — no longer marked synced.
async fn delete_copies<G: Google, S: Store>(
    google: &G,
    store: &S,
    state: &mut LoopState,
    row: &CopyRow,
    now: NaiveDateTime,
) -> Outcome {
    let user = row.user_id;
    state.forget(row.id);
    // Guarded by the dirty_at read: a user who came back in the meantime
    // keeps everything. See the module docs.
    match store.clear_sync_state(row.id, row.dirty_at).await {
        Ok(true) => {}
        Ok(false) => {
            tracing::info!(
                "user {user}: panel says gone, but the row changed since it was read; nothing deleted"
            );
            return Outcome::RowChanged;
        }
        Err(e) => return Outcome::StoreError(e),
    }

    let mut deleted_doc: Option<&str> = None;
    let firestore = match &row.firestore_doc_id {
        None => Part::Done,
        Some(doc) => match state.paused_until(Target::Firestore, now) {
            Some(until) => Part::Later(until),
            None => match google.firestore_delete(doc).await {
                Ok(()) => {
                    tracing::info!("user {user}: G1 deleted (doc {}…)", id6(doc));
                    deleted_doc = Some(doc);
                    Part::Done
                }
                Err(f) => part_for(state, Target::Firestore, now, user, f),
            },
        },
    };
    let mut deleted_file: Option<&str> = None;
    let drive = match &row.drive_file_id {
        None => Part::Done,
        Some(file) => match state.paused_until(Target::Drive, now) {
            Some(until) => Part::Later(until),
            None => match google.drive_delete(file).await {
                Ok(()) => {
                    tracing::info!("user {user}: G2 deleted (file {}…)", id6(file));
                    deleted_file = Some(file);
                    Part::Done
                }
                Err(f) => part_for(state, Target::Drive, now, user, f),
            },
        },
    };
    // content_sha was already cleared by clear_sync_state.
    if let Some(outcome) = settle(store, row, now, [&firestore, &drive], false).await {
        return outcome;
    }

    match store.delete_row(row.id, row.dirty_at).await {
        Ok(true) => {
            tracing::info!("user {user}: user gone or no longer on Micky; copy row deleted");
            Outcome::Deleted
        }
        Ok(false) => {
            // The user came back after the copies were deleted. The ids now
            // name nothing: release them so fresh ones are reserved.
            match store.release_ids(row.id, deleted_doc, deleted_file).await {
                Ok(()) => {
                    tracing::info!(
                        "user {user}: came back while its copies were being deleted; ids released"
                    );
                    Outcome::RowChanged
                }
                Err(e) => Outcome::StoreError(e),
            }
        }
        Err(e) => Outcome::StoreError(e),
    }
}

async fn record_failure<S: Store>(
    store: &S,
    row: &CopyRow,
    now: NaiveDateTime,
    failure: Failure,
    clear_sha: bool,
) -> Outcome {
    let next = now + backoff(row.attempts);
    match store
        .mark_failed(row.id, &failure.for_db(), next, clear_sha)
        .await
    {
        Ok(()) => Outcome::Failed(failure),
        Err(e) => Outcome::StoreError(e),
    }
}

fn short(sha: &str) -> &str {
    sha.get(..12).unwrap_or(sha)
}

/// How a batch ended.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BatchEnd {
    /// Ran out of rows (or stopped for a DB error); next tick as usual.
    Done,
    /// Stop and pause the loop.
    Pause(Pause),
    /// The single-instance lock is no longer ours: stop all work.
    LockLost,
}

/// Select due rows and work through them one at a time, confirming the lock
/// before each one.
pub async fn run_batch<P: Panel, G: Google, S: Store, L: Lock>(
    panel: &P,
    google: &G,
    store: &S,
    lock: &L,
    state: &mut LoopState,
) -> BatchEnd {
    state.warned_unmarked_404 = false;
    let now = chrono::Utc::now().naive_utc();
    if let Some(p) = state.both_paused(now) {
        return BatchEnd::Pause(p);
    }
    let rows = match store.due_rows(now, BATCH_SIZE).await {
        Ok(rows) => rows,
        Err(e) => {
            let due = state
                .last_db_warn
                .is_none_or(|at| at.elapsed() >= DB_WARN_EVERY);
            if due {
                tracing::warn!("cannot read micky_copies: {e}");
                state.last_db_warn = Some(Instant::now());
            } else {
                tracing::debug!("cannot read micky_copies: {e}");
            }
            state.last_error = Some((Instant::now(), format!("database: {e}")));
            return BatchEnd::Done;
        }
    };

    for row in rows {
        let now = chrono::Utc::now().naive_utc();
        if let Some(p) = state.both_paused(now) {
            return BatchEnd::Pause(p);
        }
        if !is_due(&row, now) {
            // The SQL and is_due() are the same rule; disagreement means a
            // clock or schema surprise worth seeing, not worth acting on.
            tracing::debug!("user {}: selected but not due; skipped", row.user_id);
            continue;
        }
        if !lock.held().await {
            return BatchEnd::LockLost;
        }
        let outcome = process_row(panel, google, store, state, &row, now).await;
        match handle_outcome(&row, outcome, state) {
            Flow::Next => {}
            Flow::EndBatch => return BatchEnd::Done,
            Flow::Pause(p) => return BatchEnd::Pause(p),
        }
    }
    BatchEnd::Done
}

/// What the batch does after one row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Flow {
    Next,
    /// Stop this batch; the next tick starts normally.
    EndBatch,
    /// Stop this batch and pause the loop.
    Pause(Pause),
}

/// Log one row's outcome; decide whether the batch goes on.
pub fn handle_outcome(row: &CopyRow, outcome: Outcome, state: &mut LoopState) -> Flow {
    let user = row.user_id;
    match outcome {
        Outcome::Synced { .. } | Outcome::Deleted | Outcome::RowChanged | Outcome::Deferred(_) => {
            if let Outcome::Deferred(until) = outcome {
                tracing::debug!("user {user}: waiting for a paused store until {until}");
            }
            if state.panel_disabled {
                tracing::info!("panel serves Google copies again");
                state.panel_disabled = false;
            }
            if state.panel_unreachable {
                tracing::info!("panel answers again");
                state.panel_unreachable = false;
            }
            Flow::Next
        }
        Outcome::Failed(f) => {
            let retry = backoff(row.attempts).num_seconds();
            state.last_error = Some((Instant::now(), format!("user {user}: {}", f.message)));
            tracing::warn!(
                "user {user}: {} (attempt {}, retry in {retry}s)",
                f.message,
                row.attempts + 1
            );
            Flow::Next
        }
        Outcome::UnmarkedNotFound(f) => {
            state.last_error = Some((Instant::now(), format!("user {user}: {}", f.message)));
            if !state.warned_unmarked_404 {
                tracing::warn!(
                    "panel answered 404 without x-copy-gone: route missing or request proxied? \
                     Nothing is deleted; the affected rows back off (first: user {user})"
                );
                state.warned_unmarked_404 = true;
            } else {
                tracing::debug!("user {user}: {}", f.message);
            }
            Flow::Next
        }
        Outcome::PanelDisabled => {
            if !state.panel_disabled {
                tracing::info!(
                    "panel answers 503: Google copies are switched off; checking again every {}s",
                    PANEL_DISABLED_PAUSE.as_secs()
                );
                state.panel_disabled = true;
            }
            Flow::Pause(Pause {
                length: PANEL_DISABLED_PAUSE,
                reason: "panel has Google copies switched off (503)".to_string(),
            })
        }
        Outcome::PanelUnreachable(why) => {
            if !state.panel_unreachable {
                tracing::warn!(
                    "{why}: panel unreachable (restarting?); pausing {}s, no row is charged",
                    PANEL_UNREACHABLE_PAUSE.as_secs()
                );
                state.panel_unreachable = true;
            } else {
                tracing::debug!("{why}: panel still unreachable");
            }
            state.last_error = Some((Instant::now(), why.clone()));
            Flow::Pause(Pause {
                length: PANEL_UNREACHABLE_PAUSE,
                reason: format!("panel unreachable: {why}"),
            })
        }
        Outcome::PanelUnauthorized(status) => {
            let msg = format!(
                "panel refused GOOGLE_SYNC_SECRET (HTTP {status}); it must match the panel's own .env"
            );
            tracing::error!("{msg}; pausing {}s", PANEL_AUTH_PAUSE.as_secs());
            state.last_error = Some((Instant::now(), msg.clone()));
            Flow::Pause(Pause {
                length: PANEL_AUTH_PAUSE,
                reason: msg,
            })
        }
        Outcome::StoreError(e) => {
            tracing::warn!("user {user}: database error, ending this batch: {e}");
            state.last_error = Some((Instant::now(), format!("database: {e}")));
            // The next tick retries; a DB outage needs no extra pause.
            Flow::EndBatch
        }
    }
}

/// Run until the single-instance lock is lost (checked every pass and before
/// every row): a batch every [`TICK`], a backlog line every
/// [`BACKLOG_EVERY`], pauses honoured. Also cancelled by
/// dropping the future (the binary races it against SIGTERM); every step is
/// safe to interrupt because ids are saved before use and `synced_at` is
/// written last.
pub async fn run<P: Panel, G: Google, S: Store, L: Lock>(
    panel: &P,
    google: &G,
    store: &S,
    lock: &L,
) {
    let mut state = LoopState::default();
    let mut paused: Option<(Instant, String)> = None;
    let mut next_backlog = Instant::now() + BACKLOG_EVERY;

    loop {
        // Every pass, not only before rows: with nothing due the lock
        // connection would otherwise sit idle until MySQL (or something in
        // between) drops it — and the lock with it. Wakes are at most a
        // minute apart (tick, pause end, or the backlog line).
        if !lock.held().await {
            return;
        }
        if Instant::now() >= next_backlog {
            log_backlog(store, &state, paused.as_ref()).await;
            next_backlog = Instant::now() + BACKLOG_EVERY;
        }

        if let Some((until, _)) = &paused {
            let now = Instant::now();
            if now < *until {
                // Wake for the backlog line too, so a long pause stays visible.
                tokio::time::sleep((*until).min(next_backlog).saturating_duration_since(now)).await;
                continue;
            }
            paused = None;
        }

        match run_batch(panel, google, store, lock, &mut state).await {
            BatchEnd::Done => tokio::time::sleep(TICK).await,
            BatchEnd::Pause(p) => paused = Some((Instant::now() + p.length, p.reason)),
            BatchEnd::LockLost => return,
        }
    }
}

async fn log_backlog<S: Store>(store: &S, state: &LoopState, paused: Option<&(Instant, String)>) {
    let b = match store.backlog().await {
        Ok(b) => b,
        Err(e) => {
            tracing::warn!("backlog: cannot read micky_copies: {e}");
            return;
        }
    };
    let now = chrono::Utc::now().naive_utc();
    tracing::info!("{}", backlog_line(&b, now, state, paused, Instant::now()));
}

/// The once-a-minute status line.
pub fn backlog_line(
    b: &Backlog,
    now: NaiveDateTime,
    state: &LoopState,
    paused: Option<&(Instant, String)>,
    now_i: Instant,
) -> String {
    let oldest = match b.oldest_dirty {
        Some(t) => fmt_age((now - t).num_seconds()),
        None => "-".to_string(),
    };
    let mut line = format!(
        "backlog: {} need sync, oldest dirty {oldest}, {} failing",
        b.dirty, b.failing
    );
    if let Some((until, reason)) = paused {
        if *until > now_i {
            line.push_str(&format!(
                "; PAUSED {} more ({reason})",
                fmt_age(until.saturating_duration_since(now_i).as_secs() as i64)
            ));
        }
    }
    for (t, p) in [
        (Target::Firestore, &state.firestore_pause),
        (Target::Drive, &state.drive_pause),
    ] {
        if let Some(p) = p.as_ref().filter(|p| p.until > now) {
            line.push_str(&format!(
                "; {} PAUSED {} more ({})",
                t.name(),
                fmt_age((p.until - now).num_seconds()),
                p.reason
            ));
        }
    }
    match &state.last_error {
        Some((at, msg)) => line.push_str(&format!(
            "; last error {} ago: {msg}",
            fmt_age(now_i.saturating_duration_since(*at).as_secs() as i64)
        )),
        None => line.push_str("; no errors since start"),
    }
    line
}

/// `42s`, `3m05s`, `2h07m`.
pub fn fmt_age(secs: i64) -> String {
    let s = secs.max(0);
    if s < 60 {
        format!("{s}s")
    } else if s < 3600 {
        format!("{}m{:02}s", s / 60, s % 60)
    } else {
        format!("{}h{:02}m", s / 3600, (s % 3600) / 60)
    }
}

#[cfg(test)]
mod tests;

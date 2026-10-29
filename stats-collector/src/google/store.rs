//! `micky_copies` in MySQL — the only table this service touches.
//!
//! The panel's migration owns the table; this service never runs DDL. It never
//! selects `content_key` (the AES key stays inside the panel), and every time
//! it binds is `Utc::now().naive_utc()` or a value it read: the columns are
//! naive UTC written by Python's `datetime.utcnow()`, so MySQL's `NOW()` (the
//! server's session time zone) is never used.
//!
//! `dirty_at`, `synced_at` and `key_changed_at` are `DATETIME(6)`. They are
//! read into `NaiveDateTime` with all six fractional digits and `synced_at`
//! is bound back exactly as read, so the `dirty_at > synced_at` comparison is
//! exact.
//!
//! Rows are mapped by hand with `try_get`, like the rest of the crate.

use chrono::NaiveDateTime;
use sqlx::Row;

use super::sync::{Backlog, CopyRow, IdColumn, Store, CHECK_EVERY};

/// "The copies are behind the panel". Same rule as `sync::is_dirty`.
const DIRTY: &str = "(synced_at IS NULL OR dirty_at > synced_at OR synced_at < key_changed_at)";

pub struct MySqlStore {
    pool: sqlx::MySqlPool,
}

impl MySqlStore {
    pub fn new(pool: sqlx::MySqlPool) -> Self {
        MySqlStore { pool }
    }
}

fn map_row(row: &sqlx::mysql::MySqlRow) -> Result<CopyRow, sqlx::Error> {
    Ok(CopyRow {
        id: row.try_get("id")?,
        user_id: row.try_get("user_id")?,
        key_changed_at: row.try_get("key_changed_at")?,
        firestore_doc_id: row.try_get("firestore_doc_id")?,
        drive_file_id: row.try_get("drive_file_id")?,
        content_sha: row.try_get("content_sha")?,
        dirty_at: row.try_get("dirty_at")?,
        synced_at: row.try_get("synced_at")?,
        checked_at: row.try_get("checked_at")?,
        last_upload_at: row.try_get("last_upload_at")?,
        attempts: row.try_get("attempts")?,
        next_attempt_at: row.try_get("next_attempt_at")?,
    })
}

fn db(e: sqlx::Error) -> String {
    // SQLSTATE 42S02 = table does not exist: a first deploy where this
    // service started before the panel's migration created micky_copies.
    let missing_table = e
        .as_database_error()
        .and_then(|d| d.code())
        .is_some_and(|c| c == "42S02");
    if missing_table {
        "micky_copies does not exist yet (the panel's migration has not run)".to_string()
    } else {
        e.to_string()
    }
}

impl Store for MySqlStore {
    async fn due_rows(&self, now: NaiveDateTime, limit: u32) -> Result<Vec<CopyRow>, String> {
        let sql = format!(
            "SELECT id, user_id, key_changed_at, firestore_doc_id, drive_file_id, content_sha, \
                    dirty_at, synced_at, checked_at, last_upload_at, attempts, next_attempt_at \
             FROM micky_copies \
             WHERE ({DIRTY} OR checked_at IS NULL OR checked_at < ?) \
               AND (next_attempt_at IS NULL OR next_attempt_at <= ?) \
             ORDER BY {DIRTY} DESC, dirty_at ASC \
             LIMIT ?"
        );
        let rows = sqlx::query(&sql)
            .bind(now - CHECK_EVERY)
            .bind(now)
            .bind(limit)
            .fetch_all(&self.pool)
            .await
            .map_err(db)?;
        rows.iter()
            .map(map_row)
            .collect::<Result<_, _>>()
            .map_err(db)
    }

    async fn save_id(
        &self,
        row_id: i32,
        column: IdColumn,
        old: Option<&str>,
        new: &str,
    ) -> Result<bool, String> {
        // `<=>` is MySQL's NULL-safe equality: one statement covers both "set
        // it where it is still NULL" and "replace exactly this old id".
        // `synced_at = NULL` rides the same statement: no copy exists under
        // the new id yet, so the panel must not advertise it (sync.rs docs).
        let sql = match column {
            IdColumn::Firestore => {
                "UPDATE micky_copies SET firestore_doc_id = ?, synced_at = NULL \
                 WHERE id = ? AND firestore_doc_id <=> ?"
            }
            IdColumn::Drive => {
                "UPDATE micky_copies SET drive_file_id = ?, synced_at = NULL \
                 WHERE id = ? AND drive_file_id <=> ?"
            }
        };
        let done = sqlx::query(sql)
            .bind(new)
            .bind(row_id)
            .bind(old)
            .execute(&self.pool)
            .await
            .map_err(db)?;
        Ok(done.rows_affected() == 1)
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
        // Conditional on the ids the copies were written under: if anything
        // replaced them meanwhile, this pass proved nothing about the new ones.
        let done = sqlx::query(
            "UPDATE micky_copies \
             SET synced_at = ?, checked_at = ?, content_sha = ?, \
                 last_upload_at = COALESCE(?, last_upload_at), \
                 attempts = 0, last_error = NULL, next_attempt_at = NULL \
             WHERE id = ? AND firestore_doc_id <=> ? AND drive_file_id <=> ?",
        )
        .bind(synced_at)
        .bind(now)
        .bind(sha)
        .bind(last_upload)
        .bind(row_id)
        .bind(doc_id)
        .bind(file_id)
        .execute(&self.pool)
        .await
        .map_err(db)?;
        // sqlx connects with CLIENT_FOUND_ROWS: this counts matched rows, so
        // an UPDATE that changes no value still reports 1.
        Ok(done.rows_affected() == 1)
    }

    async fn clear_sync_state(
        &self,
        row_id: i32,
        seen_dirty: NaiveDateTime,
    ) -> Result<bool, String> {
        let done = sqlx::query(
            "UPDATE micky_copies \
             SET synced_at = NULL, content_sha = NULL, last_upload_at = NULL \
             WHERE id = ? AND dirty_at <=> ?",
        )
        .bind(row_id)
        .bind(seen_dirty)
        .execute(&self.pool)
        .await
        .map_err(db)?;
        Ok(done.rows_affected() == 1)
    }

    async fn clear_sha(&self, row_id: i32) -> Result<(), String> {
        sqlx::query("UPDATE micky_copies SET content_sha = NULL WHERE id = ?")
            .bind(row_id)
            .execute(&self.pool)
            .await
            .map_err(db)?;
        Ok(())
    }

    async fn mark_failed(
        &self,
        row_id: i32,
        message: &str,
        next_attempt_at: NaiveDateTime,
        clear_sha: bool,
    ) -> Result<(), String> {
        sqlx::query(
            "UPDATE micky_copies \
             SET attempts = attempts + 1, last_error = ?, next_attempt_at = ?, \
                 content_sha = IF(?, NULL, content_sha) \
             WHERE id = ?",
        )
        .bind(message)
        .bind(next_attempt_at)
        .bind(clear_sha)
        .bind(row_id)
        .execute(&self.pool)
        .await
        .map_err(db)?;
        Ok(())
    }

    async fn defer(
        &self,
        row_id: i32,
        next_attempt_at: NaiveDateTime,
        clear_sha: bool,
    ) -> Result<(), String> {
        sqlx::query(
            "UPDATE micky_copies \
             SET next_attempt_at = ?, content_sha = IF(?, NULL, content_sha) \
             WHERE id = ?",
        )
        .bind(next_attempt_at)
        .bind(clear_sha)
        .bind(row_id)
        .execute(&self.pool)
        .await
        .map_err(db)?;
        Ok(())
    }

    async fn release_ids(
        &self,
        row_id: i32,
        deleted_doc: Option<&str>,
        deleted_file: Option<&str>,
    ) -> Result<(), String> {
        // Guarded per column by the id that was deleted (not by dirty_at: this
        // runs exactly because dirty_at moved). A `None` binds NULL, and
        // `col <=> NULL` only matches a column that is NULL already.
        sqlx::query(
            "UPDATE micky_copies \
             SET firestore_doc_id = IF(firestore_doc_id <=> ?, NULL, firestore_doc_id), \
                 drive_file_id = IF(drive_file_id <=> ?, NULL, drive_file_id), \
                 synced_at = NULL \
             WHERE id = ?",
        )
        .bind(deleted_doc)
        .bind(deleted_file)
        .bind(row_id)
        .execute(&self.pool)
        .await
        .map_err(db)?;
        Ok(())
    }

    async fn delete_row(&self, row_id: i32, seen_dirty: NaiveDateTime) -> Result<bool, String> {
        let done = sqlx::query("DELETE FROM micky_copies WHERE id = ? AND dirty_at = ?")
            .bind(row_id)
            .bind(seen_dirty)
            .execute(&self.pool)
            .await
            .map_err(db)?;
        Ok(done.rows_affected() == 1)
    }

    async fn backlog(&self) -> Result<Backlog, String> {
        let sql = format!(
            "SELECT CAST(COALESCE(SUM({DIRTY}), 0) AS SIGNED) AS dirty, \
                    MIN(CASE WHEN {DIRTY} THEN dirty_at END) AS oldest_dirty, \
                    CAST(COALESCE(SUM(attempts > 0), 0) AS SIGNED) AS failing \
             FROM micky_copies"
        );
        let row = sqlx::query(&sql).fetch_one(&self.pool).await.map_err(db)?;
        Ok(Backlog {
            dirty: row.try_get("dirty").map_err(db)?,
            oldest_dirty: row.try_get("oldest_dirty").map_err(db)?,
            failing: row.try_get("failing").map_err(db)?,
        })
    }
}

//! Single-instance guard: a MySQL advisory lock held on a dedicated
//! connection for as long as this process does Google work.
//!
//! Two sync processes (a second compose service, a manual `docker run` left
//! behind) would race on the same rows: both reserving ids, both uploading.
//! `GET_LOCK` is held per connection, so the connection that took it is kept
//! open and asked before EVERY row whether the lock is still its own; if it
//! dropped (MySQL restart, network), the lock went with it, work stops, and
//! the lock is taken again before any more work.

use std::time::Duration;

use sqlx::mysql::MySqlConnection;
use sqlx::Connection;
use tokio::sync::Mutex;

/// Server-wide lock name.
pub const LOCK_NAME: &str = "marzban-google-sync";

/// How often a refused or failed lock attempt is retried.
pub const RETRY_EVERY: Duration = Duration::from_secs(30);

/// `GET_LOCK(…, 0)` and `IS_USED_LOCK(…) = CONNECTION_ID()` both answer 1 for
/// "ours"; 0 is "someone else's", NULL an error or nobody's.
pub fn granted(answer: Option<i64>) -> bool {
    answer == Some(1)
}

/// Take the lock, retrying every [`RETRY_EVERY`] until it is granted. The
/// returned connection IS the lock: keep it open while working.
pub async fn acquire(database_url: &str) -> MySqlConnection {
    loop {
        match try_acquire(database_url).await {
            Ok(Some(conn)) => {
                tracing::info!("single-instance lock '{LOCK_NAME}' acquired");
                return conn;
            }
            Ok(None) => tracing::warn!(
                "another google-sync holds the lock '{LOCK_NAME}'; doing nothing, retrying in {}s",
                RETRY_EVERY.as_secs()
            ),
            Err(e) => tracing::warn!(
                "cannot take the single-instance lock ({e}); retrying in {}s",
                RETRY_EVERY.as_secs()
            ),
        }
        tokio::time::sleep(RETRY_EVERY).await;
    }
}

async fn try_acquire(database_url: &str) -> Result<Option<MySqlConnection>, String> {
    let mut conn = MySqlConnection::connect(database_url)
        .await
        .map_err(|e| e.to_string())?;
    // CAST: sqlx decodes integers strictly by signedness, and servers differ
    // in the integer type they give these expressions.
    let answer: Option<i64> = sqlx::query_scalar("SELECT CAST(GET_LOCK(?, 0) AS SIGNED)")
        .bind(LOCK_NAME)
        .fetch_one(&mut conn)
        .await
        .map_err(|e| e.to_string())?;
    if granted(answer) {
        Ok(Some(conn))
    } else {
        let _ = conn.close().await;
        Ok(None)
    }
}

/// The connection that holds the lock. Deliberately not `Debug`.
pub struct HeldLock {
    conn: Mutex<MySqlConnection>,
}

impl HeldLock {
    pub fn new(conn: MySqlConnection) -> Self {
        HeldLock {
            conn: Mutex::new(conn),
        }
    }
}

/// Confirmed before every row: one tiny local query. Any error (connection
/// gone) or another holder means "not ours".
impl super::sync::Lock for HeldLock {
    async fn held(&self) -> bool {
        let mut conn = self.conn.lock().await;
        let answer: Result<Option<i64>, _> =
            sqlx::query_scalar("SELECT CAST(IS_USED_LOCK(?) = CONNECTION_ID() AS SIGNED)")
                .bind(LOCK_NAME)
                .fetch_one(&mut *conn)
                .await;
        granted(answer.ok().flatten())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_a_one_means_ours() {
        assert!(granted(Some(1)));
        assert!(!granted(Some(0)), "held by another connection");
        assert!(!granted(None), "error, or held by nobody");
        assert!(!granted(Some(2)));
    }
}

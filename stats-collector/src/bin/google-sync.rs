//! marzban-google-sync
//!
//! Long-running sidecar for the Micky/Marzban panel. Keeps each Micky user's
//! encrypted backup subscriptions on Google — G1 (a Firestore document) and G2
//! (a Drive file) — in step with the panel: it finds `micky_copies` rows the
//! panel marked dirty (and re-checks clean ones every 30 min), fetches the
//! encrypted copy from the panel's internal endpoint, writes it to both
//! stores, and deletes the copies of users who are gone. The loop itself is in
//! `src/google/sync.rs`.
//!
//! Configuration comes from the panel's own `.env` (compose `env_file:`) plus
//! the compose service's `environment:`:
//!   - `SQLALCHEMY_DATABASE_URL` (required): translated like the collector's.
//!   - `GOOGLE_SYNC_SECRET` (required): bearer secret of the panel endpoint.
//!   - `PANEL_URL` (optional, default `http://127.0.0.1:8000`).
//!   - `FIRESTORE_PROJECT`, `FIRESTORE_SERVICE_ACCOUNT` (path to the JSON
//!     key) (required): G1.
//!   - `MICKY_GOOGLE_BASE` (optional, the panel's own): only read to check
//!     FIRESTORE_PROJECT against the project phones read.
//!   - `GDRIVE_CLIENT_ID`, `GDRIVE_CLIENT_SECRET`, `GDRIVE_REFRESH_TOKEN`
//!     (required): G2.
//!   - `RUST_LOG` (optional, default `info`).
//!
//! Only one instance works at a time: a MySQL advisory lock
//! (`GET_LOCK('marzban-google-sync')`, see `src/google/lock.rs`) is taken on a
//! dedicated connection before any work, confirmed before every row, and work
//! stops if it is lost.
//!
//! Anything required missing or unusable, or a FIRESTORE_PROJECT that is not
//! the project the app reads (MICKY_GOOGLE_BASE's "project", else the
//! built-in `micky-sub`) → one WARN, then idle until SIGTERM.
//! It never exits on configuration: compose would restart-loop it, and a panel
//! deployed without Google secrets must keep working.

use std::time::Duration;

use marzban_stats_collector::google::auth::ServiceAccount;
use marzban_stats_collector::google::panel::PanelClient;
use marzban_stats_collector::google::settings::Settings;
use marzban_stats_collector::google::store::MySqlStore;
use marzban_stats_collector::google::{lock, sync, GoogleClient};
use sqlx::mysql::MySqlPoolOptions;

/// One connection: the loop is sequential.
const POOL_SIZE: u32 = 1;

#[tokio::main]
async fn main() {
    init_tracing();

    let settings = match Settings::from_env() {
        Ok(s) => s,
        Err(e) => return idle(&format!("configuration: {e}")).await,
    };

    let service_account = match ServiceAccount::load(&settings.service_account_path) {
        Ok(sa) => sa,
        Err(e) => return idle(&format!("FIRESTORE_SERVICE_ACCOUNT: {e}")).await,
    };

    // Lazy connect so a DB that is still starting does not kill the container;
    // the first tick retries on its own schedule.
    let pool = match MySqlPoolOptions::new()
        .max_connections(POOL_SIZE)
        .acquire_timeout(Duration::from_secs(30))
        .connect_lazy(&settings.database_url)
    {
        Ok(pool) => pool,
        Err(e) => return idle(&format!("SQLALCHEMY_DATABASE_URL is unusable ({e})")).await,
    };

    let panel = match PanelClient::new(&settings.panel_url, &settings.sync_secret) {
        Ok(p) => p,
        Err(e) => return idle(&format!("cannot build the panel HTTP client: {e}")).await,
    };

    let client_email = service_account.client_email().to_string();
    let google = match GoogleClient::new(
        &settings.firestore_project,
        service_account,
        &settings.drive_client_id,
        &settings.drive_client_secret,
        &settings.drive_refresh_token,
    ) {
        Ok(g) => g,
        Err(e) => return idle(&format!("cannot build the Google HTTP client: {e}")).await,
    };

    let store = MySqlStore::new(pool.clone());

    tracing::info!(
        "marzban-google-sync starting (panel {}, Firestore project {}, service account {}, tick {}s, batch {})",
        settings.panel_url,
        settings.firestore_project,
        client_email,
        sync::TICK.as_secs(),
        sync::BATCH_SIZE
    );

    let mut shutdown = std::pin::pin!(shutdown_signal());
    loop {
        // No work at all until this process holds the single-instance lock.
        let held = tokio::select! {
            conn = lock::acquire(&settings.database_url) => lock::HeldLock::new(conn),
            _ = &mut shutdown => break,
        };
        tokio::select! {
            // Returns only when the lock check before a row fails.
            _ = sync::run(&panel, &google, &store, &held) => {
                tracing::warn!(
                    "lost the single-instance lock (database connection dropped?); \
                     stopping work until it is re-acquired"
                );
            }
            _ = &mut shutdown => break,
        }
    }
    tracing::info!("shutdown signal received; exiting");
    pool.close().await;
}

/// Log why, then do nothing until SIGTERM. Exiting instead would make compose
/// restart the container in a loop.
async fn idle(why: &str) {
    tracing::warn!("google-sync is idle: {why}. Google copies will not be synced until this is fixed and the container restarted.");
    shutdown_signal().await;
    tracing::info!("shutdown signal received; exiting");
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        let mut term = match signal(SignalKind::terminate()) {
            Ok(s) => s,
            Err(e) => {
                tracing::warn!("cannot install SIGTERM handler: {e}");
                let _ = tokio::signal::ctrl_c().await;
                return;
            }
        };
        tokio::select! {
            _ = term.recv() => {}
            _ = tokio::signal::ctrl_c() => {}
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}

fn init_tracing() {
    let filter = std::env::var("RUST_LOG").unwrap_or_else(|_| "info".to_string());
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .init();
}

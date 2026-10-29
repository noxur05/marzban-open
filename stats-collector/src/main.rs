//! marzban-stats-collector
//!
//! A tiny long-running sidecar for the Micky/Marzban panel. Once an hour it
//! snapshots, per admin, the monotonic lifetime traffic plus the active
//! device and active user gauges into the `admin_hourly_usages` table.
//!
//! The panel's FastAPI read endpoint (`GET /admins/usage`) diffs consecutive
//! hourly traffic snapshots to produce day-by-day / hour-by-hour history.
//!
//! This service NEVER runs DDL — Alembic owns the table. It only reads the
//! panel's tables and upserts aggregate rows.
//!
//! Configuration is entirely via the environment (it shares the panel's
//! `.env`):
//!   - `SQLALCHEMY_DATABASE_URL` (required): the panel DB URL. A
//!     SQLAlchemy `mysql+pymysql://...` scheme is translated to sqlx's
//!     `mysql://...`.
//!   - `RUST_LOG` (optional, default `info`): tracing filter.

// These modules live in the crate's lib (src/lib.rs) so the sibling
// `marzban-subs-poller` binary can share `config`'s SQLAlchemy→sqlx URL
// translation without duplicating it.
use marzban_stats_collector::{collector, config};

use std::time::Duration;

use chrono::{Timelike, Utc};
use sqlx::mysql::MySqlPoolOptions;
use tokio::time::sleep;

use marzban_stats_collector::config::Config;

#[tokio::main]
async fn main() {
    init_tracing();

    let cfg = match Config::from_env() {
        Ok(cfg) => cfg,
        Err(e) => {
            tracing::error!("configuration error: {e}");
            std::process::exit(1);
        }
    };

    // A small pool is plenty: a few short reads + one upsert per admin, once
    // an hour. Lazy connect so a transient DB outage at startup doesn't crash
    // the container (the first cycle will retry on its own schedule).
    let pool = match MySqlPoolOptions::new()
        .max_connections(config::POOL_SIZE)
        .acquire_timeout(Duration::from_secs(30))
        .connect_lazy(&cfg.database_url)
    {
        Ok(pool) => pool,
        Err(e) => {
            tracing::error!("failed to create connection pool: {e}");
            std::process::exit(1);
        }
    };

    tracing::info!(
        "marzban-stats-collector starting (pool size {})",
        config::POOL_SIZE
    );

    // Run one cycle immediately on startup, then settle into the
    // top-of-hour cadence.
    run_cycle_logged(&pool).await;

    loop {
        let wait = duration_until_next_hour();
        tracing::info!("sleeping {}s until next top-of-hour", wait.as_secs());

        // Wake early on SIGTERM/SIGINT so container stop/restart is clean.
        tokio::select! {
            _ = sleep(wait) => {
                run_cycle_logged(&pool).await;
            }
            _ = shutdown_signal() => {
                tracing::info!("shutdown signal received; exiting");
                pool.close().await;
                return;
            }
        }
    }
}

/// Run one collection cycle, logging any error. A failed cycle MUST NOT crash
/// the loop — we log and let the next hour try again.
async fn run_cycle_logged(pool: &sqlx::MySqlPool) {
    match collector::run_cycle(pool).await {
        Ok(n) => tracing::debug!("cycle ok ({n} admins)"),
        Err(e) => tracing::error!("collection cycle failed: {e}"),
    }
}

/// Time from now until the next UTC top-of-hour. Always returns a strictly
/// positive duration (at least 1s) so we never busy-loop exactly on the hour
/// boundary.
fn duration_until_next_hour() -> Duration {
    let now = Utc::now();
    let secs_into_hour = (now.minute() as i64) * 60 + (now.second() as i64);
    let mut remaining = 3600 - secs_into_hour;
    if remaining <= 0 {
        remaining = 3600;
    }
    Duration::from_secs(remaining as u64)
}

/// Resolve when either SIGINT (Ctrl-C) or SIGTERM (docker stop) arrives.
async fn shutdown_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        let mut term = match signal(SignalKind::terminate()) {
            Ok(s) => s,
            Err(e) => {
                tracing::warn!("cannot install SIGTERM handler: {e}");
                // Fall back to Ctrl-C only.
                let _ = tokio::signal::ctrl_c().await;
                return;
            }
        };
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {}
            _ = term.recv() => {}
        }
    }

    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}

/// Initialise tracing from `RUST_LOG` (default `info`).
fn init_tracing() {
    use tracing_subscriber::EnvFilter;
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .init();
}

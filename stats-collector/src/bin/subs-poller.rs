//! marzban-subs-poller
//!
//! Long-running sidecar for the Micky/Marzban panel. Every few minutes it
//! mirrors the hosts of subscriptions registered in `external_subscriptions`
//! — each one an ordinary user on ANOTHER panel — into Redis, where the panel
//! appends them at subscription-render time for admins granted
//! `can_use_external_hosts`.
//!
//! It reads the panel database (one SELECT) and writes only Redis. Losing Redis
//! costs at most one interval of external hosts; losing this process costs the
//! same, because payloads carry no TTL and the panel degrades to local hosts
//! only.
//!
//! Configuration comes entirely from the panel's own `.env` (shared via
//! `env_file:` in the compose file):
//!   - `SQLALCHEMY_DATABASE_URL` (required): a `mysql+pymysql://…` URL is
//!     translated to sqlx's `mysql://…`.
//!   - `REDIS_URL` (optional, default `redis://127.0.0.1:6379/0`): must be the
//!     same instance the panel uses, or the panel will never see a payload.
//!   - `RUST_LOG` (optional, default `info`).

use std::env;
use std::time::Duration;

use marzban_stats_collector::config::Config;
use marzban_stats_collector::poller::{self, fetch};
use rand::Rng;
use sqlx::mysql::MySqlPoolOptions;
use tokio::time::sleep;

/// Base interval between cycles. Five minutes keeps a newly added host visible
/// quickly without making a nuisance of us on someone else's panel; a fetch is
/// a handful of requests per registered subscription.
const INTERVAL_SECS: u64 = 300;

/// Fraction of the interval used as random jitter, so several panels polling the
/// same upstream do not synchronise into bursts.
const JITTER_FRACTION: f64 = 0.1;

/// One connection is enough: a single SELECT once per interval.
const POOL_SIZE: u32 = 1;

const DEFAULT_REDIS_URL: &str = "redis://127.0.0.1:6379/0";

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
    let redis_url = env::var("REDIS_URL").unwrap_or_else(|_| DEFAULT_REDIS_URL.to_string());
    if redis_url.trim().is_empty() {
        // The panel treats REDIS_URL="" as "caching disabled", in which case it
        // can never read a payload and this service has nothing to do.
        tracing::error!("REDIS_URL is empty — the panel has caching disabled, so mirrored hosts could never be served. Exiting.");
        std::process::exit(1);
    }

    // Lazy connect so a DB that is still starting does not kill the container;
    // the first cycle retries on its own schedule.
    let pool = match MySqlPoolOptions::new()
        .max_connections(POOL_SIZE)
        .acquire_timeout(Duration::from_secs(30))
        .connect_lazy(&cfg.database_url)
    {
        Ok(pool) => pool,
        Err(e) => {
            tracing::error!("failed to create connection pool: {e}");
            std::process::exit(1);
        }
    };

    let http = fetch::build_client();

    tracing::info!(
        "marzban-subs-poller starting (interval {}s, identity {} / {})",
        INTERVAL_SECS,
        fetch::USER_AGENT,
        fetch::FALLBACK_HWID
    );

    run_cycle_logged(&pool, &redis_url, &http).await;

    loop {
        let wait = next_interval();
        tracing::debug!("sleeping {}s until next cycle", wait.as_secs());
        tokio::select! {
            _ = sleep(wait) => {
                run_cycle_logged(&pool, &redis_url, &http).await;
            }
            _ = shutdown_signal() => {
                tracing::info!("shutdown signal received; exiting");
                pool.close().await;
                return;
            }
        }
    }
}

/// Run one cycle, logging any failure. A failed cycle must never break the loop
/// — the remote panels are outside our control and being unreachable is normal.
async fn run_cycle_logged(pool: &sqlx::MySqlPool, redis_url: &str, http: &reqwest::Client) {
    match poller::run_cycle(pool, redis_url, http).await {
        Ok(0) => tracing::debug!("cycle ok (no enabled subscriptions)"),
        Ok(n) => tracing::info!("cycle ok ({n} subscription(s))"),
        Err(e) => tracing::error!("cycle failed: {e}"),
    }
}

fn next_interval() -> Duration {
    let jitter = (INTERVAL_SECS as f64 * JITTER_FRACTION) as i64;
    let offset = if jitter > 0 {
        rand::thread_rng().gen_range(-jitter..=jitter)
    } else {
        0
    };
    let secs = (INTERVAL_SECS as i64 + offset).max(30) as u64;
    Duration::from_secs(secs)
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
    let filter = env::var("RUST_LOG").unwrap_or_else(|_| "info".to_string());
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .init();
}

//! Shared internals for the three Micky/Marzban sidecar binaries.
//!
//! * `marzban-stats-collector` (`src/main.rs`) — hourly per-admin usage
//!   snapshots into `admin_hourly_usages`.
//! * `marzban-subs-poller` (`src/bin/subs-poller.rs`) — mirrors the hosts of
//!   subscriptions that live on other panels into Redis.
//! * `marzban-google-sync` (`src/bin/google-sync.rs`) — keeps each Micky
//!   user's encrypted Google copies (Firestore + Drive) in step with the panel.
//!
//! They exist in one crate purely to share `config` (the SQLAlchemy→sqlx URL
//! translation) and one dependency set. No module calls another's, and they
//! run as separate processes.

pub mod collector;
pub mod config;
pub mod google;
pub mod poller;

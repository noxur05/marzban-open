//! One hourly collection cycle.
//!
//! The collector NEVER runs DDL — Alembic (the panel) owns the
//! `admin_hourly_usages` table schema. This module only SELECTs from the
//! panel's tables and UPSERTs aggregated rows.
//!
//! The three GROUP BY queries and the upsert match the SHARED CONTRACT
//! exactly. The lifetime traffic figure is *monotonic* and reset-safe: it is
//! `admins.users_usage` plus the sum of every prior reset logged in
//! `admin_usage_logs` (the panel's `reset_admin_usage` logs the zeroed amount
//! before zeroing `users_usage`, so the value never decreases — see
//! app/db/crud.py:1236-1238).

use std::collections::HashMap;

use chrono::{DateTime, NaiveDateTime, Timelike, Utc};
use sqlx::mysql::MySqlPool;
use sqlx::Row;

/// Per-admin aggregate for a single hour bucket.
#[derive(Default, Clone, Copy)]
struct AdminAgg {
    /// Monotonic cumulative lifetime traffic for the admin (bytes).
    users_usage: i64,
    /// Active (non-blocked) device count.
    device_count: i64,
    /// Active (status = 'active') user count.
    user_count: i64,
}

/// Truncate a UTC instant to the top of its hour.
fn truncate_to_hour(now: DateTime<Utc>) -> NaiveDateTime {
    now.naive_utc()
        .with_minute(0)
        .and_then(|t| t.with_second(0))
        .and_then(|t| t.with_nanosecond(0))
        // with_minute/second/nanosecond(0) can never overflow, so the unwrap
        // is unreachable, but we avoid panicking anyway.
        .unwrap_or_else(|| now.naive_utc())
}

/// Run a single collection cycle: read the three aggregates, merge them per
/// admin (defaulting missing admins to zeros), and upsert one row per admin
/// for the current UTC hour bucket.
///
/// Returns the number of admins written on success.
pub async fn run_cycle(pool: &MySqlPool) -> Result<usize, sqlx::Error> {
    let bucket = truncate_to_hour(Utc::now());
    let created_at = Utc::now().naive_utc();

    // --- 1. lifetime (monotonic) traffic per admin -----------------------
    // LEFT JOIN so admins with no reset logs still appear (with their bare
    // users_usage). Every admin in the `admins` table is included here, which
    // is what seeds the per-admin map — admins with zero users/devices still
    // get a row.
    let mut aggs: HashMap<i64, AdminAgg> = HashMap::new();

    let traffic_rows = sqlx::query(
        "SELECT a.id AS admin_id, \
                a.users_usage + COALESCE(SUM(l.used_traffic_at_reset), 0) AS users_usage \
         FROM admins a \
         LEFT JOIN admin_usage_logs l ON l.admin_id = a.id \
         GROUP BY a.id",
    )
    .fetch_all(pool)
    .await?;

    for row in traffic_rows {
        let admin_id: i64 = row.try_get("admin_id")?;
        // The sum of BIGINT + COALESCE(...) comes back as DECIMAL in MySQL;
        // read it as i64 via the column accessor below. sqlx maps the
        // expression to a numeric type, so fetch it generically.
        let users_usage: i64 = row.try_get::<i64, _>("users_usage").or_else(|_| {
            // Fallback: MySQL returns the summed expression as DECIMAL.
            row.try_get::<sqlx::types::BigDecimal, _>("users_usage")
                .map(decimal_to_i64)
        })?;
        aggs.entry(admin_id).or_default().users_usage = users_usage;
    }

    // --- 2. active devices per admin -------------------------------------
    // ACTIVE = is_blocked = 0 (no 30-day window; commit adb9f6b). Attribute
    // by users.admin_id (the owning admin).
    let device_rows = sqlx::query(
        "SELECT u.admin_id AS admin_id, COUNT(d.id) AS device_count \
         FROM user_subscription_devices d \
         JOIN users u ON u.id = d.user_id \
         WHERE d.is_blocked = 0 \
         GROUP BY u.admin_id",
    )
    .fetch_all(pool)
    .await?;

    for row in device_rows {
        // admin_id can be NULL on a user (orphaned); skip those.
        let admin_id: Option<i64> = row.try_get("admin_id")?;
        let Some(admin_id) = admin_id else { continue };
        let device_count: i64 = row.try_get("device_count")?;
        aggs.entry(admin_id).or_default().device_count = device_count;
    }

    // --- 3. active users per admin ---------------------------------------
    // ACTIVE = status = 'active' (UserStatus.active). Attribute by admin_id.
    let user_rows = sqlx::query(
        "SELECT admin_id AS admin_id, COUNT(*) AS user_count \
         FROM users \
         WHERE status = 'active' \
         GROUP BY admin_id",
    )
    .fetch_all(pool)
    .await?;

    for row in user_rows {
        let admin_id: Option<i64> = row.try_get("admin_id")?;
        let Some(admin_id) = admin_id else { continue };
        let user_count: i64 = row.try_get("user_count")?;
        aggs.entry(admin_id).or_default().user_count = user_count;
    }

    if aggs.is_empty() {
        // Empty DB / no admins yet — nothing to write, but not an error.
        tracing::info!("no admins found; nothing to record for bucket {bucket}");
        return Ok(0);
    }

    // --- 4. upsert one row per admin -------------------------------------
    // INSERT ... ON DUPLICATE KEY UPDATE keyed on the uq_admin_hour unique
    // constraint (admin_id, recorded_at). Re-running within the same hour
    // simply refreshes the gauges and the monotonic traffic value.
    let mut written = 0usize;
    for (admin_id, agg) in &aggs {
        sqlx::query(
            "INSERT INTO admin_hourly_usages \
                 (admin_id, recorded_at, users_usage, device_count, user_count, created_at) \
             VALUES (?, ?, ?, ?, ?, ?) \
             ON DUPLICATE KEY UPDATE \
                 users_usage = VALUES(users_usage), \
                 device_count = VALUES(device_count), \
                 user_count = VALUES(user_count)",
        )
        .bind(admin_id)
        .bind(bucket)
        .bind(agg.users_usage)
        .bind(agg.device_count)
        .bind(agg.user_count)
        .bind(created_at)
        .execute(pool)
        .await?;
        written += 1;
    }

    tracing::info!("recorded {written} admin row(s) for bucket {bucket} (UTC)",);
    Ok(written)
}

/// Best-effort DECIMAL -> i64. Traffic is whole bytes, so truncation toward
/// zero is exact for any value the panel ever produces; we saturate rather
/// than panic if the value somehow exceeds i64 range.
fn decimal_to_i64(d: sqlx::types::BigDecimal) -> i64 {
    use std::str::FromStr;
    // BigDecimal -> string -> trim fraction -> i64. Avoids pulling in extra
    // conversion traits and is exact for integral values.
    let s = d.to_string();
    let int_part = s.split('.').next().unwrap_or("0");
    i64::from_str(int_part).unwrap_or_else(|_| {
        if int_part.starts_with('-') {
            i64::MIN
        } else {
            i64::MAX
        }
    })
}

# marzban-stats-collector

A small long-running Rust sidecar for the Micky/Marzban panel. Once an hour it
writes one row per admin into `admin_hourly_usages`, capturing:

- **`users_usage`** — the admin's **monotonic** cumulative lifetime traffic
  (bytes). Computed identically to the panel's own logic
  (`app/db/crud.py:1236-1238`):
  `admins.users_usage + SUM(admin_usage_logs.used_traffic_at_reset)`.
  This is reset-safe: the panel's `reset_admin_usage` logs the zeroed amount
  into `admin_usage_logs` *before* zeroing `admins.users_usage`, so the value
  never decreases. The panel read endpoint diffs consecutive hourly snapshots
  to produce hour-by-hour / day-by-day usage.
- **`device_count`** — active devices gauge: `user_subscription_devices`
  rows with `is_blocked = 0` (no 30-day window — dropped in commit `adb9f6b`),
  attributed by `users.admin_id` (the owning admin).
- **`user_count`** — active users gauge: `users.status = 'active'`,
  attributed by `users.admin_id`.

Every admin in the `admins` table gets a row each hour, even with zero
users/devices (they default to `0`).

## What it does NOT do

- **No DDL, ever.** Alembic (the panel) owns the `admin_hourly_usages` table.
  This service only `SELECT`s and `INSERT ... ON DUPLICATE KEY UPDATE`s.
- It does not touch `admin_usages` (the removed legacy daily table) or any
  other table beyond the four it reads: `admins`, `admin_usage_logs`,
  `user_subscription_devices`, `users`.

## Behavior

- On startup it runs **one collection cycle immediately**, then sleeps until
  the next UTC top-of-hour and runs once per hour thereafter.
- `recorded_at` is the current UTC instant **truncated to the top of the
  hour** — the bucket key. Re-running within the same hour upserts (refreshes
  the gauges, refreshes the monotonic traffic) onto the same row via the
  `uq_admin_hour` unique constraint `(admin_id, recorded_at)`.
- A failed cycle is **logged and skipped** — it never crashes the loop; the
  next hour retries.
- Empty DB (no admins) is handled gracefully: it logs and writes nothing.
- Handles `SIGTERM`/`SIGINT` for a clean container stop.

## Environment variables

| Variable                  | Required | Default | Notes |
|---------------------------|----------|---------|-------|
| `SQLALCHEMY_DATABASE_URL` | yes      | —       | The panel DB URL. A SQLAlchemy/PyMySQL scheme (`mysql+pymysql://...`, also `mysql+mysqldb://`, `mariadb+...`) is translated to sqlx's `mysql://...`. SQLite is rejected — this collector is MySQL-only and only deployed where the panel uses MySQL. |
| `RUST_LOG`                | no       | `info`  | `tracing` env-filter, e.g. `RUST_LOG=debug`. |

It shares the panel's `.env` via `env_file: .env`, so no extra configuration
is needed in normal deployments.

## Build & run

```bash
# Local build / test (cargo)
cd stats-collector
cargo build --release
cargo test            # unit tests for the scheme translator (no DB needed)

# Run locally against a MySQL panel DB
SQLALCHEMY_DATABASE_URL='mysql+pymysql://user:pass@127.0.0.1:3306/marzban' \
  RUST_LOG=info \
  ./target/release/marzban-stats-collector
```

### Container

The image is built from the repo-root `Dockerfile.stats` (multi-stage Rust →
`gcr.io/distroless/cc-debian12`, amd64):

```bash
docker build -f Dockerfile.stats -t ghcr.io/noxur05/marzban-stats-collector:latest .
```

Suggested compose service (mirrors the panel's networking/env model):

```yaml
  stats-collector:
    image: ghcr.io/noxur05/marzban-stats-collector:latest
    restart: always
    env_file: .env
    network_mode: host
    environment:
      RUST_LOG: info
```

## No live DB needed at build time

This crate uses **runtime-checked** sqlx (`sqlx::query(...)`), **not** the
compile-time `query!`/`query_as!` macros. There is therefore no `.sqlx`
offline cache to maintain and the build never contacts a database — `cargo
build --release` works with no `DATABASE_URL` set. The SQL strings match the
SHARED CONTRACT verbatim.

## TLS / dependencies

sqlx is built with `runtime-tokio-rustls`, so the binary does **not** link
OpenSSL. The distroless/cc runtime supplies glibc + CA certificates, which is
all that's required.

//! Process configuration, read entirely from the environment.
//!
//! The collector shares the panel's `.env` (via `env_file: .env` in the
//! compose file), so it reads the same `SQLALCHEMY_DATABASE_URL` variable.
//! The panel uses a SQLAlchemy/PyMySQL-flavoured URL scheme
//! (`mysql+pymysql://...`); sqlx only understands `mysql://...`, so we
//! translate the scheme here.

use std::env;

/// Default sqlx connection-pool size. The collector only runs a handful of
/// short read queries plus one upsert per admin once an hour, so two
/// connections is plenty (and matches the contract).
pub const POOL_SIZE: u32 = 2;

#[derive(Debug)]
pub struct Config {
    /// A sqlx-compatible MySQL connection URL (`mysql://user:pass@host/db`).
    pub database_url: String,
}

impl Config {
    /// Build the config from the environment.
    ///
    /// Errors if `SQLALCHEMY_DATABASE_URL` is missing or points at a
    /// non-MySQL backend (the collector is MySQL-only by design — the panel
    /// uses SQLite only on non-prod branches, where this service is not
    /// deployed).
    pub fn from_env() -> Result<Self, String> {
        let raw = env::var("SQLALCHEMY_DATABASE_URL")
            .map_err(|_| "SQLALCHEMY_DATABASE_URL is not set".to_string())?;

        let database_url = translate_scheme(&raw)?;
        Ok(Config { database_url })
    }
}

/// Translate a SQLAlchemy database URL into a sqlx-compatible one.
///
/// Handles the common PyMySQL / mysqlclient driver suffixes
/// (`mysql+pymysql://`, `mysql+mysqldb://`, `mysql+mysqlconnector://`) by
/// stripping the `+driver` part. A bare `mysql://` is passed through
/// untouched. Anything else (e.g. `sqlite://`) is rejected.
pub fn translate_scheme(raw: &str) -> Result<String, String> {
    let (scheme, rest) = raw
        .split_once("://")
        .ok_or_else(|| format!("malformed database URL: {raw}"))?;

    // The scheme may carry a `+driver` qualifier, e.g. `mysql+pymysql`.
    let base = scheme.split('+').next().unwrap_or(scheme);

    match base {
        "mysql" | "mariadb" => Ok(format!("mysql://{rest}")),
        other => Err(format!(
            "unsupported database scheme '{other}' (collector requires MySQL/MariaDB)"
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strips_pymysql_driver() {
        assert_eq!(
            translate_scheme("mysql+pymysql://u:p@127.0.0.1:3306/marzban").unwrap(),
            "mysql://u:p@127.0.0.1:3306/marzban"
        );
    }

    #[test]
    fn passes_bare_mysql_through() {
        assert_eq!(
            translate_scheme("mysql://u:p@host/db").unwrap(),
            "mysql://u:p@host/db"
        );
    }

    #[test]
    fn strips_mariadb_driver() {
        assert_eq!(
            translate_scheme("mariadb+mariadbconnector://u@h/d").unwrap(),
            "mysql://u@h/d"
        );
    }

    #[test]
    fn rejects_sqlite() {
        assert!(translate_scheme("sqlite:///db.sqlite3").is_err());
    }

    #[test]
    fn rejects_garbage() {
        assert!(translate_scheme("not a url").is_err());
    }
}

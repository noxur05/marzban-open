//! Environment wiring for `marzban-google-sync`.
//!
//! Only wiring lives here; every tunable is a compile-time constant next to
//! the code it tunes. The service shares the panel's `.env` (compose
//! `env_file:`), so `SQLALCHEMY_DATABASE_URL` and `GOOGLE_SYNC_SECRET` are the
//! panel's own values.

use crate::config::translate_scheme;

pub const DEFAULT_PANEL_URL: &str = "http://127.0.0.1:8000";

/// The Firestore project built into the app (`docs/micky-protocol.md`,
/// "Google copies").
pub const APP_BUILTIN_PROJECT: &str = "micky-sub";

/// Every variable without which the service cannot do anything. With any of
/// them unset or empty it idles (see the binary) instead of exiting, because
/// compose would restart-loop an exiting container.
pub const REQUIRED: &[&str] = &[
    "SQLALCHEMY_DATABASE_URL",
    "GOOGLE_SYNC_SECRET",
    "FIRESTORE_PROJECT",
    "FIRESTORE_SERVICE_ACCOUNT",
    "GDRIVE_CLIENT_ID",
    "GDRIVE_CLIENT_SECRET",
    "GDRIVE_REFRESH_TOKEN",
];

/// Process settings. Deliberately not `Debug`: it holds secrets.
pub struct Settings {
    /// sqlx `mysql://…` URL, translated from the panel's SQLAlchemy URL.
    pub database_url: String,
    /// Base URL of the panel, no trailing slash.
    pub panel_url: String,
    /// Bearer secret for `GET /internal/micky-copy/{id}`.
    pub sync_secret: String,
    pub firestore_project: String,
    /// Path to the service-account JSON key (inside the container).
    pub service_account_path: String,
    pub drive_client_id: String,
    pub drive_client_secret: String,
    pub drive_refresh_token: String,
}

#[derive(Debug, PartialEq, Eq)]
pub enum SettingsError {
    /// These variables are unset or empty.
    Missing(Vec<&'static str>),
    /// Present but unusable. The text names the variable, never its value
    /// (except where the value is not secret, like a database scheme).
    Invalid(String),
    /// FIRESTORE_PROJECT is not the project phones read: every G1 copy would
    /// land where no app looks.
    ProjectMismatch { firestore: String, app: String },
}

impl std::fmt::Display for SettingsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SettingsError::Missing(names) => write!(f, "missing: {}", names.join(", ")),
            SettingsError::Invalid(why) => f.write_str(why),
            SettingsError::ProjectMismatch { firestore, app } => write!(
                f,
                "FIRESTORE_PROJECT is '{firestore}' but the app reads Firestore project '{app}' \
                 (MICKY_GOOGLE_BASE's \"project\", else the built-in '{APP_BUILTIN_PROJECT}'); \
                 G1 copies would be written where no phone looks"
            ),
        }
    }
}

impl Settings {
    pub fn from_env() -> Result<Settings, SettingsError> {
        Settings::from_lookup(|k| std::env::var(k).ok())
    }

    /// Build from any key lookup (the environment in production, a map in
    /// tests).
    pub fn from_lookup(get: impl Fn(&str) -> Option<String>) -> Result<Settings, SettingsError> {
        let value = |k: &str| {
            get(k)
                .map(|v| v.trim().to_string())
                .filter(|v| !v.is_empty())
        };

        let missing: Vec<&'static str> = REQUIRED
            .iter()
            .copied()
            .filter(|k| value(k).is_none())
            .collect();
        if !missing.is_empty() {
            return Err(SettingsError::Missing(missing));
        }
        let required = |k: &str| value(k).unwrap_or_default();

        let database_url =
            translate_scheme(&required("SQLALCHEMY_DATABASE_URL")).map_err(|_| {
                SettingsError::Invalid(
                "SQLALCHEMY_DATABASE_URL is not a MySQL/MariaDB URL (google-sync is MySQL-only)"
                    .to_string(),
            )
            })?;

        let panel_url = value("PANEL_URL")
            .unwrap_or_else(|| DEFAULT_PANEL_URL.to_string())
            .trim_end_matches('/')
            .to_string();
        if !(panel_url.starts_with("http://") || panel_url.starts_with("https://")) {
            return Err(SettingsError::Invalid(
                "PANEL_URL must start with http:// or https://".to_string(),
            ));
        }

        let firestore_project = required("FIRESTORE_PROJECT");
        if !valid_project_id(&firestore_project) {
            return Err(SettingsError::Invalid(
                "FIRESTORE_PROJECT is not a Google Cloud project id ([a-z0-9-], 6-30 chars)"
                    .to_string(),
            ));
        }
        let app = app_project(get("MICKY_GOOGLE_BASE").as_deref());
        if app != firestore_project {
            return Err(SettingsError::ProjectMismatch {
                firestore: firestore_project,
                app,
            });
        }

        Ok(Settings {
            database_url,
            panel_url,
            sync_secret: required("GOOGLE_SYNC_SECRET"),
            firestore_project,
            service_account_path: required("FIRESTORE_SERVICE_ACCOUNT"),
            drive_client_id: required("GDRIVE_CLIENT_ID"),
            drive_client_secret: required("GDRIVE_CLIENT_SECRET"),
            drive_refresh_token: required("GDRIVE_REFRESH_TOKEN"),
        })
    }
}

/// The Firestore project phones read: the `"project"` of MICKY_GOOGLE_BASE
/// when the panel passes that base on, else the app's built-in one.
///
/// Mirrors the panel's `_load_base` (`app/utils/micky_copy.py`): a base that
/// is not a non-empty JSON object of `"project"` / `"key"` strings matching
/// the app's patterns is dropped there (logged), so the app keeps its
/// built-in project.
pub fn app_project(base: Option<&str>) -> String {
    let builtin = || APP_BUILTIN_PROJECT.to_string();
    let Some(raw) = base.map(str::trim).filter(|s| !s.is_empty()) else {
        return builtin();
    };
    let Ok(serde_json::Value::Object(map)) = serde_json::from_str::<serde_json::Value>(raw) else {
        return builtin();
    };
    let valid = !map.is_empty()
        && map.iter().all(|(k, v)| match (k.as_str(), v.as_str()) {
            ("project", Some(p)) => app_project_pattern(p),
            ("key", Some(key)) => app_key_pattern(key),
            _ => false,
        });
    match map.get("project").and_then(|v| v.as_str()) {
        Some(p) if valid => p.to_string(),
        _ => builtin(),
    }
}

/// The app's pattern for a base project: `[a-z][a-z0-9-]{4,28}[a-z0-9]`.
fn app_project_pattern(s: &str) -> bool {
    let b = s.as_bytes();
    (6..=30).contains(&b.len())
        && b[0].is_ascii_lowercase()
        && (b[b.len() - 1].is_ascii_lowercase() || b[b.len() - 1].is_ascii_digit())
        && b.iter()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'-')
}

/// The app's pattern for a base API key: `[A-Za-z0-9_-]{20,128}`.
fn app_key_pattern(s: &str) -> bool {
    (20..=128).contains(&s.len())
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}

/// Google Cloud project ids: 6-30 chars of lowercase letters, digits and
/// hyphens. Checked because the id goes into every Firestore URL path.
fn valid_project_id(s: &str) -> bool {
    (6..=30).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn full() -> HashMap<&'static str, &'static str> {
        HashMap::from([
            (
                "SQLALCHEMY_DATABASE_URL",
                "mysql+pymysql://u:p@127.0.0.1/marzban",
            ),
            ("GOOGLE_SYNC_SECRET", "s3cret"),
            ("FIRESTORE_PROJECT", "micky-sub"),
            ("FIRESTORE_SERVICE_ACCOUNT", "/run/secrets/google-sa.json"),
            ("GDRIVE_CLIENT_ID", "id"),
            ("GDRIVE_CLIENT_SECRET", "secret"),
            ("GDRIVE_REFRESH_TOKEN", "refresh"),
        ])
    }

    fn load(m: &HashMap<&'static str, &'static str>) -> Result<Settings, SettingsError> {
        Settings::from_lookup(|k| m.get(k).map(|v| v.to_string()))
    }

    #[test]
    fn full_env_parses_with_defaults() {
        let s = load(&full()).unwrap();
        assert_eq!(s.database_url, "mysql://u:p@127.0.0.1/marzban");
        assert_eq!(s.panel_url, "http://127.0.0.1:8000");
        assert_eq!(s.firestore_project, "micky-sub");
    }

    #[test]
    fn missing_and_empty_are_named_together() {
        let mut m = full();
        m.remove("GDRIVE_REFRESH_TOKEN");
        m.insert("GOOGLE_SYNC_SECRET", "   ");
        match load(&m) {
            Err(SettingsError::Missing(names)) => {
                assert_eq!(names, vec!["GOOGLE_SYNC_SECRET", "GDRIVE_REFRESH_TOKEN"])
            }
            other => panic!("expected Missing, got {:?}", other.err()),
        }
    }

    #[test]
    fn panel_url_trailing_slash_is_dropped() {
        let mut m = full();
        m.insert("PANEL_URL", "http://127.0.0.1:8001/");
        assert_eq!(load(&m).unwrap().panel_url, "http://127.0.0.1:8001");
    }

    #[test]
    fn sqlite_is_invalid_not_missing() {
        let mut m = full();
        m.insert("SQLALCHEMY_DATABASE_URL", "sqlite:///db.sqlite3");
        assert!(matches!(load(&m), Err(SettingsError::Invalid(_))));
    }

    #[test]
    fn project_id_is_checked() {
        let mut m = full();
        m.insert("FIRESTORE_PROJECT", "../evil");
        assert!(matches!(load(&m), Err(SettingsError::Invalid(_))));
    }

    #[test]
    fn project_must_be_the_one_the_app_reads() {
        // No base: the app reads its built-in project.
        assert!(load(&full()).is_ok());
        let mut m = full();
        m.insert("FIRESTORE_PROJECT", "other-proj");
        match load(&m) {
            Err(e @ SettingsError::ProjectMismatch { .. }) => {
                let msg = e.to_string();
                assert!(
                    msg.contains("'other-proj'") && msg.contains("'micky-sub'"),
                    "{msg}"
                );
            }
            other => panic!("expected ProjectMismatch, got {:?}", other.err()),
        }
        // A base naming that project makes it right.
        m.insert("MICKY_GOOGLE_BASE", r#"{"project": "other-proj"}"#);
        assert!(load(&m).is_ok());
        // ...and then the built-in one is the mismatch.
        let mut m = full();
        m.insert("MICKY_GOOGLE_BASE", r#"{"project": "other-proj"}"#);
        assert!(matches!(
            load(&m),
            Err(SettingsError::ProjectMismatch { .. })
        ));
    }

    #[test]
    fn app_project_mirrors_the_panels_base_rules() {
        let key = "AIzaSyA-0123456789abcdefghij";
        assert_eq!(app_project(None), "micky-sub");
        assert_eq!(app_project(Some("  ")), "micky-sub");
        assert_eq!(app_project(Some(r#"{"project":"proj-two"}"#)), "proj-two");
        let both = format!(r#"{{"project":"proj-two","key":"{key}"}}"#);
        assert_eq!(app_project(Some(&both)), "proj-two");
        let key_only = format!(r#"{{"key":"{key}"}}"#);
        assert_eq!(app_project(Some(&key_only)), "micky-sub");
        // Dropped by the panel, so the app keeps its built-in project:
        for bad in [
            "not json",
            "[]",
            "{}",
            r#"{"project":"proj-two","extra":"x"}"#,
            r#"{"project":"Proj-Two"}"#,
            r#"{"project":"2proj-two"}"#,
            r#"{"project":"proj-two-"}"#,
            r#"{"project":"short"}"#,
            r#"{"project":"proj-two","key":"too-short"}"#,
            r#"{"project":7}"#,
        ] {
            assert_eq!(app_project(Some(bad)), "micky-sub", "{bad}");
        }
    }

    #[test]
    fn errors_never_echo_secret_values() {
        let mut m = full();
        m.insert("SQLALCHEMY_DATABASE_URL", "postgres://user:hunter2@db/x");
        let msg = load(&m).err().unwrap().to_string();
        assert!(!msg.contains("hunter2"), "{msg}");
    }
}

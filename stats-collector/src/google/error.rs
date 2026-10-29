//! Failure classification and retry timing.
//!
//! Every Google or panel problem ends up as one [`Failure`] with a [`Scope`]:
//!
//! * [`Scope::Account`] — the whole Google account is unusable right now: no
//!   answer at all, a token refresh refused, a 401 that survived one token
//!   refresh, a 429, a 5xx, or a 403 for a quota/rate/permission reason (or
//!   one we do not recognise). Trying the next user would only fail the same
//!   way (and burn quota), so the loop pauses for [`ACCOUNT_PAUSE`].
//! * [`Scope::Row`] — something about THIS user's copy: an unexpected 4xx from
//!   Google, a 403 whose every reason is about one file (see
//!   [`FILE_LEVEL_REASONS`]), the panel failing to render one user. Only that
//!   row backs off.
//!
//! Messages end up in `micky_copies.last_error` and in the log. They are built
//! only from status codes and Google's own error text, never from a token, a
//! secret or the blob.

use std::time::Duration;

/// Pause after an account-wide Google failure.
pub const ACCOUNT_PAUSE: Duration = Duration::from_secs(5 * 60);

/// First retry delay of a failed row; doubled per earlier failure.
pub const BACKOFF_BASE_SECS: i64 = 30;

/// Ceiling of the per-row retry delay.
pub const BACKOFF_MAX_SECS: i64 = 30 * 60;

/// Width of `micky_copies.last_error`.
pub const LAST_ERROR_MAX_CHARS: usize = 255;

/// How much of a Google error message is kept, so the status and the context
/// in front of it always fit in [`LAST_ERROR_MAX_CHARS`].
const REMOTE_MESSAGE_MAX_CHARS: usize = 140;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scope {
    /// Pause everything: the next user would fail the same way.
    Account,
    /// Back off this row only.
    Row,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Failure {
    pub scope: Scope,
    /// Safe to log and to store: no tokens, no secrets, no blob.
    pub message: String,
    /// The token endpoint answered `invalid_grant`: a human has to act
    /// (revoked refresh token, consent screen in Testing, deleted SA key).
    pub invalid_grant: bool,
    /// A 403 whose every reason is about ONE file (see
    /// [`FILE_LEVEL_REASONS`]): the Drive file is unusable to us, so it is
    /// replaced like a missing one.
    pub file_level: bool,
}

impl Failure {
    pub fn account(message: impl Into<String>) -> Self {
        Failure {
            scope: Scope::Account,
            message: message.into(),
            invalid_grant: false,
            file_level: false,
        }
    }

    pub fn row(message: impl Into<String>) -> Self {
        Failure {
            scope: Scope::Row,
            message: message.into(),
            invalid_grant: false,
            file_level: false,
        }
    }

    /// A non-success answer from a Google API (after the one token refresh a
    /// 401 gets), classified from its status AND its error body: a 403 is
    /// per-row only when every reason in it is about one file.
    pub fn from_google(what: &str, status: u16, body: &[u8]) -> Self {
        let e = parse_google_error(body);
        // Only Google's free text can name an object; the reason/status
        // label is a fixed vocabulary (some of it 20+ characters) and stays.
        let text = redact_ids(&e.message);
        let label = e.reasons.first().or(e.status.as_ref());
        let remote = match label {
            Some(l) if !text.is_empty() => format!("{l}: {text}"),
            Some(l) => l.clone(),
            None => text,
        };
        let mut f = Failure::with_detail(what, status, &remote);
        if status == 403 {
            f.scope = scope_for_403(&e.reasons);
            f.file_level = f.scope == Scope::Row;
        }
        f
    }

    /// A non-success HTTP status with an already-extracted message, scoped by
    /// status alone.
    pub fn from_status(what: &str, status: u16, remote_message: &str) -> Self {
        Failure::with_detail(what, status, &redact_ids(remote_message))
    }

    /// `detail` must already be redacted.
    fn with_detail(what: &str, status: u16, detail: &str) -> Self {
        let detail = clean(detail, REMOTE_MESSAGE_MAX_CHARS);
        let message = if detail.is_empty() {
            format!("{what}: HTTP {status}")
        } else {
            format!("{what}: HTTP {status} {detail}")
        };
        Failure {
            scope: scope_for_status(status),
            message,
            invalid_grant: false,
            file_level: false,
        }
    }

    /// No HTTP answer at all. Account-wide for Google: when Google does not
    /// answer, it does not answer for anyone.
    pub fn no_answer(what: &str, e: &reqwest::Error) -> Self {
        Failure::account(format!("{what}: no answer ({})", transport_kind(e)))
    }

    /// The text written to `last_error`: at most 255 characters.
    pub fn for_db(&self) -> String {
        clean(&self.message, LAST_ERROR_MAX_CHARS)
    }
}

/// Drive 403 reasons (`error.errors[].reason`) that concern ONE file: someone
/// changed that file's ownership or permissions, or it was flagged. Every
/// other user's copy is unaffected, so only that row backs off.
pub const FILE_LEVEL_REASONS: &[&str] = &[
    "insufficientFilePermissions",
    "appNotAuthorizedToFile",
    "cannotModifyViewersCanCopyContent",
    "cannotShareTeamDriveTopFolderWithAnyoneOrDomains",
    "fileOwnerNotMemberOfTeamDrive",
    "fileNeverWritable",
    "fileWriterTeamDriveMoveInDisabled",
    "cannotDownloadAbusiveFile",
];

/// Scope of a 403. Account-wide unless the body names reasons and every one
/// of them is file-level. That keeps the documented account-level ones
/// (`rateLimitExceeded`, `userRateLimitExceeded`, `dailyLimitExceeded`,
/// `quotaExceeded`, Firestore's `PERMISSION_DENIED` / `RESOURCE_EXHAUSTED`,
/// which carry no Drive reason) and anything unrecognised account-wide.
pub fn scope_for_403(reasons: &[String]) -> Scope {
    if !reasons.is_empty()
        && reasons
            .iter()
            .all(|r| FILE_LEVEL_REASONS.contains(&r.as_str()))
    {
        Scope::Row
    } else {
        Scope::Account
    }
}

/// The parts of a Google API error body this service uses.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct GoogleError {
    pub message: String,
    /// Drive: `error.errors[].reason`.
    pub reasons: Vec<String>,
    /// Firestore (and newer APIs): `error.status`, e.g. `PERMISSION_DENIED`.
    pub status: Option<String>,
}

pub fn parse_google_error(body: &[u8]) -> GoogleError {
    let Ok(v) = serde_json::from_slice::<serde_json::Value>(body) else {
        return GoogleError::default();
    };
    let err = &v["error"];
    let reasons = err["errors"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|e| e["reason"].as_str())
                .map(|r| clean(r, 60))
                .collect()
        })
        .unwrap_or_default();
    GoogleError {
        message: google_error_message(body),
        reasons,
        status: err["status"].as_str().map(|s| clean(s, 40)),
    }
}

/// Which failures stop the whole loop. 401 reaches this only after the token
/// was refreshed once and the retry still got 401. A 403 is refined by
/// [`scope_for_403`] when its body is available.
pub fn scope_for_status(status: u16) -> Scope {
    match status {
        401 | 403 | 429 | 500..=599 => Scope::Account,
        _ => Scope::Row,
    }
}

/// Delay before a failed row is tried again: 30 s after the first failure,
/// doubling per earlier failure, capped at 30 min.
pub fn backoff(previous_attempts: i32) -> chrono::Duration {
    let exp = previous_attempts.clamp(0, 16) as u32;
    let secs = BACKOFF_BASE_SECS
        .saturating_mul(1_i64 << exp)
        .min(BACKOFF_MAX_SECS);
    chrono::Duration::seconds(secs)
}

/// A short, secret-free description of a transport error. reqwest's own
/// Display carries the URL and a source chain; the URL holds no secret here,
/// but a fixed vocabulary keeps `last_error` short and greppable.
pub fn transport_kind(e: &reqwest::Error) -> &'static str {
    if e.is_timeout() {
        "timeout"
    } else if e.is_connect() {
        "connect failed"
    } else if e.is_body() || e.is_decode() {
        "body read failed"
    } else {
        "request failed"
    }
}

/// Shortest run of id characters treated as an id. Drive file ids are 33
/// characters, Firestore doc ids 32; English words in Google's messages are
/// far shorter.
const ID_RUN_MIN: usize = 20;

/// Google's error text names the object it is about ("insufficient
/// permissions for file 1AbC…", "File not found: 1AbC…"). Full ids must not
/// reach `last_error` or the log, so every run of 20+ `[A-Za-z0-9_-]`
/// characters is cut to its first 6 plus "…".
pub fn redact_ids(s: &str) -> String {
    fn flush(run: &mut String, out: &mut String) {
        if run.len() >= ID_RUN_MIN {
            out.push_str(&run[..6]);
            out.push('…');
        } else {
            out.push_str(run);
        }
        run.clear();
    }
    let mut out = String::with_capacity(s.len());
    let mut run = String::new();
    for c in s.chars() {
        if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
            run.push(c);
        } else {
            flush(&mut run, &mut out);
            out.push(c);
        }
    }
    flush(&mut run, &mut out);
    out
}

/// Printable ASCII (plus the "…" of a redacted id) only, whitespace
/// collapsed, cut at `max` bytes (so at most `max` characters). Google's
/// messages are plain English; anything else is dropped rather than risk a
/// column-charset error on the UPDATE that records the failure ("…" exists
/// in both utf8mb4 and MySQL's latin1, which is cp1252).
pub fn clean(s: &str, max: usize) -> String {
    let mut out = String::with_capacity(s.len().min(max));
    let mut last_space = true;
    for c in s.chars() {
        let c = if c.is_whitespace() { ' ' } else { c };
        if !(c == ' ' || c.is_ascii_graphic() || c == '…') {
            continue;
        }
        if c == ' ' {
            if last_space {
                continue;
            }
            last_space = true;
        } else {
            last_space = false;
        }
        if out.len() + c.len_utf8() > max {
            break;
        }
        out.push(c);
    }
    out.trim_end().to_string()
}

/// Pull a human message out of a Google error body.
///
/// API errors: `{"error":{"code":403,"message":"…","status":"PERMISSION_DENIED"}}`.
/// Token endpoint: `{"error":"invalid_grant","error_description":"…"}`.
/// Anything unparseable yields an empty string: a raw body is never echoed,
/// because it is not known to be free of secrets.
pub fn google_error_message(body: &[u8]) -> String {
    let Ok(v) = serde_json::from_slice::<serde_json::Value>(body) else {
        return String::new();
    };
    let err = &v["error"];
    if let Some(msg) = err["message"].as_str() {
        return msg.to_string();
    }
    match (err.as_str(), v["error_description"].as_str()) {
        (Some(code), Some(desc)) => format!("{code}: {desc}"),
        (Some(code), None) => code.to_string(),
        _ => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn account_wide_statuses() {
        for s in [401, 403, 429, 500, 502, 503, 599] {
            assert_eq!(scope_for_status(s), Scope::Account, "{s}");
        }
    }

    #[test]
    fn per_row_statuses() {
        for s in [400, 404, 409, 412, 413, 422] {
            assert_eq!(scope_for_status(s), Scope::Row, "{s}");
        }
    }

    #[test]
    fn from_status_keeps_scope_and_message() {
        let f = Failure::from_status("drive upload", 429, "Rate Limit Exceeded");
        assert_eq!(f.scope, Scope::Account);
        assert_eq!(f.message, "drive upload: HTTP 429 Rate Limit Exceeded");
        let f = Failure::from_status("firestore write", 400, "");
        assert_eq!(f.scope, Scope::Row);
        assert_eq!(f.message, "firestore write: HTTP 400");
    }

    #[test]
    fn backoff_doubles_from_30s_and_caps_at_30min() {
        assert_eq!(backoff(0).num_seconds(), 30);
        assert_eq!(backoff(1).num_seconds(), 60);
        assert_eq!(backoff(2).num_seconds(), 120);
        assert_eq!(backoff(5).num_seconds(), 960);
        assert_eq!(backoff(6).num_seconds(), 1800);
        assert_eq!(backoff(40).num_seconds(), 1800);
        assert_eq!(backoff(i32::MAX).num_seconds(), 1800);
        assert_eq!(backoff(-3).num_seconds(), 30);
    }

    #[test]
    fn db_message_fits_the_column() {
        let f = Failure::row("x".repeat(1000));
        assert_eq!(f.for_db().chars().count(), LAST_ERROR_MAX_CHARS);
        let f = Failure::row("bad \u{1F600} char\nand\ttabs  ");
        assert_eq!(f.for_db(), "bad char and tabs");
    }

    #[test]
    fn long_google_message_is_cut_before_storing() {
        let f = Failure::from_status("drive upload", 403, &"y".repeat(1000));
        assert!(f.message.len() <= "drive upload: HTTP 403 ".len() + 140);
        assert!(f.for_db().len() <= LAST_ERROR_MAX_CHARS);
    }

    fn drive_403(reasons: &[&str]) -> Vec<u8> {
        let errors: Vec<serde_json::Value> = reasons
            .iter()
            .map(|r| serde_json::json!({"domain": "global", "reason": r, "message": "m"}))
            .collect();
        serde_json::json!({"error": {"code": 403, "message": "Forbidden thing", "errors": errors}})
            .to_string()
            .into_bytes()
    }

    #[test]
    fn file_level_403_is_per_row() {
        for r in [
            "insufficientFilePermissions",
            "appNotAuthorizedToFile",
            "cannotModifyViewersCanCopyContent",
        ] {
            let f = Failure::from_google("drive upload", 403, &drive_403(&[r]));
            assert_eq!(f.scope, Scope::Row, "{r}");
            assert!(f.file_level, "{r}");
            assert_eq!(
                f.message,
                format!("drive upload: HTTP 403 {r}: Forbidden thing")
            );
        }
    }

    #[test]
    fn quota_rate_permission_and_unknown_403_are_account_wide() {
        for r in [
            "rateLimitExceeded",
            "userRateLimitExceeded",
            "dailyLimitExceeded",
            "quotaExceeded",
            "storageQuotaExceeded",
            "somethingNew",
        ] {
            let f = Failure::from_google("drive upload", 403, &drive_403(&[r]));
            assert_eq!(f.scope, Scope::Account, "{r}");
            assert!(!f.file_level, "{r}");
        }
        // Mixed: one account-level reason wins.
        let f = Failure::from_google(
            "drive share",
            403,
            &drive_403(&["insufficientFilePermissions", "rateLimitExceeded"]),
        );
        assert_eq!(f.scope, Scope::Account);
        // No body / no reasons: unknown.
        assert_eq!(Failure::from_google("x", 403, b"").scope, Scope::Account);
        // Firestore: status, no Drive reasons.
        let fs = br#"{"error":{"code":403,"message":"Missing or insufficient permissions.","status":"PERMISSION_DENIED"}}"#;
        let f = Failure::from_google("firestore write", 403, fs);
        assert_eq!(f.scope, Scope::Account);
        assert_eq!(
            f.message,
            "firestore write: HTTP 403 PERMISSION_DENIED: Missing or insufficient permissions."
        );
    }

    #[test]
    fn non_403_statuses_keep_their_scope_with_a_body() {
        let body = drive_403(&["insufficientFilePermissions"]);
        assert_eq!(Failure::from_google("x", 500, &body).scope, Scope::Account);
        assert_eq!(Failure::from_google("x", 429, &body).scope, Scope::Account);
        assert_eq!(Failure::from_google("x", 400, &body).scope, Scope::Row);
    }

    #[test]
    fn ids_in_google_text_are_cut_to_6_chars() {
        let file = "1WZCyO0EVB1HBHObkBOydxklo9Xm02_Yg";
        let doc = "207226496fdd1630aee76aa8d3075c30";
        let body = format!(
            r#"{{"error":{{"code":403,"message":"The user does not have sufficient permissions for file {file}.","errors":[{{"reason":"insufficientFilePermissions"}}]}}}}"#
        );
        let f = Failure::from_google("drive upload", 403, body.as_bytes());
        assert!(!f.message.contains(file), "{}", f.message);
        assert!(f.message.contains("for file 1WZCyO…."), "{}", f.message);
        assert!(f.for_db().contains("1WZCyO…"), "{}", f.for_db());
        assert!(!f.for_db().contains(file));

        let body = format!(r#"{{"error":{{"code":404,"message":"File not found: {file}."}}}}"#);
        let f = Failure::from_google("drive share", 404, body.as_bytes());
        assert_eq!(f.message, "drive share: HTTP 404 File not found: 1WZCyO….");

        let path = format!("projects/micky-sub/databases/(default)/documents/copies/{doc}");
        assert_eq!(
            redact_ids(&format!("No document to update: {path}")),
            "No document to update: projects/micky-sub/databases/(default)/documents/copies/207226…"
        );
        // Ordinary words stay; the reason label is never redacted, even when
        // it is 20+ characters long.
        let plain = "Rate Limit Exceeded: quota for this user";
        assert_eq!(redact_ids(plain), plain);
        let body = br#"{"error":{"code":403,"message":"Rate Limit Exceeded","errors":[{"reason":"userRateLimitExceeded"}]}}"#;
        assert_eq!(
            Failure::from_google("drive upload", 403, body).message,
            "drive upload: HTTP 403 userRateLimitExceeded: Rate Limit Exceeded"
        );
        assert_eq!(redact_ids(&"a".repeat(19)), "a".repeat(19));
        assert_eq!(redact_ids(&"a".repeat(20)), "aaaaaa…");
    }

    #[test]
    fn extracts_google_error_messages() {
        let api = br#"{"error":{"code":403,"message":"The user has exceeded their Drive storage quota","status":"PERMISSION_DENIED"}}"#;
        assert_eq!(
            google_error_message(api),
            "The user has exceeded their Drive storage quota"
        );
        let token = br#"{"error":"invalid_grant","error_description":"Token has been expired or revoked."}"#;
        assert_eq!(
            google_error_message(token),
            "invalid_grant: Token has been expired or revoked."
        );
        assert_eq!(google_error_message(b"<html>nope</html>"), "");
        assert_eq!(google_error_message(br#"{"other":1}"#), "");
    }
}

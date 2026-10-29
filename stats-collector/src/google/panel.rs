//! The panel side: `GET {PANEL_URL}/internal/micky-copy/{user_id}`.
//!
//! The panel decides what goes into a copy and encrypts it; this service only
//! moves the bytes. Status codes are the whole contract:
//!
//! | Status  | Meaning                                  | Action                          |
//! |---------|------------------------------------------|---------------------------------|
//! | 200     | the encrypted copy + `x-copy-sha`        | upload when needed              |
//! | 404 + `x-copy-gone: 1` | user gone, or no longer on Micky | delete both copies, then row |
//! | 404 without it | route missing, request proxied, … | this row backs off; NOTHING deleted |
//! | 503     | the feature is switched off in the panel | skip everything for 60 s        |
//! | connect failure, 502, 504 | panel restarting (deploy) | skip everything for 15 s; no row charged |
//! | timeout, body cut off | slow or broken for this user | this row backs off (it must not stall the queue head) |
//! | 401/403 | wrong `GOOGLE_SYNC_SECRET`               | ERROR, pause 5 min              |
//! | other   | render failed for this user, …           | this row backs off              |
//!
//! Deletion is the one irreversible action, so it needs the panel to say so
//! explicitly. A bare 404 is what an older panel image without this route
//! answers (e.g. during a rollback), and what a wrong or proxied `PANEL_URL`
//! answers; treating that as "user gone" would wipe every user's copies.
//!
//! The request carries no `X-Forwarded-*` or `Via` header: the panel refuses
//! proxied requests to this endpoint. The client also ignores any proxy
//! environment and follows no redirect, so the bearer secret can only ever go
//! to `PANEL_URL` itself.

use std::time::Duration;

use super::auth::read_capped;
use super::error::transport_kind;

/// Response header that turns a 404 into "this copy must not exist".
pub const GONE_HEADER: &str = "x-copy-gone";

const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

/// Largest copy accepted. Real copies are a few KB (a URI list plus headers);
/// a Firestore document caps out at 1 MiB. Anything bigger is a panel bug, and
/// uploading it would only fail at Google.
pub const MAX_COPY_BYTES: usize = 512 * 1024;

/// `0x01 | nonce (12) | ciphertext | tag (16)`: the smallest possible copy.
pub const MIN_COPY_BYTES: usize = 1 + 12 + 16;

/// Version byte of the encrypted copy format.
pub const COPY_VERSION: u8 = 0x01;

/// One rendered, encrypted copy.
#[derive(Clone, PartialEq, Eq)]
pub struct Copy {
    pub blob: Vec<u8>,
    /// Lowercase hex sha256 of the plaintext minus its volatile parts.
    pub sha: String,
}

impl std::fmt::Debug for Copy {
    // The blob is user data; only its size and sha go to a log.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Copy({} bytes, sha {})", self.blob.len(), self.sha)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PanelAnswer {
    Copy(Copy),
    /// 404 with `x-copy-gone: 1`: delete both copies and the row.
    Gone,
    /// 404 WITHOUT `x-copy-gone: 1`: not the panel's answer. Per-row error;
    /// never deletes anything.
    UnmarkedNotFound,
    /// 503.
    Disabled,
    /// Connect failure, or a 502/504 from something in front of it: the
    /// panel is restarting. Not the row's fault, so no row is charged.
    Unreachable(String),
    /// 401 or 403.
    Unauthorized(u16),
    /// Any other answer. Per-row.
    Error(String),
}

/// Map a non-200 answer. `gone_header` is the value of [`GONE_HEADER`].
pub fn classify_status(status: u16, gone_header: Option<&str>) -> PanelAnswer {
    match status {
        404 if gone_header.map(str::trim) == Some("1") => PanelAnswer::Gone,
        404 => PanelAnswer::UnmarkedNotFound,
        503 => PanelAnswer::Disabled,
        502 | 504 => PanelAnswer::Unreachable(format!("panel: HTTP {status}")),
        401 | 403 => PanelAnswer::Unauthorized(status),
        _ => PanelAnswer::Error(format!("panel: HTTP {status}")),
    }
}

/// No HTTP answer. Only a connect failure means the panel is down for
/// everyone (restarting); a timeout or a dropped connection after connecting
/// is charged to this row, so one slow user cannot stall the queue head.
fn transport_answer(e: &reqwest::Error) -> PanelAnswer {
    let what = format!("panel: no answer ({})", transport_kind(e));
    if e.is_connect() {
        PanelAnswer::Unreachable(what)
    } else {
        PanelAnswer::Error(what)
    }
}

/// Check what a 200 carried. A malformed copy is never uploaded over a good
/// one.
pub fn validate_copy(blob: Vec<u8>, sha_header: Option<&str>) -> Result<Copy, String> {
    let sha = sha_header.map(str::trim).unwrap_or("");
    if sha.len() != 64 || !sha.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("panel: missing or malformed x-copy-sha".to_string());
    }
    if blob.len() < MIN_COPY_BYTES || blob[0] != COPY_VERSION {
        return Err(format!("panel: malformed copy ({} bytes)", blob.len()));
    }
    Ok(Copy {
        blob,
        sha: sha.to_ascii_lowercase(),
    })
}

/// HTTP client for the panel endpoint. Deliberately not `Debug`.
pub struct PanelClient {
    http: reqwest::Client,
    base: String,
    secret: String,
}

impl PanelClient {
    pub fn new(base: &str, secret: &str) -> Result<PanelClient, reqwest::Error> {
        PanelClient::with_timeout(base, secret, REQUEST_TIMEOUT)
    }

    /// [`PanelClient::new`] with another request timeout (tests).
    pub fn with_timeout(
        base: &str,
        secret: &str,
        timeout: Duration,
    ) -> Result<PanelClient, reqwest::Error> {
        let http = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(CONNECT_TIMEOUT)
            .timeout(timeout)
            .user_agent(concat!("marzban-google-sync/", env!("CARGO_PKG_VERSION")))
            .build()?;
        Ok(PanelClient {
            http,
            base: base.trim_end_matches('/').to_string(),
            secret: secret.to_string(),
        })
    }

    pub async fn fetch(&self, user_id: i32) -> PanelAnswer {
        let url = format!("{}/internal/micky-copy/{user_id}", self.base);
        let mut resp = match self.http.get(&url).bearer_auth(&self.secret).send().await {
            Ok(r) => r,
            Err(e) => return transport_answer(&e),
        };
        let status = resp.status().as_u16();
        if status != 200 {
            let gone = resp
                .headers()
                .get(GONE_HEADER)
                .and_then(|v| v.to_str().ok());
            return classify_status(status, gone);
        }
        if resp
            .content_length()
            .is_some_and(|n| n > MAX_COPY_BYTES as u64)
        {
            return PanelAnswer::Error("panel: copy larger than 512 KiB".to_string());
        }
        let sha = resp
            .headers()
            .get("x-copy-sha")
            .and_then(|v| v.to_str().ok())
            .map(str::to_string);
        let blob = match read_capped(&mut resp, MAX_COPY_BYTES + 1).await {
            Ok(b) if b.len() > MAX_COPY_BYTES => {
                return PanelAnswer::Error("panel: copy larger than 512 KiB".to_string())
            }
            Ok(b) => b,
            Err(e) => {
                return PanelAnswer::Error(format!(
                    "panel: body read failed ({})",
                    transport_kind(&e)
                ))
            }
        };
        match validate_copy(blob, sha.as_deref()) {
            Ok(copy) => PanelAnswer::Copy(copy),
            Err(e) => PanelAnswer::Error(e),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn blob(n: usize) -> Vec<u8> {
        let mut b = vec![0u8; n];
        if n > 0 {
            b[0] = COPY_VERSION;
        }
        b
    }

    const SHA: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    #[test]
    fn accepts_a_well_formed_copy() {
        let c = validate_copy(blob(29), Some(SHA)).unwrap();
        assert_eq!(c.sha, SHA);
        let upper = SHA.to_ascii_uppercase();
        assert_eq!(validate_copy(blob(64), Some(&upper)).unwrap().sha, SHA);
    }

    #[test]
    fn refuses_bad_sha_headers() {
        assert!(validate_copy(blob(64), None).is_err());
        assert!(validate_copy(blob(64), Some("abc")).is_err());
        assert!(validate_copy(blob(64), Some(&"g".repeat(64))).is_err());
    }

    #[test]
    fn refuses_short_or_unversioned_blobs() {
        assert!(validate_copy(blob(28), Some(SHA)).is_err());
        assert!(validate_copy(Vec::new(), Some(SHA)).is_err());
        let mut b = blob(64);
        b[0] = 0x02;
        assert!(validate_copy(b, Some(SHA)).is_err());
    }

    #[test]
    fn only_a_marked_404_means_gone() {
        assert_eq!(classify_status(404, Some("1")), PanelAnswer::Gone);
        assert_eq!(classify_status(404, Some(" 1 ")), PanelAnswer::Gone);
        assert_eq!(classify_status(404, None), PanelAnswer::UnmarkedNotFound);
        assert_eq!(
            classify_status(404, Some("0")),
            PanelAnswer::UnmarkedNotFound
        );
        assert_eq!(
            classify_status(404, Some("true")),
            PanelAnswer::UnmarkedNotFound
        );
        assert_eq!(
            classify_status(404, Some("")),
            PanelAnswer::UnmarkedNotFound
        );
        // The header means nothing on any other status.
        assert_eq!(
            classify_status(500, Some("1")),
            PanelAnswer::Error("panel: HTTP 500".into())
        );
    }

    #[test]
    fn other_statuses() {
        assert_eq!(classify_status(503, None), PanelAnswer::Disabled);
        assert_eq!(classify_status(401, None), PanelAnswer::Unauthorized(401));
        assert_eq!(classify_status(403, None), PanelAnswer::Unauthorized(403));
        assert_eq!(
            classify_status(502, None),
            PanelAnswer::Unreachable("panel: HTTP 502".into())
        );
        assert_eq!(
            classify_status(504, Some("1")),
            PanelAnswer::Unreachable("panel: HTTP 504".into())
        );
        assert_eq!(
            classify_status(500, None),
            PanelAnswer::Error("panel: HTTP 500".into())
        );
        assert_eq!(
            classify_status(302, None),
            PanelAnswer::Error("panel: HTTP 302".into())
        );
    }

    /// A one-shot local "panel" on a plain thread: accepts one connection,
    /// reads the request, then does `answer` with the socket.
    fn fake_panel(answer: impl FnOnce(std::net::TcpStream) + Send + 'static) -> String {
        use std::io::Read;
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        std::thread::spawn(move || {
            if let Ok((mut sock, _)) = listener.accept() {
                let mut buf = [0u8; 4096];
                let _ = sock.read(&mut buf);
                answer(sock);
            }
        });
        format!("http://{addr}")
    }

    fn write(mut sock: std::net::TcpStream, bytes: &[u8]) {
        use std::io::Write;
        let _ = sock.write_all(bytes);
        let _ = sock.flush();
    }

    #[tokio::test]
    async fn connect_failure_is_unreachable() {
        // Bind then drop: the port is closed, the connect is refused.
        let port = {
            let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            l.local_addr().unwrap().port()
        };
        let c = PanelClient::new(&format!("http://127.0.0.1:{port}"), "s").unwrap();
        assert!(
            matches!(c.fetch(1).await, PanelAnswer::Unreachable(ref m) if m.contains("connect")),
            "refused connect"
        );
    }

    #[tokio::test]
    async fn a_timeout_is_charged_to_the_row() {
        let base = fake_panel(|sock| {
            std::thread::sleep(Duration::from_secs(3));
            drop(sock);
        });
        let c = PanelClient::with_timeout(&base, "s", Duration::from_millis(300)).unwrap();
        assert_eq!(
            c.fetch(1).await,
            PanelAnswer::Error("panel: no answer (timeout)".into())
        );
    }

    #[tokio::test]
    async fn a_body_cut_off_is_charged_to_the_row() {
        let base = fake_panel(|sock| {
            let head = format!(
                "HTTP/1.1 200 OK\r\ncontent-type: application/octet-stream\r\nx-copy-sha: {SHA}\r\ncontent-length: 500\r\n\r\n"
            );
            let mut bytes = head.into_bytes();
            bytes.extend_from_slice(&[1u8; 40]);
            write(sock, &bytes); // then close: 460 bytes short
        });
        let c = PanelClient::new(&base, "s").unwrap();
        assert!(
            matches!(c.fetch(1).await, PanelAnswer::Error(ref m) if m.starts_with("panel: ")),
            "cut-off body"
        );
    }

    #[tokio::test]
    async fn gateway_errors_are_unreachable() {
        for code in ["502 Bad Gateway", "504 Gateway Timeout"] {
            let resp = format!("HTTP/1.1 {code}\r\ncontent-length: 0\r\n\r\n");
            let base = fake_panel(move |sock| write(sock, resp.as_bytes()));
            let c = PanelClient::new(&base, "s").unwrap();
            assert!(
                matches!(c.fetch(1).await, PanelAnswer::Unreachable(_)),
                "{code}"
            );
        }
    }

    #[test]
    fn debug_never_prints_the_blob() {
        let c = validate_copy(
            vec![1, 0xAB, 0xCD, 0xEF]
                .into_iter()
                .chain([7u8; 40])
                .collect(),
            Some(SHA),
        )
        .unwrap();
        let s = format!("{c:?}");
        assert!(s.starts_with("Copy(44 bytes"), "{s}");
        assert!(!s.contains("171"), "{s}");
    }
}

//! `marzban-google-sync`: keeps the encrypted per-user "Google copies" of Micky
//! subscriptions in step with the panel.
//!
//! * **G1** — a Firestore document `copies/{firestore_doc_id}`, field `blob`.
//! * **G2** — a Drive file `{drive_file_id}`, shared "anyone with the link".
//!
//! The panel decides WHAT goes into a copy and WHEN it changes (it sets
//! `micky_copies.dirty_at` in the same transaction as the change), and it
//! renders and encrypts the copy on request. This service does all the Google
//! I/O, outside the panel process: it finds rows that need work, fetches the
//! copy from the panel, writes it to both stores, deletes the copies of users
//! who are gone, and retries, paces and pauses. See `sync.rs` for the loop and
//! `docs/micky-protocol.md` ("Google copies") in the app repo for the contract
//! the stored bytes must meet.
//!
//! ```text
//!   store.rs      micky_copies reads/updates (MySQL; never selects content_key)
//!   lock.rs       single-instance MySQL advisory lock
//!   panel.rs      GET /internal/micky-copy/{user_id}
//!   auth.rs       service-account JWT (Firestore) + refresh token (Drive)
//!   firestore.rs  G1 write / delete
//!   drive.rs      G2 generateIds / create / upload / share / delete
//!   sync.rs       the per-row decision flow and the loop
//!   error.rs      account-wide vs per-row failures, backoff
//!   settings.rs   env wiring
//! ```

pub mod auth;
pub mod drive;
pub mod error;
pub mod firestore;
pub mod lock;
pub mod panel;
pub mod settings;
pub mod store;
pub mod sync;

use std::time::{Duration, Instant};

use tokio::sync::Mutex;

use auth::{read_capped, Authenticator, Grant, ServiceAccount};
use error::Failure;

const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

/// Minimum spacing between two Drive calls. Drive throttles bursts per user
/// account; every Drive call of this service goes through one account.
pub const DRIVE_MIN_INTERVAL: Duration = Duration::from_millis(200);

/// Largest Google answer read. Answers are small JSON (`{"id": …}`); this only
/// bounds an error page.
const MAX_GOOGLE_BODY: usize = 64 * 1024;

/// Ids that go into Google URL paths and, through the panel, into the app's
/// `micky-g1`/`micky-g2` headers: `[A-Za-z0-9_-]{1,128}`.
pub fn valid_copy_id(s: &str) -> bool {
    drive::valid_file_id(s)
}

/// Every Google call. Deliberately not `Debug`: it holds tokens.
pub struct GoogleClient {
    http: reqwest::Client,
    project: String,
    firestore_auth: Authenticator,
    drive_auth: Authenticator,
    /// When the last Drive call started, for [`DRIVE_MIN_INTERVAL`].
    last_drive_call: Mutex<Option<Instant>>,
}

impl GoogleClient {
    pub fn new(
        project: &str,
        service_account: ServiceAccount,
        drive_client_id: &str,
        drive_client_secret: &str,
        drive_refresh_token: &str,
    ) -> Result<GoogleClient, reqwest::Error> {
        let http = reqwest::Client::builder()
            // Tokens travel in every request: never over plain HTTP.
            .https_only(true)
            .connect_timeout(CONNECT_TIMEOUT)
            .timeout(REQUEST_TIMEOUT)
            .user_agent(concat!("marzban-google-sync/", env!("CARGO_PKG_VERSION")))
            .build()?;
        Ok(GoogleClient {
            http,
            project: project.to_string(),
            firestore_auth: Authenticator::new(
                "firestore token",
                Grant::ServiceAccount(Box::new(service_account)),
            ),
            drive_auth: Authenticator::new(
                "drive token",
                Grant::RefreshToken {
                    client_id: drive_client_id.to_string(),
                    client_secret: drive_client_secret.to_string(),
                    refresh_token: drive_refresh_token.to_string(),
                },
            ),
            last_drive_call: Mutex::new(None),
        })
    }

    /// Send a request with a bearer token. A 401 drops the cached token and
    /// retries ONCE with a fresh one (a token can be revoked or expire early);
    /// a second 401 is returned to the caller, which classifies it as
    /// account-wide. Returns the status and up to 64 KiB of body.
    async fn send_authed<F>(
        &self,
        auth: &Authenticator,
        what: &str,
        build: F,
    ) -> Result<(u16, Vec<u8>), Failure>
    where
        F: Fn(&str) -> reqwest::RequestBuilder,
    {
        let mut refreshed = false;
        loop {
            let token = auth.token(&self.http).await?;
            let mut resp = build(&token)
                .send()
                .await
                .map_err(|e| Failure::no_answer(what, &e))?;
            let status = resp.status().as_u16();
            if status == 401 && !refreshed {
                auth.invalidate().await;
                refreshed = true;
                continue;
            }
            let body = read_capped(&mut resp, MAX_GOOGLE_BODY)
                .await
                .map_err(|e| Failure::no_answer(what, &e))?;
            return Ok((status, body));
        }
    }

    fn status_failure(&self, what: &str, status: u16, body: &[u8]) -> Failure {
        Failure::from_google(what, status, body)
    }

    /// Wait until [`DRIVE_MIN_INTERVAL`] has passed since the previous Drive
    /// call started.
    async fn drive_pace(&self) {
        let mut last = self.last_drive_call.lock().await;
        if let Some(prev) = *last {
            let since = prev.elapsed();
            if since < DRIVE_MIN_INTERVAL {
                tokio::time::sleep(DRIVE_MIN_INTERVAL - since).await;
            }
        }
        *last = Some(Instant::now());
    }
}

/// The real Google stores behind the loop's [`sync::Google`] seam.
impl sync::Google for GoogleClient {
    async fn firestore_write(&self, doc_id: &str, blob: &[u8]) -> Result<(), Failure> {
        check_id(doc_id, "firestore doc id")?;
        GoogleClient::firestore_write(self, doc_id, blob).await
    }

    async fn firestore_delete(&self, doc_id: &str) -> Result<(), Failure> {
        check_id(doc_id, "firestore doc id")?;
        GoogleClient::firestore_delete(self, doc_id).await
    }

    async fn drive_generate_id(&self) -> Result<String, Failure> {
        GoogleClient::drive_generate_id(self).await
    }

    async fn drive_create(&self, file_id: &str) -> Result<drive::Upload, Failure> {
        check_id(file_id, "drive file id")?;
        GoogleClient::drive_create(self, file_id).await
    }

    async fn drive_upload(&self, file_id: &str, blob: &[u8]) -> Result<drive::Upload, Failure> {
        check_id(file_id, "drive file id")?;
        GoogleClient::drive_upload(self, file_id, blob).await
    }

    async fn drive_share(&self, file_id: &str) -> Result<(), Failure> {
        check_id(file_id, "drive file id")?;
        GoogleClient::drive_share(self, file_id).await
    }

    async fn drive_delete(&self, file_id: &str) -> Result<(), Failure> {
        check_id(file_id, "drive file id")?;
        GoogleClient::drive_delete(self, file_id).await
    }
}

/// An id read from the row goes into a URL path; refuse anything that is not
/// a plain id (a hand-edited row must not become a request to another path).
fn check_id(id: &str, what: &str) -> Result<(), Failure> {
    if valid_copy_id(id) {
        Ok(())
    } else {
        Err(Failure::row(format!("{what} on the row is malformed")))
    }
}

/// The real panel behind the loop's [`sync::Panel`] seam.
impl sync::Panel for panel::PanelClient {
    async fn fetch_copy(&self, user_id: i32) -> panel::PanelAnswer {
        self.fetch(user_id).await
    }
}

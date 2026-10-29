//! G1: one Firestore document per user, `copies/{doc_id}`, field `blob`.
//!
//! Phones read it without login (`GET …/copies/<id>?key=<api key>`) and decode
//! `fields.blob.bytesValue` as STANDARD base64 with padding — the format the
//! REST API itself uses for `bytesValue`. The Firestore rules allow `get` only;
//! every write goes through this service's service account.

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use serde_json::json;

use super::error::Failure;
use super::GoogleClient;

pub const FIRESTORE_API: &str = "https://firestore.googleapis.com/v1";

/// The collection the app reads (`docs/micky-protocol.md`, "Where the copies
/// live").
pub const COLLECTION: &str = "copies";

/// URL of one copy document.
pub fn doc_url(project: &str, doc_id: &str) -> String {
    format!(
        "{FIRESTORE_API}/projects/{project}/databases/(default)/documents/{COLLECTION}/{doc_id}"
    )
}

/// URL of a create-or-replace of the `blob` field. `updateMask` keeps the
/// write to that one field; without a `currentDocument` precondition PATCH
/// creates a missing document, so the first write and every later one are the
/// same request. `mask.fieldPaths=__name__` keeps the answer to the document
/// name instead of echoing the whole blob back.
pub fn write_url(project: &str, doc_id: &str) -> String {
    format!(
        "{}?updateMask.fieldPaths=blob&mask.fieldPaths=__name__",
        doc_url(project, doc_id)
    )
}

/// The PATCH body: `bytesValue` is STANDARD base64 WITH padding.
pub fn write_body(blob: &[u8]) -> serde_json::Value {
    json!({"fields": {"blob": {"bytesValue": STANDARD.encode(blob)}}})
}

/// A new document id: 32 random lowercase hex characters (128 bits). It is
/// half of a capability URL, so it must not be guessable.
pub fn new_doc_id() -> String {
    let bytes: [u8; 16] = rand::random();
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

impl GoogleClient {
    /// Create or replace `copies/{doc_id}`.
    pub async fn firestore_write(&self, doc_id: &str, blob: &[u8]) -> Result<(), Failure> {
        let url = write_url(&self.project, doc_id);
        let body = write_body(blob);
        let (status, resp) = self
            .send_authed(&self.firestore_auth, "firestore write", |token| {
                self.http.patch(&url).bearer_auth(token).json(&body)
            })
            .await?;
        match status {
            200..=299 => Ok(()),
            _ => Err(self.status_failure("firestore write", status, &resp)),
        }
    }

    /// Delete `copies/{doc_id}`. Already gone counts as done.
    pub async fn firestore_delete(&self, doc_id: &str) -> Result<(), Failure> {
        let url = doc_url(&self.project, doc_id);
        let (status, resp) = self
            .send_authed(&self.firestore_auth, "firestore delete", |token| {
                self.http.delete(&url).bearer_auth(token)
            })
            .await?;
        match status {
            200..=299 | 404 => Ok(()),
            _ => Err(self.status_failure("firestore delete", status, &resp)),
        }
    }

    /// Read `blob` back with the service account. `None` = no such document.
    /// The loop never reads; this exists for the live test.
    pub async fn firestore_read(&self, doc_id: &str) -> Result<Option<Vec<u8>>, Failure> {
        let url = doc_url(&self.project, doc_id);
        let (status, resp) = self
            .send_authed(&self.firestore_auth, "firestore read", |token| {
                self.http.get(&url).bearer_auth(token)
            })
            .await?;
        match status {
            200 => {
                let v: serde_json::Value = serde_json::from_slice(&resp)
                    .map_err(|_| Failure::row("firestore read: not JSON"))?;
                let b64 = v["fields"]["blob"]["bytesValue"]
                    .as_str()
                    .ok_or_else(|| Failure::row("firestore read: no blob field"))?;
                STANDARD
                    .decode(b64)
                    .map(Some)
                    .map_err(|_| Failure::row("firestore read: blob is not base64"))
            }
            404 => Ok(None),
            _ => Err(self.status_failure("firestore read", status, &resp)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn doc_id_is_32_lowercase_hex() {
        for _ in 0..200 {
            let id = new_doc_id();
            assert_eq!(id.len(), 32, "{id}");
            assert!(
                id.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
                "{id}"
            );
        }
        assert_ne!(new_doc_id(), new_doc_id());
    }

    #[test]
    fn body_is_standard_padded_base64() {
        // 1 and 2 bytes force padding; 0xfb 0xff force '+' and '/', which a
        // URL-safe alphabet would turn into '-' and '_'.
        assert_eq!(
            write_body(&[0x01]),
            json!({"fields": {"blob": {"bytesValue": "AQ=="}}})
        );
        assert_eq!(
            write_body(&[0xfb, 0xff]),
            json!({"fields": {"blob": {"bytesValue": "+/8="}}})
        );
        assert_eq!(
            write_body(&[0xfb, 0xff, 0xbf]),
            json!({"fields": {"blob": {"bytesValue": "+/+/"}}})
        );
    }

    #[test]
    fn urls_match_the_app_contract() {
        assert_eq!(
            doc_url("micky-sub", "abc"),
            "https://firestore.googleapis.com/v1/projects/micky-sub/databases/(default)/documents/copies/abc"
        );
        assert_eq!(
            write_url("micky-sub", "abc"),
            "https://firestore.googleapis.com/v1/projects/micky-sub/databases/(default)/documents/copies/abc?updateMask.fieldPaths=blob&mask.fieldPaths=__name__"
        );
    }
}

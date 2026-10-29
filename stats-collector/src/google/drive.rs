//! G2: one Drive file per user, owned by a dedicated Google account and shared
//! "anyone with the link can view".
//!
//! ## Ids first
//!
//! The file id is reserved with `generateIds` and SAVED on the row before the
//! file is created with that id. A crash between create and the row update can
//! therefore never leave an orphan file nobody knows the id of: the next
//! attempt finds the id on the row, its create answers 409 ("exists"), and it
//! carries on with the upload and the share.
//!
//! ## Trashed files
//!
//! A file moved to the trash by hand still accepts uploads (200), but nobody
//! can download it any more. Create and upload therefore ask for
//! `fields=id,trashed`, and a trashed file is treated exactly like a deleted
//! one: a new id, then create + upload + share, and the trashed one is deleted
//! for good (best effort).

use serde_json::json;

use super::error::Failure;
use super::GoogleClient;

pub const DRIVE_API: &str = "https://www.googleapis.com/drive/v3";
pub const DRIVE_UPLOAD_API: &str = "https://www.googleapis.com/upload/drive/v3";

/// What a create or an upload found out about the file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Upload {
    Done,
    /// 404 (upload only): the file was deleted by hand. The caller reserves a
    /// new id.
    NotFound,
    /// The answer says `"trashed": true`: the file is in the trash, so the
    /// public link no longer serves it. Handled like `NotFound`, plus the old
    /// file is deleted for good.
    Trashed,
}

/// `"trashed": true` in a create/upload answer (`fields=id,trashed`).
pub fn is_trashed(resp: &[u8]) -> bool {
    serde_json::from_slice::<serde_json::Value>(resp)
        .map(|v| v["trashed"].as_bool() == Some(true))
        .unwrap_or(false)
}

/// The app accepts `micky-g2` values matching `[A-Za-z0-9_-]{1,128}`; an id
/// outside that would make the panel's header unusable, so it is refused here.
pub fn valid_file_id(s: &str) -> bool {
    (1..=128).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

/// Metadata for creating the file under a reserved id. The name is random:
/// anyone holding the link sees it, so it must say nothing about the user.
pub fn create_body(file_id: &str) -> serde_json::Value {
    let bytes: [u8; 8] = rand::random();
    let name: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    json!({
        "id": file_id,
        "name": format!("{name}.bin"),
        "mimeType": "application/octet-stream",
    })
}

impl GoogleClient {
    /// Reserve one file id.
    pub async fn drive_generate_id(&self) -> Result<String, Failure> {
        let url = format!("{DRIVE_API}/files/generateIds?count=1&space=drive&type=files");
        self.drive_pace().await;
        let (status, resp) = self
            .send_authed(&self.drive_auth, "drive generateIds", |token| {
                self.http.get(&url).bearer_auth(token)
            })
            .await?;
        if status != 200 {
            return Err(self.status_failure("drive generateIds", status, &resp));
        }
        let v: serde_json::Value = serde_json::from_slice(&resp)
            .map_err(|_| Failure::row("drive generateIds: not JSON"))?;
        match v["ids"][0].as_str() {
            Some(id) if valid_file_id(id) => Ok(id.to_string()),
            _ => Err(Failure::row(
                "drive generateIds: no usable id in the answer",
            )),
        }
    }

    /// Create the (empty) file under a reserved id. 409 = it already exists
    /// from an earlier, interrupted attempt = fine (the upload that follows
    /// reports whether that file is trashed). Never `NotFound`.
    pub async fn drive_create(&self, file_id: &str) -> Result<Upload, Failure> {
        let url = format!("{DRIVE_API}/files?fields=id,trashed");
        let body = create_body(file_id);
        self.drive_pace().await;
        let (status, resp) = self
            .send_authed(&self.drive_auth, "drive create", |token| {
                self.http.post(&url).bearer_auth(token).json(&body)
            })
            .await?;
        match status {
            200..=299 if is_trashed(&resp) => Ok(Upload::Trashed),
            200..=299 | 409 => Ok(Upload::Done),
            _ => Err(self.status_failure("drive create", status, &resp)),
        }
    }

    /// Replace the file's content.
    pub async fn drive_upload(&self, file_id: &str, blob: &[u8]) -> Result<Upload, Failure> {
        let url = format!("{DRIVE_UPLOAD_API}/files/{file_id}?uploadType=media&fields=id,trashed");
        self.drive_pace().await;
        let (status, resp) = self
            .send_authed(&self.drive_auth, "drive upload", |token| {
                self.http
                    .patch(&url)
                    .bearer_auth(token)
                    .header(reqwest::header::CONTENT_TYPE, "application/octet-stream")
                    .body(blob.to_vec())
            })
            .await?;
        match status {
            200..=299 if is_trashed(&resp) => Ok(Upload::Trashed),
            200..=299 => Ok(Upload::Done),
            404 => Ok(Upload::NotFound),
            _ => Err(self.status_failure("drive upload", status, &resp)),
        }
    }

    /// Move a file to the trash, the way a person would in the Drive UI. The
    /// loop never trashes; this exists for the live test.
    pub async fn drive_trash(&self, file_id: &str) -> Result<(), Failure> {
        let url = format!("{DRIVE_API}/files/{file_id}?fields=id,trashed");
        let body = json!({"trashed": true});
        self.drive_pace().await;
        let (status, resp) = self
            .send_authed(&self.drive_auth, "drive trash", |token| {
                self.http.patch(&url).bearer_auth(token).json(&body)
            })
            .await?;
        match status {
            200..=299 => Ok(()),
            _ => Err(self.status_failure("drive trash", status, &resp)),
        }
    }

    /// Share "anyone with the link, reader". Re-adding it is harmless.
    pub async fn drive_share(&self, file_id: &str) -> Result<(), Failure> {
        let url = format!("{DRIVE_API}/files/{file_id}/permissions?fields=id");
        let body = json!({"type": "anyone", "role": "reader"});
        self.drive_pace().await;
        let (status, resp) = self
            .send_authed(&self.drive_auth, "drive share", |token| {
                self.http.post(&url).bearer_auth(token).json(&body)
            })
            .await?;
        match status {
            200..=299 => Ok(()),
            _ => Err(self.status_failure("drive share", status, &resp)),
        }
    }

    /// Delete the file for good (not to the trash). Already gone = done.
    pub async fn drive_delete(&self, file_id: &str) -> Result<(), Failure> {
        let url = format!("{DRIVE_API}/files/{file_id}");
        self.drive_pace().await;
        let (status, resp) = self
            .send_authed(&self.drive_auth, "drive delete", |token| {
                self.http.delete(&url).bearer_auth(token)
            })
            .await?;
        match status {
            200..=299 | 404 => Ok(()),
            _ => Err(self.status_failure("drive delete", status, &resp)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn file_ids_follow_the_app_header_rule() {
        assert!(valid_file_id("1AbC-d_9"));
        assert!(valid_file_id(&"a".repeat(128)));
        assert!(!valid_file_id(""));
        assert!(!valid_file_id(&"a".repeat(129)));
        assert!(!valid_file_id("a/b"));
        assert!(!valid_file_id("a b"));
        assert!(!valid_file_id("a?b"));
    }

    #[test]
    fn trashed_flag_is_read_from_the_answer() {
        assert!(is_trashed(br#"{"id":"1x","trashed":true}"#));
        assert!(!is_trashed(br#"{"id":"1x","trashed":false}"#));
        assert!(!is_trashed(br#"{"id":"1x"}"#));
        assert!(!is_trashed(b""));
        assert!(!is_trashed(b"not json"));
    }

    #[test]
    fn create_body_uses_the_reserved_id_and_a_random_name() {
        let a = create_body("1xyz");
        assert_eq!(a["id"], "1xyz");
        assert_eq!(a["mimeType"], "application/octet-stream");
        let name = a["name"].as_str().unwrap();
        assert_eq!(name.len(), 16 + ".bin".len(), "{name}");
        assert!(name.ends_with(".bin"));
        assert!(name[..16]
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()));
        assert_ne!(create_body("1xyz")["name"], a["name"]);
    }
}

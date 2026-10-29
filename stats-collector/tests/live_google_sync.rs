//! Live test of every Google call `marzban-google-sync` makes.
//!
//! `#[ignore]`d by default: it needs real credentials and writes to the real
//! Firestore project and Drive account, so it must never run as part of an
//! ordinary `cargo test`. Run it deliberately, with the same variables the
//! service reads plus the app's public API key:
//!
//! ```text
//! FIRESTORE_PROJECT=… FIRESTORE_SERVICE_ACCOUNT=/path/to/service-account.json \
//! GDRIVE_CLIENT_ID=… GDRIVE_CLIENT_SECRET=… GDRIVE_REFRESH_TOKEN=… \
//! GOOGLE_API_KEY=… \
//!   cargo test --test live_google_sync -- --ignored --nocapture
//! ```
//!
//! It creates one Firestore document and one Drive file, reads both back the
//! way a phone does (API key only, no login), checks that an upload to a
//! trashed file is reported as such, and deletes both. Cleanup runs
//! whatever happened before it. Nothing secret is printed: only statuses and
//! the ids of the throwaway objects.

use marzban_stats_collector::google::auth::ServiceAccount;
use marzban_stats_collector::google::drive::{Upload, DRIVE_API};
use marzban_stats_collector::google::firestore::{doc_url, new_doc_id};
use marzban_stats_collector::google::{valid_copy_id, GoogleClient};

use base64::engine::general_purpose::STANDARD;
use base64::Engine;

fn env_or_skip(key: &str) -> Option<String> {
    match std::env::var(key) {
        Ok(v) if !v.trim().is_empty() => Some(v.trim().to_string()),
        _ => {
            eprintln!("skipping: {key} is not set");
            None
        }
    }
}

/// A blob shaped like a real copy: version byte, then ~6 KB of noise.
fn fake_copy() -> Vec<u8> {
    let mut blob = vec![0x01u8];
    blob.extend((0..6000).map(|_| rand::random::<u8>()));
    blob
}

#[tokio::test]
#[ignore = "writes to the real Firestore project and Drive account"]
async fn every_google_call_works_and_cleans_up() {
    let vars = [
        "FIRESTORE_PROJECT",
        "FIRESTORE_SERVICE_ACCOUNT",
        "GDRIVE_CLIENT_ID",
        "GDRIVE_CLIENT_SECRET",
        "GDRIVE_REFRESH_TOKEN",
        "GOOGLE_API_KEY",
    ];
    let values: Vec<Option<String>> = vars.iter().map(|k| env_or_skip(k)).collect();
    if values.iter().any(Option::is_none) {
        return;
    }
    let v: Vec<String> = values.into_iter().flatten().collect();
    let (project, sa_path, cid, csecret, refresh, api_key) =
        (&v[0], &v[1], &v[2], &v[3], &v[4], &v[5]);

    let sa = match ServiceAccount::load(sa_path) {
        Ok(sa) => sa,
        Err(e) => panic!("service account: {e}"),
    };
    let google = GoogleClient::new(project, sa, cid, csecret, refresh).expect("http client");
    let anon = reqwest::Client::new();

    // ------------------------------------------------------------- G1 ----
    let doc_id = new_doc_id();
    let blob = fake_copy();
    println!("G1 test doc {doc_id}");

    let g1: Result<(), String> = async {
        google
            .firestore_write(&doc_id, &blob)
            .await
            .map_err(|f| f.message)?;
        println!("  PATCH (create) ok");
        let back = google
            .firestore_read(&doc_id)
            .await
            .map_err(|f| f.message)?;
        if back.as_deref() != Some(&blob[..]) {
            return Err("service-account read-back differs".into());
        }
        println!("  read back with the service account: bytes match");

        // Overwrite: the same PATCH replaces the field.
        let blob2 = fake_copy();
        google
            .firestore_write(&doc_id, &blob2)
            .await
            .map_err(|f| f.message)?;
        // Phone-style read: API key only, standard padded base64.
        let resp = anon
            .get(format!("{}?key={api_key}", doc_url(project, &doc_id)))
            .send()
            .await
            .map_err(|_| "phone-style read: no answer".to_string())?;
        let status = resp.status().as_u16();
        let json: serde_json::Value = resp
            .json()
            .await
            .map_err(|_| format!("phone-style read: HTTP {status}, not JSON"))?;
        let got = json["fields"]["blob"]["bytesValue"]
            .as_str()
            .and_then(|s| STANDARD.decode(s).ok());
        if got.as_deref() != Some(&blob2[..]) {
            return Err(format!("phone-style read: HTTP {status}, bytes differ"));
        }
        println!("  PATCH (replace) ok; read with the API key only: bytes match");
        Ok(())
    }
    .await;

    // Cleanup, whatever happened above.
    let g1_delete = google.firestore_delete(&doc_id).await;
    let g1_gone = google.firestore_read(&doc_id).await;
    let g1_delete_again = google.firestore_delete(&doc_id).await;
    println!(
        "  DELETE: {:?}; read after delete: {:?}; DELETE again (404 = ok): {:?}",
        g1_delete.as_ref().map_err(|f| &f.message),
        g1_gone
            .as_ref()
            .map(|b| b.as_ref().map(Vec::len))
            .map_err(|f| &f.message),
        g1_delete_again.as_ref().map_err(|f| &f.message),
    );

    // ------------------------------------------------------------- G2 ----
    let mut file_id: Option<String> = None;
    let g2: Result<(), String> = async {
        let id = google.drive_generate_id().await.map_err(|f| f.message)?;
        if !valid_copy_id(&id) {
            return Err("generateIds returned an id the app would refuse".into());
        }
        file_id = Some(id.clone());
        println!("G2 test file {id}");
        google.drive_create(&id).await.map_err(|f| f.message)?;
        println!("  create with the reserved id ok");
        google
            .drive_create(&id)
            .await
            .map_err(|f| format!("second create (expect 409 = ok): {}", f.message))?;
        println!("  create again: 409 tolerated");
        match google
            .drive_upload(&id, &blob)
            .await
            .map_err(|f| f.message)?
        {
            Upload::Done => println!("  upload ok"),
            other => return Err(format!("upload right after create: {other:?}")),
        }
        google.drive_share(&id).await.map_err(|f| f.message)?;
        println!("  share anyone/reader ok");

        let resp = anon
            .get(format!("{DRIVE_API}/files/{id}?alt=media&key={api_key}"))
            .send()
            .await
            .map_err(|_| "phone-style download: no answer".to_string())?;
        let status = resp.status().as_u16();
        let bytes = resp
            .bytes()
            .await
            .map_err(|_| format!("phone-style download: HTTP {status}, body failed"))?;
        if status != 200 || bytes.as_ref() != &blob[..] {
            return Err(format!(
                "phone-style download: HTTP {status}, {} bytes, match={}",
                bytes.len(),
                bytes.as_ref() == &blob[..]
            ));
        }
        println!("  downloaded with the API key only (no login): bytes match");

        // Trashed by hand: the upload still answers 200, so only the
        // `trashed` field can tell.
        google.drive_trash(&id).await.map_err(|f| f.message)?;
        match google
            .drive_upload(&id, &blob)
            .await
            .map_err(|f| f.message)?
        {
            Upload::Trashed => println!("  upload to a trashed file reports Trashed"),
            other => return Err(format!("upload to a trashed file: {other:?}, want Trashed")),
        }
        Ok(())
    }
    .await;

    let mut g2_cleanup: Result<(), String> = Ok(());
    if let Some(id) = &file_id {
        let del = google.drive_delete(id).await;
        let upload_after = google.drive_upload(id, &blob).await;
        let del_again = google.drive_delete(id).await;
        println!(
            "  DELETE: {:?}; upload after delete: {:?} (want NotFound); DELETE again (404 = ok): {:?}",
            del.as_ref().map_err(|f| &f.message),
            upload_after.as_ref().map_err(|f| &f.message),
            del_again.as_ref().map_err(|f| &f.message),
        );
        g2_cleanup = match (del, upload_after, del_again) {
            (Ok(()), Ok(Upload::NotFound), Ok(())) => Ok(()),
            _ => Err(format!("cleanup of Drive file {id} did not go as expected")),
        };
    }

    // Assert only now: cleanup above has already run.
    g1.expect("G1 flow");
    g1_delete.expect("G1 delete");
    assert_eq!(g1_gone.expect("G1 read after delete"), None);
    g1_delete_again.expect("G1 delete of a missing doc counts as done");
    g2.expect("G2 flow");
    g2_cleanup.expect("G2 cleanup");
}
